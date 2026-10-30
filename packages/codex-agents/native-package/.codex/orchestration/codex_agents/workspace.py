"""The contained workspace for a write-capable child (G3).

A write child never touches the repository. The bridge exports the committed
bytes of the granted paths from one source commit into a private workspace and
records a baseline manifest in a sibling control directory the child is not
given. After the child has stopped, the workspace is snapshotted into the
control directory without following symlinks; the inventory, the ownership
check, the patch and the verifier envelope all use only that snapshot.

This module is provider-neutral and holds no routing or dispatch logic. Every
refusal raises `WorkspaceRefused` with a stable reason code.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import bridge
from .bridge import (
    CLAUDE_CONTEXT_NAMES,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_FILES,
    PATHSPEC_SPECIAL,
    BridgeRefused,
    _claude_context_path,
    _granted_paths,
    _path_granted,
    _private_file,
)
from .learning_contracts import canonical_json

SCHEMA_VERSION = 1
MAX_INVENTORY_ENTRIES = 10_000
MAX_INVENTORY_BYTES = 64 * 1024 * 1024
# Walks reopen each directory from the top without following, so depth is bounded too.
MAX_INVENTORY_DEPTH = 128
MAX_CHANGED_FILE_BYTES = 1024 * 1024
MAX_CHANGED_FILES = 200
MAX_PATCH_BYTES = 4 * 1024 * 1024
# The envelope carries the patch, the resulting content of every changed file
# again, and the manifest.
MAX_ENVELOPE_BYTES = 2 * MAX_PATCH_BYTES + 2 * 1024 * 1024
# Bounds for checking a destination tree against its commit (integration worktrees).
MAX_TREE_ENTRIES = 200_000
MAX_TREE_BYTES = 1024 * 1024 * 1024

# Never exported as writable and never accepted in a change, at any depth and
# in any letter case. Compared after Unicode normalization and case folding.
PROTECTED_NAMES = frozenset(
    {".git", ".codex", ".agents", ".claude", "agents.md", "claude.md", "claude.local.md", ".mcp.json"}
    | {name.casefold() for name in CLAUDE_CONTEXT_NAMES}
)
REGULAR_MODE = "100644"
EXECUTABLE_MODE = "100755"
EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
# Exact permission bits the export gives a file, by (writable, Git mode).
EXPORT_FILE_PERMS = {
    (True, REGULAR_MODE): 0o644, (True, EXECUTABLE_MODE): 0o755,
    (False, REGULAR_MODE): 0o444, (False, EXECUTABLE_MODE): 0o555,
}
EXPORT_DIRECTORY_PERMS = 0o700
# The only permission bits a file or directory the child creates may have. An
# owned existing file may change only by toggling all three execute bits
# together (100644 <-> 100755); every other change is `mode_change`.
NEW_FILE_PERMS = {0o644: REGULAR_MODE, 0o755: EXECUTABLE_MODE}
NEW_DIRECTORY_PERMS = frozenset({0o700, 0o755})
ENTRY_TYPES = ("file", "directory", "symlink", "fifo", "socket", "device")
FENCED_MARKER = ".fenced-"
ROOT_ENTRY = "."
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC
# Global options that keep project-side Git machinery out of every plumbing
# call: no lazy fetch from a promisor remote, no replacement objects, no
# hooks, no filesystem monitor.
_GIT_HARDENING = (
    "--no-lazy-fetch",
    "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
)
_PATCH_KEYS = {"schema_version", "source_commit", "manifest_digest", "changes"}
_CHANGE_KEYS = {"path", "op", "old_blob", "old_mode", "new_sha256", "mode", "content_b64"}
_GIT_MODES = {REGULAR_MODE, EXECUTABLE_MODE}


class WorkspaceRefused(BridgeRefused):
    """A contained-workspace step refused; `reason` is a stable code."""

    def __init__(self, reason: str, path: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class WorkspaceLayout:
    """A private run directory holding the workspace and its sibling control directory."""

    root: Path
    workspace: Path
    control: Path

    @property
    def snapshot(self) -> Path:
        return self.control / "snapshot"

    @property
    def snapshot_entries(self) -> Path:
        return self.control / "snapshot-entries.json"

    @property
    def manifest_path(self) -> Path:
        return self.control / "manifest.json"


@dataclass(frozen=True)
class VerifiedTree:
    """Proof from `verify_tree_at_commit` that a directory is exactly one clean commit."""

    root: Path
    commit: str
    identity: tuple[int, int]


# --- paths -------------------------------------------------------------------


def _fold(part: str) -> str:
    # Format characters are dropped because some filesystems ignore them in
    # names, so `.git` with a zero-width joiner must still count as `.git`.
    normalized = unicodedata.normalize("NFC", part)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf").casefold()


def is_protected_path(relative: str) -> bool:
    """True when any component of the path is a protected name, in any case."""

    return any(_fold(part) in PROTECTED_NAMES for part in PurePosixPath(relative).parts)


def _alias_check(paths: Iterable[str], reason: str) -> None:
    """Refuse when two different paths, or path prefixes, fold to the same name.

    A case-insensitive or normalization-insensitive filesystem would merge
    them into one entry, so `src/A/x` and `src/a/y` collide.
    """

    seen: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            raw = "/".join(parts[:index])
            key = "/".join(_fold(part) for part in parts[:index])
            if seen.setdefault(key, raw) != raw:
                raise WorkspaceRefused(reason, raw)


def _literal_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkspaceRefused("path_invalid")
    if value.startswith(":") or set(value) & PATHSPEC_SPECIAL:
        raise WorkspaceRefused("path_not_literal", value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise WorkspaceRefused("path_outside_repository", value)
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceRefused("path_invalid") from exc
    return path.as_posix()


def _covers(parent: str, child: str) -> bool:
    return _path_granted(child, [parent])


def _collapse(paths: Iterable[str]) -> list[str]:
    """Drop paths already covered by another path in the set."""

    unique = sorted(set(paths))
    return [path for path in unique if not any(other != path and _covers(other, path) for other in unique)]


def effective_write_set(assignment: Mapping[str, Any]) -> list[str]:
    """The assignment's `owned_paths` intersected with its `:write` permissions.

    Matching is by whole path component: an owned `src/a.py` under a write
    grant `src` yields `src/a.py`, and an owned `src` with a write grant
    `src/a.py` yields `src/a.py`. A protected name in the result refuses.
    """

    owned = assignment.get("owned_paths", [])
    if not isinstance(owned, list):
        raise WorkspaceRefused("path_invalid")
    owned_paths = [_literal_path(value) for value in owned]
    permissions = assignment.get("permissions")
    entries = permissions.get("filesystem") if isinstance(permissions, dict) else None
    writes = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str) and entry.endswith(":write"):
            writes.append(_literal_path(entry[: -len(":write")]))
    result = set()
    for own in owned_paths:
        for grant in writes:
            if _covers(grant, own):
                result.add(own)
            elif _covers(own, grant):
                result.add(grant)
    for path in result:
        if is_protected_path(path):
            raise WorkspaceRefused("write_path_protected", path)
    return _collapse(result)


def assignment_export_paths(assignment: Mapping[str, Any]) -> list[str]:
    """Granted read paths (context and owned paths) plus the effective write set."""

    granted = _granted_paths(dict(assignment))
    paths = set()
    for value in (*assignment.get("context", []), *assignment.get("owned_paths", [])):
        path = _literal_path(value)
        if _claude_context_path(path):
            raise WorkspaceRefused("path_protected", path)
        if not _path_granted(path, granted):
            raise WorkspaceRefused("path_not_granted", path)
        paths.add(path)
    paths.update(effective_write_set(assignment))
    return sorted(paths)


def _in_write_set(relative: str, write_set: Iterable[str]) -> bool:
    return any(_covers(path, relative) for path in write_set)


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _git_mode(permissions: int) -> str:
    return EXECUTABLE_MODE if permissions & stat.S_IXUSR else REGULAR_MODE


def _identity(status: os.stat_result) -> tuple[int, int]:
    return (status.st_dev, status.st_ino)


def _detach(value: Any) -> Any:
    """A private deep copy through canonical JSON, so a caller cannot change it afterwards."""

    return json.loads(canonical_json(value))


# --- layout and private files ------------------------------------------------


def create_layout(parent: Path | str | None = None) -> WorkspaceLayout:
    """Create a private run directory with `workspace/` and `control/` siblings (all 0700)."""

    base = parent if parent is not None else (bridge.WORKSPACE_PARENT or tempfile.gettempdir())
    base = os.path.realpath(base)
    root = Path(tempfile.mkdtemp(prefix="codex-agents-write-", dir=base))
    os.chmod(root, 0o700)
    workspace = root / "workspace"
    control = root / "control"
    os.mkdir(workspace, 0o700)
    os.mkdir(control, 0o700)
    return WorkspaceLayout(root=root, workspace=workspace, control=control)


def _write_private_json(path: Path, value: Any) -> None:
    _private_file(path, canonical_json(value))


def _read_private_json(path: Path) -> Any:
    descriptor = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceRefused("control_file_invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return json.loads(handle.read())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


# --- git ---------------------------------------------------------------------

_LAZY_FETCH_SUPPORTED: bool | None = None


def _require_trusted_git(root: Path) -> None:
    """Refuse a Git without `--no-lazy-fetch`, and any partial clone, before reading objects.

    A partial clone resolves a missing object by fetching it through the
    repository's remote configuration (`core.sshCommand`, URLs, helpers),
    which the bridge never runs. Reading configuration executes nothing.
    """

    global _LAZY_FETCH_SUPPORTED
    if _LAZY_FETCH_SUPPORTED is None:
        probe = bridge._git(root, "--no-lazy-fetch", "version")
        _LAZY_FETCH_SUPPORTED = probe.returncode == 0
    if not _LAZY_FETCH_SUPPORTED:
        raise WorkspaceRefused("git_unsupported")
    partial = _plumbing(root, "config", "--get", "extensions.partialClone")
    if partial.returncode == 0:
        raise WorkspaceRefused("partial_clone")
    promisors = _plumbing(
        root, "config", "--get-regexp", r"^remote\..*\.(promisor|partialclonefilter)$"
    )
    if promisors.returncode == 0 and promisors.stdout.strip():
        raise WorkspaceRefused("partial_clone")
    if promisors.returncode not in {0, 1}:
        raise WorkspaceRefused("git_unsupported")


def _plumbing(root: Path, *arguments: str, text: bool = False):
    return bridge._git(root, *_GIT_HARDENING, *arguments, text=text)


def _git_blob_id(content: bytes, object_format: str) -> str:
    algorithm = "sha256" if object_format == "sha256" else "sha1"
    return hashlib.new(algorithm, b"blob %d\x00" % len(content) + content).hexdigest()


def _hex(value: Any, lengths: tuple[int, ...] = (40, 64)) -> bool:
    return isinstance(value, str) and len(value) in lengths and all(ch in "0123456789abcdef" for ch in value)


def _repository_root(repository_root: Path | str) -> Path:
    root = Path(repository_root).resolve()
    _require_trusted_git(root)
    top = _plumbing(root, "rev-parse", "--show-toplevel", text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        raise WorkspaceRefused("repository_root_invalid")
    return root


def _object_format(root: Path) -> str:
    fmt = _plumbing(root, "rev-parse", "--show-object-format", text=True)
    object_format = fmt.stdout.strip() if fmt.returncode == 0 else ""
    if object_format not in {"sha1", "sha256"}:
        raise WorkspaceRefused("object_format_unsupported")
    return object_format


def _resolve(root: Path, revision: str, object_format: str) -> str:
    if not revision or revision.startswith("-"):
        raise WorkspaceRefused("revision_invalid")
    resolved = _plumbing(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", text=True)
    commit = resolved.stdout.strip()
    if resolved.returncode != 0 or not _hex(commit, (64 if object_format == "sha256" else 40,)):
        raise WorkspaceRefused("source_commit_unresolved")
    return commit


def resolve_source_commit(repository_root: Path, revision: str = "HEAD") -> tuple[str, str]:
    """Resolve one commit; return `(commit, object_format)`."""

    root = _repository_root(repository_root)
    object_format = _object_format(root)
    return _resolve(root, revision, object_format), object_format


def _list_committed(root: Path, commit: str, requested: str | None) -> list[tuple[str, str, str, int, str]]:
    arguments = ["ls-tree", "-r", "-l", "-z", "--full-tree", commit]
    if requested is not None:
        arguments += ["--", requested]
    listing = _plumbing(root, *arguments)
    if listing.returncode != 0:
        raise WorkspaceRefused("path_not_committed", requested)
    found = []
    for item in (part for part in listing.stdout.split(b"\x00") if part):
        header, _, name = item.partition(b"\t")
        try:
            mode, kind, sha, size = header.decode("ascii").split()
            relative = name.decode("utf-8", "strict")
        except ValueError as exc:
            raise WorkspaceRefused("export_entry_invalid", requested) from exc
        if size != "-" and not size.isdigit():
            # `ls-tree -l` prints a non-number when the object is missing.
            raise WorkspaceRefused("export_blob_unavailable", relative)
        found.append((mode, kind, sha, -1 if size == "-" else int(size), relative))
    return found


# --- no-follow directory helpers ---------------------------------------------


def _open_parents(base_fd: int, parts: list[str], *, create: bool) -> int:
    """Open the directory at `parts` below `base_fd`, one component at a time, never following."""

    current = os.dup(base_fd)
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, EXPORT_DIRECTORY_PERMS, dir_fd=current)
            try:
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
                    raise WorkspaceRefused("path_not_directory", part) from exc
                raise
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


class _DirectoryTracker:
    """Opens directories below a root and refuses two logical paths that are one directory."""

    def __init__(self, root_fd: int, reason: str) -> None:
        self.root_fd = root_fd
        self.reason = reason
        self.owners = {_identity(os.fstat(root_fd)): ""}
        self.perms: dict[str, int] = {}

    def open(self, parts: list[str], *, create: bool) -> int:
        current = os.dup(self.root_fd)
        try:
            for index, part in enumerate(parts):
                following = _open_parents(current, [part], create=create)
                os.close(current)
                current = following
                logical = "/".join(parts[: index + 1])
                status = os.fstat(current)
                if self.owners.setdefault(_identity(status), logical) != logical:
                    raise WorkspaceRefused(self.reason, logical)
                self.perms[logical] = stat.S_IMODE(status.st_mode)
            return current
        except BaseException:
            os.close(current)
            raise


def _entry_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "device"


def _checked_name(name: str, parent: str) -> str:
    try:
        name.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceRefused("entry_name_invalid") from exc
    return f"{parent}/{name}" if parent else name


def _list_names(directory_fd: int, budget: int, reason: str = "inventory_too_many_entries") -> list[str]:
    """List a directory, refusing as soon as it holds more than `budget` entries (before sorting)."""

    names = []
    with os.scandir(directory_fd) as listing:
        for entry in listing:
            if len(names) >= budget:
                raise WorkspaceRefused(reason)
            names.append(entry.name)
    names.sort()
    return names


def _read_bounded(descriptor: int, limit: int, reason: str = "inventory_too_large") -> bytes:
    chunks = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise WorkspaceRefused(reason)
    return data


def _depth_check(path: str) -> None:
    if path.count("/") + 1 >= MAX_INVENTORY_DEPTH:
        raise WorkspaceRefused("inventory_too_deep", path)


# --- export ------------------------------------------------------------------


def export_workspace(
    repository_root: Path,
    layout: WorkspaceLayout,
    read_paths: Iterable[str],
    write_set: Iterable[str],
    *,
    revision: str = "HEAD",
    max_bytes: int = MAX_EXPORT_BYTES,
    max_files: int = MAX_EXPORT_FILES,
) -> dict[str, Any]:
    """Export every read and write path from one commit; return the baseline manifest.

    Blobs are read with `git cat-file`, so no filter, attribute, hook or
    checkout configuration runs, and a Git that could lazily fetch is refused.
    A write path may be absent from the commit (the child creates it); a read
    path must exist. Claude context files are skipped; a symlink, submodule or
    `.git` component refuses. Files outside the write set, and protected
    names, are exported read-only. The manifest records each file's blob, Git
    mode, exact permission bits, size and SHA-256, and each directory's
    permission bits.
    """

    root = _repository_root(repository_root)
    writable = _collapse(_literal_path(path) for path in write_set)
    for path in writable:
        if is_protected_path(path):
            raise WorkspaceRefused("write_path_protected", path)
    requested_paths = sorted({_literal_path(path) for path in read_paths} | set(writable))
    if not requested_paths:
        raise WorkspaceRefused("export_empty")
    object_format = _object_format(root)
    commit = _resolve(root, revision, object_format)
    entries: dict[str, tuple[str, str, int]] = {}
    for requested in requested_paths:
        if _claude_context_path(requested) or any(_fold(p) == ".git" for p in requested.split("/")):
            raise WorkspaceRefused("path_protected", requested)
        found = _list_committed(root, commit, requested)
        if not found and not _in_write_set(requested, writable):
            raise WorkspaceRefused("path_not_committed", requested)
        for mode, kind, sha, size, relative in found:
            if not _covers(requested, relative):
                raise WorkspaceRefused("export_outside_request", requested)
            if _claude_context_path(relative):
                continue
            if any(_fold(part) == ".git" for part in relative.split("/")):
                raise WorkspaceRefused("path_protected", relative)
            if mode == "120000":
                raise WorkspaceRefused("export_symlink", relative)
            if mode == "160000" or kind == "commit":
                raise WorkspaceRefused("export_submodule", relative)
            if kind != "blob" or mode not in _GIT_MODES or not _hex(sha):
                raise WorkspaceRefused("export_entry_unsupported", relative)
            if any(part in {"", ".", ".."} for part in relative.split("/")):
                raise WorkspaceRefused("path_outside_repository", relative)
            entries[relative] = (sha, mode, size)
    if len(entries) > max_files:
        raise WorkspaceRefused("export_too_many_files")
    if sum(size for _, _, size in entries.values()) > max_bytes:
        raise WorkspaceRefused("export_too_large")
    _alias_check(entries, "export_path_collision")
    workspace_fd = os.open(layout.workspace, _DIRECTORY_FLAGS)
    files: dict[str, dict[str, Any]] = {}
    total = 0
    try:
        tracker = _DirectoryTracker(workspace_fd, "export_path_collision")
        file_owners: dict[tuple[int, int], str] = {}
        for relative, (sha, mode, _) in sorted(entries.items()):
            parts = relative.split("/")
            blob = _plumbing(root, "cat-file", "blob", sha)
            content = blob.stdout
            if blob.returncode != 0 or _git_blob_id(content, object_format) != sha:
                raise WorkspaceRefused("export_blob_unavailable", relative)
            total += len(content)
            if total > max_bytes:
                raise WorkspaceRefused("export_too_large")
            writable_file = _in_write_set(relative, writable) and not is_protected_path(relative)
            permissions = EXPORT_FILE_PERMS[(writable_file, mode)]
            parent_fd = tracker.open(parts[:-1], create=True)
            try:
                try:
                    descriptor = os.open(parts[-1], _CREATE_FLAGS, 0o600, dir_fd=parent_fd)
                except FileExistsError as exc:
                    # Two committed names that are one entry on this filesystem.
                    raise WorkspaceRefused("export_path_collision", relative) from exc
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    os.fchmod(handle.fileno(), permissions)
                    status = os.fstat(handle.fileno())
            finally:
                os.close(parent_fd)
            if file_owners.setdefault(_identity(status), relative) != relative:
                raise WorkspaceRefused("export_path_collision", relative)
            files[relative] = {
                "blob": sha, "mode": mode, "perm": stat.S_IMODE(status.st_mode), "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        directories = dict(sorted(tracker.perms.items()))
        root_perm = stat.S_IMODE(os.fstat(workspace_fd).st_mode)
    finally:
        os.close(workspace_fd)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_commit": commit,
        "object_format": object_format,
        "write_set": writable,
        "files": files,
        "directories": directories,
        "root_perm": root_perm,
    }
    _write_private_json(layout.manifest_path, manifest)
    return manifest


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def read_manifest(layout: WorkspaceLayout) -> dict[str, Any]:
    manifest = _read_private_json(layout.manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceRefused("manifest_invalid")
    return manifest


# --- snapshot ----------------------------------------------------------------

CLONE_NOOWNERCOPY = 0x0002


def _load_fclonefileat():
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).fclonefileat
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    return function


# The APFS clone (what `cp -c` does), taken from an already opened descriptor.
# None selects the portable copy.
_FCLONEFILEAT = _load_fclonefileat()
_CLONE_UNSUPPORTED = {errno.ENOTSUP, errno.EXDEV, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _fingerprint(status: os.stat_result) -> tuple:
    return (
        status.st_dev, status.st_ino, status.st_mode, status.st_nlink, status.st_size,
        status.st_mtime_ns, status.st_ctime_ns,
    )


def _transfer(source_fd: int, target_dir_fd: int, name: str, limit: int) -> None:
    """Clone or copy the pinned source descriptor into a new entry `name`."""

    if _FCLONEFILEAT is not None:
        if _FCLONEFILEAT(source_fd, target_dir_fd, os.fsencode(name), CLONE_NOOWNERCOPY) == 0:
            return
        error = ctypes.get_errno()
        if error not in _CLONE_UNSUPPORTED:
            raise OSError(error, os.strerror(error), name)
    os.lseek(source_fd, 0, os.SEEK_SET)
    content = _read_bounded(source_fd, limit)
    target = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=target_dir_fd)
    with os.fdopen(target, "wb") as handle:
        handle.write(content)


def _snapshot_file(
    source_dir: int, name: str, target_dir: int, path: str, limit: int
) -> tuple[str, os.stat_result]:
    """Snapshot one entry that looked like a regular file; return its type and pinned status.

    The source is opened without following and checked through its
    descriptor; the clone or copy is taken from that descriptor, and any
    change to the file while it is copied refuses with `snapshot_unstable`.
    """

    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=source_dir)
    except OSError as exc:
        if exc.errno not in {errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise
        # Swapped for a symlink or socket after the first look: record it by type.
        status = os.stat(name, dir_fd=source_dir, follow_symlinks=False)
        if stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode):
            raise WorkspaceRefused("snapshot_unstable", path) from exc
        return _entry_type(status.st_mode), status
    try:
        before = os.fstat(descriptor)
        if stat.S_ISDIR(before.st_mode):
            raise WorkspaceRefused("snapshot_unstable", path)
        if not stat.S_ISREG(before.st_mode):
            return _entry_type(before.st_mode), before
        if before.st_size > limit:
            raise WorkspaceRefused("inventory_too_large")
        _transfer(descriptor, target_dir, name, limit)
        after = os.fstat(descriptor)
        copy = os.open(name, _READ_FLAGS, dir_fd=target_dir)
        try:
            copied = os.fstat(copy)
            if _fingerprint(before) != _fingerprint(after) or not stat.S_ISREG(copied.st_mode) \
                    or copied.st_size != before.st_size:
                raise WorkspaceRefused("snapshot_unstable", path)
            # The recorded mode is authoritative; the copy stays readable.
            os.fchmod(copy, 0o600)
        finally:
            os.close(copy)
        return "file", before
    finally:
        os.close(descriptor)


def _snapshot_workspace(layout: WorkspaceLayout, max_entries: int, max_bytes: int) -> Path:
    os.mkdir(layout.snapshot, 0o700)
    try:
        source_root = os.open(layout.workspace, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise WorkspaceRefused("workspace_not_directory") from exc
    snapshot_root = os.open(layout.snapshot, _DIRECTORY_FLAGS)
    root_status = os.fstat(source_root)
    # The workspace root itself is recorded under ".", which no relative path can spell.
    entries: dict[str, dict[str, Any]] = {
        ROOT_ENTRY: {"type": "directory", "mode": stat.S_IMODE(root_status.st_mode), "nlink": root_status.st_nlink},
    }
    total = 0
    try:
        pending = [""]
        while pending:
            relative = pending.pop()
            parts = relative.split("/") if relative else []
            source = _open_parents(source_root, parts, create=False)
            target = _open_parents(snapshot_root, parts, create=False)
            try:
                for name in _list_names(source, max_entries - (len(entries) - 1)):
                    path = _checked_name(name, relative)
                    status = os.stat(name, dir_fd=source, follow_symlinks=False)
                    kind = _entry_type(status.st_mode)
                    if kind == "directory":
                        _depth_check(path)
                        os.mkdir(name, 0o700, dir_fd=target)
                        pending.append(path)
                    elif kind == "file":
                        kind, status = _snapshot_file(source, name, target, path, max_bytes - total)
                        if kind == "file":
                            total += status.st_size
                    entries[path] = {
                        "type": kind,
                        "mode": stat.S_IMODE(status.st_mode),
                        "nlink": status.st_nlink,
                    }
            finally:
                os.close(source)
                os.close(target)
    finally:
        os.close(source_root)
        os.close(snapshot_root)
    _write_private_json(layout.snapshot_entries, entries)
    return layout.snapshot


def snapshot_workspace(
    layout: WorkspaceLayout,
    *,
    max_entries: int = MAX_INVENTORY_ENTRIES,
    max_bytes: int = MAX_INVENTORY_BYTES,
) -> Path:
    """Copy the stopped child's workspace into the control directory, never following symlinks.

    Regular files are cloned (APFS `fclonefileat`) or copied from a pinned
    descriptor; directories are recreated. Every entry's type, mode and link
    count is recorded from the workspace itself, and symlinks, FIFOs, sockets
    and devices are recorded by type only. Exceeding a bound refuses.
    """

    try:
        return _snapshot_workspace(layout, max_entries, max_bytes)
    except OSError as exc:
        raise WorkspaceRefused("snapshot_failed") from exc


# --- inventory ---------------------------------------------------------------


def _inventory_snapshot(
    layout: WorkspaceLayout, max_entries: int, max_bytes: int
) -> dict[str, dict[str, Any]]:
    recorded = _read_private_json(layout.snapshot_entries)
    if not isinstance(recorded, dict):
        raise WorkspaceRefused("snapshot_invalid")
    if len(recorded) > max_entries + 1:  # the entries plus the root record
        raise WorkspaceRefused("inventory_too_many_entries")
    inventory: dict[str, dict[str, Any]] = {}
    total = 0
    root = os.open(layout.snapshot, _DIRECTORY_FLAGS)
    try:
        pending = [""]
        while pending:
            relative = pending.pop()
            directory = _open_parents(root, relative.split("/") if relative else [], create=False)
            try:
                for name in _list_names(directory, max_entries - len(inventory)):
                    path = _checked_name(name, relative)
                    status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    kind = _entry_type(status.st_mode)
                    record = recorded.get(path)
                    if not isinstance(record, dict) or record.get("type") != kind:
                        raise WorkspaceRefused("snapshot_inconsistent", path)
                    entry = {
                        "type": kind, "mode": int(record["mode"]),
                        "nlink": max(int(record["nlink"]), 1), "size": 0, "sha256": None,
                    }
                    if kind == "directory":
                        _depth_check(path)
                        pending.append(path)
                    elif kind == "file":
                        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
                        try:
                            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                                raise WorkspaceRefused("snapshot_inconsistent", path)
                            content = _read_bounded(descriptor, max_bytes - total)
                        finally:
                            os.close(descriptor)
                        total += len(content)
                        entry["size"] = len(content)
                        entry["sha256"] = hashlib.sha256(content).hexdigest()
                        entry["nlink"] = max(entry["nlink"], status.st_nlink)
                    else:
                        raise WorkspaceRefused("snapshot_inconsistent", path)
                    inventory[path] = entry
            finally:
                os.close(directory)
    finally:
        os.close(root)
    root_record = recorded.get(ROOT_ENTRY)
    if not isinstance(root_record, dict) or root_record.get("type") != "directory":
        raise WorkspaceRefused("snapshot_inconsistent", ROOT_ENTRY)
    inventory[ROOT_ENTRY] = {
        "type": "directory", "mode": int(root_record["mode"]), "nlink": int(root_record["nlink"]),
        "size": 0, "sha256": None,
    }
    for path, record in recorded.items():
        if path in inventory:
            continue
        kind = record.get("type") if isinstance(record, dict) else None
        if kind not in {"symlink", "fifo", "socket", "device"}:
            raise WorkspaceRefused("snapshot_inconsistent", str(path))
        inventory[path] = {
            "type": kind, "mode": int(record["mode"]), "nlink": int(record["nlink"]),
            "size": 0, "sha256": None,
        }
    return dict(sorted(inventory.items()))


def inventory_snapshot(
    layout: WorkspaceLayout,
    *,
    max_entries: int = MAX_INVENTORY_ENTRIES,
    max_bytes: int = MAX_INVENTORY_BYTES,
) -> dict[str, dict[str, Any]]:
    """A bounded no-follow walk of the snapshot, ignoring nothing.

    Each path maps to `{type, mode, nlink, size, sha256}`; `mode` is the full
    permission bits and `sha256` is set for regular files only. Link counts,
    modes and non-regular entries come from the record taken at snapshot
    time, because a copy has its own link count and mode.
    """

    try:
        return _inventory_snapshot(layout, max_entries, max_bytes)
    except OSError as exc:
        raise WorkspaceRefused("inventory_failed") from exc


# --- ownership check ---------------------------------------------------------


def check_ownership(manifest: Mapping[str, Any], inventory: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """Compare the snapshot inventory with the baseline; return the changes or refuse.

    Each change is `{path, op, old_blob, old_mode, mode, sha256, size}`.
    Permission rules, compared on the full permission bits:
    - a baseline file keeps its exported bits, except that an owned file may
      toggle all three execute bits together (0644 <-> 0755);
    - a baseline directory keeps its exported bits;
    - a new file must be 0644 or 0755 and a new directory 0700 or 0755.

    Refusal codes: `symlink_entry`, `special_entry`, `hard_link`,
    `protected_name`, `outside_write_set`, `mode_change`,
    `unneeded_directory`, `file_too_large`, `too_many_changes`.
    """

    baseline: Mapping[str, Mapping[str, Any]] = manifest["files"]
    baseline_dirs: Mapping[str, int] = manifest["directories"]
    write_set = list(manifest["write_set"])
    # New directories are allowed only as ancestors of files inside the write set.
    needed_dirs = {
        ancestor
        for path, entry in inventory.items()
        if entry["type"] == "file" and _in_write_set(path, write_set)
        for ancestor in _ancestors(path)
    }
    changes: list[dict] = []
    for path, entry in sorted(inventory.items()):
        if path == ROOT_ENTRY:
            if entry["type"] != "directory" or entry["mode"] != manifest["root_perm"]:
                raise WorkspaceRefused("mode_change", ROOT_ENTRY)
            continue
        kind = entry["type"]
        if kind == "symlink":
            raise WorkspaceRefused("symlink_entry", path)
        if kind in {"fifo", "socket", "device"}:
            raise WorkspaceRefused("special_entry", path)
        if kind == "directory":
            if path in baseline_dirs:
                if entry["mode"] != baseline_dirs[path]:
                    raise WorkspaceRefused("mode_change", path)
                continue
            if is_protected_path(path):
                raise WorkspaceRefused("protected_name", path)
            if path not in needed_dirs:
                raise WorkspaceRefused("unneeded_directory", path)
            if entry["mode"] not in NEW_DIRECTORY_PERMS:
                raise WorkspaceRefused("mode_change", path)
            continue
        if entry["nlink"] > 1:
            raise WorkspaceRefused("hard_link", path)
        old = baseline.get(path)
        protected = is_protected_path(path)
        owned = _in_write_set(path, write_set) and not protected
        toggled = old is not None and entry["mode"] == old["perm"] ^ EXECUTE_BITS
        if old is not None and old["sha256"] == entry["sha256"]:
            if old["perm"] == entry["mode"]:
                continue
            # A permission-only change: the execute toggle on an owned file, nothing else.
            if not (owned and toggled):
                raise WorkspaceRefused("mode_change", path)
        else:
            if protected:
                raise WorkspaceRefused("protected_name", path)
            if not owned:
                raise WorkspaceRefused("outside_write_set", path)
            if old is None and entry["mode"] not in NEW_FILE_PERMS:
                raise WorkspaceRefused("mode_change", path)
            if old is not None and entry["mode"] != old["perm"] and not toggled:
                raise WorkspaceRefused("mode_change", path)
        if entry["size"] > MAX_CHANGED_FILE_BYTES:
            raise WorkspaceRefused("file_too_large", path)
        changes.append({
            "path": path, "op": "add" if old is None else "modify",
            "old_blob": None if old is None else old["blob"],
            "old_mode": None if old is None else old["mode"],
            "mode": _git_mode(entry["mode"]), "sha256": entry["sha256"], "size": entry["size"],
        })
    for path, old in sorted(baseline.items()):
        current = inventory.get(path)
        if current is not None and current["type"] == "file":
            continue
        if is_protected_path(path):
            raise WorkspaceRefused("protected_name", path)
        if not _in_write_set(path, write_set):
            raise WorkspaceRefused("outside_write_set", path)
        changes.append({
            "path": path, "op": "delete", "old_blob": old["blob"], "old_mode": old["mode"],
            "mode": None, "sha256": None, "size": 0,
        })
    if len(changes) > MAX_CHANGED_FILES:
        raise WorkspaceRefused("too_many_changes")
    return sorted(changes, key=lambda change: change["path"])


# --- patch -------------------------------------------------------------------


def encode_patch(patch: Mapping[str, Any]) -> bytes:
    return canonical_json(patch)


def patch_digest(patch: Mapping[str, Any]) -> str:
    return hashlib.sha256(encode_patch(patch)).hexdigest()


def _read_snapshot_file(layout: WorkspaceLayout, path: str) -> bytes:
    root = os.open(layout.snapshot, _DIRECTORY_FLAGS)
    try:
        parts = path.split("/")
        parent = _open_parents(root, parts[:-1], create=False)
        try:
            descriptor = os.open(parts[-1], _READ_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
    finally:
        os.close(root)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceRefused("snapshot_inconsistent", path)
        return _read_bounded(descriptor, MAX_CHANGED_FILE_BYTES)
    finally:
        os.close(descriptor)


def build_patch(layout: WorkspaceLayout, manifest: Mapping[str, Any], changes: list[dict]) -> dict[str, Any]:
    """Build the canonical patch from checked changes, reading content from the snapshot only."""

    entries = []
    for change in sorted(changes, key=lambda item: item["path"]):
        content_b64 = None
        if change["op"] != "delete":
            content = _read_snapshot_file(layout, change["path"])
            if hashlib.sha256(content).hexdigest() != change["sha256"]:
                raise WorkspaceRefused("snapshot_inconsistent", change["path"])
            content_b64 = base64.b64encode(content).decode("ascii")
        entries.append({
            "path": change["path"], "op": change["op"], "old_blob": change["old_blob"],
            "old_mode": change["old_mode"], "new_sha256": change["sha256"], "mode": change["mode"],
            "content_b64": content_b64,
        })
    patch = {
        "schema_version": SCHEMA_VERSION,
        "source_commit": manifest["source_commit"],
        "manifest_digest": manifest_digest(manifest),
        "changes": entries,
    }
    validate_patch(patch)
    return patch


def collect_patch(layout: WorkspaceLayout, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Inventory the snapshot, check ownership and build the patch."""

    return build_patch(layout, manifest, check_ownership(manifest, inventory_snapshot(layout)))


