from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import contextlib
import errno
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT

from codex_agents import host_paths, learning_store
from codex_agents.learning_store import (
    BOTH_STATES,
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
    """The default learning state comes from the account database, on every platform."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        self.account = self.base / "account"
        self.account.mkdir()
        self.other = self.base / "other-home"
        self.other.mkdir()

    def resolve(self, platform: str, **environ: str) -> tuple[Path, Path | None]:
        environment = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
        environment.update(HOME=str(self.other), **environ)
        with mock.patch.object(host_paths.pwd, "getpwuid", lambda _uid: _account(self.account)), \
                mock.patch.object(host_paths.sys, "platform", platform), \
                mock.patch.dict(os.environ, environment, clear=True):
            return default_state_root(), host_paths.legacy_state_root()

    def test_macos_and_linux_use_the_account_homes_local_state(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                default, _legacy = self.resolve(platform)
                # Never the overridden HOME, and the same hidden folder on both.
                self.assertEqual(self.account / ".local" / "state" / "codex-agents", default)

    def test_xdg_state_home_is_honoured_when_absolute(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                default, _ = self.resolve(platform, XDG_STATE_HOME=str(self.base / "xdg"))
                self.assertEqual(self.base / "xdg" / "codex-agents", default)
                default, _ = self.resolve(platform, XDG_STATE_HOME="relative/state")
                self.assertEqual(self.account / ".local" / "state" / "codex-agents", default)

    def test_only_macos_has_an_old_state_folder(self) -> None:
        _default, legacy = self.resolve("darwin")
        # Where the pre-0.8.9 code put it: HOME's Library/Application Support.
        self.assertEqual(self.other / "Library" / "Application Support" / "codex-agents", legacy)
        self.assertIsNone(self.resolve("linux")[1])


class LegacyStateMoveTests(unittest.TestCase):
    """The one-time, owner-driven move of the pre-0.8.9 macOS state folder."""

    def setUp(self) -> None:
        # Short: the collector's socket path is limited to about 100 bytes.
        self.base = Path(tempfile.mkdtemp(prefix="mv-", dir=os.path.realpath("/tmp")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "h"
        self.legacy = self.home / "Library" / "Application Support" / "codex-agents"
        self.target = self.base / "x" / "codex-agents"
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

    def test_registration_stops_the_collector_then_moves_the_folder(self) -> None:
        state = self.old_state()
        ensure_collector(state, CLI, project_root=self.project)
        CollectorClient(self.legacy / "collector.sock").health()
        before = (self.legacy / "digest.key").read_bytes()
        real_rename = os.rename
        renamed: list[bool] = []

        def checking_rename(source, destination, *args, **kwargs):
            if Path(source) == self.legacy:
                # The old collector answers no more when the folder moves.
                with self.assertRaises(CollectorError):
                    CollectorClient(self.legacy / "collector.sock", timeout=0.2).health()
                renamed.append(True)
            return real_rename(source, destination, *args, **kwargs)

        with mock.patch.object(learning_store.os, "rename", checking_rename):
            report = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertEqual([True], renamed)
        self.assertTrue(report["ready"])
        self.assertTrue(report["state_moved"])
        self.assertFalse(os.path.lexists(self.legacy))
        # The same state, owner-only, now at the default folder, with the
        # collector running there.
        self.assertEqual(before, (self.target / "digest.key").read_bytes())
        self.assertEqual(0o700, stat.S_IMODE(self.target.stat().st_mode))
        moved = LearningState()
        self.assertEqual(self.target, moved.root)
        # The registration the old ledger held is still there.
        CollectorClient(moved.socket).health(self.project)
        self.assertEqual([self.project.resolve()], registered_project_roots())
        # Only once: a later registration finds the new folder alone.
        again = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertFalse(again["state_moved"])

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
        self.assertFalse(migrate_legacy_state(CLI, self.project))
        self.assertEqual(self.target, LearningState(create=False).root)
        report = self.run_cli("learning-register", "--project-root", str(self.project))
        self.assertEqual((True, False), (report["ready"], report["state_moved"]))

    def test_a_move_that_cannot_happen_in_one_step_refuses_and_keeps_the_old_folder(self) -> None:
        self.old_state(register=False)

        def cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        with mock.patch.object(learning_store.os, "rename", cross_device):
            with self.assertRaises(StateError) as caught:
                migrate_legacy_state(CLI, self.project)
        self.assertEqual(STATE_MOVE_FAILED, str(caught.exception))
        self.assertTrue(os.path.lexists(self.legacy))
        self.assertFalse(os.path.lexists(self.target))

    def test_a_new_folder_too_long_for_the_socket_refuses_before_moving(self) -> None:
        self.old_state(register=False)
        with mock.patch.object(learning_store, "UNIX_SOCKET_PATH_MAX", 20):
            with self.assertRaises(StateError) as caught:
                migrate_legacy_state(CLI, self.project)
        self.assertEqual(STATE_SOCKET_TOO_LONG, str(caught.exception))
        self.assertTrue(os.path.lexists(self.legacy))

    def test_an_unsafe_old_folder_is_not_moved(self) -> None:
        self.old_state(register=False)
        os.chmod(self.legacy, 0o755)
        with self.assertRaisesRegex(StateError, "unsafe permissions"):
            migrate_legacy_state(CLI, self.project)
        self.assertFalse(os.path.lexists(self.target))

    def test_no_old_folder_moves_nothing(self) -> None:
        self.assertFalse(migrate_legacy_state(CLI, self.project))
        self.assertFalse(os.path.lexists(self.target))


if __name__ == "__main__":
    unittest.main()
