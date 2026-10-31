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
entries install added, and first removes the Codex commands rule through the project's own
owner command. Run it isolated: ``python3 -I -B -S scripts/install.py``.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX_INSTALLER = ROOT / "packages" / "codex-agents" / "scripts" / "install.py"
CLAUDE_INSTALLER = ROOT / "packages" / "claude-agents" / "scripts" / "package.py"
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
MANAGED_PATHS = (".codex", ".agents", ".claude")
CODEX_MANIFEST = Path(".codex") / "orchestration" / "install-manifest.json"
CLAUDE_SETTINGS = ".claude/settings.json"
PERMISSIONS_RECORD = ".claude/agent-orchestration-permissions.json"
# Git on the project never runs repository hooks or a configured file-system monitor, so a
# hook or monitor script an AI agent planted in the working tree cannot run as the owner.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
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
PROTECTED_EDITS = ("/.codex/**", "/.agents/**", "/.claude/settings*.json",
                   "/" + PERMISSIONS_RECORD, "/.git/**")
CLI_PATTERN = "*.codex/orchestration/cli.py"
MAX_SETTINGS_BYTES = 1024 * 1024

_ATTEST_SPEC = importlib.util.spec_from_file_location("agent_orchestration_attest",
                                                      ROOT / "scripts" / "attest.py")
attest = importlib.util.module_from_spec(_ATTEST_SPEC)
_ATTEST_SPEC.loader.exec_module(attest)


class InstallRefused(RuntimeError):
    """The target is not in a state where packages may be changed."""


def _git(target: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(target), *GIT_SAFE, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallRefused(f"target is not a usable Git working tree: {target}")
    return result.stdout.strip()


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
    if mutating and _git(target, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all",
                         "--", *MANAGED_PATHS):
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

    rules: list[str] = []
    for name in owner_only_commands(orchestration):
        rules += [f"Bash({CLI_PATTERN} {name})", f"Bash({CLI_PATTERN} {name} *)"]
    rules += [f"Edit({path})" for path in PROTECTED_EDITS]
    return rules


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

    def write(self, relative: str, content: bytes) -> None:
        parent, name = self._parent(relative, create=True)
        temporary = f".{name}.{os.getpid()}.tmp"
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=parent)
            try:
                os.write(handle, content)
                os.fchmod(handle, 0o644)
            finally:
                os.close(handle)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
            raise
        finally:
            os.close(parent)

    def remove(self, relative: str) -> None:
        try:
            parent, name = self._parent(relative, create=False)
        except FileNotFoundError:
            return
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)


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


def _load_record(project: _Project) -> dict | None:
    raw = project.read(PERMISSIONS_RECORD)
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
        raise InstallRefused(f"{PERMISSIONS_RECORD} is unreadable; restore it from Git before continuing")
    return record


def _record_bytes(record: dict) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def plan_settings(target: Path, operation: str, *, orchestration: Path = ORCHESTRATION) -> dict:
    """What install or remove does to .claude/settings.json, with the bytes to write.

    Install adds every missing deny rule and records which ones it added; user entries are
    never dropped or reordered. Remove takes out only the recorded entries, then the
    permissions and deny keys and the file itself when install created them and they are
    now empty. Returns {"action", "changed_paths", "writes": {path: bytes | None}}.
    """

    project = _Project(target)
    try:
        data, raw = _load_settings(project)
        record = _load_record(project)
    finally:
        project.close()
    writes: dict[str, bytes | None] = {}
    if operation == "remove":
        if record is None:
            return {"action": "none", "changed_paths": [], "writes": writes, "removed": []}
        ours = set(record["deny"])
        removed: list[str] = []
        if data is not None:
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
        writes[PERMISSIONS_RECORD] = None
        return {"action": "remove", "changed_paths": sorted(writes), "writes": writes, "removed": removed}
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
        "created_settings_file": record["created_settings_file"] if record else data is None,
        "created_permissions": (record["created_permissions"] if record else False)
        or data is None or "permissions" not in data,
        "created_deny": (record["created_deny"] if record else False)
        or data is None or "deny" not in (data.get("permissions") or {}),
        "deny": [rule for rule in desired if rule in previous or rule in added],
    }
    if data is None or updated != data:
        writes[CLAUDE_SETTINGS] = _dump(updated)
    if record is None or _record_bytes(new_record) != _record_bytes(record):
        writes[PERMISSIONS_RECORD] = _record_bytes(new_record)
    action = "unchanged" if not writes else ("create" if data is None else "merge")
    return {"action": action, "changed_paths": sorted(writes), "writes": writes, "added": added}


def apply_settings(target: Path, plan: dict) -> None:
    project = _Project(target)
    try:
        for relative, content in sorted(plan["writes"].items()):
            if content is None:
                project.remove(relative)
            else:
                project.write(relative, content)
    finally:
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
    return {key: value for key, value in plan.items() if key != "writes"}


# --- the Codex commands rule -------------------------------------------------------------

RULE_COMMAND = "remove-codex-rule"


def _project_cli(target: Path, args: list[str]) -> dict:
    """Run the project's installed CLI isolated, after checking it is this checkout's copy."""

    try:
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


def _in_temporary_folder(target: Path) -> bool:
    real = Path(os.path.realpath(target))
    return any(attest.within(real, root) for root in attest.temp_roots())


def plan_codex_rule(target: Path) -> dict:
    """How remove takes out the Codex commands rule and its protected runtime copy.

    The project's own owner command does it. A project with no Codex install, or one in a
    temporary folder (which never gets a rule), has nothing to remove. An installed CLI too
    old to have the command is asked, read-only, whether a rule exists; if one does, remove
    fails closed and names the file to delete.
    """

    if not (target / CODEX_MANIFEST).is_file():
        return {"action": "none", "reason": "the Codex package is not installed"}
    if _in_temporary_folder(target):
        return {"action": "none", "reason": "a project in a temporary folder never gets a rule"}
    try:
        attest.check_project_runtime(target)
    except attest.AttestError as exc:
        return {"error": f"the Codex commands rule cannot be removed: {exc}"}
    if RULE_COMMAND in _parser_commands(ORCHESTRATION / "cli.py"):
        return {"action": RULE_COMMAND}
    status = _project_cli(target, ["host-command", "--project-root", str(target)])
    if status.get("codex_rule") == "missing":
        return {"action": "none", "reason": "no Codex commands rule exists for this project"}
    rule_file = status.get("rule_file")
    shown = re.sub(r"[\x00-\x1f\x7f]", "", rule_file) if isinstance(rule_file, str) else None
    where = f"delete {shown}" if shown else (
        "delete the agent-orchestration-*.rules file for this project in your Codex rules folder")
    return {"error": f"this installed orchestrator cannot remove its Codex commands rule; {where} "
                     "yourself, then run remove again"}


def apply_codex_rule(target: Path, plan: dict) -> dict:
    if plan.get("action") != RULE_COMMAND:
        return dict(plan)
    report = _project_cli(target, [RULE_COMMAND, "--project-root", str(target)])
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
        settings_plan = plan_settings(target, operation)
        summary["claude_settings"] = _settings_report(settings_plan)
    except (InstallRefused, OSError) as exc:
        settings_plan = None
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
            apply_settings(target, settings_plan)
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
        if summary["codex_rule"].get("action") == RULE_COMMAND:
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
