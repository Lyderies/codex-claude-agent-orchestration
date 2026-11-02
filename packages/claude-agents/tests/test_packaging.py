from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_agents import packaging
from claude_agents.packaging import PackageError, compile_bundle, package_operation

SOURCE = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.target = self.base / "target"
        subprocess.run(
            ["/usr/bin/git", "init", "--quiet", "--template=", str(self.target)],
            check=True, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
                             "GIT_CONFIG_GLOBAL": "/dev/null"},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def op(self, operation="install", apply=False, source=SOURCE, previous=None):
        return package_operation(source, self.target, operation, apply=apply,
                                 previous_source_root=previous)

    def inventory(self):
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in self.target.rglob("*") if p.is_file() and not p.is_symlink()}

    def cloned_source(self):
        clone = self.base / "new-source"
        for relative in ("pyproject.toml", "src/claude_agents/__init__.py",
                         "src/claude_agents/contracts.py", "src/claude_agents/validation.py"):
            destination = clone / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
        shutil.copytree(SOURCE / "tests/fixtures/native", clone / "tests/fixtures/native")
        with (clone / "src/claude_agents/__init__.py").open("a") as stream:
            stream.write("\n# Synthetic package revision.\n")
        return clone

    def test_bundle_is_deterministic_namespaced_and_provider_disabled(self) -> None:
        bundle = compile_bundle(SOURCE)
        self.assertEqual(bundle, compile_bundle(SOURCE))
        manifest = json.loads(bundle.manifest)
        self.assertIs(manifest["provider_execution_enabled"], False)
        self.assertEqual(len(bundle.files), 9)
        for path, content in bundle.files:
            self.assertTrue(path.startswith(".claude/"))
            self.assertNotIn("settings", path)
            self.assertNotIn("hooks", path)
            if path.startswith(".claude/agents"):
                self.assertIn(b"maxTurns: 8", content)
                self.assertNotIn(b"Inert fixture only.", content)
                self.assertIn(b"provides no model isolation", content)

    def test_preview_install_verify_update_rollback_and_idempotence(self) -> None:
        before = self.inventory()
        self.assertEqual(self.op()["result"], "preview")
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.op(apply=True)["result"], "applied")
        self.assertEqual(self.op("verify")["result"], "verified")
        self.assertEqual(self.op(apply=True)["result"], "unchanged")
        new = self.cloned_source()
        with self.assertRaisesRegex(PackageError, "untrusted_manifest"):
            self.op("update", apply=True, source=new)
        self.op("update", apply=True, source=new, previous=SOURCE)
        self.op("verify", source=new)
        self.op("update", apply=True, source=SOURCE, previous=new)
        self.op("verify")

    def test_exact_remove_preserves_codex_user_state_and_empty_directories(self) -> None:
        sentinels = {
            ".codex/agents/user.toml": b"codex",
            ".agents/skills/user/SKILL.md": b"user",
            ".claude/settings.local.json": b"{}",
            ".claude/agents/user.md": b"owner role",
            "AGENTS.md": b"owner instructions",
            "CLAUDE.md": b"owner instructions",
        }
        for path, content in sentinels.items():
            target = self.target / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.op(apply=True)
        self.op("remove", apply=True)
        for path, content in sentinels.items():
            self.assertEqual((self.target / path).read_bytes(), content)
        self.assertFalse((self.target / packaging.MANIFEST).exists())
        self.assertTrue((self.target / ".claude/claude-agents/lib").is_dir())
        self.assertEqual(self.op("remove", apply=True)["result"], "unchanged")

    def test_collision_and_modified_removal_refuse_before_any_payload_change(self) -> None:
        path = self.target / ".claude/agents/claude_agents_researcher.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unmanaged")
        before = self.inventory()
        with self.assertRaisesRegex(PackageError, "unmanaged_conflict"):
            self.op(apply=True)
        self.assertEqual(self.inventory(), before)
        path.unlink()
        self.op(apply=True)
        modified = self.target / ".claude/agents/claude_agents_verification_worker.md"
        modified.write_bytes(b"local changes")
        before = self.inventory()
        with self.assertRaisesRegex(PackageError, "managed_file_modified"):
            self.op("remove", apply=True)
        self.assertEqual(self.inventory(), before)

    def test_manifest_forgery_and_pending_marker_fail_closed(self) -> None:
        self.op(apply=True)
        manifest_path = self.target / packaging.MANIFEST
        original = manifest_path.read_bytes()
        manifest = json.loads(original)
        manifest["files"]["../../owner"] = "a" * 64
        manifest_path.write_text(json.dumps(manifest))
        before = self.inventory()
        with self.assertRaisesRegex(PackageError, "untrusted_manifest"):
            self.op("remove", apply=True)
        self.assertEqual(self.inventory(), before)
        manifest_path.write_bytes(original)
        (self.target / packaging.PENDING).write_bytes(b"interrupted operation")
        with self.assertRaisesRegex(PackageError, "installation_pending"):
            self.op("verify")
        with self.assertRaisesRegex(PackageError, "installation_pending"):
            self.op("remove", apply=True)

    def test_symlinked_components_and_managed_leaf_are_refused(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (self.target / ".claude").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PackageError):
            self.op(apply=True)
        self.assertEqual(list(outside.iterdir()), [])
        (self.target / ".claude").unlink()
        self.op(apply=True)
        leaf = self.target / ".claude/agents/claude_agents_researcher.md"
        leaf.unlink()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"unchanged")
        leaf.symlink_to(sentinel)
        with self.assertRaises(PackageError):
            self.op("remove", apply=True)
        self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_synchronous_failure_rolls_back_known_payload_and_releases_controls(self) -> None:
        original = packaging._set_file
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic failure")
            return original(*args)
        before = self.inventory()
        with patch.object(packaging, "_set_file", side_effect=fail_second):
            with self.assertRaisesRegex(PackageError, "operation_failed"):
                self.op(apply=True)
        self.assertEqual(self.inventory(), before)
        self.op(apply=True)
        self.op("verify")

    def test_rollback_preserves_unknown_user_change_and_leaves_pending_marker(self) -> None:
        original = packaging._set_file
        calls = 0
        changed = None
        def fail_and_change(root, relative, before, after):
            nonlocal calls, changed
            calls += 1
            if calls == 1:
                changed = self.target / relative
            if calls == 2:
                changed.write_bytes(b"concurrent user change")
                raise OSError("synthetic failure")
            return original(root, relative, before, after)
        with patch.object(packaging, "_set_file", side_effect=fail_and_change):
            with self.assertRaisesRegex(PackageError, "rollback_pending"):
                self.op(apply=True)
        self.assertEqual(changed.read_bytes(), b"concurrent user change")
        self.assertTrue((self.target / packaging.PENDING).is_file())
        with self.assertRaisesRegex(PackageError, "installation_pending"):
            self.op("verify")

    def test_non_git_target_is_refused_without_creating_package_paths(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(PackageError, "git_target_required"):
            package_operation(SOURCE, plain, "install", apply=True)
        self.assertEqual(list(plain.iterdir()), [])

    def test_linked_worktree_target_installs_verifies_and_removes(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        (self.target / "README").write_text("x")
        identity = ["-c", "user.name=Tests", "-c", "user.email=tests@example.invalid"]
        for args in (["add", "README"], [*identity, "commit", "-qm", "init"]):
            subprocess.run(["/usr/bin/git", "-C", str(self.target), *args], check=True, env=env)
        linked = self.base / "linked"
        subprocess.run(["/usr/bin/git", "-C", str(self.target), "worktree", "add", "-q", str(linked)],
                       check=True, env=env)
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(len(package_operation(SOURCE, linked, "install")["changed_paths"]), 10)
        package_operation(SOURCE, linked, "install", apply=True)
        self.assertEqual(package_operation(SOURCE, linked, "verify")["result"], "verified")
        package_operation(SOURCE, linked, "remove", apply=True)
        self.assertFalse((linked / ".claude/claude-agents/install-manifest.json").exists())

    def test_malformed_or_symlinked_git_marker_is_refused(self) -> None:
        malformed = self.base / "malformed"
        malformed.mkdir()
        (malformed / ".git").write_bytes(b"not a git pointer\n")
        dangling = self.base / "dangling"
        dangling.mkdir()
        (dangling / ".git").write_bytes(b"gitdir: " + str(self.base / "missing").encode() + b"\n")
        linked = self.base / "symlinked"
        linked.mkdir()
        (linked / ".git").symlink_to(self.target / ".git")
        for target in (malformed, dangling, linked):
            with self.subTest(target=target.name):
                with self.assertRaisesRegex(PackageError, "git_target_required"):
                    package_operation(SOURCE, target, "install", apply=True)
                self.assertFalse((target / ".claude").exists())

    def test_fifo_collision_refuses_promptly_without_modifying_target(self) -> None:
        fifo = self.target / ".claude/agents/claude_agents_researcher.md"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        code = (
            "from pathlib import Path\n"
            "import sys\n"
            "from claude_agents.packaging import PackageError,package_operation\n"
            "try:\n"
            " package_operation(Path(sys.argv[1]),Path(sys.argv[2]),'install',"
            "apply=sys.argv[3]=='apply')\n"
            "except PackageError as error:\n"
            " print(str(error))\n"
            "else:\n"
            " raise SystemExit(10)\n"
        )
        for mode in ("preview", "apply"):
            result = subprocess.run(
                [sys.executable, "-c", code, str(SOURCE), str(self.target), mode],
                cwd=self.base, capture_output=True, text=True, timeout=3,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SOURCE / "src"),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "unsafe_file")
            self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))
            self.assertFalse((self.target / packaging.MANIFEST).exists())
            self.assertFalse((self.target / packaging.PENDING).exists())
