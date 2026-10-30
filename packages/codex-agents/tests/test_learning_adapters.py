from __future__ import annotations

import json
import unittest

from tests.support import ORCHESTRATION_ROOT

from codex_agents.learning_adapters import (
    load_client_capabilities,
    normalize_app_server,
    normalize_hook,
    reconcile_run,
)
from tests.learning_support import DIGEST_KEY


class LearningAdapterTests(unittest.TestCase):
    def test_hook_discards_prompt_paths_and_tool_content(self) -> None:
        payload = {
            "session_id": "thread-secret-id",
            "transcript_path": "/private/transcript.jsonl",
            "cwd": "/private/project",
            "hook_event_name": "PostToolUse",
            "model": "gpt-5.6-luna",
            "turn_id": "turn-secret-id",
            "tool_name": "Bash",
            "tool_use_id": "tool-secret-id",
            "tool_input": {"command": "echo SUPER_SECRET"},
            "tool_response": "SUPER_SECRET output",
        }
        record = normalize_hook(
            payload,
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            observed_at_ms=100,
        )
        encoded = json.dumps(record)
        for forbidden in ("SUPER_SECRET", "/private", "thread-secret", "turn-secret"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual("tool_output_observed", record["event_code"])
        self.assertEqual("lifecycle_partial", record["coverage"])

    def test_app_usage_uses_total_snapshot_without_double_counting(self) -> None:
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 2,
                        "cachedInputTokens": 1,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 4,
                    },
                    "total": {
                        "inputTokens": 100,
                        "cachedInputTokens": 25,
                        "outputTokens": 40,
                        "reasoningOutputTokens": 10,
                        "totalTokens": 140,
                    },
                    "modelContextWindow": 1000,
                },
            },
        }
        record = normalize_app_server(
            message,
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            coverage="root_turn_complete",
            observed_at_ms=100,
        )
        assert record is not None
        self.assertEqual(100, record["usage"]["input_tokens"])
        self.assertEqual(40, record["usage"]["output_tokens"])
        self.assertEqual(25, record["usage"]["cached_tokens"])
        self.assertIsNone(record["usage"]["model_calls"])

    def test_app_item_discards_commands_output_and_reasoning(self) -> None:
        record = normalize_app_server(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "completedAtMs": 100,
                    "item": {
                        "id": "item-1",
                        "type": "commandExecution",
                        "status": "completed",
                        "command": "cat PRIVATE",
                        "aggregatedOutput": "PRIVATE",
                        "durationMs": 3,
                    },
                },
            },
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            observed_at_ms=100,
        )
        assert record is not None
        self.assertNotIn("PRIVATE", json.dumps(record))
        self.assertEqual(1, record["usage"]["tool_calls"])

    def test_streaming_delta_is_ignored(self) -> None:
        self.assertIsNone(
            normalize_app_server(
                {"method": "item/agentMessage/delta", "params": {"delta": "secret"}},
                digest_key=DIGEST_KEY,
                client_version="codex-cli-0.153.0",
            )
        )

    def test_current_client_matrix_does_not_claim_child_usage(self) -> None:
        clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        cli = clients["codex-cli-0.153.0"]
        self.assertEqual("root_turn_complete", cli["app_server_coverage"])
        self.assertFalse(cli["child_usage"])

        hooks = clients["codex-hooks-v1"]
        self.assertEqual(["hook"], hooks["producers"])
        self.assertTrue(hooks["root_lifecycle"])
        self.assertTrue(hooks["child_lifecycle"])
        self.assertTrue(hooks["tool_lifecycle"])
        self.assertFalse(hooks["root_usage"])
        self.assertFalse(hooks["child_usage"])
        self.assertEqual(["host"], clients["codex-host-v1"]["producers"])

    def test_reconciliation_uses_app_terminal_and_never_promotes_root_only_usage(self) -> None:
        started = normalize_app_server(
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
            },
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            coverage="root_turn_complete",
            observed_at_ms=1,
        )
        usage = normalize_app_server(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 10,
                            "cachedInputTokens": 0,
                            "outputTokens": 5,
                            "reasoningOutputTokens": 0,
                            "totalTokens": 15,
                        }
                    },
                },
            },
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            coverage="root_turn_complete",
            observed_at_ms=2,
        )
        completed = normalize_app_server(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            coverage="root_turn_complete",
            observed_at_ms=3,
        )
        capability = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )["codex-cli-0.153.0"]
        result = reconcile_run(
            [
                {"source": "app_server", "record": item}
                for item in (started, usage, completed)
                if item is not None
            ],
            capability=capability,
        )
        self.assertEqual("root_turn_complete", result["coverage"])
        self.assertFalse(result["eligible_for_efficiency"])
        self.assertEqual("completed", result["terminal_status"])

    def test_decreasing_usage_marks_run_conflicting(self) -> None:
        def usage(total: int, observed: int) -> dict:
            record = normalize_app_server(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "tokenUsage": {
                            "total": {
                                "inputTokens": total,
                                "cachedInputTokens": 0,
                                "outputTokens": total,
                                "reasoningOutputTokens": 0,
                                "totalTokens": total * 2,
                            }
                        },
                    },
                },
                digest_key=DIGEST_KEY,
                client_version="codex-cli-0.153.0",
                observed_at_ms=observed,
            )
            assert record is not None
            return record

        capability = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )["codex-cli-0.153.0"]
        result = reconcile_run(
            [
                {"source": "app_server", "record": usage(20, 1)},
                {"source": "app_server", "record": usage(10, 2)},
            ],
            capability=capability,
        )
        self.assertEqual("conflicting", result["coverage"])


if __name__ == "__main__":
    unittest.main()
