"""Closed contracts for the local learning ledger.

The validators deliberately accept metadata only.  Project identity, source sequence,
and ingest time are assigned by the collector and are not fields a producer may claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import base64
import binascii
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractError, RESULT_VALIDATION_REASON_CODES


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 65_536
MAX_ID_LENGTH = 160
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

RECORD_TYPES = {
    "event",
    "outcome",
    "feedback",
    "evaluation_case",
    "evaluation_assignment",
    "qa_evidence",
    "evidence_review",
    "evaluation_trial",
    "candidate",
    "comparison",
    "canary_report",
    "grader_calibration",
    "owner_grant",
    "promotion",
    "learning_export",
    "provider_identity",
    "provider_headroom",
    "provider_billing",
    "billing_attestation",
    "admission_decision",
    "dispatch_intent",
    "host_run_profile",
    "probe_admission",
}
SOURCES = {
    "hook", "app_server", "host", "evaluator", "verifier", "owner", "lifecycle",
    "provider",
}
EVENT_CODES = {
    "session_started",
    "session_ended",
    "turn_started",
    "turn_stop_observed",
    "turn_completed",
    "turn_failed",
    "turn_interrupted",
    "subagent_started",
    "subagent_stop_observed",
    "tool_started",
    "tool_output_observed",
    "item_completed",
    "model_rerouted",
    "usage_snapshot",
    "hook_completed",
    "no_progress",
    "retry",
    "repair",
    "escalation",
}
ROLES = {
    "codex_agents_researcher",
    "codex_agents_planner",
    "codex_agents_implementation_worker",
    "codex_agents_security_reviewer",
    "codex_agents_verification_worker",
    "claude_agents_researcher",
    "claude_agents_planner",
    "claude_agents_implementation_worker",
    "claude_agents_security_reviewer",
    "claude_agents_verification_worker",
}
TASK_FAMILIES = {
    "inventory",
    "research",
    "planning",
    "coding",
    "security",
    "verification",
}
STATUSES = {
    "started",
    "observed",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "rejected",
    "unknown",
}
STOP_REASONS = {
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "blocked",
    "timeout",
    "no_progress",
    "other",
    "unknown",
}
COVERAGE_CLASSES = {
    "end_to_end_complete",
    "root_turn_complete",
    "lifecycle_partial",
    "handshake_only",
    "partial",
    "conflicting",
    "unknown",
}
OUTCOME_STATUSES = {
    "accepted",
    "rejected",
    "unverified",
    "failed",
    "cancelled",
    "interrupted",
}
EXECUTION_STATES = {
    "completed", "failed", "timed_out", "interrupted", "cancelled", "invalid"
}
SEMANTIC_VERDICTS = {
    "pass", "ready", "ready-with-corrections", "not-ready", "blocked", "reject",
    "not-applicable",
}
ADMISSION_STATES = {"unverified", "accepted", "rejected", "pending"}
FEEDBACK_LABELS = {"accepted", "rejected", "corrected"}
FEEDBACK_REASONS = {
    "quality",
    "incorrect",
    "incomplete",
    "unsafe",
    "usage",
    "scope",
    "other",
}
CORPUS_STATES = {"development", "promotion_holdout", "canary"}
CANDIDATE_TARGETS = {
    "routing",
    "model_effort",
    "topology",
    "skill",
    "prompt",
    "context",
    "verification",
    "bounds",
}
CANDIDATE_EFFECTS = {"optimize", "narrow", "strengthen"}
CANDIDATE_STATUSES = {
    "proposed",
    "evaluated",
    "rejected",
    "canary_approved",
    "canary_failed",
    "canary_passed",
    "promoted",
    "reverted",
}
GRANT_KINDS = {"canary", "promotion"}
PROMOTION_ACTIONS = {
    "evaluated",
    "rejected",
    "canary_started",
    "canary_passed",
    "canary_failed",
    "promoted",
    "reverted",
    "drift_detected",
}
COMPARISON_DECISIONS = {"promote", "reject", "insufficient_evidence"}
COMPARISON_REASONS = {
    "duplicate_case", "minimum_cases", "holdout_invalid", "corpus_leakage",
    "client_coverage", "population_incomplete", "task_slice_mismatch",
    "duplicate_trial", "matched_population", "configuration_mismatch", "coverage",
    "grader_calibration", "deterministic", "security", "required_criteria",
    "acceptance", "case_acceptance_regression", "case_criterion_regression",
    "case_quality_regression", "mean_quality",
    "absolute_quality", "terminal_failure", "repair_rate", "usage_missing",
    "usage_regression", "material_improvement", "paired_confidence",
}
PROTECTED_TARGETS = {"authority", "privacy", "mandatory_verification"}
TARGET_CHANGE_PATHS = {
    "routing": {"routing.selection"},
    "model_effort": {"routing.model_effort"},
    "topology": {"orchestration.topology"},
    "skill": {"skills.selection"},
    "prompt": {"prompts.template_digest"},
    "context": {"context.selection"},
    "verification": {"verification.additional_checks"},
    "bounds": {"bounds.limits"},
}
SUPPORTED_MODELS = {
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "gpt-6-astra",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
}
SUPPORTED_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
BOUND_KEYS = {
    "max_attempts",
    "max_steers_per_attempt",
    "max_no_progress_events",
    "wait_seconds",
    "timeout_seconds",
    "max_tool_calls",
}

USAGE_FIELDS = {
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "reasoning_tokens",
    "model_calls",
    "tool_calls",
    "latency_ms",
    "cost_microunits",
}


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ContractError("record input contains a non-finite number")


def parse_bounded_json(
    raw: bytes | str, *, max_bytes: int = MAX_RECORD_BYTES
) -> dict[str, Any]:
    """Decode one bounded JSON object while rejecting duplicate keys and binary data."""

    value = parse_bounded_json_value(raw, max_bytes=max_bytes)
    if not isinstance(value, dict):
        raise ContractError("record must be an object")
    return value


def parse_bounded_json_value(
    raw: bytes | str, *, max_bytes: int = MAX_RECORD_BYTES
) -> Any:
    """Decode one bounded JSON value while rejecting duplicate keys and binary data."""

    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes):
        raise ContractError("record input must be UTF-8 JSON")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
        or not encoded
        or len(encoded) > max_bytes
        or b"\x00" in encoded
    ):
        raise ContractError("record input is empty, binary, or oversized")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_nonfinite_json,
        )
    except UnicodeDecodeError as exc:
        raise ContractError("record input must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ContractError("record input is invalid JSON") from exc
    return value


def canonical_json(data: Any) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def keyed_digest(key: bytes, value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def external_id(key: bytes, namespace: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("external identifier must be a non-empty string")
    return f"{namespace}:{keyed_digest(key, value)}"


def _closed(data: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError(f"{label} must be an object")
    missing = sorted(fields - data.keys())
    if missing:
        raise ContractError(f"{label} missing required fields")
    unknown = sorted(data.keys() - fields)
    if unknown:
        raise ContractError(f"{label} has unknown fields")
    return data


def _identifier(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ContractError(f"{field} must be an opaque identifier")


def _digest(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
        raise ContractError(f"{field} must be a lowercase SHA-256 digest")


def _enum(value: Any, choices: set[str], field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or value not in choices:
        raise ContractError(f"{field} has an unsupported value")


def provider_for_model(model: Any) -> str:
    """Derive the provider from a model name; records never store it separately."""

    if isinstance(model, str) and model.startswith("gpt-"):
        return "codex"
    if isinstance(model, str) and model.startswith("claude-"):
        return "claude"
    raise ContractError("model does not belong to a supported provider")


def provider_for_role(role: Any) -> str:
    if isinstance(role, str) and role.startswith("codex_agents_"):
        return "codex"
    if isinstance(role, str) and role.startswith("claude_agents_"):
        return "claude"
    raise ContractError("role does not belong to a supported provider")


def _provider_consistent(model: Any, role: Any, label: str) -> None:
    if provider_for_model(model) != provider_for_role(role):
        raise ContractError(f"{label} model and role belong to different providers")


def _integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ContractError(f"{field} must be an integer from {minimum} to {maximum}")


def _number(
    value: Any,
    field: str,
    *,
    minimum: float = 0,
    maximum: float = 1,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ContractError(f"{field} must be a number from {minimum} to {maximum}")


def _boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be boolean")


def _digest_list(
    value: Any, field: str, *, minimum: int = 0, maximum: int = 64
) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ContractError(f"{field} must be a bounded digest array")
    if len(value) != len(set(value)):
        raise ContractError(f"{field} must not contain duplicates")
    for item in value:
        _digest(item, field)


def _base(data: dict[str, Any], record_type: str) -> None:
    if data["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported learning schema version")
    if data["record_type"] != record_type:
        raise ContractError(f"record_type must be {record_type}")
    _identifier(data["record_id"], "record_id")


def _usage(value: Any) -> None:
    data = _closed(value, USAGE_FIELDS, "usage")
    for field in USAGE_FIELDS:
        _integer(data[field], f"usage.{field}", nullable=True)
    cached = data["cached_tokens"]
    inputs = data["input_tokens"]
    reasoning = data["reasoning_tokens"]
    outputs = data["output_tokens"]
    if cached is not None and inputs is not None and cached > inputs:
        raise ContractError("usage.cached_tokens cannot exceed input_tokens")
    if reasoning is not None and outputs is not None and reasoning > outputs:
        raise ContractError("usage.reasoning_tokens cannot exceed output_tokens")


EVENT_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "run_id",
    "turn_id",
    "assignment_id",
    "parent_run_id",
    "observed_at_ms",
    "event_code",
    "model",
    "effort",
    "role",
    "task_family",
    "task_slice",
    "topology",
    "configuration_digest",
    "status",
    "stop_reason",
    "tool_digest",
    "item_digest",
    "coverage",
    "usage",
}

HOST_OBSERVATION_FIELDS = {
    "run_id", "run_handle", "client_version", "result_attempts", "terminal_state"
}


def validate_host_observation(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("host observation has unknown or missing fields")
    fields = set(data)
    legacy_fields = {"run_id", "client_version", "result_attempts"}
    trusted_fields = {"run_handle", "result_attempts"}
    if fields not in {
        frozenset(legacy_fields),
        frozenset(legacy_fields | {"terminal_state"}),
        frozenset(trusted_fields),
        frozenset(trusted_fields | {"terminal_state"}),
    }:
        raise ContractError("host observation has unknown or missing fields")
    if "run_handle" in data:
        if not isinstance(data["run_handle"], str) or not re.fullmatch(
            r"hrn_[0-9a-f]{48}", data["run_handle"]
        ):
            raise ContractError("run_handle is invalid")
    else:
        for field in ("run_id", "client_version"):
            _identifier(data[field], field)
    if "terminal_state" in data:
        _enum(data["terminal_state"], EXECUTION_STATES, "terminal_state")
    attempts = data["result_attempts"]
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise ContractError("result_attempts must contain one result and at most one repair")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or set(attempt) != {
            "status",
            "reason_code",
            "receipt_id",
        }:
            raise ContractError("result_attempts contains an invalid entry")
        expected_status = "accepted" if index == len(attempts) - 1 else "rejected"
        if attempt["status"] != expected_status:
            raise ContractError("result_attempts must end in one accepted result")
        reason_code = attempt["reason_code"]
        receipt_id = attempt["receipt_id"]
        if expected_status == "accepted":
            if reason_code is not None or receipt_id is not None:
                raise ContractError(
                    "accepted result attempt cannot have rejection metadata"
                )
        elif (
            not isinstance(reason_code, str)
            or reason_code not in RESULT_VALIDATION_REASON_CODES
            or not isinstance(receipt_id, str)
            or not re.fullmatch(r"rpr_[0-9a-f]{48}", receipt_id)
        ):
            raise ContractError("rejected result attempt lacks a valid receipt")
    return data


def load_released_candidate_digests(
    path: Path,
    *,
    verify_signature: Callable[[bytes, bytes], bool] | None = None,
    require_signature_verification: bool = True,
) -> frozenset[str]:
    """Load an owner-authenticated allowlist for promoted general candidates."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("learning release registry is invalid") from exc
    if not isinstance(data, dict) or set(data) != {
        "version", "candidate_digests", "signature_b64"
    }:
        raise ContractError("learning release registry is invalid")
    values = data["candidate_digests"]
    if (
        data["version"] != 1
        or not isinstance(values, list)
        or len(values) > 256
        or values != sorted(set(values))
    ):
        raise ContractError("learning release registry is invalid")
    for value in values:
        _digest(value, "released candidate digest")
    signature_b64 = data["signature_b64"]
    if not values:
        if signature_b64 is not None:
            raise ContractError("empty learning release registry must be unsigned")
        return frozenset()
    if not isinstance(signature_b64, str):
        raise ContractError("learning release registry is not owner authenticated")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ContractError("learning release registry signature is invalid") from exc
    payload = canonical_json({"version": 1, "candidate_digests": values})
    if not signature or len(signature) > 16_384:
        raise ContractError("learning release registry signature is invalid")
    if require_signature_verification and (
        verify_signature is None or not verify_signature(payload, signature)
    ):
        raise ContractError("learning release registry signature is invalid")
    return frozenset(values)


