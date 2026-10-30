"""Strict validation for task, result, and evaluation contracts."""

from __future__ import annotations

import math
import re
from typing import Any


TASK_FAMILIES = {
    "inventory",
    "research",
    "planning",
    "coding",
    "security",
    "verification",
}
COMPLEXITIES = {"low", "medium", "high", "extreme"}
RISKS = {"low", "medium", "high", "critical"}
OPERATIONS = {
    "mechanical",
    "exploration",
    "implementation",
    "diagnosis",
    "synthesis",
    "review",
    "end_to_end",
}
AMBIGUITIES = {"known", "partial", "high", "unknown"}
CONTEXT_BREADTHS = {"narrow", "repository", "cross_system", "unknown"}
ACCEPTANCE_STRENGTHS = {
    "deterministic",
    "executable",
    "independent_judgment",
    "owner_decision",
}
RESULT_STATUSES = {"completed", "failed", "blocked", "interrupted", "cancelled"}
EXECUTION_STATES = {
    "completed",
    "failed",
    "timed_out",
    "interrupted",
    "cancelled",
    "invalid",
}
SEMANTIC_VERDICTS = {
    "pass",
    "ready",
    "ready-with-corrections",
    "not-ready",
    "blocked",
    "reject",
    "not-applicable",
}
ADMISSION_STATES = {"unverified", "accepted", "rejected", "pending"}
EVIDENCE_TYPES = {"command", "source", "artifact", "review", "observation"}
EVIDENCE_OUTCOMES = {"pass", "fail", "observed", "not_run"}
FAILURE_REASONS = {
    "result_contract",
    "transient",
    "corrected_input",
    "semantic",
    "reasoning",
    "authority",
}
FAILURE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
VERIFIED_FAILURE_REFERENCE = re.compile(r"^verification:[0-9a-f]{64}$")
RESULT_VALIDATION_REASON_CODES = {
    "assignment_mismatch",
    "duplicate_criterion",
    "failed_completed_evidence",
    "invalid_evidence",
    "invalid_shape",
    "invalid_status",
    "invalid_usage",
    "missing_evidence",
    "missing_required_evidence",
    "missing_required_fields",
    "unknown_criterion",
    "unknown_fields",
    "inconsistent_axes",
}
BOUND_FIELDS = {
    "max_attempts",
    "max_steers_per_attempt",
    "max_no_progress_events",
    "wait_seconds",
    "timeout_seconds",
    "max_tool_calls",
}


class ContractError(ValueError):
    """Raised when a contract is missing or contains unsupported data."""


class ResultContractError(ContractError):
    """A result-contract rejection with a bounded, content-free reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in RESULT_VALIDATION_REASON_CODES:
            raise ValueError("unsupported result validation reason code")
        super().__init__(message)
        self.reason_code = reason_code


def _required(data: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - data.keys())
    if missing:
        raise ContractError(f"{label} missing required fields: {', '.join(missing)}")


def _nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")


def _validate_permissions(data: Any, label: str) -> dict[str, Any]:
    fields = {"tools", "filesystem", "network", "mcps", "external_effects"}
    if not isinstance(data, dict):
        raise ContractError(f"{label} must be an object")
    _required(data, fields, label)
    unknown = sorted(data.keys() - fields)
    if unknown:
        raise ContractError(f"{label} has unknown fields: {', '.join(unknown)}")
    for field in ("tools", "filesystem", "mcps"):
        value = data[field]
        if (
            not isinstance(value, list)
            or not all(isinstance(item, str) and item for item in value)
            or len(value) != len(set(value))
        ):
            raise ContractError(f"{label}.{field} must be an array of unique strings")
    for field in ("network", "external_effects"):
        if not isinstance(data[field], bool):
            raise ContractError(f"{label}.{field} must be boolean")
    if data["mcps"]:
        raise ContractError(
            f"{label}.mcps must be empty in version 1; pinned MCP identity is unavailable"
        )
    return data


def validate_parent_grant(data: Any) -> dict[str, Any]:
    """Validate the manager's declared maximum child authority."""

    return _validate_permissions(data, "parent_grant")