def validate_patch(patch: Any) -> list[tuple[dict, bytes | None]]:
    """Check a patch's shape, paths, content digests and caps; return changes with decoded content.

    `add` has `old_blob` and `old_mode` null; `modify` has both set and a
    `mode`; `delete` has both set and `mode`, `new_sha256` and `content_b64`
    null.
    """

    if not isinstance(patch, dict) or set(patch) != _PATCH_KEYS:
        raise WorkspaceRefused("patch_invalid")
    if patch["schema_version"] != SCHEMA_VERSION or not _hex(patch["source_commit"]) \
            or not _hex(patch["manifest_digest"], (64,)):
        raise WorkspaceRefused("patch_invalid")
    changes = patch["changes"]
    if not isinstance(changes, list):
        raise WorkspaceRefused("patch_invalid")
    if len(changes) > MAX_CHANGED_FILES:
        raise WorkspaceRefused("too_many_changes")
    if len(encode_patch(patch)) > MAX_PATCH_BYTES:
        raise WorkspaceRefused("patch_too_large")
    decoded = []
    previous = None
    for change in changes:
        if not isinstance(change, dict) or set(change) != _CHANGE_KEYS:
            raise WorkspaceRefused("patch_invalid")
        path = _literal_path(change["path"])
        if path != change["path"] or (previous is not None and path <= previous):
            raise WorkspaceRefused("patch_invalid", change.get("path"))
        previous = path
        if is_protected_path(path):
            raise WorkspaceRefused("protected_name", path)
        op = change["op"]
        old_blob, old_mode = change["old_blob"], change["old_mode"]
        new_sha, mode, content_b64 = change["new_sha256"], change["mode"], change["content_b64"]
        if op not in {"add", "modify", "delete"}:
            raise WorkspaceRefused("patch_invalid", path)
        if op == "add":
            if old_blob is not None or old_mode is not None:
                raise WorkspaceRefused("patch_invalid", path)
        elif not _hex(old_blob, (len(patch["source_commit"]),)) or old_mode not in _GIT_MODES:
            raise WorkspaceRefused("patch_invalid", path)
        if op == "delete":
            if mode is not None or new_sha is not None or content_b64 is not None:
                raise WorkspaceRefused("patch_invalid", path)
            decoded.append((change, None))
            continue
        if mode not in _GIT_MODES or not _hex(new_sha, (64,)) or not isinstance(content_b64, str):
            raise WorkspaceRefused("patch_invalid", path)
        try:
            content = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise WorkspaceRefused("patch_invalid", path) from exc
        if len(content) > MAX_CHANGED_FILE_BYTES:
            raise WorkspaceRefused("file_too_large", path)
        if hashlib.sha256(content).hexdigest() != new_sha:
            raise WorkspaceRefused("patch_content_mismatch", path)
        decoded.append((change, content))
    return decoded


