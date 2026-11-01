"""Which folders the calling Codex session can write, read from Codex's own record.

The owner's command rule runs the primary-host commands outside the sandbox for
every Codex session on the machine. That is safe only while no session can write
the folders that rule depends on: the home folder and its ancestors, `~/.codex`
(the rule and the protected copy), the orchestrator clone, the trusted
installation folders, the owner's learning state and the host-private scratch
containers, nor a folder that contains the project (Codex runs a nested trusted
project's `.codex/config.toml` and hooks outside the sandbox) or the project's
own `.codex`, `.agents` or `.git`. Codex 0.164 records each session in
`~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread id>.jsonl` (JSON
lines): a `session_meta` record first, with `payload.cwd` and
`payload.runtime_workspace_roots`, then a `turn_context` record at every turn,
with `payload.sandbox_policy.type` and
`payload.file_system_sandbox_policy.entries`. Commands Codex runs carry
`CODEX_THREAD_ID`, and a command the rule runs is a fresh process whose
environment the model cannot set.

Only those two records are read, never conversation content: the first line, and
the last `turn_context`, read backwards in bounded steps. Anything unexpected
(no thread ID, no record or several, a shape this code does not know, a write
it cannot place) refuses. Messages are plain and carry no paths.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .host_paths import (
    BRIDGE_SCRATCH_CONTAINER,
    OWNER_SCRATCH_CONTAINER,
    TRUSTED_INSTALL_ROOTS,
    HostPathError,
    _component_problem,
    _open_verified_directory,
    account_home,
    default_state_root,
    host_scratch_base,
    legacy_state_root,
)

THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ROLLOUT_NAME = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(" + THREAD_ID.pattern + r")\.jsonl"
)
SANDBOXED_TYPES = frozenset({"workspace-write", "read-only"})
# What Codex's `slash_tmp` writable entry names.
SLASH_TMP = "/tmp"
UNSANDBOXED_TYPES = frozenset({"danger-full-access"})
# Day folders searched for a session's record, newest first, and entries read per folder.
MAX_DAY_FOLDERS = 400
MAX_FOLDER_ENTRIES = 20_000
# The first record carries the session's instructions, so it may be long.
MAX_FIRST_RECORD_BYTES = 8 * 1_048_576
# How far back from the end the last turn_context is looked for.
MAX_TAIL_BYTES = 32 * 1_048_576
TAIL_BLOCK = 256 * 1024
# The wizard's scan: records changed in the last 14 days, at most this many.
SCAN_DAYS = 14
MAX_SCANNED_RECORDS = 200
# Inside a project, Codex keeps these read-only at the top of a writable root; a
# session may not be given them as writable folders of their own.
PROJECT_CONTROL_FOLDERS = (".codex", ".agents", ".git")


class SessionRefused(RuntimeError):
    """The calling session fails the guard; the message is plain and path-free."""


NO_THREAD = (
    "This command did not come from a Codex session (CODEX_THREAD_ID is not set), so the "
    "orchestrator cannot check which folders the session can write"
)
NO_RECORD = (
    "Run Codex without --ephemeral so the orchestrator can check this session's folders "
    "(no record of this session was found)"
)
UNKNOWN_SHAPE = (
    "This Codex session's record is not in the format the orchestrator knows (Codex "
    "0.164), so it cannot check which folders the session can write"
)
REASONS = {
    "root": "This Codex session can write to the whole disk (/); open Codex on the project "
            "folder instead",
    "home": "This Codex session can write to your home folder; open Codex on the project "
            "folder instead",
    "above_home": "This Codex session can write to a folder that holds your home folder; "
                  "open Codex on the project folder instead",
    "codex_home": "This Codex session can write to ~/.codex, where the Codex rule and the "
                  "orchestrator's protected copy live; open Codex on the project folder instead",
    "copy": "This Codex session can write to the orchestrator's protected copy; open Codex "
            "on the project folder instead",
    "clone": "This Codex session can write to the orchestrator's setup folder; open Codex on "
             "the project folder instead",
    "tools": "This Codex session can write to a folder where trusted tools are installed "
             "(such as ~/.local or /opt/homebrew); open Codex on the project folder instead",
    "state": "This Codex session can write to the orchestrator's learning state (its ledger "
             "and the owner's key); open Codex on the project folder instead",
    "scratch": "This Codex session can write to the orchestrator's private working folder; "
               "open Codex on the project folder instead",
    "project_parent": "This Codex session was opened on a folder that contains this project, "
                      "where it could change the project's Codex settings and hooks; open "
                      "Codex on the project folder itself",
    "project_control": "This Codex session can write to this project's .codex, .agents or "
                       ".git folder; open Codex on the project folder without adding them",
    "unsandboxed": "This Codex session runs without its sandbox (danger-full-access); open "
                   "Codex with its normal sandbox",
}


@dataclass(frozen=True)
class SessionRecord:
    """The two records the guard reads from one rollout file."""

    thread_id: str
    day: str
    sandbox_type: str
    writable: tuple[Path, ...]
    special_writes: tuple[str, ...]


def _shape(condition: bool) -> None:
    if not condition:
        raise SessionRefused(UNKNOWN_SHAPE)


def _open_dir_at(parent: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    if _component_problem(os.fstat(descriptor)) is not None:
        os.close(descriptor)
        raise SessionRefused(UNKNOWN_SHAPE)
    return descriptor


def _names(descriptor: int, pattern: re.Pattern[str]) -> list[str]:
    names = []
    with os.scandir(descriptor) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_FOLDER_ENTRIES:
                break
            if pattern.fullmatch(entry.name):
                names.append(entry.name)
    return sorted(names, reverse=True)


def _sessions_folder(sessions: Path) -> int | None:
    """A descriptor for a `sessions` folder, every component checked; None when absent."""

    if not os.path.lexists(sessions):
        return None
    try:
        descriptor, canonical = _open_verified_directory(sessions)
    except (HostPathError, OSError) as exc:
        raise SessionRefused(UNKNOWN_SHAPE) from exc
    if canonical != sessions:
        os.close(descriptor)
        raise SessionRefused(UNKNOWN_SHAPE)
    return descriptor


def _day_folders(sessions: int) -> Iterable[tuple[str, int]]:
    """Yield (YYYY-MM-DD, descriptor) for day folders, newest first, bounded."""

    seen = 0
    for year in _names(sessions, re.compile(r"\d{4}")):
        year_fd = _open_dir_at(sessions, year)
        try:
            for month in _names(year_fd, re.compile(r"\d{2}")):
                month_fd = _open_dir_at(year_fd, month)
                try:
                    for day in _names(month_fd, re.compile(r"\d{2}")):
                        if seen >= MAX_DAY_FOLDERS:
                            return
                        seen += 1
                        day_fd = _open_dir_at(month_fd, day)
                        try:
                            yield f"{year}-{month}-{day}", day_fd
                        finally:
                            os.close(day_fd)
                finally:
                    os.close(month_fd)
        finally:
            os.close(year_fd)


def _open_record(folder: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or _component_problem(info) is not None:
        os.close(descriptor)
        raise SessionRefused(UNKNOWN_SHAPE)
    return descriptor


def _first_record(descriptor: int) -> dict:
    chunks, size = [], 0
    while size <= MAX_FIRST_RECORD_BYTES:
        chunk = os.pread(descriptor, 65536, size)
        if not chunk:
            break
        end = chunk.find(b"\n")
        if end >= 0:
            chunks.append(chunk[:end])
            break
        chunks.append(chunk)
        size += len(chunk)
    else:
        raise SessionRefused(UNKNOWN_SHAPE)
    return _record(b"".join(chunks))


def _record(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError) as exc:
        raise SessionRefused(UNKNOWN_SHAPE) from exc
    _shape(isinstance(value, dict) and isinstance(value.get("payload"), dict))
    return value


def _last_turn_context(descriptor: int) -> dict:
    """The file's last `turn_context` record, read backwards in bounded blocks."""

    position = os.fstat(descriptor).st_size
    scanned = 0
    carry = b""
    while position > 0 and scanned < MAX_TAIL_BYTES:
        step = min(TAIL_BLOCK, position)
        position -= step
        scanned += step
        lines = (os.pread(descriptor, step, position) + carry).split(b"\n")
        carry = lines[0]
        for line in reversed(lines[1:]):
            found = _turn_context(line)
            if found is not None:
                return found
    if position == 0:
        found = _turn_context(carry)
        if found is not None:
            return found
    raise SessionRefused(UNKNOWN_SHAPE)


