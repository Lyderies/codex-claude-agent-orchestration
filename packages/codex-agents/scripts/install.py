#!/usr/bin/env python3
"""Install, verify, update, or remove the native Codex package."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from string import hexdigits


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = REPOSITORY_ROOT / "native-package"
MANIFEST_PATH = Path(".codex/orchestration/install-manifest.json")
PROJECT_AGENT_ROUTING_PATH = Path(
    ".codex/orchestration/project-agent-routing.json"
)
OPERATIONAL_CHECKS_PATH = Path(".codex/orchestration/operational-checks.json")
ORCHESTRATION_ROOT = BUNDLE_ROOT / ".codex" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from codex_agents.learning_engine import (  # noqa: E402 - source-tree bootstrap above
    validate_operational_check_configuration_input,
)
from codex_agents.routing import validate_project_agent_mapping  # noqa: E402
from codex_agents.validation import validate_package  # noqa: E402


class InstallError(RuntimeError):
    """Raised when an install action would overwrite or remove unknown content."""


class TargetRoot:
    """One validated target directory pinned by an open no-follow descriptor."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self.descriptor = descriptor

    def close(self) -> None:
        os.close(self.descriptor)


Target = Path | TargetRoot


# The only path under .claude this package owns: the Claude orchestration skill.
# Everything else under .claude belongs to the Claude package or the project.
CLAUDE_SKILL_PREFIX = (".claude", "skills", "agent-orchestration")


def _safe_relative(
    raw: str, *, allow_manifest: bool = False, allow_directory: bool = False
) -> Path:
    path = Path(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallError(f"unsafe manifest path: {raw!r}")
    claude_skill = (
        len(path.parts) > len(CLAUDE_SKILL_PREFIX)
        and path.parts[: len(CLAUDE_SKILL_PREFIX)] == CLAUDE_SKILL_PREFIX
    )
    # Removing emptied directories may also walk the prefix's own ancestors.
    claude_ancestor = allow_directory and path.parts == CLAUDE_SKILL_PREFIX[: len(path.parts)]
    if (
        path.parts[0] not in {".agents", ".codex"}
        and raw != "VERSION"
        and not claude_skill
        and not claude_ancestor
    ):
        raise InstallError(f"unsupported package path: {raw!r}")
    if path == MANIFEST_PATH and not allow_manifest:
        raise InstallError(f"unsupported package path: {raw!r}")
    return path


def _open_parent_fd(
    target: Target,
    relative: str,
    *,
    create: bool = False,
    allow_manifest: bool = False,
    allow_directory: bool = False,
) -> tuple[int, str]:
    path = _safe_relative(
        relative, allow_manifest=allow_manifest, allow_directory=allow_directory
    )
    descriptor = (
        os.dup(target.descriptor)
        if isinstance(target, TargetRoot)
        else os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    )
    try:
        for part in path.parent.parts:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise InstallError(
                    f"refusing symlinked install path or non-directory component: {relative}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _read_from_parent(parent_fd: int, name: str, relative: str) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"refusing symlinked install path: {relative}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InstallError(f"managed path is not a regular file: {relative}")
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _read_managed_bytes(
    target: Target, relative: str, *, allow_manifest: bool = False
) -> bytes | None:
    try:
        parent_fd, name = _open_parent_fd(
            target, relative, allow_manifest=allow_manifest
        )
    except FileNotFoundError:
        return None
    try:
        return _read_from_parent(parent_fd, name, relative)
    finally:
        os.close(parent_fd)


def _managed_digest(
    target: Target, relative: str, *, allow_manifest: bool = False
) -> str | None:
    content = _read_managed_bytes(target, relative, allow_manifest=allow_manifest)
    return None if content is None else hashlib.sha256(content).hexdigest()


def _atomic_write_managed(
    target: Target,
    relative: str,
    content: bytes,
    *,
    mode: int,
    allow_manifest: bool = False,
) -> None:
    parent_fd, name = _open_parent_fd(
        target,
        relative,
        create=True,
        allow_manifest=allow_manifest,
    )
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _unlink_managed(
    target: Target,
    relative: str,
    *,
    expected_digest: str | None = None,
    allow_manifest: bool = False,
) -> bool:
    try:
        parent_fd, name = _open_parent_fd(
            target, relative, allow_manifest=allow_manifest
        )
    except FileNotFoundError:
        return False
    try:
        if expected_digest is not None:
            content = _read_from_parent(parent_fd, name, relative)
            if content is None:
                return False
            current = hashlib.sha256(content).hexdigest()
            if current != expected_digest:
                raise InstallError(f"managed file changed before removal: {relative}")
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        except IsADirectoryError as exc:
            raise InstallError(f"managed path is not a regular file: {relative}") from exc
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _rmdir_managed(target: Target, relative: str) -> None:
    try:
        parent_fd, name = _open_parent_fd(target, relative, allow_directory=True)
    except (FileNotFoundError, InstallError):
        return
    try:
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.ENOTDIR}:
                raise
    finally:
        os.close(parent_fd)


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _bundle_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(BUNDLE_ROOT.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.name == ".DS_Store"
            or path == BUNDLE_ROOT / "VERSION"
        ):
            continue
        if path.is_symlink():
            raise InstallError(f"bundle contains symlink: {path.relative_to(BUNDLE_ROOT)}")
        relative = path.relative_to(BUNDLE_ROOT).as_posix()
        if Path(relative) == MANIFEST_PATH:
            continue
        files[relative] = path
    if not files:
        raise InstallError("native package contains no files")
    return files


def _load_manifest(target: Target) -> dict | None:
    raw = _read_managed_bytes(
        target, MANIFEST_PATH.as_posix(), allow_manifest=True
    )
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"invalid install manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or not isinstance(manifest.get("version"), str)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise InstallError("install manifest is missing its file map")
    for relative, digest in manifest["files"].items():
        if not isinstance(relative, str):
            raise InstallError("install manifest paths must be strings")
        _safe_relative(relative)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in hexdigits for character in digest)
        ):
            raise InstallError(f"invalid manifest digest: {relative}")
    return manifest


