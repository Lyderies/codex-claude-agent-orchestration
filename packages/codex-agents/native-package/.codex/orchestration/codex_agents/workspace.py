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
# The envelope carries the patch and the resulting content of every changed
# file again, so it may hold each changed byte twice.
MAX_ENVELOPE_BYTES = 2 * MAX_PATCH_BYTES + 1024 * 1024

# Never exported as writable and never accepted in a change, at any depth and
# in any letter case. Compared after Unicode normalization and case folding.
PROTECTED_NAMES = frozenset(
    {".git", ".codex", ".agents", ".claude", "agents.md", "claude.md", "claude.local.md", ".mcp.json"}
    | {name.casefold() for name in CLAUDE_CONTEXT_NAMES}
)
REGULAR_MODE = "100644"
EXECUTABLE_MODE = "100755"
ENTRY_TYPES = ("file", "directory", "symlink", "fifo", "socket", "device")
SPECIAL_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
# Global options that keep project-side Git machinery out of every plumbing
# call: no replacement objects, no hooks, no filesystem monitor.
_GIT_HARDENING = (
    "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
)
_PATCH_KEYS = {"schema_version", "source_commit", "manifest_digest", "changes"}
_CHANGE_KEYS = {"path", "op", "old_blob", "new_sha256", "mode", "content_b64"}


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


# --- paths -------------------------------------------------------------------


def _fold(part: str) -> str:
    # Format characters are dropped because some filesystems ignore them in
    # names, so `.git` with a zero-width joiner must still count as `.git`.
    normalized = unicodedata.normalize("NFC", part)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf").casefold()


def is_protected_path(relative: str) -> bool:
    """True when any component of the path is a protected name, in any case."""

    return any(_fold(part) in PROTECTED_NAMES for part in PurePosixPath(relative).parts)


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

_LAZY_FETCH_FLAG: tuple[str, ...] | None = None


def _git_options(root: Path) -> tuple[str, ...]:
    # `--no-lazy-fetch` (Git 2.44+) stops a partial clone from fetching a
    # missing blob through a remote. Older clients do not know the option.
    global _LAZY_FETCH_FLAG
    if _LAZY_FETCH_FLAG is None:
        probe = bridge._git(root, "--no-lazy-fetch", "version")
        _LAZY_FETCH_FLAG = ("--no-lazy-fetch",) if probe.returncode == 0 else ()
    return (*_LAZY_FETCH_FLAG, *_GIT_HARDENING)


def _plumbing(root: Path, *arguments: str, text: bool = False):
    return bridge._git(root, *_git_options(root), *arguments, text=text)


def _git_blob_id(content: bytes, object_format: str) -> str:
    algorithm = "sha256" if object_format == "sha256" else "sha1"
    return hashlib.new(algorithm, b"blob %d\x00" % len(content) + content).hexdigest()


def _hex(value: str, lengths: tuple[int, ...] = (40, 64)) -> bool:
    return len(value) in lengths and all(ch in "0123456789abcdef" for ch in value)


