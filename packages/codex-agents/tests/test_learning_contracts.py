from __future__ import annotations

import unittest
from copy import deepcopy

from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import (
    candidate_digest,
    parse_bounded_json,
    provider_for_model,
    validate_candidate,
    validate_host_observation,
    validate_learning_record,
)
from tests.learning_support import candidate, event_record, verification_outcome


def usage_record(**changes) -> dict:
    return {
        "schema_version": 1, "record_type": "provider_usage", "record_id": "provider_usage:1",
        "provider": "claude", "run_id": "run-1", "phase": "after",
        "requested_model": "claude-opus-5-5",
        "models": [
            {"model": None, "input_tokens": 1, "cached_input_tokens": None,
             "cache_creation_input_tokens": None, "output_tokens": None,
             "reasoning_output_tokens": None},
            {"model": "claude-opus-5-5", "input_tokens": 10, "cached_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 2,
             "reasoning_output_tokens": None},
        ],
        "helper_models": ["claude-haiku-5-5"],
        "input_tokens": 10, "cached_input_tokens": 0, "cache_creation_input_tokens": 0,
        "output_tokens": 2, "reasoning_output_tokens": None, "observed_at_ms": 5,
        **changes,
    }


class LearningContractTests(unittest.TestCase):
    def test_provider_usage_and_unavailable_readings_are_closed(self) -> None:
        validate_learning_record(usage_record())
        validate_learning_record(usage_record(
            provider="codex", requested_model="gpt-6.1-sol", models=[], helper_models=[],
        ))
        model = usage_record()["models"][1]
        for changes, message in (
            ({"prompt": "x"}, "unknown fields"),
            ({"phase": "before"}, "phase"),
            ({"run_id": None}, "run_id"),
            ({"requested_model": "gpt-6.1-sol"}, "disagree"),
            ({"provider": "codex", "requested_model": "gpt-6.1-sol"}, "only as totals"),
            ({"models": list(reversed(usage_record()["models"]))}, "sorted"),
            ({"models": [model, model]}, "sorted and unique"),
            ({"models": [{**model, "model": "fixture-model-x"}]}, "model"),
            ({"models": [{**model, "cost": 1}]}, "unknown fields"),
            ({"helper_models": ["fixture-model-x"]}, "helper_models"),
            ({"input_tokens": -1}, "input_tokens"),
            ({"output_tokens": True}, "output_tokens"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ContractError, message):
                validate_learning_record(usage_record(**changes))
        unavailable = {
            "schema_version": 1, "record_type": "observation_unavailable",
            "record_id": "observation_unavailable:1", "provider": "codex",
            "run_id": "run-1", "phase": "after", "reason_code": "observation_failed",
            "observed_at_ms": 5,
        }
        validate_learning_record(unavailable)
        for changes, message in (
            ({"reason_code": "failed: /some/path"}, "reason_code"),
            ({"phase": None}, "phase"),
            ({"run_id": None}, "run_id"),
            ({"detail": "x"}, "unknown fields"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ContractError, message):
                validate_learning_record({**unavailable, **changes})

    def test_run_observations_carry_a_phase_only_with_a_run(self) -> None:
        headroom = {
            "schema_version": 1, "record_type": "provider_headroom",
            "record_id": "provider_headroom:1", "provider": "codex",
            "account_fingerprint": "a" * 64, "windows": [], "status": None,
            "run_id": None, "phase": None, "observed_at_ms": 5,
        }
        validate_learning_record(headroom)
        validate_learning_record({**headroom, "run_id": "run-1", "phase": "before"})
        for changes in (
            {"run_id": "run-1", "phase": None}, {"run_id": None, "phase": "after"},
            {"run_id": "run-1", "phase": "later"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_learning_record({**headroom, **changes})

    def test_provider_is_derived_from_the_model(self) -> None:
        self.assertEqual("codex", provider_for_model("gpt-6.1-sol"))
        self.assertEqual("claude", provider_for_model("claude-opus-5-5"))
        for model in ("gemini-3", "", None):
            with self.subTest(model=model), self.assertRaisesRegex(
                ContractError, "supported provider"
            ):
                provider_for_model(model)

    def test_records_reject_a_model_and_role_from_different_providers(self) -> None:
        outcome = verification_outcome()
        outcome["model"] = "claude-opus-5-5"
        with self.assertRaisesRegex(ContractError, "different providers"):
            validate_learning_record(outcome)
        outcome = verification_outcome()
        outcome["role"] = "claude_agents_verification_worker"
        with self.assertRaisesRegex(ContractError, "different providers"):
            validate_learning_record(outcome)
        event = event_record()
        event.update(model="claude-haiku-5-5", role="codex_agents_researcher")
        with self.assertRaisesRegex(ContractError, "different providers"):
            validate_learning_record(event)

    def test_host_observation_accepts_one_bounded_contract_repair(self) -> None:
        observation = {
            "run_id": "run_1",
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": "rpr_" + "a" * 48,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        self.assertEqual(observation, validate_host_observation(observation))

    def test_host_observation_rejects_unbounded_or_free_text_repairs(self) -> None:
        observation = {
            "run_id": "run_1",
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "model ignored the schema",
                    "receipt_id": "rpr_" + "a" * 48,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        with self.assertRaisesRegex(ContractError, "valid receipt"):
            validate_host_observation(observation)
        observation["result_attempts"] = [
            {
                "status": "rejected",
                "reason_code": "missing_evidence",
                "receipt_id": "rpr_" + "a" * 48,
            },
            {
                "status": "rejected",
                "reason_code": "invalid_shape",
                "receipt_id": "rpr_" + "b" * 48,
            },
            {"status": "accepted", "reason_code": None, "receipt_id": None},
        ]
        with self.assertRaisesRegex(ContractError, "at most one repair"):
            validate_host_observation(observation)

    def test_outcome_rejects_non_string_repair_reason_without_type_error(self) -> None:
        record = verification_outcome("invalid_repair_reason")
        record["repairs"] = 1
        record["repair_reason_codes"] = [["missing_evidence"]]

        with self.assertRaisesRegex(ContractError, "repair reason codes"):
            validate_learning_record(record)

    def test_event_is_closed_and_contains_no_project_claim(self) -> None:
        record = event_record()
        self.assertEqual("evt_1", validate_learning_record(record)["record_id"])
        record["project_id"] = "forged"
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_learning_record(record)

    def test_raw_prompt_and_full_trace_are_rejected(self) -> None:
        record = event_record()
        record["prompt"] = "secret"
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_learning_record(record)

    def test_rejected_field_name_is_not_reflected_in_exception_text(self) -> None:
        record = event_record()
        record["TOP_SECRET_FIELD_NAME"] = "secret"
        with self.assertRaises(ContractError) as captured:
            validate_learning_record(record)
        self.assertNotIn("TOP_SECRET", str(captured.exception))
        self.assertNotIn("secret", str(captured.exception))
        record = event_record()
        record["record_type"] = "full_trace"
        with self.assertRaisesRegex(ContractError, "unsupported"):
            validate_learning_record(record)

    def test_duplicate_json_key_binary_and_oversize_fail_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "duplicate"):
            parse_bounded_json('{"record_type":"event","record_type":"event"}')
        with self.assertRaisesRegex(ContractError, "binary"):
            parse_bounded_json(b'{"x":"\x00"}')
        with self.assertRaisesRegex(ContractError, "oversized"):
            parse_bounded_json(b"x" * 70_000)

    def test_forward_schema_version_is_rejected(self) -> None:
        record = event_record()
        record["schema_version"] = 2
        with self.assertRaisesRegex(ContractError, "schema version"):
            validate_learning_record(record)

    def test_usage_subsets_cannot_be_double_counted(self) -> None:
        record = event_record()
        record["usage"]["input_tokens"] = 10
        record["usage"]["cached_tokens"] = 11
        with self.assertRaisesRegex(ContractError, "cached_tokens"):
            validate_learning_record(record)

    def test_candidate_digest_is_stable_across_lifecycle_status(self) -> None:
        record = candidate()
        changed = deepcopy(record)
        changed["status"] = "evaluated"
        self.assertEqual(candidate_digest(record), candidate_digest(changed))
        self.assertEqual(record["candidate_digest"], validate_candidate(changed)["candidate_digest"])

    def test_candidate_cannot_touch_protected_fields(self) -> None:
        record = candidate()
        record["change_manifest"][0]["path_code"] = "authority.tools"
        with self.assertRaisesRegex(ContractError, "unsupported"):
            validate_candidate(record)


if __name__ == "__main__":
    unittest.main()