def validate_task(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("task must be an object")
    required = {
        "id",
        "objective",
        "family",
        "complexity",
        "risk",
        "operation",
        "ambiguity",
        "context_breadth",
        "acceptance_strength",
        "routing_evidence_refs",
        "meaningful",
        "deterministic_validation",
        "independent_parts",
        "parallel_safe",
        "failed_attempts",
        "failure_events",
    }
    allowed = required | {
        "owned_paths",
        "required_capabilities",
        "prohibited_actions",
        "verification_for_family",
        "verification_for_provider",
    }
    _required(data, required, "task")
    unknown = sorted(data.keys() - allowed)
    if unknown:
        raise ContractError(f"task has unknown fields: {', '.join(unknown)}")
    _nonempty_string(data["id"], "id")
    _nonempty_string(data["objective"], "objective")
    if not isinstance(data["family"], str) or data["family"] not in TASK_FAMILIES:
        raise ContractError(f"unsupported family: {data['family']}")
    verification_for_family = data.get("verification_for_family")
    if verification_for_family is not None:
        if data["family"] != "verification":
            raise ContractError(
                "verification_for_family is valid only for verification tasks"
            )
        if (
            not isinstance(verification_for_family, str)
            or verification_for_family not in TASK_FAMILIES - {"verification"}
        ):
            raise ContractError("verification_for_family is unsupported")
    verification_for_provider = data.get("verification_for_provider")
    if verification_for_provider is not None:
        if data["family"] != "verification":
            raise ContractError(
                "verification_for_provider is valid only for verification tasks"
            )
        if verification_for_provider not in {"codex", "claude"}:
            raise ContractError("verification_for_provider is unsupported")
    if not isinstance(data["complexity"], str) or data["complexity"] not in COMPLEXITIES:
        raise ContractError(f"unsupported complexity: {data['complexity']}")
    if not isinstance(data["risk"], str) or data["risk"] not in RISKS:
        raise ContractError(f"unsupported risk: {data['risk']}")
    for field, supported in (
        ("operation", OPERATIONS),
        ("ambiguity", AMBIGUITIES),
        ("context_breadth", CONTEXT_BREADTHS),
        ("acceptance_strength", ACCEPTANCE_STRENGTHS),
    ):
        if not isinstance(data[field], str) or data[field] not in supported:
            raise ContractError(f"unsupported {field}: {data[field]}")
    routing_evidence_refs = data["routing_evidence_refs"]
    if (
        not isinstance(routing_evidence_refs, list)
        or len(routing_evidence_refs) > 16
        or not all(
            isinstance(reference, str) and FAILURE_REFERENCE.fullmatch(reference)
            for reference in routing_evidence_refs
        )
        or len(routing_evidence_refs) != len(set(routing_evidence_refs))
    ):
        raise ContractError(
            "routing_evidence_refs must be at most 16 unique safe references"
        )
    for field in ("meaningful", "deterministic_validation", "parallel_safe"):
        if not isinstance(data[field], bool):
            raise ContractError(f"{field} must be boolean")
    if (
        data["acceptance_strength"] == "deterministic"
        and not data["deterministic_validation"]
    ):
        raise ContractError(
            "deterministic acceptance requires deterministic_validation"
        )
    if data["risk"] in {"high", "critical"} and not data["meaningful"]:
        raise ContractError("high and critical risk tasks must be meaningful")
    if not isinstance(data["independent_parts"], int) or data["independent_parts"] < 1:
        raise ContractError("independent_parts must be an integer of at least 1")
    if not isinstance(data["failed_attempts"], int) or not 0 <= data["failed_attempts"] <= 2:
        raise ContractError("failed_attempts must be an integer from 0 to 2")
    failure_events = data["failure_events"]
    if not isinstance(failure_events, list):
        raise ContractError("failure_events must be an array")
    minimum_events = data["failed_attempts"]
    maximum_events = data["failed_attempts"] * 2
    if not minimum_events <= len(failure_events) <= maximum_events:
        raise ContractError(
            "failure_events must contain one or two events per failed attempt"
        )
    seen_events: set[tuple[int, str, tuple[str, ...]]] = set()
    seen_reasoning_references: set[str] = set()
    event_attempts: list[int] = []
    for index, event in enumerate(failure_events):
        if not isinstance(event, dict) or set(event) != {
            "attempt",
            "reason",
            "evidence_refs",
        }:
            raise ContractError(
                f"failure_events[{index}] must contain exactly attempt, reason, and evidence_refs"
            )
        attempt = event["attempt"]
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or not 1 <= attempt <= data["failed_attempts"]
        ):
            raise ContractError(f"failure_events[{index}].attempt is invalid")
        event_attempts.append(attempt)
        reason = event["reason"]
        if not isinstance(reason, str) or reason not in FAILURE_REASONS:
            raise ContractError(f"failure_events[{index}].reason is unsupported")
        evidence_refs = event["evidence_refs"]
        if (
            not isinstance(evidence_refs, list)
            or not evidence_refs
            or len(evidence_refs) > 16
            or not all(
                isinstance(reference, str) and FAILURE_REFERENCE.fullmatch(reference)
                for reference in evidence_refs
            )
            or len(evidence_refs) != len(set(evidence_refs))
        ):
            raise ContractError(
                f"failure_events[{index}].evidence_refs must be one to 16 unique safe references"
            )
        event_key = (attempt, reason, tuple(sorted(evidence_refs)))
        if event_key in seen_events:
            raise ContractError("failure_events must not repeat the same event")
        seen_events.add(event_key)
        if reason in {"semantic", "reasoning"}:
            if not all(
                VERIFIED_FAILURE_REFERENCE.fullmatch(reference)
                for reference in evidence_refs
            ):
                raise ContractError(
                    "semantic and reasoning failures require verification record digests"
                )
            reused = seen_reasoning_references.intersection(evidence_refs)
            if reused:
                raise ContractError(
                    "semantic and reasoning failures need distinct evidence references"
                )
            seen_reasoning_references.update(evidence_refs)
    if set(event_attempts) != set(range(1, data["failed_attempts"] + 1)) or any(
        event_attempts.count(attempt) > 2 for attempt in set(event_attempts)
    ):
        raise ContractError(
            "failure_events must contain one or two events for each failed attempt"
        )
    for field in ("owned_paths", "required_capabilities", "prohibited_actions"):
        if field in data and (
            not isinstance(data[field], list)
            or not all(isinstance(item, str) for item in data[field])
        ):
            raise ContractError(f"{field} must be an array of strings")
    return data


