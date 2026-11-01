#!/usr/bin/env python3
"""Install, verify or remove the Codex and Claude agent packages in one Git project.

Preview is the default; pass --apply to change files. Each package keeps its own
installer, namespace and manifest. Both packages preview first, and nothing is applied
unless both previews succeed.

    install.py install --target PATH [--apply] [--project-agent-routing FILE]
                       [--operational-checks FILE] [--replace-project-config]
    install.py verify  --target PATH
    install.py remove  --target PATH [--apply]

Install also merges Claude Code permission deny rules into the project's
.claude/settings.json, so a Claude primary cannot run the orchestrator's owner-only commands
or edit the orchestration, Claude settings or Git folders. Remove takes out only the
entries install added, and first removes the Codex commands rule with this repository's own
``remove-codex-rule``. Owner commands always run this repository's orchestrator, never the
project's copy; Git runs from a trusted real binary with hooks, signing and maintenance off,
and porcelain commands only while the repository's settings name no helper program. Run it
isolated: ``python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py``.
"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX_INSTALLER = ROOT / "packages" / "codex-agents" / "scripts" / "install.py"
CLAUDE_INSTALLER = ROOT / "packages" / "claude-agents" / "scripts" / "package.py"
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
MANAGED_PATHS = (".codex", ".agents", ".claude")
CODEX_MANIFEST = Path(".codex") / "orchestration" / "install-manifest.json"
CLAUDE_SETTINGS = ".claude/settings.json"
# Where an earlier version kept its ownership record, inside the project. The record now
# lives in the owner's learning state (OwnerState); the old file is read once to migrate.
LEGACY_RECORD = ".claude/agent-orchestration-permissions.json"
OWNER_RECORD = "owner-record"  # the plan's key for the record in the owner's state
# Git on the project runs only plumbing (ls-files, ls-tree, rev-parse, hash-object,
# update-index, write-tree, commit-tree, update-ref): no status, add or commit, so no clean
# or smudge filter, textconv, hook, signing program, pager or maintenance can run whatever
# the project's configuration says. These options switch the helpers off as well, every
# transport is refused and lazy fetching is off, and a partial clone or promisor remote is
# refused (git_plumbing.repository_layout_problem) before any object is read.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false")
GIT_ENVIRONMENT = {"xcrun_nocache": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
# Repository settings that name a program Git may start (setup.sh uses the same pattern):
# filters, diff and merge drivers, transports, pagers, editors, signing and credential
# helpers, trailer commands, and includes, whose files cannot be vetted here.
GIT_HELPER_KEYS = (
    r"^(filter\..+\.(clean|smudge|process)|diff\.external|diff\..+\.(command|textconv)"
    r"|merge\..+\.driver|core\.(sshcommand|pager|editor|askpass|gitproxy|alternaterefscommand)"
    r"|gpg\.(.+\.)?program|gpg\.ssh\.defaultkeycommand|credential\.(.+\.)?helper|pager\..+"
    r"|sequence\.editor|include\.path|includeif\..+\.path|trailer\..+\.(command|cmd))$"
)
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "REBASE_HEAD": "a rebase",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase or am",
    "sequencer": "a cherry-pick or revert sequence",
    "BISECT_LOG": "a bisect",
}
# How to undo a package step that was applied before a later package failed.
REVERSE = {"install": "remove", "remove": "install"}
# Owner commands a Claude primary may never run, whatever this checkout's parser lists.
ALWAYS_OWNER_ONLY = ("install-codex-rule", "remove-codex-rule")
# Paths a Claude primary may never edit with its file tools or recognised shell writes.
PROTECTED_EDITS = ("/.codex/**", "/.agents/**", "/.claude/settings*.json", "/.git/**")
CLI_PATTERN = "*.codex/orchestration/cli.py"
MAX_SETTINGS_BYTES = 1024 * 1024
# Every owner-only command an earlier release's installer denied (0.8.8 and 0.8.9). A recorded
# entry counts as the installer's own only when it is in the current or an earlier generated
# set, so a planted record can never make the installer delete a user's own rule.
HISTORICAL_OWNER_ONLY = (
    "learning-route-canary", "evaluate", "learning-validate", "learning-register", "learning-serve",
    "learning-backup-project", "learning-verify-backup", "learning-delete-project",
    "learning-normalize-app", "learning-ingest-app", "learning-compare",
    "learning-record-billing-attestation", "learning-prepare-upgrade-reconciliation",
    "learning-commit-upgrade-reconciliation", "learning-record-verified-outcome", "learning-expire",
    "learning-run-cycle", "learning-create-evaluation-assignment", "learning-record-trial",
    "learning-record-evidence-review", "learning-record-comparison", "learning-mine-case",
    "learning-review-case", "learning-analyze", "learning-generate-candidate",
    "learning-calibrate-grader", "learning-record-feedback", "learning-evaluate-candidate",
    "learning-start-canary", "learning-complete-canary", "learning-promote", "learning-record-drift",
    "learning-rollback", "learning-configure-owner-key", "learning-create-grant-request",
    "learning-attach-grant-signature", "host-command", "install-codex-rule", "remove-codex-rule",
)
HISTORICAL_PROTECTED_EDITS = (*PROTECTED_EDITS, "/" + LEGACY_RECORD)
_ATTEST_SPEC = importlib.util.spec_from_file_location("agent_orchestration_attest",
                                                      ROOT / "scripts" / "attest.py")
attest = importlib.util.module_from_spec(_ATTEST_SPEC)
_ATTEST_SPEC.loader.exec_module(attest)
# This repository's own runtime decides which git binary is real and trusted.
sys.path.insert(0, str(ORCHESTRATION))
from codex_agents.git_plumbing import git_executable, repository_layout_problem  # noqa: E402
# Files the Codex installer writes for one checkout only: the rendered learning hooks hold
# absolute paths to this checkout and its trusted Python. They are never committed: an
# untracked one is never a change, and a commit takes a tracked one (0.8.x committed the
# static hooks file) out of the index while the file itself stays.
from codex_agents.hook_config import LOCAL_FILES as LOCAL_ONLY  # noqa: E402


class InstallRefused(RuntimeError):
    """The target is not in a state where packages may be changed."""


def _git_binary(target: Path) -> str:
    """The real git binary at a fixed, trusted location (never macOS's xcrun shim), outside
    the project and every temporary folder."""

    found = git_executable([Path(os.path.realpath(target))])
    if found is None:
        raise InstallRefused("no trusted Git was found (macOS: the Command Line Tools or Homebrew's "
                             "git; Linux: /usr/bin/git)")
    return found


def _git_run(target: Path, *args: str, input: bytes | None = None, index: Path | None = None,
             text: bool = True) -> subprocess.CompletedProcess:
    """Run plumbing git on the project with every helper, transport and lazy fetch off."""

    env = {**os.environ, **GIT_ENVIRONMENT}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        [_git_binary(target), "-C", str(target), *GIT_SAFE, "--literal-pathspecs", *args],
        capture_output=True, text=text, input=input,
        stdin=subprocess.DEVNULL if input is None else None, env=env)


def _git(target: Path, *args: str) -> str:
    result = _git_run(target, *args)
    if result.returncode != 0:
        raise InstallRefused(f"target is not a usable Git working tree: {target}")
    return result.stdout.strip()


def _require_plain_repository(target: Path) -> None:
    """Refuse a repository Git could fetch into (a partial clone or promisor remote) or whose
    git directory lies inside its own working tree, before any object is read."""

    problem = repository_layout_problem(_git_binary(target), Path(os.path.realpath(target)))
    if problem is not None:
        raise InstallRefused(f"{problem}; setup does not run Git on it")


def _records(output: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in output.split(b"\0") if item]


def _blob_id(data: bytes, object_format: str) -> str:
    digest = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    return digest(b"blob %d\0" % len(data) + data).hexdigest()


def _worktree_entry(root_fd: int, relative: str) -> tuple[str, bytes] | None:
    """The working-tree file at `relative` as (mode, bytes), read without following any
    symlink and without any Git filter; None when it is absent, ("", b"") when it is not a
    file Git could hold (a folder, a device, a path through a symlinked folder)."""

    parts = relative.split("/")
    descriptor = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError:
                return "", b""
            os.close(descriptor)
            descriptor = child
        try:
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return "120000", os.fsencode(os.readlink(parts[-1], dir_fd=descriptor))
        if not stat.S_ISREG(info.st_mode):
            return "", b""
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            chunks = []
            while chunk := os.read(handle, 1 << 20):
                chunks.append(chunk)
        finally:
            os.close(handle)
        return ("100755" if info.st_mode & stat.S_IXUSR else "100644"), b"".join(chunks)
    finally:
        os.close(descriptor)


def _head(target: Path) -> str | None:
    result = _git_run(target, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def project_changes(target: Path, paths: list[str] | tuple[str, ...]) -> list[str]:
    """The paths under `paths` with uncommitted changes, found with plumbing only.

    A path counts when the index differs from HEAD (staged or conflicted), when the working
    tree's bytes or mode differ from the index (hashed here, never through a filter), when it
    is gone from the working tree, or when it is untracked and not ignored. This is what
    `git status` reports, without running anything the repository's settings name.
    """

    _require_plain_repository(target)
    object_format = _git(target, "rev-parse", "--show-object-format") or "sha1"
    filemode = _git_run(target, "config", "--bool", "core.fileMode").stdout.strip() != "false"
    head = _head(target)
    tracked: dict[str, tuple[str, str]] = {}
    if head is not None:
        listing = _git_run(target, "ls-tree", "-r", "-z", "--full-tree", head, "--", *paths, text=False)
        if listing.returncode != 0:
            raise InstallRefused("Git could not read the project's last commit")
        for record in _records(listing.stdout):
            meta, path = record.split("\t", 1)
            mode, _kind, oid = meta.split(" ")
            tracked[path] = (mode, oid)
    staged = _git_run(target, "ls-files", "-s", "-z", "--", *paths, text=False)
    untracked = _git_run(target, "ls-files", "-z", "--others", "--exclude-standard", "--", *paths, text=False)
    if staged.returncode != 0 or untracked.returncode != 0:
        raise InstallRefused("Git could not read the project's index")
    changed: set[str] = {path for path in _records(untracked.stdout) if path not in LOCAL_ONLY}
    index: dict[str, tuple[str, str]] = {}
    for record in _records(staged.stdout):
        meta, path = record.split("\t", 1)
        mode, oid, stage = meta.split(" ")
        if stage != "0":
            changed.add(path)
        else:
            index[path] = (mode, oid)
    changed.update(path for path in tracked if path not in index)
    root_fd = os.open(os.path.realpath(target), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for path, (mode, oid) in index.items():
            if tracked.get(path) != (mode, oid):
                changed.add(path)
            if mode == "160000":  # a submodule: only its recorded commit is compared
                continue
            entry = _worktree_entry(root_fd, path)
            if entry is None or not entry[0]:
                changed.add(path)
                continue
            same_mode = entry[0] == mode or (not filemode and "120000" not in (mode, entry[0]))
            if not same_mode or _blob_id(entry[1], object_format) != oid:
                changed.add(path)
    finally:
        os.close(root_fd)
    return sorted(changed)


def commit_paths(target: Path, message: str, detail: str, paths: list[str]) -> str:
    """Commit exactly `paths` as they are in the working tree, with plumbing only.

    A LOCAL_ONLY path is always committed as removed from the index; its file stays.
    Blobs are written with ``hash-object -w --no-filters``, a private index built from HEAD
    gets them with ``update-index --index-info``, then ``write-tree``, ``commit-tree``
    (never signed) and ``update-ref`` with the old value, so a branch that moved meanwhile
    refuses. The project's own index then records the same entries for those paths and
    keeps everything else it had staged. Returns "committed" or "nothing".
    """

    if not paths:
        raise InstallRefused("there is nothing to commit")
    _require_plain_repository(target)
    helpers = repository_helpers(target)  # defence in depth: none of these commands would run them
    if helpers:
        raise InstallRefused(helpers_refusal(helpers))
    object_format = _git(target, "rev-parse", "--show-object-format") or "sha1"
    zero = "0" * (64 if object_format == "sha256" else 40)
    head = _head(target)
    branch = _git_run(target, "symbolic-ref", "-q", "HEAD")
    reference = branch.stdout.strip() if branch.returncode == 0 else None
    try:
        scratch = attest.make_scratch("index-")
    except attest.AttestError as exc:
        raise InstallRefused(str(exc)) from None
    try:
        private_index = scratch / "index"
        seeded = _git_run(target, "read-tree", *((head,) if head else ("--empty",)), index=private_index)
        if seeded.returncode != 0:
            raise InstallRefused("Git could not read the project's last commit")
        records = []
        root_fd = os.open(os.path.realpath(target), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for path in paths:
                entry = None if path in LOCAL_ONLY else _worktree_entry(root_fd, path)
                if entry is None:
                    records.append(f"0 {zero}\t{path}\0")
                    continue
                if not entry[0]:
                    raise InstallRefused(f"{path} is not a file Git can commit")
                written = _git_run(target, "hash-object", "-w", "--no-filters", "--stdin",
                                   input=entry[1], text=False)
                if written.returncode != 0:
                    raise InstallRefused("Git could not store a file")
                records.append(f"{entry[0]} {written.stdout.decode().strip()}\t{path}\0")
        finally:
            os.close(root_fd)
        info = "".join(records).encode("utf-8", "surrogateescape")
        if _git_run(target, "update-index", "-z", "--index-info", input=info, index=private_index,
                    text=False).returncode != 0:
            raise InstallRefused("Git could not stage the files")
        tree = _git_run(target, "write-tree", index=private_index)
        if tree.returncode != 0:
            raise InstallRefused("Git could not write the commit's tree")
        tree_id = tree.stdout.strip()
        if head and _git(target, "rev-parse", f"{head}^{{tree}}") == tree_id:
            return "nothing"
        created = _git_run(target, "commit-tree", tree_id, *(("-p", head) if head else ()), "--no-gpg-sign",
                           "-m", message, "-m", detail)
        if created.returncode != 0:
            raise InstallRefused("Git refused the commit; the usual cause is that your Git identity "
                                 "(user.name and user.email) is not set")
        commit = created.stdout.strip()
        moved = _git_run(target, "update-ref", "-m", message, *(("--no-deref",) if reference is None else ()),
                         reference or "HEAD", commit, head or zero)
        if moved.returncode != 0:
            raise InstallRefused("the branch moved while setup ran, so nothing was committed")
        if _git_run(target, "update-index", "-z", "--index-info", input=info, text=False).returncode != 0:
            raise InstallRefused("the commit was made, but Git could not update the project's index; "
                                 "run git status and stage the committed files yourself")
        return "committed"
    finally:
        attest.remove_scratch(scratch)


def repository_helpers(target: Path) -> list[str]:
    """The repository's own settings that name a program Git could start, read with plumbing.

    Only the repository's configuration (and its worktree configuration, when enabled) is
    read, without following includes: an include is itself reported. The owner's own global
    and system settings are trusted.
    """

    scopes = ["--local"]
    enabled = _git_run(target, "config", "--local", "--no-includes", "--bool", "--get",
                       "extensions.worktreeConfig")
    if enabled.returncode == 0 and enabled.stdout.strip() == "true":
        scopes.append("--worktree")
    found: set[str] = set()
    for scope in scopes:
        result = _git_run(target, "config", scope, "--no-includes", "--name-only", "--get-regexp",
                          GIT_HELPER_KEYS)
        if result.returncode == 1:
            continue
        if result.returncode != 0:
            raise InstallRefused("the project's Git settings cannot be read, so setup will not run Git on it")
        found.update(re.sub(r"[\x00-\x1f\x7f]", "", line) for line in result.stdout.splitlines() if line)
    return sorted(found)


def helpers_refusal(helpers: list[str]) -> str:
    shown = ", ".join(helpers[:4]) + (" and more" if len(helpers) > 4 else "")
    return (f"the project's Git settings name programs Git would run ({shown}); setup does not run Git "
            "commands that could start them as you. Remove them from .git/config, or install and "
            "commit by hand")


def check_target(target: Path, *, mutating: bool) -> None:
    """Refuse targets that are mid-operation, conflicted, or dirty in managed paths."""

    top = Path(_git(target, "rev-parse", "--show-toplevel")).resolve()
    if top != target.resolve():
        raise InstallRefused(f"target must be the top of its working tree: {top}")
    git_dir = Path(_git(target, "rev-parse", "--absolute-git-dir"))
    for marker, operation in IN_PROGRESS.items():
        if (git_dir / marker).exists():
            raise InstallRefused(f"{operation} is in progress in the target; finish or abort it first")
    if _git(target, "ls-files", "--unmerged"):
        raise InstallRefused("the target has unmerged paths; resolve them first")
    if not mutating:
        return
    helpers = repository_helpers(target)
    if helpers:
        raise InstallRefused(helpers_refusal(helpers))
    if project_changes(target, MANAGED_PATHS):
        raise InstallRefused("managed paths (.codex, .agents, .claude) have uncommitted changes")


def _python() -> str:
    try:
        return attest.trusted_python()
    except attest.AttestError as exc:
        raise InstallRefused(str(exc)) from None


def _run(installer: Path, operation: str, target: Path, apply: bool, extra: list[str]) -> dict:
    command = [_python(), *attest.PYTHON_FLAGS, str(installer), operation, "--target", str(target), *extra]
    if apply:
        command.append("--apply")
    result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"error": (result.stderr or result.stdout).strip()[-2000:] or "no output"}
    if not isinstance(report, dict):
        report = {"error": "unexpected installer output"}
    report["exit_code"] = result.returncode
    return report


def _failed(report: dict) -> bool:
    return "error" in report or report.get("exit_code") == 2


# --- the owner-only command list ----------------------------------------------------------


def _parser_commands(cli: Path) -> list[str]:
    """Every subcommand cli.py's build_parser adds, read from the source, never run."""

    tree = ast.parse(cli.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_parser":
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "add_parser"):
                    first = call.args[0] if call.args else None
                    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                        raise InstallRefused("the orchestrator's command list could not be read")
                    names.append(first.value)
    return names


def _primary_commands(host_commands: Path) -> list[str]:
    tree = ast.parse(host_commands.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)
                and any(isinstance(t, ast.Name) and t.id == "PRIMARY_HOST_COMMANDS" for t in node.targets)):
            values = [item.value for item in node.value.elts if isinstance(item, ast.Constant)]
            if len(values) == len(node.value.elts) and all(isinstance(v, str) for v in values):
                return values
    raise InstallRefused("the orchestrator's command list could not be read")


