from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
_SPEC = importlib.util.spec_from_file_location("umbrella_install", _SCRIPT)
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
# Project-owned configuration the Codex installer writes and deliberately keeps on removal.
PROJECT_CONFIG = {
    ".codex/orchestration/project-agent-routing.json",
    ".codex/orchestration/operational-checks.json",
}


def outside_temporary_folders() -> Path | None:
    """An existing folder outside every temporary root (the Codex rule step refuses those)."""

    candidates = []
    if sys.platform == "darwin":
        candidates.append(subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                                         text=True, check=False).stdout.strip())
    candidates += [os.environ.get("XDG_RUNTIME_DIR") or "", str(Path(__file__).resolve().parents[1])]
    temporary = [Path(os.path.realpath(path)) for path in
                 ("/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp")]
    for candidate in candidates:
        real = Path(os.path.realpath(candidate)) if candidate else None
        if real and real.is_dir() and not any(real == root or root in real.parents for root in temporary):
            return real
    return None


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], env=GIT_ENV, capture_output=True,
                          text=True, check=check)


def inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative.split("/")[0] != ".git" and path.is_file() and not path.is_symlink():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def make_target(base: Path) -> tuple[Path, list[str]]:
    """A committed Git project and the Codex installer's required project inputs."""

    target = base / "target"
    target.mkdir()
    git(target, "init", "-q")
    (target / "README.md").write_text("# Target\n", encoding="utf-8")
    qa = target / "qa-check"
    qa.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8")
    qa.chmod(0o700)
    git(target, "add", ".")
    git(target, "commit", "-qm", "initial")
    inputs = base / "inputs"
    inputs.mkdir()
    routing = inputs / "project-agent-routing.json"
    routing.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}), encoding="utf-8")
    check = {
        "driver": "python-wrapper",
        "argv": ["./qa-check"],
        "executable_sha256": hashlib.sha256(qa.read_bytes()).hexdigest(),
        "interpreter_sha256": hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest(),
        "runtime_tree": None,
        "timeout_seconds": 5,
    }
    checks = inputs / "operational-checks.json"
    checks.write_text(json.dumps({
        "version": 1,
        "deterministic_checks": [{"check_id": "tests", **check}],
        "security_checks": [{"check_id": "security", **check}],
    }), encoding="utf-8")
    return target, ["--project-agent-routing", str(routing), "--operational-checks", str(checks)]


class UmbrellaInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.target, self.codex_options = make_target(self.base)

    def install(self, target: Path | None = None, apply: bool = False) -> dict:
        return installer.operate("install", target or self.target, apply=apply,
                                 codex_options=self.codex_options)

    def test_preview_reports_both_packages_and_changes_nothing(self) -> None:
        before = inventory(self.target)
        summary = self.install()
        self.assertTrue(summary["ok"], summary)
        self.assertFalse(summary["applied"])
        self.assertNotIn("error", summary["codex"])
        self.assertEqual(len(summary["claude"]["changed_paths"]), 10)
        self.assertEqual(inventory(self.target), before)

    def test_install_verify_and_remove_restore_unmanaged_files(self) -> None:
        before = inventory(self.target)
        installed = self.install(apply=True)
        self.assertTrue(installed["ok"] and installed["applied"], installed)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        verified = installer.operate("verify", self.target)
        self.assertEqual(verified["claude"]["result"], "verified", verified)
        self.assertTrue(verified["codex"]["package_ready"], verified)
        # The Codex package's Claude skill sits beside the Claude package's files.
        self.assertTrue((self.target / ".claude/skills/agent-orchestration/SKILL.md").is_file())
        self.assertTrue((self.target / ".claude/skills/claude-agents/SKILL.md").is_file())
        removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"] and removed["applied"], removed)
        after = {path: digest for path, digest in inventory(self.target).items() if path not in PROJECT_CONFIG}
        self.assertEqual(after, before)

    def test_existing_install_is_adopted_without_changes(self) -> None:
        self.install(apply=True)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        before = inventory(self.target)
        again = installer.operate("install", self.target, apply=True)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["codex"]["action"], "update")
        self.assertEqual(again["claude"]["changed_paths"], [])
        self.assertEqual(inventory(self.target), before)

    def test_merge_in_progress_is_refused(self) -> None:
        git(self.target, "switch", "-q", "-c", "other")
        (self.target / "README.md").write_text("# Other\n", encoding="utf-8")
        git(self.target, "commit", "-qam", "other")
        git(self.target, "switch", "-q", "-")
        (self.target / "README.md").write_text("# Main\n", encoding="utf-8")
        git(self.target, "commit", "-qam", "main")
        self.assertNotEqual(git(self.target, "merge", "other", check=False).returncode, 0)
        with self.assertRaisesRegex(installer.InstallRefused, "a merge is in progress"):
            self.install()

    def test_each_in_progress_operation_is_refused(self) -> None:
        git_dir = self.target / ".git"
        for marker, is_dir in (("CHERRY_PICK_HEAD", False), ("REVERT_HEAD", False), ("REBASE_HEAD", False),
                               ("rebase-merge", True), ("rebase-apply", True), ("sequencer", True),
                               ("BISECT_LOG", False)):
            with self.subTest(marker=marker):
                path = git_dir / marker
                path.mkdir() if is_dir else path.write_text("0" * 40 + "\n")
                with self.assertRaisesRegex(installer.InstallRefused, "in progress"):
                    self.install()
                shutil.rmtree(path) if is_dir else path.unlink()

    def test_unmerged_entries_without_a_merge_are_refused(self) -> None:
        blob = git(self.target, "hash-object", "-w", "README.md").stdout.strip()
        entries = "".join(f"100644 {blob} {stage}\tREADME.md\n" for stage in (1, 2, 3))
        subprocess.run(["git", "-C", str(self.target), "update-index", "--index-info"], input=entries,
                       env=GIT_ENV, text=True, check=True)
        with self.assertRaisesRegex(installer.InstallRefused, "unmerged paths"):
            self.install()

    def assert_refused_before_any_write(self, summary: dict, before: dict[str, str]) -> None:
        self.assertFalse(summary["ok"], summary)
        self.assertFalse(summary["applied"])
        self.assertFalse((self.target / installer.CODEX_MANIFEST).exists())
        self.assertEqual(inventory(self.target), before)

    def test_committed_claude_collision_refuses_before_codex_writes(self) -> None:
        collision = self.target / ".claude" / "agents" / "claude_agents_researcher.md"
        collision.parent.mkdir(parents=True)
        collision.write_text("project-owned agent\n", encoding="utf-8")
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "project agent")
        before = inventory(self.target)
        self.assert_refused_before_any_write(self.install(apply=True), before)

    def test_symlinked_claude_directory_refuses_before_codex_writes(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.target / ".claude").symlink_to(elsewhere, target_is_directory=True)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "symlinked .claude")
        before = inventory(self.target)
        self.assert_refused_before_any_write(self.install(apply=True), before)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_failed_second_package_rolls_back_the_first(self) -> None:
        before = inventory(self.target)
        real_run = installer._run

        def failing_claude_apply(script, operation, target, apply, extra):
            if script == installer.CLAUDE_INSTALLER and apply:
                return {"error": "simulated write failure", "exit_code": 2}
            return real_run(script, operation, target, apply, extra)

        installer._run = failing_claude_apply
        self.addCleanup(setattr, installer, "_run", real_run)
        summary = self.install(apply=True)
        self.assertFalse(summary["ok"], summary)
        self.assertEqual(summary["rollback"], [{"package": "codex", "action": "remove", "ok": True}])
        self.assertFalse((self.target / installer.CODEX_MANIFEST).exists())
        after = {path: digest for path, digest in inventory(self.target).items() if path not in PROJECT_CONFIG}
        self.assertEqual(after, before)

    def test_apply_is_refused_when_managed_paths_are_dirty(self) -> None:
        self.install(apply=True)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        managed = self.target / ".claude" / "skills" / "claude-agents" / "SKILL.md"
        managed.write_text(managed.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallRefused, "uncommitted changes"):
            installer.operate("remove", self.target, apply=True)

    def test_linked_worktree_target_previews(self) -> None:
        linked = self.base / "linked"
        git(self.target, "worktree", "add", "-q", str(linked))
        summary = self.install(target=linked)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(len(summary["claude"]["changed_paths"]), 10)

    def test_subdirectory_target_is_refused(self) -> None:
        nested = self.target / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(installer.InstallRefused, "top of its working tree"):
            self.install(target=nested)

    # Python and Git run isolated

    def test_package_installers_run_isolated_in_the_real_interpreter(self) -> None:
        seen: list[list[str]] = []
        real_run = subprocess.run

        def spy(command, *args, **kwargs):
            seen.append([str(part) for part in command])
            return real_run(command, *args, **kwargs)

        with mock.patch.object(installer.subprocess, "run", spy):
            self.install()
        pythons = [command for command in seen if command[0] == os.path.realpath(sys.executable)]
        self.assertEqual(len(pythons), 2)
        for command in pythons:
            self.assertEqual(command[0], os.path.realpath(sys.executable))
            self.assertEqual(command[1:6], ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"])
        for command in seen:
            if command[0] == "git":
                self.assertEqual(command[3:7], list(installer.GIT_SAFE), command)

    def test_git_never_runs_a_planted_fsmonitor(self) -> None:
        marker = self.base / "fsmonitor-ran"
        monitor = self.base / "monitor.sh"
        monitor.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        monitor.chmod(0o755)
        git(self.target, "config", "core.fsmonitor", str(monitor))
        installer.check_target(self.target, mutating=True)
        self.assertFalse(marker.exists())
        git(self.target, "status", "--porcelain")  # the same setting does run for plain git
        self.assertTrue(marker.exists())

    # .claude/settings.json deny rules

    def test_deny_rules_cover_every_owner_command_and_no_primary_command(self) -> None:
        orchestration = installer.ORCHESTRATION
        parsed = installer._parser_commands(orchestration / "cli.py")
        primary = installer._primary_commands(orchestration / "codex_agents" / "host_commands.py")
        self.assertEqual(len(primary), 23)
        owner = installer.owner_only_commands()
        self.assertEqual(set(owner), (set(parsed) - set(primary)) | {"install-codex-rule", "remove-codex-rule"})
        for name in ("learning-register", "learning-record-billing-attestation", "learning-configure-owner-key",
                     "learning-create-grant-request", "learning-attach-grant-signature",
                     "learning-delete-project", "learning-backup-project", "learning-verify-backup",
                     "learning-promote", "learning-rollback", "learning-start-canary",
                     "learning-complete-canary", "learning-serve", "learning-prepare-upgrade-reconciliation",
                     "learning-commit-upgrade-reconciliation", "install-codex-rule", "remove-codex-rule"):
            self.assertIn(name, owner)
        rules = installer.deny_rules()
        for name in owner:
            self.assertIn(f"Bash(*.codex/orchestration/cli.py {name})", rules)
            self.assertIn(f"Bash(*.codex/orchestration/cli.py {name} *)", rules)
        for name in primary:
            self.assertFalse(any(rule.startswith(f"Bash(*.codex/orchestration/cli.py {name}")
                                 and rule[len(f"Bash(*.codex/orchestration/cli.py {name}"):][:1] in (")", " ")
                                 for rule in rules), name)
        for path in ("/.codex/**", "/.agents/**", "/.claude/settings*.json", "/.git/**"):
            self.assertIn(f"Edit({path})", rules)

    def settings(self) -> dict:
        return json.loads((self.target / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_install_creates_settings_and_remove_takes_them_away(self) -> None:
        preview = self.install()
        self.assertEqual(preview["claude_settings"]["action"], "create")
        self.assertEqual(preview["claude_settings"]["changed_paths"],
                         [installer.PERMISSIONS_RECORD, installer.CLAUDE_SETTINGS])
        self.assertFalse((self.target / ".claude" / "settings.json").exists())
        self.assertTrue(self.install(apply=True)["ok"])
        self.assertEqual(self.settings(), {"permissions": {"deny": installer.deny_rules()}})
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        verified = installer.operate("verify", self.target)
        self.assertTrue(verified["claude_settings"]["ready"], verified["claude_settings"])
        removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["claude_settings"]["action"], "remove")
        self.assertFalse((self.target / ".claude" / "settings.json").exists())
        self.assertFalse((self.target / installer.PERMISSIONS_RECORD).exists())

    def test_user_settings_are_kept_and_only_added_entries_are_removed(self) -> None:
        mine = installer.deny_rules()[0]
        user = {
            "model": "opus",
            "permissions": {"allow": ["Bash(npm test *)"], "deny": ["Read(./.env)", mine, 7],
                            "ask": ["Bash(git push *)"]},
            "env": {"FOO": "bar"},
        }
        settings = self.target / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps(user), encoding="utf-8")
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "user settings")
        summary = self.install(apply=True)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["claude_settings"]["action"], "merge")
        merged = self.settings()
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["env"], {"FOO": "bar"})
        self.assertEqual(merged["permissions"]["allow"], ["Bash(npm test *)"])
        self.assertEqual(merged["permissions"]["ask"], ["Bash(git push *)"])
        self.assertEqual(merged["permissions"]["deny"][:3], ["Read(./.env)", mine, 7])
        self.assertEqual(set(merged["permissions"]["deny"][3:]) | {mine}, set(installer.deny_rules()))
        record = json.loads((self.target / installer.PERMISSIONS_RECORD).read_text())
        self.assertNotIn(mine, record["deny"])  # the user had it first, so it stays theirs
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        again = self.install(apply=True)  # an update with nothing new changes nothing
        self.assertEqual(again["claude_settings"]["action"], "unchanged")
        removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), user)

    def test_an_entry_install_added_and_no_longer_wants_is_removed_on_update(self) -> None:
        self.assertTrue(self.install(apply=True)["ok"])
        record_path = self.target / installer.PERMISSIONS_RECORD
        record = json.loads(record_path.read_text())
        record["deny"].append("Bash(*.codex/orchestration/cli.py retired-command)")
        record_path.write_text(json.dumps(record))
        data = self.settings()
        data["permissions"]["deny"].append("Bash(*.codex/orchestration/cli.py retired-command)")
        (self.target / ".claude" / "settings.json").write_text(json.dumps(data))
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        self.assertTrue(self.install(apply=True)["ok"])
        self.assertEqual(self.settings()["permissions"]["deny"], installer.deny_rules())

    def test_unmergeable_settings_refuse_before_any_write(self) -> None:
        settings = self.target / ".claude" / "settings.json"
        settings.parent.mkdir()
        for content in ("{not json", "[]", '{"permissions": []}', '{"permissions": {"deny": "x"}}'):
            with self.subTest(content=content):
                settings.write_text(content, encoding="utf-8")
                git(self.target, "add", ".")
                git(self.target, "commit", "-qm", "settings", check=False)
                before = inventory(self.target)
                summary = self.install(apply=True)
                self.assertIn("error", summary["claude_settings"])
                self.assert_refused_before_any_write(summary, before)

    def test_a_symlinked_settings_file_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text("{}", encoding="utf-8")
        (self.target / ".claude").mkdir()
        (self.target / ".claude" / "settings.json").symlink_to(elsewhere)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "symlinked settings")
        before = inventory(self.target)
        summary = self.install(apply=True)
        self.assertIn("symlink", summary["claude_settings"]["error"])
        self.assert_refused_before_any_write(summary, before)
        self.assertEqual(elsewhere.read_text(), "{}")

    def test_verify_reports_missing_deny_rules(self) -> None:
        self.assertTrue(self.install(apply=True)["ok"])
        data = self.settings()
        dropped = data["permissions"]["deny"].pop()
        (self.target / ".claude" / "settings.json").write_text(json.dumps(data))
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        verified = installer.operate("verify", self.target)
        self.assertFalse(verified["ready"])
        self.assertEqual(verified["claude_settings"]["missing"], [dropped])
        self.assertIn("required_action", verified["claude_settings"])