# --- destination tree --------------------------------------------------------


def _file_state(parent_fd: int, name: str, object_format: str) -> tuple[str, str, tuple[int, int]] | None:
    """`(blob, git_mode, identity)` of a regular file, None when absent; refuse anything else."""

    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise WorkspaceRefused("apply_entry_not_file", name)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    try:
        pinned = os.fstat(descriptor)
        if not stat.S_ISREG(pinned.st_mode):
            raise WorkspaceRefused("apply_entry_not_file", name)
        content = _read_bounded(descriptor, MAX_INVENTORY_BYTES)
    finally:
        os.close(descriptor)
    return _git_blob_id(content, object_format), _git_mode(pinned.st_mode), _identity(pinned)


def verify_tree_at_commit(root: Path | str, commit: str) -> VerifiedTree:
    """Prove a checkout is exactly `commit`: HEAD at it, a clean index, and a clean worktree.

    Uses the same hardened Git (no lazy fetch, no replacement objects, no
    hooks or monitor). The index is compared with `diff-index --cached`, which
    reads no worktree content. The worktree is compared by this module, not
    Git: a no-follow walk hashes every entry outside the top-level `.git` and
    requires exactly the committed paths, blobs and modes, so no filter or
    attribute runs. Refusals: `tree_not_at_commit`, `tree_index_dirty`,
    `tree_not_clean`, `tree_unverifiable`, `tree_too_large`.
    """

    resolved_root = _repository_root(root)
    object_format = _object_format(resolved_root)
    if _resolve(resolved_root, "HEAD", object_format) != commit:
        raise WorkspaceRefused("tree_not_at_commit")
    index = _plumbing(resolved_root, "diff-index", "--cached", "--quiet", "--no-renames", commit, "--")
    if index.returncode != 0:
        raise WorkspaceRefused("tree_index_dirty")
    unmerged = _plumbing(resolved_root, "ls-files", "-u", "-z")
    if unmerged.returncode != 0 or unmerged.stdout:
        raise WorkspaceRefused("tree_index_dirty")
    expected: dict[str, tuple[str, str]] = {}
    for mode, kind, sha, _, relative in _list_committed(resolved_root, commit, None):
        if mode == "160000" or kind != "blob":
            raise WorkspaceRefused("tree_unverifiable", relative)
        expected[relative] = (mode, sha)
        if len(expected) > MAX_TREE_ENTRIES:
            raise WorkspaceRefused("tree_too_large")
    expected_dirs = {ancestor for path in expected for ancestor in _ancestors(path)}
    seen: set[str] = set()
    total = 0
    root_fd = os.open(resolved_root, _DIRECTORY_FLAGS)
    try:
        identity = _identity(os.fstat(root_fd))
        pending = [""]
        while pending:
            relative = pending.pop()
            directory = _open_parents(root_fd, relative.split("/") if relative else [], create=False)
            try:
                for name in _list_names(directory, MAX_TREE_ENTRIES - len(seen), "tree_too_large"):
                    if not relative and name == ".git":
                        continue
                    path = _checked_name(name, relative)
                    status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(status.st_mode):
                        if path not in expected_dirs:
                            raise WorkspaceRefused("tree_not_clean", path)
                        _depth_check(path)
                        pending.append(path)
                        continue
                    want = expected.get(path)
                    if want is None:
                        raise WorkspaceRefused("tree_not_clean", path)
                    if stat.S_ISLNK(status.st_mode):
                        target = os.readlink(name, dir_fd=directory)
                        actual = ("120000", _git_blob_id(os.fsencode(target), object_format))
                    elif stat.S_ISREG(status.st_mode):
                        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
                        try:
                            pinned = os.fstat(descriptor)
                            content = _read_bounded(descriptor, MAX_TREE_BYTES - total, "tree_too_large")
                        finally:
                            os.close(descriptor)
                        total += len(content)
                        actual = (_git_mode(pinned.st_mode), _git_blob_id(content, object_format))
                    else:
                        raise WorkspaceRefused("tree_not_clean", path)
                    if actual != want:
                        raise WorkspaceRefused("tree_not_clean", path)
                    seen.add(path)
            finally:
                os.close(directory)
    finally:
        os.close(root_fd)
    if seen != set(expected):
        raise WorkspaceRefused("tree_not_clean")
    return VerifiedTree(root=resolved_root, commit=commit, identity=identity)