def validate_event(data: Any) -> dict[str, Any]:
    data = _closed(data, EVENT_FIELDS, "event")
    _base(data, "event")
    for field in ("run_id",):
        _identifier(data[field], field)
    for field in ("turn_id", "assignment_id", "parent_run_id"):
        _identifier(data[field], field, nullable=True)
    _integer(data["observed_at_ms"], "observed_at_ms")
    _enum(data["event_code"], EVENT_CODES, "event_code")
    _identifier(data["model"], "model", nullable=True)
    _enum(data["effort"], SUPPORTED_EFFORTS, "effort", nullable=True)
    _enum(data["role"], ROLES, "role", nullable=True)
    if data["model"] in SUPPORTED_MODELS and data["role"] is not None:
        _provider_consistent(data["model"], data["role"], "event")
    _enum(data["task_family"], TASK_FAMILIES, "task_family", nullable=True)
    _identifier(data["task_slice"], "task_slice", nullable=True)
    _identifier(data["topology"], "topology", nullable=True)
    _digest(data["configuration_digest"], "configuration_digest", nullable=True)
    _enum(data["status"], STATUSES, "status")
    _enum(data["stop_reason"], STOP_REASONS, "stop_reason", nullable=True)
    _digest(data["tool_digest"], "tool_digest", nullable=True)
    _digest(data["item_digest"], "item_digest", nullable=True)
    _enum(data["coverage"], COVERAGE_CLASSES, "coverage")
    _usage(data["usage"])
    return data


OUTCOME_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "run_id",
    "assignment_id",
    "task_slice",
    "fixture_digest",
    "model",
    "effort",
    "role",
    "topology",
    "max_parallel",
    "verification_required",
    "verifier_digest",
    "bounds_digest",
    "package_digest",
    "policy_digest",
    "agent_digest",
    "skill_digests",
    "prompt_digest",
    "client_digest",
    "configuration_digest",
    "observed_at_ms",
    "status",
    "execution_state",
    "semantic_verdict",
    "admission_state",
    "retry_attempt",
    "retry_of_assignment_id",
    "deterministic_pass",
    "security_pass",
    "required_criteria_total",
    "required_criteria_passed",
    "quality_micros",
    "repairs",
    "repair_reason_codes",
    "delegation_overhead_tokens",
    "no_progress_events",
    "terminal_failure",
    "verifier_disagreement",
    "accepted",
    "coverage",
    "evaluation_candidate_digest",
    "evaluation_case_digest",
    "evidence_digests",
    "usage",
    "usage_sources",
}


