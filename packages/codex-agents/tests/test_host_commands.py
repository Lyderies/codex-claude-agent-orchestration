from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT, TEMP_ROOT

import cli
from codex_agents.host_commands import (
    PRIMARY_HOST_COMMANDS,
    HostCommandError,
    argv_prefix,
    render_rule,
    rule_file_name,
    rule_path,
    rule_status,
    write_rule,
)
from codex_agents.learning_store import (
    PACKAGE_VERSION,
    CollectorError,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    default_state_root,
)
from codex_agents.learning_adapters import load_client_capabilities

# Commands only the owner runs. None may run outside a sandboxed Codex primary.
OWNER_COMMANDS = {
    "learning-register",
    "learning-serve",
    "learning-record-billing-attestation",
    "learning-configure-owner-key",
    "learning-create-grant-request",
    "learning-attach-grant-signature",
    "learning-delete-project",
    "learning-backup-project",
    "learning-verify-backup",
    "learning-promote",
    "learning-rollback",
    "learning-start-canary",
    "learning-complete-canary",
    "learning-prepare-upgrade-reconciliation",
    "learning-commit-upgrade-reconciliation",
    "learning-record-verified-outcome",
    "learning-calibrate-grader",
    "learning-record-feedback",
    "learning-expire",
    "install-codex-rule",
}
PYTHON = "/opt/example/bin/python3"