# --- apply -------------------------------------------------------------------


def _files_below(parent_fd: int, name: str) -> set[str] | None:
    """Regular files below a directory entry, relative to it; None when the entry is absent or a file."""

    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(status.st_mode):
        return None
    found: set[str] = set()
    top = _open_parents(parent_fd, [name], create=False)
    try:
        pending = [""]
        while pending:
            relative = pending.pop()
            directory = _open_parents(top, relative.split("/") if relative else [], create=False)
            try:
                budget = MAX_INVENTORY_ENTRIES - len(found) - len(pending)
                for child in _list_names(directory, budget):
                    path = f"{relative}/{child}" if relative else child
                    kind = _entry_type(os.stat(child, dir_fd=directory, follow_symlinks=False).st_mode)
                    if kind == "directory":
                        _depth_check(path)
                        pending.append(path)
                    elif kind == "file":
                        found.add(path)
                    else:
                        raise WorkspaceRefused("apply_entry_not_file", path)
            finally:
                os.close(directory)
    finally:
        os.close(top)
    return found


def _check_existing_spelling(root_fd: int, parts: list[str]) -> None:
    """Refuse when an existing entry along `parts` is spelled differently on disk.

    A case- or normalization-insensitive destination resolves `src/a` to an
    existing `src/A`; writing through it would land beside the wrong name.
    Each component that exists is looked up by exact name in a bounded listing
    of its parent; the walk stops at the first component that does not exist
    or is not a directory (later checks handle those).
    """

    current = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            try:
                os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return
            if part not in _list_names(current, MAX_TREE_ENTRIES, "tree_too_large"):
                raise WorkspaceRefused("apply_path_collision", "/".join(parts[: index + 1]))
            try:
                following = _open_parents(current, [part], create=False)
            except WorkspaceRefused:
                return
            os.close(current)
            current = following
    finally:
        os.close(current)