def _target_root(path: Path) -> TargetRoot:
    target = path.resolve()
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise InstallError(f"target is not a stable directory: {target}") from exc
    try:
        try:
            git_descriptor = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except OSError as exc:
            raise InstallError(f"target is not a Git working tree: {target}") from exc
        try:
            git_mode = os.fstat(git_descriptor).st_mode
            if not (stat.S_ISDIR(git_mode) or stat.S_ISREG(git_mode)):
                raise InstallError(f"target has unsupported .git entry: {target}")
        finally:
            os.close(git_descriptor)
        return TargetRoot(target, descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _plan_install(target: Target, update: bool) -> tuple[dict[str, Path], dict | None]:
    files = _bundle_files()
    manifest = _load_manifest(target)
    if update and manifest is None:
        raise InstallError("cannot update: package is not installed")
    if not update and manifest is not None:
        raise InstallError("package is already installed; use update")
    old_files = {} if manifest is None else manifest["files"]
    conflicts: list[str] = []
    for relative, source in files.items():
        current = _managed_digest(target, relative)
        if current is None:
            continue
        if relative in old_files:
            if current != old_files[relative]:
                conflicts.append(f"modified installed file: {relative}")
        elif current != _digest(source):
            conflicts.append(f"existing unmanaged file: {relative}")
    if update:
        for relative, old_digest in old_files.items():
            if relative in files:
                continue
            current = _managed_digest(target, relative)
            if current is not None and current != old_digest:
                conflicts.append(f"modified obsolete file: {relative}")
    if conflicts:
        raise InstallError("install conflicts:\n- " + "\n- ".join(sorted(conflicts)))
    return files, manifest


def _write_manifest(target: Target, files: dict[str, Path]) -> None:
    version = (BUNDLE_ROOT / "VERSION").read_text().strip()
    manifest = {
        "format": 1,
        "version": version,
        "files": {relative: _digest(source) for relative, source in sorted(files.items())},
    }
    content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write_managed(
        target,
        MANIFEST_PATH.as_posix(),
        content,
        mode=0o600,
        allow_manifest=True,
    )


def _read_explicit_configuration(path: Path, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"{label} input is not a regular file")
        content = path.read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read {label} input") from exc
    if len(content) > 65_536:
        raise InstallError(f"{label} input is too large")
    return content


def _project_configuration_plan(
    target: TargetRoot,
    *,
    project_agent_routing: Path | None,
    operational_checks: Path | None,
    replace_project_config: bool,
) -> tuple[dict[str, bytes], dict[str, str]]:
    inputs = {
        PROJECT_AGENT_ROUTING_PATH.as_posix(): (
            project_agent_routing,
            "project agent routing",
        ),
        OPERATIONAL_CHECKS_PATH.as_posix(): (
            operational_checks,
            "operational checks",
        ),
    }
    selected: dict[str, bytes] = {}
    actions: dict[str, str] = {}
    for relative, (source, label) in inputs.items():
        existing = _read_managed_bytes(target, relative)
        supplied = (
            _read_explicit_configuration(source, label) if source is not None else None
        )
        if existing is None and supplied is None:
            raise InstallError(
                f"required {label} configuration is missing; provide --{label.replace(' ', '-')}"
            )
        if existing is not None and supplied is None:
            selected[relative] = existing
            actions[relative] = "preserve"
            continue
        assert supplied is not None
        if existing is not None and existing != supplied and not replace_project_config:
            raise InstallError(
                f"existing project-owned configuration differs: {relative}; "
                "use --replace-project-config to replace it explicitly"
            )
        selected[relative] = supplied
        if existing == supplied:
            actions[relative] = "preserve"
        elif existing is None:
            actions[relative] = "create"
        else:
            actions[relative] = "replace"

    try:
        validate_project_agent_mapping(
            target.path,
            selected[PROJECT_AGENT_ROUTING_PATH.as_posix()],
            ORCHESTRATION_ROOT / "config" / "routing-policy.json",
            ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
            fallback_root=BUNDLE_ROOT,
        )
        validate_operational_check_configuration_input(
            target.path, selected[OPERATIONAL_CHECKS_PATH.as_posix()]
        )
    except (ValueError, OSError) as exc:
        raise InstallError(f"project configuration is invalid: {exc}") from exc
    return selected, actions


def install(
    target_path: Path,
    apply: bool,
    update: bool = False,
    *,
    project_agent_routing: Path | None = None,
    operational_checks: Path | None = None,
    replace_project_config: bool = False,
) -> dict:
    target = _target_root(target_path)
    try:
        project_configuration, configuration_actions = _project_configuration_plan(
            target,
            project_agent_routing=project_agent_routing,
            operational_checks=operational_checks,
            replace_project_config=replace_project_config,
        )
        files, manifest = _plan_install(target, update)
        action = "update" if update else "install"
        report = {
            "action": action,
            "apply": apply,
            "target": str(target.path),
            "files": sorted(files),
            "project_configuration": configuration_actions,
        }
        if not apply:
            return report
        for relative, source in files.items():
            source_mode = stat.S_IMODE(source.stat().st_mode)
            _atomic_write_managed(
                target,
                relative,
                source.read_bytes(),
                mode=source_mode,
            )
        if update and manifest is not None:
            for relative, old_digest in manifest["files"].items():
                if relative in files:
                    continue
                _unlink_managed(target, relative, expected_digest=old_digest)
        for relative, content in project_configuration.items():
            if configuration_actions[relative] in {"create", "replace"}:
                _atomic_write_managed(target, relative, content, mode=0o600)
        _write_manifest(target, files)
        verification = verify(target.path)
        report.update(verification)
        report["action"] = action
        report["apply"] = True
        report["project_configuration"] = configuration_actions
        return report
    finally:
        target.close()


def verify(target_path: Path) -> dict:
    target = _target_root(target_path)
    try:
        manifest = _load_manifest(target)
        if manifest is None:
            raise InstallError("package is not installed")
        missing: list[str] = []
        modified: list[str] = []
        for relative, expected in manifest["files"].items():
            current = _managed_digest(target, relative)
            if current is None:
                missing.append(relative)
            elif current != expected:
                modified.append(relative)
        unknown_package_files = sorted(set(_bundle_files()) - set(manifest["files"]))
        runtime = validate_package(target.path)
        manifest_errors = [
            *(f"missing managed file: {item}" for item in missing),
            *(f"modified managed file: {item}" for item in modified),
            *(
                f"package file absent from manifest: {item}"
                for item in unknown_package_files
            ),
        ]
        package_errors = [*manifest_errors, *runtime["package_errors"]]
        project_errors = list(runtime["project_errors"])
        package_ready = not package_errors
        project_ready = not project_errors
        report = {
            "ready": package_ready and project_ready,
            "package_ready": package_ready,
            "project_ready": project_ready,
            "target": str(target.path),
            "version": manifest.get("version"),
            "missing": missing,
            "modified": modified,
            "package_files_not_in_manifest": unknown_package_files,
            "errors": [*package_errors, *project_errors],
            "package_errors": package_errors,
            "project_errors": project_errors,
            "warnings": runtime["warnings"],
        }
        if any("not committed and clean" in error for error in project_errors):
            report["required_action"] = (
                "commit both project configuration files, their mapped agents, and "
                "any project-relative check executables, then rerun installer verify"
            )
        return report
    finally:
        target.close()


def remove(target_path: Path, apply: bool) -> dict:
    target = _target_root(target_path)
    try:
        manifest = _load_manifest(target)
        if manifest is None:
            raise InstallError("package is not installed")
        modified: list[str] = []
        removable: list[str] = []
        for relative, expected in manifest["files"].items():
            current = _managed_digest(target, relative)
            if current is None:
                continue
            if current == expected:
                removable.append(relative)
            else:
                modified.append(relative)
        if modified:
            raise InstallError(
                "refusing to remove modified installed files:\n- "
                + "\n- ".join(sorted(modified))
            )
        report = {
            "action": "remove",
            "apply": apply,
            "target": str(target.path),
            "files": sorted(removable),
        }
        if not apply:
            return report
        directories: set[Path] = set()
        for relative in removable:
            path = Path(relative)
            directory = path.parent
            while directory != Path("."):
                directories.add(directory)
                directory = directory.parent
            _unlink_managed(
                target, relative, expected_digest=manifest["files"][relative]
            )
        manifest_directory = MANIFEST_PATH.parent
        while manifest_directory != Path("."):
            directories.add(manifest_directory)
            manifest_directory = manifest_directory.parent
        _unlink_managed(
            target,
            MANIFEST_PATH.as_posix(),
            allow_manifest=True,
        )
        for directory in sorted(
            directories, key=lambda item: len(item.parts), reverse=True
        ):
            _rmdir_managed(target, directory.as_posix())
        return report
    finally:
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "update", "remove"):
        child = subparsers.add_parser(command)
        child.add_argument("--target", required=True, type=Path)
        child.add_argument("--apply", action="store_true", help="perform the displayed action")
        if command in {"install", "update"}:
            child.add_argument("--project-agent-routing", type=Path)
            child.add_argument("--operational-checks", type=Path)
            child.add_argument("--replace-project-config", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "install":
            report = install(
                args.target,
                args.apply,
                project_agent_routing=args.project_agent_routing,
                operational_checks=args.operational_checks,
                replace_project_config=args.replace_project_config,
            )
        elif args.command == "update":
            report = install(
                args.target,
                args.apply,
                update=True,
                project_agent_routing=args.project_agent_routing,
                operational_checks=args.operational_checks,
                replace_project_config=args.replace_project_config,
            )
        elif args.command == "remove":
            report = remove(args.target, args.apply)
        else:
            report = verify(args.target)
    except (InstallError, OSError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