def resolve_source_commit(repository_root: Path, revision: str = "HEAD") -> tuple[str, str]:
    """Resolve one commit; return `(commit, object_format)`."""

    root = Path(repository_root).resolve()
    top = _plumbing(root, "rev-parse", "--show-toplevel", text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        raise WorkspaceRefused("repository_root_invalid")
    if not revision or revision.startswith("-"):
        raise WorkspaceRefused("revision_invalid")
    resolved = _plumbing(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", text=True)
    commit = resolved.stdout.strip()
    if resolved.returncode != 0 or not _hex(commit):
        raise WorkspaceRefused("source_commit_unresolved")
    fmt = _plumbing(root, "rev-parse", "--show-object-format", text=True)
    object_format = fmt.stdout.strip() if fmt.returncode == 0 else "sha1"
    if object_format not in {"sha1", "sha256"}:
        raise WorkspaceRefused("object_format_unsupported")
    if len(commit) != (64 if object_format == "sha256" else 40):
        raise WorkspaceRefused("source_commit_unresolved")
    return commit, object_format


# --- export ------------------------------------------------------------------


def _list_committed(root: Path, commit: str, requested: str) -> list[tuple[str, str, str, int, str]]:
    listing = _plumbing(root, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, "--", requested)
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
        found.append((mode, kind, sha, -1 if size == "-" else int(size), relative))
    return found


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
    checkout configuration runs. A write path may be absent from the commit
    (the child creates it); a read path must exist. Claude context files are
    skipped; a symlink, submodule or `.git` component refuses the export.
    Files outside the write set, and protected names, are exported read-only.
    """

    root = Path(repository_root).resolve()
    writable = _collapse(_literal_path(path) for path in write_set)
    for path in writable:
        if is_protected_path(path):
            raise WorkspaceRefused("write_path_protected", path)
    requested_paths = sorted({_literal_path(path) for path in read_paths} | set(writable))
    if not requested_paths:
        raise WorkspaceRefused("export_empty")
    commit, object_format = resolve_source_commit(root, revision)
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
            if kind != "blob" or mode not in {REGULAR_MODE, EXECUTABLE_MODE} or not _hex(sha):
                raise WorkspaceRefused("export_entry_unsupported", relative)
            entries[relative] = (sha, mode, size)
    if len(entries) > max_files:
        raise WorkspaceRefused("export_too_many_files")
    if sum(size for _, _, size in entries.values()) > max_bytes:
        raise WorkspaceRefused("export_too_large")
    workspace_fd = os.open(layout.workspace, _DIRECTORY_FLAGS)
    files: dict[str, dict[str, Any]] = {}
    total = 0
    try:
        for relative, (sha, mode, _) in sorted(entries.items()):
            parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise WorkspaceRefused("path_outside_repository", relative)
            blob = _plumbing(root, "cat-file", "blob", sha)
            content = blob.stdout
            if blob.returncode != 0 or _git_blob_id(content, object_format) != sha:
                raise WorkspaceRefused("export_blob_unavailable", relative)
            total += len(content)
            if total > max_bytes:
                raise WorkspaceRefused("export_too_large")
            writable_file = _in_write_set(relative, writable) and not is_protected_path(relative)
            permissions = 0o644 if writable_file else 0o444
            if mode == EXECUTABLE_MODE:
                permissions |= 0o111
            parent_fd = _open_parents(workspace_fd, parts[:-1], create=True)
            try:
                try:
                    descriptor = os.open(parts[-1], _CREATE_FLAGS, 0o600, dir_fd=parent_fd)
                except FileExistsError as exc:
                    # Two committed names that collide on a case-insensitive filesystem.
                    raise WorkspaceRefused("export_path_collision", relative) from exc
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    os.fchmod(handle.fileno(), permissions)
            finally:
                os.close(parent_fd)
            files[relative] = {
                "blob": sha, "mode": mode, "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    finally:
        os.close(workspace_fd)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_commit": commit,
        "object_format": object_format,
        "write_set": writable,
        "files": files,
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


# --- no-follow directory helpers ---------------------------------------------


def _open_parents(base_fd: int, parts: list[str], *, create: bool) -> int:
    """Open the directory at `parts` below `base_fd`, one component at a time, never following."""

    current = os.dup(base_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
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


def _read_bounded(descriptor: int, limit: int) -> bytes:
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
        raise WorkspaceRefused("inventory_too_large")
    return data


# --- snapshot ----------------------------------------------------------------

CLONE_NOFOLLOW = 0x0001
CLONE_NOOWNERCOPY = 0x0002


def _load_clonefileat():
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).clonefileat
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    return function


# The APFS clone (what `cp -c` uses). None selects the portable copy.
_CLONEFILEAT = _load_clonefileat()


def _clone_file(src_fd: int, name: str, dst_fd: int) -> bool:
    """Clone one entry without following it; False when cloning is unavailable here."""

    if _CLONEFILEAT is None:
        return False
    encoded = os.fsencode(name)
    if _CLONEFILEAT(src_fd, encoded, dst_fd, encoded, CLONE_NOFOLLOW | CLONE_NOOWNERCOPY) == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOTSUP, errno.EXDEV, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
        return False
    raise OSError(error, os.strerror(error), name)


def _copy_file(src_fd: int, name: str, dst_fd: int, limit: int) -> os.stat_result | None:
    """Copy a regular file without following; return its source status, or None if it is not regular."""

    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=src_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP, errno.ENOTSUP}:
            return None
        raise
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            return None
        content = _read_bounded(descriptor, limit)
    finally:
        os.close(descriptor)
    target = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=dst_fd)
    with os.fdopen(target, "wb") as handle:
        handle.write(content)
    return status


def _snapshot_workspace(layout: WorkspaceLayout, max_entries: int, max_bytes: int) -> Path:
    os.mkdir(layout.snapshot, 0o700)
    try:
        source_root = os.open(layout.workspace, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise WorkspaceRefused("workspace_not_directory") from exc
    snapshot_root = os.open(layout.snapshot, _DIRECTORY_FLAGS)
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    try:
        pending = [""]
        while pending:
            relative = pending.pop()
            parts = relative.split("/") if relative else []
            source = _open_parents(source_root, parts, create=False)
            target = _open_parents(snapshot_root, parts, create=False)
            try:
                with os.scandir(source) as listing:
                    names = sorted(entry.name for entry in listing)
                for name in names:
                    path = _checked_name(name, relative)
                    if len(entries) >= max_entries:
                        raise WorkspaceRefused("inventory_too_many_entries")
                    status = os.stat(name, dir_fd=source, follow_symlinks=False)
                    kind = _entry_type(status.st_mode)
                    if kind == "directory":
                        if path.count("/") + 1 >= MAX_INVENTORY_DEPTH:
                            raise WorkspaceRefused("inventory_too_deep", path)
                        os.mkdir(name, 0o700, dir_fd=target)
                        pending.append(path)
                    elif kind == "file":
                        if total + status.st_size > max_bytes:
                            raise WorkspaceRefused("inventory_too_large")
                        if _clone_file(source, name, target):
                            cloned = os.stat(name, dir_fd=target, follow_symlinks=False)
                            if stat.S_ISDIR(cloned.st_mode):
                                raise WorkspaceRefused("snapshot_unstable", path)
                            if not stat.S_ISREG(cloned.st_mode):
                                # Swapped for something else after the first look.
                                os.unlink(name, dir_fd=target)
                                kind = _entry_type(cloned.st_mode)
                            elif total + cloned.st_size > max_bytes:
                                raise WorkspaceRefused("inventory_too_large")
                            else:
                                # The recorded mode is authoritative; the copy stays readable.
                                os.chmod(name, 0o600, dir_fd=target, follow_symlinks=False)
                                total += cloned.st_size
                        else:
                            copied = _copy_file(source, name, target, max_bytes - total)
                            if copied is None:
                                status = os.stat(name, dir_fd=source, follow_symlinks=False)
                                kind = _entry_type(status.st_mode)
                                if kind in {"file", "directory"}:
                                    raise WorkspaceRefused("snapshot_unstable", path)
                            else:
                                status = copied
                                total += os.stat(name, dir_fd=target, follow_symlinks=False).st_size
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


# --- inventory ---------------------------------------------------------------


def _inventory_snapshot(
    layout: WorkspaceLayout, max_entries: int, max_bytes: int
) -> dict[str, dict[str, Any]]:
    recorded = _read_private_json(layout.snapshot_entries)
    if not isinstance(recorded, dict):
        raise WorkspaceRefused("snapshot_invalid")
    inventory: dict[str, dict[str, Any]] = {}
    total = 0
    root = os.open(layout.snapshot, _DIRECTORY_FLAGS)
    try:
        pending = [""]
        while pending:
            relative = pending.pop()
            directory = _open_parents(root, relative.split("/") if relative else [], create=False)
            try:
                with os.scandir(directory) as listing:
                    names = sorted(entry.name for entry in listing)
                for name in names:
                    path = _checked_name(name, relative)
                    if len(inventory) >= max_entries:
                        raise WorkspaceRefused("inventory_too_many_entries")
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
                        if path.count("/") + 1 >= MAX_INVENTORY_DEPTH:
                            raise WorkspaceRefused("inventory_too_deep", path)
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
    for path, record in recorded.items():
        if path in inventory:
            continue
        kind = record.get("type") if isinstance(record, dict) else None
        if kind not in {"symlink", "fifo", "socket", "device"}:
            raise WorkspaceRefused("snapshot_inconsistent", str(path))
        if len(inventory) >= max_entries:
            raise WorkspaceRefused("inventory_too_many_entries")
        inventory[path] = {
            "type": kind, "mode": int(record["mode"]), "nlink": int(record["nlink"]),
            "size": 0, "sha256": None,
        }
    return dict(sorted(inventory.items()))


def snapshot_workspace(
    layout: WorkspaceLayout,
    *,
    max_entries: int = MAX_INVENTORY_ENTRIES,
    max_bytes: int = MAX_INVENTORY_BYTES,
) -> Path:
    """Copy the stopped child's workspace into the control directory, never following symlinks.

    Regular files are cloned (APFS) or copied; directories are recreated.
    Every entry's type, mode and link count is recorded from the workspace
    itself, and symlinks, FIFOs, sockets and devices are recorded by type
    only. Exceeding a bound refuses.
    """

    try:
        return _snapshot_workspace(layout, max_entries, max_bytes)
    except OSError as exc:
        raise WorkspaceRefused("snapshot_failed") from exc


def inventory_snapshot(
    layout: WorkspaceLayout,
    *,
    max_entries: int = MAX_INVENTORY_ENTRIES,
    max_bytes: int = MAX_INVENTORY_BYTES,
) -> dict[str, dict[str, Any]]:
    """A bounded no-follow walk of the snapshot, ignoring nothing.

    Each path maps to `{type, mode, nlink, size, sha256}`; `sha256` is set for
    regular files only. Link counts and non-regular entries come from the
    record taken at snapshot time, because a copy has its own link count.
    """

    try:
        return _inventory_snapshot(layout, max_entries, max_bytes)
    except OSError as exc:
        raise WorkspaceRefused("inventory_failed") from exc


# --- ownership check ---------------------------------------------------------


def _git_mode(permissions: int) -> str:
    return EXECUTABLE_MODE if permissions & stat.S_IXUSR else REGULAR_MODE


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def check_ownership(manifest: Mapping[str, Any], inventory: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """Compare the snapshot inventory with the baseline; return the changes or refuse.

    Each change is `{path, op, old_blob, mode, sha256, size}`. Refusal codes:
    `symlink_entry`, `special_entry`, `hard_link`, `protected_name`,
    `outside_write_set`, `mode_change`, `unneeded_directory`,
    `file_too_large`, `too_many_changes`.
    """

    baseline: Mapping[str, Mapping[str, Any]] = manifest["files"]
    write_set = list(manifest["write_set"])
    baseline_dirs = {ancestor for path in baseline for ancestor in _ancestors(path)}
    # New directories are allowed only as ancestors of files inside the write set.
    needed_dirs = {
        ancestor
        for path, entry in inventory.items()
        if entry["type"] == "file" and _in_write_set(path, write_set)
        for ancestor in _ancestors(path)
    }
    changes: list[dict] = []
    for path, entry in sorted(inventory.items()):
        kind = entry["type"]
        if kind == "symlink":
            raise WorkspaceRefused("symlink_entry", path)
        if kind in {"fifo", "socket", "device"}:
            raise WorkspaceRefused("special_entry", path)
        if kind == "directory":
            if path in baseline_dirs:
                continue
            if is_protected_path(path):
                raise WorkspaceRefused("protected_name", path)
            if path not in needed_dirs:
                raise WorkspaceRefused("unneeded_directory", path)
            continue
        if entry["nlink"] > 1:
            raise WorkspaceRefused("hard_link", path)
        old = baseline.get(path)
        new_mode = _git_mode(entry["mode"])
        if old is not None and old["sha256"] == entry["sha256"] and old["mode"] == new_mode \
                and not entry["mode"] & SPECIAL_MODE_BITS:
            continue
        if is_protected_path(path):
            raise WorkspaceRefused("protected_name", path)
        owned = _in_write_set(path, write_set)
        content_changed = old is None or old["sha256"] != entry["sha256"]
        if content_changed and not owned:
            raise WorkspaceRefused("outside_write_set", path)
        if entry["mode"] & SPECIAL_MODE_BITS or (old is not None and old["mode"] != new_mode and not owned):
            raise WorkspaceRefused("mode_change", path)
        if entry["size"] > MAX_CHANGED_FILE_BYTES:
            raise WorkspaceRefused("file_too_large", path)
        changes.append({
            "path": path, "op": "add" if old is None else "modify",
            "old_blob": None if old is None else old["blob"],
            "mode": new_mode, "sha256": entry["sha256"], "size": entry["size"],
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
            "path": path, "op": "delete", "old_blob": old["blob"], "mode": old["mode"],
            "sha256": None, "size": 0,
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
            "new_sha256": change["sha256"], "mode": change["mode"], "content_b64": content_b64,
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
    """Check a patch's shape, paths, content digests and caps; return changes with decoded content."""

    if not isinstance(patch, dict) or set(patch) != _PATCH_KEYS:
        raise WorkspaceRefused("patch_invalid")
    if patch["schema_version"] != SCHEMA_VERSION or not isinstance(patch["source_commit"], str) \
            or not _hex(patch["source_commit"]) or not isinstance(patch["manifest_digest"], str) \
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
        op, old_blob, new_sha, mode, content_b64 = (
            change["op"], change["old_blob"], change["new_sha256"], change["mode"], change["content_b64"],
        )
        if mode not in {REGULAR_MODE, EXECUTABLE_MODE} or op not in {"add", "modify", "delete"}:
            raise WorkspaceRefused("patch_invalid", path)
        if (op == "add") != (old_blob is None) or (old_blob is not None and not (
                isinstance(old_blob, str) and _hex(old_blob))):
            raise WorkspaceRefused("patch_invalid", path)
        if op == "delete":
            if new_sha is not None or content_b64 is not None:
                raise WorkspaceRefused("patch_invalid", path)
            decoded.append((change, None))
            continue
        if not isinstance(new_sha, str) or not _hex(new_sha, (64,)) or not isinstance(content_b64, str):
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


def _blob_of(parent_fd: int, name: str, object_format: str) -> str | None:
    """The Git blob id of a regular file, or None when it does not exist; refuse anything else."""

    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise WorkspaceRefused("apply_entry_not_file", name)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceRefused("apply_entry_not_file", name)
        content = _read_bounded(descriptor, MAX_INVENTORY_BYTES)
    finally:
        os.close(descriptor)
    return _git_blob_id(content, object_format)


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
                with os.scandir(directory) as listing:
                    names = [entry.name for entry in listing]
                for child in names:
                    path = f"{relative}/{child}" if relative else child
                    kind = _entry_type(os.stat(child, dir_fd=directory, follow_symlinks=False).st_mode)
                    if kind == "directory":
                        if path.count("/") + 1 >= MAX_INVENTORY_DEPTH:
                            raise WorkspaceRefused("inventory_too_deep", path)
                        pending.append(path)
                    elif kind == "file":
                        found.add(path)
                    else:
                        raise WorkspaceRefused("apply_entry_not_file", path)
                    if len(found) + len(pending) > MAX_INVENTORY_ENTRIES:
                        raise WorkspaceRefused("inventory_too_many_entries")
            finally:
                os.close(directory)
    finally:
        os.close(top)
    return found


def apply_patch_to_tree(patch: Mapping[str, Any], root: Path | str) -> None:
    """Apply a validated patch to a directory tree without following symlinks.

    Every `old_blob` is verified against the tree, and every added path is
    checked to be free, before anything is written; a mismatch refuses with
    `apply_blob_mismatch` or `apply_path_exists`. Deletes run first, so a
    file may become a directory and back.
    """

    decoded = validate_patch(dict(patch))
    object_format = "sha256" if len(patch["source_commit"]) == 64 else "sha1"
    deleted = {change["path"] for change, _ in decoded if change["op"] == "delete"}
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for change, _ in decoded:
            parts = change["path"].split("/")
            try:
                parent = _open_parents(root_fd, parts[:-1], create=False)
            except FileNotFoundError:
                if change["op"] != "add":
                    raise WorkspaceRefused("apply_blob_mismatch", change["path"]) from None
                continue
            except WorkspaceRefused:
                # A file stands where a directory is needed; only allowed if it is deleted first.
                prefix = next((a for a in _ancestors(change["path"]) if a in deleted), None)
                if change["op"] == "add" and prefix is not None:
                    continue
                raise
            try:
                if change["op"] == "add":
                    below = _files_below(parent, parts[-1])
                    if below is not None:
                        if not {f"{change['path']}/{name}" for name in below} <= deleted:
                            raise WorkspaceRefused("apply_path_exists", change["path"])
                    elif _blob_of(parent, parts[-1], object_format) is not None:
                        raise WorkspaceRefused("apply_path_exists", change["path"])
                elif _blob_of(parent, parts[-1], object_format) != change["old_blob"]:
                    raise WorkspaceRefused("apply_blob_mismatch", change["path"])
            finally:
                os.close(parent)
        ordered = [item for item in decoded if item[0]["op"] == "delete"]
        ordered += [item for item in decoded if item[0]["op"] != "delete"]
        for change, content in ordered:
            parts = change["path"].split("/")
            if change["op"] == "delete":
                parent = _open_parents(root_fd, parts[:-1], create=False)
                try:
                    os.unlink(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
                _prune_empty(root_fd, parts[:-1])
                continue
            parent = _open_parents(root_fd, parts[:-1], create=True)
            try:
                temporary = f".{parts[-1]}.patch-{secrets.token_hex(8)}"
                descriptor = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=parent)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content or b"")
                        os.fchmod(handle.fileno(), 0o755 if change["mode"] == EXECUTABLE_MODE else 0o644)
                    os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                except BaseException:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=parent)
                    raise
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


