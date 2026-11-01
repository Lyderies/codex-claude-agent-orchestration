from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import contextlib
import errno
import io
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests.learning_support import generate_test_owner_key

from codex_agents import host_paths, learning_store
from codex_agents.host_paths import STATE_PARENT_UNSAFE, XDG_STATE_REFUSED
from codex_agents.learning_store import (
    BOTH_STATES,
    LINKED_STATE,
    STATE_MOVE_FAILED,
    STATE_SOCKET_TOO_LONG,
    UNMOVED_STATE,
    CollectorClient,
    CollectorError,
    LearningLedger,
    LearningState,
    StateError,
    authorize_collector_runtime,
    default_state_root,
    ensure_collector,
    migrate_legacy_state,
    registered_project_roots,
)
from cli import main as cli_main

CLI = ORCHESTRATION_ROOT / "cli.py"
_REAL_ACCOUNT = pwd.getpwuid(os.getuid())


def _account(home: Path) -> pwd.struct_passwd:
    real = _REAL_ACCOUNT
    return pwd.struct_passwd((real.pw_name, "x", real.pw_uid, real.pw_gid, "", str(home), "/bin/sh"))


class DefaultStateRootTests(unittest.TestCase):
    """The default learning state: the account database's home, or a hidden XDG folder."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        self.account = self.base / "account"
        self.account.mkdir()
        self.other = self.base / "other-home"
        self.other.mkdir()

    @contextlib.contextmanager
    def environment(self, platform: str = sys.platform, **environ: str):
        environment = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
        environment.update(HOME=str(self.other), **environ)
        with mock.patch.object(host_paths.pwd, "getpwuid", lambda _uid: _account(self.account)), \
                mock.patch.object(host_paths.sys, "platform", platform), \
                mock.patch.dict(os.environ, environment, clear=True):
            yield

    def resolve(self, platform: str = sys.platform, **environ: str) -> tuple[Path, Path | None]:
        with self.environment(platform, **environ):
            return default_state_root(), host_paths.legacy_state_root()

    def assert_xdg_refused(self, value: str) -> None:
        with self.environment(XDG_STATE_HOME=value):
            with self.assertRaises(StateError) as caught:
                default_state_root()
            self.assertEqual(XDG_STATE_REFUSED, str(caught.exception))
            with self.assertRaises(StateError):
                LearningState()

    def test_macos_and_linux_use_the_account_homes_local_state(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                default, _legacy = self.resolve(platform)
                # Never the overridden HOME, and the same hidden folder on both.
                self.assertEqual(self.account / ".local" / "state" / "codex-agents", default)

    def test_a_hidden_xdg_folder_in_home_is_accepted(self) -> None:
        # How the tests isolate a scratch home: HOME and XDG_STATE_HOME under it.
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                default, _ = self.resolve(
                    platform, XDG_STATE_HOME=str(self.other / ".local" / "state")
                )
                self.assertEqual(self.other / ".local" / "state" / "codex-agents", default)
        # A relative value is ignored, as the XDG specification says.
        default, _ = self.resolve(XDG_STATE_HOME="relative/state")
        self.assertEqual(self.account / ".local" / "state" / "codex-agents", default)

    def test_a_non_hidden_or_outside_xdg_folder_is_refused(self) -> None:
        for value in (
            self.other / "State",  # a folder a permitted session may be opened on
            self.other / ".local",  # the hidden folder itself, not inside it
            self.base / "elsewhere" / ".state" / "x",  # outside HOME
            self.account / ".local" / "state",  # the account home is not HOME here
            self.other,
        ):
            with self.subTest(value=value):
                self.assert_xdg_refused(str(value))
        with self.environment(XDG_STATE_HOME=str(self.other / ".local" / "state")), \
                mock.patch.dict(os.environ, {"HOME": "relative"}):
            with self.assertRaisesRegex(StateError, "XDG_STATE_HOME must be inside"):
                default_state_root()

    def test_a_symlinked_xdg_folder_is_refused(self) -> None:
        real = self.other / ".real-local"
        (real / "state").mkdir(parents=True)
        (self.other / ".local").symlink_to(real)
        self.assert_xdg_refused(str(self.other / ".local" / "state"))
        # Also a link further down, and one written through a symlinked HOME.
        (self.other / ".data").mkdir()
        (self.other / ".data" / "state").symlink_to(real / "state")
        self.assert_xdg_refused(str(self.other / ".data" / "state"))

    def test_unsafe_folders_above_the_state_are_refused(self) -> None:
        local = self.account / ".local"
        local.mkdir(mode=0o700)
        os.chmod(local, 0o777)
        with self.environment():
            with self.assertRaises(StateError) as caught:
                LearningState()
            self.assertEqual(STATE_PARENT_UNSAFE, str(caught.exception))
        os.chmod(local, 0o755)
        (local / "state").symlink_to(self.base / "elsewhere")
        with self.environment(), self.assertRaisesRegex(StateError, "symbolic link"):
            default_state_root()

    def test_missing_folders_above_the_state_are_created_owner_only(self) -> None:
        with self.environment():
            state = LearningState()
        self.assertEqual(self.account / ".local" / "state" / "codex-agents", state.root)
        for folder in (self.account / ".local", self.account / ".local" / "state", state.root):
            self.assertEqual(0, stat.S_IMODE(folder.stat().st_mode) & 0o077, folder)

    def test_only_macos_has_an_old_state_folder(self) -> None:
        _default, legacy = self.resolve("darwin")
        # Where the pre-0.8.9 code put it: HOME's Library/Application Support.
        self.assertEqual(self.other / "Library" / "Application Support" / "codex-agents", legacy)
        self.assertIsNone(self.resolve("linux")[1])


class HardLinkTests(unittest.TestCase):
    """No state file may have a second hard link that something else could write."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        self.state = LearningState(self.base / "state")
        _key, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)

    def test_a_hard_linked_signer_or_key_is_refused(self) -> None:
        self.state.owner_allowed_signers()
        os.link(self.state.allowed_signers_path, self.base / "linked-signer")
        with self.assertRaisesRegex(StateError, "hard link|unsafe"):
            self.state.owner_allowed_signers()
        os.unlink(self.base / "linked-signer")
        self.state.owner_allowed_signers()
        os.link(self.state.key_path, self.base / "linked-key")
        with self.assertRaisesRegex(StateError, "hard link|unsafe"):
            self.state.digest_key()


