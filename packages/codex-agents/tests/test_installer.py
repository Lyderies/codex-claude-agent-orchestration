from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import REPOSITORY_ROOT, MACOS_SANDBOX_ONLY, TEMP_ROOT

from codex_agents.learning_store import CollectorClient, CollectorError, LearningState

SPEC = importlib.util.spec_from_file_location(
    "codex_agents_installer", REPOSITORY_ROOT / "scripts" / "install.py"
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def empty_target(self, directory: str) -> Path:
        target = Path(directory) / "target"
        target.mkdir()
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "Installer Tests"],
            check=True,
        )
        (target / "README.md").write_text("# Empty target\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-qm", "initial"], check=True
        )
        return target

    def target(self, directory: str) -> Path:
        target = Path(directory) / "target"
        target.mkdir()
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "Installer Tests"],
            check=True,
        )
        agents = target / ".codex" / "agents"
        agents.mkdir(parents=True)
        for name in ("project_researcher", "project_reviewer"):
            (agents / f"{name}.toml").write_text(
                f'name = "{name}"\n'
                f'description = "{name}"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Return bounded evidence."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-qm", "project agents"],
            check=True,
        )
        return target

    def project_inputs(self, directory: str) -> dict[str, Path]:
        target = Path(directory) / "target"
        qa = target / "qa-check"
        qa.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8")
        qa.chmod(0o700)
        subprocess.run(["git", "-C", str(target), "add", "qa-check"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-qm", "add QA check"],
            check=True,
        )
        inputs = Path(directory) / "inputs"
        inputs.mkdir(exist_ok=True)
        routing = inputs / "project-agent-routing.json"
        routing.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roles": {"research": "project_researcher"},
                    "verifiers": {"research": "project_reviewer"},
                }
            )
        )
        checks = inputs / "operational-checks.json"
        checks.write_text(
            json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [
                        {
                            "check_id": "tests",
                            "driver": "python-wrapper",
                            "argv": ["./qa-check"],
                            "executable_sha256": hashlib.sha256(
                                qa.read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": hashlib.sha256(
                                Path("/usr/bin/python3").read_bytes()
                            ).hexdigest(),
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                    "security_checks": [
                        {
                            "check_id": "security",
                            "driver": "python-wrapper",
                            "argv": ["./qa-check"],
                            "executable_sha256": hashlib.sha256(
                                qa.read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": hashlib.sha256(
                                Path("/usr/bin/python3").read_bytes()
                            ).hexdigest(),
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                }
            )
        )
        return {"project_agent_routing": routing, "operational_checks": checks}

    @MACOS_SANDBOX_ONLY
    def test_install_verify_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            preview = installer.install(target, apply=False, **inputs)
            self.assertFalse((target / installer.MANIFEST_PATH).exists())
            self.assertIn(".codex/agents/codex_agents_researcher.toml", preview["files"])
            installed_report = installer.install(target, apply=True, **inputs)
            self.assertTrue(installed_report["package_ready"])
            self.assertFalse(installed_report["project_ready"])
            self.assertIn("commit", installed_report["required_action"])
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "install package"],
                check=True,
            )
            self.assertTrue(installer.verify(target)["ready"])
            installed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(target / ".codex" / "orchestration" / "cli.py"),
                    "validate",
                    "--root",
                    str(target),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr or installed.stdout)
            self.assertTrue(json.loads(installed.stdout)["ready"])
            installer.remove(target, apply=True)
            self.assertFalse((target / installer.MANIFEST_PATH).exists())
            self.assertTrue((target / installer.PROJECT_AGENT_ROUTING_PATH).exists())
            self.assertTrue((target / installer.OPERATIONAL_CHECKS_PATH).exists())
            self.assertTrue((target / ".git").exists())

    def test_claude_skill_installs_verifies_and_removes_with_the_package(self) -> None:
        skill = ".claude/skills/agent-orchestration/SKILL.md"
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            installer.install(target, apply=True, **inputs)
            manifest = json.loads((target / installer.MANIFEST_PATH).read_text())
            self.assertIn(skill, manifest["files"])
            self.assertEqual(
                [path for path in manifest["files"] if path.startswith(".claude/")],
                [skill],
            )
            self.assertTrue((target / skill).is_file())
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "install package"],
                check=True,
            )
            self.assertTrue(installer.verify(target)["package_ready"])
            (target / skill).write_text("changed\n")
            with self.assertRaisesRegex(installer.InstallError, "refusing to remove"):
                installer.remove(target, apply=True)
            subprocess.run(["git", "-C", str(target), "checkout", "--", skill], check=True)
            installer.remove(target, apply=True)
            self.assertFalse((target / skill).exists())
            self.assertFalse((target / ".claude").exists())

    def test_unmanaged_file_at_the_claude_skill_path_refuses_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            existing = target / ".claude/skills/agent-orchestration/SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("---\nname: agent-orchestration\n---\nProject owned.\n")
            with self.assertRaisesRegex(installer.InstallError, "existing unmanaged file"):
                installer.install(target, apply=True, **self.project_inputs(directory))
            self.assertIn("Project owned", existing.read_text())

    def test_identical_unmanaged_files_are_not_claimed(self) -> None:
        for relative in (
            ".claude/skills/agent-orchestration/SKILL.md",
            ".agents/skills/plan-change/SKILL.md",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                target = self.target(directory)
                existing = target / relative
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes((installer.BUNDLE_ROOT / relative).read_bytes())
                with self.assertRaisesRegex(
                    installer.InstallError, f"existing unmanaged file: {relative}"
                ):
                    installer.install(
                        target, apply=True, **self.project_inputs(directory)
                    )
                self.assertFalse((target / installer.MANIFEST_PATH).exists())

    def test_only_the_claude_skill_prefix_is_admitted_under_claude(self) -> None:
        installer._safe_relative(".claude/skills/agent-orchestration/SKILL.md")
        for path in (
            ".claude/settings.json",
            ".claude/agents/claude_agents_researcher.md",
            ".claude/skills/claude-agents/SKILL.md",
            ".claude/claude-agents/manifest.json",
            ".claude/skills/agent-orchestration",
            ".claude/skills/agent-orchestration-extra/SKILL.md",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                installer.InstallError, "unsupported package path"
            ):
                installer._safe_relative(path)
        installer._safe_relative(".claude/skills", allow_directory=True)
        with self.assertRaises(installer.InstallError):
            installer._safe_relative(".claude/agents", allow_directory=True)

    @MACOS_SANDBOX_ONLY
    def test_clean_project_installs_with_explicit_empty_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.empty_target(directory)
            inputs = self.project_inputs(directory)
            inputs["project_agent_routing"].write_text(
                json.dumps({"version": 1, "roles": {}, "verifiers": {}}),
                encoding="utf-8",
            )

            report = installer.install(target, apply=True, **inputs)

            self.assertTrue(report["package_ready"], report)
            self.assertFalse(report["project_ready"], report)
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "install package"],
                check=True,
            )
            self.assertTrue(installer.verify(target)["ready"])

    @MACOS_SANDBOX_ONLY
    def test_clean_project_mapping_can_select_packaged_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.empty_target(directory)
            inputs = self.project_inputs(directory)
            inputs["project_agent_routing"].write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"planning": "codex_agents_planner"},
                        "verifiers": {},
                    }
                ),
                encoding="utf-8",
            )

            report = installer.install(target, apply=True, **inputs)

            self.assertTrue(report["package_ready"], report)
            self.assertFalse(report["project_ready"], report)
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "install package"],
                check=True,
            )
            self.assertTrue(installer.verify(target)["ready"])

    def test_installed_runtime_creates_registered_project_partition_backup(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            installer.install(target, apply=True, **inputs)
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "install package"],
                check=True,
            )
            state_root = Path(directory) / "state"
            registered = subprocess.run(
                [
                    sys.executable,
                    str(target / ".codex" / "orchestration" / "cli.py"),
                    "learning-register",
                    "--state-root", str(state_root),
                    "--project-root", str(target),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, registered.returncode, registered.stdout)
            try:
                first_backup = subprocess.run(
                    [
                        sys.executable,
                        str(REPOSITORY_ROOT / "scripts" / "backup_installed_project.py"),
                        "--target", str(target),
                        "--state-root", str(state_root),
                        "--file-id", "preupdate_conflict",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, first_backup.returncode, first_backup.stdout)
                failed_backup = subprocess.run(
                    [
                        sys.executable,
                        str(REPOSITORY_ROOT / "scripts" / "backup_installed_project.py"),
                        "--target", str(target),
                        "--state-root", str(state_root),
                        "--file-id", "preupdate_conflict",
                        "--leave-quiesced",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(2, failed_backup.returncode, failed_backup.stdout)
                self.assertEqual(
                    6,
                    CollectorClient(
                        LearningState(state_root, create=False).socket
                    ).health(target)["schema_version"],
                )
                backed_up = subprocess.run(
                    [
                        sys.executable,
                        str(REPOSITORY_ROOT / "scripts" / "backup_installed_project.py"),
                        "--target", str(target),
                        "--state-root", str(state_root),
                        "--file-id", "preupdate_test",
                        "--leave-quiesced",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, backed_up.returncode, backed_up.stdout)
                report = json.loads(backed_up.stdout)
                self.assertTrue(report["ready"])
                self.assertTrue(report["collector_quiesced"])
                with self.assertRaises(CollectorError):
                    CollectorClient(LearningState(state_root, create=False).socket).health()
                payload = json.loads(Path(report["path"]).read_bytes())
                self.assertEqual(2, payload["body"]["schema_version"])
                self.assertEqual(
                    str(target.resolve()),
                    payload["body"]["project"]["canonical_root"],
                )
                self.assertIn(
                    "handle_digest",
                    payload["body"]["columns"]["host_run_bindings"],
                )
                restarted = subprocess.run(
                    [
                        sys.executable,
                        str(target / ".codex" / "orchestration" / "cli.py"),
                        "learning-register",
                        "--state-root", str(state_root),
                        "--project-root", str(target),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, restarted.returncode, restarted.stdout)
                verified = subprocess.run(
                    [
                        sys.executable,
                        str(target / ".codex" / "orchestration" / "cli.py"),
                        "learning-verify-backup",
                        "--state-root", str(state_root),
                        "--project-root", str(target),
                        "--file-id", "preupdate_test",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, verified.returncode, verified.stdout)
                self.assertTrue(json.loads(verified.stdout)["verified"])
            finally:
                state = LearningState(state_root, create=False)
                try:
                    CollectorClient(state.socket).shutdown()
                except CollectorError:
                    pass

    def test_remove_preserves_unmanifested_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            installer.install(target, apply=True, **self.project_inputs(directory))
            cache = target / ".codex" / "orchestration" / "codex_agents" / "__pycache__"
            cache.mkdir()
            managed = cache / "routing.cpython-999.pyc"
            unknown = cache / "owner.cpython-999.pyc"
            managed.write_bytes(b"managed bytecode")
            unknown.write_bytes(b"owner bytecode")
            report = installer.remove(target, apply=True)
            self.assertNotIn("generated_files", report)
            self.assertTrue(managed.exists())
            self.assertTrue(unknown.exists())

    def test_modified_file_blocks_remove_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            installer.install(target, apply=True, **inputs)
            agent = target / ".codex" / "agents" / "codex_agents_researcher.toml"
            agent.write_text(agent.read_text() + "\n# local change\n")
            with self.assertRaisesRegex(installer.InstallError, "modified installed file"):
                installer.install(target, apply=True, update=True, **inputs)
            with self.assertRaisesRegex(installer.InstallError, "refusing to remove"):
                installer.remove(target, apply=True)

    def test_unmanaged_conflict_blocks_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            agent = target / ".codex" / "hooks.json"
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text("owner content")
            with self.assertRaisesRegex(installer.InstallError, "existing unmanaged file"):
                installer.install(target, apply=True, **inputs)

    def test_manifest_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            manifest = target / installer.MANIFEST_PATH
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"format": 1, "version": "0", "files": {"../../outside": "0" * 64}})
            )
            with self.assertRaisesRegex(installer.InstallError, "unsafe manifest path"):
                installer.verify(target)

    def test_manifest_cannot_claim_git_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            manifest = target / installer.MANIFEST_PATH
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {"format": 1, "version": "0", "files": {".git/config": "0" * 64}}
                )
            )
            with self.assertRaisesRegex(installer.InstallError, "unsupported package path"):
                installer.verify(target)

    def test_symlinked_install_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (target / ".agents").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "symlinked install path"):
                installer.install(
                    target, apply=False, **self.project_inputs(directory)
                )

    def test_dangling_manifest_symlink_is_rejected_without_outside_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            manifest = target / installer.MANIFEST_PATH
            manifest.parent.mkdir(parents=True)
            outside = Path(directory) / "outside.json"
            manifest.symlink_to(outside)
            with self.assertRaisesRegex(installer.InstallError, "symlinked install path"):
                installer.install(
                    target, apply=True, **self.project_inputs(directory)
                )
            self.assertFalse(outside.exists())

    def test_manifest_parent_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (target / ".codex" / "orchestration").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(installer.InstallError, "symlinked install path"):
                installer.install(target, apply=True)
            self.assertEqual([], list(outside.iterdir()))

    def test_managed_write_stays_with_opened_parent_during_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            managed_parent = target / ".agents"
            managed_parent.mkdir()
            detached = target / ".agents-detached"
            outside = Path(directory) / "outside"
            outside.mkdir()
            original = installer._open_parent_fd

            def swapped_parent(*args, **kwargs):
                descriptor, name = original(*args, **kwargs)
                managed_parent.rename(detached)
                managed_parent.symlink_to(outside, target_is_directory=True)
                return descriptor, name

            with patch.object(installer, "_open_parent_fd", side_effect=swapped_parent):
                installer._atomic_write_managed(
                    target, ".agents/test.txt", b"managed", mode=0o600
                )
            self.assertEqual(b"managed", (detached / "test.txt").read_bytes())
            self.assertFalse((outside / "test.txt").exists())

    def test_managed_unlink_stays_with_opened_parent_during_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            managed_parent = target / ".agents"
            managed_parent.mkdir()
            managed_file = managed_parent / "test.txt"
            managed_file.write_bytes(b"managed")
            expected = installer._digest(managed_file)
            detached = target / ".agents-detached"
            outside = Path(directory) / "outside"
            outside.mkdir()
            outside_file = outside / "test.txt"
            outside_file.write_bytes(b"owner")
            original = installer._open_parent_fd

            def swapped_parent(*args, **kwargs):
                descriptor, name = original(*args, **kwargs)
                managed_parent.rename(detached)
                managed_parent.symlink_to(outside, target_is_directory=True)
                return descriptor, name

            with patch.object(installer, "_open_parent_fd", side_effect=swapped_parent):
                installer._unlink_managed(
                    target, ".agents/test.txt", expected_digest=expected
                )
            self.assertFalse((detached / "test.txt").exists())
            self.assertEqual(b"owner", outside_file.read_bytes())

    def test_install_retains_pinned_root_during_root_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            detached = Path(directory) / "target-detached"
            outside = Path(directory) / "outside"
            outside.mkdir()
            original = installer._plan_install

            def swap_after_plan(*args, **kwargs):
                plan = original(*args, **kwargs)
                target.rename(detached)
                target.symlink_to(outside, target_is_directory=True)
                return plan

            with patch.object(installer, "_plan_install", side_effect=swap_after_plan):
                with self.assertRaises(installer.InstallError):
                    installer.install(target, apply=True, **inputs)
            self.assertTrue((detached / installer.MANIFEST_PATH).is_file())
            self.assertFalse((outside / installer.MANIFEST_PATH).exists())

    def test_missing_project_inputs_fail_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            with self.assertRaisesRegex(
                installer.InstallError, "required project agent routing configuration"
            ):
                installer.install(target, apply=True)
            self.assertFalse((target / installer.MANIFEST_PATH).exists())

    def test_reinstall_preserves_project_owned_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            inputs = self.project_inputs(directory)
            installer.install(target, apply=True, **inputs)
            routing = target / installer.PROJECT_AGENT_ROUTING_PATH
            checks = target / installer.OPERATIONAL_CHECKS_PATH
            before = (routing.read_bytes(), checks.read_bytes())

            installer.install(target, apply=True, update=True)

            self.assertEqual(before, (routing.read_bytes(), checks.read_bytes()))
            changed = Path(directory) / "changed-routing.json"
            changed.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"research": "project_reviewer"},
                        "verifiers": {"research": "project_researcher"},
                    }
                )
            )
            with self.assertRaisesRegex(
                installer.InstallError, "existing project-owned configuration differs"
            ):
                installer.install(
                    target,
                    apply=True,
                    update=True,
                    project_agent_routing=changed,
                )


if __name__ == "__main__":
    unittest.main()
