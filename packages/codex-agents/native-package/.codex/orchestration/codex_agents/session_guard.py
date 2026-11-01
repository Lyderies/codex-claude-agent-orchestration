"""Which folders the calling Codex session can write, read from Codex's own record.

The owner's command rule runs the primary-host commands outside the sandbox for
every Codex session on the machine. That is safe only while no session can write
the folders that rule depends on: the home folder and its ancestors, `~/.codex`
(the rule and the protected copy), the orchestrator clone and the trusted
installation folders. Codex 0.164 records each session in
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
    TRUSTED_INSTALL_ROOTS,
    HostPathError,
    _component_problem,
    _open_verified_directory,
    account_home,
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


def protected_folders(copy: Path | None, clone: Path | None) -> list[tuple[str, list[Chain]]]:
    """The folders no session may write, inside or around, with their reason keys.

    The copy and the clone are taken as recorded at install (lexically) and as
    they resolve now.
    """

    home = account_home()
    folders = [("codex_home", _forms(home / ".codex"))]
    if copy is not None:
        folders.append(("copy", _forms(copy)))
    if clone is not None:
        folders.append(("clone", _forms(clone)))
    folders.extend(("tools", _forms(Path(root))) for root in TRUSTED_INSTALL_ROOTS)
    return folders


def writable_problem(path: Path, protected: list[tuple[str, list[Chain]]]) -> str | None:
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
                # Inside a hidden top-level folder: ~/.codex or an installation
                # such as ~/.local.
                return "codex_home" if written[len(home)][1] == ".codex" else "tools"
        for key, chains in protected:
            for folder in chains:
                if _prefix(written, folder) or _prefix(folder, written):
                    return key
    return None


def check_record(record: SessionRecord, protected: list[tuple[str, list[Chain]]]) -> None:
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
        key = writable_problem(path, protected)
        if key is not None:
            raise SessionRefused(REASONS[key])


def check_current_session(copy: Path | None, clone: Path | None) -> None:
    """The guard every primary-host command the rule runs passes first."""

    record = find_session(os.environ.get("CODEX_THREAD_ID"))
    check_record(record, protected_folders(copy, clone))


def session_guard_status(copy: Path | None, clone: Path | None) -> str:
    """``ok``, or the plain reason the guard would refuse this session."""

    try:
        check_current_session(copy, clone)
    except SessionRefused as exc:
        return str(exc)
    return "ok"


def scan_recent_sessions(
    clone: Path, codex_home: Path, home: Path, now: float | None = None
) -> list[dict[str, str]]:
    """Recent sessions that could write the clone, the home folder or a folder above it.

    For the wizard's warning, with the same parsing as the guard: records in
    `<codex_home>/sessions` changed in the last SCAN_DAYS days, at most
    MAX_SCANNED_RECORDS, newest day folders first. A record it cannot read is
    skipped; this is a warning, not the guard, so it follows the folders Codex
    itself uses in this environment.
    """

    now = time.time() if now is None else now
    # The home Codex uses here, and the account's own home besides; both as written
    # and as they resolve, compared like the guard compares them.
    homes = [*_forms(home), *_forms(account_home())]
    clones = _forms(clone)
    findings: list[dict[str, str]] = []
    try:
        sessions = _sessions_folder(Path(os.path.realpath(codex_home)) / "sessions")
    except SessionRefused:
        return findings
    if sessions is None:
        return findings
    read = 0
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
                        kind = "home"
                    elif any(
                        _prefix(written, held) or _prefix(held, written)
                        for written in forms for held in clones
                    ):
                        kind = "clone"
                    else:
                        continue
                    found.append({"date": record.day, "folder": str(path), "holds": kind})
                if any(kind in {"root", "home"} for kind in record.special_writes):
                    found.append({
                        "date": record.day, "folder": str(Path(os.path.normpath(home))),
                        "holds": "home",
                    })
                # The workspace is both an entry and a runtime root: one line each.
                findings.extend(item for index, item in enumerate(found) if item not in found[:index])
    except (OSError, SessionRefused):
        return findings
    finally:
        os.close(sessions)
    return findings