class LegacyStateMoveTests(unittest.TestCase):
    """The one-time, owner-driven copy of the pre-0.8.9 macOS state folder."""

    def setUp(self) -> None:
        # Short: the collector's socket path is limited to about 100 bytes.
        self.base = Path(tempfile.mkdtemp(prefix="mv-", dir=os.path.realpath("/tmp")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "h"
        self.home.mkdir()
        self.legacy = self.home / "Library" / "Application Support" / "codex-agents"
        self.target = self.home / ".local" / "state" / "codex-agents"
        self.project = self.base / "p"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        # Everything this test touches lies in its own scratch folder.
        for path in (self.legacy, self.target):
            self.assertTrue(str(path).startswith(str(self.base) + os.sep))
        for patcher in (
            mock.patch.dict(os.environ, {
                "HOME": str(self.home), "XDG_STATE_HOME": str(self.target.parent),
            }),
            mock.patch.object(host_paths, "legacy_state_root", lambda: self.legacy),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_collectors)

    def stop_collectors(self) -> None:
        for root in (self.legacy, self.target):
            client = CollectorClient(root / "collector.sock", timeout=0.2)
            try:
                client.shutdown()
            except CollectorError:
                continue
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    client.health()
                except CollectorError:
                    break
                time.sleep(0.02)

    def old_state(self, *, register: bool = True) -> LearningState:
        self.legacy.parent.mkdir(parents=True)
        state = LearningState(self.legacy)
        authorize_collector_runtime(state, CLI)
        _key, public = generate_test_owner_key(self.base)
        state.install_owner_public_key(public)
        # The installer's records live here too; a large file is copied in chunks.
        installer = state.root / "installer"
        installer.mkdir(mode=0o700)
        (installer / "claude-settings-0123.json").write_bytes(b"x" * 3_000_000)
        os.chmod(installer / "claude-settings-0123.json", 0o600)
        if register:
            ledger = LearningLedger(state)
            try:
                ledger.register_project(self.project, now_ms=1)
            finally:
                ledger.close()
        return state

    def run_cli(self, *arguments: str, expected: int = 0) -> dict:
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["cli.py", *arguments]), \
                contextlib.redirect_stdout(stdout):
            code = cli_main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def moved_aside(self) -> list[Path]:
        return sorted(self.legacy.parent.glob("codex-agents.moved-*"))

    def assert_nothing_staged(self) -> None:
        parent = self.target.parent
        self.assertEqual([], list(parent.glob(".codex-agents.staging-*")) if parent.exists() else [])
        self.assertFalse(os.path.lexists(self.target))

    def test_only_the_old_folder_refuses_every_other_use(self) -> None:
        self.old_state()
        for create in (True, False):
            with self.subTest(create=create):
                with self.assertRaisesRegex(StateError, "old macOS folder"):
                    LearningState(create=create)
        self.assertFalse(os.path.lexists(self.target))
        refused = self.run_cli("learning-status", "--project-root", str(self.project), expected=2)
        self.assertEqual({"ready": False, "error": UNMOVED_STATE}, refused)
        self.assertIn("./setup.sh (collector step)", UNMOVED_STATE)
        self.assertFalse(os.path.lexists(self.target))
        # An explicitly named state root is not the default and is not checked.
        LearningState(self.legacy, create=False)

    def test_registration_stops_the_collector_copies_and_sets_the_old_folder_aside(self) -> None:
        state = self.old_state()
        ensure_collector(state, CLI, project_root=self.project)
        CollectorClient(self.legacy / "collector.sock").health()
        before = {
            name: (self.legacy / name).read_bytes()
            for name in ("digest.key", "owner-allowed-signers", "collector-authorization.json")
        }
        old_inode = os.stat(self.legacy / "owner-allowed-signers").st_ino
        real_backup = learning_store._backup_state_database
        stopped: list[bool] = []

        def checking_backup(old, staged):
            # The old collector answers no more when the ledger is copied.
            with self.assertRaises(CollectorError):
                CollectorClient(self.legacy / "collector.sock", timeout=0.2).health()
            stopped.append(True)
            return real_backup(old, staged)

        with mock.patch.object(learning_store, "_backup_state_database", checking_backup):
            report = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertEqual([True], stopped)
        self.assertTrue(report["ready"])
        # The old folder is renamed aside, inert, and the report names it.
        aside = self.moved_aside()
        self.assertEqual(1, len(aside))
        self.assertEqual(str(aside[0]), report["state_moved"])
        self.assertRegex(aside[0].name, r"^codex-agents\.moved-\d{8}T\d{6}Z$")
        self.assertFalse(os.path.lexists(self.legacy))
        # The same state, in fresh files, owner-only, with the collector running there.
        for name, content in before.items():
            self.assertEqual(content, (self.target / name).read_bytes(), name)
        self.assertNotEqual(old_inode, os.stat(self.target / "owner-allowed-signers").st_ino)
        self.assertEqual(
            3_000_000,
            (self.target / "installer" / "claude-settings-0123.json").stat().st_size,
        )
        self.assertEqual(0o700, stat.S_IMODE(self.target.stat().st_mode))
        self.assertEqual([], list(self.target.parent.glob(".codex-agents.staging-*")))
        moved = LearningState()
        self.assertEqual(self.target, moved.root)
        # The registration the old ledger held is still there.
        CollectorClient(moved.socket).health(self.project)
        self.assertEqual([self.project.resolve()], registered_project_roots())
        # Only once: a later registration finds the new folder alone.
        again = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertIsNone(again["state_moved"])

    def test_a_descriptor_held_before_the_move_cannot_change_the_new_signer(self) -> None:
        self.old_state(register=False)
        held = os.open(self.legacy / "owner-allowed-signers", os.O_WRONLY)
        self.addCleanup(os.close, held)
        aside = migrate_legacy_state(CLI, self.project)
        before = (self.target / "owner-allowed-signers").read_bytes()
        os.lseek(held, 0, os.SEEK_SET)
        os.write(held, b"codex-agents-owner ssh-ed25519 AAAAattacker\n")
        self.assertEqual(before, (self.target / "owner-allowed-signers").read_bytes())
        LearningState().owner_allowed_signers()
        # The write landed in the inert old copy only.
        self.assertIn(b"attacker", (aside / "owner-allowed-signers").read_bytes())

    def test_a_hard_linked_old_file_refuses_and_changes_nothing(self) -> None:
        self.old_state(register=False)
        planted = self.base / "project-link"
        os.link(self.legacy / "owner-allowed-signers", planted)
        with self.assertRaises(StateError) as caught:
            migrate_legacy_state(CLI, self.project)
        self.assertEqual(LINKED_STATE, str(caught.exception))
        self.assert_nothing_staged()
        self.assertTrue(os.path.lexists(self.legacy))
        self.assertEqual([], self.moved_aside())
        os.unlink(planted)
        # A symlink or a special file in the old folder refuses the same way.
        (self.legacy / "artifacts" / "planted").symlink_to(self.base / "elsewhere")
        with self.assertRaisesRegex(StateError, "second hard link, a symbolic link"):
            migrate_legacy_state(CLI, self.project)
        self.assert_nothing_staged()

    def test_a_failed_copy_removes_the_staging_folder_and_keeps_the_old_one(self) -> None:
        self.old_state()

        def disk_full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "no space left on device")

        for patched in ("_copy_state_file", "_backup_state_database"):
            with self.subTest(step=patched), \
                    mock.patch.object(learning_store, patched, disk_full):
                with self.assertRaises(StateError) as caught:
                    migrate_legacy_state(CLI, self.project)
                self.assertEqual(STATE_MOVE_FAILED, str(caught.exception))
                self.assert_nothing_staged()
                self.assertTrue(os.path.lexists(self.legacy))
                self.assertEqual([], self.moved_aside())
        # A copy that fails its checks is removed the same way.
        with mock.patch.object(
            learning_store.LearningState, "owner_allowed_signers",
            side_effect=StateError("owner allowed-signers must name exactly one owner key"),
        ):
            with self.assertRaisesRegex(StateError, "did not pass its checks"):
                migrate_legacy_state(CLI, self.project)
        self.assert_nothing_staged()
        # Setting the old folder aside fails: the published copy is taken back.
        real_rename = os.rename

        def refuse_aside(source, destination, *args, **kwargs):
            if Path(source) == self.legacy:
                raise OSError(errno.EACCES, "permission denied")
            return real_rename(source, destination, *args, **kwargs)

        with mock.patch.object(learning_store.os, "rename", refuse_aside):
            with self.assertRaises(StateError):
                migrate_legacy_state(CLI, self.project)
        self.assert_nothing_staged()
        self.assertTrue(os.path.lexists(self.legacy))
        # And afterwards the move still works.
        self.assertIsNotNone(migrate_legacy_state(CLI, self.project))
        ledger = sqlite3.connect(self.target / "learning.sqlite3")
        try:
            self.assertEqual("ok", ledger.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            ledger.close()

    def test_both_folders_refuse(self) -> None:
        self.old_state()
        LearningState(self.target)
        with self.assertRaisesRegex(StateError, "both in its old macOS folder"):
            migrate_legacy_state(CLI, self.project)
        refused = self.run_cli(
            "learning-register", "--project-root", str(self.project), expected=2
        )
        self.assertEqual(BOTH_STATES, refused["error"])
        self.assertTrue(os.path.lexists(self.legacy))
        # Every other command uses the new folder; the old one is not special.
        self.assertEqual(self.target, LearningState(create=False).root)

    def test_only_the_new_folder_is_normal(self) -> None:
        LearningState(self.target)
        self.assertIsNone(migrate_legacy_state(CLI, self.project))
        self.assertEqual(self.target, LearningState(create=False).root)
        report = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertEqual((True, None), (report["ready"], report["state_moved"]))

    def test_a_new_folder_too_long_for_the_socket_refuses_before_copying(self) -> None:
        self.old_state(register=False)
        with mock.patch.object(learning_store, "UNIX_SOCKET_PATH_MAX", 20):
            with self.assertRaises(StateError) as caught:
                migrate_legacy_state(CLI, self.project)
        self.assertEqual(STATE_SOCKET_TOO_LONG, str(caught.exception))
        self.assertTrue(os.path.lexists(self.legacy))

    def test_an_unsafe_old_folder_is_not_copied(self) -> None:
        self.old_state(register=False)
        os.chmod(self.legacy, 0o755)
        with self.assertRaisesRegex(StateError, "unsafe permissions"):
            migrate_legacy_state(CLI, self.project)
        self.assertFalse(os.path.lexists(self.target))

    def test_no_old_folder_moves_nothing(self) -> None:
        self.assertIsNone(migrate_legacy_state(CLI, self.project))
        self.assertFalse(os.path.lexists(self.target))


if __name__ == "__main__":
    unittest.main()