def validate_outcome(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("outcome must be an object")
    axis_fields = {
        "execution_state", "semantic_verdict", "admission_state",
        "retry_attempt", "retry_of_assignment_id",
    }
    optional_fields = axis_fields | {"usage_sources"}
    missing = sorted((OUTCOME_FIELDS - optional_fields) - data.keys())
    unknown = sorted(data.keys() - OUTCOME_FIELDS)
    present_axes = axis_fields & data.keys()
    if missing:
        raise ContractError("outcome missing required fields")
    if unknown:
        raise ContractError("outcome has unknown fields")
    if present_axes and present_axes != axis_fields:
        raise ContractError("outcome has a partial separated-axis contract")
    _base(data, "outcome")
    for field in ("run_id", "assignment_id", "task_slice"):
        _identifier(data[field], field)
    _identifier(data["model"], "model")
    _enum(data["effort"], {"low", "medium", "high", "xhigh", "max", "ultra"}, "effort")
    _enum(data["role"], ROLES, "role")
    _provider_consistent(data["model"], data["role"], "outcome")
    _enum(data["topology"], {"single", "delegated", "parallel"}, "topology")
    for field in (
        "package_digest",
        "policy_digest",
        "agent_digest",
        "prompt_digest",
        "client_digest",
        "configuration_digest",
        "fixture_digest",
        "bounds_digest",
    ):
        _digest(data[field], field)
    _digest(data["verifier_digest"], "verifier_digest", nullable=True)
    _digest_list(data["skill_digests"], "skill_digests")
    _integer(data["max_parallel"], "max_parallel", minimum=1, maximum=64)
    _boolean(data["verification_required"], "verification_required")
    if data["verification_required"] != (data["verifier_digest"] is not None):
        raise ContractError("verification state and verifier digest disagree")
    if data["topology"] == "single" and data["max_parallel"] != 1:
        raise ContractError("single topology must use one worker")
    configuration = {
        key: data[key]
        for key in (
            "model",
            "effort",
            "role",
            "topology",
            "max_parallel",
            "verification_required",
            "verifier_digest",
            "bounds_digest",
            "package_digest",
            "policy_digest",
            "agent_digest",
            "skill_digests",
            "prompt_digest",
            "client_digest",
        )
    }
    if data["configuration_digest"] != content_digest(configuration):
        raise ContractError("configuration_digest does not match the version vector")
    _integer(data["observed_at_ms"], "observed_at_ms")
    _enum(data["status"], OUTCOME_STATUSES, "status")
    if present_axes:
        _enum(data["execution_state"], EXECUTION_STATES, "execution_state")
        _enum(data["semantic_verdict"], SEMANTIC_VERDICTS, "semantic_verdict")
        _enum(data["admission_state"], ADMISSION_STATES, "admission_state")
        _integer(data["retry_attempt"], "retry_attempt", minimum=1, maximum=1_000_000)
        _identifier(
            data["retry_of_assignment_id"], "retry_of_assignment_id", nullable=True
        )
        if data["retry_attempt"] == 1 and data["retry_of_assignment_id"] is not None:
            raise ContractError("first outcome attempt cannot have a retry predecessor")
        if data["retry_attempt"] > 1 and data["retry_of_assignment_id"] is None:
            raise ContractError("retried outcome requires a predecessor assignment")
    for field in (
        "deterministic_pass",
        "security_pass",
        "terminal_failure",
        "verifier_disagreement",
        "accepted",
    ):
        _boolean(data[field], field)
    _integer(data["required_criteria_total"], "required_criteria_total", maximum=10_000)
    _integer(data["required_criteria_passed"], "required_criteria_passed", maximum=10_000)
    if data["required_criteria_passed"] > data["required_criteria_total"]:
        raise ContractError("required criteria passed exceeds total")
    _integer(data["quality_micros"], "quality_micros", maximum=1_000_000)
    _integer(data["repairs"], "repairs", maximum=1)
    reason_codes = data["repair_reason_codes"]
    if (
        not isinstance(reason_codes, list)
        or len(reason_codes) != data["repairs"]
        or any(
            not isinstance(code, str) or code not in RESULT_VALIDATION_REASON_CODES
            for code in reason_codes
        )
    ):
        raise ContractError("repair reason codes do not match the observed repairs")
    _integer(
        data["delegation_overhead_tokens"],
        "delegation_overhead_tokens",
        maximum=10**12,
    )
    _integer(data["no_progress_events"], "no_progress_events", maximum=10_000)
    _enum(data["coverage"], COVERAGE_CLASSES, "coverage")
    _digest(
        data["evaluation_candidate_digest"],
        "evaluation_candidate_digest",
        nullable=True,
    )
    _digest(data["evaluation_case_digest"], "evaluation_case_digest", nullable=True)
    if (data["evaluation_candidate_digest"] is None) != (
        data["evaluation_case_digest"] is None
    ):
        raise ContractError("evaluation candidate and case bindings must appear together")
    _digest_list(data["evidence_digests"], "evidence_digests", minimum=1)
    _usage(data["usage"])
    if "usage_sources" in data:
        sources = data["usage_sources"]
        if not isinstance(sources, dict) or set(sources) != USAGE_FIELDS:
            raise ContractError("usage_sources must cover the closed usage field set")
        for field, source in sources.items():
            if (
                not isinstance(source, dict)
                or set(source) != {"source", "coverage"}
            ):
                raise ContractError(f"usage_sources.{field} is invalid")
            _enum(
                source["source"],
                {"host_observed", "client_observed", "unavailable"},
                f"usage_sources.{field}.source",
            )
            _enum(
                source["coverage"], COVERAGE_CLASSES,
                f"usage_sources.{field}.coverage",
            )
            if (data["usage"][field] is None) != (source["source"] == "unavailable"):
                raise ContractError(f"usage_sources.{field} disagrees with usage")
    expected_terminal_failure = data["status"] in {
        "failed", "cancelled", "interrupted"
    }
    if data["accepted"] != (data["status"] == "accepted"):
        raise ContractError("outcome status and accepted state disagree")
    if present_axes:
        expected_admission = {
            "accepted": "accepted",
            "rejected": "rejected",
            "unverified": "unverified",
            "failed": "rejected",
            "cancelled": "rejected",
            "interrupted": "rejected",
        }[data["status"]]
        if data["admission_state"] != expected_admission:
            raise ContractError("outcome status and admission state disagree")
        if data["execution_state"] != "completed" and data["accepted"]:
            raise ContractError("non-completed execution cannot be accepted")
    if data["terminal_failure"] != expected_terminal_failure:
        raise ContractError("outcome status and terminal failure state disagree")
    positive_verdict = (
        not present_axes
        or data["semantic_verdict"] in {"pass", "ready", "ready-with-corrections"}
    )
    if data["accepted"] and (
        not data["deterministic_pass"]
        or not data["security_pass"]
        or (
            positive_verdict
            and data["required_criteria_passed"] != data["required_criteria_total"]
        )
    ):
        raise ContractError("accepted outcome has a failed hard gate")
    return data


FEEDBACK_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "run_id",
    "observed_at_ms",
    "label",
    "reason_code",
    "correction_digest",
}


def validate_feedback(data: Any) -> dict[str, Any]:
    data = _closed(data, FEEDBACK_FIELDS, "feedback")
    _base(data, "feedback")
    _identifier(data["run_id"], "run_id")
    _integer(data["observed_at_ms"], "observed_at_ms")
    _enum(data["label"], FEEDBACK_LABELS, "label")
    _enum(data["reason_code"], FEEDBACK_REASONS, "reason_code")
    _digest(data["correction_digest"], "correction_digest", nullable=True)
    if data["label"] == "corrected" and data["correction_digest"] is None:
        raise ContractError("corrected feedback requires correction_digest")
    return data


CASE_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "source_outcome_id",
    "supersedes_case_id",
    "task_slice",
    "corpus_state",
    "fixture_digest",
    "criteria_digest",
    "lineage_digest",
    "similarity_group_digest",
    "reviewed",
    "review_digest",
    "frozen",
}

TRIAL_FIELDS = {
    "schema_version", "record_type", "record_id", "case_id", "trial_id", "cohort",
    "task_slice", "configuration_digest", "coverage", "assigned",
    "status", "accepted", "deterministic_pass", "security_pass",
    "required_criteria_pass",
    "quality_micros", "terminal_failure", "repairs", "input_tokens",
    "output_tokens", "model_calls", "client_version", "client_digest",
    "outcome_digest", "evaluation_assignment_digest", "host_configuration_digest",
    "observed_at_ms",
}

QA_EVIDENCE_FIELDS = {
    "schema_version", "record_type", "record_id", "evaluation_assignment_digest",
    "trial_id", "case_digest", "host_outcome_digest", "evaluator_assignment_digest",
    "evaluator_result_digest",
    "verifier_host_outcome_digest", "verifier_assignment_digest",
    "verifier_result_digest",
    "deterministic_checks", "security_checks",
    "deterministic_check_digests", "security_check_digests",
    "deterministic_pass", "security_pass", "required_criteria_total",
    "required_criteria_passed", "verifier_disagreement", "accepted",
    "execution_state", "semantic_verdict", "observed_at_ms",
}

EVIDENCE_REVIEW_FIELDS = {
    "schema_version", "record_type", "record_id", "subject_digests",
    "accepted", "observed_at_ms",
}


def validate_evidence_review(data: Any) -> dict[str, Any]:
    data = _closed(data, EVIDENCE_REVIEW_FIELDS, "evidence review")
    _base(data, "evidence_review")
    _digest_list(data["subject_digests"], "subject_digests", minimum=1, maximum=20_000)
    if data["subject_digests"] != sorted(data["subject_digests"]):
        raise ContractError("evidence review subjects must be sorted")
    _boolean(data["accepted"], "accepted")
    _integer(data["observed_at_ms"], "observed_at_ms")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"evidence_review:{content_digest(material)}":
        raise ContractError("evidence review record_id does not match content")
    return data