def routing_task_slice(data: Any) -> str:
    """Return the explicit task-shape slice used for verified routing learning."""

    task = validate_task(data)
    return "-".join(
        task[field]
        for field in (
            "family",
            "complexity",
            "risk",
            "operation",
            "ambiguity",
            "context_breadth",
            "acceptance_strength",
        )
    )


def model_selection_slice(data: Any) -> str:
    """Return the risk-neutral task shape used for model/effort learning."""

    task = validate_task(data)
    return "-".join(
        (
            task["family"],
            task["complexity"],
            task["operation"],
            task["ambiguity"],
            task["context_breadth"],
            task["acceptance_strength"],
        )
    )


def validate_assignment_shape(data: Any) -> dict[str, Any]:
    """Validate the closed assignment contract without granting any authority."""

    if not isinstance(data, dict):
        raise ContractError("assignment must be an object")
    required = {
        "assignment_id",
        "task_id",
        "objective",
        "role",
        "model",
        "effort",
        "attempt",
        "meaningful",
        "owned_paths",
        "context",
        "permissions",
        "expected_output",
        "required_evidence",
        "verification_required",
        "bounds",
        "stopping_conditions",
        "prohibited_actions",
    }
    _required(data, required, "assignment")
    unknown = sorted(data.keys() - required - {"retry_of_assignment_id"})
    if unknown:
        raise ContractError(f"assignment has unknown fields: {', '.join(unknown)}")
    for field in (
        "assignment_id",
        "task_id",
        "objective",
        "role",
        "model",
        "effort",
        "expected_output",
    ):
        _nonempty_string(data[field], field)
    if not isinstance(data["attempt"], int) or data["attempt"] < 1:
        raise ContractError("attempt must be a positive integer")
    retry_of = data.get("retry_of_assignment_id")
    if data["attempt"] == 1 and retry_of is not None:
        raise ContractError("first assignment attempt cannot have a retry predecessor")
    if data["attempt"] > 1 and (
        not isinstance(retry_of, str) or not retry_of.strip()
    ):
        raise ContractError("retry assignment requires its predecessor identifier")
    for field in ("meaningful", "verification_required"):
        if not isinstance(data[field], bool):
            raise ContractError(f"{field} must be boolean")
    for field in (
        "owned_paths",
        "context",
        "stopping_conditions",
        "prohibited_actions",
    ):
        if not isinstance(data[field], list) or not all(
            isinstance(item, str) for item in data[field]
        ):
            raise ContractError(f"{field} must be an array of strings")
    required_evidence = data["required_evidence"]
    if not isinstance(required_evidence, list) or not required_evidence:
        raise ContractError("required_evidence must be a non-empty array")
    evidence_ids: set[str] = set()
    for index, criterion in enumerate(required_evidence):
        if not isinstance(criterion, dict) or set(criterion) != {"id", "description"}:
            raise ContractError(
                f"required_evidence[{index}] must contain exactly id and description"
            )
        _nonempty_string(criterion["id"], f"required_evidence[{index}].id")
        _nonempty_string(
            criterion["description"], f"required_evidence[{index}].description"
        )
        if criterion["id"] in evidence_ids:
            raise ContractError(f"duplicate required evidence id: {criterion['id']}")
        evidence_ids.add(criterion["id"])
    _validate_permissions(data["permissions"], "permissions")
    bounds = data["bounds"]
    if not isinstance(bounds, dict):
        raise ContractError("bounds must be an object")
    _required(bounds, BOUND_FIELDS, "bounds")
    bounds_unknown = sorted(bounds.keys() - BOUND_FIELDS)
    if bounds_unknown:
        raise ContractError(f"bounds has unknown fields: {', '.join(bounds_unknown)}")
    for field in BOUND_FIELDS:
        minimum = 0 if field in {"max_steers_per_attempt", "max_tool_calls"} else 1
        if not isinstance(bounds[field], int) or bounds[field] < minimum:
            raise ContractError(f"bounds.{field} must be an integer of at least {minimum}")
    return data


