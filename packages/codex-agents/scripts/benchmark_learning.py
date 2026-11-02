#!/usr/bin/env python3
"""Measure local learning telemetry overhead without model or network calls."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATION_ROOT = REPOSITORY_ROOT / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from codex_agents.learning_adapters import normalize_hook  # noqa: E402
from codex_agents.learning_store import (  # noqa: E402
    CollectorClient,
    CollectorServer,
    LearningLedger,
    LearningState,
)


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def _measure(iterations: int, operation: Callable[[int], None]) -> list[float]:
    durations = []
    for index in range(iterations):
        started = time.perf_counter_ns()
        operation(index)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    return durations


def _payload(index: int) -> dict:
    return {
        "session_id": "benchmark-session",
        "cwd": "ignored-after-binding",
        "hook_event_name": "PreToolUse",
        "tool_name": "exec_command",
        "tool_use_id": f"benchmark-tool-{index}",
        "turn_id": "benchmark-turn",
        "model": "gpt-6-luna",
        "tool_input": {"cmd": "TEST_SECRET_NOT_RETAINED"},
    }


def benchmark(iterations: int, hook_iterations: int, limits_path: Path | None) -> dict:
    with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
        base = Path(directory)
        state = LearningState(base / "state")
        project = base / "project"
        project.mkdir()
        ledger = LearningLedger(state)
        project_id = ledger.register_project(project)
        key = state.digest_key()
        normalize_times = _measure(
            iterations,
            lambda index: normalize_hook(
                _payload(index),
                digest_key=key,
                client_version="codex-cli-0.153.0",
                observed_at_ms=index + 1,
            ),
        )
        records = [
            normalize_hook(
                _payload(index + iterations),
                digest_key=key,
                client_version="codex-cli-0.153.0",
                observed_at_ms=index + 1,
            )
            for index in range(iterations)
        ]
        ledger_times = _measure(
            iterations,
            lambda index: ledger.append(
                project_id, records[index], producer="hook", now_ms=index + 1
            ),
        )
        server = CollectorServer(ledger)
        server.start()
        try:
            client = CollectorClient(state.socket, timeout=2)
            ipc_records = [
                normalize_hook(
                    _payload(index + iterations * 2),
                    digest_key=key,
                    client_version="codex-cli-0.153.0",
                    observed_at_ms=index + 1,
                )
                for index in range(iterations)
            ]

            def collect(index: int) -> None:
                binding = client.bind(
                    project, "benchmark-session", "hook", "codex-cli-0.153.0"
                )
                client.ingest(binding, ipc_records[index])

            ipc_times = _measure(
                iterations,
                collect,
            )

            hook_script = ORCHESTRATION_ROOT / "learning_hook.py"

            def run_hook(index: int) -> None:
                payload = _payload(index + iterations * 3)
                payload["cwd"] = str(project)
                payload["session_id"] = f"benchmark-hook-{index}"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(hook_script),
                        "--state-root",
                        str(state.root),
                        "--client-version",
                        "codex-cli-0.153.0",
                    ],
                    input=json.dumps(payload),
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("hook process failed")

            hook_times = _measure(hook_iterations, run_hook)
        finally:
            server.stop()
            ledger.close()
    measurements = {
        "normalize": summarize(normalize_times),
        "ledger_commit": summarize(ledger_times),
        "collector_round_trip": summarize(ipc_times),
        "cold_hook_process": summarize(hook_times),
    }
    limits = None
    violations: list[str] = []
    if limits_path is not None:
        limits_data = json.loads(limits_path.read_text())
        limits = limits_data.get("p95_ms")
        if not isinstance(limits, dict):
            raise ValueError("overhead limits are invalid")
        violations = sorted(
            name
            for name, result in measurements.items()
            if not isinstance(limits.get(name), (int, float))
            or result["p95_ms"] > limits[name]
        )
    return {
        "ready": not violations,
        "client": "codex-cli-0.153.0",
        "iterations": iterations,
        "cold_hook_iterations": hook_iterations,
        "measurements": measurements,
        "limits_p95_ms": limits,
        "violations": violations,
        "model_calls": 0,
        "network_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--hook-iterations", type=int, default=20)
    parser.add_argument("--limits", type=Path)
    args = parser.parse_args()
    if args.iterations < 20 or args.hook_iterations < 5:
        parser.error("iterations must be at least 20 and hook iterations at least 5")
    report = benchmark(args.iterations, args.hook_iterations, args.limits)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
