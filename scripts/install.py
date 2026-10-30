#!/usr/bin/env python3
"""Install, verify or remove the Codex and Claude agent packages in one Git project.

Preview is the default; pass --apply to change files. Each package keeps its own
installer, namespace and manifest. Both packages preview first, and nothing is applied
unless both previews succeed.

    install.py install --target PATH [--apply] [--project-agent-routing FILE]
                       [--operational-checks FILE] [--replace-project-config]
    install.py verify  --target PATH
    install.py remove  --target PATH [--apply]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX_INSTALLER = ROOT / "packages" / "codex-agents" / "scripts" / "install.py"
CLAUDE_INSTALLER = ROOT / "packages" / "claude-agents" / "scripts" / "package.py"
MANAGED_PATHS = (".codex", ".agents", ".claude")
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "REBASE_HEAD": "a rebase",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase or am",
    "BISECT_LOG": "a bisect",
}


class InstallRefused(RuntimeError):
    """The target is not in a state where packages may be changed."""


def _git(target: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)
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
    if mutating and _git(target, "status", "--porcelain", "--untracked-files=all", "--", *MANAGED_PATHS):
        raise InstallRefused("managed paths (.codex, .agents, .claude) have uncommitted changes")


def _run(installer: Path, operation: str, target: Path, apply: bool, extra: list[str]) -> dict:
    command = [sys.executable, str(installer), operation, "--target", str(target), *extra]
    if apply:
        command.append("--apply")
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"error": (result.stderr or result.stdout).strip()[-2000:] or "no output"}
    if not isinstance(report, dict):
        report = {"error": "unexpected installer output"}
    report["exit_code"] = result.returncode
    return report


def _failed(report: dict) -> bool:
    return "error" in report or report["exit_code"] == 2


def operate(operation: str, target: Path, *, apply: bool = False, codex_options: list[str] | None = None) -> dict:
    target = target.absolute()
    check_target(target, mutating=apply and operation != "verify")
    codex_options = codex_options or []
    summary: dict = {"operation": operation, "target": str(target), "applied": False}
    if operation == "verify":
        codex = _run(CODEX_INSTALLER, "verify", target, False, [])
        claude = _run(CLAUDE_INSTALLER, "verify", target, False, [])
        summary.update(codex=codex, claude=claude)
        summary["ready"] = codex.get("ready") is True and claude.get("result") == "verified"
        return summary
    order = [(CODEX_INSTALLER, "codex", codex_options), (CLAUDE_INSTALLER, "claude", [])]
    if operation == "remove":
        order.reverse()
    previews = {name: _run(installer, operation, target, False, extra) for installer, name, extra in order}
    summary.update(previews)
    if any(_failed(report) for report in previews.values()) or not apply:
        summary["ok"] = not any(_failed(report) for report in previews.values())
        return summary
    for installer, name, extra in order:
        summary[name] = _run(installer, operation, target, True, extra)
        if _failed(summary[name]):
            summary["ok"] = False
            summary["next_step"] = f"{name} failed after earlier packages were applied; rerun after fixing"
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
