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


GIT_NO_LAZY_FETCH = subprocess.run(
    ["git", "--no-lazy-fetch", "version"], capture_output=True, check=False
).returncode == 0
NEEDS_GIT = unittest.skipUnless(
    GIT_NO_LAZY_FETCH, "the workspace refuses a Git without --no-lazy-fetch (needs Git 2.44 or later)"
)


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


def case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def hand_patch(source_commit: str, changes: list[dict]) -> dict:
    entries = []
    for change in changes:
        content = change.get("content")
        entries.append({
            "path": change["path"], "op": change["op"], "old_blob": change.get("old_blob"),
            "old_mode": change.get("old_mode"),
            "new_sha256": None if content is None else hashlib.sha256(content).hexdigest(),
            "mode": change.get("mode", None if content is None else "100644"),
            "content_b64": None if content is None else base64.b64encode(content).decode(),
        })
    return {
        "schema_version": 1, "source_commit": source_commit, "manifest_digest": "0" * 64,
        "changes": sorted(entries, key=lambda item: item["path"]),
    }


@NEEDS_GIT
class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
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
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
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
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
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
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        self.assertRefused("export_symlink", workspace.export_workspace, self.project, layout, ["src"], [])

    def test_submodule_is_refused(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-index", "--add", "--cacheinfo", f"160000,{commit},src/sub")
        git(self.project, "commit", "-qm", "submodule")
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        self.assertRefused("export_submodule", workspace.export_workspace, self.project, layout, ["src"], [])

    def test_missing_read_path_refuses_but_missing_write_path_is_allowed(self) -> None:
        layout, manifest = self.export(read=("src",), write_set=("new/module.py",))
        self.assertEqual(manifest["write_set"], ["new/module.py"])
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
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
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
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
            "src/nested/deep.txt": ("delete", None),
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
        if not os.stat(layout.workspace / "src" / "app.py").st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
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
        for clone in (workspace._FCLONEFILEAT, None):
            layout, _ = self.export(write_set=("src",))
            write(layout.workspace / "src" / "app.py", "print('child')\n")
            os.chmod(layout.workspace / "src" / "app.py", 0o755)
            os.mkfifo(layout.workspace / "src" / "pipe")
            with mock.patch.object(workspace, "_FCLONEFILEAT", clone):
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
        workspace.remove_quiescent_tree(layout.workspace)
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
        verified = workspace.verify_tree_at_commit(target, manifest["source_commit"])
        workspace.apply_patch_to_tree(patch, target, verified=verified)
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
        self.assertRefused(
            "apply_blob_mismatch", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
        )
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
        self.assertRefused(
            "apply_path_exists", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
        )
        os.unlink(target / "src" / "pkg" / "new.py")
        os.unlink(target / "src" / "app.py")
        os.symlink("run.sh", target / "src" / "app.py")
        self.assertRefused(
            "apply_entry_not_file", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
        )
        os.unlink(target / "src" / "app.py")
        os.rmdir(target / "src" / "pkg")
        outside = self.base / "elsewhere"
        outside.mkdir()
        os.symlink(outside, target / "src" / "pkg")
        with self.assertRaises(WorkspaceRefused):
            workspace.apply_patch_to_tree(patch, target, base_verified_by_caller=True)
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
        envelope = workspace.build_verifier_envelope(patch, manifest, binding)
        self.assertEqual(envelope["patch_digest"], workspace.patch_digest(patch))
        self.assertEqual(envelope["source_commit"], manifest["source_commit"])
        self.assertEqual(envelope["manifest_digest"], workspace.manifest_digest(manifest))
        self.assertEqual(envelope["run_binding"], binding)
        self.assertEqual([item["path"] for item in envelope["resulting_files"]], ["src/app.py"])
        self.assertEqual(
            base64.b64decode(envelope["resulting_files"][0]["content_b64"]), b"print('changed')\n"
        )
        again = workspace.build_verifier_envelope(
            copy.deepcopy(patch), copy.deepcopy(manifest), dict(reversed(binding.items()))
        )
        self.assertEqual(workspace.envelope_digest(again), workspace.envelope_digest(envelope))
        changed = workspace.build_verifier_envelope(patch, manifest, {**binding, "attempt": 2})
        self.assertNotEqual(workspace.envelope_digest(changed), workspace.envelope_digest(envelope))
        self.assertRefused(
            "run_binding_invalid", workspace.build_verifier_envelope, patch, manifest, {"x": [1]}
        )
        self.assertEqual(envelope["manifest"], manifest)
        other = {**manifest, "write_set": ["docs"]}
        self.assertRefused(
            "envelope_manifest_mismatch", workspace.build_verifier_envelope, patch, other, binding
        )
        # The envelope is detached from the caller's patch and manifest.
        before = workspace.envelope_digest(envelope)
        patch["changes"][0]["path"] = "src/tampered.py"
        manifest["write_set"].append("docs")
        self.assertEqual(workspace.envelope_digest(envelope), before)

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
        workspace.cleanup_layout(layout, quiescent=True)
        self.assertFalse(os.path.lexists(layout.root))
        self.assertEqual((outside / "keep.txt").read_text(), "keep\n")
        workspace.cleanup_layout(layout, quiescent=True)

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
            workspace.cleanup_layout(layout, quiescent=True)
        finally:
            sys.setrecursionlimit(limit)
        self.assertFalse(os.path.lexists(layout.root))

    def test_cleanup_of_symlinked_layout_root(self) -> None:
        outside = self.base / "outside"
        write(outside / "keep.txt", "keep\n")
        layout = workspace.create_layout(self.runs)
        workspace.cleanup_layout(layout, quiescent=True)
        os.symlink(outside, layout.root)
        workspace.cleanup_layout(layout, quiescent=True)
        self.assertFalse(os.path.lexists(layout.root))
        self.assertTrue((outside / "keep.txt").exists())


class GitHardeningTest(WorkspaceTestCase):
    def test_git_without_no_lazy_fetch_is_refused(self) -> None:
        original = bridge._git

        def old_git(root, *arguments, text=False):
            if "--no-lazy-fetch" in arguments:
                return subprocess.CompletedProcess(arguments, 129, b"", b"")
            return original(root, *arguments, text=text)

        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        with mock.patch.object(workspace, "_LAZY_FETCH_SUPPORTED", None), \
                mock.patch.object(bridge, "_git", side_effect=old_git):
            self.assertRefused(
                "git_unsupported", workspace.export_workspace, self.project, layout, ["src"], ["src"]
            )

    def partial_clone(self, *, extension: bool) -> Path:
        marker = self.base / "remote-command-ran"
        blob = git(self.project, "rev-parse", "HEAD:src/app.py")
        os.unlink(self.project / ".git" / "objects" / blob[:2] / blob[2:])
        git(self.project, "remote", "add", "origin", "ssh://example.invalid/repo.git")
        git(self.project, "config", "remote.origin.promisor", "true")
        git(self.project, "config", "core.sshCommand", f"touch {marker}; false")
        if extension:
            git(self.project, "config", "core.repositoryformatversion", "1")
            git(self.project, "config", "extensions.partialClone", "origin")
            git(self.project, "config", "--unset", "remote.origin.promisor")
        return marker

    def test_partial_clone_is_refused_without_running_remote_configuration(self) -> None:
        for extension in (True, False):
            with self.subTest(extension=extension):
                self.setUp()
                marker = self.partial_clone(extension=extension)
                layout = workspace.create_layout(self.runs)
                self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
                self.assertRefused(
                    "partial_clone", workspace.export_workspace, self.project, layout, ["src"], ["src"]
                )
                self.assertFalse(marker.exists())
                self.assertEqual(list(layout.workspace.iterdir()), [])

    def test_missing_object_never_fetches_even_past_the_partial_clone_check(self) -> None:
        marker = self.partial_clone(extension=True)
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        original = bridge._git

        def without_partial_config(root, *arguments, text=False):
            if "config" in arguments:
                return subprocess.CompletedProcess(arguments, 1, b"", b"")
            return original(root, *arguments, text=text)

        with mock.patch.object(bridge, "_git", side_effect=without_partial_config):
            self.assertRefused(
                "export_blob_unavailable", workspace.export_workspace, self.project, layout, ["src"], ["src"]
            )
        self.assertFalse(marker.exists())


class ReviewFixesTest(WorkspaceTestCase):
    # Cleanup (finding 2)

    def test_cleanup_without_quiescence_only_fences(self) -> None:
        layout, _ = self.export()
        fenced = workspace.cleanup_layout(layout)
        self.assertFalse(os.path.lexists(layout.root))
        self.assertEqual(fenced.parent, layout.root.parent)
        self.assertTrue(fenced.name.startswith(layout.root.name + workspace.FENCED_MARKER))
        self.assertTrue((fenced / "workspace" / "src" / "app.py").exists())
        self.assertIsNone(workspace.cleanup_layout(layout))
        workspace.remove_quiescent_tree(fenced)
        self.assertFalse(os.path.lexists(fenced))
        workspace.remove_quiescent_tree(fenced)

    def test_cleanup_stops_before_unlinking_in_a_relocated_directory(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        layout = workspace.create_layout(self.runs)
        write(layout.workspace / "a" / "b" / "keep.txt", "keep\n")
        write(layout.workspace / "a" / "sibling.txt", "keep\n")
        status = os.stat(layout.workspace / "a")
        target = (status.st_dev, status.st_ino)
        original = workspace._verified_parent
        moved = []

        def relocating(current, place):
            here = os.fstat(current)
            if (here.st_dev, here.st_ino) == target and not moved:
                os.rename(layout.workspace / "a", outside / "a")
                moved.append(True)
            return original(current, place)

        with mock.patch.object(workspace, "_verified_parent", side_effect=relocating):
            self.assertRefused("cleanup_relocated", workspace.cleanup_layout, layout, quiescent=True)
        self.assertTrue(moved)
        self.assertEqual((outside / "a" / "b" / "keep.txt").read_text(), "keep\n")
        self.assertEqual((outside / "a" / "sibling.txt").read_text(), "keep\n")
        workspace.remove_quiescent_tree(layout.root)

    # Snapshot (finding 3)

    def test_file_changing_during_snapshot_is_unstable(self) -> None:
        for clone in (workspace._FCLONEFILEAT, None):
            with self.subTest(clone=clone is not None):
                layout, _ = self.export()
                original = workspace._transfer

                def growing(source_fd, target_dir_fd, name, limit, layout=layout):
                    original(source_fd, target_dir_fd, name, limit)
                    if name == "app.py":
                        with open(layout.workspace / "src" / "app.py", "ab") as handle:
                            handle.write(b"more\n")

                with mock.patch.object(workspace, "_FCLONEFILEAT", clone), \
                        mock.patch.object(workspace, "_transfer", side_effect=growing):
                    self.assertRefused("snapshot_unstable", workspace.snapshot_workspace, layout)

    def swap_before_open(self, layout, replace):
        real_open = os.open
        swapped = []

        def swapping(path, flags, *arguments, **keywords):
            if path == "app.py" and flags & os.O_NONBLOCK and not swapped:
                swapped.append(True)
                replace(layout.workspace / "src" / "app.py")
            return real_open(path, flags, *arguments, **keywords)

        return mock.patch.object(workspace.os, "open", side_effect=swapping), swapped

    def test_file_swapped_for_symlink_before_open_is_recorded_as_symlink(self) -> None:
        layout, manifest = self.export()

        def to_symlink(path):
            path.unlink()
            os.symlink("/etc/passwd", path)

        patcher, swapped = self.swap_before_open(layout, to_symlink)
        with patcher:
            workspace.snapshot_workspace(layout)
        self.assertTrue(swapped)
        self.assertEqual(workspace.inventory_snapshot(layout)["src/app.py"]["type"], "symlink")
        self.assertRefused("symlink_entry", workspace.collect_patch, layout, manifest)

    def test_file_swapped_for_directory_is_never_cloned(self) -> None:
        layout, _ = self.export()

        def to_directory(path):
            path.unlink()
            path.mkdir()
            (path / "inner.txt").write_text("x")

        patcher, swapped = self.swap_before_open(layout, to_directory)
        with patcher:
            self.assertRefused("snapshot_unstable", workspace.snapshot_workspace, layout)
        self.assertTrue(swapped)
        self.assertFalse(os.path.lexists(layout.snapshot / "src" / "app.py"))

    def test_hard_link_count_comes_from_the_pinned_descriptor(self) -> None:
        layout, manifest = self.export()
        os.link(layout.workspace / "src" / "app.py", self.base / "outside-link")
        workspace.snapshot_workspace(layout)
        self.assertEqual(workspace.inventory_snapshot(layout)["src/app.py"]["nlink"], 2)
        self.assertRefused("hard_link", workspace.collect_patch, layout, manifest)

    # Streaming caps (finding 4)

    def test_listing_stops_at_the_budget_before_full_enumeration(self) -> None:
        directory = self.base / "many"
        directory.mkdir()
        for index in range(100):
            (directory / f"f{index}").write_text("x")
        real_scandir = os.scandir
        consumed = []

        class Counting:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self

            def __exit__(self, *details):
                self.inner.close()

            def __iter__(self):
                for entry in self.inner:
                    consumed.append(entry.name)
                    yield entry

        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(workspace.os, "scandir", side_effect=lambda fd: Counting(real_scandir(fd))):
                self.assertRefused("inventory_too_many_entries", workspace._list_names, descriptor, 5)
        finally:
            os.close(descriptor)
        self.assertEqual(len(consumed), 6)

    # Base verification (finding 5)

    def worktree(self, name="checkout", commit=None) -> Path:
        target = self.base / name
        git(self.project, "worktree", "add", "-q", "--detach", str(target), commit or "HEAD")
        return target

    def test_verify_tree_at_commit(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        target = self.worktree()
        verified = workspace.verify_tree_at_commit(target, commit)
        self.assertEqual(verified.commit, commit)
        write(target / "src" / "untracked.py", "x\n")
        self.assertRefused("tree_not_clean", workspace.verify_tree_at_commit, target, commit)
        os.unlink(target / "src" / "untracked.py")
        write(target / "src" / "app.py", "edited\n")
        self.assertRefused("tree_not_clean", workspace.verify_tree_at_commit, target, commit)
        git(target, "add", "src/app.py")
        self.assertRefused("tree_index_dirty", workspace.verify_tree_at_commit, target, commit)
        git(target, "reset", "-q", "--hard")
        os.chmod(target / "src" / "app.py", 0o755)
        self.assertRefused("tree_not_clean", workspace.verify_tree_at_commit, target, commit)
        os.chmod(target / "src" / "app.py", 0o644)
        write(self.project / "src" / "app.py", "next\n")
        later = self.commit("later")
        self.assertRefused("tree_not_at_commit", workspace.verify_tree_at_commit, target, later)
        workspace.verify_tree_at_commit(target, commit)

    def test_apply_requires_an_established_base(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        patch = hand_patch(commit, [{"path": "src/new.py", "op": "add", "content": b"x\n"}])
        target = self.worktree()
        self.assertRefused("apply_base_unverified", workspace.apply_patch_to_tree, patch, target)
        write(self.project / "src" / "app.py", "next\n")
        later = self.commit("later")
        other = self.worktree("other", later)
        stale = workspace.verify_tree_at_commit(other, later)
        self.assertRefused("apply_base_mismatch", workspace.apply_patch_to_tree, patch, target, verified=stale)
        wrong_root = workspace.verify_tree_at_commit(target, commit)
        self.assertRefused(
            "apply_base_mismatch", workspace.apply_patch_to_tree, patch, other,
            verified=workspace.VerifiedTree(other, commit, wrong_root.identity),
        )
        workspace.apply_patch_to_tree(patch, target, verified=wrong_root)
        self.assertEqual((target / "src" / "new.py").read_text(), "x\n")

    def test_apply_refuses_old_mode_mismatch(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        blob = git(self.project, "rev-parse", "HEAD:src/app.py")
        patch = hand_patch(commit, [{
            "path": "src/app.py", "op": "modify", "old_blob": blob, "old_mode": "100644", "content": b"y\n",
        }])
        target = self.worktree()
        os.chmod(target / "src" / "app.py", 0o755)
        self.assertRefused(
            "apply_mode_mismatch", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
        )
        self.assertEqual((target / "src" / "app.py").read_text(), "print('app')\n")
        delete = hand_patch(commit, [{"path": "src/app.py", "op": "delete", "old_blob": blob, "old_mode": None}])
        self.assertRefused("patch_invalid", workspace.validate_patch, delete)

    # Export aliasing (finding 6)

    def commit_paths(self, *paths: str) -> None:
        for path in paths:
            blob = subprocess.run(
                ["git", "-C", str(self.project), "hash-object", "-w", "--stdin"],
                input=path.encode(), capture_output=True, check=True,
            ).stdout.decode().strip()
            git(self.project, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        git(self.project, "commit", "-qm", "aliases")

    def test_export_refuses_case_aliased_directories(self) -> None:
        self.commit_paths("src/A/x.txt", "src/a/y.txt")
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        self.assertRefused(
            "export_path_collision", workspace.export_workspace, self.project, layout, ["src"], []
        )

    def test_export_refuses_aliased_directory_identities(self) -> None:
        if not case_insensitive(self.base):
            self.skipTest("needs a case-insensitive filesystem to alias two directory names")
        self.commit_paths("src/A/x.txt", "src/a/y.txt")
        layout = workspace.create_layout(self.runs)
        self.addCleanup(workspace.cleanup_layout, layout, quiescent=True)
        with mock.patch.object(workspace, "_alias_check"):
            self.assertRefused(
                "export_path_collision", workspace.export_workspace, self.project, layout, ["src"], []
            )

    # Apply aliasing and exclusive adds (finding 7)

    def test_apply_refuses_folded_aliases_in_the_patch(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        for paths in (("src/Pkg/a.py", "src/pkg/b.py"), ("src/NEW.py", "src/new.py"),
                      ("src/café.py", "src/café.py")):
            with self.subTest(paths=paths):
                patch = hand_patch(commit, [{"path": p, "op": "add", "content": b"x"} for p in paths])
                target = Path(tempfile.mkdtemp(dir=self.base))
                self.assertRefused(
                    "apply_path_collision", workspace.apply_patch_to_tree, patch, target,
                    base_verified_by_caller=True,
                )
                self.assertEqual(list(target.iterdir()), [])

    def test_add_created_between_preflight_and_write_is_not_overwritten(self) -> None:
        commit = git(self.project, "rev-parse", "HEAD")
        patch = hand_patch(commit, [{"path": "src/new.py", "op": "add", "content": b"patch\n"}])
        target = self.base / "tree"
        (target / "src").mkdir(parents=True)
        original = workspace._install_file

        def racing(parent, name, content, mode, *, replace):
            write(target / "src" / name, "racer\n")
            return original(parent, name, content, mode, replace=replace)

        with mock.patch.object(workspace, "_install_file", side_effect=racing):
            self.assertRefused(
                "apply_path_exists", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
            )
        self.assertEqual((target / "src" / "new.py").read_text(), "racer\n")
        self.assertEqual(sorted(p.name for p in (target / "src").iterdir()), ["new.py"])

    def test_apply_refuses_case_alias_of_an_existing_file(self) -> None:
        target = self.base / "tree"
        write(target / "src" / "readme.txt", "existing\n")
        if not case_insensitive(target):
            self.skipTest("needs a case-insensitive filesystem")
        commit = git(self.project, "rev-parse", "HEAD")
        patch = hand_patch(commit, [{"path": "src/README.txt", "op": "add", "content": b"new\n"}])
        self.assertRefused(
            "apply_path_exists", workspace.apply_patch_to_tree, patch, target, base_verified_by_caller=True
        )
        self.assertEqual((target / "src" / "readme.txt").read_text(), "existing\n")

    # Full permission bits (finding 8)

    def refused_mode(self, change, write_set=("src",)):
        layout, manifest = self.export(write_set=write_set)
        change(layout.workspace)
        workspace.snapshot_workspace(layout)
        self.assertRefused("mode_change", workspace.collect_patch, layout, manifest)

    def test_manifest_records_full_permission_bits(self) -> None:
        _, manifest = self.export(write_set=("src/app.py",))
        self.assertEqual(manifest["files"]["src/app.py"]["perm"], 0o644)
        self.assertEqual(manifest["files"]["src/run.sh"]["perm"], 0o555)
        self.assertEqual(manifest["files"]["docs/guide.md"]["perm"], 0o444)
        self.assertEqual(manifest["directories"], {"docs": 0o700, "src": 0o700, "src/nested": 0o700})

    def test_permission_changes_other_than_the_execute_toggle_refuse(self) -> None:
        cases = {
            "group write on owned file": lambda w: os.chmod(w / "src" / "app.py", 0o664),
            "owner-only execute": lambda w: os.chmod(w / "src" / "app.py", 0o744),
            "owner write on read-only file": lambda w: os.chmod(w / "docs" / "guide.md", 0o644),
            "read-only protected file": lambda w: os.chmod(w / "src" / "AGENTS.md", 0o555),
            "baseline directory": lambda w: os.chmod(w / "src" / "nested", 0o755),
            "new file 0600": lambda w: write(w / "src" / "new.py", "x\n", 0o600),
            "new directory 0777": lambda w: (write(w / "src" / "pkg" / "a.py", "x\n"),
                                             os.chmod(w / "src" / "pkg", 0o777)),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.refused_mode(change)

    def test_execute_toggle_on_owned_file_is_allowed(self) -> None:
        layout, manifest = self.export(write_set=("src",))
        os.chmod(layout.workspace / "src" / "run.sh", 0o644)
        patch = self.collect(layout, manifest)
        self.assertEqual(
            [(c["path"], c["op"], c["old_mode"], c["mode"]) for c in patch["changes"]],
            [("src/run.sh", "modify", "100755", "100644")],
        )

    # Long names (finding 11)

    def test_255_byte_basename_round_trip(self) -> None:
        name = "n" * 252 + ".py"
        self.assertEqual(len(name.encode()), 255)
        layout, manifest = self.export(write_set=("src",))
        write(layout.workspace / "src" / name, "long\n")
        patch = self.collect(layout, manifest)
        target = self.worktree()
        verified = workspace.verify_tree_at_commit(target, manifest["source_commit"])
        workspace.apply_patch_to_tree(patch, target, verified=verified)
        self.assertEqual((target / "src" / name).read_text(), "long\n")
        self.assertEqual([p for p in os.listdir(target / "src") if p.endswith(".tmp")], [])


class RoundTwoFixesTest(WorkspaceTestCase):
    def test_apply_refuses_an_existing_directory_spelled_differently(self) -> None:
        write(self.project / "src" / "A" / "existing.txt", "existing\n")
        commit = self.commit("upper")
        # The child owned only src/a/new.txt, which is absent, so nothing about src/A was exported.
        layout, manifest = self.export(read=("docs",), write_set=("src/a/new.txt",))
        self.assertNotIn("src/A/existing.txt", manifest["files"])
        write(layout.workspace / "src" / "a" / "new.txt", "new\n")
        patch = self.collect(layout, manifest)
        self.assertEqual([c["path"] for c in patch["changes"]], ["src/a/new.txt"])
        target = self.base / "checkout"
        git(self.project, "worktree", "add", "-q", "--detach", str(target), commit)
        verified = workspace.verify_tree_at_commit(target, commit)
        if case_insensitive(self.base):
            self.assertRefused(
                "apply_path_collision", workspace.apply_patch_to_tree, patch, target, verified=verified
            )
            self.assertEqual(sorted(os.listdir(target / "src" / "A")), ["existing.txt"])
        else:
            # The alias cannot occur here: src/a is a separate, new directory.
            workspace.apply_patch_to_tree(patch, target, verified=verified)
            self.assertEqual((target / "src" / "a" / "new.txt").read_text(), "new\n")
            self.assertEqual(sorted(os.listdir(target / "src" / "A")), ["existing.txt"])

    def test_spelling_check_on_both_filesystems(self) -> None:
        tree = self.base / "tree"
        write(tree / "src" / "A" / "existing.txt", "existing\n")
        descriptor = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        # Exact spellings, and absent components, pass everywhere.
        workspace._check_existing_spelling(descriptor, ["src", "A", "existing.txt"])
        workspace._check_existing_spelling(descriptor, ["src", "B", "new.txt"])
        # Inject the alias a case-insensitive filesystem would produce: `src/a` resolves,
        # but the listing holds only `A`.
        real_stat = os.stat

        def aliasing_stat(path, *arguments, **keywords):
            if path == "a":
                path = "A"
            return real_stat(path, *arguments, **keywords)

        with mock.patch.object(workspace.os, "stat", side_effect=aliasing_stat):
            self.assertRefused(
                "apply_path_collision", workspace._check_existing_spelling, descriptor, ["src", "a", "new.txt"]
            )

    def test_modify_and_delete_targets_spelled_differently_are_refused(self) -> None:
        if not case_insensitive(self.base):
            self.skipTest("a differently spelled existing file can only be reached on a case-insensitive filesystem")
        commit = git(self.project, "rev-parse", "HEAD")
        blob = git(self.project, "rev-parse", "HEAD:src/app.py")
        target = self.base / "checkout"
        git(self.project, "worktree", "add", "-q", "--detach", str(target), commit)
        for change in (
            {"path": "src/APP.py", "op": "modify", "old_blob": blob, "old_mode": "100644", "content": b"y\n"},
            {"path": "src/App.py", "op": "delete", "old_blob": blob, "old_mode": "100644"},
        ):
            with self.subTest(op=change["op"]):
                self.assertRefused(
                    "apply_path_collision", workspace.apply_patch_to_tree, hand_patch(commit, [change]),
                    target, base_verified_by_caller=True,
                )
                self.assertEqual((target / "src" / "app.py").read_text(), "print('app')\n")

    def test_workspace_root_mode_change_is_refused(self) -> None:
        layout, manifest = self.export()
        self.assertEqual(manifest["root_perm"], 0o700)
        os.chmod(layout.workspace, 0o777)
        workspace.snapshot_workspace(layout)
        self.assertEqual(workspace.inventory_snapshot(layout)["."]["mode"], 0o777)
        self.assertRefused("mode_change", workspace.collect_patch, layout, manifest)

    def test_unchanged_workspace_root_is_accepted(self) -> None:
        layout, manifest = self.export()
        self.assertEqual(self.collect(layout, manifest)["changes"], [])


if __name__ == "__main__":
    unittest.main()
