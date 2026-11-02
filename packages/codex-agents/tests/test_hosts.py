from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import unittest

import tests.support  # noqa: F401

from codex_agents.hosts import (
    CLAUDE_HOST_CLIENT,
    CODEX_HOST_CLIENT,
    HostClientUnknown,
    detect_host_client,
    resolve_host_client,
)

CODEX = {"CODEX_THREAD_ID": "t", "CODEX_SESSION_ID": "s", "CODEX_SANDBOX": "seatbelt"}
CLAUDE = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "session"}


class HostDetectionTests(unittest.TestCase):
    def test_each_marker_combination(self) -> None:
        cases = (
            (CODEX, CODEX_HOST_CLIENT),
            ({"CODEX_SESSION_ID": "s"}, CODEX_HOST_CLIENT),
            (CLAUDE, CLAUDE_HOST_CLIENT),
            ({**CLAUDE, "CODEX_SANDBOX": "seatbelt"}, CLAUDE_HOST_CLIENT),
            ({**CODEX, **CLAUDE}, None),
            ({}, None),
            ({"CODEX_SANDBOX": "seatbelt"}, None),
            ({"CLAUDECODE": "1"}, None),
            ({"CLAUDECODE": "0", "CLAUDE_CODE_SESSION_ID": "s"}, None),
            ({**CODEX, "CLAUDECODE": "0"}, None),
        )
        for environ, expected in cases:
            with self.subTest(environ=sorted(environ)):
                if expected is None:
                    with self.assertRaisesRegex(HostClientUnknown, "host_client_unknown"):
                        detect_host_client(environ)
                else:
                    self.assertEqual(expected, detect_host_client(environ))

    def test_explicit_host_must_match_the_detected_one(self) -> None:
        self.assertEqual(
            CLAUDE_HOST_CLIENT, resolve_host_client(CLAUDE, CLAUDE_HOST_CLIENT)
        )
        with self.assertRaisesRegex(HostClientUnknown, "host_client_unknown"):
            resolve_host_client(CLAUDE, CODEX_HOST_CLIENT)
        with self.assertRaisesRegex(HostClientUnknown, "host_client_unknown"):
            resolve_host_client({}, CODEX_HOST_CLIENT)


if __name__ == "__main__":
    unittest.main()