def _turn_context(line: bytes) -> dict | None:
    if b"turn_context" not in line:
        return None
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("type") == "turn_context":
        _shape(isinstance(value.get("payload"), dict))
        return value
    return None


def _absolute(value: object) -> Path:
    """A recorded absolute path, kept as written (normalised, no symlink followed)."""

    _shape(isinstance(value, str) and os.path.isabs(value) and "\x00" not in value)
    return Path(os.path.normpath(value))


def _read_session(folder: int, name: str, thread_id: str, day: str) -> SessionRecord:
    descriptor = _open_record(folder, name)
    try:
        meta = _first_record(descriptor)
        context = _last_turn_context(descriptor)
    finally:
        os.close(descriptor)
    _shape(meta.get("type") == "session_meta")
    payload = meta["payload"]
    _shape("id" not in payload or payload["id"] == thread_id)
    _absolute(payload.get("cwd"))
    roots = payload.get("runtime_workspace_roots")
    _shape(isinstance(roots, list))
    workspace_roots = [_absolute(root) for root in roots]
    special: list[str] = []
    turn = context["payload"]
    policy = turn.get("sandbox_policy")
    _shape(isinstance(policy, dict) and isinstance(policy.get("type"), str))
    sandbox_type = policy["type"]
    # A workspace root is writable in every mode but read-only, where only the
    # policy's explicit write entries count.
    writable = [] if sandbox_type == "read-only" else list(workspace_roots)
    files = turn.get("file_system_sandbox_policy")
    _shape(isinstance(files, dict) and isinstance(files.get("entries"), list))
    for entry in files["entries"]:
        _shape(isinstance(entry, dict) and isinstance(entry.get("access"), str))
        target = entry.get("path")
        _shape(isinstance(target, dict) and isinstance(target.get("type"), str))
        if entry["access"] != "write":
            continue
        if target["type"] == "path":
            writable.append(_absolute(target.get("path")))
            continue
        _shape(target["type"] == "special" and isinstance(target.get("value"), dict))
        kind = target["value"].get("kind")
        _shape(isinstance(kind, str))
        if kind == "slash_tmp":
            writable.append(Path(os.path.normpath(SLASH_TMP)))
        elif kind == "tmpdir":
            writable.append(Path(os.path.normpath(
                os.environ.get("TMPDIR") or tempfile.gettempdir()
            )))
        else:
            # The root, the home folder or anything else this code cannot place.
            special.append(kind)
    return SessionRecord(thread_id, day, sandbox_type, tuple(writable), tuple(special))


