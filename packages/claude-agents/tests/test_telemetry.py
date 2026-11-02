from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError

from claude_agents.telemetry import TelemetryError, parse_no_tool_stream

MODEL = "claude-sonnet-5-5"


def frames() -> list:
    """Synthetic shape from current fake-cli-v5-sonnet-success/stdout.jsonl."""
    tokens = {"input_tokens": 20, "output_tokens": 1,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    model_usage = {
        "inputTokens": 20, "outputTokens": 1, "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0, "canonicalModel": MODEL,
        "provider": "firstParty", "costBasis": "list", "costUSD": 0.00005,
        "webSearchRequests": 0, "thinkingTokens": 0,
        "contextWindow": 1000000, "maxOutputTokens": 128000,
    }
    return [
        {"type": "system", "subtype": "init", "session_id": "session-1",
         "model": MODEL, "tools": [], "mcp_servers": [], "skills": [], "plugins": [],
         "apiKeySource": "ANTHROPIC_API_KEY", "agents": ["claude", "Explore", "Plan"]},
        {"type": "assistant", "session_id": "session-1", "parent_tool_use_id": None,
         "message": {"id": "msg_synthetic", "type": "message", "role": "assistant",
                     "model": MODEL, "content": [{"type": "text", "text": "OK"}],
                     "stop_reason": None, "stop_sequence": None,
                     "usage": {**tokens, "output_tokens": 0}}},
        {"type": "result", "subtype": "success", "session_id": "session-1",
         "is_error": False, "num_turns": 1, "stop_reason": "end_turn",
         "terminal_reason": "completed", "result": "OK",
         "usage": {**tokens, "output_tokens_details": {"thinking_tokens": 0},
                   "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                      "ephemeral_5m_input_tokens": 0},
                   "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                   "iterations": [], "fallback_credit": None},
         "modelUsage": {MODEL: model_usage}, "total_cost_usd": 0.00005,
         "permission_denials": [], "safety_stops": 0, "queued_turn_count": 0,
         "result_index": 0, "subagent_stats": {"spawned": 0, "requested": {
             "foreground": 0, "background": 0}, "by_type": {}},
         "model_usage_since_last_result": {MODEL: deepcopy(model_usage)}},
    ]


def stream(value=None) -> str:
    return "\n".join(json.dumps(v) for v in (frames() if value is None else value)) + "\n"


class TelemetryTests(unittest.TestCase):
    def parse(self, value=None, **kwargs):
        return parse_no_tool_stream(stream(value), requested_model=MODEL, **kwargs)

    def test_actual_nested_assistant_shape_and_distinct_coverage_are_retained(self) -> None:
        data = frames()
        data[1]["message"]["usage"]["input_tokens"] = 10
        data[2]["usage"]["input_tokens"] = 19
        data[2]["usage"]["cache_read_input_tokens"] = 100
        observed = self.parse(data)
        self.assertEqual(observed.assistant_usage.input_tokens, 10)
        self.assertEqual(observed.assistant_usage.output_tokens, 0)
        self.assertEqual(observed.terminal_root_usage.input_tokens, 19)
        self.assertEqual(observed.terminal_root_usage.cache_read_input_tokens, 100)
        self.assertEqual(observed.model_usage.tokens.input_tokens, 20)
        self.assertEqual(observed.model_usage.client_list_cost_usd, 0.00005)
        self.assertEqual((observed.assistant_frame_count, observed.terminal_frame_count), (1, 1))
        self.assertFalse(observed.runtime_enabled)
        self.assertFalse(observed.billing_verified)
        self.assertIsNone(observed.subscription_cost_usd)
        self.assertIsNone(observed.quota_remaining)
        with self.assertRaises(FrozenInstanceError):
            observed.runtime_enabled = True

    def test_missing_duplicate_extra_and_out_of_order_frames_fail(self) -> None:
        data = frames()
        for changed in (data[:1] + data[2:], data + [data[2]], [data[1], data[0], data[2]],
                        data[:2], data[:1] + [data[1], data[1], data[2]],
                        data[:2] + [{"type": "user"}]):
            with self.assertRaises(TelemetryError):
                self.parse(changed)

    def test_exact_init_assistant_key_canonical_and_provider_are_required(self) -> None:
        mutations = [
            lambda d: d[0].update(model="other"),
            lambda d: d[1]["message"].update(model="other"),
            lambda d: d[1].update(message={"model": MODEL}),
            lambda d: d[2]["modelUsage"].update({"other": d[2]["modelUsage"][MODEL]}),
            lambda d: d[2]["modelUsage"][MODEL].update(canonicalModel="other"),
            lambda d: d[2]["modelUsage"][MODEL].update(provider="bedrock"),
            lambda d: d[2]["modelUsage"][MODEL].update(costBasis="billed"),
            lambda d: d[2]["model_usage_since_last_result"][MODEL].update(provider="other"),
            lambda d: d[1].update(session_id="other"),
        ]
        for mutate in mutations:
            data = frames()
            mutate(data)
            with self.assertRaises(TelemetryError):
                self.parse(data)

    def test_tools_plugins_child_ids_and_server_activity_are_denied(self) -> None:
        for field in ("tools", "skills", "plugins", "mcp_servers"):
            data = frames()
            data[0][field] = ["unexpected"]
            with self.assertRaises(TelemetryError):
                self.parse(data)
        for field in ("parent_tool_use_id", "parent_agent_id", "agent_id"):
            data = frames()
            data[1][field] = "child"
            with self.assertRaises(TelemetryError):
                self.parse(data)
        mutations = [
            lambda d: d[1]["message"].update(content=[{"type": "tool_use", "name": "Bash"}]),
            lambda d: d[2]["usage"]["server_tool_use"].update(web_search_requests=1),
            lambda d: d[2]["subagent_stats"].update(spawned=1),
            lambda d: d[2].update(permission_denials=[{"tool": "Bash"}]),
            lambda d: d[2]["usage"].update(fallback_credit={"credit": 1}),
        ]
        for mutate in mutations:
            data = frames()
            mutate(data)
            with self.assertRaises(TelemetryError):
                self.parse(data)
        with self.assertRaises(TelemetryError):
            self.parse(expected_plugins=("plugin",))

    def test_error_budget_refusal_stop_and_excess_turns_are_not_success(self) -> None:
        for change in ({"is_error": True}, {"is_error": 0},
                       {"subtype": "error_max_budget_usd"}, {"stop_reason": "refusal"},
                       {"num_turns": 2}, {"num_turns": True}, {"num_turns": 0},
                       {"result": "DIFFERENT"}, {"errors": ["synthetic error"]}):
            data = frames()
            data[2].update(change)
            with self.assertRaises(TelemetryError):
                self.parse(data)

    def test_usage_fields_are_strict_bounded_and_no_unknown_is_zero(self) -> None:
        for changed in (True, -1, 257, 1.5, None):
            data = frames()
            data[2]["modelUsage"][MODEL]["outputTokens"] = changed
            with self.assertRaises(TelemetryError):
                self.parse(data)
        for location in ("assistant", "root", "model"):
            data = frames()
            value = (data[1]["message"]["usage"] if location == "assistant"
                     else data[2]["usage"] if location == "root"
                     else data[2]["modelUsage"][MODEL])
            del value["inputTokens" if location == "model" else "input_tokens"]
            with self.assertRaises(TelemetryError):
                self.parse(data)
        for cost in (True, -1, float("nan"), float("inf"), 10**7):
            data = frames()
            data[2]["modelUsage"][MODEL]["costUSD"] = cost
            with self.assertRaises(TelemetryError):
                self.parse(data)
        with self.assertRaises(TelemetryError):
            self.parse(max_turns=True)
        with self.assertRaises(TelemetryError):
            self.parse(max_output_tokens=257)

    def test_bounded_unknown_metadata_is_ignored_but_never_trusted(self) -> None:
        data = frames()
        data[0]["future_metadata"] = {"runtime_enabled": True, "billing_verified": True,
                                     "admission_state": "accepted", "cost": 12.5}
        self.assertFalse(self.parse(data).runtime_enabled)
        self.assertFalse(self.parse(data).billing_verified)
        data[0]["future_metadata"]["cost"] = 10**20
        with self.assertRaises(TelemetryError):
            self.parse(data)

    def test_json_depth_duplicates_nonfinite_and_content_free_errors(self) -> None:
        raw = stream()
        malformed = [
            raw.replace('"type": "system"', '"type": "system", "type": "system"', 1),
            raw.replace('"costUSD": 5e-05', '"costUSD": NaN', 1),
            raw.replace('"costUSD": 5e-05', '"costUSD": 1e999', 1),
            "[" * 40 + "]" * 40,
            raw + "x" * 262145,
            raw.replace('"tools": []', '"tools": SECRET_MARKER', 1),
        ]
        data = frames()
        nested = {}
        for _ in range(40):
            nested = {"nested": nested}
        data[0]["metadata"] = nested
        malformed.append(stream(data))
        for text in malformed:
            with self.assertRaises(TelemetryError) as caught:
                parse_no_tool_stream(text, requested_model=MODEL)
            self.assertNotIn("SECRET", str(caught.exception))
