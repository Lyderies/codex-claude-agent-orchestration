"""Host-side locations that a sandboxed Codex session cannot reach.

A command the owner's Codex rule runs outside the sandbox must not execute or load
anything from a location the sandboxed model can write, and must not write
something there that it later trusts or hands to a child. On Codex 0.164 the
workspace-write sandbox can write the workspace (except its top-level `.git`,
`.codex` and `.agents`), `/tmp` and `$TMPDIR`; the per-user cache directory, the
learning state and the rest of the home directory stay read-only to it.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# The flags every Python launched outside the sandbox gets: isolated mode (no
# environment variables, user site-packages or script directory on the path), no
# bytecode written, and no site-packages at all, so no `.pth` or `sitecustomize`
# runs. The runtime needs only the standard library and its own package.
PYTHON_ISOLATION_FLAGS = ("-I", "-B", "-S")
# Group members may write these on macOS by design (Homebrew, python.org); the group
# is the administrators, who can already change anything on the machine.
MACOS_ADMIN_GROUP = 80
GETCONF = "/usr/bin/getconf"


class HostPathError(RuntimeError):
    """A host path is unsafe or unavailable; messages never carry the path."""


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def darwin_directory(name: str) -> str | None:
    """A per-user Darwin directory from getconf, or None."""

    try:
        info = os.lstat(GETCONF)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            return None
        completed = subprocess.run(
            [GETCONF, name], env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and os.path.isabs(value) else None


def temp_roots() -> set[Path]:
    """Every temporary directory a sandboxed session may write, resolved."""

    candidates = ["/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or ""]
    if sys.platform == "darwin":
        candidates.append(darwin_directory("DARWIN_USER_TEMP_DIR") or "")
    return {Path(os.path.realpath(item)) for item in candidates if os.path.isabs(item)}


def _component_problem(info: os.stat_result) -> str | None:
    uid = os.geteuid()
    if info.st_uid not in (0, uid):
        return "a path component belongs to another user"
    if stat.S_ISLNK(info.st_mode):
        return None
    writable_by_others = info.st_mode & stat.S_IWOTH
    writable_by_group = info.st_mode & stat.S_IWGRP and not (
        sys.platform == "darwin" and info.st_gid == MACOS_ADMIN_GROUP
    )
    if (writable_by_others or writable_by_group) and not (
        stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX and info.st_uid == 0
    ):
        return "a path component is writable by other users"
    return None


def trusted_location_problem(path: Path, untrusted: list[Path]) -> str | None:
    """Why `path` may not be trusted to run outside the sandbox, or None.

    The path must be canonical, so no symlink anywhere on it can later be
    retargeted: callers pin ``os.path.realpath`` and check that. Every
    component must belong to this user or root and be writable by no one else,
    and the path may not lie inside an untrusted root (the project, temp roots).
    """

    if not path.is_absolute():
        return "the path is not absolute"
    if Path(os.path.realpath(path)) != path:
        return "the path is not canonical"
    roots = [Path(os.path.realpath(root)) for root in untrusted]
    if any(within(path, root) for root in roots):
        return "the path lies inside the project or a temporary directory"
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return "a path component is unavailable"
        if stat.S_ISLNK(info.st_mode):
            return "a path component is a symbolic link"
        problem = _component_problem(info)
        if problem is not None:
            return problem
    return None


def overlaps_untrusted(path: Path, untrusted: list[Path]) -> bool:
    """Whether `path` is inside an untrusted root, or contains one."""

    resolved = Path(os.path.realpath(path))
    for root in untrusted:
        root = Path(os.path.realpath(root))
        if within(resolved, root) or within(root, resolved):
            return True
    return False


def _open_verified_directory(path: Path) -> tuple[int, Path]:
    """Open an existing directory by descriptors, never following a symlink.

    Root-owned system links (such as /var on macOS) are resolved first; any
    other symlink refuses. Every component must pass the ownership and
    writability rules. Returns the descriptor and the canonical path.
    """

    full = Path(os.path.abspath(path))
    for _ in range(40):
        current = Path(full.anchor)
        for index, part in enumerate(full.parts[1:]):
            current = current / part
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise HostPathError("a host directory is unavailable") from exc
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != 0:
                    raise HostPathError("a host directory path passes through a symlink")
                full = Path(os.path.realpath(current), *full.parts[index + 2:])
                break
        else:
            break
    else:
        raise HostPathError("a host directory path has too many symbolic links")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in full.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            problem = _component_problem(os.fstat(descriptor))
            if problem is not None:
                raise HostPathError(problem)
    except OSError as exc:
        os.close(descriptor)
        raise HostPathError("a host directory path passes through a symlink") from exc
    except HostPathError:
        os.close(descriptor)
        raise
    return descriptor, full


def _private_child(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise HostPathError("a private host directory is not owner-only")
    return descriptor


def private_directory(base: Path, container: str, prefix: str) -> Path:
    """Create a fresh owner-only directory at `<base>/<container>/<prefix><random>`.

    `base` must already exist; it and its ancestors are verified by directory
    descriptors with no-follow opens, and the container and the new directory
    are created and checked through those descriptors. Only the caller's
    process writes inside it, and it is never a sandbox-writable location when
    `base` is the learning state or the per-user cache directory.
    """

    try:
        base_fd, canonical = _open_verified_directory(base)
    except OSError as exc:
        raise HostPathError("a host directory is unavailable") from exc
    try:
        try:
            container_fd = _private_child(base_fd, container, create=True)
        except OSError as exc:
            raise HostPathError("a private host directory is unavailable") from exc
        try:
            for _ in range(64):
                name = f"{prefix}{secrets.token_hex(4)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=container_fd)
                except FileExistsError:
                    continue
                os.close(_private_child(container_fd, name, create=False))
                return canonical / container / name
            raise HostPathError("a private host directory could not be created")
        finally:
            os.close(container_fd)
    finally:
        os.close(base_fd)


def remove_private_directory(path: Path) -> None:
    """Remove a private directory, including read-only directories inside it."""

    for current, directories, _files in os.walk(path, followlinks=False):
        for name in directories:
            child = os.path.join(current, name)
            if not os.path.islink(child):
                try:
                    os.chmod(child, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)