def cli_subcommands() -> set[str]:
    parser = cli.build_parser()
    actions = [
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    return set(actions[0].choices)


class AllowlistTests(unittest.TestCase):
    def test_allowlist_is_a_subset_of_real_subcommands(self) -> None:
        self.assertEqual(len(PRIMARY_HOST_COMMANDS), len(set(PRIMARY_HOST_COMMANDS)))
        self.assertLessEqual(set(PRIMARY_HOST_COMMANDS), cli_subcommands())

    def test_allowlist_excludes_every_owner_command(self) -> None:
        self.assertLessEqual(OWNER_COMMANDS, cli_subcommands())
        self.assertEqual(set(), set(PRIMARY_HOST_COMMANDS) & OWNER_COMMANDS)

    def test_host_command_itself_stays_sandboxed(self) -> None:
        self.assertIn("host-command", cli_subcommands())
        self.assertNotIn("host-command", PRIMARY_HOST_COMMANDS)


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.make_project("project")
        self.codex_home = self.base / "codex-home"
        self.environ = {"CODEX_HOME": str(self.codex_home)}

    def make_project(self, name: str) -> Path:
        project = self.base / name
        (project / ".codex" / "orchestration").mkdir(parents=True)
        (project / ".codex" / "orchestration" / "cli.py").write_text("# stand-in\n")
        return project

    @staticmethod
    def pattern(text: str) -> list[str]:
        """The literal tokens of the rule's pattern, decoded like Starlark strings."""

        body = text.split("pattern = [", 1)[1].split("    ],\n    decision", 1)[0]
        return [
            ast.literal_eval(line.strip().rstrip(","))
            for line in body.splitlines()
            if line.strip().startswith('"')
        ]

    def test_rule_pins_absolute_paths_and_only_the_allowlist(self) -> None:
        text = render_rule(self.project, PYTHON)
        cli_path = str(self.project / ".codex" / "orchestration" / "cli.py")
        self.assertEqual([PYTHON, cli_path, *PRIMARY_HOST_COMMANDS], self.pattern(text))
        self.assertEqual([PYTHON, cli_path], argv_prefix(self.project, PYTHON))
        self.assertEqual(1, text.count("prefix_rule("))
        self.assertIn('    decision = "allow",\n', text)
        self.assertIn("justification = ", text)
        # Codex checks these examples when it loads the file.
        match = text.split("    match = [\n", 1)[1].split("    ],\n", 1)[0]
        not_match = text.split("    not_match = [\n", 1)[1].split("    ],\n", 1)[0]
        self.assertIn('"learning-status"', match)
        self.assertIn('"learning-register"', not_match)
        self.assertIn('["python3", ".codex/orchestration/cli.py", "learning-status"', not_match)
        self.assertIn(f'["{PYTHON}", ".codex/orchestration/cli.py"', not_match)
        for owner in OWNER_COMMANDS - {"learning-register"}:
            self.assertNotIn(f'"{owner}"', text)
        header = text.split("prefix_rule(", 1)[0]
        self.assertTrue(all(line.startswith("#") for line in header.strip().splitlines()))
        self.assertIn("with the owner's consent", header)
        self.assertIn("To remove it, delete this file", header)

    def test_rule_quotes_backslashes_and_double_quotes(self) -> None:
        project = self.make_project('odd "name" \\ dir')
        text = render_rule(project, PYTHON)
        cli_path = str(project / ".codex" / "orchestration" / "cli.py")
        self.assertEqual(cli_path, self.pattern(text)[1])
        self.assertIn('odd \\"name\\" \\\\ dir', text)

    def test_line_breaks_and_control_characters_are_refused(self) -> None:
        for name in ("line\nbreak", "tab\there", "bell\x07", "rtl‮override", "sep x"):
            with self.subTest(name=repr(name)):
                project = self.make_project(name)
                with self.assertRaisesRegex(HostCommandError, "cannot hold"):
                    render_rule(project, PYTHON)
        for interpreter in ("/opt/py\nthon", "python3", "", "relative/python3"):
            with self.subTest(interpreter=repr(interpreter)):
                with self.assertRaises(HostCommandError):
                    render_rule(self.project, interpreter)

    def test_the_interpreter_is_the_running_one_unresolved(self) -> None:
        self.assertEqual(sys.executable, argv_prefix(self.project)[0])
        self.assertIn(_quoted(sys.executable), render_rule(self.project))

    def test_project_cli_must_be_a_regular_installed_file(self) -> None:
        with self.assertRaisesRegex(HostCommandError, "not installed"):
            render_rule(self.base, PYTHON)
        linked = self.base / "linked"
        (linked / ".codex" / "orchestration").mkdir(parents=True)
        (linked / ".codex" / "orchestration" / "cli.py").symlink_to(
            self.project / ".codex" / "orchestration" / "cli.py"
        )
        with self.assertRaisesRegex(HostCommandError, "symlink"):
            render_rule(linked, PYTHON)
        borrowed = self.base / "borrowed"
        borrowed.mkdir()
        (borrowed / ".codex").symlink_to(self.project / ".codex")
        with self.assertRaisesRegex(HostCommandError, "symlink"):
            render_rule(borrowed, PYTHON)

    def test_file_name_is_a_digest_of_the_resolved_root(self) -> None:
        digest = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:12]
        self.assertEqual(f"agent-orchestration-{digest}.rules", rule_file_name(self.project))
        self.assertEqual(
            self.codex_home / "rules" / f"agent-orchestration-{digest}.rules",
            rule_path(self.project, self.environ),
        )
        home = Path.home() / ".codex" / "rules" / f"agent-orchestration-{digest}.rules"
        self.assertEqual(home, rule_path(self.project, {}))
        with self.assertRaisesRegex(HostCommandError, "absolute"):
            rule_path(self.project, {"CODEX_HOME": "relative/codex"})

    def test_write_is_private_atomic_and_idempotent(self) -> None:
        self.assertEqual("missing", rule_status(self.project, self.environ, PYTHON))
        self.assertEqual("created", write_rule(self.project, self.environ, PYTHON))
        path = rule_path(self.project, self.environ)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        self.assertEqual(render_rule(self.project, PYTHON), path.read_text())
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        before = path.stat()
        self.assertEqual("unchanged", write_rule(self.project, self.environ, PYTHON))
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_a_differing_rule_is_replaced_only_when_asked(self) -> None:
        write_rule(self.project, self.environ, PYTHON)
        path = rule_path(self.project, self.environ)
        other = "/opt/other/bin/python3"
        self.assertEqual("stale", rule_status(self.project, self.environ, other))
        self.assertEqual("differs", write_rule(self.project, self.environ, other))
        self.assertEqual(render_rule(self.project, PYTHON), path.read_text())
        self.assertEqual(
            "replaced", write_rule(self.project, self.environ, other, replace=True)
        )
        self.assertEqual(render_rule(self.project, other), path.read_text())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("installed", rule_status(self.project, self.environ, other))
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_a_symlinked_rules_directory_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.codex_home.mkdir()
        (self.codex_home / "rules").symlink_to(elsewhere)
        with self.assertRaisesRegex(HostCommandError, "rules directory is a symlink"):
            write_rule(self.project, self.environ, PYTHON)
        self.assertEqual([], os.listdir(elsewhere))
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_a_symlinked_rule_file_is_refused_and_never_followed(self) -> None:
        target = self.base / "target.rules"
        target.write_text(render_rule(self.project, PYTHON))
        rules = self.codex_home / "rules"
        rules.mkdir(parents=True)
        (rules / rule_file_name(self.project)).symlink_to(target)
        for replace in (False, True):
            with self.assertRaisesRegex(HostCommandError, "rule file is a symlink"):
                write_rule(self.project, self.environ, PYTHON, replace=replace)
        self.assertEqual(render_rule(self.project, PYTHON), target.read_text())
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_a_non_regular_rule_file_is_refused(self) -> None:
        rules = self.codex_home / "rules"
        (rules / rule_file_name(self.project)).mkdir(parents=True)
        with self.assertRaisesRegex(HostCommandError, "not a regular file"):
            write_rule(self.project, self.environ, PYTHON, replace=True)
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    @unittest.skipUnless(shutil.which("codex"), "the Codex CLI is not installed")
    def test_codex_loads_the_rule_and_its_self_tests(self) -> None:
        project = self.make_project('proj "q" \\ s;x')
        rules = self.base / "check.rules"
        rules.write_text(render_rule(project, PYTHON))
        prefix = argv_prefix(project, PYTHON)

        def check(*argv: str) -> dict:
            completed = subprocess.run(
                ["codex", "execpolicy", "check", "--rules", str(rules), *argv],
                capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                env={**os.environ, "CODEX_HOME": str(self.codex_home)},
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            return json.loads(completed.stdout)

        self.codex_home.mkdir()
        for command in PRIMARY_HOST_COMMANDS:
            self.assertEqual("allow", check(*prefix, command, "--x").get("decision"), command)
        for argv in (
            [*prefix, "learning-register"],
            [*prefix, "host-command"],
            ["python3", ".codex/orchestration/cli.py", "dispatch"],
            [PYTHON, ".codex/orchestration/cli.py", "dispatch"],
        ):
            self.assertEqual([], check(*argv)["matchedRules"], argv)


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class HostCommandCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        (self.project / ".codex" / "orchestration").mkdir(parents=True)
        (self.project / ".codex" / "orchestration" / "cli.py").write_text("# stand-in\n")
        self.codex_home = self.base / "codex-home"

    def run_cli(self, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), *arguments,
             "--project-root", str(self.project)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": str(self.codex_home)},
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def test_host_command_reports_the_prefix_and_rule_without_writing(self) -> None:
        report = self.run_cli("host-command", expected=1)
        cli_path = str(self.project / ".codex" / "orchestration" / "cli.py")
        self.assertEqual(
            {"ready": False, "argv_prefix": [sys.executable, cli_path],
             "codex_rule": "missing",
             "rule_file": str(rule_path(self.project, {"CODEX_HOME": str(self.codex_home)}))},
            report,
        )
        self.assertFalse(self.codex_home.exists())

    def test_install_then_host_command_reports_installed(self) -> None:
        printed = self.run_cli("install-codex-rule", "--print")
        self.assertFalse(self.codex_home.exists())
        self.assertEqual(render_rule(self.project), printed["rule_text"])
        self.assertEqual("created", self.run_cli("install-codex-rule")["codex_rule"])
        self.assertEqual(printed["rule_text"], Path(printed["rule_file"]).read_text())
        self.assertEqual("unchanged", self.run_cli("install-codex-rule")["codex_rule"])
        report = self.run_cli("host-command")
        self.assertEqual(("installed", True), (report["codex_rule"], report["ready"]))

    def test_a_differing_rule_needs_replace(self) -> None:
        environ = {"CODEX_HOME": str(self.codex_home)}
        write_rule(self.project, environ, "/opt/other/bin/python3")
        self.assertEqual("stale", self.run_cli("host-command", expected=1)["codex_rule"])
        refused = self.run_cli("install-codex-rule", expected=1)
        self.assertEqual(("differs", False), (refused["codex_rule"], refused["ready"]))
        self.assertEqual(
            "replaced", self.run_cli("install-codex-rule", "--replace")["codex_rule"]
        )
        self.assertEqual("installed", self.run_cli("host-command")["codex_rule"])

    def test_refusals_are_fixed_messages_without_paths(self) -> None:
        self.codex_home.mkdir()
        (self.codex_home / "rules").symlink_to(self.base)
        report = self.run_cli("install-codex-rule", expected=2)
        self.assertEqual(
            {"ready": False, "error": "Codex rules directory is a symlink"}, report
        )


class SandboxHintTests(unittest.TestCase):
    """A primary that cannot reach the collector keeps its error and gets a hint."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        LearningState(self.state_root)
        self.project = base / "project"
        self.project.mkdir()

    def run_main(self, command: str, *arguments: str, env: dict | None = None) -> dict:
        stdout = io.StringIO()
        argv = ["cli.py", command, "--state-root", str(self.state_root),
                "--project-root", str(self.project), *arguments]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(stdout):
            cli.main()
        return json.loads(stdout.getvalue())

    def blocked(self, *args, **kwargs):
        raise PermissionError("blocked by the sandbox")

    def test_a_blocked_lifecycle_command_gets_the_hint(self) -> None:
        with mock.patch.object(cli.CollectorMaintenance, "acquire", self.blocked):
            report = self.run_main("learning-open-orchestration")
        self.assertEqual("filesystem operation failed", report["error"])
        self.assertFalse(report["ready"])
        self.assertIn("host-command", report["hint"])
        self.assertIn("argv_prefix", report["hint"])
        self.assertIn("./setup.sh", report["hint"])

    def test_no_hint_without_codex_markers_or_for_owner_commands(self) -> None:
        unmarked = {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}
        with mock.patch.object(cli.CollectorMaintenance, "acquire", self.blocked):
            self.assertEqual(
                {"ready": False, "error": "filesystem operation failed"},
                self.run_main("learning-open-orchestration", env=unmarked),
            )
            self.assertEqual(
                {"ready": False, "error": "filesystem operation failed"},
                self.run_main("learning-expire"),
            )

    def test_learning_status_hint_follows_reachability(self) -> None:
        with mock.patch.object(cli, "ensure_collector", self.blocked):
            report = self.run_main("learning-status")
        self.assertEqual("filesystem operation failed", report["error"])
        self.assertIn("hint", report)

        def unhealthy(*args, **kwargs):
            raise CollectorError("collector did not become healthy")

        with mock.patch.object(cli, "ensure_collector", unhealthy):
            report = self.run_main("learning-status")
        self.assertEqual("collector or project registration is unavailable", report["error"])
        self.assertIn("hint", report)

        def rejected(*args, **kwargs):
            raise CollectorError("collector rejected request: x", code="project_unregistered")

        with mock.patch.object(cli, "ensure_collector", rejected):
            report = self.run_main("learning-status")
        self.assertNotIn("hint", report)


class OwnRootTests(unittest.TestCase):
    """An installed CLI pins its primary-host commands to its project and state.

    The owner's Codex rule may run them outside the sandbox with model-chosen
    arguments, so another project, state root or policy is refused first.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        shutil.copytree(
            BUNDLE_ROOT, self.project,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        (self.project / ".codex" / "orchestration" / "install-manifest.json").write_text(
            json.dumps({"format": 1, "version": PACKAGE_VERSION, "files": {}})
        )
        self.cli = self.project / ".codex" / "orchestration" / "cli.py"
        # The default learning state follows HOME (XDG_STATE_HOME on Linux), which
        # a model cannot set for a command Codex runs outside the sandbox.
        self.home = self.base / "home"
        self.env = {
            **os.environ, "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with mock.patch.dict(os.environ, self.env):
            self.default_state = default_state_root()
        self.other_state = self.base / "other-state"
        # A model-built repository whose own configuration runs a command on any
        # index refresh; it must never see a git call from an installed CLI.
        self.other = self.base / "other"
        self.marker = self.base / "hostile-git-ran"
        hook = self.base / "fsmonitor.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{self.marker}"\nexit 1\n')
        hook.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(self.other)], check=True)
        (self.other / "file").write_text("x\n")
        subprocess.run(
            ["git", "-C", str(self.other), "config", "core.fsmonitor", str(hook)], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.other), "status", "--porcelain"],
            check=True, capture_output=True,
        )
        self.assertTrue(self.marker.exists(), "the hostile repository is not armed")
        self.marker.unlink()

    def run_cli(self, *arguments: str, cwd: Path | None = None) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(self.cli), *arguments],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env=self.env, cwd=cwd,
        )
        return completed.returncode, json.loads(completed.stdout)

    def assert_refused(self, option: str, *arguments: str) -> None:
        code, report = self.run_cli(*arguments)
        self.assertEqual(2, code, arguments)
        self.assertFalse(report["ready"])
        self.assertTrue(report["error"].startswith(f"{option} is refused"), (arguments, report))
        self.assertEqual({"ready", "error"}, set(report))

    def test_other_roots_state_and_policy_are_refused_before_any_work(self) -> None:
        own, other, state = str(self.project), str(self.other), str(self.other_state)
        context = ("--assignment", "a.json", "--task", "t.json", "--parent-grant", "g.json")
        lifecycle = {
            "dispatch": (*context, "--route", "r.json", "--run-handle", "h"),
            "provider-observe": ("--provider", "claude", "--billing-probe"),
            "learning-open-host-run": (
                *context, "--route", "r.json", "--orchestration-handle", "o",
            ),
            "learning-run-operational-checks": (
                "--host-outcome-digest", "0" * 64, "--verifier-host-outcome-digest", "1" * 64,
            ),
            "learning-status": (),
            "learning-open-orchestration": (),
            "learning-route": ("--task", "t.json"),
        }
        for command, extra in lifecycle.items():
            with self.subTest(command=command):
                self.assert_refused("--project-root", command, *extra, "--project-root", other)
                self.assert_refused(
                    "--state-root", command, *extra, "--project-root", own, "--state-root", state,
                )
                # argparse keeps the last value; that is the one checked.
                self.assert_refused(
                    "--project-root", command, *extra,
                    "--project-root", own, "--project-root", other,
                )
                # Abbreviations reach the same option.
                self.assert_refused("--project-root", command, *extra, "--proj", other)
        self.assert_refused("--root", "validate", "--root", other)
        self.assert_refused("--root", "inventory", "--root", other)
        self.assert_refused("--root", "route", "--task", "t.json", "--root", other)
        self.assert_refused("--policy", "route", "--task", "t.json", "--policy", "p.json")
        self.assert_refused("--policy", "inventory", "--policy", "p.json")
        for command in ("validate-assignment", "result-preflight", "validate-result"):
            with self.subTest(command=command):
                result = ("--result", "r.json") if command == "validate-result" else ()
                self.assert_refused("--policy", command, *context, *result, "--pol", "p.json")
                self.assert_refused(
                    "--project-root", command, *context, *result, "--project-root", other,
                )
                self.assert_refused(
                    "--state-root", command, *context, *result, "--state-root", state,
                )
        self.assertFalse(self.marker.exists(), "an installed CLI ran git in another project")
        self.assertFalse(self.other_state.exists())

    def test_own_project_and_default_state_still_work(self) -> None:
        state = LearningState(self.default_state)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        ledger.register_project(self.project, now_ms=1)
        ledger.close()
        link = self.base / "link"
        link.symlink_to(self.project)
        for arguments, cwd in (
            (("--project-root", str(self.project)), None),
            (("--project-root", str(link), "--state-root", str(self.default_state)), None),
            (("--project-root", "."), self.project),
        ):
            with self.subTest(arguments=arguments):
                code, report = self.run_cli("learning-open-orchestration", *arguments, cwd=cwd)
                self.assertEqual(0, code, report)
                self.assertTrue(report["ready"])
        for arguments in ((), ("--root", str(self.project))):
            code, report = self.run_cli("validate", *arguments)
            self.assertIn("package_ready", report)
        # Owner commands and the source tree are not pinned.
        code, report = self.run_cli("host-command", "--project-root", str(self.project))
        self.assertIn("argv_prefix", report)

    def test_source_tree_cli_keeps_its_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), "learning-status",
             "--state-root", str(self.other_state), "--project-root", str(self.project)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env=self.env,
        )
        self.assertNotIn("is refused", completed.stdout)


if __name__ == "__main__":
    unittest.main()