def validate_assignment(
    data: Any,
    *,
    task: Any,
    route: Any,
    parent_grant: Any,
) -> dict[str, Any]:
    data = validate_assignment_shape(data)
    permissions = data["permissions"]
    parent = validate_parent_grant(parent_grant)
    checked_task = validate_task(task)
    if not isinstance(route, dict):
        raise ContractError("route must be an object")
    for field in ("tools", "filesystem", "mcps"):
        excess = sorted(set(permissions[field]) - set(parent[field]))
        if excess:
            raise ContractError(
                f"permissions.{field} exceeds parent grant: {', '.join(excess)}"
            )
    for field in ("network", "external_effects"):
        if permissions[field] and not parent[field]:
            raise ContractError(f"permissions.{field} exceeds parent grant")
    invalid_filesystem = [
        item
        for item in permissions["filesystem"]
        if not item.endswith(":read") and not item.endswith(":write")
    ]
    if invalid_filesystem:
        raise ContractError("version 1 child filesystem permissions are invalid")
    write_requested = any(
        item.endswith(":write") for item in permissions["filesystem"]
    )
    if write_requested and (
        checked_task["family"] not in {"coding", "verification"}
        or route.get("sandbox_mode") != "workspace-write"
        or "project_agent_mapping_digest" not in route
    ):
        raise ContractError(
            "version 1 child filesystem writes require an explicitly mapped "
            "workspace-write coding or verification route"
        )
    if permissions["network"] or permissions["external_effects"]:
        raise ContractError("version 1 child network and external effects must be false")
    bounds = data["bounds"]
    for field in ("task_id", "role", "model", "effort", "verification_required", "bounds"):
        if field not in route:
            raise ContractError(f"route missing required field: {field}")
    exact = {
        "task_id": checked_task["id"],
        "role": route["role"],
        "model": route["model"],
        "effort": route["effort"],
        "meaningful": checked_task["meaningful"],
        "verification_required": route["verification_required"],
    }
    for field, expected in exact.items():
        if data[field] != expected:
            raise ContractError(f"assignment {field} does not match routed value")
    expected_attempt = checked_task["failed_attempts"] + 1
    if data["attempt"] != expected_attempt:
        raise ContractError("assignment attempt does not match task failure history")
    if data["attempt"] > bounds["max_attempts"]:
        raise ContractError("attempt exceeds assignment max_attempts")
    route_bounds = route["bounds"]
    if not isinstance(route_bounds, dict) or set(route_bounds) != BOUND_FIELDS:
        raise ContractError("route bounds are invalid")
    for field in BOUND_FIELDS:
        if bounds[field] > route_bounds[field]:
            raise ContractError(f"bounds.{field} exceeds routed policy bound")
    task_paths = set(checked_task.get("owned_paths", []))
    if not set(data["owned_paths"]).issubset(task_paths):
        raise ContractError("owned_paths exceed task ownership")
    required_prohibitions = set(checked_task.get("prohibited_actions", []))
    if not required_prohibitions.issubset(data["prohibited_actions"]):
        raise ContractError("assignment omits task prohibited actions")
    return data


