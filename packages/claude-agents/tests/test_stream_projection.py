from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import json
import unittest

from claude_agents.stream_projection import (
    KnownProjectionErrorCodes, ProvisionalUsage, ProjectedNoToolObservation,
    SYSTEM_SUBTYPES, frame_descriptor, known_error_code, parse_provisional_usage,
    passive_system, project_no_tool, strip_thinking,
)
from claude_agents.telemetry import TelemetryError
from tests.test_telemetry import MODEL, frames

CANARY = "PRIVATE_REASONING_PAYLOAD_DO_NOT_RETAIN"


def baseline():
    values = frames()
    for index, frame in enumerate(values):
        frame["uuid"] = f"frame-{index}"
    return values


def encode(values):
    return "\n".join(json.dumps(frame, ensure_ascii=False) for frame in values) + "\n"


def passive(subtype, identity="passive-1", **fields):
    return {"type": "system", "subtype": subtype, "session_id": "session-1",
            "uuid": identity, **fields}


def split():
    values = baseline()
    thinking = deepcopy(values[1])
    thinking["uuid"] = "thinking-frame"
    thinking["message"]["content"] = [{"type": "thinking", "thinking": CANARY,
                                       "signature": CANARY + "_signature"}]
    redacted = deepcopy(values[1])
    redacted["uuid"] = "redacted-frame"
    redacted["message"]["content"] = [{"type": "redacted_thinking", "data": CANARY + "_opaque"}]
    return [values[0], thinking, redacted, values[1], values[2]]


