"""Integrate a verified write patch into the primary checkout (G3).

One serialized operation under the ledger writer lock:

1. reconcile any earlier promotion left unfinished, then check the
   preconditions: a passed write result, an `accepted` other-provider
   verification, not yet integrated, and the calling host owns the run;
2. the primary checkout is on a named branch at the patch's source commit,
   with a clean index, every tracked file equal to that commit, and nothing
   at any path the patch adds;
3. a detached disposable worktree is registered without a checkout, in a
   private directory under the system temp directory, and the source
   commit's raw blobs are written into it by this module; it is verified and
   the patch is applied to it;
4. the project's operational checks run there, through the sandboxed runner;
5. the patched tree is committed with plumbing under a private index, with
   the primary's author identity;
6. promotion takes Git's own `HEAD.lock`, branch ref lock and `index.lock`,
   re-verifies the primary under them, records a `promoting` intent, writes
   the new index into `index.lock` and the patch's raw bytes into the
   working tree, then renames the index and the ref into place (Git's own
   commit protocol); any failure before the ref rename restores every
   touched path and the old index;
7. a `write_integration` record is written, for refusals too;
8. the worktree is removed on every terminal state.

No attribute-aware Git command runs: no checkout, merge, switch or
`worktree add` with a checkout, and no filter. The primary checkout is
written only by step 6, after every check passed. Every Git call uses the
contained workspace's hardening: no hooks, no filesystem monitor, no lazy
fetch, no replacement objects and literal pathspecs. Refusals carry stable
reason codes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from . import workspace
from .bridge import BridgeRefused
from .contracts import ContractError
from .learning_contracts import content_digest
from .workspace import WorkspaceRefused

# Where disposable worktrees are created; None means the system temp directory.
INTEGRATION_PARENT: str | None = None
WORKTREE_PREFIX = "codex-agents-integrate-"
GIT_TIMEOUT_SECONDS = 60
_DEFERRED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_LOCK_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | workspace._O_CLOEXEC
_BRANCH_REF = re.compile(r"refs/heads/[^\x00-\x20\x7f~^:?*\[\\]{1,1000}")


class IntegrationRefused(Exception):
    """An integration step refused; `reason` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- git ---------------------------------------------------------------------


