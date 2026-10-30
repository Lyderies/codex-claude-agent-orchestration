from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import base64
import copy
import hashlib
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import bridge, workspace
from codex_agents.workspace import WorkspaceRefused


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def write(path: Path, content: bytes | str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    if mode is not None:
        os.chmod(path, mode)


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        git(self.project, "init", "-q")
        git(self.project, "config", "user.email", "tests@example.invalid")
        git(self.project, "config", "user.name", "Workspace Tests")
        write(self.project / "src" / "app.py", "print('app')\n")
        write(self.project / "src" / "run.sh", "#!/bin/sh\necho run\n", 0o755)
        write(self.project / "src" / "nested" / "deep.txt", "deep\n")
        write(self.project / "docs" / "guide.md", "guide\n")
        write(self.project / "src" / "AGENTS.md", "agent notes\n")
        write(self.project / "src" / "CLAUDE.md", "claude notes\n")
        self.commit("initial")
        self.runs = self.base / "runs"
        self.runs.mkdir()

    def commit(self, message: str) -> str:
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", message)
        return git(self.project, "rev-parse", "HEAD")

    def export(self, read=("src", "docs"), write_set=("src",)):
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        manifest = workspace.export_workspace(self.project, layout, read, write_set)
        return layout, manifest

    def collect(self, layout, manifest):
        workspace.snapshot_workspace(layout)
        return workspace.collect_patch(layout, manifest)

    def assertRefused(self, reason: str, function, *arguments, **keywords):
        with self.assertRaises(WorkspaceRefused) as caught:
            function(*arguments, **keywords)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception


class WriteSetTest(unittest.TestCase):
    def assignment(self, owned, filesystem):
        return {"owned_paths": owned, "permissions": {"filesystem": filesystem}}

    def test_intersection_is_by_component(self) -> None:
        result = workspace.effective_write_set(self.assignment(
            ["src/a.py", "lib", "tools/x.py", "srcx/y.py"],
            ["src:write", "lib/core:write", "tools:read", "src2:write"],
        ))
        self.assertEqual(result, ["lib/core", "src/a.py"])

    def test_covered_paths_collapse(self) -> None:
        result = workspace.effective_write_set(self.assignment(["src", "src/a.py"], ["src:write"]))
        self.assertEqual(result, ["src"])

    def test_protected_write_path_refuses(self) -> None:
        for owned in ("pkg/.Codex/x.py", "pkg/agents.MD", "a/.GIT/config", "CLAUDE.local.md"):
            with self.subTest(owned=owned), self.assertRaises(WorkspaceRefused) as caught:
                workspace.effective_write_set(self.assignment([owned], [owned + ":write"]))
            self.assertEqual(caught.exception.reason, "write_path_protected")

    def test_non_literal_paths_refuse(self) -> None:
        for owned in ("../x", "/abs", "src/*.py", ":(glob)src", "a//b", "a/./b"):
            with self.subTest(owned=owned), self.assertRaises(WorkspaceRefused):
                workspace.effective_write_set(self.assignment([owned], ["src:write"]))

    def test_export_paths_include_write_set(self) -> None:
        assignment = {
            "context": ["docs"], "owned_paths": ["src/new.py"],
            "permissions": {"filesystem": ["docs:read", "src:write"]},
        }
        self.assertEqual(workspace.assignment_export_paths(assignment), ["docs", "src/new.py"])

    def test_protected_names_at_depth_and_case(self) -> None:
        for path in (
            "a/b/.git", "a/.Git/x", "x/.CODEX/y", "deep/er/.agents/z", "q/.claude/s",
            "r/Agents.md", "r/claude.MD", "r/Claude.Local.md", "r/.MCP.json", "r/.g‍it/x",
        ):
            with self.subTest(path=path):
                self.assertTrue(workspace.is_protected_path(path))
        self.assertFalse(workspace.is_protected_path("src/gitignore.md"))
        self.assertTrue(set(bridge.CLAUDE_CONTEXT_NAMES) <= workspace.PROTECTED_NAMES)


class ExportTest(WorkspaceTestCase):
    def test_export_layout_and_manifest(self) -> None:
        layout, manifest = self.export()
        self.assertEqual(layout.workspace.parent, layout.control.parent)
        self.assertEqual(stat.S_IMODE(os.stat(layout.control).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(layout.root).st_mode), 0o700)
        self.assertFalse((layout.workspace / ".git").exists())
        self.assertEqual(manifest["source_commit"], git(self.project, "rev-parse", "HEAD"))
        self.assertEqual(
            sorted(manifest["files"]),
            ["docs/guide.md", "src/AGENTS.md", "src/app.py", "src/nested/deep.txt", "src/run.sh"],
        )
        self.assertFalse((layout.workspace / "src" / "CLAUDE.md").exists())
        entry = manifest["files"]["src/app.py"]
        self.assertEqual(entry["blob"], git(self.project, "rev-parse", "HEAD:src/app.py"))
        self.assertEqual(entry["size"], len(b"print('app')\n"))
        self.assertEqual(workspace.read_manifest(layout), manifest)
        self.assertEqual(stat.S_IMODE(os.stat(layout.manifest_path).st_mode), 0o600)

    def test_modes_are_preserved(self) -> None:
        layout, manifest = self.export()
        self.assertEqual(manifest["files"]["src/run.sh"]["mode"], "100755")
        self.assertEqual(manifest["files"]["src/app.py"]["mode"], "100644")
        self.assertTrue(os.stat(layout.workspace / "src" / "run.sh").st_mode & stat.S_IXUSR)
        self.assertFalse(os.stat(layout.workspace / "src" / "app.py").st_mode & stat.S_IXUSR)
        # Read-only paths and protected names are not exported writable.
        self.assertFalse(os.stat(layout.workspace / "docs" / "guide.md").st_mode & 0o222)
        self.assertFalse(os.stat(layout.workspace / "src" / "AGENTS.md").st_mode & 0o222)
        self.assertTrue(os.stat(layout.workspace / "src" / "app.py").st_mode & stat.S_IWUSR)

    def test_export_reads_one_commit_even_if_a_commit_lands_between_paths(self) -> None:
        first = git(self.project, "rev-parse", "HEAD")
        original = bridge._git
        landed = []

        def racing(root, *arguments, text=False):
            result = original(root, *arguments, text=text)
            if "ls-tree" in arguments and not landed:
                write(self.project / "src" / "app.py", "print('moved')\n")
                write(self.project / "docs" / "guide.md", "moved\n")
                landed.append(self.commit("moved"))
            return result

        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        with mock.patch.object(bridge, "_git", side_effect=racing):
            manifest = workspace.export_workspace(self.project, layout, ["docs", "src"], ["src"])
        self.assertTrue(landed)
        self.assertEqual(manifest["source_commit"], first)
        self.assertEqual((layout.workspace / "src" / "app.py").read_text(), "print('app')\n")
        self.assertEqual((layout.workspace / "docs" / "guide.md").read_text(), "guide\n")

    def test_symlink_is_refused(self) -> None:
        os.symlink("app.py", self.project / "src" / "link.py")
        self.commit("link")
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        self.assertRefused("export_symlink", workspace.export_workspace, self.project, layout, ["src"], [])

    def test_submodule_is_refused(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-index", "--add", "--cacheinfo", f"160000,{commit},src/sub")
        git(self.project, "commit", "-qm", "submodule")
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        self.assertRefused("export_submodule", workspace.export_workspace, self.project, layout, ["src"], [])

    def test_missing_read_path_refuses_but_missing_write_path_is_allowed(self) -> None:
        layout, manifest = self.export(read=("src",), write_set=("new/module.py",))
        self.assertEqual(manifest["write_set"], ["new/module.py"])
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        self.assertRefused(
            "path_not_committed", workspace.export_workspace, self.project, layout, ["absent"], []
        )

    def test_uncommitted_bytes_are_never_exported(self) -> None:
        write(self.project / "src" / "app.py", "dirty\n")
        layout, _ = self.export()
        self.assertEqual((layout.workspace / "src" / "app.py").read_text(), "print('app')\n")

    def test_filters_and_hooks_do_not_run(self) -> None:
        marker = self.base / "filter-ran"
        write(self.project / ".gitattributes", "*.py filter=evil\n")
        self.commit("attributes")
        git(self.project, "config", "filter.evil.smudge", f"touch {marker}; cat")
        git(self.project, "config", "core.fsmonitor", f"touch {marker}; false")
        layout, _ = self.export()
        self.assertFalse(marker.exists())
        self.assertEqual((layout.workspace / "src" / "app.py").read_text(), "print('app')\n")

    def test_protected_write_set_refuses_export(self) -> None:
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout)
        self.assertRefused(
            "write_path_protected", workspace.export_workspace, self.project, layout, ["src"],
            ["src/AGENTS.md"],
        )


class OwnershipTest(WorkspaceTestCase):
    def test_no_change_yields_empty_patch(self) -> None:
        layout, manifest = self.export()
        patch = self.collect(layout, manifest)
        self.assertEqual(patch["changes"], [])
        self.assertEqual(patch["manifest_digest"], workspace.manifest_digest(manifest))

    def test_owned_changes_are_accepted(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / "app.py", "print('changed')\n")
        write(layout.workspace / "src" / "pkg" / "new.py", "x = 1\n")
        os.unlink(layout.workspace / "src" / "nested" / "deep.txt")
        os.chmod(layout.workspace / "src" / "app.py", 0o755)
        patch = self.collect(layout, manifest)
        ops = {change["path"]: (change["op"], change["mode"]) for change in patch["changes"]}
        self.assertEqual(ops, {
            "src/app.py": ("modify", "100755"),
            "src/nested/deep.txt": ("delete", "100644"),
            "src/pkg/new.py": ("add", "100644"),
        })

    def test_change_outside_write_set(self) -> None:
        layout, manifest = self.export(write_set=("src/app.py",))
        os.chmod(layout.workspace / "docs" / "guide.md", 0o644)
        write(layout.workspace / "docs" / "guide.md", "edited\n")
        workspace.snapshot_workspace(layout)
        self.assertRefused("outside_write_set", workspace.collect_patch, layout, manifest)

    def test_add_and_delete_outside_write_set(self) -> None:
        layout, manifest = self.export(write_set=("src/app.py",))
        write(layout.workspace / "src" / "other.py", "x\n")
        workspace.snapshot_workspace(layout)
        self.assertRefused("outside_write_set", workspace.collect_patch, layout, manifest)
        layout, manifest = self.export(write_set=("src/app.py",))
        os.unlink(layout.workspace / "src" / "run.sh")
        workspace.snapshot_workspace(layout)
        self.assertRefused("outside_write_set", workspace.collect_patch, layout, manifest)

    def test_protected_names_refused_at_depth_and_case(self) -> None:
        for name in ("pkg/.Git/config", "a/b/Claude.md", "x/.CoDeX/config.toml", "deeper/agents.MD", ".mcp.JSON"):
            with self.subTest(name=name):
                layout, manifest = self.export(write_set=("src",))
                write(layout.workspace / "src" / name, "x\n")
                workspace.snapshot_workspace(layout)
                self.assertRefused("protected_name", workspace.collect_patch, layout, manifest)

    def test_changed_protected_baseline_file_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.chmod(layout.workspace / "src" / "AGENTS.md", 0o644)
        write(layout.workspace / "src" / "AGENTS.md", "rewritten\n")
        workspace.snapshot_workspace(layout)
        self.assertRefused("protected_name", workspace.collect_patch, layout, manifest)

    def test_empty_protected_directory_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        (layout.workspace / "src" / ".git").mkdir()
        workspace.snapshot_workspace(layout)
        self.assertRefused("protected_name", workspace.collect_patch, layout, manifest)

    def test_unneeded_directory_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        (layout.workspace / "src" / "empty").mkdir()
        workspace.snapshot_workspace(layout)
        self.assertRefused("unneeded_directory", workspace.collect_patch, layout, manifest)

    def test_directory_needed_for_owned_file_is_allowed(self) -> None:
        layout, manifest = self.export(read=("src",), write_set=("new/deep/module.py",))
        write(layout.workspace / "new" / "deep" / "module.py", "x = 1\n")
        patch = self.collect(layout, manifest)
        self.assertEqual([change["path"] for change in patch["changes"]], ["new/deep/module.py"])

    def test_symlink_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.symlink("/etc/passwd", layout.workspace / "src" / "link")
        workspace.snapshot_workspace(layout)
        self.assertRefused("symlink_entry", workspace.collect_patch, layout, manifest)

    def test_symlink_replacing_owned_file_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.unlink(layout.workspace / "src" / "app.py")
        os.symlink("run.sh", layout.workspace / "src" / "app.py")
        workspace.snapshot_workspace(layout)
        self.assertRefused("symlink_entry", workspace.collect_patch, layout, manifest)

    def test_hard_link_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.link(layout.workspace / "src" / "app.py", layout.workspace / "src" / "alias.py")
        workspace.snapshot_workspace(layout)
        self.assertRefused("hard_link", workspace.collect_patch, layout, manifest)

    def test_hard_link_to_outside_file_is_refused(self) -> None:
        outside = self.base / "outside.txt"
        write(outside, "secret\n")
        layout, manifest = self.export(write_set=("src",))
        os.link(outside, layout.workspace / "src" / "copy.txt")
        workspace.snapshot_workspace(layout)
        self.assertRefused("hard_link", workspace.collect_patch, layout, manifest)

    def test_fifo_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.mkfifo(layout.workspace / "src" / "pipe")
        workspace.snapshot_workspace(layout)
        self.assertFalse((layout.snapshot / "src" / "pipe").exists())
        self.assertRefused("special_entry", workspace.collect_patch, layout, manifest)

    def test_socket_is_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        # A relative bind keeps the socket path under the platform's length limit.
        previous = os.getcwd()
        os.chdir(layout.workspace / "src")
        try:
            server.bind("sock")
        finally:
            os.chdir(previous)
        workspace.snapshot_workspace(layout)
        self.assertRefused("special_entry", workspace.collect_patch, layout, manifest)

    def test_mode_change_outside_write_set(self) -> None:
        layout, manifest = self.export(write_set=("src/app.py",))
        os.chmod(layout.workspace / "src" / "run.sh", 0o444)
        workspace.snapshot_workspace(layout)
        self.assertRefused("mode_change", workspace.collect_patch, layout, manifest)

    def test_special_mode_bits_are_refused(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.chmod(layout.workspace / "src" / "app.py", 0o644 | stat.S_ISGID)
        if not os.stat(layout.workspace / "src" / "app.py").st_mode & stat.S_ISGID:
            os.chmod(layout.workspace / "src" / "app.py", 0o644 | stat.S_ISVTX)
        if not os.stat(layout.workspace / "src" / "app.py").st_mode & workspace.SPECIAL_MODE_BITS:
            self.skipTest("filesystem drops special mode bits")
        workspace.snapshot_workspace(layout)
        self.assertRefused("mode_change", workspace.collect_patch, layout, manifest)

    def test_file_size_cap(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / "big.bin", b"x" * (workspace.MAX_CHANGED_FILE_BYTES + 1))
        workspace.snapshot_workspace(layout)
        self.assertRefused("file_too_large", workspace.collect_patch, layout, manifest)

    def test_changed_file_count_cap(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        for index in range(workspace.MAX_CHANGED_FILES + 1):
            write(layout.workspace / "src" / "many" / f"f{index}.txt", f"{index}\n")
        workspace.snapshot_workspace(layout)
        self.assertRefused("too_many_changes", workspace.collect_patch, layout, manifest)

    def test_patch_size_cap(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        for index in range(5):
            write(layout.workspace / "src" / f"chunk{index}.bin", os.urandom(workspace.MAX_CHANGED_FILE_BYTES))
        workspace.snapshot_workspace(layout)
        self.assertRefused("patch_too_large", workspace.collect_patch, layout, manifest)

    def test_inventory_entry_bound(self) -> None:
        layout, _ = self.export(write_set=("src",))
        for index in range(5):
            write(layout.workspace / "src" / "x" / f"{index}", "x")
        self.assertRefused("inventory_too_many_entries", workspace.snapshot_workspace, layout, max_entries=8)

    def test_inventory_depth_bound(self) -> None:
        layout, _ = self.export(write_set=("src",))
        write(layout.workspace / "src" / Path(*["d"] * workspace.MAX_INVENTORY_DEPTH) / "f", "x")
        self.assertRefused("inventory_too_deep", workspace.snapshot_workspace, layout)

    def test_inventory_byte_bound(self) -> None:
        layout, _ = self.export(write_set=("src",))
        write(layout.workspace / "src" / "big.bin", b"x" * 4096)
        self.assertRefused("inventory_too_large", workspace.snapshot_workspace, layout, max_bytes=1024)
        layout, _ = self.export(write_set=("src",))
        workspace.snapshot_workspace(layout)
        self.assertRefused("inventory_too_large", workspace.inventory_snapshot, layout, max_bytes=8)


class SnapshotTest(WorkspaceTestCase):
    def test_writes_after_snapshot_do_not_change_inventory(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / "app.py", "print('child')\n")
        workspace.snapshot_workspace(layout)
        before = workspace.inventory_snapshot(layout)
        write(layout.workspace / "src" / "app.py", "print('survivor')\n")
        write(layout.workspace / "src" / "late.py", "late\n")
        os.symlink("/etc", layout.workspace / "src" / "late-link")
        self.assertEqual(workspace.inventory_snapshot(layout), before)
        patch = workspace.collect_patch(layout, manifest)
        content = base64.b64decode(patch["changes"][0]["content_b64"])
        self.assertEqual(content, b"print('child')\n")

    def test_portable_copy_matches_clone(self) -> None:
        inventories = []
        for clone in (workspace._CLONEFILEAT, None):
            layout, _ = self.export(write_set=("src",))
            write(layout.workspace / "src" / "app.py", "print('child')\n")
            os.chmod(layout.workspace / "src" / "app.py", 0o755)
            os.mkfifo(layout.workspace / "src" / "pipe")
            with mock.patch.object(workspace, "_CLONEFILEAT", clone):
                workspace.snapshot_workspace(layout)
            inventories.append(workspace.inventory_snapshot(layout))
        self.assertEqual(inventories[0], inventories[1])
        self.assertEqual(inventories[0]["src/pipe"]["type"], "fifo")
        self.assertEqual(inventories[0]["src/app.py"]["mode"] & 0o777, 0o755)

    def test_snapshot_does_not_follow_symlinked_directories(self) -> None:
        outside = self.base / "outside"
        write(outside / "secret.txt", "secret\n")
        layout, _ = self.export(write_set=("src",))
        os.symlink(outside, layout.workspace / "src" / "escape")
        workspace.snapshot_workspace(layout)
        inventory = workspace.inventory_snapshot(layout)
        self.assertEqual(inventory["src/escape"]["type"], "symlink")
        self.assertNotIn("src/escape/secret.txt", inventory)
        self.assertFalse(os.path.lexists(layout.snapshot / "src" / "escape"))

    def test_replaced_workspace_symlink_is_refused(self) -> None:
        layout, _ = self.export(write_set=("src",))
        workspace._remove_path(layout.workspace)
        os.symlink(self.project, layout.workspace)
        self.assertRefused("workspace_not_directory", workspace.snapshot_workspace, layout)


class PatchTest(WorkspaceTestCase):
    def changed_patch(self):
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / "app.py", b"print('changed')\n\x00\xff")
        write(layout.workspace / "src" / "pkg" / "new.py", "x = 1\n")
        os.chmod(layout.workspace / "src" / "pkg" / "new.py", 0o755)
        os.unlink(layout.workspace / "src" / "nested" / "deep.txt")
        return layout, manifest, self.collect(layout, manifest)

    def test_patch_shape_and_digest_stability(self) -> None:
        layout, manifest, patch = self.changed_patch()
        self.assertEqual(set(patch), {"schema_version", "source_commit", "manifest_digest", "changes"})
        self.assertEqual([c["path"] for c in patch["changes"]], sorted(c["path"] for c in patch["changes"]))
        again = workspace.collect_patch(layout, manifest)
        self.assertEqual(workspace.encode_patch(again), workspace.encode_patch(patch))
        self.assertEqual(workspace.patch_digest(again), workspace.patch_digest(patch))
        self.assertEqual(
            workspace.patch_digest(patch), hashlib.sha256(workspace.encode_patch(patch)).hexdigest()
        )
        reordered = dict(reversed(list(copy.deepcopy(patch).items())))
        self.assertEqual(workspace.patch_digest(reordered), workspace.patch_digest(patch))

    def test_round_trip_into_a_checkout(self) -> None:
        _, manifest, patch = self.changed_patch()
        target = self.base / "checkout"
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-q", "--detach", str(target),
             manifest["source_commit"]],
            check=True, capture_output=True,
        )
        workspace.apply_patch_to_tree(patch, target)
        self.assertEqual((target / "src" / "app.py").read_bytes(), b"print('changed')\n\x00\xff")
        self.assertTrue(os.stat(target / "src" / "pkg" / "new.py").st_mode & stat.S_IXUSR)
        self.assertFalse((target / "src" / "nested").exists())
        status = git(target, "status", "--porcelain")
        self.assertIn("src/app.py", status)
        self.assertIn("src/nested/deep.txt", status)

    def test_apply_refuses_mismatched_old_blob(self) -> None:
        _, _, patch = self.changed_patch()
        target = self.base / "tree"
        write(target / "src" / "app.py", "something else\n")
        write(target / "src" / "nested" / "deep.txt", "deep\n")
        self.assertRefused("apply_blob_mismatch", workspace.apply_patch_to_tree, patch, target)
        # Nothing was written before the refusal.
        self.assertEqual((target / "src" / "app.py").read_text(), "something else\n")
        self.assertFalse((target / "src" / "pkg").exists())

    def test_apply_refuses_existing_add_target_and_symlinks(self) -> None:
        _, manifest, patch = self.changed_patch()
        target = self.base / "tree"
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-q", "--detach", str(target),
             manifest["source_commit"]],
            check=True, capture_output=True,
        )
        write(target / "src" / "pkg" / "new.py", "occupied\n")
        self.assertRefused("apply_path_exists", workspace.apply_patch_to_tree, patch, target)
        os.unlink(target / "src" / "pkg" / "new.py")
        os.unlink(target / "src" / "app.py")
        os.symlink("run.sh", target / "src" / "app.py")
        self.assertRefused("apply_entry_not_file", workspace.apply_patch_to_tree, patch, target)
        os.unlink(target / "src" / "app.py")
        os.rmdir(target / "src" / "pkg")
        outside = self.base / "elsewhere"
        outside.mkdir()
        os.symlink(outside, target / "src" / "pkg")
        with self.assertRaises(WorkspaceRefused):
            workspace.apply_patch_to_tree(patch, target)
        self.assertEqual(list(outside.iterdir()), [])

    def test_tampered_patch_is_refused(self) -> None:
        _, _, patch = self.changed_patch()
        tampered = copy.deepcopy(patch)
        tampered["changes"][0]["content_b64"] = base64.b64encode(b"other").decode()
        self.assertRefused("patch_content_mismatch", workspace.validate_patch, tampered)
        protected = copy.deepcopy(patch)
        protected["changes"][0]["path"] = "src/.claude/x"
        self.assertRefused("protected_name", workspace.validate_patch, protected)
        unsorted = copy.deepcopy(patch)
        unsorted["changes"].reverse()
        self.assertRefused("patch_invalid", workspace.validate_patch, unsorted)