class StreamProjectionTests(unittest.TestCase):
    def parse(self, values):
        return project_no_tool(encode(values), requested_model=MODEL)

    def test_sdk_system_descriptors_are_closed_and_do_not_echo_unknowns(self):
        self.assertEqual(len(SYSTEM_SUBTYPES), 28)
        for subtype in SYSTEM_SUBTYPES:
            self.assertEqual(frame_descriptor({"type": "system", "subtype": subtype}), "system:" + subtype)
        for value in (None, CANARY, [], {}):
            self.assertEqual(frame_descriptor({"type": "system", "subtype": value}), "system:unknown")
        self.assertEqual(frame_descriptor({"type": CANARY}), "unknown")

    def test_split_blocks_one_message_and_no_reasoning_payload_or_hash_survives(self):
        values = split()
        result = self.parse(values)
        self.assertEqual(result.thinking_counts.thinking_blocks, 1)
        self.assertEqual(result.thinking_counts.redacted_blocks, 1)
        self.assertEqual(result.original_assistant_frames, 3)
        self.assertEqual(result.observation.no_tool.model_usage.tokens.output_tokens, 1)
        self.assertFalse(result.assistant_usage_is_final)
        self.assertEqual(result.hash_scope, "canonical_text_only_projection")
        self.assertFalse(result.observation.no_tool.runtime_enabled)
        self.assertNotIn(CANARY, json.dumps(asdict(result)))
        changed = deepcopy(values)
        changed[1]["message"]["content"][0].update(thinking="different", signature="unrelated")
        changed[2]["message"]["content"][0]["data"] = "other opaque value"
        self.assertEqual(result, self.parse(changed))  # No stripped-payload fingerprint.
        with self.assertRaises(FrozenInstanceError):
            result.original_assistant_frames = 4

    def test_thinking_tokens_already_belong_to_terminal_output(self):
        values = split()
        values[-1]["usage"]["output_tokens"] = 8
        values[-1]["usage"]["output_tokens_details"]["thinking_tokens"] = 5
        for key in ("modelUsage", "model_usage_since_last_result"):
            values[-1][key][MODEL].update(outputTokens=8, thinkingTokens=5)
        result = self.parse(values)
        self.assertEqual(result.observation.no_tool.model_usage.tokens.output_tokens, 8)
        self.assertEqual(result.observation.no_tool.model_usage.tokens.thinking_tokens, 5)

    def test_strip_returns_detached_text_and_tool_data_and_counts_only(self):
        message = baseline()[1]["message"]
        message["content"] = [
            {"type": "thinking", "thinking": CANARY, "signature": CANARY},
            {"type": "tool_use", "id": "tool-1", "name": "read_artifact",
             "input": {"artifact": "source", "nested": [1]}},
            {"type": "text", "text": "OK"},
        ]
        cleaned, counts = strip_thinking(message)
        self.assertEqual(counts.thinking_blocks, 1)
        self.assertEqual(cleaned["content"], message["content"][1:])
        cleaned["content"][0]["input"]["nested"].append(2)
        self.assertEqual(message["content"][1]["input"]["nested"], [1])
        self.assertNotIn(CANARY, json.dumps(cleaned))
        with self.assertRaises(TelemetryError):
            self.parse([baseline()[0], {**baseline()[1], "message": message}, baseline()[2]])

    def test_malformed_thinking_redaction_and_unknown_blocks_are_rejected(self):
        blocks = [
            {"type": "thinking", "thinking": "x"},
            {"type": "thinking", "thinking": 1, "signature": "x"},
            {"type": "thinking", "thinking": "x", "signature": None},
            {"type": "thinking", "thinking": "x", "signature": "x", "extra": CANARY},
            {"type": "redacted_thinking", "data": []},
            {"type": "redacted_thinking", "data": "x", "signature": "x"},
            {"type": "server_tool_use", "name": "unrecognized"},
            {"type": "text", "text": True},
        ]
        for block in blocks:
            message = deepcopy(baseline()[1]["message"])
            message["content"] = [block]
            with self.subTest(kind=block.get("type")), self.assertRaises(TelemetryError) as caught:
                strip_thinking(message)
            self.assertNotIn(CANARY, str(caught.exception))

    def test_valid_passive_metadata_can_precede_init_and_follow_result(self):
        values = baseline()
        controls = [
            passive("thinking_tokens", estimated_tokens=3.5, estimated_tokens_delta=0,
                    user_message_uuid="user-1"),
            passive("session_state_changed", "passive-2", state="running"),
            passive("status", "passive-3", status="requesting", permissionMode="dontAsk"),
            passive("status", "passive-4", status=None),
            passive("session_state_changed", "passive-5", state="idle"),
        ]
        result = self.parse([controls[0], values[0], controls[1], controls[2],
                             values[1], values[2], controls[3], controls[4]])
        self.assertEqual(dict(result.passive_counts),
                         {"thinking_tokens": 1, "session_state_changed": 2, "status": 2})
        observation = passive_system(controls[0], "session-1")
        self.assertEqual(asdict(observation), {"subtype": "thinking_tokens", "state": None})
        self.assertNotIn("session-1", repr(observation))

    def test_only_three_closed_passive_schemas_and_safe_states_are_accepted(self):
        invalid = [
            passive("session_state_changed", state="requires_action"),
            passive("status", status="compacting"),
            passive("status", status=None, permissionMode="acceptEdits"),
            passive("status", status=None, compact_result="success"),
            passive("status", status=None, compact_error=CANARY),
            passive("thinking_tokens", estimated_tokens=True, estimated_tokens_delta=0),
            passive("thinking_tokens", estimated_tokens=float("inf"), estimated_tokens_delta=0),
            passive("thinking_tokens", estimated_tokens=1, estimated_tokens_delta=-1),
            passive("thinking_tokens", estimated_tokens=1, estimated_tokens_delta=0, user_message_uuid=None),
        ]
        for frame in invalid:
            with self.assertRaises(TelemetryError):
                passive_system(frame, "session-1")
        for subtype in SYSTEM_SUBTYPES - {"thinking_tokens", "session_state_changed", "status"} | {"unknown"}:
            with self.assertRaises(TelemetryError):
                passive_system(passive(subtype), "session-1")

    def test_session_binding_and_active_before_init_or_after_result_are_denied(self):
        values = baseline()
        changed = passive("status", status=None)
        changed["session_id"] = "other-session"
        cases = [[changed, *values], [values[1], values[0], values[2]],
                 [*values, values[1]], [*values, values[2]],
                 [passive("task_started"), *values]]
        for case in cases:
            with self.assertRaises(TelemetryError):
                self.parse(case)

    def test_every_original_assistant_error_child_and_resume_flag_is_checked(self):
        mutations = [
            lambda f: f.update(error={"message": CANARY}),
            lambda f: f.update(parent_tool_use_id="child"),
            lambda f: f.update(agent_id="child"),
            lambda f: f.update(subagent_type="worker"),
            lambda f: f.update(resumed_from_incomplete_thinking=True),
            lambda f: f.update(resume_reason="interrupted"),
            lambda f: f.update(supersedes=["earlier"]),
            lambda f: f.update(aborted=True),
            lambda f: f["message"].update(error={"message": CANARY}),
            lambda f: f["message"].update(model="other-model"),
            lambda f: f["message"].update(role="user"),
            lambda f: f["message"].update(type="other"),
            lambda f: f["message"].update(stop_reason="refusal"),
            lambda f: f["message"]["usage"].update(fallback_credit={"enabled": True}),
        ]
        for mutate in mutations:
            values = split()
            mutate(values[1])  # Counterevidence on discarded thinking frame cannot vanish.
            with self.assertRaises(TelemetryError):
                self.parse(values)

    def test_duplicate_frames_multiple_ids_usage_drift_and_missing_identity_are_denied(self):
        changes = [
            lambda v: v.insert(2, deepcopy(v[1])),
            lambda v: v[3]["message"].update(id="new-response"),
            lambda v: v[3]["message"]["usage"].update(input_tokens=21),
            lambda v: v[3]["message"]["usage"].update(cache_read_input_tokens=1),
            lambda v: v[1].pop("uuid"),
        ]
        for change in changes:
            values = split()
            change(values)
            with self.assertRaises(TelemetryError):
                self.parse(values)
        values = split()
        values[3]["message"]["usage"]["output_tokens"] = 1
        self.assertEqual(self.parse(values).observation.no_tool.assistant_usage.output_tokens, 0)

    def test_missing_text_truncation_oversize_and_extra_response_are_denied(self):
        values = split()
        for broken in (values[:3] + values[-1:], values[:-1], values + [baseline()[1]]):
            with self.assertRaises(TelemetryError):
                self.parse(broken)
        for raw in (encode(values) + "x" * 262145,
                    encode(values).replace('"type": "thinking"', '"type": "thinking", "type": "thinking"', 1),
                    encode(values).replace('"output_tokens": 0', '"output_tokens": NaN', 1)):
            with self.assertRaises(TelemetryError):
                project_no_tool(raw, requested_model=MODEL)

    def test_rate_validation_and_model_usage_checks_remain_delegated(self):
        values = split()
        rate = {"type": "rate_limit_event", "session_id": "session-1", "uuid": "rate-1",
                "rate_limit_info": {"status": "allowed", "overageEnabled": False}}
        result = self.parse([values[0], rate, *values[1:]])
        self.assertEqual(len(result.observation.rate_limits), 1)
        rate["rate_limit_info"]["overageEnabled"] = True
        with self.assertRaises(TelemetryError):
            self.parse([values[0], rate, *values[1:]])
        values[-1]["modelUsage"][MODEL]["canonicalModel"] = "different"
        with self.assertRaises(TelemetryError):
            self.parse(values)


    def test_known_nullable_betamessage_metadata_and_empty_transform_list(self):
        values = split()
        for frame in values[1:-1]:
            frame["message"].update(container=None, context_management=None, stop_details=None,
                                    diagnostics=None, input_transformations=None)
        values[3]["message"]["input_transformations"] = []
        result = self.parse(values)
        self.assertIsInstance(result.observation.no_tool, ProjectedNoToolObservation)
        for key, value in (("container", {}), ("stop_details", {"type": "refusal"}),
                           ("context_management", {"applied_edits": []}),
                           ("diagnostics", {"cache_miss_reason": None}),
                           ("input_transformations", [{"type": "thinking_dropped"}]),
                           ("future_metadata", None)):
            changed = deepcopy(values)
            changed[1]["message"][key] = value
            with self.subTest(key=key), self.assertRaises(TelemetryError):
                self.parse(changed)

    def test_provisional_nullable_counts_remain_unknown_without_final_count_fabrication(self):
        values = split()
        for frame in values[1:-1]:
            frame["message"]["usage"] = {
                "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": None,
                "cache_creation": None, "server_tool_use": None, "iterations": None,
                "output_tokens_details": None, "fallback_credit": None,
                "service_tier": None, "speed": "standard", "inference_geo": "",
            }
        result = self.parse(values).observation.no_tool
        self.assertIsInstance(result.assistant_usage, ProvisionalUsage)
        self.assertEqual(result.assistant_usage.input_tokens, 0)
        self.assertIsNone(result.assistant_usage.cache_read_input_tokens)
        self.assertIsNone(result.assistant_usage.cache_creation_input_tokens)
        self.assertIsNone(result.assistant_usage.thinking_tokens)
        self.assertEqual(result.terminal_root_usage.input_tokens, 20)
        self.assertEqual(result.terminal_root_usage.cache_read_input_tokens, 0)
        self.assertFalse(result.runtime_enabled)
        cleaned, _counts = strip_thinking(values[1]["message"], max_output_tokens=256)
        self.assertIsNone(cleaned["usage"]["cache_creation"])
        self.assertIsNone(cleaned["usage"]["iterations"])
        self.assertIsNone(cleaned["usage"]["output_tokens_details"])
        with self.assertRaises(FrozenInstanceError):
            result.assistant_usage.output_tokens = 5

    def test_provisional_thinking_above_placeholder_is_not_added_to_final_output(self):
        values = split()
        values[1]["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 17}
        values[3]["message"]["usage"]["output_tokens"] = 1
        result = self.parse(values).observation.no_tool
        self.assertEqual(result.assistant_usage.output_tokens, 0)
        self.assertEqual(result.assistant_usage.thinking_tokens, 17)
        self.assertEqual(result.model_usage.tokens.output_tokens, 1)

    def test_cache_unknown_to_known_join_preserves_values_and_rejects_known_conflicts(self):
        values = split()
        for frame in values[1:3]:
            frame["message"]["usage"]["cache_read_input_tokens"] = None
            frame["message"]["usage"].pop("cache_creation_input_tokens")
        joined = self.parse(values).observation.no_tool.assistant_usage
        self.assertEqual(joined.cache_read_input_tokens, 0)
        self.assertEqual(joined.cache_creation_input_tokens, 0)
        values[2]["message"]["usage"]["cache_read_input_tokens"] = 5
        with self.assertRaises(TelemetryError):
            self.parse(values)

    def test_provisional_metadata_cannot_hide_tools_iterations_cost_modes_or_bad_counts(self):
        valid = {"input_tokens": 2, "output_tokens": 0, "cache_creation": None,
                 "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
                 "server_tool_use": None, "iterations": None, "output_tokens_details": None,
                 "service_tier": "standard", "speed": None, "inference_geo": "global"}
        self.assertIsNone(parse_provisional_usage(valid, 512).thinking_tokens)
        changes = [
            {"input_tokens": None}, {"input_tokens": True}, {"output_tokens": -1},
            {"output_tokens": 513}, {"cache_read_input_tokens": True},
            {"output_tokens_details": {"thinking_tokens": -1}},
            {"server_tool_use": {"web_search_requests": 1}},
            {"server_tool_use": {"unknown": 0}}, {"iterations": [{"type": "message"}]},
            {"fallback_credit": {}}, {"service_tier": "priority"}, {"speed": "fast"},
            {"cache_creation": {"unknown": 0}}, {"unrecognized": None},
        ]
        for change in changes:
            with self.subTest(keys=list(change)), self.assertRaises(TelemetryError):
                parse_provisional_usage({**valid, **change}, 512)

    def test_terminal_completeness_and_model_counterevidence_remain_strict(self):
        for target in ("root-cache", "model-cache", "root-thinking", "model-provider"):
            values = split()
            for frame in values[1:-1]:
                frame["message"]["usage"]["cache_read_input_tokens"] = None
            terminal = values[-1]
            if target == "root-cache":
                terminal["usage"]["cache_read_input_tokens"] = None
            elif target == "model-cache":
                terminal["modelUsage"][MODEL]["cacheReadInputTokens"] = None
            elif target == "root-thinking":
                terminal["usage"]["output_tokens_details"]["thinking_tokens"] = 100
            else:
                terminal["modelUsage"][MODEL]["provider"] = "unknown"
            with self.subTest(target=target), self.assertRaises(TelemetryError):
                self.parse(values)

    def test_error_labels_are_closed_and_never_expose_exception_payload(self):
        try:
            strip_thinking({**baseline()[1]["message"], "container": {}})
        except TelemetryError as error:
            code = known_error_code(error)
            self.assertEqual(code, "server_container_activity")
            self.assertIn(code, KnownProjectionErrorCodes)
        else:
            self.fail("nonnull container accepted")
        self.assertEqual(known_error_code(TelemetryError(CANARY)), "projection_denied")
        self.assertEqual(known_error_code(ValueError(CANARY)), "projection_denied")
        self.assertNotIn(CANARY, repr(KnownProjectionErrorCodes))


    def test_closed_block_metadata_normalizes_only_inactive_fields_and_counts_provenance(self):
        message = baseline()[1]["message"]
        plain = {"type": "tool_use", "id": "tool-1", "name": "read_artifact",
                 "input": {"artifact": "source", "nested": [1]}}
        direct = {**deepcopy(plain), "id": "tool-2", "caller": {"type": "direct"},
                  "toolset_name": None}
        message["content"] = [plain, direct, {"type": "text", "text": "A"},
                              {"type": "text", "text": "B", "citations": None},
                              {"type": "text", "text": "C", "citations": []}]
        cleaned, counts = strip_thinking(message)
        self.assertEqual(cleaned["content"], [plain, {**plain, "id": "tool-2"},
                                             {"type": "text", "text": "A"},
                                             {"type": "text", "text": "B"},
                                             {"type": "text", "text": "C"}])
        self.assertEqual(asdict(counts.block_metadata), {
            "caller_not_reported": 1, "caller_direct": 1,
            "toolset_not_reported": 1, "toolset_inactive": 1,
            "citations_not_reported": 1, "citations_inactive": 2})
        cleaned["content"][1]["input"]["nested"].append(2)
        self.assertEqual(direct["input"]["nested"], [1])
        with self.assertRaises(FrozenInstanceError):
            counts.block_metadata.caller_direct = 99

    def test_caller_toolset_and_argument_boundary_failures_have_fixed_labels(self):
        base = {"type": "tool_use", "id": "tool-1", "name": "read_artifact",
                "input": {"artifact": "source"}}
        cases = [
            ({"caller": value}, "invalid_tool_caller")
            for value in (None, {}, [], "direct", {"type": True},
                          {"type": "direct", "extra": CANARY},
                          {"type": "code_execution_20250825", "tool_id": CANARY},
                          {"type": "code_execution_20260120", "tool_id": CANARY},
                          {"type": CANARY})
        ] + [
            ({"toolset_name": value}, "toolset_routing")
            for value in ("", CANARY, False, 0, [], {})
        ] + [
            ({"input": value}, "invalid_tool_input")
            for value in (None, [], "source", True, 0)
        ] + [({"extra": CANARY}, "invalid_tool_block")]
        for update, code in cases:
            message = deepcopy(baseline()[1]["message"])
            message["content"] = [{**base, **update}]
            with self.subTest(code=code, update=list(update)), self.assertRaises(TelemetryError) as caught:
                strip_thinking(message)
            self.assertEqual(known_error_code(caught.exception), code)
            self.assertIn(code, KnownProjectionErrorCodes)
            self.assertNotIn(CANARY, str(caught.exception))

    def test_active_or_malformed_citations_and_server_blocks_are_not_local_data(self):
        cases = [({"type": "text", "text": "OK", "citations": value}, "invalid_citations")
                 for value in ([{"type": "char_location", "cited_text": CANARY}], {}, "", False, 0)]
        cases += [({"type": "text", "text": "OK", "extra": CANARY}, "invalid_text_block")]
        cases += [({"type": kind, "id": "tool-1", "name": "read_artifact",
                    "input": {"artifact": "source"}, "caller": {"type": "direct"}}, "unexpected_content")
                  for kind in ("server_tool_use", "mcp_tool_use", "mcp_tool_result")]
        for block, code in cases:
            message = deepcopy(baseline()[1]["message"])
            message["content"] = [block]
            with self.subTest(code=code), self.assertRaises(TelemetryError) as caught:
                strip_thinking(message)
            self.assertEqual(known_error_code(caught.exception), code)
            self.assertNotIn(CANARY, str(caught.exception))

    def test_no_tool_projection_aggregates_citations_without_granting_tool_authority(self):
        values = split()
        values[3]["message"]["content"][0]["citations"] = []
        result = self.parse(values)
        self.assertEqual(result.thinking_counts.block_metadata.citations_inactive, 1)
        self.assertEqual(result.thinking_counts.block_metadata.citations_not_reported, 0)
        self.assertEqual(result.thinking_counts.block_metadata.caller_direct, 0)
        plain = self.parse(baseline())
        self.assertEqual(plain.thinking_counts.block_metadata.citations_not_reported, 1)
        message = deepcopy(baseline()[1]["message"])
        message["content"] = [{"type": "tool_use", "id": "tool-1", "name": "read_artifact",
                               "input": {"artifact": "source"}, "caller": {"type": "direct"},
                               "toolset_name": None}]
        with self.assertRaisesRegex(TelemetryError, "unexpected_tool"):
            self.parse([baseline()[0], {**baseline()[1], "message": message}, baseline()[2]])
        for update in ({"error": CANARY}, {"stop_reason": "refusal"}, {"container": {}}):
            values = split()
            values[3]["message"]["content"][0]["citations"] = None
            values[1]["message"].update(update)
            with self.assertRaises(TelemetryError):
                self.parse(values)


if __name__ == "__main__":
    unittest.main()
