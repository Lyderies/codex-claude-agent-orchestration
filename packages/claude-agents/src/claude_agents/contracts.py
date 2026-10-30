"""Closed, immutable fixture contracts; parsed observations are never authority."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable

SYNTHETIC_MODEL = "fixture-claude-v1"
MAX_JSON_DEPTH = 32
READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
ROLES = frozenset({
    "claude_agents_researcher", "claude_agents_planner",
    "claude_agents_implementation_worker", "claude_agents_security_reviewer",
    "claude_agents_verification_worker",
})
STATES = frozenset({"pre-send", "sent", "acknowledged", "terminal", "ambiguous"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class ContractError(ValueError):
    """Content-free code: callers must not append rejected input to diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def reject(code: str) -> None:
    raise ContractError(code)


def _record(value: Any, fields: str, *, versioned: bool = True) -> dict:
    if type(value) is not dict or set(value) != set(fields.split()):
        reject("invalid_fields")
    if versioned and (type(value["schema_version"]) is not int
                      or value["schema_version"] != 1):
        reject("unsupported_schema")
    return value


def _text(value: Any, maximum: int = 4096) -> str:
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        reject("invalid_text")
    return value


def _identifier(value: Any) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        reject("invalid_identifier")
    return value


def relative_path(value: Any) -> str:
    """Canonical literal root-relative token, not a filesystem reference monitor."""
    if (type(value) is not str or len(value) > 256
            or any(p in {"", ".", ".."} or not _PATH_PART.fullmatch(p)
                   for p in value.split("/"))):
        reject("invalid_path")
    return value


def _choice(value: Any, choices: set | frozenset) -> str:
    if type(value) is not str or value not in choices:
        reject("unsupported_value")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        reject("invalid_number")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        reject("invalid_boolean")
    return value


def _sequence(value: Any, parser: Callable, minimum: int = 0,
              maximum: int = 32) -> tuple:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        reject("invalid_array")
    result = tuple(parser(item) for item in value)
    if len(result) != len(set(result)):
        reject("duplicate_item")
    return result


def _empty(value: Any) -> tuple:
    if type(value) is not list or value:
        reject("capability_denied")
    return ()


def _disabled(value: Any) -> bool:
    if _boolean(value):
        reject("capability_denied")
    return False


