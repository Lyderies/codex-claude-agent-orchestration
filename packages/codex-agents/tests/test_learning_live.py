from __future__ import annotations

import importlib.util
import json
import unittest

from tests.support import REPOSITORY_ROOT


SPEC = importlib.util.spec_from_file_location(
    "codex_agents_learning_live", REPOSITORY_ROOT / "scripts" / "check_learning_live.py"
)
assert SPEC and SPEC.loader
learning_live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(learning_live)


class LearningLiveTests(unittest.TestCase):
    def test_fixture_has_same_required_event_classes_as_live_check(self) -> None:
        messages = json.loads(
            (
                REPOSITORY_ROOT
                / "tests"
                / "fixtures"
                / "learning"
                / "app-server-events.json"
            ).read_text()
        )
        report = learning_live.normalize_messages(messages, b"f" * 32)
        self.assertTrue(report["ready"])
        self.assertTrue(report["fixture_event_classes_matched"])
        self.assertEqual("root_turn_complete", report["coverage"])
        self.assertFalse(report["eligible_for_efficiency"])
        self.assertFalse(report["raw_content_persisted"])


if __name__ == "__main__":
    unittest.main()