def validate_qa_evidence(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("QA evidence must be an object")
    optional_axes = {"execution_state", "semantic_verdict"}
    if QA_EVIDENCE_FIELDS - optional_axes - data.keys():
        raise ContractError("QA evidence missing required fields")
    if data.keys() - QA_EVIDENCE_FIELDS:
        raise ContractError("QA evidence has unknown fields")
    present_axes = optional_axes & data.keys()
    if present_axes and present_axes != optional_axes:
        raise ContractError("QA evidence has a partial separated-axis contract")
    _base(data, "qa_evidence")
    for field in (
        "evaluation_assignment_digest", "case_digest", "host_outcome_digest",
        "evaluator_assignment_digest", "evaluator_result_digest",
        "verifier_host_outcome_digest",
        "verifier_assignment_digest",
        "verifier_result_digest",
    ):
        _digest(data[field], field)
    _identifier(data["trial_id"], "trial_id")
    for checks_field, digests_field in (
        ("deterministic_checks", "deterministic_check_digests"),
        ("security_checks", "security_check_digests"),
    ):
        checks = data[checks_field]
        if not isinstance(checks, list) or not checks or len(checks) > 256:
            raise ContractError(f"{checks_field} must be a non-empty bounded array")
        identifiers: set[str] = set()
        for item in checks:
            if not isinstance(item, dict) or set(item) != {
                "check_id", "passed", "evidence_digest"
            }:
                raise ContractError(f"{checks_field} contains an invalid check")
            _identifier(item["check_id"], f"{checks_field}.check_id")
            if item["check_id"] in identifiers:
                raise ContractError(f"{checks_field} contains a duplicate check")
            identifiers.add(item["check_id"])
            _boolean(item["passed"], f"{checks_field}.passed")
            _digest(item["evidence_digest"], f"{checks_field}.evidence_digest")
        if data[digests_field] != sorted(content_digest(item) for item in checks):
            raise ContractError(f"{checks_field} digests do not match checks")
    _digest_list(
        data["deterministic_check_digests"],
        "deterministic_check_digests",
        minimum=1,
    )
    _digest_list(
        data["security_check_digests"],
        "security_check_digests",
        minimum=1,
    )
    for field in (
        "deterministic_pass", "security_pass", "verifier_disagreement", "accepted"
    ):
        _boolean(data[field], field)
    _integer(data["required_criteria_total"], "required_criteria_total", maximum=10_000)
    _integer(data["required_criteria_passed"], "required_criteria_passed", maximum=10_000)
    if data["required_criteria_passed"] > data["required_criteria_total"]:
        raise ContractError("QA evidence required criteria passed exceeds total")
    _integer(data["observed_at_ms"], "observed_at_ms")
    execution_state = data.get("execution_state", "completed")
    semantic_verdict = data.get("semantic_verdict", "pass")
    _enum(execution_state, EXECUTION_STATES, "execution_state")
    _enum(semantic_verdict, SEMANTIC_VERDICTS, "semantic_verdict")
    positive = semantic_verdict in {"pass", "ready", "ready-with-corrections"}
    if data["accepted"] and (
        execution_state != "completed"
        or not data["deterministic_pass"]
        or not data["security_pass"]
        or data["verifier_disagreement"]
        or (
            positive
            and data["required_criteria_passed"] != data["required_criteria_total"]
        )
    ):
        raise ContractError("accepted QA evidence has a failed hard gate")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"qa_evidence:{content_digest(material)}":
        raise ContractError("QA evidence record_id does not match content")
    return data

EVALUATION_ASSIGNMENT_FIELDS = {
    "schema_version", "record_type", "record_id", "candidate_digest",
    "task_slice", "case_ids", "case_digests", "case_set_digest",
    "client_version", "client_digest", "trial_plan", "observed_at_ms",
}


def validate_evaluation_assignment(data: Any) -> dict[str, Any]:
    data = _closed(data, EVALUATION_ASSIGNMENT_FIELDS, "evaluation_assignment")
    _base(data, "evaluation_assignment")
    _digest(data["candidate_digest"], "candidate_digest")
    _identifier(data["task_slice"], "task_slice")
    case_ids = data["case_ids"]
    if not isinstance(case_ids, list) or not case_ids or len(case_ids) > 10_000:
        raise ContractError("evaluation assignment case_ids must be bounded")
    for case_id in case_ids:
        _identifier(case_id, "case_ids")
    if case_ids != sorted(set(case_ids)):
        raise ContractError("evaluation assignment case_ids must be sorted and unique")
    _digest_list(data["case_digests"], "case_digests", maximum=10_000)
    if len(data["case_digests"]) != len(case_ids):
        raise ContractError("evaluation assignment case digests do not match cases")
    if data["case_set_digest"] != content_digest(case_ids):
        raise ContractError("evaluation assignment case set digest does not match")
    _identifier(data["client_version"], "client_version")
    _digest(data["client_digest"], "client_digest")
    plan = data["trial_plan"]
    if not isinstance(plan, list) or len(plan) != len(case_ids) * 2:
        raise ContractError("evaluation assignment must plan two trials per case")
    normalized: list[tuple[str, str]] = []
    trial_ids: list[str] = []
    for item in plan:
        item = _closed(
            item,
            {"trial_id", "case_id", "cohort", "configuration_digest"},
            "evaluation assignment trial",
        )
        _identifier(item["trial_id"], "trial_id")
        _identifier(item["case_id"], "case_id")
        _enum(item["cohort"], {"champion", "candidate"}, "cohort")
        _digest(item["configuration_digest"], "configuration_digest")
        normalized.append((item["case_id"], item["cohort"]))
        trial_ids.append(item["trial_id"])
    expected = [(case_id, cohort) for case_id in case_ids for cohort in ("candidate", "champion")]
    if normalized != expected or len(trial_ids) != len(set(trial_ids)):
        raise ContractError("evaluation assignment trial population is invalid")
    _integer(data["observed_at_ms"], "observed_at_ms")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"evaluation_assignment:{content_digest(material)}":
        raise ContractError("evaluation assignment record_id does not match content")
    return data


def validate_evaluation_trial(data: Any) -> dict[str, Any]:
    data = _closed(data, TRIAL_FIELDS, "evaluation_trial")
    _base(data, "evaluation_trial")
    for field in ("case_id", "trial_id", "task_slice", "client_version"):
        _identifier(data[field], field)
    _enum(data["cohort"], {"champion", "candidate"}, "cohort")
    _digest(data["configuration_digest"], "configuration_digest")
    _enum(data["coverage"], COVERAGE_CLASSES, "coverage")
    _enum(data["status"], OUTCOME_STATUSES, "status")
    for field in (
        "assigned", "accepted", "deterministic_pass", "security_pass",
        "required_criteria_pass", "terminal_failure",
    ):
        _boolean(data[field], field)
    _integer(data["quality_micros"], "quality_micros", maximum=1_000_000)
    _integer(data["repairs"], "repairs", maximum=10_000)
    for field in ("input_tokens", "output_tokens", "model_calls"):
        _integer(data[field], field, maximum=10**15, nullable=True)
    _digest(data["client_digest"], "client_digest")
    _digest(data["outcome_digest"], "outcome_digest")
    _digest(data["evaluation_assignment_digest"], "evaluation_assignment_digest")
    _digest(data["host_configuration_digest"], "host_configuration_digest")
    _integer(data["observed_at_ms"], "observed_at_ms")
    if data["accepted"] != (data["status"] == "accepted"):
        raise ContractError("trial status and accepted state disagree")
    if data["terminal_failure"] != (
        data["status"] in {"failed", "cancelled", "interrupted"}
    ):
        raise ContractError("trial status and terminal failure state disagree")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"trial:{content_digest(material)}":
        raise ContractError("evaluation trial record_id does not match content")
    return data


def validate_evaluation_case(data: Any) -> dict[str, Any]:
    data = _closed(data, CASE_FIELDS, "evaluation_case")
    _base(data, "evaluation_case")
    for field in ("source_outcome_id", "task_slice"):
        _identifier(data[field], field)
    _identifier(data["supersedes_case_id"], "supersedes_case_id", nullable=True)
    _enum(data["corpus_state"], CORPUS_STATES, "corpus_state")
    for field in ("fixture_digest", "criteria_digest", "lineage_digest"):
        _digest(data[field], field)
    _digest(
        data["similarity_group_digest"], "similarity_group_digest", nullable=True
    )
    for field in ("reviewed", "frozen"):
        _boolean(data[field], field)
    _digest(data["review_digest"], "review_digest", nullable=True)
    if data["reviewed"] != (data["review_digest"] is not None):
        raise ContractError("reviewed state and review_digest disagree")
    if data["corpus_state"] != "development" and not data["reviewed"]:
        raise ContractError("unreviewed case cannot enter a controlled corpus")
    if data["frozen"] and data["corpus_state"] == "development":
        raise ContractError("development case cannot be frozen")
    if (data["corpus_state"] == "development") != (
        data["similarity_group_digest"] is None
    ):
        raise ContractError("similarity group must be assigned during controlled-corpus review")
    return data


CANDIDATE_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "candidate_digest",
    "scope",
    "task_slice",
    "target",
    "effect",
    "base_digest",
    "patch_digest",
    "evidence_digests",
    "status",
    "change_manifest",
    "champion_configuration",
    "candidate_configuration",
    "eligibility_receipt",
}