class CodexRuleRemovalTests(unittest.TestCase):
    """Uninstall takes out the Codex commands rule through the project's own owner command."""

    def setUp(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        self.base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.target, codex_options = make_target(self.base)
        installed = installer.operate("install", self.target, apply=True, codex_options=codex_options)
        self.assertTrue(installed["ok"], installed)
        git(self.target, "add", "-A")
        git(self.target, "commit", "-qm", "install packages")
        self.calls: list[list[str]] = []

    def cli(self, reports: dict):
        def fake(target, args):
            self.calls.append(list(args))
            return reports[args[0]]
        return mock.patch.object(installer, "_project_cli", fake)

    def commands(self, *, with_rule_command: bool):
        real = installer._parser_commands

        def parsed(cli):
            names = [name for name in real(cli) if name != installer.RULE_COMMAND]
            return names + [installer.RULE_COMMAND] if with_rule_command else names
        return mock.patch.object(installer, "_parser_commands", parsed)

    def test_remove_calls_the_owner_command_before_removing_files(self) -> None:
        reports = {installer.RULE_COMMAND: {"ready": True, "removed": ["/rules/agent-orchestration-x.rules"]}}
        with self.cli(reports), self.commands(with_rule_command=True):
            preview = installer.operate("remove", self.target)
            self.assertEqual(preview["codex_rule"], {"action": installer.RULE_COMMAND})
            self.assertEqual(self.calls, [])  # a preview never runs it
            removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(self.calls, [[installer.RULE_COMMAND, "--project-root", str(self.target.absolute())]])
        self.assertEqual(removed["codex_rule"]["removed"], ["/rules/agent-orchestration-x.rules"])
        self.assertFalse((self.target / installer.CODEX_MANIFEST).exists())

    def test_a_failed_rule_removal_changes_nothing_else(self) -> None:
        before = inventory(self.target)
        with self.cli({installer.RULE_COMMAND: {"ready": False}}), self.commands(with_rule_command=True):
            removed = installer.operate("remove", self.target, apply=True)
        self.assertFalse(removed["ok"])
        self.assertIn("error", removed["codex_rule"])
        self.assertEqual(inventory(self.target), before)

    def test_an_old_cli_fails_closed_and_names_the_rule_file(self) -> None:
        before = inventory(self.target)
        status = {"ready": True, "codex_rule": "installed", "rule_file": "/h/.codex/rules/agent-orchestration-a.rules"}
        with self.cli({"host-command": status}), self.commands(with_rule_command=False):
            removed = installer.operate("remove", self.target, apply=True)
        self.assertFalse(removed["ok"])
        self.assertIn("delete /h/.codex/rules/agent-orchestration-a.rules yourself", removed["codex_rule"]["error"])
        self.assertEqual(self.calls, [["host-command", "--project-root", str(self.target.absolute())]])
        self.assertEqual(inventory(self.target), before)

    def test_an_old_cli_with_no_rule_lets_remove_continue(self) -> None:
        with self.cli({"host-command": {"ready": True, "codex_rule": "missing"}}), \
                self.commands(with_rule_command=False):
            removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["codex_rule"]["action"], "none")

    def test_a_modified_cli_is_never_run(self) -> None:
        cli = self.target / ".codex" / "orchestration" / "cli.py"
        cli.write_text(cli.read_text() + "\n# changed\n")
        git(self.target, "commit", "-qam", "change the cli")
        ran: list[list[str]] = []
        real_run = subprocess.run

        def spy(command, *args, **kwargs):
            ran.append([str(part) for part in command])
            return real_run(command, *args, **kwargs)

        with self.commands(with_rule_command=True), mock.patch.object(installer.subprocess, "run", spy):
            removed = installer.operate("remove", self.target, apply=True)
        self.assertFalse(removed["ok"])
        self.assertIn("not an exact copy", removed["codex_rule"]["error"])
        self.assertFalse(any(str(cli) in command for command in ran))


if __name__ == "__main__":
    unittest.main()