def owner_only_commands(orchestration: Path = ORCHESTRATION) -> list[str]:
    """CLI subcommands minus the primary-host ones, plus the rule commands: never Claude's."""

    parsed = _parser_commands(orchestration / "cli.py")
    primary = _primary_commands(orchestration / "codex_agents" / "host_commands.py")
    if not parsed or not primary or not set(primary) <= set(parsed):
        raise InstallRefused("the orchestrator's command list could not be read")
    names = [name for name in parsed if name not in primary]
    names += [name for name in ALWAYS_OWNER_ONLY if name not in names]
    if any(not re.fullmatch(r"[a-z][a-z0-9-]*", name) for name in names):
        raise InstallRefused("the orchestrator's command list could not be read")
    return names


def deny_rules(orchestration: Path = ORCHESTRATION) -> list[str]:
    """The Claude Code permission deny rules install adds, in a stable order.

    Each owner command gets two Bash rules: the command alone and the command with
    arguments. The leading ``*`` covers ``python3``, ``python3 -I -B -S``, an absolute
    interpreter, ``./`` and absolute paths to the CLI alike. Deny rules are a policy layer
    on Claude Code's tools, not a sandbox: a command spelled another way, or a script that
    starts the CLI itself, is not matched.
    """

    return _rules_for(owner_only_commands(orchestration), PROTECTED_EDITS)


