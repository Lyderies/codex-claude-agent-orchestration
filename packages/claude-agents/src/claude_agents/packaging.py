"""Native developer package compiler and conservative owner-invoked installer.

No live provider adapter, hooks, settings, launcher, credentials, or model route.
Use quiescent targets. The cooperative lock is not protection from hostile same-user
writers; any observed unknown mutation is preserved and blocks further operations.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .contracts import ROLES
from .validation import validate_native_inventory

MANIFEST = ".claude/claude-agents/install-manifest.json"
PENDING = ".claude/claude-agents/install-pending.json"
LOCK = ".claude/claude-agents/install.lock"
_MODULES = ("__init__.py", "contracts.py", "validation.py")
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LIMIT = 262144
_CONTRACTS = {
    "researcher": "Cite current primary evidence and preserve uncertainty.",
    "planner": "Trace each step to accepted requirements and observable exits.",
    "implementation_worker": "Return one bounded patch for the primary to apply.",
    "security_reviewer": "Report concrete boundary defects with evidence and severity.",
    "verification_worker": "Independently test assigned criteria without fixing failures.",
}


class PackageError(ValueError):
    """Content-free package failure code."""


def _fail(code: str) -> None:
    raise PackageError(code)


def _root(path: Path) -> int:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        _fail("invalid_root")
    descriptor = os.open("/", _FLAGS)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _parent(root: int, relative: str, create: bool = False) -> tuple[int, str]:
    parts = relative.split("/")
    if any(p in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", p)
           for p in parts):
        _fail("unsafe_path")
    descriptor = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _read(root: int, relative: str) -> bytes | None:
    try:
        parent, name = _parent(root, relative)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
                _fail("unsafe_file")
            result = b""
            while len(result) <= _LIMIT:
                chunk = os.read(descriptor, min(65536, _LIMIT + 1 - len(result)))
                if not chunk:
                    return result
                result += chunk
            _fail("oversized_file")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


_GIT_HEAD = re.compile(rb"(?:ref: refs/heads/[A-Za-z0-9_./-]+|[0-9a-f]{40}|[0-9a-f]{64})\n")
_GIT_LINK = re.compile(rb"gitdir: ([^\0\r\n]{1,4096})\n?")


def _linked_git_dir_exists(target: Path, pointer: bytes) -> bool:
    """Check, without opening it, that a linked worktree points at a Git directory with a HEAD."""
    git_dir = Path(os.fsdecode(pointer))
    if not git_dir.is_absolute():
        git_dir = target / git_dir
    try:
        return (stat.S_ISDIR(os.lstat(git_dir).st_mode)
                and stat.S_ISREG(os.lstat(git_dir / "HEAD").st_mode))
    except OSError:
        return False


def _is_git_target(root: int, target: Path) -> bool:
    """Accept a Git directory or a linked-worktree marker file; never follow either."""
    try:
        info = os.stat(".git", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode):
        head = _read(root, ".git/HEAD")
        return head is not None and _GIT_HEAD.fullmatch(head) is not None
    if stat.S_ISREG(info.st_mode):
        marker = _read(root, ".git")
        match = _GIT_LINK.fullmatch(marker) if marker is not None else None
        return match is not None and _linked_git_dir_exists(target, match.group(1))
    return False


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _fail("write_failed")
        view = view[written:]
    os.fsync(descriptor)


def _exclusive(root: int, relative: str, content: bytes) -> None:
    parent, name = _parent(root, relative, True)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        try:
            _write_all(descriptor, content)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    finally:
        os.close(parent)


def _set_file(root: int, relative: str, before: bytes | None,
              after: bytes | None) -> None:
    if _read(root, relative) != before:
        _fail("owned_file_changed")
    parent, name = _parent(root, relative, after is not None)
    temporary = ".claude-agents-" + secrets.token_hex(16)
    created = None
    try:
        if after is None:
            os.unlink(name, dir_fd=parent)
        else:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            try:
                created = os.fstat(descriptor)
                _write_all(descriptor, after)
            finally:
                os.close(descriptor)
            if _read(root, relative) != before:
                _fail("owned_file_changed")
            if before is None:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            else:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if created is not None:
            try:
                current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class Bundle:
    files: tuple[tuple[str, bytes], ...]
    manifest: bytes


def compile_bundle(source_root: Path) -> Bundle:
    """Compile fixed paths from an owner-selected trusted source; never execute it."""
    root = None
    try:
        root = _root(source_root)
        def source(relative: str) -> bytes:
            content = _read(root, relative)
            if content is None:
                _fail("source_missing")
            return content
        project = tomllib.loads(source("pyproject.toml").decode())["project"]
        if (project["name"] != "claude-agents" or type(project["version"]) is not str
                or not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", project["version"])):
            _fail("invalid_source_metadata")
        texts = [source("tests/fixtures/native/" + name + ".md").decode()
                 for name in sorted(ROLES)]
        fixtures = validate_native_inventory(texts)
        files = {}
        for fixture, text in zip(fixtures, texts, strict=True):
            role = fixture.name.removeprefix("claude_agents_")
            text = text.replace("\n---\n", "\nmaxTurns: 8\n---\n", 1)
            text = text.replace("Inert fixture only. Do not execute or install this definition.\n", "")
            text += (
                "\n" + _CONTRACTS[role] + "\n"
                "Use one assignment and one attempt. Do not retry or reroute.\n"
                "Return the assigned structured result with admission_state unverified.\n"
                "Keep unavailable usage null. Do not attest effect cessation or accept your result.\n"
                "Model omission allows native inheritance and provides no model isolation.\n"
                "Only the host/controller may select models or grant capabilities.\n"
            )
            files[".claude/agents/" + fixture.name + ".md"] = text.encode()
        files[".claude/skills/claude-agents/SKILL.md"] = (
            "---\nname: claude-agents\n"
            "description: Apply bounded research planning patch review and verification workflows\n"
            "---\n\n"
            "Use one bounded native role at a time with exact scope and evidence criteria.\n"
            "Keep provider/model choice, tools, secrets and admission in the host/controller.\n"
            "Obtain separate verification before accepting a meaningful result.\n"
            "The package grants no permissions and supplies no durable provider adapter.\n"
            "Model and tool inheritance remain native behavior, not package isolation.\n"
            "Do not enable hooks, MCP, usage credits, metered APIs or Fable.\n"
        ).encode()
        for module in _MODULES:
            content = source("src/claude_agents/" + module)
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module])
                    if any(name not in {"__future__", "json", "re", "dataclasses",
                                        "typing", "contracts"} for name in names):
                        _fail("non_offline_helper")
            files[".claude/claude-agents/lib/claude_agents/" + module] = content
        metadata = {
            "schema_version": 1, "namespace": "claude-agents",
            "package_version": project["version"], "provider_execution_enabled": False,
            "native_discovery_verified": False, "model_isolation": "not_provided",
            "files": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(files.items())},
        }
        metadata["bundle_sha256"] = hashlib.sha256(_canonical(metadata)).hexdigest()
        return Bundle(tuple(sorted(files.items())), _canonical(metadata))
    except (OSError, UnicodeError, KeyError, SyntaxError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("invalid_source") from None
    finally:
        if root is not None:
            os.close(root)


def _plan(root: int, bundle: Bundle, previous: Bundle | None, operation: str,
          locked: bool = False) -> list[tuple[str, bytes | None, bytes | None]]:
    if _read(root, PENDING) is not None or (not locked and _read(root, LOCK) is not None):
        _fail("installation_pending")
    manifest = _read(root, MANIFEST)
    old = None
    if manifest is not None:
        old = next((b for b in (bundle, previous) if b is not None
                    and b.manifest == manifest), None)
        if old is None:
            _fail("untrusted_manifest")
    elif operation in {"verify", "update"}:
        _fail("not_installed")
    old_files = dict(old.files) if old else {}
    new_files = dict(bundle.files)
    for path in sorted(set(old_files) | set(new_files)):
        content = _read(root, path)
        if content != old_files.get(path):
            _fail("managed_file_modified" if old else "unmanaged_conflict")
    if operation in {"install", "verify"} and old and old.manifest != bundle.manifest:
        _fail("update_required")
    if operation == "verify":
        return []
    desired = {} if operation == "remove" else new_files
    result = [(p, old_files.get(p), desired.get(p))
              for p in sorted(set(old_files) | set(new_files))
              if old_files.get(p) != desired.get(p)]
    after = None if operation == "remove" else bundle.manifest
    if manifest != after:
        result.append((MANIFEST, manifest, after))
    return result


def package_operation(source_root: Path, target: Path, operation: str, *,
                      apply: bool = False, previous_source_root: Path | None = None) -> dict:
    """Preview by default. Update/rollback requires exact trusted old bundle bytes."""
    if type(operation) is not str or operation not in {"install", "update", "verify", "remove"}:
        _fail("invalid_operation")
    if type(apply) is not bool:
        _fail("invalid_apply")
    bundle = compile_bundle(source_root)
    previous = compile_bundle(previous_source_root) if previous_source_root else None
    root = None
    token = secrets.token_hex(16).encode()
    lock_owned = False
    try:
        root = _root(target)
        if not _is_git_target(root, target):
            _fail("git_target_required")
        changes = _plan(root, bundle, previous, operation)
        report = {
            "operation": operation, "changed_paths": [p for p, _, _ in changes],
            "provider_execution_enabled": False, "native_discovery_verified": False,
            "result": "verified" if operation == "verify" else "preview",
        }
        if not apply or operation == "verify" or not changes:
            if apply and not changes and operation != "verify":
                report["result"] = "unchanged"
            return report
        _exclusive(root, LOCK, token)
        lock_owned = True
        changes = _plan(root, bundle, previous, operation, locked=True)
        report["changed_paths"] = [p for p, _, _ in changes]
        if not changes:
            report["result"] = "unchanged"
            return report
        marker = _canonical({
            "schema_version": 1, "operation": operation, "token": token.decode(),
            "changes": [{"path": p,
                         "before": None if b is None else hashlib.sha256(b).hexdigest(),
                         "after": None if a is None else hashlib.sha256(a).hexdigest()}
                        for p, b, a in changes],
        })
        _exclusive(root, PENDING, marker)
        attempted = []
        try:
            for change in changes:
                attempted.append(change)
                _set_file(root, *change)
            _set_file(root, PENDING, marker, None)
        except BaseException:
            incomplete = False
            for path, before, after in reversed(attempted):
                try:
                    current = _read(root, path)
                    if current == before:
                        continue
                    if current != after:
                        incomplete = True
                    else:
                        _set_file(root, path, after, before)
                except (OSError, PackageError):
                    incomplete = True
            if incomplete and _read(root, PENDING) is None:
                _exclusive(root, PENDING, marker)
            if not incomplete and _read(root, PENDING) == marker:
                _set_file(root, PENDING, marker, None)
            raise PackageError("rollback_pending" if incomplete else "operation_failed") from None
        report["result"] = "applied"
        return report
    except OSError:
        raise PackageError("unsafe_or_unavailable_path") from None
    finally:
        if root is not None:
            if lock_owned:
                try:
                    if _read(root, LOCK) == token:
                        _set_file(root, LOCK, token, None)
                except (OSError, PackageError):
                    pass
            os.close(root)