def build_verifier_envelope(patch: Mapping[str, Any], run_binding: Mapping[str, Any]) -> dict[str, Any]:
    """The bridge-owned artifact a verifier receives: the patch, its digests and the resulting files."""

    decoded = validate_patch(dict(patch))
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
        "patch_digest": patch_digest(patch),
        "patch": dict(patch),
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
            if exc.errno not in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
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


def _remove_children(top_fd: int) -> None:
    """Empty a directory without following symlinks, recursing or reopening from the top.

    It holds one directory descriptor at a time and climbs back with `..`,
    checking each step against the identities recorded on the way down, so a
    directory moved by a surviving process stops the removal instead of
    redirecting it.
    """

    current = os.dup(top_fd)
    trail: list[tuple[str, tuple[int, int]]] = []
    try:
        while True:
            with os.scandir(current) as listing:
                names = [entry.name for entry in listing]
            subdirectory = None
            for name in names:
                status = os.stat(name, dir_fd=current, follow_symlinks=False)
                if stat.S_ISDIR(status.st_mode):
                    subdirectory = subdirectory or name
                else:
                    os.unlink(name, dir_fd=current)
            if subdirectory is not None:
                here = os.fstat(current)
                _make_traversable(current, subdirectory)
                child = os.open(subdirectory, _DIRECTORY_FLAGS, dir_fd=current)
                trail.append((subdirectory, (here.st_dev, here.st_ino)))
                os.close(current)
                current = child
                continue
            if not trail:
                return
            name, expected = trail.pop()
            here = os.fstat(current)
            parent = os.open("..", _DIRECTORY_FLAGS, dir_fd=current)
            above = os.fstat(parent)
            link = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (above.st_dev, above.st_ino) != expected or (link.st_dev, link.st_ino) != (
                    here.st_dev, here.st_ino):
                os.close(parent)
                raise WorkspaceRefused("cleanup_incomplete", name)
            os.close(current)
            current = parent
            os.rmdir(name, dir_fd=current)
    finally:
        os.close(current)


def _remove_path(path: Path) -> None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return
    parent = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        if not stat.S_ISDIR(status.st_mode):
            os.unlink(path.name, dir_fd=parent)
            return
        _make_traversable(parent, path.name)
        directory = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            _remove_children(directory)
        finally:
            os.close(directory)
        os.rmdir(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def cleanup_layout(layout: WorkspaceLayout) -> None:
    """Remove the workspace, the snapshot and the control directory; safe to repeat.

    All three live under the private run directory, which is removed as one
    no-follow tree; a symlink planted in its place is unlinked, not followed.
    """

    if layout.workspace.parent != layout.root or layout.control.parent != layout.root:
        raise WorkspaceRefused("layout_invalid")
    _remove_path(layout.root)