def _rules_for(commands, edits) -> list[str]:
    rules: list[str] = []
    for name in commands:
        rules += [f"Bash({CLI_PATTERN} {name})", f"Bash({CLI_PATTERN} {name} *)"]
    rules += [f"Edit({path})" for path in edits]
    return rules


def generated_rules(orchestration: Path = ORCHESTRATION) -> set[str]:
    """Every rule this installer or an earlier release's ever adds."""

    return {*deny_rules(orchestration), *_rules_for(HISTORICAL_OWNER_ONLY, HISTORICAL_PROTECTED_EDITS)}


# --- .claude/settings.json ---------------------------------------------------------------


class _Project:
    """The target folder pinned by a descriptor; files below it are opened without following
    any symlink, so a planted link cannot redirect a read or a write."""

    def __init__(self, target: Path) -> None:
        self.fd = os.open(os.path.realpath(target), os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        os.close(self.fd)

    def _parent(self, relative: str, *, create: bool) -> tuple[int, str]:
        parts = relative.split("/")
        descriptor = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except FileNotFoundError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            raise InstallRefused(f"{relative} has a symlinked or unusable folder") from exc
        return descriptor, parts[-1]

    def read(self, relative: str) -> bytes | None:
        try:
            parent, name = self._parent(relative, create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                raise InstallRefused(f"{relative} is a symlink or not a regular file")
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                data = os.read(handle, MAX_SETTINGS_BYTES + 1)
            finally:
                os.close(handle)
            if len(data) > MAX_SETTINGS_BYTES:
                raise InstallRefused(f"{relative} is too large to merge safely")
            return data
        finally:
            os.close(parent)

    def _current(self, parent: int, name: str) -> bytes | None:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return b"\0not a regular file"
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            return os.read(handle, MAX_SETTINGS_BYTES + 1)
        finally:
            os.close(handle)

    def publish(self, relative: str, content: bytes, before: bytes | None, staging: int | None) -> None:
        """Replace `relative` with `content`, unless it no longer holds `before`.

        The new file is written in the owner's staging folder (`staging`, a pinned descriptor
        outside the project) and renamed into place from there, so no agent can swap it
        between writing and publishing; the destination is compared with `before` right
        before the rename. Only when the staging folder is on another file system does the
        file go through a fresh, randomly named temporary file beside the destination.
        """

        parent, name = self._parent(relative, create=True)
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        source = None
        try:
            if staging is not None:
                _write_new(staging, temporary, content, 0o644)
                source = staging
            if self._current(parent, name) != before:
                raise InstallRefused(f"{relative} changed while setup ran, so it was not overwritten; "
                                     "run setup again")
            if source is not None:
                try:
                    os.rename(temporary, name, src_dir_fd=source, dst_dir_fd=parent)
                    return
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    os.unlink(temporary, dir_fd=source)
                    source = None
            _write_new(parent, temporary, content, 0o644)
            source = parent
            if self._current(parent, name) != before:
                raise InstallRefused(f"{relative} changed while setup ran, so it was not overwritten; "
                                     "run setup again")
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            source = None
        finally:
            if source is not None:
                try:
                    os.unlink(temporary, dir_fd=source)
                except OSError:
                    pass
            os.close(parent)

    def remove(self, relative: str, before: bytes | None) -> None:
        try:
            parent, name = self._parent(relative, create=False)
        except FileNotFoundError:
            return
        try:
            current = self._current(parent, name)
            if current is None:
                return
            if current != before:
                raise InstallRefused(f"{relative} changed while setup ran, so it was not removed; "
                                     "run setup again")
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)


def _write_new(directory: int, name: str, content: bytes, mode: int) -> None:
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
    try:
        os.write(handle, content)
        os.fchmod(handle, mode)
        os.fsync(handle)
    finally:
        os.close(handle)


class OwnerState:
    """The installer's owner-only folder in the owner's learning state, opened by verified
    descriptors. It holds each project's ownership record (which deny rules the installer
    added) and stages settings files before they are published, outside every project."""

    FOLDER = "installer"

    def __init__(self, *, create: bool) -> None:
        self.fd: int | None = None
        base = attest.default_state_root()
        try:
            if create:
                base.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif not base.is_dir():
                return
            base_fd, _canonical = attest._open_verified_directory(base)
        except (OSError, attest.AttestError) as exc:
            raise InstallRefused(f"the owner's learning state folder cannot be used safely: {exc}") from None
        try:
            self.fd = attest._private_child(base_fd, self.FOLDER, create=create)
        except FileNotFoundError:
            self.fd = None
        except (OSError, attest.AttestError) as exc:
            raise InstallRefused(f"the installer's folder in the learning state cannot be used: {exc}") from None
        finally:
            os.close(base_fd)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)

    @staticmethod
    def record_name(target: Path) -> str:
        digest = hashlib.sha256(os.path.realpath(target).encode("utf-8", "surrogateescape")).hexdigest()
        return f"claude-settings-{digest[:32]}.json"

    def read(self, name: str) -> bytes | None:
        if self.fd is None:
            return None
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise InstallRefused("the installer's ownership record is not a regular file")
            return os.read(handle, MAX_SETTINGS_BYTES + 1)
        finally:
            os.close(handle)

    def write(self, name: str, content: bytes) -> None:
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        _write_new(self.fd, temporary, content, 0o600)
        try:
            os.rename(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            os.unlink(temporary, dir_fd=self.fd)
            raise

    def remove(self, name: str) -> None:
        if self.fd is None:
            return
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def staging(self) -> int | None:
        """An owner-only staging folder's descriptor; the caller removes it with `unstage`."""

        if self.fd is None:
            return None
        name = f"staging-{secrets.token_hex(8)}"
        os.mkdir(name, 0o700, dir_fd=self.fd)
        self._staging = name
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)

    def unstage(self, descriptor: int | None) -> None:
        if descriptor is None:
            return
        try:
            for leftover in os.listdir(descriptor):
                os.unlink(leftover, dir_fd=descriptor)
        finally:
            os.close(descriptor)
            os.rmdir(self._staging, dir_fd=self.fd)


