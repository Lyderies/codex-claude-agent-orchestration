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


def isolate_home(test: unittest.TestCase, home: Path) -> None:
    """Point HOME at a test folder (remove runs remove-codex-rule, which reads ~/.codex)."""

    home.mkdir(mode=0o700, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if key != "CODEX_HOME"}
    patcher = mock.patch.dict(os.environ, {**environment, "HOME": str(home)}, clear=True)
    patcher.start()
    test.addCleanup(patcher.stop)


class UmbrellaInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
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
        trusted = installer.git_executable([self.target])
        gits = [command for command in seen if os.path.basename(command[0]) == "git"]
        self.assertTrue(gits)
        for command in gits:
            self.assertEqual(command[0], trusted, command)  # never a PATH lookup
            self.assertEqual(command[3:3 + len(installer.GIT_SAFE)], list(installer.GIT_SAFE), command)
        for option in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "commit.gpgSign=false",
                       "tag.gpgSign=false", "gc.auto=0", "maintenance.auto=false"):
            self.assertIn(option, installer.GIT_SAFE)

    def test_repository_helper_programs_refuse_before_any_write(self) -> None:
        before = inventory(self.target)
        for key, value in (("filter.planted.clean", "./planted.sh"), ("filter.planted.process", "./p.sh"),
                           ("diff.planted.textconv", "./p.sh"), ("merge.planted.driver", "./p.sh"),
                           ("core.sshCommand", "./p.sh"), ("gpg.program", "./p.sh"),
                           ("gpg.ssh.program", "./p.sh"), ("credential.helper", "./p.sh"),
                           ("core.pager", "./p.sh"), ("sequence.editor", "./p.sh"), ("core.editor", "./p.sh"),
                           ("include.path", "../planted.config"), ("includeIf.gitdir:/.path", "x")):
            with self.subTest(key=key):
                git(self.target, "config", key, value)
                with self.assertRaisesRegex(installer.InstallRefused, "name programs Git would run"):
                    self.install(apply=True)
                self.assertIn(key.lower().split(".")[0], " ".join(installer.repository_helpers(self.target)).lower())
                git(self.target, "config", "--unset", key)
        self.assertEqual(installer.repository_helpers(self.target), [])
        self.assertEqual(inventory(self.target), before)
        # The owner's own hooks path and the settings Git is told to ignore stay allowed.
        git(self.target, "config", "core.hooksPath", ".githooks")
        self.assertEqual(installer.repository_helpers(self.target), [])

    def test_a_planted_clean_filter_never_runs(self) -> None:
        marker = self.base / "filter-ran"
        script = self.base / "filter.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        script.chmod(0o755)
        git(self.target, "config", "filter.planted.clean", str(script))
        (self.target / ".git" / "info").mkdir(exist_ok=True)
        (self.target / ".git" / "info" / "attributes").write_text("* filter=planted\n")
        with self.assertRaises(installer.InstallRefused):
            self.install(apply=True)
        self.assertFalse(marker.exists())

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
        # An earlier release generated the retired rule; this one no longer wants it.
        with mock.patch.object(installer, "HISTORICAL_OWNER_ONLY",
                               (*installer.HISTORICAL_OWNER_ONLY, "retired-command")):
            self.assertTrue(self.install(apply=True)["ok"])
        self.assertEqual(self.settings()["permissions"]["deny"], installer.deny_rules())

    def test_a_forged_record_never_deletes_a_users_rule(self) -> None:
        settings = self.target / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(curl *)"]}}), encoding="utf-8")
        record = self.target / installer.PERMISSIONS_RECORD
        record.write_text(json.dumps({"format": 1, "created_settings_file": False, "created_permissions": False,
                                      "created_deny": False, "deny": ["Bash(curl *)", "Edit(/src/**)"]}))
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "planted record")
        self.assertTrue(self.install(apply=True)["ok"])
        deny = self.settings()["permissions"]["deny"]
        self.assertEqual(deny[0], "Bash(curl *)")
        owned = json.loads(record.read_text())["deny"]
        self.assertNotIn("Bash(curl *)", owned)
        self.assertNotIn("Edit(/src/**)", owned)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "install packages")
        self.assertTrue(installer.operate("remove", self.target, apply=True)["ok"])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")),
                         {"permissions": {"deny": ["Bash(curl *)"]}})

    def test_historical_generated_rules_count_as_the_installers(self) -> None:
        generated = installer.generated_rules()
        self.assertTrue(set(installer.deny_rules()) <= generated)
        for name in installer.HISTORICAL_OWNER_ONLY:
            self.assertIn(f"Bash(*.codex/orchestration/cli.py {name} *)", generated)
        self.assertNotIn("Bash(curl *)", generated)

    def test_settings_changed_after_planning_are_never_overwritten(self) -> None:
        plan = installer.plan_settings(self.target, "install")
        settings = self.target / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(rm *)"]}}), encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallRefused, "changed while setup ran"):
            installer.apply_settings(self.target, plan)
        self.assertEqual(json.loads(settings.read_text()), {"permissions": {"deny": ["Bash(rm *)"]}})
        self.assertFalse((self.target / installer.PERMISSIONS_RECORD).exists())
        # Applying replans from the current files, so the edit is kept.
        installer.apply_settings(self.target, installer.plan_settings(self.target, "install"))
        self.assertEqual(self.settings()["permissions"]["deny"][0], "Bash(rm *)")

    def test_an_interrupted_paired_write_recovers(self) -> None:
        plan = installer.plan_settings(self.target, "install")
        self.assertEqual(plan["order"], [installer.PERMISSIONS_RECORD, installer.CLAUDE_SETTINGS])
        real_write = installer._Project.write

        def fail_on_settings(project, relative, content):
            if relative == installer.CLAUDE_SETTINGS:
                raise OSError("simulated failure")
            return real_write(project, relative, content)

        with mock.patch.object(installer._Project, "write", fail_on_settings):
            with self.assertRaises(OSError):
                installer.apply_settings(self.target, plan)
        # The record already claims the entries; the next install adds and owns them.
        self.assertTrue((self.target / installer.PERMISSIONS_RECORD).exists())
        self.assertFalse((self.target / ".claude" / "settings.json").exists())
        installer.apply_settings(self.target, installer.plan_settings(self.target, "install"))
        self.assertEqual(self.settings()["permissions"]["deny"], installer.deny_rules())
        removal = installer.plan_settings(self.target, "remove")
        self.assertEqual(removal["order"], [installer.CLAUDE_SETTINGS, installer.PERMISSIONS_RECORD])
        installer.apply_settings(self.target, removal)
        self.assertFalse((self.target / ".claude" / "settings.json").exists())
        self.assertFalse((self.target / installer.PERMISSIONS_RECORD).exists())

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
    """Uninstall takes out the Codex commands rule with this repository's own remove-codex-rule."""

    def setUp(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        self.base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
        self.target, codex_options = make_target(self.base)
        installed = installer.operate("install", self.target, apply=True, codex_options=codex_options)
        self.assertTrue(installed["ok"], installed)
        git(self.target, "add", "-A")
        git(self.target, "commit", "-qm", "install packages")
        self.ran: list[list[str]] = []
        real_run = subprocess.run

        def spy(command, *args, **kwargs):
            self.ran.append([str(part) for part in command])
            return real_run(command, *args, **kwargs)

        patcher = mock.patch.object(installer.subprocess, "run", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def owner_runs(self) -> list[list[str]]:
        return [command for command in self.ran if command[6:7] == [str(installer.attest.clone_cli())]]

    def test_remove_runs_this_checkouts_remove_codex_rule_first(self) -> None:
        rules = self.base / "home" / ".codex" / "rules"
        rules.mkdir(parents=True, mode=0o700)
        digest = hashlib.sha256(str(self.target.resolve()).encode()).hexdigest()[:12]
        rule = rules / f"agent-orchestration-{digest}.rules"
        rule.write_text("# a rule for this project\n")
        rule.chmod(0o600)
        preview = installer.operate("remove", self.target)
        self.assertEqual(preview["codex_rule"], {"action": installer.RULE_COMMAND})
        self.assertTrue(rule.exists())  # a preview never runs it
        self.assertEqual(self.owner_runs(), [])
        removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["codex_rule"]["removed"], [str(rule)])
        self.assertFalse(rule.exists())
        runs = self.owner_runs()
        self.assertEqual([command[7:] for command in runs],
                         [[installer.RULE_COMMAND, "--project-root", str(self.target.absolute())]])
        self.assertEqual(runs[0][1:6], ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"])
        self.assertFalse((self.target / installer.CODEX_MANIFEST).exists())
        project_cli = str(self.target / ".codex" / "orchestration" / "cli.py")
        self.assertFalse(any(project_cli in command for command in self.ran))

    def test_a_failed_rule_removal_changes_nothing_else(self) -> None:
        before = inventory(self.target)
        with mock.patch.object(installer, "_owner_cli", return_value={"ready": False}):
            removed = installer.operate("remove", self.target, apply=True)
        self.assertFalse(removed["ok"])
        self.assertIn("error", removed["codex_rule"])
        self.assertEqual(inventory(self.target), before)

    def test_an_older_or_modified_install_is_still_removed_without_running_its_code(self) -> None:
        manifest = self.target / installer.CODEX_MANIFEST
        data = json.loads(manifest.read_text())
        data["version"] = "0.0.1"
        manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        git(self.target, "commit", "-qam", "an older install")
        removed = installer.operate("remove", self.target, apply=True)
        self.assertTrue(removed["ok"], removed)
        project_cli = str(self.target / ".codex" / "orchestration" / "cli.py")
        self.assertFalse(any(project_cli in command for command in self.ran))
        self.assertEqual(len(self.owner_runs()), 1)

    def test_owner_commands_that_need_the_install_refuse_a_modified_one(self) -> None:
        cli = self.target / ".codex" / "orchestration" / "cli.py"
        cli.write_text(cli.read_text() + "\n# changed\n")
        with self.assertRaisesRegex(installer.InstallRefused, "not an exact copy"):
            installer._owner_cli(self.target, ["host-command", "--project-root", str(self.target)])
        self.assertEqual(self.owner_runs(), [])


def interpreter_trusted() -> bool:
    """Whether the Codex rule may name this Python (CI toolcaches can be world-writable)."""

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from codex_agents.host_commands import interpreter_path; interpreter_path()",
         str(installer.ORCHESTRATION)],
        capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    return completed.returncode == 0


@unittest.skipUnless(interpreter_trusted(), "this machine's Python cannot be named by the Codex rule")
class OwnerCommandsFromThisCheckoutTests(unittest.TestCase):
    """Owner commands run from this checkout's package with --project-root behave as the
    project's own copy would, and install-codex-rule copies exactly this checkout's runtime."""

    def setUp(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        self.base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / "home"
        isolate_home(self, self.home)
        self.target, codex_options = make_target(self.base)
        installed = installer.operate("install", self.target, apply=True, codex_options=codex_options)
        self.assertTrue(installed["ok"], installed)
        git(self.target, "add", "-A")
        git(self.target, "commit", "-qm", "install packages")
        self.installed_cli = self.target / ".codex" / "orchestration" / "cli.py"

    def run_cli(self, cli: Path, *args: str) -> dict:
        completed = subprocess.run([os.path.realpath(sys.executable), "-I", "-B", "-S", "-X",
                                    "pycache_prefix=/dev/null", str(cli), *args],
                                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return json.loads(completed.stdout)

    def test_host_command_and_the_rule_lifecycle_match_and_copy_this_checkout(self) -> None:
        clone = installer.attest.clone_cli()
        root = ["--project-root", str(self.target)]
        before = self.run_cli(clone, "host-command", *root)
        self.assertEqual(before, self.run_cli(self.installed_cli, "host-command", *root))
        self.assertEqual(before["codex_rule"], "missing")
        printed = self.run_cli(clone, "install-codex-rule", *root, "--print")
        self.assertEqual(printed, self.run_cli(self.installed_cli, "install-codex-rule", *root, "--print"))
        written = self.run_cli(clone, "install-codex-rule", *root)
        self.assertEqual(written["codex_rule"], "created", written)
        copy = Path(written["runtime_copy"])
        self.assertTrue(str(copy).startswith(str(self.home / ".codex" / "agent-orchestration")))
        sys.path.insert(0, str(installer.ORCHESTRATION))
        try:
            from codex_agents.runtime_files import RUNTIME_FILES
        finally:
            sys.path.remove(str(installer.ORCHESTRATION))
        for relative in RUNTIME_FILES:
            self.assertEqual((copy / ".codex/orchestration" / relative).read_bytes(),
                             (installer.ORCHESTRATION / relative).read_bytes(), relative)
        after = self.run_cli(clone, "host-command", *root)
        self.assertEqual(after, self.run_cli(self.installed_cli, "host-command", *root))
        self.assertEqual(after["codex_rule"], "installed")
        self.assertEqual(after["argv_prefix"][-1], str(copy / ".codex" / "orchestration" / "cli.py"))
        removed = self.run_cli(clone, "remove-codex-rule", *root)
        self.assertTrue(removed["ready"])
        self.assertIn(written["rule_file"], removed["removed"])
        self.assertFalse(copy.exists())
        self.assertEqual(self.run_cli(clone, "host-command", *root)["codex_rule"], "missing")

    def test_configure_owner_key_writes_the_same_record_from_either_copy(self) -> None:
        key = self.base / "throwaway"
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True, capture_output=True)
        records = []
        for name, cli in (("clone", installer.attest.clone_cli()), ("installed", self.installed_cli)):
            state = self.base / f"state-{name}"
            report = self.run_cli(cli, "learning-configure-owner-key", "--public-key", f"{key}.pub",
                                  "--state-root", str(state))
            self.assertTrue(report["ready"], report)
            records.append((state / "owner-allowed-signers").read_bytes())
        self.assertEqual(records[0], records[1])
        self.assertRegex(records[0].decode(), r"^codex-agents-owner ssh-ed25519 [A-Za-z0-9+/=]+\n$")


if __name__ == "__main__":
    unittest.main()