def find_session(thread_id: str | None) -> SessionRecord:
    """The record of the session `thread_id` names; refused unless exactly one exists."""

    if not thread_id:
        raise SessionRefused(NO_THREAD)
    if not THREAD_ID.fullmatch(thread_id):
        raise SessionRefused("CODEX_THREAD_ID is not a Codex session ID")
    # The account's own ~/.codex, from the account database, never HOME.
    sessions = _sessions_folder(account_home() / ".codex" / "sessions")
    if sessions is None:
        raise SessionRefused(NO_RECORD)
    matches: list[SessionRecord] = []
    try:
        for day, folder in _day_folders(sessions):
            for name in _names(folder, ROLLOUT_NAME):
                if ROLLOUT_NAME.fullmatch(name).group(1) == thread_id:
                    matches.append(_read_session(folder, name, thread_id, day))
    except OSError as exc:
        raise SessionRefused(UNKNOWN_SHAPE) from exc
    finally:
        os.close(sessions)
    if not matches:
        raise SessionRefused(NO_RECORD)
    if len(matches) > 1:
        raise SessionRefused(
            "Several Codex session records match this session, so the orchestrator cannot "
            "tell which folders it can write"
        )
    return matches[0]


# One path component: its file-system identity (device, inode) when it exists, and its
# case-folded name.
Component = tuple[tuple[int, int] | None, str]
Chain = list[Component]


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _chain(path: Path) -> Chain:
    """Every component of an absolute path from `/` down, with its identity."""

    current = Path(path.anchor)
    chain: Chain = [(_identity(current), "")]
    for part in path.parts[1:]:
        current = current / part
        chain.append((_identity(current), part.casefold()))
    return chain