def _dump(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _load_settings(project: _Project) -> tuple[dict | None, bytes | None]:
    raw = project.read(CLAUDE_SETTINGS)
    if raw is None:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InstallRefused(f"{CLAUDE_SETTINGS} is not valid JSON, so it cannot be merged safely") from None
    if not isinstance(data, dict):
        raise InstallRefused(f"{CLAUDE_SETTINGS} is not a JSON object, so it cannot be merged safely")
    permissions = data.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        raise InstallRefused(f"{CLAUDE_SETTINGS} has a permissions value that is not an object")
    if isinstance(permissions, dict) and not isinstance(permissions.get("deny", []), list):
        raise InstallRefused(f"{CLAUDE_SETTINGS} has a permissions.deny value that is not a list")
    return data, raw


def _load_record(raw: bytes | None, generated: set[str], where: str) -> dict | None:
    """An ownership record of the entries the installer added, keeping only generated rules.

    An entry outside every set this installer generates is never treated as the installer's.
    """

    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        record = None
    flags = ("created_settings_file", "created_permissions", "created_deny")
    if (not isinstance(record, dict) or record.get("format") != 1
            or not all(isinstance(record.get(flag), bool) for flag in flags)
            or not isinstance(record.get("deny"), list)
            or not all(isinstance(item, str) for item in record["deny"])):
        raise InstallRefused(f"{where} is unreadable; restore or delete it before continuing")
    return {**record, "deny": [entry for entry in record["deny"] if entry in generated]}


def _record_bytes(record: dict) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def plan_settings(target: Path, operation: str, *, orchestration: Path = ORCHESTRATION) -> dict:
    """What install or remove does to .claude/settings.json, with the bytes to write.

    Install adds every missing deny rule and records which ones it added in the owner's
    learning state (OwnerState), outside the project, so nothing in the project can claim a
    rule for the installer; user entries are never dropped or reordered. Remove takes out
    only the entries recorded there, then the permissions and deny keys and the file itself
    when install created them and they are now empty. An entry not recorded is never
    removed. A record an earlier version kept in the project (LEGACY_RECORD) is used once,
    only while no owner record exists and only for generated rules, then deleted.
    Returns {"action", "changed_paths", "writes", "original", "order"}.
    """

    generated = generated_rules(orchestration)
    project = _Project(target)
    try:
        data, raw = _load_settings(project)
        legacy_raw = project.read(LEGACY_RECORD)
    finally:
        project.close()
    owner = OwnerState(create=False)
    try:
        owner_raw = owner.read(OwnerState.record_name(target))
    finally:
        owner.close()
    if owner_raw is not None:
        record = _load_record(owner_raw, generated, "the installer's ownership record")
    else:
        record = _load_record(legacy_raw, generated, LEGACY_RECORD)
    # The bytes this plan was made from: apply_settings writes nothing if any of them changed.
    original = {CLAUDE_SETTINGS: raw, LEGACY_RECORD: legacy_raw, OWNER_RECORD: owner_raw}
    writes: dict[str, bytes | None] = {}
    if legacy_raw is not None:
        writes[LEGACY_RECORD] = None
    if operation == "remove":
        removed: list[str] = []
        if record is not None and data is not None:
            ours = set(record["deny"])
            updated = json.loads(json.dumps(data))
            permissions = updated.get("permissions")
            if isinstance(permissions, dict) and isinstance(permissions.get("deny"), list):
                keep = []
                for entry in permissions["deny"]:
                    if isinstance(entry, str) and entry in ours:
                        removed.append(entry)
                    else:
                        keep.append(entry)
                permissions["deny"] = keep
                if record["created_deny"] and not keep:
                    del permissions["deny"]
                if record["created_permissions"] and not permissions:
                    del updated["permissions"]
            if record["created_settings_file"] and not updated:
                writes[CLAUDE_SETTINGS] = None
            elif updated != data:
                writes[CLAUDE_SETTINGS] = _dump(updated)
        if owner_raw is not None:
            writes[OWNER_RECORD] = None
        # Settings first, then the records: a failure in between leaves a record naming entries
        # already gone, which the next install or removal handles.
        order = [key for key in (CLAUDE_SETTINGS, OWNER_RECORD, LEGACY_RECORD) if key in writes]
        action = "remove" if writes else "none"
        return {"action": action, "changed_paths": sorted(set(writes) - {OWNER_RECORD}), "writes": writes,
                "removed": removed, "original": original, "order": order}
    desired = deny_rules(orchestration)
    previous = set(record["deny"]) if record else set()
    updated = json.loads(json.dumps(data)) if data is not None else {}
    permissions = updated.setdefault("permissions", {})
    deny = permissions.setdefault("deny", [])
    # Entries install added earlier that this version no longer wants go; everything else stays.
    deny[:] = [entry for entry in deny if not (isinstance(entry, str) and entry in previous
                                                 and entry not in desired)]
    present = {entry for entry in deny if isinstance(entry, str)}
    added = [rule for rule in desired if rule not in present]
    deny.extend(added)
    new_record = {
        "format": 1,
        "project": os.path.realpath(target),
        "created_settings_file": record["created_settings_file"] if record else data is None,
        "created_permissions": (record["created_permissions"] if record else False)
        or data is None or "permissions" not in data,
        "created_deny": (record["created_deny"] if record else False)
        or data is None or "deny" not in (data.get("permissions") or {}),
        "deny": [rule for rule in desired if rule in previous or rule in added],
    }
    if data is None or updated != data:
        writes[CLAUDE_SETTINGS] = _dump(updated)
    if owner_raw is None or _record_bytes(new_record) != owner_raw:
        writes[OWNER_RECORD] = _record_bytes(new_record)
    action = "unchanged" if not set(writes) - {OWNER_RECORD} else ("create" if data is None else "merge")
    # The owner record first, then the settings: a failure in between leaves a record that
    # claims entries not yet in the settings, which the next install adds and owns and a
    # removal skips. The project's old record goes last, once the owner record exists.
    order = [key for key in (OWNER_RECORD, CLAUDE_SETTINGS, LEGACY_RECORD) if key in writes]
    return {"action": action, "changed_paths": sorted(set(writes) - {OWNER_RECORD}), "writes": writes,
            "added": added, "original": original, "order": order}


def apply_settings(target: Path, plan: dict) -> None:
    """Write a plan made moments before; refuse if anything it read has changed.

    Every file is compared with the bytes the plan read; the settings file again right
    before its rename (Project.publish). The ownership record lives in the owner's state.
    """

    name = OwnerState.record_name(target)
    project = _Project(target)
    owner = OwnerState(create=OWNER_RECORD in plan["writes"] and plan["writes"][OWNER_RECORD] is not None
                       or CLAUDE_SETTINGS in plan["writes"])
    staging = None
    try:
        for key in (CLAUDE_SETTINGS, LEGACY_RECORD):
            if project.read(key) != plan["original"][key]:
                raise InstallRefused(f"{key} changed while setup ran, so it was not overwritten; run setup again")
        if owner.read(name) != plan["original"][OWNER_RECORD]:
            raise InstallRefused("the installer's ownership record changed while setup ran; run setup again")
        for key in plan["order"]:
            content = plan["writes"][key]
            if key == OWNER_RECORD:
                owner.remove(name) if content is None else owner.write(name, content)
            elif content is None:
                project.remove(key, plan["original"][key])
            else:
                staging = owner.staging()
                try:
                    project.publish(key, content, plan["original"][key], staging)
                finally:
                    owner.unstage(staging)
                    staging = None
    finally:
        owner.close()
        project.close()


def verify_settings(target: Path, *, orchestration: Path = ORCHESTRATION) -> dict:
    project = _Project(target)
    try:
        data, _ = _load_settings(project)
    except InstallRefused as exc:
        return {"ready": False, "error": str(exc)}
    finally:
        project.close()
    deny = ((data or {}).get("permissions") or {}).get("deny") or []
    missing = [rule for rule in deny_rules(orchestration) if rule not in deny]
    report = {"ready": data is not None and not missing, "settings": CLAUDE_SETTINGS, "missing": missing}
    if not report["ready"]:
        report["required_action"] = (
            "the Claude Code permission rules that keep owner commands away from a Claude primary "
            "are missing from .claude/settings.json; run install again (./setup.sh, Update the install)"
        )
    return report


def _settings_report(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key not in ("writes", "original", "order")}


# --- the Codex commands rule -------------------------------------------------------------

RULE_COMMAND = "remove-codex-rule"


def _owner_cli(target: Path, args: list[str], *, needs_install: bool = True) -> dict:
    """Run this repository's own CLI for the project, isolated; never the project's copy.

    Commands that depend on the installed files first require the installed orchestrator to
    be this release, byte for byte (attest.check_project_runtime).
    """

    try:
        if needs_install:
            attest.check_project_runtime(target)
        command = attest.cli_command(target, args)
    except attest.AttestError as exc:
        raise InstallRefused(str(exc)) from None
    result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = None
    return report if isinstance(report, dict) else {"ready": False}


def plan_codex_rule(target: Path) -> dict:
    """Remove takes out the Codex commands rule and its protected copy first.

    This repository's own ``remove-codex-rule --project-root <root>`` does it. It needs only
    the project's resolved path, so it works whatever version the project has installed, for
    a project in a temporary folder, and when no rule exists (it then removes nothing).
    """

    return {"action": RULE_COMMAND}


def apply_codex_rule(target: Path, plan: dict) -> dict:
    report = _owner_cli(target, [RULE_COMMAND, "--project-root", str(target)], needs_install=False)
    removed = report.get("removed")
    if report.get("ready") is not True or not isinstance(removed, list):
        return {"action": RULE_COMMAND, "error": "the Codex commands rule could not be removed",
                "exit_code": 2}
    return {"action": RULE_COMMAND, "removed": [item for item in removed if isinstance(item, str)]}


# --- operations --------------------------------------------------------------------------


def _roll_back(applied: list[tuple[str, object]], target: Path) -> list[dict]:
    """Undo earlier steps in reverse order after a later step fails to apply."""

    results = []
    for name, step in reversed(applied):
        if name == "codex_rule":
            results.append({"package": name, "action": None, "ok": False,
                            "note": "the Codex commands rule was removed; run ./setup.sh to allow it again"})
            continue
        if name == "claude_settings":
            reverse = "install" if step == "remove" else "remove"
            try:
                apply_settings(target, plan_settings(target, reverse))
                ok = True
            except (InstallRefused, OSError):
                ok = False
            results.append({"package": name, "action": reverse, "ok": ok})
            continue
        installer, op = step
        reverse = REVERSE.get(op)
        if reverse is None:
            results.append({"package": name, "action": None, "ok": False,
                            "note": f"a {op} cannot be undone automatically; restore from version control"})
            continue
        report = _run(installer, reverse, target, True, [])
        results.append({"package": name, "action": reverse, "ok": not _failed(report)})
    return results


def operate(operation: str, target: Path, *, apply: bool = False, codex_options: list[str] | None = None) -> dict:
    target = target.absolute()
    check_target(target, mutating=apply and operation != "verify")
    codex_options = codex_options or []
    summary: dict = {"operation": operation, "target": str(target), "applied": False}
    if operation == "verify":
        codex = _run(CODEX_INSTALLER, "verify", target, False, [])
        claude = _run(CLAUDE_INSTALLER, "verify", target, False, [])
        settings = verify_settings(target)
        summary.update(codex=codex, claude=claude, claude_settings=settings)
        summary["ready"] = (codex.get("ready") is True and claude.get("result") == "verified"
                            and settings["ready"])
        return summary
    # An existing Codex install is adopted through its own update path; unchanged bytes are a no-op.
    codex_operation = operation
    if operation == "install" and (target / CODEX_MANIFEST).is_file():
        codex_operation = "update"
    order = [(CODEX_INSTALLER, "codex", codex_operation, codex_options),
             (CLAUDE_INSTALLER, "claude", operation, [])]
    if operation == "remove":
        order.reverse()
    previews = {name: _run(installer, op, target, False, extra) for installer, name, op, extra in order}
    summary.update(previews)
    try:
        summary["claude_settings"] = _settings_report(plan_settings(target, operation))
    except (InstallRefused, OSError) as exc:
        summary["claude_settings"] = {"error": str(exc)}
    rule_plan = None
    if operation == "remove":
        try:
            rule_plan = plan_codex_rule(target)
        except (InstallRefused, OSError) as exc:
            rule_plan = {"error": str(exc)}
        summary["codex_rule"] = rule_plan
    failed = [report for report in (*previews.values(), summary["claude_settings"],
                                    *([rule_plan] if rule_plan is not None else [])) if _failed(report)]
    if failed or not apply:
        summary["ok"] = not failed
        return summary
    applied: list[tuple[str, object]] = []

    def settings_step() -> bool:
        try:
            # Re-read both files now, so edits made while the packages were applied are kept.
            apply_settings(target, plan_settings(target, operation))
        except (InstallRefused, OSError) as exc:
            summary["claude_settings"] = {**summary["claude_settings"], "error": str(exc)}
            return False
        applied.append(("claude_settings", operation))
        return True

    if operation == "remove":
        summary["codex_rule"] = apply_codex_rule(target, rule_plan)
        if _failed(summary["codex_rule"]):
            summary["ok"] = False
            return summary
        if summary["codex_rule"].get("removed"):
            applied.append(("codex_rule", None))
        if not settings_step():
            summary["ok"] = False
            summary["rollback"] = _roll_back(applied, target)
            return summary
    for installer, name, op, extra in order:
        summary[name] = _run(installer, op, target, True, extra)
        if _failed(summary[name]):
            summary["ok"] = False
            summary["rollback"] = _roll_back(applied, target)
            return summary
        applied.append((name, (installer, op)))
    if operation == "install" and not settings_step():
        summary["ok"] = False
        summary["rollback"] = _roll_back(applied, target)
        return summary
    summary["applied"] = True
    summary["ok"] = True
    return summary


def git_tool(argv: list[str]) -> int:
    """Plumbing-only git for ./setup.sh, which loads this file and calls it.

    ``changes PROJECT [PATH...]`` prints, NUL-delimited, the paths with uncommitted changes
    (project_changes); ``commit PROJECT MESSAGE DETAIL PATH...`` commits exactly those paths
    (commit_paths) and exits 3 when there was nothing to commit. A refusal exits 2 with its
    reason on stderr; any other failure exits 1.
    """

    try:
        if len(argv) >= 2 and argv[0] == "changes":
            found = project_changes(Path(argv[1]), argv[2:] or list(MANAGED_PATHS))
            sys.stdout.buffer.write(b"".join(item.encode("utf-8", "surrogateescape") + b"\0" for item in found))
            return 0
        if len(argv) >= 5 and argv[0] == "commit":
            return 0 if commit_paths(Path(argv[1]), argv[2], argv[3], argv[4:]) == "committed" else 3
    except InstallRefused as exc:
        print(exc, file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, ValueError):
        return 1
    print("unknown git tool", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("operation", choices=("install", "verify", "remove"))
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="perform the previewed changes")
    parser.add_argument("--project-agent-routing", type=Path)
    parser.add_argument("--operational-checks", type=Path)
    parser.add_argument("--replace-project-config", action="store_true")
    args = parser.parse_args(argv)
    codex_options: list[str] = []
    if args.operation == "install":
        if args.project_agent_routing:
            codex_options += ["--project-agent-routing", str(args.project_agent_routing.absolute())]
        if args.operational_checks:
            codex_options += ["--operational-checks", str(args.operational_checks.absolute())]
        if args.replace_project_config:
            codex_options.append("--replace-project-config")
    try:
        summary = operate(args.operation, args.target, apply=args.apply, codex_options=codex_options)
    except InstallRefused as exc:
        print(json.dumps({"operation": args.operation, "refused": str(exc)}, indent=2))
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.operation == "verify":
        return 0 if summary["ready"] else 1
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