EXECUTION_CONFIGURATION_FIELDS = {
    "model", "effort", "role", "topology", "max_parallel",
    "verification_required", "verifier_digest", "bounds_digest",
    "package_digest", "policy_digest",
    "agent_digest", "skill_digests", "prompt_digest", "client_digest",
}


def _execution_configuration(value: Any, label: str) -> dict[str, Any]:
    value = _closed(value, EXECUTION_CONFIGURATION_FIELDS, label)
    _enum(value["model"], SUPPORTED_MODELS, f"{label}.model")
    _enum(value["effort"], SUPPORTED_EFFORTS, f"{label}.effort")
    _enum(value["role"], ROLES, f"{label}.role")
    _provider_consistent(value["model"], value["role"], label)
    _enum(value["topology"], {"single", "delegated", "parallel"}, f"{label}.topology")
    _integer(value["max_parallel"], f"{label}.max_parallel", minimum=1, maximum=64)
    _boolean(value["verification_required"], f"{label}.verification_required")
    _digest(value["verifier_digest"], f"{label}.verifier_digest", nullable=True)
    _digest(value["bounds_digest"], f"{label}.bounds_digest")
    if value["verification_required"] != (value["verifier_digest"] is not None):
        raise ContractError(f"{label} verification state and verifier digest disagree")
    if value["topology"] == "single" and value["max_parallel"] != 1:
        raise ContractError(f"{label} single topology must use one worker")
    for field in (
        "package_digest", "policy_digest", "agent_digest", "prompt_digest",
        "client_digest",
    ):
        _digest(value[field], f"{label}.{field}")
    _digest_list(value["skill_digests"], f"{label}.skill_digests")
    return value


def validate_execution_configuration(value: Any) -> dict[str, Any]:
    """Validate the complete configuration used for candidate replay."""

    return _execution_configuration(value, "execution_configuration")


def validate_candidate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("candidate must be an object")
    missing = sorted((CANDIDATE_FIELDS - {"eligibility_receipt"}) - data.keys())
    unknown = sorted(data.keys() - CANDIDATE_FIELDS)
    if missing:
        raise ContractError("candidate missing required fields")
    if unknown:
        raise ContractError("candidate has unknown fields")
    _base(data, "candidate")
    _enum(data["scope"], {"general", "project"}, "scope")
    _identifier(data["task_slice"], "task_slice")
    _enum(data["target"], CANDIDATE_TARGETS, "target")
    _enum(data["effect"], CANDIDATE_EFFECTS, "effect")
    for field in ("base_digest", "patch_digest"):
        _digest(data[field], field)
    _digest_list(data["evidence_digests"], "evidence_digests")
    receipt = data.get("eligibility_receipt")
    if receipt is not None:
        receipt_fields = {
            "task_slice", "evidence_set_digest", "independent_case_count",
            "usage_complete", "version_unconfounded", "task_mix_matched",
            "retry_deduplicated", "verifier_evidence_complete",
        }
        receipt = _closed(receipt, receipt_fields, "candidate eligibility receipt")
        _identifier(receipt["task_slice"], "eligibility_receipt.task_slice")
        _digest(receipt["evidence_set_digest"], "eligibility_receipt.evidence_set_digest")
        _integer(
            receipt["independent_case_count"],
            "eligibility_receipt.independent_case_count",
            minimum=20,
            maximum=1_000_000,
        )
        for field in receipt_fields - {
            "task_slice", "evidence_set_digest", "independent_case_count"
        }:
            _boolean(receipt[field], f"eligibility_receipt.{field}")
            if receipt[field] is not True:
                raise ContractError("candidate eligibility receipt contains a failed gate")
        if (
            receipt["task_slice"] != data["task_slice"]
            or receipt["evidence_set_digest"]
            != content_digest(sorted(data["evidence_digests"]))
        ):
            raise ContractError("candidate eligibility receipt does not bind its evidence")
    _enum(data["status"], CANDIDATE_STATUSES, "status")
    manifest = data["change_manifest"]
    if not isinstance(manifest, list) or not manifest or len(manifest) > 16:
        raise ContractError("change_manifest must be a bounded non-empty array")
    paths: list[str] = []
    for change in manifest:
        change = _closed(
            change,
            {
                "path_code",
                "before_digest",
                "after_digest",
                "before_value",
                "after_value",
            },
            "candidate change",
        )
        _enum(
            change["path_code"],
            TARGET_CHANGE_PATHS[data["target"]],
            "change_manifest.path_code",
        )
        _digest(change["before_digest"], "change_manifest.before_digest")
        _digest(change["after_digest"], "change_manifest.after_digest")
        _candidate_change_value(change["path_code"], change["before_value"], "before_value")
        _candidate_change_value(change["path_code"], change["after_value"], "after_value")
        if change["before_digest"] != content_digest(change["before_value"]):
            raise ContractError("candidate before_digest does not match exact value")
        if change["after_digest"] != content_digest(change["after_value"]):
            raise ContractError("candidate after_digest does not match exact value")
        if change["before_digest"] == change["after_digest"]:
            raise ContractError("candidate change must alter its target")
        if change["path_code"] == "verification.additional_checks" and not set(
            change["before_value"]
        ).issubset(change["after_value"]):
            raise ContractError("verification candidate must retain every existing check")
        if change["path_code"] == "bounds.limits" and any(
            key not in change["after_value"]
            or change["after_value"][key] > value
            for key, value in change["before_value"].items()
        ):
            raise ContractError("bounds candidate may only retain or reduce every limit")
        paths.append(change["path_code"])
    if len(paths) != len(set(paths)):
        raise ContractError("change_manifest path codes must be unique")
    if data["target"] == "verification" and data["effect"] != "strengthen":
        raise ContractError("verification candidates may only strengthen checks")
    if data["target"] == "bounds" and data["effect"] != "narrow":
        raise ContractError("bounds candidates may only narrow limits")
    if data["target"] == "topology" and (
        data["effect"] != "narrow"
        or any(
            change["path_code"] != "orchestration.topology"
            or change["after_value"] != "single"
            or change["before_value"] == "single"
            for change in manifest
        )
    ):
        raise ContractError("topology candidates may only narrow execution to one worker")
    if data["patch_digest"] != content_digest(manifest):
        raise ContractError("patch_digest does not match the typed change manifest")
    champion_configuration = _execution_configuration(
        data["champion_configuration"], "champion_configuration"
    )
    candidate_configuration = _execution_configuration(
        data["candidate_configuration"], "candidate_configuration"
    )
    expected_configuration = dict(champion_configuration)
    for change in manifest:
        if change["path_code"] == "routing.model_effort":
            if {
                "model": champion_configuration["model"],
                "effort": champion_configuration["effort"],
            } != change["before_value"]:
                raise ContractError("candidate before-value differs from champion configuration")
            expected_configuration.update(change["after_value"])
        elif change["path_code"] == "orchestration.topology":
            if champion_configuration["topology"] != change["before_value"]:
                raise ContractError("candidate topology differs from champion configuration")
            expected_configuration["topology"] = change["after_value"]
            if change["after_value"] == "single":
                expected_configuration["max_parallel"] = 1
        elif change["path_code"] == "skills.selection":
            if champion_configuration["skill_digests"] != change["before_value"]:
                raise ContractError("candidate skills differ from champion configuration")
            expected_configuration["skill_digests"] = change["after_value"]
        elif change["path_code"] == "prompts.template_digest":
            if champion_configuration["prompt_digest"] != change["before_value"]:
                raise ContractError("candidate prompt differs from champion configuration")
            expected_configuration["prompt_digest"] = change["after_value"]
    if candidate_configuration != expected_configuration:
        raise ContractError("candidate configuration is not the exact manifest result")
    if data["base_digest"] != candidate_base_digest(champion_configuration):
        raise ContractError("base_digest does not match the exact champion configuration")
    expected = candidate_digest(data)
    if data["candidate_digest"] != expected:
        raise ContractError("candidate_digest does not match canonical candidate")
    return data


def candidate_base_digest(champion_configuration: dict[str, Any]) -> str:
    """Digest the complete champion execution configuration."""

    return content_digest(
        _execution_configuration(champion_configuration, "champion_configuration")
    )