def _same(left: Component, right: Component) -> bool:
    """One folder, judged by identity when both exist, else by case-folded name.

    A case-insensitive volume gives `Work` and `work` one identity, and a
    missing component is compared by name ignoring case: when unsure, the
    components count as the same, so the guard refuses.
    """

    if left[0] is not None and right[0] is not None:
        return left[0] == right[0]
    return left[1] == right[1]


def _prefix(short: Chain, long: Chain) -> bool:
    return len(short) <= len(long) and all(map(_same, short, long))


def _forms(path: Path) -> list[Chain]:
    """The path as written (no symlink followed) and, when it differs, as it resolves now.

    A protected path is protected under both: a folder renamed and replaced by
    a symlink still blocks a session on the folder that held it.
    """

    lexical = Path(os.path.normpath(path))
    real = Path(os.path.realpath(lexical))
    return [_chain(lexical)] + ([_chain(real)] if real != lexical else [])


def state_folders() -> list[Path]:
    """The owner's learning state as the commands use it (XDG_STATE_HOME or the
    account's `~/.local/state`), from this module's account home.

    An XDG_STATE_HOME outside a hidden home folder, or an unsafe folder above
    the state, refuses here as it does for every command.
    """

    try:
        return [default_state_root(account_home())]
    except HostPathError as exc:
        raise SessionRefused(str(exc)) from None


def scratch_folders() -> list[Path]:
    """The host-private scratch containers (bridge and owner tools), when available.

    The learning state's own `scratch` lies inside the state folder.
    """

    base = host_scratch_base()
    if base is None:
        return []
    return [Path(base) / BRIDGE_SCRATCH_CONTAINER, Path(base) / OWNER_SCRATCH_CONTAINER]


def protected_folders(copy: Path | None, clone: Path | None) -> list[tuple[str, list[Chain]]]:
    """The folders no session may write, inside or around, with their reason keys.

    The copy and the clone are taken as recorded at install (lexically) and as
    they resolve now; so are the learning state and the scratch containers.
    """

    home = account_home()
    folders = [("codex_home", _forms(home / ".codex"))]
    if copy is not None:
        folders.append(("copy", _forms(copy)))
    if clone is not None:
        folders.append(("clone", _forms(clone)))
    folders.extend(("state", _forms(folder)) for folder in state_folders())
    folders.extend(("scratch", _forms(folder)) for folder in scratch_folders())
    folders.extend(("tools", _forms(Path(root))) for root in TRUSTED_INSTALL_ROOTS)
    return folders


def _project_problem(written: Chain, project: Path) -> str | None:
    """A folder that strictly contains the project, or its `.codex`, `.agents` or
    `.git` (or a folder inside one) as a writable folder of its own.

    The project itself stays allowed: Codex keeps those three read-only at the
    top of every writable root (the record lists them as read entries).
    """

    for held in _forms(project):
        if len(written) < len(held) and _prefix(written, held):
            return "project_parent"
    for name in PROJECT_CONTROL_FOLDERS:
        for held in _forms(project / name):
            if _prefix(held, written):
                return "project_control"
    return None


def writable_problem(
    path: Path, protected: list[tuple[str, list[Chain]]], project: Path | None = None
) -> str | None:
    """The reason key when a session able to write `path` defeats the rule, or None.

    Containment is decided component by component on file-system identity and
    case-folded names, for the path as written and as it resolves, so neither
    a symlink nor a different spelling on a case-insensitive volume hides it.
    """

    homes = _forms(account_home())
    for written in _forms(path):
        if len(written) == 1:
            return "root"
        for home in homes:
            if _prefix(written, home):
                return "home" if len(written) == len(home) else "above_home"
        for home in homes:
            if _prefix(home, written) and written[len(home)][1].startswith("."):
                # Inside a hidden top-level folder: ~/.codex, an installation such
                # as ~/.local, or the default learning state under ~/.local/state.
                return "codex_home" if written[len(home)][1] == ".codex" else "tools"
        for key, chains in protected:
            for folder in chains:
                if _prefix(written, folder) or _prefix(folder, written):
                    return key
        if project is not None:
            key = _project_problem(written, project)
            if key is not None:
                return key
    return None


