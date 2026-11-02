"""Quality-first aggregation for recorded model and effort trials."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from .contracts import ContractError, validate_evaluation_record


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        try:
            records.append(validate_evaluation_record(data))
        except ContractError as exc:
            raise ContractError(f"{path}:{line_number}: {exc}") from exc
    if not records:
        raise ContractError("evaluation record file is empty")
    return records


def _usage_sum(records: Iterable[dict[str, Any]], field: str) -> int | None:
    values = [record.get(field) for record in records]
    if any(value is None for value in values):
        return None
    return sum(values)


def _stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "trials": len(records),
        "task_ids": sorted({record["task_id"] for record in records}),
        "pass_rate": sum(record["passed"] for record in records) / len(records),
        "quality_score": mean(record["quality_score"] for record in records),
        "repair_rate": sum(record["repairs"] > 0 for record in records) / len(records),
        "repairs": sum(record["repairs"] for record in records),
        "latency_ms": sum(record["latency_ms"] for record in records),
        "tool_calls": sum(record["tool_calls"] for record in records),
        "input_tokens": _usage_sum(records, "input_tokens"),
        "cached_tokens": _usage_sum(records, "cached_tokens"),
        "output_tokens": _usage_sum(records, "output_tokens"),
        "reasoning_tokens": _usage_sum(records, "reasoning_tokens"),
        "cost": None
        if any(record.get("cost") is None for record in records)
        else sum(record["cost"] for record in records),
    }


def evaluate(records: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    thresholds = policy["quality_thresholds"]
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_family[record["family"]].append(record)

    required_families = set(thresholds.get("required_families", []))
    families: dict[str, Any] = {}
    ready = True
    for missing_family in sorted(required_families - set(by_family)):
        ready = False
        families[missing_family] = {"ready": False, "errors": ["required family is missing"]}
    for family, family_records in sorted(by_family.items()):
        baselines = [record for record in family_records if record["baseline"]]
        candidates = [record for record in family_records if not record["baseline"]]
        baseline_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in baselines:
            baseline_by_task[record["task_id"]].append(record)
        duplicate_baselines = sorted(
            task_id for task_id, items in baseline_by_task.items() if len(items) != 1
        )
        if not baselines or duplicate_baselines:
            ready = False
            families[family] = {
                "ready": False,
                "errors": [
                    "one baseline is required for each evaluated task"
                    if not duplicate_baselines
                    else f"duplicate baselines: {', '.join(duplicate_baselines)}"
                ],
            }
            continue
        baseline_stats = _stats(baselines)
        baseline_ready = (
            baseline_stats["quality_score"] >= thresholds["minimum_score"]
            and baseline_stats["pass_rate"] == 1.0
        )
        grouped_candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in candidates:
            grouped_candidates[(record["model"], record["effort"])].append(record)
        candidate_results: list[dict[str, Any]] = []
        baseline_tasks = set(baseline_by_task)
        for (model, effort), candidate_records in sorted(grouped_candidates.items()):
            stats = _stats(candidate_records)
            covered_tasks = set(stats["task_ids"])
            accepted = (
                baseline_ready
                and len(candidate_records) >= thresholds["minimum_trials_per_family"]
                and covered_tasks == baseline_tasks
                and stats["pass_rate"] >= baseline_stats["pass_rate"]
                and stats["quality_score"] >= thresholds["minimum_score"]
                and stats["quality_score"] >= baseline_stats["quality_score"]
                and stats["repair_rate"] <= thresholds["maximum_repair_rate"]
            )
            candidate_results.append(
                {"model": model, "effort": effort, "accepted": accepted, **stats}
            )
        family_ready = baseline_ready and any(item["accepted"] for item in candidate_results)
        ready = ready and family_ready
        families[family] = {
            "ready": family_ready,
            "baseline_ready": baseline_ready,
            "baseline": baseline_stats,
            "candidates": candidate_results,
        }
    return {"ready": ready, "families": families, "record_count": len(records)}
