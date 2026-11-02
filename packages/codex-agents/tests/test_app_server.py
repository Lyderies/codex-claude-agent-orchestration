from __future__ import annotations

import importlib.util
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from tests.support import REPOSITORY_ROOT


SPEC = importlib.util.spec_from_file_location(
    "codex_agents_app_server", REPOSITORY_ROOT / "scripts" / "check_app_server.py"
)
assert SPEC and SPEC.loader
app_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_server)


class AppServerTests(unittest.TestCase):
    def test_process_group_escalates_and_reaps_after_timeout(self) -> None:
        process = Mock()
        process.pid = 4321
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="codex app-server", timeout=0.1),
            0,
        ]
        with patch.object(app_server.os, "killpg") as killpg:
            app_server._stop_process_group(process, timeout=0.1)
        self.assertEqual(
            [
                unittest.mock.call(4321, signal.SIGTERM),
                unittest.mock.call(4321, signal.SIGKILL),
            ],
            killpg.call_args_list,
        )
        self.assertEqual(2, process.wait.call_count)

    def test_cleanup_stops_group_even_when_pipe_close_fails(self) -> None:
        process = Mock()
        process.stdin.close.side_effect = BrokenPipeError("closed")
        selector = Mock()
        with patch.object(app_server, "_stop_process_group") as stop:
            app_server._cleanup_process(process, selector)
        stop.assert_called_once_with(process)
        selector.close.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_selector_setup_failure_still_stops_process_group(self) -> None:
        process = Mock()
        process.stdin = Mock()
        process.stdout = Mock()
        process.stderr = Mock()
        with (
            patch.object(app_server.subprocess, "Popen", return_value=process),
            patch.object(
                app_server.selectors,
                "DefaultSelector",
                side_effect=OSError("selector unavailable"),
            ),
            patch.object(app_server, "_stop_process_group") as stop,
        ):
            with self.assertRaisesRegex(OSError, "selector unavailable"):
                app_server.check(REPOSITORY_ROOT)
        stop.assert_called_once_with(process)

    def test_partial_selector_registration_still_stops_process_group(self) -> None:
        process = Mock()
        process.stdin = Mock()
        process.stdout = Mock()
        process.stderr = Mock()
        selector = Mock()
        selector.register.side_effect = [None, OSError("register failed")]
        with (
            patch.object(app_server.subprocess, "Popen", return_value=process),
            patch.object(app_server.selectors, "DefaultSelector", return_value=selector),
            patch.object(app_server, "_stop_process_group") as stop,
        ):
            with self.assertRaisesRegex(OSError, "register failed"):
                app_server.check(REPOSITORY_ROOT)
        stop.assert_called_once_with(process)
        selector.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