def _git(
    root: Path,
    *arguments: str,
    stdin: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
    timeout: int = GIT_TIMEOUT_SECONDS,
    new_session: bool = False,
) -> subprocess.CompletedProcess:
    """Hardened Git, as `workspace._plumbing`, with optional input and environment.

    The environment is the bridge's fixed one (no HOME, so no global
    configuration), plus only the named identity variables.
    """

    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise IntegrationRefused("write_git_unavailable")
    environment = {
        "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "GIT_LITERAL_PATHSPECS": "1",
        **(extra_env or {}),
    }
    try:
        return subprocess.run(
            [executable, "--literal-pathspecs", "-C", str(root), *workspace._GIT_HARDENING,
             *arguments],
            env=environment,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            start_new_session=new_session,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationRefused("write_git_failed") from exc


def _output(result: subprocess.CompletedProcess) -> str:
    if result.returncode != 0:
        raise IntegrationRefused("write_git_failed")
    return result.stdout.decode("utf-8", "strict").strip()


def author_identity(root: Path) -> tuple[str, str]:
    """The primary repository's configured `user.name` and `user.email`.

    Read with `git config --get`, which executes nothing, from the
    repository, global and system configuration (HOME and XDG_CONFIG_HOME
    are passed for this read only). Missing either refuses with
    `write_identity_missing`.
    """

    extra = {
        name: os.environ[name] for name in ("HOME", "XDG_CONFIG_HOME") if os.environ.get(name)
    }
    values = []
    for key in ("user.name", "user.email"):
        result = _git(root, "config", "--get", key, extra_env=extra)
        value = result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
        if not value or any(ord(character) < 32 or character in "<>" for character in value):
            raise IntegrationRefused("write_identity_missing")
        values.append(value)
    return values[0], values[1]


# --- the primary checkout ----------------------------------------------------


def _tracked_files_clean(root: Path, commit: str, object_format: str) -> bool:
    """Whether every path tracked at `commit` exists in the checkout with its exact blob and mode.

    The comparison is this module's, not Git's: each file is opened without
    following symlinks and hashed, so no filter, attribute or monitor runs.
    Untracked and ignored files are not considered here.
    """

    total = 0
    entries = workspace._list_committed(root, commit, None)
    if len(entries) > workspace.MAX_TREE_ENTRIES:
        raise IntegrationRefused("write_primary_dirty")
    root_fd = os.open(root, workspace._DIRECTORY_FLAGS)
    try:
        for mode, kind, sha, _, relative in entries:
            if mode == "160000" or kind != "blob":
                return False
            parts = relative.split("/")
            try:
                parent = workspace._open_parents(root_fd, parts[:-1], create=False)
            except (OSError, WorkspaceRefused):
                return False
            try:
                try:
                    status = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if stat.S_ISLNK(status.st_mode):
                    target = os.readlink(parts[-1], dir_fd=parent)
                    actual = ("120000", workspace._git_blob_id(os.fsencode(target), object_format))
                elif stat.S_ISREG(status.st_mode):
                    descriptor = os.open(parts[-1], workspace._READ_FLAGS, dir_fd=parent)
                    try:
                        pinned = os.fstat(descriptor)
                        content = workspace._read_bounded(
                            descriptor, workspace.MAX_TREE_BYTES - total, "tree_too_large"
                        )
                    finally:
                        os.close(descriptor)
                    total += len(content)
                    actual = (
                        workspace._git_mode(pinned.st_mode),
                        workspace._git_blob_id(content, object_format),
                    )
                else:
                    return False
                if actual != (mode, sha):
                    return False
            finally:
                os.close(parent)
    except WorkspaceRefused:
        return False
    finally:
        os.close(root_fd)
    return True


def _added_path_occupied(root: Path, path: str, deleted: set[str]) -> bool:
    """Whether anything stands at a path the patch adds, or at a non-directory ancestor of it.

    An ancestor that is a file the patch deletes is expected; anything else
    there (an untracked or ignored file, link or directory) is a collision.
    """

    parts = path.split("/")
    current = os.open(root, workspace._DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(parts):
            try:
                status = os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if index == len(parts) - 1:
                return True
            if not stat.S_ISDIR(status.st_mode):
                return "/".join(parts[: index + 1]) not in deleted
            following = os.open(part, workspace._DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    finally:
        os.close(current)
    return False


def check_primary(
    root: Path,
    patch: Mapping[str, Any],
    object_format: str,
    *,
    branch: str | None = None,
) -> str:
    """Refuse unless the primary can take the patch as a fast-forward; return its branch ref.

    `write_base_moved`: HEAD is detached, not at the patch's source commit,
    or (with `branch`) on another branch. `write_primary_dirty`: staged or
    unmerged changes, or a tracked file that differs from the source commit.
    `write_primary_collision`: something already stands at a path the patch
    adds.
    """

    symbolic = _git(root, "symbolic-ref", "-q", "HEAD")
    ref = symbolic.stdout.decode("utf-8", "replace").strip() if symbolic.returncode == 0 else ""
    if not ref.startswith("refs/heads/") or (branch is not None and ref != branch):
        raise IntegrationRefused("write_base_moved")
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head.returncode != 0 or head.stdout.decode("ascii", "replace").strip() != patch["source_commit"]:
        raise IntegrationRefused("write_base_moved")
    staged = _git(root, "diff-index", "--cached", "--quiet", "--no-renames", patch["source_commit"], "--")
    if staged.returncode == 1:
        raise IntegrationRefused("write_primary_dirty")
    if staged.returncode != 0:
        raise IntegrationRefused("write_git_failed")
    unmerged = _git(root, "ls-files", "-u", "-z")
    if unmerged.returncode != 0 or unmerged.stdout:
        raise IntegrationRefused("write_primary_dirty")
    if not _tracked_files_clean(root, patch["source_commit"], object_format):
        raise IntegrationRefused("write_primary_dirty")
    deleted = {change["path"] for change in patch["changes"] if change["op"] == "delete"}
    for change in patch["changes"]:
        if change["op"] == "add" and _added_path_occupied(root, change["path"], deleted):
            raise IntegrationRefused("write_primary_collision")
    return ref


# --- the disposable worktree ---------------------------------------------------


def _private_parent(root: Path) -> Path:
    base = Path(os.path.realpath(INTEGRATION_PARENT or tempfile.gettempdir()))
    private = Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX, dir=base))
    os.chmod(private, 0o700)
    if private.is_relative_to(root) or root.is_relative_to(private):
        shutil.rmtree(private, ignore_errors=True)
        raise IntegrationRefused("write_worktree_inside_repository")
    return private


def remove_worktree(root: Path, private: Path) -> None:
    """Remove the disposable worktree and its registration; never raises.

    Termination signals are held until the removal is complete.
    """

    with deferred_signals():
        tree = private / "tree"
        with contextlib.suppress(IntegrationRefused):
            _git(root, "worktree", "remove", "--force", "--force", str(tree))
        shutil.rmtree(private, ignore_errors=True)
        with contextlib.suppress(IntegrationRefused):
            _git(root, "worktree", "prune")


def _materialize(root: Path, tree: Path, commit: str, object_format: str) -> None:
    """Write `commit`'s raw blobs into `tree`, with their modes, following no symlinks.

    Blobs come from `cat-file --batch`, so no filter, attribute or checkout
    configuration runs. A submodule or a `.git` component refuses
    (`write_tree_unsupported`).
    """

    entries = workspace._list_committed(root, commit, None)
    if len(entries) > workspace.MAX_TREE_ENTRIES:
        raise IntegrationRefused("write_tree_too_large")
    for mode, kind, sha, _, relative in entries:
        if (
            kind != "blob" or mode not in {"100644", "100755", "120000"}
            or any(workspace._fold(part) == ".git" for part in relative.split("/"))
        ):
            raise IntegrationRefused("write_tree_unsupported")
    total = sum(max(size, 0) for _, _, _, size, _ in entries)
    if total > workspace.MAX_TREE_BYTES:
        raise IntegrationRefused("write_tree_too_large")
    batch = _git(
        root, "cat-file", "--batch",
        stdin=b"".join(f"{sha}\n".encode() for _, _, sha, _, _ in entries),
        timeout=600,
    )
    if batch.returncode != 0:
        raise IntegrationRefused("write_git_failed")
    output = batch.stdout
    offset = 0
    tree_fd = os.open(tree, workspace._DIRECTORY_FLAGS)
    try:
        tracker = workspace._DirectoryTracker(tree_fd, "write_tree_collision")
        for mode, _, sha, _, relative in entries:
            newline = output.find(b"\n", offset)
            header = output[offset:newline].decode("ascii", "replace").split()
            if newline < 0 or len(header) != 3 or header[0] != sha or header[1] != "blob":
                raise IntegrationRefused("write_tree_unavailable")
            size = int(header[2])
            content = output[newline + 1: newline + 1 + size]
            offset = newline + 1 + size + 1
            if len(content) != size or workspace._git_blob_id(content, object_format) != sha:
                raise IntegrationRefused("write_tree_unavailable")
            parts = relative.split("/")
            parent = tracker.open(parts[:-1], create=True)
            try:
                if mode == "120000":
                    os.symlink(os.fsdecode(content), parts[-1], dir_fd=parent)
                    continue
                try:
                    descriptor = os.open(parts[-1], workspace._CREATE_FLAGS, 0o600, dir_fd=parent)
                except FileExistsError as exc:
                    raise IntegrationRefused("write_tree_collision") from exc
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    os.fchmod(handle.fileno(), 0o755 if mode == "100755" else 0o644)
            finally:
                os.close(parent)
    finally:
        os.close(tree_fd)


def _prepare_worktree(root: Path, private: Path, source: str, object_format: str) -> Path:
    """Register a detached worktree without checking out, and fill it from raw blobs."""

    tree = private / "tree"
    added = _git(root, "worktree", "add", "--no-checkout", "--detach", "--quiet", str(tree), source)
    if added.returncode != 0:
        raise IntegrationRefused("write_worktree_failed")
    tree = Path(os.path.realpath(tree))
    # The worktree's own index, read from the tree object: no working-tree file is read.
    if _git(tree, "read-tree", source).returncode != 0:
        raise IntegrationRefused("write_worktree_failed")
    _materialize(root, tree, source, object_format)
    return tree


def _index_lines(decoded: list[tuple[dict, bytes | None]], zero: str, blobs: Mapping[str, str]) -> bytes:
    deletes = [
        f"0 {zero}\t{change['path']}".encode() + b"\x00"
        for change, _ in decoded if change["op"] == "delete"
    ]
    updates = [
        f"{change['mode']} {blobs[change['path']]}\t{change['path']}".encode() + b"\x00"
        for change, _ in decoded if change["op"] != "delete"
    ]
    return b"".join(deletes + updates)


def _write_blobs(
    root: Path, decoded: list[tuple[dict, bytes | None]], object_format: str
) -> dict[str, str]:
    blobs = {}
    for change, content in decoded:
        if change["op"] == "delete":
            continue
        blob = _output(_git(root, "hash-object", "-w", "--no-filters", "--stdin", stdin=content or b""))
        if blob != workspace._git_blob_id(content or b"", object_format):
            raise IntegrationRefused("write_commit_failed")
        blobs[change["path"]] = blob
    return blobs


def _commit_tree(
    tree: Path,
    private: Path,
    patch: Mapping[str, Any],
    decoded: list[tuple[dict, bytes | None]],
    object_format: str,
    *,
    identity: tuple[str, str],
    message: str,
) -> tuple[str, str, dict[str, str]]:
    """Commit the patched tree from the patch's own bytes; return `(commit, tree, blobs)`.

    Blobs are written with `hash-object --no-filters` and staged under a
    private `GIT_INDEX_FILE` read from the source tree, with
    `update-index --index-info` (deletes first); `write-tree` and
    `commit-tree --no-gpg-sign` finish it. The worktree's own index and
    detached HEAD then move to the new commit and `verify_tree_at_commit`
    proves the checked files, the index and the commit agree.
    """

    source = patch["source_commit"]
    index = {"GIT_INDEX_FILE": str(private / "commit-index")}
    if _git(tree, "read-tree", source, extra_env=index).returncode != 0:
        raise IntegrationRefused("write_commit_failed")
    blobs = _write_blobs(tree, decoded, object_format)
    staged = _git(
        tree, "update-index", "-z", "--index-info",
        stdin=_index_lines(decoded, "0" * len(source), blobs), extra_env=index,
    )
    if staged.returncode != 0:
        raise IntegrationRefused("write_commit_failed")
    tree_id = _output(_git(tree, "write-tree", extra_env=index))
    name, email = identity
    people = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    commit = _output(_git(
        tree, "-c", "commit.gpgSign=false", "commit-tree", "--no-gpg-sign",
        tree_id, "-p", source, "-F", "-",
        stdin=message.encode("utf-8"), extra_env=people,
    ))
    if not workspace._hex(commit, (len(source),)):
        raise IntegrationRefused("write_commit_failed")
    if (
        _git(tree, "read-tree", commit).returncode != 0
        or _git(tree, "update-ref", "--no-deref", "HEAD", commit, source).returncode != 0
    ):
        raise IntegrationRefused("write_commit_failed")
    try:
        workspace.verify_tree_at_commit(tree, commit)
    except WorkspaceRefused as exc:
        raise IntegrationRefused("write_tested_tree_mismatch") from exc
    if _output(_git(tree, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")) != tree_id:
        raise IntegrationRefused("write_tested_tree_mismatch")
    return commit, tree_id, blobs


# --- promotion -------------------------------------------------------------------


def _git_directories(root: Path) -> tuple[Path, Path]:
    """The primary's per-worktree Git directory (HEAD, index) and common directory (refs)."""

    storage = _git(root, "config", "--get", "extensions.refStorage")
    if storage.returncode == 0 and storage.stdout.strip() not in {b"", b"files"}:
        raise IntegrationRefused("write_promotion_unsupported")
    gitdir = Path(_output(_git(root, "rev-parse", "--path-format=absolute", "--git-dir")))
    common = Path(_output(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")))
    return Path(os.path.realpath(gitdir)), Path(os.path.realpath(common))


class _Locks:
    """Git's own lock files for HEAD, the branch ref and the index, created exclusively."""

    def __init__(self, gitdir: Path, common: Path, branch: str) -> None:
        if _BRANCH_REF.fullmatch(branch) is None or any(
            part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/")
        ):
            raise IntegrationRefused("write_base_moved")
        self.head = gitdir / "HEAD.lock"
        self.ref = common / f"{branch}.lock"
        self.ref_target = common / branch
        self.index = gitdir / "index.lock"
        self.index_target = gitdir / "index"
        self.held: dict[Path, int] = {}

    def acquire(self) -> None:
        try:
            for path in (self.head, self.ref, self.index):
                if path == self.ref:
                    os.makedirs(path.parent, mode=0o755, exist_ok=True)
                try:
                    self.held[path] = os.open(path, _LOCK_FLAGS, 0o644)
                except FileExistsError as exc:
                    raise IntegrationRefused("write_promotion_busy") from exc
        except BaseException:
            self.release()
            raise

    def write(self, path: Path, content: bytes) -> None:
        descriptor = self.held.pop(path)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.held[path] = -1

    def commit(self, path: Path, target: Path) -> None:
        """Rename a written lock over its target, as Git commits a lock."""

        os.rename(path, target)
        self.held.pop(path, None)

    def release(self) -> None:
        """Close and remove every lock still held; never raises."""

        for path, descriptor in list(self.held.items()):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            with contextlib.suppress(OSError):
                os.unlink(path)
            self.held.pop(path, None)


def _read_regular(root: Path, path: str) -> tuple[bytes, int] | None:
    """A tracked file's raw bytes and permission bits, or None when absent."""

    parts = path.split("/")
    root_fd = os.open(root, workspace._DIRECTORY_FLAGS)
    try:
        try:
            parent = workspace._open_parents(root_fd, parts[:-1], create=False)
        except (FileNotFoundError, WorkspaceRefused):
            return None
        try:
            try:
                descriptor = os.open(parts[-1], workspace._READ_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise IntegrationRefused("write_promotion_race")
                content = workspace._read_bounded(
                    descriptor, 64 * 1024 * 1024, "write_file_too_large"
                )
                return content, stat.S_IMODE(status.st_mode)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
    finally:
        os.close(root_fd)


def _restore(root: Path, originals: Mapping[str, tuple[bytes, int] | None]) -> None:
    """Put every touched path back to its recorded original, following no symlinks."""

    root_fd = os.open(root, workspace._DIRECTORY_FLAGS)
    try:
        for path, original in sorted(originals.items(), reverse=True):
            parts = path.split("/")
            if original is None:
                try:
                    parent = workspace._open_parents(root_fd, parts[:-1], create=False)
                except (FileNotFoundError, WorkspaceRefused):
                    continue
                try:
                    with contextlib.suppress(FileNotFoundError):
                        if stat.S_ISDIR(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
                            continue
                        os.unlink(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
                workspace._prune_empty(root_fd, parts[:-1])
        for path, original in sorted(originals.items()):
            if original is None:
                continue
            content, permissions = original
            parts = path.split("/")
            parent = workspace._open_parents(root_fd, parts[:-1], create=True)
            try:
                with contextlib.suppress(FileNotFoundError):
                    if stat.S_ISDIR(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
                        shutil.rmtree(os.path.join(root, *parts))
                temporary = f".cca-{secrets.token_hex(8)}.tmp"
                descriptor = os.open(temporary, workspace._CREATE_FLAGS, 0o600, dir_fd=parent)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    os.fchmod(handle.fileno(), permissions)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                os.close(parent)
    finally:
        os.close(root_fd)


def _write_ref_lock(locks: _Locks, commit: str) -> None:
    locks.write(locks.ref, f"{commit}\n".encode("ascii"))


def _commit_ref(locks: _Locks) -> None:
    locks.commit(locks.ref, locks.ref_target)


def _append_reflogs(
    gitdir: Path, common: Path, branch: str, old: str, new: str, identity: tuple[str, str]
) -> None:
    """Append Git reflog lines for the branch and HEAD when their logs exist; best effort."""

    name, email = identity
    line = (
        f"{old} {new} {name} <{email}> {int(time.time())} {time.strftime('%z')}\t"
        "integrate: fast-forward to a verified write patch\n"
    ).encode("utf-8")
    for log in (common / "logs" / branch, gitdir / "logs" / "HEAD"):
        with contextlib.suppress(OSError):
            descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | workspace._O_CLOEXEC)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)


def _promote(
    ledger: Any,
    project_id: str,
    root: Path,
    target: Mapping[str, Any],
    patch: Mapping[str, Any],
    decoded: list[tuple[dict, bytes | None]],
    blobs: Mapping[str, str],
    object_format: str,
    facts: dict[str, Any],
    identity: tuple[str, str],
) -> None:
    """Fast-forward the primary by hand, under Git's own locks; restore it on any failure.

    `write_promotion_busy`: a lock already exists. `write_promotion_race`:
    the re-verification under the locks fails. Both leave the primary
    untouched. After the `promoting` intent is recorded, a failure before
    the ref rename restores the working tree and the old index and is
    `write_promotion_failed`, or `write_integration_needs_owner` if the
    restoration itself failed.
    """

    source = patch["source_commit"]
    commit = facts["new_commit"]
    branch = facts["branch"]
    gitdir, common = _git_directories(root)
    locks = _Locks(gitdir, common, branch)
    locks.acquire()
    originals: dict[str, tuple[bytes, int] | None] | None = None
    old_index: bytes | None = None
    index_renamed = False
    try:
        try:
            check_primary(root, patch, object_format, branch=branch)
            branch_head = _git(root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
            if branch_head.stdout.decode("ascii", "replace").strip() != source:
                raise IntegrationRefused("write_base_moved")
        except IntegrationRefused as exc:
            raise IntegrationRefused("write_promotion_race") from exc
        try:
            with open(locks.index_target, "rb") as handle:
                old_index = handle.read()
        except OSError as exc:
            raise IntegrationRefused("write_promotion_race") from exc
        originals = {
            change["path"]: (None if change["op"] == "add" else _read_regular(root, change["path"]))
            for change, _ in decoded
        }
        facts["intent"] = ledger.record_write_integration(
            project_id, base=target["write_result"], outcome="promoting", reason=None,
            **_record_facts(facts),
        )
        try:
            locks.write(locks.index, old_index)
            staged = _git(
                root, "-c", "core.splitIndex=false", "update-index", "-z", "--index-info",
                stdin=_index_lines(decoded, "0" * len(source), blobs),
                extra_env={"GIT_INDEX_FILE": str(locks.index)},
            )
            if staged.returncode != 0:
                raise IntegrationRefused("write_promotion_failed")
            workspace.apply_patch_to_tree(patch, root, base_verified_by_caller=True)
            _write_ref_lock(locks, commit)
            locks.commit(locks.index, locks.index_target)
            index_renamed = True
            _commit_ref(locks)
        except BaseException as exc:
            restored = True
            try:
                if index_renamed:
                    _restore_index(locks, old_index)
                _restore(root, originals)
            except BaseException:  # noqa: BLE001 - the primary may be left changed
                restored = False
            if isinstance(exc, Exception):
                raise IntegrationRefused(
                    "write_promotion_failed" if restored else "write_integration_needs_owner"
                ) from exc
            raise
    finally:
        locks.release()
    _append_reflogs(gitdir, common, branch, source, commit, identity)


def _restore_index(locks: _Locks, old_index: bytes) -> None:
    descriptor = os.open(locks.index, _LOCK_FLAGS, 0o644)
    locks.held[locks.index] = descriptor
    locks.write(locks.index, old_index)
    locks.commit(locks.index, locks.index_target)


def _record_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: facts[key]
        for key in ("new_commit", "tested_tree_digest", "checks_receipt_digest", "branch")
    }


# --- reconciliation -----------------------------------------------------------------


def reconcile_integrations(ledger: Any, project_id: str, root: Path) -> list[dict[str, Any]]:
    """Finish every `promoting` intent left by an interrupted integration.

    The branch at the candidate commit means the promotion happened
    (`integrated`). At the source commit with a clean index and tracked
    files, it did not (`refused`, `write_promotion_failed`). Anything else is
    `write_integration_needs_owner`, which blocks re-integration of that run.
    """

    return [
        _reconcile_intent(ledger, project_id, root, intent)
        for intent in ledger.unfinished_write_integrations(project_id)
    ]


def _reconcile_intent(
    ledger: Any, project_id: str, root: Path, intent: Mapping[str, Any]
) -> dict[str, Any]:
    with deferred_signals():
        reason = "write_integration_needs_owner"
        try:
            resolved = workspace._repository_root(root)
            object_format = workspace._object_format(resolved)
            head = _git(resolved, "rev-parse", "--verify", "--quiet", f"{intent['branch']}^{{commit}}")
            at = head.stdout.decode("ascii", "replace").strip() if head.returncode == 0 else ""
            if at == intent["new_commit"]:
                reason = None
            elif at == intent["source_commit"]:
                staged = _git(
                    resolved, "diff-index", "--cached", "--quiet", "--no-renames",
                    intent["source_commit"], "--",
                )
                if staged.returncode == 0 and _tracked_files_clean(
                    resolved, intent["source_commit"], object_format
                ):
                    reason = "write_promotion_failed"
        except (IntegrationRefused, WorkspaceRefused, BridgeRefused, OSError):
            reason = "write_integration_needs_owner"
        return ledger.record_write_integration(
            project_id, base=intent,
            outcome="integrated" if reason is None else "refused", reason=reason,
            new_commit=intent["new_commit"], tested_tree_digest=intent["tested_tree_digest"],
            checks_receipt_digest=intent["checks_receipt_digest"], branch=intent["branch"],
        )


def commit_message(target: Mapping[str, Any], patch_digest: str) -> str:
    record = target["write_result"]
    verifiers = ", ".join(target["verifier_providers"]) or "none"
    return (
        f"Integrate verified write patch for task {target['assignment_id']}\n\n"
        f"Task: {target['assignment_id']} ({target['task_family']})\n"
        f"Worker provider: {target['worker_provider']}\n"
        f"Verifier provider: {verifiers}\n"
        f"Patch digest: {patch_digest}\n"
        f"Bridge run: {record['bridge_run_id']}\n"
    )


# --- signals -------------------------------------------------------------------


@contextlib.contextmanager
def deferred_signals() -> Iterator[None]:
    """Hold termination signals until the block ends, then deliver the first one.

    Promotion and its record must not be cut short. The fast-forward also
    runs in its own session, so a terminal's signal never reaches it. Off the
    main thread handlers cannot be installed and nothing is deferred.
    """

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    received: list[int] = []

    def hold(signum: int, _frame: Any) -> None:
        received.append(signum)

    previous = {signum: signal.signal(signum, hold) for signum in _DEFERRED_SIGNALS}
    try:
        yield
    finally:
        for signum, value in previous.items():
            signal.signal(signum, value)
        if received:
            signal.raise_signal(received[0])


# --- the operation -------------------------------------------------------------


def _checks_passed(report: Any) -> bool:
    if not isinstance(report, dict):
        return False
    deterministic = report.get("deterministic_checks")
    security = report.get("security_checks", [])
    return (
        isinstance(deterministic, list) and bool(deterministic)
        and isinstance(security, list)
        and all(isinstance(item, dict) and item.get("passed") is True
                for item in (*deterministic, *security))
    )


def _default_checks(root: Path) -> dict[str, Any]:
    from .learning_engine import run_operational_check_suite

    return run_operational_check_suite(root)


def integrate_write(
    ledger: Any,
    project_id: str,
    *,
    project_root: Path | str,
    bridge_run_id: str,
    host_client: str,
    run_checks: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Integrate one verified write patch; return the `write_integration` record and report.

    Everything runs under the ledger writer lock, after any unfinished
    promotion has been reconciled. A refusal is recorded with its reason
    and returned, not raised; an unrecorded write result or a host that
    does not own its run raises before anything is recorded.
    """

    checks = run_checks or _default_checks
    facts: dict[str, Any] = {
        "new_commit": None, "tested_tree_digest": None, "checks_receipt_digest": None,
        "branch": None, "intent": None,
    }
    with ledger.writer_lock():
        reconcile_integrations(ledger, project_id, Path(project_root))
        target = ledger.write_integration_target(
            project_id, bridge_run_id, host_client=host_client
        )
        reason = target["refusal"]
        check_report = None
        with contextlib.ExitStack() as critical:
            if reason is None:
                try:
                    check_report = _integrate_locked(
                        ledger, project_id, Path(project_root), target, facts, checks, critical
                    )
                except IntegrationRefused as exc:
                    reason = exc.reason
                except WorkspaceRefused as exc:
                    reason = exc.reason if exc.reason.startswith("write_") else f"write_{exc.reason}"
                except BridgeRefused:
                    reason = "write_git_failed"
                except BaseException as exc:
                    # Cancelled or failed unexpectedly: the worktree is already
                    # gone. Record why, holding signals, then let it continue.
                    with deferred_signals():
                        if facts["intent"] is not None:
                            # The primary may have moved: decide from its branch.
                            with contextlib.suppress(Exception):
                                _reconcile_intent(ledger, project_id, Path(project_root), facts["intent"])
                        else:
                            ledger.record_write_integration(
                                project_id, base=target["write_result"], outcome="refused",
                                reason="write_integration_interrupted"
                                if not isinstance(exc, Exception) else "write_integration_failed",
                                **_record_facts(facts),
                            )
                    raise
            integration = ledger.record_write_integration(
                project_id, base=target["write_result"],
                outcome="refused" if reason is not None else "integrated", reason=reason,
                **_record_facts(facts),
            )
    return {"write_integration": integration, "check_report": check_report}


def _integrate_locked(
    ledger: Any,
    project_id: str,
    root: Path,
    target: Mapping[str, Any],
    facts: dict[str, Any],
    checks: Callable[[Path], dict[str, Any]],
    critical: contextlib.ExitStack,
) -> dict[str, Any]:
    """Steps 2 to 6 and 8; the worktree is removed before this returns or raises."""

    record = target["write_result"]
    root = workspace._repository_root(root)
    object_format = workspace._object_format(root)
    # The patch bytes come only through the verified artifact read.
    try:
        patch_bytes = ledger.write_result_artifact(project_id, record["bridge_run_id"], "patch")
    except Exception as exc:  # noqa: BLE001 - any failure to read is one refusal
        raise IntegrationRefused("write_patch_unavailable") from exc
    if patch_bytes is None:
        raise IntegrationRefused("write_patch_unavailable")
    try:
        patch = json.loads(patch_bytes)
        decoded = workspace.validate_patch(patch)
    except (ValueError, WorkspaceRefused) as exc:
        raise IntegrationRefused("write_patch_invalid") from exc
    if (
        workspace.encode_patch(patch) != patch_bytes
        or workspace.patch_digest(patch) != record["patch_digest"]
        or patch["source_commit"] != record["source_commit"]
        or len(patch["source_commit"]) != (64 if object_format == "sha256" else 40)
    ):
        raise IntegrationRefused("write_patch_invalid")
    branch = check_primary(root, patch, object_format)
    identity = author_identity(root)

    private = _private_parent(root)
    try:
        tree = _prepare_worktree(root, private, patch["source_commit"], object_format)
        verified = workspace.verify_tree_at_commit(tree, patch["source_commit"])
        workspace.apply_patch_to_tree(patch, tree, verified=verified)

        try:
            report = checks(tree)
        except (ContractError, OSError, BridgeRefused, WorkspaceRefused) as exc:
            raise IntegrationRefused("write_checks_failed") from exc
        if isinstance(report, dict):
            facts["checks_receipt_digest"] = content_digest(report)
        if not _checks_passed(report):
            raise IntegrationRefused("write_checks_failed")

        commit, tree_id, blobs = _commit_tree(
            tree, private, patch, decoded, object_format, identity=identity,
            message=commit_message(target, record["patch_digest"]),
        )
        facts.update(new_commit=commit, tested_tree_digest=tree_id, branch=branch)

        # Promotion and its record are not interrupted by a termination signal.
        critical.enter_context(deferred_signals())
        _promote(
            ledger, project_id, root, target, patch, decoded, blobs, object_format, facts,
            identity,
        )
        return report
    finally:
        remove_worktree(root, private)
