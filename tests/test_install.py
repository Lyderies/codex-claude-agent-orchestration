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


class UmbrellaInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.target = self.base / "target"
        self.target.mkdir()
        git(self.target, "init", "-q")
        (self.target / "README.md").write_text("# Target\n", encoding="utf-8")
        qa = self.target / "qa-check"
        qa.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8")
        qa.chmod(0o700)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "initial")
        inputs = self.base / "inputs"
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
        self.codex_options = ["--project-agent-routing", str(routing), "--operational-checks", str(checks)]

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


if __name__ == "__main__":
    unittest.main()