class EnvelopeAndCleanupTest(WorkspaceTestCase):
    def test_envelope_digest_stability(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / "app.py", "print('changed')\n")
        os.unlink(layout.workspace / "src" / "nested" / "deep.txt")
        patch = self.collect(layout, manifest)
        binding = {"run_id": "run-1", "admission_id": "admission-1", "attempt": 1}
        envelope = workspace.build_verifier_envelope(patch, binding)
        self.assertEqual(envelope["patch_digest"], workspace.patch_digest(patch))
        self.assertEqual(envelope["source_commit"], manifest["source_commit"])
        self.assertEqual(envelope["manifest_digest"], workspace.manifest_digest(manifest))
        self.assertEqual(envelope["run_binding"], binding)
        self.assertEqual([item["path"] for item in envelope["resulting_files"]], ["src/app.py"])
        self.assertEqual(
            base64.b64decode(envelope["resulting_files"][0]["content_b64"]), b"print('changed')\n"
        )
        again = workspace.build_verifier_envelope(copy.deepcopy(patch), dict(reversed(binding.items())))
        self.assertEqual(workspace.envelope_digest(again), workspace.envelope_digest(envelope))
        changed = workspace.build_verifier_envelope(patch, {**binding, "attempt": 2})
        self.assertNotEqual(workspace.envelope_digest(changed), workspace.envelope_digest(envelope))
        self.assertRefused("run_binding_invalid", workspace.build_verifier_envelope, patch, {"x": [1]})

    def test_cleanup_is_idempotent_and_does_not_follow(self) -> None:
        outside = self.base / "outside"
        write(outside / "keep.txt", "keep\n")
        layout, _ = self.export(write_set=("src",))
        os.symlink(outside, layout.workspace / "src" / "escape")
        locked = layout.workspace / "src" / "locked"
        write(locked / "inner.txt", "x")
        os.chmod(locked, 0o500)
        workspace.snapshot_workspace(layout)
        os.chmod(locked, 0o000)
        workspace.cleanup_layout(layout)
        self.assertFalse(os.path.lexists(layout.root))
        self.assertEqual((outside / "keep.txt").read_text(), "keep\n")
        workspace.cleanup_layout(layout)

    def test_cleanup_of_deep_tree(self) -> None:
        layout = workspace.create_layout(self.runs)
        directory = os.open(layout.workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(400):
                os.mkdir("d", dir_fd=directory)
                following = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = following
        finally:
            os.close(directory)
        # Removal does not recurse: it succeeds below a recursion limit smaller than the depth.
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(250)
        try:
            workspace.cleanup_layout(layout)
        finally:
            sys.setrecursionlimit(limit)
        self.assertFalse(os.path.lexists(layout.root))

    def test_cleanup_of_symlinked_layout_root(self) -> None:
        outside = self.base / "outside"
        write(outside / "keep.txt", "keep\n")
        layout = workspace.create_layout(self.runs)
        workspace.cleanup_layout(layout)
        os.symlink(outside, layout.root)
        workspace.cleanup_layout(layout)
        self.assertFalse(os.path.lexists(layout.root))
        self.assertTrue((outside / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