def _install_file(parent: int, name: str, content: bytes, mode: str, *, replace: bool) -> None:
    """Write through a short random temporary name, then link it in (add) or replace (modify).

    An add never overwrites: `link` fails when the name exists, so a collision
    refuses with `apply_path_exists`.
    """

    temporary = f".cca-{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            os.fchmod(handle.fileno(), 0o755 if mode == EXECUTABLE_MODE else 0o644)
        if replace:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            return
        try:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError as exc:
            raise WorkspaceRefused("apply_path_exists", name) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)


def apply_patch_to_tree(
    patch: Mapping[str, Any],
    root: Path | str,
    *,
    verified: VerifiedTree | None = None,
    base_verified_by_caller: bool = False,
) -> None:
    """Apply a validated patch to a directory tree without following symlinks.

    The caller (integration) must first establish that the destination is
    exactly the patch's source commit and clean: pass the `VerifiedTree` from
    `verify_tree_at_commit`, or assert `base_verified_by_caller` after an
    equivalent check. Without either, apply refuses (`apply_base_unverified`).

    Before anything is written, every changed path is checked: an `old_blob`
    or `old_mode` mismatch refuses (`apply_blob_mismatch`,
    `apply_mode_mismatch`), an added path must be free (`apply_path_exists`),
    and paths that one filesystem entry would serve twice refuse
    (`apply_path_collision`). Deletes run first, so a file may become a
    directory and back; additions are created exclusively.
    """

    patch = _detach(patch)
    decoded = validate_patch(patch)
    if verified is not None:
        if not isinstance(verified, VerifiedTree) or verified.commit != patch["source_commit"]:
            raise WorkspaceRefused("apply_base_mismatch")
    elif not base_verified_by_caller:
        raise WorkspaceRefused("apply_base_unverified")
    _alias_check((change["path"] for change, _ in decoded), "apply_path_collision")
    object_format = "sha256" if len(patch["source_commit"]) == 64 else "sha1"
    deleted = {change["path"] for change, _ in decoded if change["op"] == "delete"}
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        if verified is not None and _identity(os.fstat(root_fd)) != verified.identity:
            raise WorkspaceRefused("apply_base_mismatch")
        tracker = _DirectoryTracker(root_fd, "apply_path_collision")
        file_owners: dict[tuple[int, int], str] = {}
        for change, _ in decoded:
            path = change["path"]
            parts = path.split("/")
            _check_existing_spelling(root_fd, parts if change["op"] != "add" else parts[:-1])
            try:
                parent = tracker.open(parts[:-1], create=False)
            except FileNotFoundError:
                if change["op"] != "add":
                    raise WorkspaceRefused("apply_blob_mismatch", path) from None
                continue
            except WorkspaceRefused as exc:
                # A file stands where a directory is needed; allowed only if it is deleted first.
                if exc.reason == "path_not_directory" and change["op"] == "add" \
                        and any(ancestor in deleted for ancestor in _ancestors(path)):
                    continue
                raise
            try:
                if change["op"] == "add":
                    below = _files_below(parent, parts[-1])
                    if below is not None:
                        if not {f"{path}/{name}" for name in below} <= deleted:
                            raise WorkspaceRefused("apply_path_exists", path)
                    elif _file_state(parent, parts[-1], object_format) is not None:
                        raise WorkspaceRefused("apply_path_exists", path)
                    continue
                state = _file_state(parent, parts[-1], object_format)
                if state is None or state[0] != change["old_blob"]:
                    raise WorkspaceRefused("apply_blob_mismatch", path)
                if state[1] != change["old_mode"]:
                    raise WorkspaceRefused("apply_mode_mismatch", path)
                if file_owners.setdefault(state[2], path) != path:
                    raise WorkspaceRefused("apply_path_collision", path)
            finally:
                os.close(parent)
        for change, _ in decoded:
            if change["op"] != "delete":
                continue
            parts = change["path"].split("/")
            parent = _open_parents(root_fd, parts[:-1], create=False)
            try:
                os.unlink(parts[-1], dir_fd=parent)
            finally:
                os.close(parent)
            _prune_empty(root_fd, parts[:-1])
        # Fresh identities: deletes may have freed directories whose inodes get reused.
        tracker = _DirectoryTracker(root_fd, "apply_path_collision")
        for change, content in decoded:
            if change["op"] == "delete":
                continue
            parts = change["path"].split("/")
            parent = tracker.open(parts[:-1], create=True)
            try:
                _install_file(parent, parts[-1], content or b"", change["mode"], replace=change["op"] == "modify")
            finally:
                os.close(parent)
    finally:
        os.close(root_fd)