def _candidate_change_value(path_code: str, value: Any, field: str) -> None:
    if path_code == "routing.model_effort":
        value = _closed(value, {"model", "effort"}, field)
        _enum(value["model"], SUPPORTED_MODELS, f"{field}.model")
        _enum(value["effort"], SUPPORTED_EFFORTS, f"{field}.effort")
    elif path_code == "orchestration.topology":
        _enum(value, {"single", "delegated", "parallel"}, field)
    elif path_code in {"skills.selection", "context.selection", "verification.additional_checks"}:
        if not isinstance(value, list) or len(value) > 64 or len(value) != len(set(value)):
            raise ContractError(f"{field} must be a bounded unique array")
        for item in value:
            if path_code == "verification.additional_checks":
                _identifier(item, field)
            else:
                _digest(item, field)
    elif path_code == "prompts.template_digest":
        _digest(value, field)
    elif path_code == "bounds.limits":
        if not isinstance(value, dict) or not value or not set(value).issubset(BOUND_KEYS):
            raise ContractError(f"{field} contains unsupported bounds")
        for key, item in value.items():
            _integer(item, f"{field}.{key}", maximum=1_000_000)
    elif path_code == "routing.selection":
        _identifier(value, field)
    else:  # pragma: no cover - path allowlist is checked first.
        raise ContractError("candidate path is unsupported")


def candidate_digest(data: dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in data.items()
        if key not in {"candidate_digest", "record_id", "status"}
    }
    return content_digest(material)


COMPARISON_FIELDS = {
    "schema_version", "record_type", "record_id", "candidate_digest",
    "champion_digest", "task_slice", "case_ids", "case_set_digest",
    "case_digests", "trial_digests", "client_version",
    "client_digest", "grader_digest", "decision", "reasons", "metrics",
    "independent_review_digest", "evaluation_assignment_digest",
}
COMPARISON_METRIC_FIELDS = {
    "champion_tokens", "candidate_tokens", "champion_model_calls",
    "candidate_model_calls", "token_ratio_micros", "model_call_ratio_micros",
    "proven_improvements",
}


def validate_comparison(data: Any) -> dict[str, Any]:
    data = _closed(data, COMPARISON_FIELDS, "comparison")
    _base(data, "comparison")
    for field in (
        "candidate_digest", "champion_digest", "case_set_digest", "client_digest",
        "independent_review_digest", "evaluation_assignment_digest",
    ):
        _digest(data[field], field)
    _digest(data["grader_digest"], "grader_digest", nullable=True)
    _identifier(data["task_slice"], "task_slice")
    if not isinstance(data["case_ids"], list) or len(data["case_ids"]) > 10_000:
        raise ContractError("comparison case_ids must be bounded")
    for case_id in data["case_ids"]:
        _identifier(case_id, "case_ids")
    if data["case_ids"] != sorted(set(data["case_ids"])):
        raise ContractError("comparison case_ids must be sorted and unique")
    if data["case_set_digest"] != content_digest(data["case_ids"]):
        raise ContractError("comparison case set digest does not match")
    if not isinstance(data["case_digests"], list) or len(data["case_digests"]) > 10_000:
        raise ContractError("comparison case_digests must be bounded")
    for digest in data["case_digests"]:
        _digest(digest, "case_digests")
    if len(data["case_digests"]) != len(data["case_ids"]):
        raise ContractError("comparison case digests do not match case population")
    _digest_list(data["trial_digests"], "trial_digests", maximum=20_000)
    if data["trial_digests"] != sorted(data["trial_digests"]):
        raise ContractError("comparison trial digests must be sorted")
    _identifier(data["client_version"], "client_version")
    _enum(data["decision"], COMPARISON_DECISIONS, "decision")
    if not isinstance(data["reasons"], list) or data["reasons"] != sorted(set(data["reasons"])):
        raise ContractError("comparison reasons must be sorted and unique")
    for reason in data["reasons"]:
        _enum(reason, COMPARISON_REASONS, "reason")
    metrics = _closed(data["metrics"], COMPARISON_METRIC_FIELDS, "comparison metrics")
    for field in COMPARISON_METRIC_FIELDS - {"proven_improvements"}:
        _integer(metrics[field], f"metrics.{field}", maximum=10**15, nullable=True)
    improvements = metrics["proven_improvements"]
    if not isinstance(improvements, list) or improvements != sorted(set(improvements)):
        raise ContractError("proven improvements must be sorted and unique")
    for item in improvements:
        _enum(item, {"tokens", "model_calls"}, "proven improvement")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"comparison:{content_digest(material)}":
        raise ContractError("comparison record_id does not match content")
    return data


CANARY_REPORT_FIELDS = {
    "schema_version", "record_type", "record_id", "candidate_digest",
    "champion_digest", "case_digests", "case_set_digest", "deterministic_pass",
    "security_pass", "required_criteria_pass", "quality_regressions", "passed",
    "comparison_digest", "case_outcomes", "observed_at_ms", "independent_review_digest",
}

GRADER_CALIBRATION_FIELDS = {
    "schema_version", "record_type", "record_id", "configuration", "labels",
    "labels_digest", "label_count", "agreement_micros", "swap_stability_micros",
    "disagreements", "ready", "observed_at_ms",
}
GRADER_CONFIGURATION_FIELDS = {
    "model", "effort", "prompt_digest", "rubric_digest", "masking",
    "drift_digest", "owner_override_digest",
}


def validate_grader_calibration(data: Any) -> dict[str, Any]:
    data = _closed(data, GRADER_CALIBRATION_FIELDS, "grader_calibration")
    _base(data, "grader_calibration")
    configuration = _closed(
        data["configuration"], GRADER_CONFIGURATION_FIELDS, "grader configuration"
    )
    _enum(configuration["model"], SUPPORTED_MODELS, "grader model")
    _enum(configuration["effort"], SUPPORTED_EFFORTS, "grader effort")
    _boolean(configuration["masking"], "grader masking")
    if configuration["masking"] is not True:
        raise ContractError("grader calibration requires masked candidate ordering")
    for field in (
        "prompt_digest", "rubric_digest", "drift_digest", "owner_override_digest"
    ):
        _digest(configuration[field], f"grader {field}")
    labels = data["labels"]
    if not isinstance(labels, list) or len(labels) > 1000:
        raise ContractError("grader labels must be a bounded array")
    for label in labels:
        label = _closed(label, {"owner", "grader", "swapped"}, "grader label")
        for field in ("owner", "grader", "swapped"):
            _enum(label[field], {"a", "b", "tie"}, f"grader label {field}")
    if data["labels_digest"] != content_digest(labels):
        raise ContractError("grader labels digest does not match labels")
    if data["label_count"] != len(labels):
        raise ContractError("grader label count does not match labels")
    _integer(data["label_count"], "label_count", maximum=1000)
    for field in ("agreement_micros", "swap_stability_micros"):
        _integer(data[field], field, maximum=1_000_000)
    _integer(data["disagreements"], "disagreements", maximum=1000)
    _boolean(data["ready"], "ready")
    _integer(data["observed_at_ms"], "observed_at_ms")
    count = len(labels)
    agreement = round(
        (sum(item["owner"] == item["grader"] for item in labels) / count) * 1_000_000
    ) if count else 0
    swapped_back = {"a": "b", "b": "a", "tie": "tie"}
    stability = round(
        (sum(item["grader"] == swapped_back[item["swapped"]] for item in labels) / count)
        * 1_000_000
    ) if count else 0
    disagreements = sum(item["owner"] != item["grader"] for item in labels)
    expected_ready = count >= 30 and agreement >= 900_000 and stability >= 950_000
    if (
        data["agreement_micros"] != agreement
        or data["swap_stability_micros"] != stability
        or data["disagreements"] != disagreements
        or data["ready"] != expected_ready
    ):
        raise ContractError("grader calibration metrics do not match labels")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"calibration:{content_digest(material)}":
        raise ContractError("grader calibration record_id does not match content")
    return data


def validate_canary_report(data: Any) -> dict[str, Any]:
    data = _closed(data, CANARY_REPORT_FIELDS, "canary_report")
    _base(data, "canary_report")
    for field in (
        "candidate_digest", "champion_digest", "case_set_digest",
        "independent_review_digest",
    ):
        _digest(data[field], field)
    _digest_list(data["case_digests"], "case_digests")
    if data["case_digests"] != sorted(data["case_digests"]):
        raise ContractError("canary case digests must be sorted")
    if data["case_set_digest"] != content_digest(data["case_digests"]):
        raise ContractError("canary case set digest does not match")
    _digest(data["comparison_digest"], "comparison_digest")
    _integer(data["observed_at_ms"], "observed_at_ms")
    case_outcomes = data["case_outcomes"]
    if not isinstance(case_outcomes, list) or not case_outcomes or len(case_outcomes) > 64:
        raise ContractError("canary case outcomes must be a bounded non-empty array")
    bound_cases = []
    for item in case_outcomes:
        item = _closed(
            item,
            {
                "case_digest", "champion_outcome_digest",
                "candidate_outcome_digest",
            },
            "canary case outcome",
        )
        _digest(item["case_digest"], "case_digest")
        _digest(item["champion_outcome_digest"], "champion_outcome_digest")
        _digest(item["candidate_outcome_digest"], "candidate_outcome_digest")
        bound_cases.append(item["case_digest"])
    if sorted(bound_cases) != data["case_digests"] or len(bound_cases) != len(set(bound_cases)):
        raise ContractError("canary outcomes must cover every canary case exactly once")
    for field in ("deterministic_pass", "security_pass", "required_criteria_pass", "passed"):
        _boolean(data[field], field)
    _integer(data["quality_regressions"], "quality_regressions", maximum=10_000)
    expected_pass = (
        data["deterministic_pass"] and data["security_pass"]
        and data["required_criteria_pass"] and data["quality_regressions"] == 0
    )
    if data["passed"] != expected_pass:
        raise ContractError("canary pass does not match hard gates")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"canary:{content_digest(material)}":
        raise ContractError("canary report record_id does not match content")
    return data


