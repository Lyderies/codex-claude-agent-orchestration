from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

from claude_agents.contracts import (
    ContractError, parse_assignment, parse_billing_observation, parse_parent_grant,
    parse_result, parse_task, parse_usage, parse_version_observation, safe_load_json,
    to_wire,
)
from claude_agents.validation import assess_fixture_readiness


def bundle() -> dict:
    capabilities = {
        "tools": ["Read", "Glob", "Grep"], "read_paths": ["src/one.py", "docs/spec.md"],
        "network": False, "mcps": [], "hooks": [], "secrets": [],
        "external_effects": False,
    }
    bounds = {
        "max_attempts": 1, "timeout_seconds": 120, "max_tool_calls": 8,
        "max_no_progress_events": 2,
    }
    return {
        "schema_version": 1,
        "task": {
            "schema_version": 1, "task_id": "task-1", "job_id": "job-1",
            "objective": "Inspect synthetic fixture", "family": "research",
            "owned_paths": ["src/one.py", "docs/spec.md"],
            "required_evidence": ["C-1", "C-2"],
        },
        "parent_grant": {
            "schema_version": 1, "grant_id": "grant-1", "task_id": "task-1",
            "job_id": "job-1", "capabilities": deepcopy(capabilities),
            "bounds": deepcopy(bounds),
        },
        "assignment": {
            "schema_version": 1, "assignment_id": "assignment-1", "task_id": "task-1",
            "job_id": "job-1", "attempt_id": "attempt-1", "attempt": 1,
            "parent_grant_id": "grant-1", "role": "claude_agents_researcher",
            "requested_model": "fixture-claude-v1", "context_paths": ["src/one.py"],
            "capabilities": deepcopy(capabilities), "bounds": deepcopy(bounds),
            "required_evidence": ["C-1", "C-2"], "lifecycle_state": "pre-send",
        },
        "version": {
            "schema_version": 1, "assignment_id": "assignment-1",
            "attempt_id": "attempt-1", "source": "synthetic", "channel": "latest",
            "client_version": "9.9.9", "latest_version": "9.9.9",
            "fresh": True, "capabilities_verified": True,
        },
        "billing": {
            "schema_version": 1, "assignment_id": "assignment-1",
            "attempt_id": "attempt-1", "source": "synthetic", "provider": "first_party",
            "auth_method": "claude.ai", "billing_mode": "subscription",
            "usage_credits": "disabled", "entitlement": "available", "fresh": True,
        },
    }


def result() -> dict:
    return {
        "schema_version": 1, "assignment_id": "assignment-1", "task_id": "task-1",
        "job_id": "job-1", "attempt_id": "attempt-1",
        "requested_model": "fixture-claude-v1", "resolved_model": "fixture-claude-v1",
        "execution_state": "completed", "semantic_verdict": "ready",
        "admission_state": "unverified", "summary": "Synthetic inspection complete",
        "artifacts": ["research/result.json"],
        "evidence": [
            {"criterion_id": c, "type": "observation", "reference": "fixture",
             "outcome": "pass", "details": "Synthetic criterion checked"}
            for c in ("C-1", "C-2")
        ],
        "unverified_claims": [], "remaining_risks": [], "usage": None,
    }