def _legacy_result_axes(status: str) -> tuple[str, str, str]:
    """Project a legacy status without pretending a negative review failed to run."""

    return {
        "completed": ("completed", "pass", "unverified"),
        "failed": ("failed", "not-applicable", "unverified"),
        "blocked": ("completed", "blocked", "unverified"),
        "interrupted": ("interrupted", "not-applicable", "unverified"),
        "cancelled": ("cancelled", "not-applicable", "unverified"),
    }[status]


def legacy_result_status(execution_state: str, semantic_verdict: str) -> str:
    """Return the stable v1 status projection for v2 result axes."""

    if execution_state == "completed":
        return "blocked" if semantic_verdict == "blocked" else "completed"
    return {
        "failed": "failed",
        "timed_out": "failed",
        "interrupted": "interrupted",
        "cancelled": "cancelled",
        "invalid": "failed",
    }[execution_state]


def result_axes(
    data: dict[str, Any],
    *,
    attempt: int = 1,
    retry_of_assignment_id: str | None = None,
) -> dict[str, Any]:
    """Read explicit result axes or a lossless legacy projection."""

    legacy = _legacy_result_axes(data["status"])
    return {
        "execution_state": data.get("execution_state", legacy[0]),
        "semantic_verdict": data.get("semantic_verdict", legacy[1]),
        "admission_state": data.get("admission_state", legacy[2]),
        "retry": data.get(
            "retry",
            {
                "attempt": attempt,
                "retry_of_assignment_id": retry_of_assignment_id,
            },
        ),
    }


def result_preflight(assignment: Any) -> dict[str, Any]:
    """Return the exact, non-mutating result contract before an attempt starts."""

    try:
        assignment = validate_assignment_shape(assignment)
    except ContractError as exc:
        raise ResultContractError(
            "assignment_mismatch", "a validated assignment is required"
        ) from exc
    required_evidence = assignment.get("required_evidence")
    if not isinstance(required_evidence, list) or not required_evidence:
        raise ResultContractError(
            "assignment_mismatch", "assignment has no structured required evidence"
        )
    criteria = []
    for item in required_evidence:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("description"), str)
        ):
            raise ResultContractError(
                "assignment_mismatch", "assignment required evidence is invalid"
            )
        criteria.append({"id": item["id"], "description": item["description"]})
    return {
        "assignment_id": assignment["assignment_id"],
        "template": {
            "assignment_id": assignment["assignment_id"],
            "status": "completed",
            "execution_state": "completed",
            "semantic_verdict": "pass",
            "admission_state": "unverified",
            "retry": {
                "attempt": assignment.get("attempt", 1),
                "retry_of_assignment_id": assignment.get("retry_of_assignment_id"),
            },
            "summary": "",
            "artifacts": [],
            "evidence": [
                {
                    "criterion_id": item["id"],
                    "type": "artifact",
                    "reference": "",
                    "outcome": "observed",
                    "details": "",
                }
                for item in criteria
            ],
            "unverified_claims": [],
            "remaining_risks": [],
            "usage": None,
        },
        "required_evidence": criteria,
        "evidence_types": sorted(EVIDENCE_TYPES),
        "evidence_outcomes": sorted(EVIDENCE_OUTCOMES),
        "execution_states": sorted(EXECUTION_STATES),
        "semantic_verdicts": sorted(SEMANTIC_VERDICTS),
        "admission_states": sorted(ADMISSION_STATES),
        "consistency_rules": [
            "status must equal the legacy projection of execution_state and semantic_verdict",
            "only completed execution may carry a substantive semantic verdict",
            "accepted admission requires completed execution; verdict remains independent",
            "completed execution must cover every required criterion",
            "negative verdicts may contain fail or not_run evidence",
        ],
    }