GRANT_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "grant_kind",
    "project_id",
    "candidate_digest",
    "champion_digest",
    "review_digest",
    "bounds_digest",
    "issued_at_ms",
    "expires_at_ms",
    "nonce_digest",
    "signature_b64",
}


def validate_owner_grant(data: Any) -> dict[str, Any]:
    data = _closed(data, GRANT_FIELDS, "owner_grant")
    _base(data, "owner_grant")
    _enum(data["grant_kind"], GRANT_KINDS, "grant_kind")
    _identifier(data["project_id"], "project_id")
    for field in (
        "candidate_digest",
        "champion_digest",
        "review_digest",
        "bounds_digest",
        "nonce_digest",
    ):
        _digest(data[field], field)
    signature = data["signature_b64"]
    if not isinstance(signature, str) or not signature or len(signature) > 24_000:
        raise ContractError("owner grant signature is invalid")
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ContractError("owner grant signature is invalid") from exc
    if not decoded or len(decoded) > 16_384:
        raise ContractError("owner grant signature is invalid")
    _integer(data["issued_at_ms"], "issued_at_ms")
    _integer(data["expires_at_ms"], "expires_at_ms")
    if data["expires_at_ms"] <= data["issued_at_ms"]:
        raise ContractError("owner grant must expire after issue")
    material = {key: value for key, value in data.items() if key != "record_id"}
    if data["record_id"] != f"grant:{content_digest(material)}":
        raise ContractError("owner grant record_id does not match signature")
    return data


PROMOTION_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "candidate_digest",
    "champion_digest",
    "action",
    "observed_at_ms",
    "grant_id",
    "evidence_digest",
    "canary_case_digests",
}


def validate_promotion(data: Any) -> dict[str, Any]:
    data = _closed(data, PROMOTION_FIELDS, "promotion")
    _base(data, "promotion")
    for field in ("candidate_digest", "champion_digest", "evidence_digest"):
        _digest(data[field], field)
    _enum(data["action"], PROMOTION_ACTIONS, "action")
    _integer(data["observed_at_ms"], "observed_at_ms")
    _identifier(data["grant_id"], "grant_id", nullable=True)
    _digest_list(data["canary_case_digests"], "canary_case_digests")
    if data["action"] in {"canary_started", "promoted"} and data["grant_id"] is None:
        raise ContractError("authorized promotion action requires grant_id")
    return data


EXPORT_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "export_digest",
    "scope",
    "candidate_digest",
    "artifact_id",
    "schema_digest",
}


def validate_learning_export(data: Any) -> dict[str, Any]:
    data = _closed(data, EXPORT_FIELDS, "learning_export")
    _base(data, "learning_export")
    _enum(data["scope"], {"general", "project"}, "scope")
    for field in ("export_digest", "candidate_digest", "schema_digest"):
        _digest(data[field], field)
    _identifier(data["artifact_id"], "artifact_id")
    return data


PROVIDER_NAMES = {"codex", "claude"}
PROVIDER_IDENTITY_FIELDS = {
    "schema_version", "record_type", "record_id", "provider", "account_fingerprint",
    "logged_in", "auth_method", "subscription_type", "plan_type", "client_path",
    "client_sha256", "observed_at_ms",
}
PROVIDER_HEADROOM_FIELDS = {
    "schema_version", "record_type", "record_id", "provider", "account_fingerprint",
    "windows", "status", "run_id", "observed_at_ms",
}
HEADROOM_WINDOW_FIELDS = {"window_minutes", "scope", "used_percent", "resets_at"}
PROVIDER_BILLING_FIELDS = {
    "schema_version", "record_type", "record_id", "provider", "account_fingerprint",
    "paid_usage_usable", "has_credits", "unlimited", "ordinary_usage_allowed",
    "is_using_overage", "overage_status", "overage_disabled_reason", "run_id",
    "observed_at_ms",
}
BILLING_ATTESTATION_FIELDS = {
    "schema_version", "record_type", "record_id", "provider", "account_fingerprint",
    "no_paid_overage", "no_usage_credits", "issued_at", "observed_at_ms",
}
BILLING_GRANT_FIELDS = {
    "version", "provider", "account_fingerprint", "no_paid_overage",
    "no_usage_credits", "issued_at",
}
ADMISSION_DECISION_FIELDS = {
    "schema_version", "record_type", "record_id", "run_id", "provider", "decision",
    "reason_code", "admission_basis", "account_fingerprint", "evidence_digests",
    "observed_at_ms",
}
ADMISSION_REASON_CODES = {
    "admitted",
    "provider_api_key_present",
    "provider_identity_missing",
    "billing_attestation_missing",
    "claude_subscription_missing",
    "paid_usage_usable",
    "billing_unverified",
    "provider_exhausted",
    "claude_backend_not_subscription",
    "claude_model_override_present",
}
CLAUDE_HEADROOM_STATUSES = {"allowed", "allowed_warning", "rejected"}
CLAUDE_OVERAGE_STATUSES = {"allowed", "allowed_warning", "rejected"}
CLAUDE_AUTH_METHODS = frozenset({"claude.ai"})
CLAUDE_SUBSCRIPTION_TYPES = frozenset({"max", "pro", "team", "enterprise"})
# The app-server PlanType enum, without its catch-all unknown value.
CODEX_PLAN_TYPES = frozenset({
    "free", "go", "plus", "pro", "prolite", "promax", "team",
    "self_serve_business_prolite", "self_serve_business_usage_based", "business", "ent26",
    "enterprise_cbp_automation", "enterprise_cbp_usage_based", "enterprise",
    "edu", "edu_plus", "edu_pro", "sci", "sci_plus",
})
# Reasons that mean paid overage is switched off. Credit exhaustion, fetch
# errors, unknown and "no limits configured" do not prove that, so they stay out.
CLAUDE_OVERAGE_DISABLED_REASONS = frozenset({
    "overage_not_provisioned", "org_level_disabled", "org_level_disabled_until",
    "org_service_level_disabled", "seat_tier_level_disabled", "member_level_disabled",
    "seat_tier_zero_credit_limit", "group_zero_credit_limit", "member_zero_credit_limit",
})
CLAUDE_WINDOW_SCOPES = {
    "five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
    "seven_day_overage_included", "overage",
}


def _observation_base(data: dict[str, Any], record_type: str) -> None:
    _base(data, record_type)
    _enum(data["provider"], PROVIDER_NAMES, "provider")
    _digest(data["account_fingerprint"], "account_fingerprint")
    _integer(data["observed_at_ms"], "observed_at_ms")


def validate_provider_identity(data: Any) -> dict[str, Any]:
    data = _closed(data, PROVIDER_IDENTITY_FIELDS, "provider_identity")
    _observation_base(data, "provider_identity")
    _boolean(data["logged_in"], "logged_in")
    _enum(data["auth_method"], set(CLAUDE_AUTH_METHODS), "auth_method", nullable=True)
    _enum(
        data["subscription_type"], set(CLAUDE_SUBSCRIPTION_TYPES), "subscription_type",
        nullable=True,
    )
    _enum(data["plan_type"], set(CODEX_PLAN_TYPES), "plan_type", nullable=True)
    # The local client path is kept for audit only and is never printed.
    if (
        not isinstance(data["client_path"], str)
        or not data["client_path"].startswith("/")
        or not 1 < len(data["client_path"]) <= 1024
        or "\x00" in data["client_path"]
    ):
        raise ContractError("client_path must be an absolute path")
    _digest(data["client_sha256"], "client_sha256")
    return data


