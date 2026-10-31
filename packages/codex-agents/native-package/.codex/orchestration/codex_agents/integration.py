"""Integrate a verified write patch into the primary checkout (G3).

One serialized operation under the ledger writer lock:

1. preconditions: a passed write result, an `accepted` other-provider
   verification, not yet integrated, and the calling host owns the run;
2. the primary checkout is on a named branch at the patch's source commit,
   with a clean index, every tracked file equal to that commit, and nothing
   at any path the patch adds;
3. a detached disposable worktree at the source commit, in a private
   directory under the system temp directory, is verified and the patch is
   applied to it;
4. the project's operational checks run there, through the sandboxed runner;
5. the patched tree is committed there, with the primary's author identity;
6. under a re-check of step 2 the primary is fast-forwarded to that commit;
7. a `write_integration` record is written, for refusals too;
8. the worktree is removed on every terminal state.

The primary checkout is written only by step 6, after every check passed.
Every Git call uses the contained workspace's hardening: no hooks, no
filesystem monitor, no lazy fetch, no replacement objects and literal
pathspecs. Refusals carry stable reason codes.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
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
MERGE_TIMEOUT_SECONDS = 600
_DEFERRED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


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
    """Remove the disposable worktree and its registration; never raises."""

    tree = private / "tree"
    with contextlib.suppress(IntegrationRefused):
        _git(root, "worktree", "remove", "--force", "--force", str(tree))
    shutil.rmtree(private, ignore_errors=True)
    with contextlib.suppress(IntegrationRefused):
        _git(root, "worktree", "prune")


def _commit_tree(
    tree: Path,
    patch: Mapping[str, Any],
    decoded: list[tuple[dict, bytes | None]],
    object_format: str,
    *,
    identity: tuple[str, str],
    message: str,
) -> tuple[str, str]:
    """Commit the patched tree in the worktree from the patch's own bytes; return `(commit, tree)`.

    Blobs are written with `hash-object --no-filters` and staged with
    `update-index --index-info` (deletes first), so no clean filter runs and
    the committed content is exactly the patch's. The worktree's detached
    HEAD then moves to the new commit and `verify_tree_at_commit` proves the
    checked files, the index and the commit agree.
    """

    zero = "0" * len(patch["source_commit"])
    deletes = []
    updates = []
    for change, content in decoded:
        if change["op"] == "delete":
            deletes.append(f"0 {zero}\t{change['path']}".encode() + b"\x00")
            continue
        blob = _output(_git(tree, "hash-object", "-w", "--no-filters", "--stdin", stdin=content or b""))
        if blob != workspace._git_blob_id(content or b"", object_format):
            raise IntegrationRefused("write_commit_failed")
        updates.append(f"{change['mode']} {blob}\t{change['path']}".encode() + b"\x00")
    staged = _git(tree, "update-index", "-z", "--index-info", stdin=b"".join(deletes + updates))
    if staged.returncode != 0:
        raise IntegrationRefused("write_commit_failed")
    tree_id = _output(_git(tree, "write-tree"))
    name, email = identity
    people = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    commit = _output(_git(
        tree, "-c", "commit.gpgSign=false", "commit-tree", "--no-gpg-sign",
        tree_id, "-p", patch["source_commit"], "-F", "-",
        stdin=message.encode("utf-8"), extra_env=people,
    ))
    if not workspace._hex(commit, (len(patch["source_commit"]),)):
        raise IntegrationRefused("write_commit_failed")
    moved = _git(tree, "update-ref", "--no-deref", "HEAD", commit, patch["source_commit"])
    if moved.returncode != 0:
        raise IntegrationRefused("write_commit_failed")
    try:
        workspace.verify_tree_at_commit(tree, commit)
    except WorkspaceRefused as exc:
        raise IntegrationRefused("write_tested_tree_mismatch") from exc
    if _output(_git(tree, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")) != tree_id:
        raise IntegrationRefused("write_tested_tree_mismatch")
    return commit, tree_id


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

    Everything runs under the ledger writer lock. A refusal is recorded with
    its reason and returned, not raised; an unrecorded write result or a
    host that does not own its run raises before anything is recorded.
    """

    checks = run_checks or _default_checks
    facts: dict[str, Any] = {
        "new_commit": None, "tested_tree_digest": None, "checks_receipt_digest": None,
    }
    with ledger.writer_lock():
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
                    # gone; record why, then let the exception continue.
                    interrupted = not isinstance(exc, Exception)
                    ledger.record_write_integration(
                        project_id, write_result=target["write_result"],
                        reason="write_integration_interrupted" if interrupted
                        else "write_integration_failed",
                        **facts,
                    )
                    raise
            integration = ledger.record_write_integration(
                project_id, write_result=target["write_result"], reason=reason, **facts,
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
        tree = private / "tree"
        added = _git(root, "worktree", "add", "--detach", "--quiet", str(tree), patch["source_commit"])
        if added.returncode != 0:
            raise IntegrationRefused("write_worktree_failed")
        tree = Path(os.path.realpath(tree))
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

        commit, tree_id = _commit_tree(
            tree, patch, decoded, object_format, identity=identity,
            message=commit_message(target, record["patch_digest"]),
        )
        facts["new_commit"] = commit
        facts["tested_tree_digest"] = tree_id

        # Promotion and its record are not interrupted by a termination signal.
        critical.enter_context(deferred_signals())
        try:
            check_primary(root, patch, object_format, branch=branch)
        except IntegrationRefused as exc:
            raise IntegrationRefused("write_promotion_race") from exc
        try:
            merged = _git(
                root,
                "-c", "merge.verifySignatures=false", "-c", "submodule.recurse=false",
                "merge", "--ff-only", "--no-autostash", "--no-verify-signatures", "--no-stat",
                "--quiet", commit,
                timeout=MERGE_TIMEOUT_SECONDS, new_session=True,
            ).returncode
        except IntegrationRefused:
            # Not started, or killed at the timeout: inspect what it left.
            merged = None
        if merged != 0:
            try:
                check_primary(root, patch, object_format, branch=branch)
            except IntegrationRefused as exc:
                raise IntegrationRefused("write_promotion_incomplete") from exc
            raise IntegrationRefused("write_promotion_failed")
        _confirm_promoted(root, branch, commit, object_format)
        return report
    finally:
        remove_worktree(root, private)


def _confirm_promoted(root: Path, branch: str, commit: str, object_format: str) -> None:
    """The primary's branch, index and tracked files are exactly the new commit."""

    try:
        symbolic = _git(root, "symbolic-ref", "-q", "HEAD")
        head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        staged = _git(root, "diff-index", "--cached", "--quiet", "--no-renames", commit, "--")
        promoted = (
            symbolic.returncode == 0
            and symbolic.stdout.decode("utf-8", "replace").strip() == branch
            and head.returncode == 0
            and head.stdout.decode("ascii", "replace").strip() == commit
            and staged.returncode == 0
            and _tracked_files_clean(root, commit, object_format)
        )
    except (OSError, IntegrationRefused):
        promoted = False
    if not promoted:
        raise IntegrationRefused("write_promotion_incomplete")
