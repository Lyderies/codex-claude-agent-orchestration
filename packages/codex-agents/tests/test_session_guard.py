from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import session_guard
from codex_agents.session_guard import (
    NO_RECORD,
    NO_THREAD,
    SessionRefused,
    check_current_session,
    scan_recent_sessions,
    session_guard_status,
)

THREAD = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
OTHER_THREAD = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5c"


def path_entry(path: Path, access: str = "write", **extra) -> dict:
    return {"path": {"type": "path", "path": str(path)}, "access": access, **extra}


def special_entry(kind: str, access: str = "write") -> dict:
    return {"path": {"type": "special", "value": {"kind": kind}}, "access": access}


def workspace_entries(workspace: Path, *extra: dict) -> list[dict]:
    """What Codex 0.164 records for a workspace-write session on `workspace`."""

    return [
        special_entry("root", "read"),
        path_entry(workspace),
        special_entry("slash_tmp"),
        special_entry("tmpdir"),
        *(path_entry(workspace / name, "read", missing_path_behavior="skip")
          for name in (".git", ".agents", ".codex")),
        *extra,
    ]


def record_line(kind: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": "2026-11-01T10:00:00.000Z", "type": kind, "payload": payload},
        separators=(",", ":"),
    ) + "\n"


class SessionFiles:
    """Synthetic Codex 0.164 rollout files in a scratch home."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def write(
        self,
        workspace: Path,
        *,
        thread: str = THREAD,
        entries: list[dict] | None = None,
        sandbox: str = "workspace-write",
        roots: list[str] | None = None,
        day: tuple[str, str, str] = ("2026", "11", "01"),
        before: str = "",
        after: str = "",
        meta: dict | None = None,
    ) -> Path:
        folder = self.home / ".codex" / "sessions" / day[0] / day[1] / day[2]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-{day[0]}-{day[1]}-{day[2]}T10-00-00-{thread}.jsonl"
        payload = meta if meta is not None else {
            "id": thread, "cwd": str(workspace),
            "runtime_workspace_roots": roots if roots is not None else [str(workspace)],
        }
        context = {
            "cwd": str(workspace),
            "sandbox_policy": {"type": sandbox},
            "file_system_sandbox_policy": {
                "entries": entries if entries is not None else workspace_entries(workspace)
            },
        }
        path.write_text(
            record_line("session_meta", payload)
            + before
            + record_line("turn_context", context)
            + after
        )
        return path


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.home / "Work" / "project"
        self.project.mkdir(parents=True)
        self.clone = self.home / "Tools" / "orchestration-clone"
        self.clone.mkdir(parents=True)
        self.copy = self.home / ".codex" / "agent-orchestration" / "abc" / ("0" * 64)
        self.sessions = SessionFiles(self.home)
        # The scratch home sits in the system temp folder, so the sandbox's /tmp
        # and TMPDIR stand-ins are folders beside it.
        (self.base / "tmp").mkdir()
        (self.base / "tmpdir").mkdir()
        for patcher in (
            mock.patch.object(session_guard, "account_home", lambda: self.home),
            mock.patch.object(session_guard, "SLASH_TMP", str(self.base / "tmp")),
            mock.patch.dict(os.environ, {
                "CODEX_THREAD_ID": THREAD, "TMPDIR": str(self.base / "tmpdir"),
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self) -> None:
        check_current_session(self.copy, self.clone)

    def assert_refused(self, text: str) -> None:
        with self.assertRaises(SessionRefused) as caught:
            self.check()
        self.assertIn(text, str(caught.exception))
        self.assertNotIn(str(self.base), str(caught.exception))


class SessionGuardTests(GuardTestCase):
    def test_a_project_workspace_is_accepted(self) -> None:
        self.sessions.write(self.project)
        self.check()
        self.assertEqual("ok", session_guard_status(self.copy, self.clone))

    def test_a_read_only_session_is_accepted(self) -> None:
        self.sessions.write(
            self.project, sandbox="read-only", entries=[special_entry("root", "read")],
            roots=[str(self.project)],
        )
        self.check()

    def test_the_home_folder_as_workspace_is_refused(self) -> None:
        self.sessions.write(self.home)
        self.assert_refused("can write to your home folder")

    def test_a_folder_above_the_home_folder_is_refused(self) -> None:
        self.sessions.write(self.base)
        self.assert_refused("holds your home folder")
        self.sessions.write(Path("/"), thread=THREAD)
        self.assert_refused("whole disk")

    def test_a_workspace_holding_the_clone_is_refused(self) -> None:
        self.sessions.write(self.home / "Tools")
        self.assert_refused("orchestrator's setup folder")
        self.sessions.write(self.clone / "packages")
        self.assert_refused("orchestrator's setup folder")

    def test_an_extra_writable_tools_folder_is_refused(self) -> None:
        # As `codex --add-dir ~/.local` records it: one more writable entry and root.
        local = self.home / ".local"
        self.sessions.write(
            self.project, entries=workspace_entries(self.project, path_entry(local)),
            roots=[str(self.project), str(local)],
        )
        self.assert_refused("trusted tools are installed")
        self.sessions.write(
            self.project, entries=workspace_entries(self.project, path_entry(Path("/opt/homebrew"))),
        )
        self.assert_refused("trusted tools are installed")
        self.sessions.write(
            self.project, entries=workspace_entries(self.project),
            roots=[str(self.project), str(self.home / ".codex")],
        )
        self.assert_refused("~/.codex")

    def test_a_writable_root_or_home_entry_is_refused(self) -> None:
        self.sessions.write(
            self.project, entries=workspace_entries(self.project, special_entry("root"))
        )
        self.assert_refused("whole disk")
        self.sessions.write(
            self.project, entries=workspace_entries(self.project, special_entry("home"))
        )
        self.assert_refused("your home folder")
        self.sessions.write(
            self.project, entries=workspace_entries(self.project, special_entry("elsewhere"))
        )
        self.assert_refused("not in the format")

    def test_a_clone_swapped_for_a_symlink_stays_protected(self) -> None:
        # A session on ~/Work holds the clone; the model renames the clone and leaves a
        # symlink to an unrelated folder at its recorded path.
        work_clone = self.home / "Work" / "orchestrator"
        work_clone.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        work_clone.rename(self.home / "Work" / "moved")
        work_clone.symlink_to(elsewhere)
        self.sessions.write(self.home / "Work")
        with self.assertRaisesRegex(SessionRefused, "orchestrator's setup folder"):
            check_current_session(self.copy, work_clone)
        # The folder the recorded path now resolves to is protected as well.
        self.sessions.write(elsewhere)
        with self.assertRaisesRegex(SessionRefused, "orchestrator's setup folder"):
            check_current_session(self.copy, work_clone)
        # A session on the project folder alone still passes.
        self.sessions.write(self.project)
        check_current_session(self.copy, work_clone)

    def test_a_different_spelling_of_a_protected_folder_is_refused(self) -> None:
        # On a case-insensitive volume `work` and `Work` are one folder; elsewhere the
        # spelling is compared ignoring case, so the guard refuses when unsure.
        clone = self.home / "Work" / "orchestrator"
        clone.mkdir()
        self.sessions.write(self.home / "work")
        with self.assertRaisesRegex(SessionRefused, "orchestrator's setup folder"):
            check_current_session(self.copy, clone)
        for variant, reason in (
            (self.home / ".LOCAL" / "bin", "trusted tools are installed"),
            (self.home / ".Codex", "~/.codex"),
            (Path("/OPT/HOMEBREW"), "trusted tools are installed"),
            (Path(str(self.home).upper()), "home folder"),
        ):
            with self.subTest(variant=variant):
                self.sessions.write(
                    self.project, entries=workspace_entries(self.project, path_entry(variant))
                )
                self.assert_refused(reason)

    def test_read_only_workspace_roots_are_not_writable(self) -> None:
        # A read-only session reviewing the clone lists it as a workspace root but
        # cannot write it: only explicit write entries count.
        self.sessions.write(
            self.clone, sandbox="read-only", entries=[
                special_entry("root", "read"), path_entry(self.clone, "read"),
            ],
        )
        self.check()
        self.assertEqual([], scan_recent_sessions(self.clone, self.home / ".codex", self.home))
        # An explicit write entry still counts in read-only mode.
        self.sessions.write(
            self.clone, sandbox="read-only",
            entries=[special_entry("root", "read"), path_entry(self.home)],
        )
        self.assert_refused("your home folder")
        # And in workspace-write mode a workspace root counts as writable.
        self.sessions.write(
            self.project, entries=workspace_entries(self.project), roots=[str(self.clone)],
        )
        self.assert_refused("orchestrator's setup folder")

    def test_danger_full_access_is_refused(self) -> None:
        self.sessions.write(self.project, sandbox="danger-full-access", entries=[])
        self.assert_refused("danger-full-access")
        self.sessions.write(self.project, sandbox="external-sandbox")
        self.assert_refused("not in the format")

    def test_a_missing_record_or_thread_id_is_refused(self) -> None:
        # `codex exec --ephemeral` writes no record.
        self.assert_refused(NO_RECORD)
        self.sessions.write(self.project, thread=OTHER_THREAD)
        self.assert_refused(NO_RECORD)
        for value in ("", "tests-thread", THREAD.upper(), THREAD + "0"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CODEX_THREAD_ID": value}):
                with self.assertRaises(SessionRefused):
                    self.check()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_refused(NO_THREAD)

    def test_several_records_for_one_session_are_refused(self) -> None:
        self.sessions.write(self.project)
        self.sessions.write(self.project, day=("2026", "10", "31"))
        self.assert_refused("Several Codex session records")

    def test_malformed_records_are_refused(self) -> None:
        path = self.sessions.write(self.project)
        good = path.read_text()
        for broken in (
            "not json\n" + good.split("\n", 1)[1],
            good.replace('"type":"session_meta"', '"type":"response_item"', 1),
            good.replace('"runtime_workspace_roots":[', '"runtime_workspace_roots":{"x":[', 1)
            .replace("]", "]}", 1),
            good.replace('"entries":[', '"entries":{"x":[', 1),
            good.replace('"type":"turn_context"', '"type":"event_msg"'),
            good.replace('"access":"write"', '"access":1', 1),
            good.replace('"path":{"type":"path"', '"path":{"type":"glob"', 1),
        ):
            with self.subTest(broken=broken[:60]):
                path.write_text(broken)
                self.assert_refused("not in the format")
        # A symlinked record is never followed.
        target = self.base / "elsewhere.jsonl"
        target.write_text(good)
        path.unlink()
        path.symlink_to(target)
        self.assert_refused("not in the format")

    def test_the_last_turn_context_is_read_from_the_end_of_a_large_record(self) -> None:
        # Megabytes of conversation before and after the settings of the current
        # turn: only the last turn_context counts, and the reader never needs more
        # than the tail.
        filler = record_line("response_item", {"type": "message", "text": "x" * 1_000_000})
        earlier = record_line("turn_context", {
            "cwd": str(self.project), "sandbox_policy": {"type": "workspace-write"},
            "file_system_sandbox_policy": {"entries": workspace_entries(self.home)},
        })
        self.sessions.write(self.project, before=earlier + filler * 6, after=filler * 3)
        self.check()
        # The other way round: the current turn writes the home folder.
        last = earlier
        self.sessions.write(self.project, before=filler * 2, after=filler * 2 + last)
        self.assert_refused("your home folder")
        # Settings further back than the bound fail closed.
        with mock.patch.object(session_guard, "MAX_TAIL_BYTES", 1_000_000):
            self.sessions.write(self.project, after=filler * 3)
            self.assert_refused("not in the format")


class SessionScanTests(GuardTestCase):
    def test_recent_sessions_covering_the_clone_or_home_are_listed(self) -> None:
        self.sessions.write(self.project, thread=OTHER_THREAD, day=("2026", "11", "02"))
        self.assertEqual([], scan_recent_sessions(self.clone, self.home / ".codex", self.home))
        self.sessions.write(self.home / "Tools", day=("2026", "11", "01"))
        third = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5d"
        self.sessions.write(self.home, thread=third, day=("2026", "10", "30"))
        findings = scan_recent_sessions(self.clone, self.home / ".codex", self.home)
        self.assertEqual(
            [{"date": "2026-11-01", "folder": str(self.home / "Tools"), "holds": "clone"},
             {"date": "2026-10-30", "folder": str(self.home), "holds": "home"}],
            findings,
        )
        # Records not changed for more than two weeks are not read.
        later = time.time() + 15 * 86400
        self.assertEqual(
            [], scan_recent_sessions(self.clone, self.home / ".codex", self.home, now=later)
        )

    def test_unreadable_records_are_skipped(self) -> None:
        path = self.sessions.write(self.home)
        path.write_text("not json\n")
        self.assertEqual([], scan_recent_sessions(self.clone, self.home / ".codex", self.home))


if __name__ == "__main__":
    unittest.main()
