"""Adversarial tests for the operational-QA temporary-tree monitor."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT  # noqa: F401

from codex_agents import learning_engine
from codex_agents.contracts import ContractError


class FakeProcess:
    def __init__(self, polls: list[int | None]) -> None:
        self.polls = iter(polls)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        value = next(self.polls, self.returncode)
        if value is not None:
            self.returncode = value
        return value


class QATreeScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "work"
        self.pytest_root = self.work / "pytest"
        self.pytest_root.mkdir(parents=True)
        self.target = self.pytest_root / "target"
        self.target.write_text("ok", encoding="utf-8")

    def current_symlink(self) -> Path:
        current = self.pytest_root / "current"
        current.symlink_to(self.target)
        return current

    def wait(self, process: FakeProcess, timeout_seconds: int = 10) -> tuple[str, int | None]:
        return learning_engine._wait_for_qa_process(
            process,
            work=self.work,
            timeout_seconds=timeout_seconds,
            allowed_symlink_root=self.pytest_root,
        )

    def test_disappeared_listed_entry_gets_one_new_scan(self) -> None:
        self.current_symlink()
        original = os.stat
        vanished = False

        def racing_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            nonlocal vanished
            if path == "current" and kwargs.get("dir_fd") is not None and not vanished:
                vanished = True
                os.unlink(path, dir_fd=kwargs["dir_fd"])
                raise FileNotFoundError(errno.ENOENT, "entry vanished")
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "stat", side_effect=racing_stat), patch.object(
            learning_engine.time, "sleep"
        ):
            self.assertEqual(("passed", 0), self.wait(FakeProcess([0])))
        self.assertTrue(vanished)

    def test_readlink_einval_after_replacement_gets_new_scan(self) -> None:
        current = self.current_symlink()
        original = os.readlink
        replaced = False

        def racing_readlink(path: str, *args: object, **kwargs: object) -> str:
            nonlocal replaced
            if path == "current" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                current.unlink()
                current.write_text("new", encoding="utf-8")
                raise OSError(errno.EINVAL, "entry changed")
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "readlink", side_effect=racing_readlink), patch.object(
            learning_engine.time, "sleep"
        ):
            self.assertEqual(("passed", 0), self.wait(FakeProcess([0])))
        self.assertTrue(replaced)

    def test_entry_that_reappears_after_stat_enoent_gets_new_scan(self) -> None:
        current = self.current_symlink()
        original = os.stat
        replaced = False

        def racing_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            nonlocal replaced
            if path == "current" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                current.unlink()
                try:
                    original(path, *args, **kwargs)
                except FileNotFoundError:
                    current.write_text("replacement", encoding="utf-8")
                    raise
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "stat", side_effect=racing_stat), patch.object(
            learning_engine.time, "sleep"
        ):
            self.assertEqual(("passed", 0), self.wait(FakeProcess([0])))
        self.assertTrue(replaced)

    def test_symlink_disappearance_during_postcheck_gets_new_scan(self) -> None:
        current = self.current_symlink()
        original = os.readlink
        calls = 0

        def racing_readlink(path: str, *args: object, **kwargs: object) -> str:
            nonlocal calls
            if path == "current" and kwargs.get("dir_fd") is not None:
                calls += 1
                if calls == 2:
                    current.unlink()
                    raise FileNotFoundError(errno.ENOENT, "entry vanished")
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "readlink", side_effect=racing_readlink), patch.object(
            learning_engine.time, "sleep"
        ):
            self.assertEqual(("passed", 0), self.wait(FakeProcess([0])))
        self.assertEqual(2, calls)

    def test_persistent_outside_symlink_fails_closed(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("private", encoding="utf-8")
        (self.pytest_root / "current").symlink_to(outside)
        with self.assertRaisesRegex(ContractError, "storage is invalid"):
            self.wait(FakeProcess([0]))

    def test_replacement_with_outside_symlink_fails_on_stable_scan(self) -> None:
        current = self.current_symlink()
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("private", encoding="utf-8")
        original = os.readlink
        replaced = False

        def racing_readlink(path: str, *args: object, **kwargs: object) -> str:
            nonlocal replaced
            if path == "current" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                current.unlink()
                current.symlink_to(outside)
                raise OSError(errno.EINVAL, "entry changed")
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "readlink", side_effect=racing_readlink), patch.object(
            learning_engine.time, "sleep"
        ), self.assertRaisesRegex(ContractError, "storage is invalid"):
            self.wait(FakeProcess([0]))

    def test_unchanged_entry_io_fault_fails_closed(self) -> None:
        self.current_symlink()
        with patch.object(
            learning_engine.os, "readlink",
            side_effect=PermissionError(errno.EACCES, "denied"),
        ), self.assertRaisesRegex(ContractError, "storage is invalid"):
            self.wait(FakeProcess([0]))

    def test_changed_entry_does_not_hide_unrelated_readlink_faults(self) -> None:
        for fault in (errno.EACCES, errno.EIO):
            with self.subTest(fault=fault):
                current = self.current_symlink()
                original = os.readlink
                calls = 0

                def failing_readlink(path: str, *args: object, **kwargs: object) -> str:
                    nonlocal calls
                    if path == "current" and kwargs.get("dir_fd") is not None:
                        calls += 1
                        current.unlink()
                        current.write_text("replacement", encoding="utf-8")
                        raise OSError(fault, "unrelated I/O fault")
                    return original(path, *args, **kwargs)

                with patch.object(
                    learning_engine.os, "readlink", side_effect=failing_readlink
                ), self.assertRaisesRegex(ContractError, "storage is invalid"):
                    self.wait(FakeProcess([0]))
                self.assertEqual(1, calls)
                current.unlink()

    def test_unrelated_stat_io_fault_is_not_transient(self) -> None:
        self.current_symlink()
        original = os.stat

        def failed_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if path == "current" and kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "failed")
            return original(path, *args, **kwargs)

        with patch.object(learning_engine.os, "stat", side_effect=failed_stat), self.assertRaises(OSError):
            self.wait(FakeProcess([0]))

    def test_byte_and_entry_caps_still_fail_closed(self) -> None:
        (self.work / "second").write_text("more", encoding="utf-8")
        for cap in ("MAX_QA_TEMP_BYTES", "MAX_QA_TEMP_ENTRIES"):
            with self.subTest(cap=cap), patch.object(learning_engine, cap, 1), self.assertRaisesRegex(
                ContractError, "exceeds its bounds"
            ):
                learning_engine._bounded_tree_size(self.work)

    def test_exit_requires_a_scan_started_after_exit(self) -> None:
        process = FakeProcess([None, 0])
        scans: list[bool] = []

        def scan(*_args: object, **_kwargs: object) -> int:
            scans.append(process.returncode is not None)
            if len(scans) == 1:
                raise learning_engine._TransientQATreeScan
            return 0

        with patch.object(learning_engine, "_bounded_tree_size", side_effect=scan), patch.object(
            learning_engine.time, "sleep"
        ):
            self.assertEqual(("passed", 0), self.wait(process))
        self.assertEqual([False, True], scans)

    def test_unstable_postexit_scans_fail_after_bound(self) -> None:
        with patch.object(
            learning_engine, "_bounded_tree_size", side_effect=learning_engine._TransientQATreeScan
        ) as scan, patch.object(learning_engine.time, "sleep"), self.assertRaisesRegex(
            ContractError, "storage is unstable"
        ):
            self.wait(FakeProcess([0]))
        self.assertEqual(learning_engine.MAX_QA_TRANSIENT_TREE_SCANS + 1, scan.call_count)

    def test_retry_cannot_pass_after_deadline(self) -> None:
        with patch.object(
            learning_engine, "_bounded_tree_size", side_effect=learning_engine._TransientQATreeScan
        ) as scan, patch.object(
            learning_engine.time, "monotonic", side_effect=[0.0, 0.9, 1.0]
        ), patch.object(learning_engine.time, "sleep"), self.assertRaisesRegex(
            ContractError, "storage is unstable"
        ):
            self.wait(FakeProcess([0]), timeout_seconds=1)
        self.assertEqual(1, scan.call_count)

    def test_failed_child_and_active_timeout_keep_their_status(self) -> None:
        self.assertEqual(("failed", 3), self.wait(FakeProcess([3])))
        with patch.object(learning_engine.time, "monotonic", side_effect=[0.0, 1.0]):
            self.assertEqual(("timeout", None), self.wait(FakeProcess([None]), timeout_seconds=1))


if __name__ == "__main__":
    unittest.main()