def validate_provider_headroom(data: Any) -> dict[str, Any]:
    data = _closed(data, PROVIDER_HEADROOM_FIELDS, "provider_headroom")
    _observation_base(data, "provider_headroom")
    windows = data["windows"]
    if not isinstance(windows, list) or len(windows) > 16:
        raise ContractError("provider_headroom windows must be a bounded array")
    for window in windows:
        window = _closed(window, HEADROOM_WINDOW_FIELDS, "headroom window")
        _integer(window["window_minutes"], "window_minutes", minimum=1, nullable=True)
        _enum(window["scope"], CLAUDE_WINDOW_SCOPES, "scope", nullable=True)
        _number(
            window["used_percent"], "used_percent", minimum=0, maximum=100,
            nullable=True,
        )
        _integer(window["resets_at"], "resets_at", nullable=True)
    _enum(data["status"], CLAUDE_HEADROOM_STATUSES, "status", nullable=True)
    _identifier(data["run_id"], "run_id", nullable=True)
    return data


def validate_provider_billing(data: Any) -> dict[str, Any]:
    data = _closed(data, PROVIDER_BILLING_FIELDS, "provider_billing")
    _observation_base(data, "provider_billing")
    for field in (
        "paid_usage_usable", "has_credits", "unlimited", "ordinary_usage_allowed",
        "is_using_overage",
    ):
        if data[field] is not None:
            _boolean(data[field], field)
    _enum(data["overage_status"], CLAUDE_OVERAGE_STATUSES, "overage_status", nullable=True)
    _enum(
        data["overage_disabled_reason"], set(CLAUDE_OVERAGE_DISABLED_REASONS),
        "overage_disabled_reason", nullable=True,
    )
    _identifier(data["run_id"], "run_id", nullable=True)
    return data


def validate_billing_grant(data: Any) -> dict[str, Any]:
    """Validate the closed owner billing grant before it is signed or stored."""

    data = _closed(data, BILLING_GRANT_FIELDS, "billing grant")
    if data["version"] != 1:
        raise ContractError("billing grant version is unsupported")
    _enum(data["provider"], PROVIDER_NAMES, "provider")
    _digest(data["account_fingerprint"], "account_fingerprint")
    if data["no_paid_overage"] is not True or data["no_usage_credits"] is not True:
        raise ContractError("billing grant must attest no paid overage and no usage credits")
    _integer(data["issued_at"], "issued_at")
    return data


def validate_billing_attestation(data: Any) -> dict[str, Any]:
    data = _closed(data, BILLING_ATTESTATION_FIELDS, "billing_attestation")
    _observation_base(data, "billing_attestation")
    validate_billing_grant(
        {"version": 1, **{field: data[field] for field in BILLING_GRANT_FIELDS - {"version"}}}
    )
    if data["observed_at_ms"] != data["issued_at"]:
        raise ContractError("billing attestation time must equal its issue time")
    return data


def validate_admission_decision(data: Any) -> dict[str, Any]:
    data = _closed(data, ADMISSION_DECISION_FIELDS, "admission_decision")
    _base(data, "admission_decision")
    _identifier(data["run_id"], "run_id", nullable=True)
    _enum(data["provider"], PROVIDER_NAMES, "provider")
    _enum(data["decision"], {"pass", "deny"}, "decision")
    _enum(data["reason_code"], ADMISSION_REASON_CODES, "reason_code")
    if (data["decision"] == "pass") != (data["reason_code"] == "admitted"):
        raise ContractError("admission decision and reason disagree")
    _enum(
        data["admission_basis"], {"evidence", "bootstrap"}, "admission_basis",
        nullable=True,
    )
    if (data["decision"] == "pass") != (data["admission_basis"] is not None):
        raise ContractError("an admission basis belongs only to a pass")
    if data["admission_basis"] == "bootstrap" and data["provider"] != "claude":
        raise ContractError("only Claude admissions may bootstrap")
    if data["decision"] == "pass" and (
        data["run_id"] is None
        or data["account_fingerprint"] is None
        or not data["evidence_digests"]
    ):
        raise ContractError(
            "an admitted decision needs a run, an account fingerprint, and evidence"
        )
    _digest(data["account_fingerprint"], "account_fingerprint", nullable=True)
    _digest_list(data["evidence_digests"], "evidence_digests")
    _integer(data["observed_at_ms"], "observed_at_ms")
    return data


DISPATCH_INTENT_FIELDS = {
    "schema_version", "record_type", "record_id", "run_id", "bridge_run_id",
    "provider", "model", "effort", "deadline_at_ms", "observed_at_ms",
}


def validate_dispatch_intent(data: Any) -> dict[str, Any]:
    """One bridged launch, written before the child starts; never resent."""

    data = _closed(data, DISPATCH_INTENT_FIELDS, "dispatch_intent")
    _base(data, "dispatch_intent")
    _identifier(data["run_id"], "run_id")
    if not isinstance(data["bridge_run_id"], str) or re.fullmatch(
        r"brg_[0-9a-f]{32}", data["bridge_run_id"]
    ) is None:
        raise ContractError("bridge_run_id is invalid")
    _enum(data["provider"], PROVIDER_NAMES, "provider")
    _enum(data["model"], SUPPORTED_MODELS, "model")
    _enum(data["effort"], SUPPORTED_EFFORTS, "effort")
    if provider_for_model(data["model"]) != data["provider"]:
        raise ContractError("dispatch_intent model and provider disagree")
    _integer(data["observed_at_ms"], "observed_at_ms")
    _integer(data["deadline_at_ms"], "deadline_at_ms", minimum=data["observed_at_ms"] + 1)
    return data


HOST_RUN_PROFILE_FIELDS = {
    "schema_version", "record_type", "record_id", "run_id", "host_client",
    "backend_check", "model_pin", "subagent_type", "model_alias", "effort",
    "observed_at_ms",
}


def validate_host_run_profile(data: Any) -> dict[str, Any]:
    """The host-side checks an opened run passed, and how its model was pinned."""

    data = _closed(data, HOST_RUN_PROFILE_FIELDS, "host_run_profile")
    _base(data, "host_run_profile")
    _identifier(data["run_id"], "run_id")
    _enum(data["host_client"], {"codex-host-v1", "claude-host-v1"}, "host_client")
    _enum(data["backend_check"], {"passed", "not_applicable"}, "backend_check")
    _enum(data["model_pin"], {"alias"}, "model_pin", nullable=True)
    _identifier(data["subagent_type"], "subagent_type", nullable=True)
    _enum(data["model_alias"], {"opus", "sonnet", "haiku"}, "model_alias", nullable=True)
    _enum(data["effort"], SUPPORTED_EFFORTS, "effort", nullable=True)
    if (data["model_pin"] is None) != (data["model_alias"] is None):
        raise ContractError("host run profile pin and alias disagree")
    _integer(data["observed_at_ms"], "observed_at_ms")
    return data


PROBE_ADMISSION_FIELDS = ADMISSION_DECISION_FIELDS - {"run_id"}


def validate_probe_admission(data: Any) -> dict[str, Any]:
    """The admission decision for one Claude billing probe, which spends a little."""

    data = _closed(data, PROBE_ADMISSION_FIELDS, "probe_admission")
    if data.get("provider") != "claude":
        raise ContractError("only Claude billing is probed")
    validate_admission_decision({
        **data,
        "record_type": "admission_decision",
        # A probe has no run; stand in a placeholder so the shared rules apply.
        "run_id": "probe" if data["decision"] == "pass" else None,
    })
    _base(data, "probe_admission")
    return data


VALIDATORS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "event": validate_event,
    "outcome": validate_outcome,
    "feedback": validate_feedback,
    "evaluation_case": validate_evaluation_case,
    "evaluation_assignment": validate_evaluation_assignment,
    "qa_evidence": validate_qa_evidence,
    "evidence_review": validate_evidence_review,
    "evaluation_trial": validate_evaluation_trial,
    "candidate": validate_candidate,
    "comparison": validate_comparison,
    "canary_report": validate_canary_report,
    "grader_calibration": validate_grader_calibration,
    "owner_grant": validate_owner_grant,
    "promotion": validate_promotion,
    "learning_export": validate_learning_export,
    "provider_identity": validate_provider_identity,
    "provider_headroom": validate_provider_headroom,
    "provider_billing": validate_provider_billing,
    "billing_attestation": validate_billing_attestation,
    "admission_decision": validate_admission_decision,
    "dispatch_intent": validate_dispatch_intent,
    "host_run_profile": validate_host_run_profile,
    "probe_admission": validate_probe_admission,
}


def validate_learning_record(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("learning record must be an object")
    record_type = data.get("record_type")
    validator = VALIDATORS.get(record_type)
    if validator is None:
        raise ContractError("unsupported learning record type")
    checked = validator(data)
    if len(canonical_json(checked)) > MAX_RECORD_BYTES:
        raise ContractError("learning record is oversized")
    return checked
