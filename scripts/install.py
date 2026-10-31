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
import importlib.util
import json
import os
import re
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
PERMISSIONS_RECORD = ".claude/agent-orchestration-permissions.json"
# Git on the project never runs repository hooks, a file-system monitor, a signing program
# or automatic maintenance. A Codex session opened on a parent folder can write the
# project's .git, so porcelain commands also run only while the repository's own settings
# name no program for Git to start (repository_helpers).
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false")
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
PROTECTED_EDITS = ("/.codex/**", "/.agents/**", "/.claude/settings*.json",
                   "/" + PERMISSIONS_RECORD, "/.git/**")
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
HISTORICAL_PROTECTED_EDITS = PROTECTED_EDITS

_ATTEST_SPEC = importlib.util.spec_from_file_location("agent_orchestration_attest",
                                                      ROOT / "scripts" / "attest.py")
attest = importlib.util.module_from_spec(_ATTEST_SPEC)
_ATTEST_SPEC.loader.exec_module(attest)
# This repository's own runtime decides which git binary is real and trusted.
sys.path.insert(0, str(ORCHESTRATION))
from codex_agents.git_plumbing import git_executable  # noqa: E402 - this checkout's package


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


def _git_run(target: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_git_binary(target), "-C", str(target), *GIT_SAFE, *args], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, env={**os.environ, "xcrun_nocache": "1"})


def _git(target: Path, *args: str) -> str:
    result = _git_run(target, *args)
    if result.returncode != 0:
        raise InstallRefused(f"target is not a usable Git working tree: {target}")
    return result.stdout.strip()


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
    if _git(target, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all",
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


def _load_record(project: _Project, raw: bytes | None, generated: set[str]) -> dict | None:
    """The installer's record of the entries it added, keeping only generated rules.

    An entry outside every set this installer generates is never treated as the installer's,
    so a planted record cannot make an update or a removal delete a user's own rule.
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
        raise InstallRefused(f"{PERMISSIONS_RECORD} is unreadable; restore it from Git before continuing")
    return {**record, "deny": [entry for entry in record["deny"] if entry in generated]}


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
        record_raw = project.read(PERMISSIONS_RECORD)
        record = _load_record(project, record_raw, generated_rules(orchestration))
    finally:
        project.close()
    # The bytes this plan was made from: apply_settings writes nothing if either changed.
    original = {CLAUDE_SETTINGS: raw, PERMISSIONS_RECORD: record_raw}
    writes: dict[str, bytes | None] = {}
    if operation == "remove":
        if record is None:
            return {"action": "none", "changed_paths": [], "writes": writes, "removed": [],
                    "original": original, "order": []}
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
        # Settings first, then the record: a failure in between leaves a record naming entries
        # already gone, which the next install or removal handles.
        order = [path for path in (CLAUDE_SETTINGS, PERMISSIONS_RECORD) if path in writes]
        return {"action": "remove", "changed_paths": sorted(writes), "writes": writes, "removed": removed,
                "original": original, "order": order}
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
    # The record first, then the settings: a failure in between leaves a record that claims
    # entries not yet in the settings, which the next install adds and owns and a removal skips.
    order = [path for path in (PERMISSIONS_RECORD, CLAUDE_SETTINGS) if path in writes]
    return {"action": action, "changed_paths": sorted(writes), "writes": writes, "added": added,
            "original": original, "order": order}


def apply_settings(target: Path, plan: dict) -> None:
    """Write a plan made moments before; refuse if either file changed since it was read."""

    project = _Project(target)
    try:
        for relative, before in plan["original"].items():
            if project.read(relative) != before:
                raise InstallRefused(f"{relative} changed while setup ran, so it was not overwritten; "
                                     "run setup again")
        for relative in plan["order"]:
            content = plan["writes"][relative]
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
