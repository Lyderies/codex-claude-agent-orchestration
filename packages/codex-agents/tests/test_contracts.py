from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.contracts import (
    EVIDENCE_OUTCOME_SEVERITY,
    EVIDENCE_OUTCOMES,
    ContractError,
    ResultContractError,
    model_selection_slice,
    routing_task_slice,
    result_preflight,
    validate_assignment,
    validate_parent_grant,
    validate_result,
    validate_task,
)
from codex_agents.routing import load_effective_policy, load_policy, route_task


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json")

    def task(self) -> dict:
        return {
            "id": "task-1",
            "objective": "Inspect the fixture",
            "family": "research",
            "complexity": "medium",
            "risk": "low",
            "operation": "exploration",
            "ambiguity": "known",
            "context_breadth": "repository",
            "acceptance_strength": "independent_judgment",
            "routing_evidence_refs": [],
            "meaningful": True,
            "deterministic_validation": False,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
        }

    def test_task_rejects_unknown_fields(self) -> None:
        task = self.task()
        task["authority"] = "anything"
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_task(task)

    def test_task_requires_explicit_routing_shape(self) -> None:
        task = self.task()
        del task["operation"]
        with self.assertRaisesRegex(ContractError, "missing required fields: operation"):
            validate_task(task)

    def test_task_rejects_unsafe_routing_evidence(self) -> None:
        task = self.task()
        task["routing_evidence_refs"] = ["raw routing evidence contains spaces"]
        with self.assertRaisesRegex(ContractError, "unique safe references"):
            validate_task(task)

    def test_high_risk_task_must_be_meaningful(self) -> None:
        task = self.task()
        task.update(risk="high", meaningful=False)
        with self.assertRaisesRegex(ContractError, "must be meaningful"):
            validate_task(task)

    def test_model_selection_slice_excludes_risk(self) -> None:
        low = self.task()
        high = {**low, "risk": "high"}
        self.assertNotEqual(routing_task_slice(low), routing_task_slice(high))
        self.assertEqual(model_selection_slice(low), model_selection_slice(high))

    def test_deterministic_acceptance_requires_deterministic_validation(self) -> None:
        task = self.task()
        task.update(
            acceptance_strength="deterministic",
            deterministic_validation=False,
        )
        with self.assertRaisesRegex(ContractError, "deterministic acceptance"):
            validate_task(task)

    def test_verification_source_family_is_only_valid_for_verification_tasks(self) -> None:
        task = self.task()
        task["verification_for_family"] = "research"
        with self.assertRaisesRegex(ContractError, "only for verification tasks"):
            validate_task(task)

    def test_task_rejects_invalid_attempt_count(self) -> None:
        task = self.task()
        task["failed_attempts"] = 3
        with self.assertRaisesRegex(ContractError, "0 to 2"):
            validate_task(task)

    def test_task_requires_failure_events_for_each_failed_attempt(self) -> None:
        task = self.task()
        task["failed_attempts"] = 1
        with self.assertRaisesRegex(ContractError, "one or two events"):
            validate_task(task)

    def test_task_rejects_raw_failure_evidence(self) -> None:
        task = self.task()
        task["failed_attempts"] = 1
        task["failure_events"] = [
            {
                "attempt": 1,
                "reason": "result_contract",
                "evidence_refs": ["raw model output must not be stored"],
            }
        ]
        with self.assertRaisesRegex(ContractError, "unique safe references"):
            validate_task(task)

    def test_repeated_reasoning_failures_need_distinct_evidence(self) -> None:
        task = self.task()
        task["failed_attempts"] = 1
        task["failure_events"] = [
            {"attempt": 1, "reason": "semantic", "evidence_refs": ["verification:" + "a" * 64]},
            {"attempt": 1, "reason": "reasoning", "evidence_refs": ["verification:" + "a" * 64]},
        ]
        with self.assertRaisesRegex(ContractError, "distinct evidence"):
            validate_task(task)

    def test_failure_evidence_references_are_bounded(self) -> None:
        task = self.task()
        task["failed_attempts"] = 1
        task["failure_events"] = [
            {
                "attempt": 1,
                "reason": "semantic",
                "evidence_refs": [f"verification:{index:064x}" for index in range(17)],
            }
        ]
        with self.assertRaisesRegex(ContractError, "one to 16"):
            validate_task(task)

    def test_completed_result_requires_evidence(self) -> None:
        result = {
            "assignment_id": "assignment-1",
            "status": "completed",
            "summary": "Done",
            "artifacts": [],
            "evidence": [],
            "unverified_claims": [],
            "remaining_risks": [],
        }
        with self.assertRaisesRegex(ResultContractError, "must contain evidence") as captured:
            validate_result(result, assignment=self.assignment())
        self.assertEqual("missing_evidence", captured.exception.reason_code)

    def test_cli_returns_only_safe_result_reason_code(self) -> None:
        invalid = {
            "assignment_id": "assignment-1",
            "status": "completed",
            "summary": "TOP_SECRET rejected output",
            "artifacts": [],
            "evidence": [],
            "unverified_claims": [],
            "remaining_risks": [],
        }
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            inputs = {
                "task.json": self.task(),
                "assignment.json": self.assignment(),
                "parent.json": self.parent_grant(),
                "result.json": invalid,
            }
            for name, value in inputs.items():
                root.joinpath(name).write_text(json.dumps(value))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "cli.py"),
                    "validate-result",
                    "--task",
                    str(root / "task.json"),
                    "--assignment",
                    str(root / "assignment.json"),
                    "--parent-grant",
                    str(root / "parent.json"),
                    "--result",
                    str(root / "result.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(2, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            {
                "ready": False,
                "error": "result validation failed",
                "reason_code": "missing_evidence",
                "repair_receipt": None,
                "learning_ready": None,
            },
            report,
        )
        self.assertNotIn("TOP_SECRET", completed.stdout)

    def test_result_enums_and_usage_reject_non_scalar_values_safely(self) -> None:
        base = {
            "assignment_id": "assignment-1",
            "status": "completed",
            "summary": "Done",
            "artifacts": [],
            "evidence": [
                {
                    "criterion_id": "line-marker",
                    "type": "command",
                    "reference": "test",
                    "outcome": "pass",
                    "details": "passed",
                }
            ],
            "unverified_claims": [],
            "remaining_risks": [],
        }
        for field, value, reason in (
            ("status", ["SECRET"], "invalid_status"),
            ("usage", {"input_tokens": float("inf")}, "invalid_usage"),
        ):
            candidate = dict(base)
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ResultContractError) as captured:
                    validate_result(candidate, assignment=self.assignment())
                self.assertEqual(reason, captured.exception.reason_code)

        for field in ("type", "outcome"):
            candidate = json.loads(json.dumps(base))
            candidate["evidence"][0][field] = ["SECRET"]
            with self.subTest(field=field):
                with self.assertRaises(ResultContractError) as captured:
                    validate_result(candidate, assignment=self.assignment())
                self.assertEqual("invalid_evidence", captured.exception.reason_code)

    def test_cli_maps_malformed_and_nonfinite_json_to_invalid_shape(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            for name, value in {
                "task.json": self.task(),
                "assignment.json": self.assignment(),
                "parent.json": self.parent_grant(),
            }.items():
                root.joinpath(name).write_text(json.dumps(value))
            for raw in ("not json TOP_SECRET", '{"usage":NaN}'):
                root.joinpath("result.json").write_text(raw)
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ORCHESTRATION_ROOT / "cli.py"),
                        "validate-result",
                        "--task",
                        str(root / "task.json"),
                        "--assignment",
                        str(root / "assignment.json"),
                        "--parent-grant",
                        str(root / "parent.json"),
                        "--result",
                        str(root / "result.json"),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                report = json.loads(completed.stdout)
                self.assertEqual(2, completed.returncode, completed.stderr)
                self.assertEqual("invalid_shape", report["reason_code"])
                self.assertNotIn("TOP_SECRET", completed.stdout)

    def test_cli_does_not_treat_missing_result_file_as_model_repair(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            for name, value in {
                "task.json": self.task(),
                "assignment.json": self.assignment(),
                "parent.json": self.parent_grant(),
            }.items():
                root.joinpath(name).write_text(json.dumps(value))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "cli.py"),
                    "validate-result",
                    "--task", str(root / "task.json"),
                    "--assignment", str(root / "assignment.json"),
                    "--parent-grant", str(root / "parent.json"),
                    "--result", str(root / "missing-result.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(2, completed.returncode)
        self.assertEqual(
            {"ready": False, "error": "filesystem operation failed"},
            json.loads(completed.stdout),
        )

    def test_failure_reference_container_is_rejected_without_type_error(self) -> None:
        task = self.task()
        task["failed_attempts"] = 1
        task["failure_events"] = [
            {"attempt": 1, "reason": "semantic", "evidence_refs": [["SECRET"]]}
        ]
        with self.assertRaisesRegex(ContractError, "safe references"):
            validate_task(task)

    def test_completed_result_rejects_failed_evidence(self) -> None:
        result = {
            "assignment_id": "assignment-1",
            "status": "completed",
            "summary": "Done",
            "artifacts": [],
            "evidence": [
                {
                    "criterion_id": "line-marker",
                    "type": "command",
                    "reference": "python3 -m unittest",
                    "outcome": "fail",
                    "details": "one failure",
                }
            ],
            "unverified_claims": [],
            "remaining_risks": [],
        }
        with self.assertRaisesRegex(ContractError, "failed or unrun"):
            validate_result(result, assignment=self.assignment())

    def test_completed_negative_review_has_separate_execution_and_verdict(self) -> None:
        value = {
            "assignment_id": "assignment-1",
            "status": "completed",
            "execution_state": "completed",
            "semantic_verdict": "not-ready",
            "admission_state": "unverified",
            "retry": {"attempt": 1, "retry_of_assignment_id": None},
            "summary": "Review completed and found a material defect",
            "artifacts": [],
            "evidence": [{
                "criterion_id": "line-marker",
                "type": "review",
                "reference": "review:material-defect",
                "outcome": "fail",
                "details": "The reviewed criterion is not satisfied",
            }],
            "unverified_claims": [],
            "remaining_risks": ["Material defect remains"],
        }
        self.assertEqual(
            "not-ready",
            validate_result(value, assignment=self.assignment())["semantic_verdict"],
        )

    def test_result_preflight_is_exact_and_non_mutating(self) -> None:
        assigned = self.assignment()
        before = json.dumps(assigned, sort_keys=True)
        preflight = result_preflight(assigned)
        self.assertEqual(before, json.dumps(assigned, sort_keys=True))
        self.assertEqual(["line-marker"], [
            item["criterion_id"] for item in preflight["template"]["evidence"]
        ])
        self.assertEqual("unverified", preflight["template"]["admission_state"])

    def test_result_preflight_states_one_evidence_entry_per_criterion(self) -> None:
        # Several findings for one criterion share its entry, at the most severe
        # outcome, so a repair never collapses a fail into a milder label.
        self.assertEqual(sorted(EVIDENCE_OUTCOMES), sorted(EVIDENCE_OUTCOME_SEVERITY))
        self.assertEqual("fail", EVIDENCE_OUTCOME_SEVERITY[0])
        rules = result_preflight(self.assignment())["consistency_rules"]
        self.assertIn(
            "each required criterion has exactly one evidence entry: put several "
            "findings for it in that entry's details, and set its outcome to the most "
            "severe of them (fail, then not_run, then observed, then pass)",
            rules,
        )

    def test_result_preflight_binds_retry_predecessor(self) -> None:
        assigned = self.assignment()
        assigned.update(
            attempt=2,
            retry_of_assignment_id="assignment-previous",
        )
        preflight = result_preflight(assigned)
        value = preflight["template"]
        value["summary"] = "Retry completed"
        value["evidence"][0].update(
            reference="review:retry", details="Criterion observed on retry"
        )
        self.assertEqual(
            "assignment-previous",
            validate_result(value, assignment=assigned)["retry"][
                "retry_of_assignment_id"
            ],
        )

    def test_result_preflight_uses_project_mapped_workspace_write_agent(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            agents = root / ".codex/agents"
            orchestration = root / ".codex/orchestration"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            (agents / "implementation_worker.toml").write_text(
                'name = "implementation_worker"\n'
                'description = "Bounded implementation worker"\n'
                'sandbox_mode = "workspace-write"\n'
                'developer_instructions = "Complete the bounded assignment."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps(
                    {"version": 1, "roles": {"coding": "implementation_worker"}}
                )
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test Runner"], cwd=root, check=True
            )
            subprocess.run(["git", "add", ".codex"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test mapping"], cwd=root, check=True
            )
            task = self.task()
            task.update(family="coding", owned_paths=["source.txt"])
            policy = load_effective_policy(
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            route = route_task(task, policy)
            assignment = self.assignment()
            assignment.update(
                role=route["role"],
                model=route["model"],
                effort=route["effort"],
                owned_paths=["source.txt"],
            )
            assignment["permissions"]["filesystem"] = ["source.txt:write"]
            grant = self.parent_grant()
            grant["filesystem"] = ["source.txt:write"]
            for name, value in {
                "task.json": task,
                "assignment.json": assignment,
                "parent.json": grant,
            }.items():
                (root / name).write_text(json.dumps(value))

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "cli.py"),
                    "result-preflight",
                    "--project-root", str(root),
                    "--task", str(root / "task.json"),
                    "--assignment", str(root / "assignment.json"),
                    "--parent-grant", str(root / "parent.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ready"])
        self.assertEqual("assignment-1", report["template"]["assignment_id"])

    def test_legacy_retry_result_inherits_assignment_lineage(self) -> None:
        assigned = self.assignment()
        assigned.update(
            attempt=2,
            retry_of_assignment_id="assignment-previous",
        )
        value = {
            "assignment_id": assigned["assignment_id"],
            "status": "completed",
            "summary": "Legacy retry completed",
            "artifacts": [],
            "evidence": [{
                "criterion_id": "line-marker",
                "type": "review",
                "reference": "review:legacy-retry",
                "outcome": "pass",
                "details": "Criterion observed",
            }],
            "unverified_claims": [],
            "remaining_risks": [],
        }
        self.assertEqual(
            value, validate_result(value, assignment=assigned)
        )

    def assignment(self) -> dict:
        return {
            "assignment_id": "assignment-1",
            "task_id": "task-1",
            "objective": "Read one fixture",
            "role": "codex_agents_researcher",
            "model": "gpt-6.1-sol",
            "effort": "high",
            "attempt": 1,
            "meaningful": True,
            "owned_paths": [],
            "context": ["source.txt"],
            "permissions": {
                "tools": ["shell"],
                "filesystem": ["source.txt:read"],
                "network": False,
                "mcps": [],
                "external_effects": False,
            },
            "expected_output": "One marker",
            "required_evidence": [
                {"id": "line-marker", "description": "line number"}
            ],
            "verification_required": True,
            "bounds": {
                "max_attempts": 2,
                "max_steers_per_attempt": 1,
                "max_no_progress_events": 2,
                "wait_seconds": 60,
                "timeout_seconds": 600,
                "max_tool_calls": 30,
            },
            "stopping_conditions": ["marker found"],
            "prohibited_actions": ["network"],
        }

    def test_assignment_accepts_bounded_input(self) -> None:
        task = self.task()
        self.assertEqual(
            "assignment-1",
            validate_assignment(
                self.assignment(),
                task=task,
                route=route_task(task, self.policy),
                parent_grant=self.parent_grant(),
            )["assignment_id"],
        )

    def test_assignment_rejects_unlisted_authority(self) -> None:
        assignment = self.assignment()
        assignment["permissions"]["publish"] = True
        with self.assertRaisesRegex(ContractError, "permissions has unknown fields"):
            self.validate_assignment(assignment)

    def parent_grant(self) -> dict:
        return {
            "tools": ["shell"],
            "filesystem": ["source.txt:read"],
            "network": False,
            "mcps": [],
            "external_effects": False,
        }

    def validate_assignment(self, assignment: dict) -> dict:
        task = self.task()
        return validate_assignment(
            assignment,
            task=task,
            route=route_task(task, self.policy),
            parent_grant=self.parent_grant(),
        )

    def test_assignment_rejects_route_override(self) -> None:
        assignment = self.assignment()
        assignment["model"] = "gpt-6-astra"
        with self.assertRaisesRegex(ContractError, "model does not match routed value"):
            self.validate_assignment(assignment)

    def test_assignment_rejects_parent_authority_expansion(self) -> None:
        assignment = self.assignment()
        assignment["permissions"]["network"] = True
        with self.assertRaisesRegex(ContractError, "network exceeds parent grant"):
            self.validate_assignment(assignment)

    def test_assignment_rejects_excessive_bound(self) -> None:
        assignment = self.assignment()
        assignment["bounds"]["max_attempts"] = 3
        with self.assertRaisesRegex(ContractError, "exceeds routed policy bound"):
            self.validate_assignment(assignment)

    def test_assignment_attempt_must_match_task_failure_history(self) -> None:
        assignment = self.assignment()
        assignment["attempt"] = 2
        assignment["retry_of_assignment_id"] = "assignment-0"
        with self.assertRaisesRegex(ContractError, "does not match task failure history"):
            self.validate_assignment(assignment)

    def test_third_assignment_is_outside_the_attempt_bound(self) -> None:
        task = self.task()
        task.update(
            failed_attempts=2,
            failure_events=[
                {"attempt": 1, "reason": "transient", "evidence_refs": ["host:1"]},
                {"attempt": 2, "reason": "transient", "evidence_refs": ["host:2"]},
            ],
        )
        assignment = self.assignment()
        assignment["attempt"] = 3
        assignment["retry_of_assignment_id"] = "assignment-2"
        assignment["bounds"]["max_attempts"] = 3

        with self.assertRaisesRegex(ContractError, "exceeds routed policy bound"):
            validate_assignment(
                assignment,
                task=task,
                route=route_task(task, self.policy),
                parent_grant=self.parent_grant(),
            )


    def test_assignment_owned_paths_must_fit_even_when_task_list_is_empty(self) -> None:
        assignment = self.assignment()
        assignment["owned_paths"] = ["unassigned.py"]
        with self.assertRaisesRegex(ContractError, "exceed task ownership"):
            self.validate_assignment(assignment)

    def test_assignment_child_filesystem_write_requires_mapped_writable_route(self) -> None:
        assignment = self.assignment()
        assignment["permissions"]["filesystem"] = ["source.txt:write"]
        grant = self.parent_grant()
        grant["filesystem"] = ["source.txt:write"]
        task = self.task()
        with self.assertRaisesRegex(ContractError, "explicitly mapped"):
            validate_assignment(
                assignment,
                task=task,
                route=route_task(task, self.policy),
                parent_grant=grant,
            )

    def test_assignment_accepts_mapped_coding_write_within_parent_grant(self) -> None:
        task = self.task()
        task.update(family="coding", owned_paths=["source.txt"])
        policy = deepcopy(self.policy)
        policy["project_agent_roles"] = {"coding": "implementation_worker"}
        policy["project_agent_role_sandboxes"] = {"coding": "workspace-write"}
        policy["project_agent_mapping_digest"] = "a" * 64
        route = route_task(task, policy)
        assignment = self.assignment()
        assignment.update(
            role=route["role"],
            model=route["model"],
            effort=route["effort"],
            owned_paths=["source.txt"],
        )
        assignment["permissions"]["filesystem"] = ["source.txt:write"]
        grant = self.parent_grant()
        grant["filesystem"] = ["source.txt:write"]

        checked = validate_assignment(
            assignment,
            task=task,
            route=route,
            parent_grant=grant,
        )

        self.assertEqual(["source.txt:write"], checked["permissions"]["filesystem"])

    def test_parent_grant_rejects_duplicate_capability(self) -> None:
        grant = self.parent_grant()
        grant["tools"] = ["shell", "shell"]
        with self.assertRaisesRegex(ContractError, "unique strings"):
            validate_parent_grant(grant)

    def test_parent_grant_rejects_name_only_mcp_authority(self) -> None:
        grant = self.parent_grant()
        grant["mcps"] = ["mutable-name"]
        with self.assertRaisesRegex(ContractError, "must be empty in version 1"):
            validate_parent_grant(grant)

    def test_assignment_rejects_name_only_mcp_authority(self) -> None:
        assignment = self.assignment()
        assignment["permissions"]["mcps"] = ["mutable-name"]
        with self.assertRaisesRegex(ContractError, "must be empty in version 1"):
            self.validate_assignment(assignment)

    def completed_result(self) -> dict:
        return {
            "assignment_id": "assignment-1",
            "status": "completed",
            "summary": "Done",
            "artifacts": [],
            "evidence": [
                {
                    "criterion_id": "line-marker",
                    "type": "command",
                    "reference": "sed -n 2p source.txt",
                    "outcome": "pass",
                    "details": "BETA",
                }
            ],
            "unverified_claims": [],
            "remaining_risks": [],
        }

    def test_result_must_match_assignment(self) -> None:
        result = self.completed_result()
        result["assignment_id"] = "other"
        with self.assertRaisesRegex(ContractError, "does not match assignment"):
            validate_result(result, assignment=self.assignment())

    def test_completed_result_covers_every_required_criterion(self) -> None:
        assignment = self.assignment()
        assignment["required_evidence"].append(
            {"id": "second-check", "description": "second check"}
        )
        with self.assertRaisesRegex(ContractError, "missing required evidence"):
            validate_result(self.completed_result(), assignment=assignment)

    def test_result_rejects_duplicate_evidence_criterion(self) -> None:
        result = self.completed_result()
        result["evidence"].append(dict(result["evidence"][0]))
        with self.assertRaisesRegex(ContractError, "duplicate evidence criterion_id"):
            validate_result(result, assignment=self.assignment())


if __name__ == "__main__":
    unittest.main()