def check_record(
    record: SessionRecord,
    protected: list[tuple[str, list[Chain]]],
    project: Path | None = None,
) -> None:
    """Refuse unless the session is sandboxed and can write none of the protected folders."""

    if record.sandbox_type in UNSANDBOXED_TYPES:
        raise SessionRefused(REASONS["unsandboxed"])
    if record.sandbox_type not in SANDBOXED_TYPES:
        raise SessionRefused(UNKNOWN_SHAPE)
    for kind in record.special_writes:
        if kind == "root":
            raise SessionRefused(REASONS["root"])
        if kind == "home":
            raise SessionRefused(REASONS["home"])
        raise SessionRefused(UNKNOWN_SHAPE)
    for path in record.writable:
        key = writable_problem(path, protected, project)
        if key is not None:
            raise SessionRefused(REASONS[key])


def check_current_session(
    copy: Path | None, clone: Path | None, project: Path | None = None
) -> None:
    """The guard every primary-host command the rule runs passes first.

    `project` is the copy's bound project: no writable folder may strictly
    contain it or be its `.codex`, `.agents` or `.git`.
    """

    record = find_session(os.environ.get("CODEX_THREAD_ID"))
    check_record(record, protected_folders(copy, clone), project)


def session_guard_status(
    copy: Path | None, clone: Path | None, project: Path | None = None
) -> str:
    """``ok``, or the plain reason the guard would refuse this session."""

    try:
        check_current_session(copy, clone, project)
    except SessionRefused as exc:
        return str(exc)
    return "ok"


# The wizard's warning for each finding of codex-session-scan.
SCAN_MESSAGES = {
    "home": "Codex was opened on a folder that holds your home folder; open Codex on a "
            "project folder only",
    "clone": "Codex was opened on a folder that holds this setup folder; open Codex on a "
             "project folder only",
    "state": "Codex was opened on a folder that holds the orchestrator's learning state; "
             "open Codex on a project folder only",
    "project": "Codex was opened on a folder that contains a set-up project ({name}); open "
               "Codex on the project itself",
}
# Runtime copies (host_commands.RUNTIME_CONTAINER) and their bindings name the
# projects set up with the Codex commands rule.
RUNTIME_CONTAINER = "agent-orchestration"
BINDING_PARTS = (".codex", "orchestration", "runtime-binding.json")
MAX_SETUP_PROJECTS = 256


def _read_small(parent: int, name: str, limit: int = 65_536) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file")
        return os.read(descriptor, limit + 1)[:limit]
    finally:
        os.close(descriptor)