def validate_result(data: Any, *, assignment: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ResultContractError("invalid_shape", "result must be an object")
    required = {
        "assignment_id",
        "status",
        "summary",
        "artifacts",
        "evidence",
        "unverified_claims",
        "remaining_risks",
    }
    allowed = required | {
        "usage",
        "execution_state",
        "semantic_verdict",
        "admission_state",
        "retry",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ResultContractError(
            "missing_required_fields",
            f"result missing required fields: {', '.join(missing)}",
        )
    unknown = sorted(data.keys() - allowed)
    if unknown:
        raise ResultContractError("unknown_fields", "result has unknown fields")
    try:
        _nonempty_string(data["assignment_id"], "assignment_id")
    except ContractError as exc:
        raise ResultContractError("invalid_shape", str(exc)) from exc
    if not isinstance(assignment, dict) or not isinstance(
        assignment.get("assignment_id"), str
    ):
        raise ResultContractError(
            "assignment_mismatch", "a validated assignment is required"
        )
    if data["assignment_id"] != assignment["assignment_id"]:
        raise ResultContractError(
            "assignment_mismatch", "result assignment_id does not match assignment"
        )
    if not isinstance(data["status"], str) or data["status"] not in RESULT_STATUSES:
        raise ResultContractError("invalid_status", "result status is unsupported")
    axes = result_axes(
        data,
        attempt=assignment.get("attempt", 1),
        retry_of_assignment_id=assignment.get("retry_of_assignment_id"),
    )
    execution_state = axes["execution_state"]
    semantic_verdict = axes["semantic_verdict"]
    admission_state = axes["admission_state"]
    if execution_state not in EXECUTION_STATES:
        raise ResultContractError("invalid_status", "execution_state is unsupported")
    if semantic_verdict not in SEMANTIC_VERDICTS:
        raise ResultContractError("invalid_status", "semantic_verdict is unsupported")
    if admission_state not in ADMISSION_STATES:
        raise ResultContractError("invalid_status", "admission_state is unsupported")
    if data["status"] != legacy_result_status(execution_state, semantic_verdict):
        raise ResultContractError(
            "inconsistent_axes", "legacy result status disagrees with explicit axes"
        )
    substantive = SEMANTIC_VERDICTS - {"not-applicable"}
    if execution_state != "completed" and semantic_verdict in substantive:
        raise ResultContractError(
            "inconsistent_axes", "non-completed execution cannot claim a semantic verdict"
        )
    if admission_state == "accepted" and execution_state != "completed":
        raise ResultContractError(
            "inconsistent_axes", "accepted admission requires completed execution"
        )
    retry = axes["retry"]
    if (
        not isinstance(retry, dict)
        or set(retry) != {"attempt", "retry_of_assignment_id"}
        or not isinstance(retry["attempt"], int)
        or isinstance(retry["attempt"], bool)
        or retry["attempt"] < 1
        or (
            retry["retry_of_assignment_id"] is not None
            and (
                not isinstance(retry["retry_of_assignment_id"], str)
                or not retry["retry_of_assignment_id"].strip()
            )
        )
    ):
        raise ResultContractError("invalid_shape", "retry lineage is invalid")
    if retry["attempt"] != assignment.get("attempt", 1):
        raise ResultContractError(
            "assignment_mismatch", "retry attempt does not match assignment"
        )
    if retry["attempt"] == 1 and retry["retry_of_assignment_id"] is not None:
        raise ResultContractError(
            "inconsistent_axes", "first attempt cannot reference a retry predecessor"
        )
    if retry["attempt"] > 1 and retry["retry_of_assignment_id"] is None:
        raise ResultContractError(
            "inconsistent_axes", "retry attempt requires its predecessor assignment"
        )
    if retry["retry_of_assignment_id"] != assignment.get("retry_of_assignment_id"):
        raise ResultContractError(
            "assignment_mismatch", "retry predecessor does not match assignment"
        )
    if not isinstance(data["summary"], str):
        raise ResultContractError("invalid_shape", "summary must be a string")
    for field in ("artifacts", "unverified_claims", "remaining_risks"):
        if not isinstance(data[field], list) or not all(
            isinstance(item, str) for item in data[field]
        ):
            raise ResultContractError(
                "invalid_shape", f"{field} must be an array of strings"
            )
    if not isinstance(data["evidence"], list) or not all(
        isinstance(item, dict) for item in data["evidence"]
    ):
        raise ResultContractError(
            "invalid_evidence", "evidence must be an array of objects"
        )
    if execution_state == "completed" and not data["evidence"]:
        raise ResultContractError(
            "missing_evidence", "completed result must contain evidence"
        )
    evidence_fields = {"criterion_id", "type", "reference", "outcome", "details"}
    required_criteria = {
        item["id"]
        for item in assignment.get("required_evidence", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not required_criteria:
        raise ResultContractError(
            "assignment_mismatch", "assignment has no structured required evidence"
        )
    covered_criteria: set[str] = set()
    for index, item in enumerate(data["evidence"]):
        missing_evidence_fields = sorted(evidence_fields - item.keys())
        if missing_evidence_fields:
            raise ResultContractError(
                "invalid_evidence",
                f"evidence[{index}] missing required fields",
            )
        unknown_evidence = sorted(item.keys() - evidence_fields)
        if unknown_evidence:
            raise ResultContractError(
                "invalid_evidence",
                f"evidence[{index}] has unknown fields: {', '.join(unknown_evidence)}"
            )
        if not isinstance(item["type"], str) or item["type"] not in EVIDENCE_TYPES:
            raise ResultContractError(
                "invalid_evidence", f"evidence[{index}].type is unsupported"
            )
        try:
            _nonempty_string(item["criterion_id"], f"evidence[{index}].criterion_id")
        except ContractError as exc:
            raise ResultContractError("invalid_evidence", str(exc)) from exc
        if item["criterion_id"] not in required_criteria:
            raise ResultContractError(
                "unknown_criterion",
                f"evidence[{index}].criterion_id is not required by the assignment"
            )
        if item["criterion_id"] in covered_criteria:
            raise ResultContractError(
                "duplicate_criterion",
                f"duplicate evidence criterion_id: {item['criterion_id']}"
            )
        covered_criteria.add(item["criterion_id"])
        try:
            _nonempty_string(item["reference"], f"evidence[{index}].reference")
        except ContractError as exc:
            raise ResultContractError("invalid_evidence", str(exc)) from exc
        if (
            not isinstance(item["outcome"], str)
            or item["outcome"] not in EVIDENCE_OUTCOMES
        ):
            raise ResultContractError(
                "invalid_evidence", f"evidence[{index}].outcome is unsupported"
            )
        if not isinstance(item["details"], str):
            raise ResultContractError(
                "invalid_evidence", f"evidence[{index}].details must be a string"
            )
    if (
        execution_state == "completed"
        and semantic_verdict in {"pass", "ready"}
        and any(
        item["outcome"] in {"fail", "not_run"} for item in data["evidence"]
        )
    ):
        raise ResultContractError(
            "failed_completed_evidence",
            "completed result cannot contain failed or unrun evidence",
        )
    if execution_state == "completed" and covered_criteria != required_criteria:
        missing = sorted(required_criteria - covered_criteria)
        raise ResultContractError(
            "missing_required_evidence",
            "completed result is missing required evidence: " + ", ".join(missing)
        )
    usage = data.get("usage")
    if usage is not None:
        usage_fields = {
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            "reasoning_tokens",
            "tool_calls",
            "latency_ms",
            "cost",
        }
        if not isinstance(usage, dict):
            raise ResultContractError(
                "invalid_usage", "usage must be null or an object"
            )
        usage_unknown = sorted(usage.keys() - usage_fields)
        if usage_unknown:
            raise ResultContractError(
                "invalid_usage",
                f"usage has unknown fields: {', '.join(usage_unknown)}",
            )
        for field, value in usage.items():
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ResultContractError(
                    "invalid_usage",
                    f"usage.{field} must be null or a non-negative number",
                )
    return dict(data)


def validate_evaluation_record(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("evaluation record must be an object")
    required = {
        "task_id",
        "family",
        "model",
        "effort",
        "baseline",
        "passed",
        "quality_score",
        "repairs",
        "latency_ms",
        "tool_calls",
    }
    optional = {
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cost",
        "notes",
    }
    _required(data, required, "evaluation record")
    unknown = sorted(data.keys() - required - optional)
    if unknown:
        raise ContractError(f"evaluation record has unknown fields: {', '.join(unknown)}")
    for field in ("task_id", "family", "model", "effort"):
        _nonempty_string(data[field], field)
    for field in ("baseline", "passed"):
        if not isinstance(data[field], bool):
            raise ContractError(f"{field} must be boolean")
    score = data["quality_score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
        raise ContractError("quality_score must be a number from 0 to 1")
    for field in ("repairs", "latency_ms", "tool_calls"):
        if not isinstance(data[field], int) or data[field] < 0:
            raise ContractError(f"{field} must be a non-negative integer")
    for field in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
        value = data.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ContractError(f"{field} must be null or a non-negative integer")
    cost = data.get("cost")
    if cost is not None and (
        not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0
    ):
        raise ContractError("cost must be null or a non-negative number")
    return data