class ContractTests(unittest.TestCase):
    def assert_denied(self, value: dict, code: str | None = None) -> None:
        with self.assertRaises(ContractError) as caught:
            assess_fixture_readiness(value)
        if code is not None:
            self.assertEqual(caught.exception.code, code)

    def test_valid_roundtrip_is_detached_and_cannot_enable_runtime(self) -> None:
        value = bundle()
        ready = assess_fixture_readiness(safe_load_json(json.dumps(value)))
        self.assertFalse(ready.runtime_enabled)
        self.assertEqual(to_wire(ready.task), value["task"])
        self.assertEqual(to_wire(ready.assignment), value["assignment"])
        value["assignment"]["capabilities"]["tools"].append("Bash")
        self.assertEqual(ready.assignment.capabilities.tools, ("Read", "Glob", "Grep"))
        detached = to_wire(ready.assignment)
        detached["capabilities"]["read_paths"].clear()
        self.assertEqual(len(ready.assignment.capabilities.read_paths), 2)
        with self.assertRaises(FrozenInstanceError):
            ready.runtime_enabled = True

    def test_schema_missing_unknown_nested_and_boolean_numbers_are_rejected(self) -> None:
        for path, field, changed in (
            ("task", "schema_version", True), ("task", "schema_version", 2),
            ("task", "authority", "SECRET_MARKER"), ("assignment", "attempt", True),
            ("assignment", "attempt", 2), ("billing", "fresh", 1),
            ("version", "capabilities_verified", 1),
        ):
            with self.subTest(path=path, field=field, changed=changed):
                value = bundle()
                value[path][field] = changed
                self.assert_denied(value)
        value = bundle()
        del value["task"]["objective"]
        self.assert_denied(value)
        value = bundle()
        value["assignment"]["bounds"]["spend_usd"] = 1
        self.assert_denied(value)

    def test_parent_grant_can_only_be_narrowed(self) -> None:
        for tools in ([], ["Read"], ["Glob"], ["Grep"], ["Read", "Grep"]):
            value = bundle()
            value["assignment"]["capabilities"]["tools"] = tools
            self.assertFalse(assess_fixture_readiness(value).runtime_enabled)
        for name, changed in (
            ("tools", ["Bash"]), ("tools", ["Read", "Read"]),
            ("network", True), ("mcps", ["server"]), ("hooks", ["command"]),
            ("secrets", ["credential"]), ("external_effects", True),
            ("read_paths", ["src/one.py/subpath"]),
        ):
            value = bundle()
            value["assignment"]["capabilities"][name] = changed
            self.assert_denied(value)
        value = bundle()
        value["parent_grant"]["capabilities"]["tools"] = ["Read"]
        self.assert_denied(value, "grant_expansion")
        value = bundle()
        value["assignment"]["capabilities"]["read_paths"] = ["src/one.py"]
        value["assignment"]["context_paths"] = []
        assess_fixture_readiness(value)

    def test_bounds_and_context_may_not_expand(self) -> None:
        for changed in (True, -1, 0, 3601, float("inf"), float("nan")):
            value = bundle()
            value["assignment"]["bounds"]["timeout_seconds"] = changed
            self.assert_denied(value)
        value = bundle()
        value["assignment"]["bounds"]["max_tool_calls"] = 9
        self.assert_denied(value, "bound_expansion")
        value = bundle()
        value["assignment"]["context_paths"] = ["docs/ungranted.md"]
        self.assert_denied(value, "grant_expansion")

    def test_identity_and_required_criteria_cannot_be_rebound(self) -> None:
        for part, field in (
            ("assignment", "task_id"), ("assignment", "job_id"),
            ("assignment", "parent_grant_id"), ("parent_grant", "task_id"),
            ("parent_grant", "job_id"), ("version", "attempt_id"),
            ("billing", "assignment_id"),
        ):
            value = bundle()
            value[part][field] = "other"
            self.assert_denied(value)
        value = bundle()
        value["assignment"]["required_evidence"] = ["C-1"]
        self.assert_denied(value, "evidence_mismatch")

    def test_paths_and_arrays_are_bounded_and_literal(self) -> None:
        for path in ("/etc/passwd", "../secret", "src/../secret", "a//b", ".",
                     "C:/secret", "src/*.py", "src\\secret", "a/" + "x" * 65):
            value = bundle()
            value["task"]["owned_paths"] = [path]
            self.assert_denied(value, "invalid_path")
        value = bundle()
        value["task"]["required_evidence"] = ["C"] * 33
        self.assert_denied(value)
        value = bundle()
        value["task"]["objective"] = "x" * 4097
        self.assert_denied(value)
        value = bundle()
        value["task"]["task_id"] = "x" * 65
        self.assert_denied(value)

    def test_ambiguity_and_any_already_sent_attempt_fail_readiness(self) -> None:
        for state in ("sent", "acknowledged", "terminal", "ambiguous"):
            value = bundle()
            value["assignment"]["lifecycle_state"] = state
            self.assert_denied(value, "attempt_not_pre_send")
        value = bundle()
        value["assignment"]["requested_model"] = "claude-sonnet-5-5"
        self.assert_denied(value, "synthetic_model_required")

    def test_unknown_stale_unsupported_version_and_billing_fail_closed(self) -> None:
        for part, field, changed in (
            ("version", "source", "unavailable"), ("version", "channel", "stable"),
            ("version", "client_version", "2.1.94"), ("version", "client_version", None),
            ("version", "latest_version", None), ("version", "fresh", False),
            ("version", "capabilities_verified", False),
            ("billing", "source", "unavailable"), ("billing", "provider", "unknown"),
            ("billing", "auth_method", "api_key"), ("billing", "billing_mode", "api"),
            ("billing", "usage_credits", "unknown"), ("billing", "usage_credits", "enabled"),
            ("billing", "entitlement", "unavailable"), ("billing", "fresh", False),
        ):
            value = bundle()
            value[part][field] = changed
            self.assert_denied(value)

    def test_result_binds_models_identities_and_never_admits_itself(self) -> None:
        assignment = assess_fixture_readiness(bundle()).assignment
        parsed = parse_result(result(), assignment=assignment)
        self.assertEqual(parsed.admission_state, "unverified")
        self.assertIsNone(parsed.usage)
        self.assertEqual(to_wire(parsed), result())
        for field, changed in (
            ("assignment_id", "other"), ("job_id", "other"), ("attempt_id", "other"),
            ("requested_model", "other"), ("resolved_model", "other"),
            ("admission_state", "accepted"), ("terminal_verified", True),
            ("effects_resolved", True),
        ):
            data = result()
            data[field] = changed
            with self.assertRaises(ContractError):
                parse_result(data, assignment=assignment)

    def test_completed_negative_review_is_valid_but_missing_evidence_is_not(self) -> None:
        assignment = assess_fixture_readiness(bundle()).assignment
        data = result()
        data["semantic_verdict"] = "reject"
        data["evidence"][0]["outcome"] = "fail"
        self.assertEqual(parse_result(data, assignment=assignment).execution_state,
                         "completed")
        for change in ("missing", "duplicate", "unknown", "failed_positive"):
            data = result()
            if change == "missing":
                data["evidence"].pop()
            elif change == "duplicate":
                data["evidence"][1]["criterion_id"] = "C-1"
            elif change == "unknown":
                data["evidence"][1]["criterion_id"] = "other"
            else:
                data["evidence"][0]["outcome"] = "fail"
            with self.assertRaises(ContractError):
                parse_result(data, assignment=assignment)

    def test_usage_unknowns_provenance_subsets_and_numbers(self) -> None:
        usage = {"schema_version": 1, "source": "unavailable", "coverage": "unknown",
                 "input_tokens": None, "cached_tokens": None, "output_tokens": None,
                 "reasoning_tokens": None, "tool_calls": None, "latency_ms": None}
        self.assertEqual(to_wire(parse_usage(usage)), usage)
        for field, changed in (("input_tokens", 0), ("input_tokens", True),
                               ("input_tokens", -1), ("cost", 0)):
            value = deepcopy(usage)
            value[field] = changed
            with self.assertRaises(ContractError):
                parse_usage(value)
        usage.update(source="synthetic", coverage="complete", input_tokens=10,
                     cached_tokens=11, output_tokens=2, reasoning_tokens=1,
                     tool_calls=0, latency_ms=1)
        with self.assertRaises(ContractError):
            parse_usage(usage)
        usage["cached_tokens"] = 5
        parse_usage(usage)

    def test_loaders_and_errors_do_not_echo_rejected_content(self) -> None:
        for text in ('{"key": 1, "key": 2}', '{"x": NaN}', '{"x": Infinity}',
                     '{"x": 1e999}', '{"x": 1.5}', "[" * 2000 + "]" * 2000):
            with self.assertRaises(ContractError):
                safe_load_json(text)
        value = bundle()
        value["task"]["SECRET_MARKER"] = "SECRET_VALUE"
        with self.assertRaises(ContractError) as caught:
            assess_fixture_readiness(value)
        self.assertEqual(str(caught.exception), "invalid_fields")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_public_constructors_cannot_bypass_assignment_or_result_checks(self) -> None:
        value = bundle()
        task = parse_task(value["task"])
        parent = parse_parent_grant(value["parent_grant"])
        unsafe_parent = replace(parent, capabilities=replace(parent.capabilities, network=True))
        with self.assertRaises(ContractError):
            parse_assignment(value["assignment"], task=task, parent=unsafe_parent)
        unsafe_task = replace(task, owned_paths=("../secret",))
        with self.assertRaises(ContractError):
            parse_assignment(value["assignment"], task=unsafe_task, parent=parent)
        assignment = assess_fixture_readiness(value).assignment
        for changed in (
            replace(assignment, requested_model="claude-fable-5-1"),
            replace(assignment, required_evidence=()),
            replace(assignment, capabilities=replace(assignment.capabilities, network=True)),
        ):
            with self.assertRaises(ContractError):
                parse_result(result(), assignment=changed)
        for model in ("fixture-fable", "claude-fable-5-1", "sonnet", "api-model"):
            changed = bundle()
            changed["assignment"]["requested_model"] = model
            self.assert_denied(changed, "synthetic_model_required")

    def test_json_depth_is_bounded_without_treating_string_content_as_structure(self) -> None:
        with self.assertRaises(ContractError) as caught:
            safe_load_json("[" * 33 + "]" * 33)
        self.assertEqual(caught.exception.code, "invalid_json")
        quoted = 'brackets ' + "[{" * 100 + ' escaped quote " and slash \\'
        self.assertEqual(safe_load_json(json.dumps({"text": quoted})), {"text": quoted})
        self.assertEqual(safe_load_json("[" * 32 + "]" * 32),
                         json.loads("[" * 32 + "]" * 32))

    def test_observation_and_nested_contract_roundtrips(self) -> None:
        value = bundle()
        task = parse_task(value["task"])
        parent = parse_parent_grant(value["parent_grant"])
        self.assertEqual(to_wire(parent), value["parent_grant"])
        self.assertEqual(to_wire(parse_assignment(value["assignment"], task=task,
                                                  parent=parent)), value["assignment"])
        self.assertEqual(to_wire(parse_version_observation(value["version"])),
                         value["version"])
        self.assertEqual(to_wire(parse_billing_observation(value["billing"])),
                         value["billing"])


if __name__ == "__main__":
    unittest.main()
