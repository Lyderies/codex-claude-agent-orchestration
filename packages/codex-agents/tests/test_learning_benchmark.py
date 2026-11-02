from __future__ import annotations

import importlib.util
import unittest

from tests.support import REPOSITORY_ROOT


SPEC = importlib.util.spec_from_file_location(
    "codex_agents_learning_benchmark",
    REPOSITORY_ROOT / "scripts" / "benchmark_learning.py",
)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class LearningBenchmarkTests(unittest.TestCase):
    def test_summary_uses_observed_distribution(self) -> None:
        report = benchmark.summarize([float(index) for index in range(1, 101)])
        self.assertEqual(50.5, report["p50_ms"])
        self.assertEqual(95.0, report["p95_ms"])
        self.assertEqual(100.0, report["max_ms"])


if __name__ == "__main__":
    unittest.main()