def _version(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _VERSION.fullmatch(value):
        reject("invalid_version")
    return value


def safe_load_json(text: str) -> Any:
    """Bound input/depth; reject duplicate keys and floating/nonfinite numbers."""
    if type(text) is not str or len(text) > 131072:
        reject("invalid_json")
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                reject("invalid_json")
        elif character in "]}":
            depth -= 1

    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                reject("duplicate_key")
            result[key] = value
        return result

    def number(_: str) -> None:
        reject("invalid_number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=number,
                          parse_constant=number)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid_json") from None


def to_wire(value: Any) -> dict:
    """Return a detached JSON-shaped copy; tuple fields become arrays."""
    try:
        return json.loads(json.dumps(asdict(value), allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise ContractError("invalid_contract") from None


@dataclass(frozen=True, slots=True)
class Bounds:
    max_attempts: int
    timeout_seconds: int
    max_tool_calls: int
    max_no_progress_events: int


def _bounds(value: Any) -> Bounds:
    d = _record(value, "max_attempts timeout_seconds max_tool_calls "
                "max_no_progress_events", versioned=False)
    return Bounds(_integer(d["max_attempts"], 1, 1),
                  _integer(d["timeout_seconds"], 1, 3600),
                  _integer(d["max_tool_calls"], 0, 128),
                  _integer(d["max_no_progress_events"], 1, 16))


@dataclass(frozen=True, slots=True)
class Capabilities:
    tools: tuple[str, ...]
    read_paths: tuple[str, ...]
    network: bool
    mcps: tuple
    hooks: tuple
    secrets: tuple
    external_effects: bool


def _capabilities(value: Any) -> Capabilities:
    d = _record(value, "tools read_paths network mcps hooks secrets external_effects",
                versioned=False)
    return Capabilities(
        _sequence(d["tools"], lambda v: _choice(v, READ_TOOLS), maximum=3),
        _sequence(d["read_paths"], relative_path),
        _disabled(d["network"]), _empty(d["mcps"]), _empty(d["hooks"]),
        _empty(d["secrets"]), _disabled(d["external_effects"]),
    )


@dataclass(frozen=True, slots=True)
class Task:
    schema_version: int
    task_id: str
    job_id: str
    objective: str
    family: str
    owned_paths: tuple[str, ...]
    required_evidence: tuple[str, ...]


def parse_task(value: Any) -> Task:
    d = _record(value, "schema_version task_id job_id objective family owned_paths "
                "required_evidence")
    return Task(
        1, _identifier(d["task_id"]), _identifier(d["job_id"]),
        _text(d["objective"]),
        _choice(d["family"], {"inventory", "research", "planning", "coding",
                             "documentation", "security", "verification"}),
        _sequence(d["owned_paths"], relative_path, 1),
        _sequence(d["required_evidence"], _identifier, 1),
    )


@dataclass(frozen=True, slots=True)
class ParentGrant:
    schema_version: int
    grant_id: str
    task_id: str
    job_id: str
    capabilities: Capabilities
    bounds: Bounds


def parse_parent_grant(value: Any) -> ParentGrant:
    d = _record(value, "schema_version grant_id task_id job_id capabilities bounds")
    return ParentGrant(1, _identifier(d["grant_id"]), _identifier(d["task_id"]),
                       _identifier(d["job_id"]), _capabilities(d["capabilities"]),
                       _bounds(d["bounds"]))


@dataclass(frozen=True, slots=True)
class Assignment:
    schema_version: int
    assignment_id: str
    task_id: str
    job_id: str
    attempt_id: str
    attempt: int
    parent_grant_id: str
    role: str
    requested_model: str
    context_paths: tuple[str, ...]
    capabilities: Capabilities
    bounds: Bounds
    required_evidence: tuple[str, ...]
    lifecycle_state: str


def _assignment_shape(value: Any) -> Assignment:
    d = _record(value, "schema_version assignment_id task_id job_id attempt_id "
                "attempt parent_grant_id role requested_model context_paths "
                "capabilities bounds required_evidence lifecycle_state")
    a = Assignment(
        1, _identifier(d["assignment_id"]), _identifier(d["task_id"]),
        _identifier(d["job_id"]), _identifier(d["attempt_id"]),
        _integer(d["attempt"], 1, 1), _identifier(d["parent_grant_id"]),
        _choice(d["role"], ROLES), _identifier(d["requested_model"]),
        _sequence(d["context_paths"], relative_path),
        _capabilities(d["capabilities"]), _bounds(d["bounds"]),
        _sequence(d["required_evidence"], _identifier, 1),
        _choice(d["lifecycle_state"], STATES),
    )
    if a.requested_model != SYNTHETIC_MODEL:
        reject("synthetic_model_required")
    return a


def parse_assignment(value: Any, *, task: Task, parent: ParentGrant) -> Assignment:
    task = parse_task(to_wire(task))
    parent = parse_parent_grant(to_wire(parent))
    a = _assignment_shape(value)
    if (a.task_id != task.task_id or a.job_id != task.job_id
            or parent.task_id != task.task_id or parent.job_id != task.job_id
            or a.parent_grant_id != parent.grant_id):
        reject("identity_mismatch")
    if a.required_evidence != task.required_evidence:
        reject("evidence_mismatch")
    if (not set(a.capabilities.tools) <= set(parent.capabilities.tools)
            or not set(a.capabilities.read_paths) <= set(parent.capabilities.read_paths)
            or not set(a.context_paths) <= set(a.capabilities.read_paths)
            or not set(a.capabilities.read_paths) <= set(task.owned_paths)):
        reject("grant_expansion")
    if any(v > getattr(parent.bounds, key)
           for key, v in asdict(a.bounds).items()):
        reject("bound_expansion")
    return a


@dataclass(frozen=True, slots=True)
class Usage:
    schema_version: int
    source: str
    coverage: str
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    tool_calls: int | None
    latency_ms: int | None


def parse_usage(value: Any) -> Usage | None:
    if value is None:
        return None
    names = ("input_tokens", "cached_tokens", "output_tokens",
             "reasoning_tokens", "tool_calls", "latency_ms")
    d = _record(value, "schema_version source coverage " + " ".join(names))
    source = _choice(d["source"], {"synthetic", "unavailable"})
    coverage = _choice(d["coverage"], {"unknown", "partial", "complete"})
    numbers = tuple(None if d[k] is None else _integer(d[k], 0, 1000000000)
                    for k in names)
    if (source == "unavailable" and (coverage != "unknown"
                                    or any(v is not None for v in numbers))):
        reject("usage_provenance")
    if coverage == "unknown" and any(v is not None for v in numbers):
        reject("usage_provenance")
    if coverage == "complete" and any(v is None for v in numbers):
        reject("usage_provenance")
    for total, subset in ((numbers[0], numbers[1]), (numbers[2], numbers[3])):
        if subset is not None and (total is None or subset > total):
            reject("invalid_usage_subset")
    return Usage(1, source, coverage, *numbers)


@dataclass(frozen=True, slots=True)
class Evidence:
    criterion_id: str
    type: str
    reference: str
    outcome: str
    details: str


def _evidence(value: Any) -> Evidence:
    d = _record(value, "criterion_id type reference outcome details", versioned=False)
    return Evidence(
        _identifier(d["criterion_id"]),
        _choice(d["type"], {"command", "source", "artifact", "review", "observation"}),
        _text(d["reference"], 512),
        _choice(d["outcome"], {"pass", "fail", "observed", "not_run"}),
        _text(d["details"]),
    )


@dataclass(frozen=True, slots=True)
class Result:
    schema_version: int
    assignment_id: str
    task_id: str
    job_id: str
    attempt_id: str
    requested_model: str
    resolved_model: str
    execution_state: str
    semantic_verdict: str
    admission_state: str
    summary: str
    artifacts: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    unverified_claims: tuple[str, ...]
    remaining_risks: tuple[str, ...]
    usage: Usage | None


def parse_result(value: Any, *, assignment: Assignment) -> Result:
    assignment = _assignment_shape(to_wire(assignment))
    d = _record(value, "schema_version assignment_id task_id job_id attempt_id "
                "requested_model resolved_model execution_state semantic_verdict "
                "admission_state summary artifacts evidence unverified_claims "
                "remaining_risks usage")
    result = Result(
        1, *(_identifier(d[k]) for k in ("assignment_id", "task_id", "job_id",
                                        "attempt_id", "requested_model",
                                        "resolved_model")),
        _choice(d["execution_state"], {"completed", "failed", "timed_out",
                                      "interrupted", "cancelled", "invalid"}),
        _choice(d["semantic_verdict"], {"pass", "ready", "ready-with-corrections",
                                       "not-ready", "blocked", "reject",
                                       "not-applicable"}),
        _choice(d["admission_state"], {"unverified"}), _text(d["summary"]),
        _sequence(d["artifacts"], relative_path),
        _sequence(d["evidence"], _evidence),
        _sequence(d["unverified_claims"], _text),
        _sequence(d["remaining_risks"], _text), parse_usage(d["usage"]),
    )
    if any(getattr(result, k) != getattr(assignment, k)
           for k in ("assignment_id", "task_id", "job_id", "attempt_id",
                     "requested_model")):
        reject("identity_mismatch")
    if result.resolved_model != assignment.requested_model:
        reject("model_mismatch")
    criteria = tuple(e.criterion_id for e in result.evidence)
    if (len(criteria) != len(set(criteria))
            or not set(criteria) <= set(assignment.required_evidence)):
        reject("invalid_evidence")
    if result.execution_state == "completed":
        if set(criteria) != set(assignment.required_evidence):
            reject("missing_evidence")
    if result.semantic_verdict in {"pass", "ready"}:
        if (result.execution_state != "completed" or result.unverified_claims
                or result.remaining_risks
                or any(e.outcome not in {"pass", "observed"} for e in result.evidence)):
            reject("inconsistent_verdict")
    return result


@dataclass(frozen=True, slots=True)
class VersionObservation:
    schema_version: int
    assignment_id: str
    attempt_id: str
    source: str
    channel: str
    client_version: str | None
    latest_version: str | None
    fresh: bool
    capabilities_verified: bool


def parse_version_observation(value: Any) -> VersionObservation:
    d = _record(value, "schema_version assignment_id attempt_id source channel "
                "client_version latest_version fresh capabilities_verified")
    return VersionObservation(
        1, _identifier(d["assignment_id"]), _identifier(d["attempt_id"]),
        _choice(d["source"], {"synthetic", "unavailable"}),
        _choice(d["channel"], {"latest", "stable", "unknown"}),
        _version(d["client_version"]), _version(d["latest_version"]),
        _boolean(d["fresh"]), _boolean(d["capabilities_verified"]),
    )


@dataclass(frozen=True, slots=True)
class BillingObservation:
    schema_version: int
    assignment_id: str
    attempt_id: str
    source: str
    provider: str
    auth_method: str
    billing_mode: str
    usage_credits: str
    entitlement: str
    fresh: bool


def parse_billing_observation(value: Any) -> BillingObservation:
    d = _record(value, "schema_version assignment_id attempt_id source provider "
                "auth_method billing_mode usage_credits entitlement fresh")
    return BillingObservation(
        1, _identifier(d["assignment_id"]), _identifier(d["attempt_id"]),
        _choice(d["source"], {"synthetic", "unavailable"}),
        _choice(d["provider"], {"first_party", "third_party", "unknown"}),
        _choice(d["auth_method"], {"claude.ai", "api_key", "unknown"}),
        _choice(d["billing_mode"], {"subscription", "api", "usage_credits", "unknown"}),
        _choice(d["usage_credits"], {"disabled", "enabled", "unknown"}),
        _choice(d["entitlement"], {"available", "unavailable", "unknown"}),
        _boolean(d["fresh"]),
    )