def bound_projects(codex_home: Path) -> list[Path]:
    """The projects named by runtime-copy bindings under `<codex_home>/agent-orchestration`.

    For the wizard's warning only: every folder is opened without following
    symlinks, and anything unreadable is skipped.
    """

    projects: list[Path] = []
    try:
        container = os.open(
            Path(os.path.realpath(codex_home)) / RUNTIME_CONTAINER,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError:
        return projects
    try:
        for digest in _names(container, re.compile(r"[0-9a-f]{1,64}")):
            try:
                digest_fd = os.open(
                    digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=container
                )
            except OSError:
                continue
            try:
                for runtime in _names(digest_fd, re.compile(r"[0-9a-f]{64}")):
                    if len(projects) >= MAX_SETUP_PROJECTS:
                        return projects
                    descriptor = digest_fd
                    opened: list[int] = []
                    try:
                        for part in (runtime, *BINDING_PARTS[:-1]):
                            descriptor = os.open(
                                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor,
                            )
                            opened.append(descriptor)
                        binding = json.loads(_read_small(descriptor, BINDING_PARTS[-1]))
                        root = binding["project_root"]
                        if isinstance(root, str) and os.path.isabs(root):
                            projects.append(Path(os.path.normpath(root)))
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                    finally:
                        for item in opened:
                            os.close(item)
            finally:
                os.close(digest_fd)
    except OSError:
        return projects
    finally:
        os.close(container)
    return projects


def scan_recent_sessions(
    clone: Path,
    codex_home: Path,
    home: Path,
    now: float | None = None,
    *,
    projects: Iterable[Path] = (),
) -> list[dict[str, str]]:
    """Recent sessions that could write the clone, the home folder or a folder above
    it, the learning state, or a folder that contains a set-up project.

    For the wizard's warning, with the same parsing as the guard: records in
    `<codex_home>/sessions` changed in the last SCAN_DAYS days, at most
    MAX_SCANNED_RECORDS, newest day folders first. A record it cannot read is
    skipped; this is a warning, not the guard, so it follows the folders Codex
    itself uses in this environment. The set-up projects are `projects` and
    those the runtime-copy bindings name; the learning state is the default
    folder and, while it exists, the pre-0.8.9 macOS one. Each finding has a
    plain `message`.
    """

    now = time.time() if now is None else now
    # The home Codex uses here, and the account's own home besides; both as written
    # and as they resolve, compared like the guard compares them.
    homes = [*_forms(home), *_forms(account_home())]
    clones = _forms(clone)
    try:
        state = [default_state_root(account_home())]
    except HostPathError:
        state = []
    legacy = legacy_state_root()
    if legacy is not None and os.path.lexists(legacy):
        state.append(legacy)
    states = [chain for folder in state for chain in _forms(folder)]
    setup: list[tuple[str, list[Chain]]] = []
    seen: set[Path] = set()
    for project in [*projects, *bound_projects(codex_home)]:
        project = Path(os.path.normpath(project))
        if project.is_absolute() and project not in seen:
            seen.add(project)
            setup.append((project.name, _forms(project)))
    findings: list[dict[str, str]] = []
    try:
        sessions = _sessions_folder(Path(os.path.realpath(codex_home)) / "sessions")
    except SessionRefused:
        return findings
    if sessions is None:
        return findings
    read = 0

    def finding(day: str, folder: Path, kind: str, name: str = "") -> dict[str, str]:
        return {
            "date": day, "folder": str(folder), "holds": kind,
            "message": SCAN_MESSAGES[kind].format(name=name),
        }

    try:
        for day, folder in _day_folders(sessions):
            for name in _names(folder, ROLLOUT_NAME):
                if read >= MAX_SCANNED_RECORDS:
                    return findings
                try:
                    info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                    if now - info.st_mtime > SCAN_DAYS * 86400:
                        continue
                    read += 1
                    record = _read_session(
                        folder, name, ROLLOUT_NAME.fullmatch(name).group(1), day
                    )
                except (SessionRefused, OSError):
                    continue
                found: list[dict[str, str]] = []
                for path in record.writable:
                    forms = _forms(path)
                    if any(_prefix(written, held) for written in forms for held in homes):
                        found.append(finding(record.day, path, "home"))
                        continue
                    if any(
                        _prefix(written, held) or _prefix(held, written)
                        for written in forms for held in clones
                    ):
                        found.append(finding(record.day, path, "clone"))
                    if any(
                        _prefix(written, held) or _prefix(held, written)
                        for written in forms for held in states
                    ):
                        found.append(finding(record.day, path, "state"))
                    for project, chains in setup:
                        if any(
                            len(written) < len(held) and _prefix(written, held)
                            for written in forms for held in chains
                        ):
                            found.append(finding(record.day, path, "project", project))
                if any(kind in {"root", "home"} for kind in record.special_writes):
                    found.append(finding(record.day, Path(os.path.normpath(home)), "home"))
                # The workspace is both an entry and a runtime root: one line each.
                findings.extend(item for index, item in enumerate(found) if item not in found[:index])
    except (OSError, SessionRefused):
        return findings
    finally:
        os.close(sessions)
    return findings