def _prune_empty(root_fd: int, parts: list[str]) -> None:
    """Remove directories emptied by deletes, deepest first; stop at the first non-empty one."""

    for depth in range(len(parts), 0, -1):
        parent = _open_parents(root_fd, parts[: depth - 1], create=False)
        try:
            os.rmdir(parts[depth - 1], dir_fd=parent)
        except OSError:
            return
        finally:
            os.close(parent)


# --- verifier envelope -------------------------------------------------------


def build_verifier_envelope(
    patch: Mapping[str, Any], manifest: Mapping[str, Any], run_binding: Mapping[str, Any]
) -> dict[str, Any]:
    """The bridge-owned artifact a verifier receives.

    The patch and the trusted manifest are detached copies; the manifest must
    match the patch's source commit and manifest digest
    (`envelope_manifest_mismatch`).
    """

    patch = _detach(patch)
    manifest = _detach(manifest)
    decoded = validate_patch(patch)
    if not isinstance(manifest, dict) or manifest.get("source_commit") != patch["source_commit"] \
            or manifest_digest(manifest) != patch["manifest_digest"]:
        raise WorkspaceRefused("envelope_manifest_mismatch")
    if not isinstance(run_binding, Mapping) or not run_binding:
        raise WorkspaceRefused("run_binding_invalid")
    binding = {}
    for key, value in run_binding.items():
        if not isinstance(key, str) or not key:
            raise WorkspaceRefused("run_binding_invalid")
        if value is not None and not isinstance(value, (str, int, bool)):
            raise WorkspaceRefused("run_binding_invalid")
        binding[key] = value
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "run_binding": binding,
        "source_commit": patch["source_commit"],
        "manifest_digest": patch["manifest_digest"],
        "manifest": manifest,
        "patch_digest": patch_digest(patch),
        "patch": patch,
        "resulting_files": [
            {"path": change["path"], "sha256": change["new_sha256"], "content_b64": change["content_b64"]}
            for change, content in decoded if content is not None
        ],
    }
    if len(canonical_json(envelope)) > MAX_ENVELOPE_BYTES:
        raise WorkspaceRefused("envelope_too_large")
    return envelope


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(envelope)).hexdigest()


