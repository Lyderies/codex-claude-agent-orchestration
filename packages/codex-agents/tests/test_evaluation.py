from __future__ import annotations

import unittest
from copy import deepcopy

from tests.support import ORCHESTRATION_ROOT

from codex_agents.evaluation import evaluate
from codex_agents.routing import load_policy


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = deepcopy(
            load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json")
        )
        cls.policy["quality_thresholds"]["required_families"] = ["coding"]

    def record(self, task_id: str, baseline: bool, **overrides: object) -> dict:
        record = {
            "task_id": task_id,
            "family": "coding",
            "model": "gpt-6-astra" if baseline else "gpt-5.6-luna",
            "effort": "high" if baseline else "medium",
            "baseline": baseline,
            "passed": True,
            "quality_score": 1.0,
            "repairs": 0,
            "latency_ms": 1000 if baseline else 500,
            "tool_calls": 2,
            "input_tokens": 100,
            "cached_tokens": 0,
            "output_tokens": 50,
            "reasoning_tokens": 20,
            "cost": None,
            "notes": "fixture",
        }
        record.update(overrides)
        return record

    def test_candidate_is_accepted_only_after_matching_quality(self) -> None:
        records = [
            self.record("code-1", True),
            self.record("code-2", True),
            self.record("code-1", False),
            self.record("code-2", False),
        ]
        report = evaluate(records, self.policy)
        self.assertTrue(report["ready"])
        self.assertTrue(report["families"]["coding"]["candidates"][0]["accepted"])

    def test_quality_regression_rejects_candidate(self) -> None:
        records = [
            self.record("code-1", True),
            self.record("code-2", True),
            self.record("code-1", False, quality_score=0.8),
            self.record("code-2", False),
        ]
        report = evaluate(records, self.policy)
        self.assertFalse(report["ready"])

    def test_missing_task_coverage_rejects_candidate(self) -> None:
        records = [
            self.record("code-1", True),
            self.record("code-2", True),
            self.record("code-1", False),
            self.record("code-1", False),
        ]
        report = evaluate(records, self.policy)
        self.assertFalse(report["ready"])

    def test_missing_required_family_rejects_matrix(self) -> None:
        policy = deepcopy(self.policy)
        policy["quality_thresholds"]["required_families"] = ["coding", "research"]
        records = [
            self.record("code-1", True),
            self.record("code-2", True),
            self.record("code-1", False),
            self.record("code-2", False),
        ]
        report = evaluate(records, policy)
        self.assertFalse(report["ready"])
        self.assertEqual(
            ["required family is missing"], report["families"]["research"]["errors"]
        )


if __name__ == "__main__":
    unittest.main()