# --- cleanup -----------------------------------------------------------------


def _make_traversable(parent_fd: int, name: str) -> None:
    """Give a directory owner rwx without following a symlink at `name`."""

    if os.chmod in os.supports_dir_fd and os.chmod in os.supports_follow_symlinks:
        try:
            os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
            return
        except NotImplementedError:
            pass
        except OSError as exc:
            if exc.errno not in _CLONE_UNSUPPORTED:
                raise
    # Linux has no lchmod: pin the inode with O_PATH and chmod through procfs.
    o_path = getattr(os, "O_PATH", 0)
    if not o_path or not os.path.isdir("/proc/self/fd"):
        raise WorkspaceRefused("cleanup_incomplete", name)
    descriptor = os.open(name, o_path | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
    try:
        os.chmod(f"/proc/self/fd/{descriptor}", 0o700)
    finally:
        os.close(descriptor)


def _verified_parent(current: int, place: tuple[str, tuple[int, int]]) -> int:
    """Open `..` of `current` and prove `current` is still entry `name` of the expected parent."""

    name, expected = place
    here = _identity(os.fstat(current))
    parent = os.open("..", _DIRECTORY_FLAGS, dir_fd=current)
    try:
        link = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(os.fstat(parent)) != expected or _identity(link) != here:
            raise WorkspaceRefused("cleanup_relocated", name)
        return parent
    except BaseException:
        os.close(parent)
        raise


def _remove_children(top_fd: int, place: tuple[str, tuple[int, int]]) -> None:
    """Empty a directory without following symlinks, recursing or reopening from the top.

    It holds one directory descriptor at a time, streams each listing, and
    climbs back with `..`. Before unlinking anything in a directory, and
    before removing it, it re-proves the directory is still the expected
    entry of the expected parent (`cleanup_relocated` otherwise).
    """

    current = os.dup(top_fd)
    trail: list[tuple[str, tuple[int, int]]] = []
    try:
        while True:
            os.close(_verified_parent(current, place))
            subdirectory = None
            with os.scandir(current) as listing:
                for entry in listing:
                    status = os.stat(entry.name, dir_fd=current, follow_symlinks=False)
                    if stat.S_ISDIR(status.st_mode):
                        subdirectory = entry.name
                        break
                    os.unlink(entry.name, dir_fd=current)
            if subdirectory is not None:
                here = _identity(os.fstat(current))
                _make_traversable(current, subdirectory)
                child = os.open(subdirectory, _DIRECTORY_FLAGS, dir_fd=current)
                trail.append(place)
                place = (subdirectory, here)
                os.close(current)
                current = child
                continue
            if not trail:
                return
            parent = _verified_parent(current, place)
            os.close(current)
            current = parent
            os.rmdir(place[0], dir_fd=current)
            place = trail.pop()
    finally:
        os.close(current)


def remove_quiescent_tree(path: Path | str) -> None:
    """Remove a run directory or fenced leftover; only after its processes are proven gone.

    Never follows a symlink: a symlink at `path` is unlinked. Safe to repeat.
    """

    path = Path(path)
    parent = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        try:
            status = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(status.st_mode):
            os.unlink(path.name, dir_fd=parent)
            return
        _make_traversable(parent, path.name)
        directory = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            _remove_children(directory, (path.name, _identity(os.fstat(parent))))
        finally:
            os.close(directory)
        os.rmdir(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def fence_leftover(path: Path | str) -> Path | None:
    """Rename a run directory to a fenced leftover name in the same parent; None if it is gone.

    The rename moves the entry itself and follows nothing. The startup sweep
    removes fenced leftovers once their processes are proven gone.
    """

    path = Path(path)
    parent = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        try:
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        fenced = f"{path.name}{FENCED_MARKER}{secrets.token_hex(8)}"
        os.rename(path.name, fenced, src_dir_fd=parent, dst_dir_fd=parent)
        return path.parent / fenced
    finally:
        os.close(parent)


def cleanup_layout(layout: WorkspaceLayout, *, quiescent: bool = False) -> Path | None:
    """Remove or fence the workspace, the snapshot and the control directory; safe to repeat.

    Destructive removal needs proven quiescence: the caller passes
    `quiescent=True` only after its process checks found nothing still
    running in the workspace. Otherwise the run directory is only renamed to
    a fenced leftover (returned) for the startup sweep.
    """

    if layout.workspace.parent != layout.root or layout.control.parent != layout.root:
        raise WorkspaceRefused("layout_invalid")
    if not quiescent:
        return fence_leftover(layout.root)
    remove_quiescent_tree(layout.root)
    return None
