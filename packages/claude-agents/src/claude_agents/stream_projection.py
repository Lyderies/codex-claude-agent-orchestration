"""Offline projection of SDK 0.3.310 frames; never an execution/billing grant.

SDK evidence: sdk.d.ts SHA256
d82d92c92eec70bf4133f9c27ac5c7dc0b8d165159b0d066f348d049c6bac86d,
SDKAssistantMessage:3688, session state:6142, status:6175, thinking:6415.
Removed thinking/signature/opaque payloads are never returned or hashed. Thinking
tokens already belong to output tokens; block/progress counts are not token totals.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import math

from .subscription_stream import RateLimitObservation, parse_subscription_stream
from .telemetry import ModelObservation, TelemetryError, TokenObservation, _frame, _text, _usage

SYSTEM_SUBTYPES = frozenset({
    "api_retry", "background_tasks_changed", "commands_changed", "compact_boundary",
    "control_request_progress", "elicitation_complete", "files_persisted",
    "hook_progress", "hook_response", "hook_started", "informational",
    "local_command_output", "memory_recall", "mirror_error",
    "model_refusal_fallback", "model_refusal_no_fallback", "notification",
    "permission_denied", "plugin_install", "session_state_changed", "status", "init",
    "task_notification", "task_progress", "task_started", "task_updated",
    "thinking_tokens", "worker_shutting_down",
})
_KINDS = frozenset({"assistant", "user", "result", "rate_limit_event", "stream_event",
                    "tool_progress", "tool_use_summary", "auth_status"})
_PASSIVE = ("thinking_tokens", "session_state_changed", "status")
_ACTIVE = frozenset({"resume_reason", "resumed_from_incomplete_thinking", "supersedes",
                     "aborted", "resumed", "is_resumed"})
_CHILD = ("parent_tool_use_id", "parent_id", "parent_agent_id", "agent_id", "subagent_type")
_USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens",
          "cache_creation_input_tokens", "output_tokens_details", "server_tool_use",
          "cache_creation", "iterations", "fallback_credit", "service_tier", "speed", "inference_geo")


# Fixed labels only; delegated frozen-parser codes are included below.
KnownProjectionErrorCodes = frozenset({
    "active_or_unknown_system", "active_session_state", "active_status",
    "assistant_error", "assistant_order_or_identity", "billing_counterevidence",
    "duplicate_frame", "duplicate_json_key", "input_transformation_activity",
    "invalid_cache_usage", "invalid_cost_estimate", "invalid_expectation",
    "invalid_frame", "invalid_identifier", "invalid_number",
    "invalid_rate_limit_boolean", "invalid_rate_limit_frame", "invalid_rate_limit_info",
    "invalid_rate_limit_number", "invalid_text", "invalid_thinking_estimate",
    "invalid_turn_count", "invalid_usage", "malformed_json",
    "malformed_thinking", "meaningful_message_metadata", "missing_init",
    "missing_or_duplicate_init", "missing_or_extra_frames", "missing_text_or_terminal",
    "missing_usage", "model_mismatch", "model_surface_mismatch",
    "multiple_or_changed_message", "permission_change", "projection_denied",
    "result_content_mismatch", "result_error", "result_order",
    "resumed_or_aborted", "server_container_activity", "session_mismatch",
    "stop_details_activity", "stream_depth", "stream_size",
    "unexpected_activity", "unexpected_child", "unexpected_content",
    "unexpected_customization", "unexpected_frame", "unexpected_stop",
    "unexpected_tool", "unknown_message_field", "unknown_usage_field",
    "invalid_citations", "invalid_text_block", "invalid_tool_block",
    "invalid_tool_caller", "invalid_tool_input", "toolset_routing",
    "unsupported_rate_limit_value", "unsupported_usage_metadata",
})


def known_error_code(error: BaseException) -> str:
    """Safe diagnostic label; never stringify an exception or return caller data."""
    if (type(error) is TelemetryError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in KnownProjectionErrorCodes):
        return error.args[0]
    return "projection_denied"


def _require(value, code="projection_denied"):
    if not value:
        raise TelemetryError(code)


def frame_descriptor(frame: object) -> str:
    """Only fixed labels; this description performs no admission validation."""
    if type(frame) is not dict:
        return "unknown"
    kind = frame.get("type")
    if kind == "system":
        subtype = frame.get("subtype")
        return "system:" + subtype if type(subtype) is str and subtype in SYSTEM_SUBTYPES else "system:unknown"
    return kind if type(kind) is str and kind in _KINDS else "unknown"


@dataclass(frozen=True, slots=True)
class PassiveSystem:
    subtype: str
    state: str | None = None


@dataclass(frozen=True, slots=True)
class BlockMetadataCounts:
    """Validated inactive metadata observations, never direct-execution proof."""
    caller_not_reported: int = 0
    caller_direct: int = 0
    toolset_not_reported: int = 0
    toolset_inactive: int = 0
    citations_not_reported: int = 0
    citations_inactive: int = 0

    def plus(self, other: BlockMetadataCounts) -> BlockMetadataCounts:
        return BlockMetadataCounts(
            self.caller_not_reported + other.caller_not_reported,
            self.caller_direct + other.caller_direct,
            self.toolset_not_reported + other.toolset_not_reported,
            self.toolset_inactive + other.toolset_inactive,
            self.citations_not_reported + other.citations_not_reported,
            self.citations_inactive + other.citations_inactive)


@dataclass(frozen=True, slots=True)
class ThinkingCounts:
    thinking_blocks: int = 0
    redacted_blocks: int = 0
    block_metadata: BlockMetadataCounts = field(default_factory=BlockMetadataCounts)


@dataclass(frozen=True, slots=True)
class ProvisionalUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    thinking_tokens: int | None
    source: str = field(default="assistant.message.usage.provisional", init=False)


@dataclass(frozen=True, slots=True)
class ProjectedNoToolObservation:
    requested_model: str
    session_id: str
    assistant_frame_count: int
    terminal_frame_count: int
    num_turns: int
    assistant_usage: ProvisionalUsage
    terminal_root_usage: TokenObservation
    model_usage: ModelObservation
    root_client_cost_estimate_usd: float | None
    result_sha256: str
    stream_sha256: str
    runtime_enabled: bool = field(default=False, init=False)
    billing_verified: bool = field(default=False, init=False)
    subscription_cost_usd: None = field(default=None, init=False)
    quota_remaining: None = field(default=None, init=False)
    five_hour_reset_at: None = field(default=None, init=False)
    weekly_reset_at: None = field(default=None, init=False)


@dataclass(frozen=True, slots=True)
class ProjectedSubscriptionObservation:
    no_tool: ProjectedNoToolObservation
    rate_limits: tuple[RateLimitObservation, ...]
    original_stream_sha256: str


@dataclass(frozen=True, slots=True)
class NoToolProjection:
    observation: ProjectedSubscriptionObservation
    descriptors: tuple[str, ...]
    passive_counts: tuple[tuple[str, int], ...]
    thinking_counts: ThinkingCounts
    original_assistant_frames: int
    assistant_usage_is_final: bool = False
    hash_scope: str = "canonical_text_only_projection"


def _identifier(value):
    text = _text(value, 128)
    _require(all(c.isascii() and (c.isalnum() or c in "_-") for c in text), "invalid_identifier")
    return text


def passive_system(frame: object, session_id: str) -> PassiveSystem:
    """Closed passive schemas. Estimates are validated and discarded."""
    _identifier(session_id)
    _require(type(frame) is dict and frame.get("type") == "system")
    subtype = frame.get("subtype")
    _require(type(subtype) is str and subtype in _PASSIVE, "active_or_unknown_system")
    common = {"type", "subtype", "uuid", "session_id"}
    _require(frame.get("session_id") == session_id, "session_mismatch")
    _identifier(frame.get("uuid"))
    if subtype == "thinking_tokens":
        required = common | {"estimated_tokens", "estimated_tokens_delta"}
        _require(required <= frame.keys() and set(frame) <= required | {"user_message_uuid"})
        for name in ("estimated_tokens", "estimated_tokens_delta"):
            number = frame[name]
            _require(type(number) in (int, float) and 0 <= number <= 10**9 and math.isfinite(number),
                     "invalid_thinking_estimate")
        if "user_message_uuid" in frame:
            _identifier(frame["user_message_uuid"])
        return PassiveSystem(subtype)
    if subtype == "session_state_changed":
        _require(set(frame) == common | {"state"} and type(frame.get("state")) is str
                 and frame["state"] in ("running", "idle"), "active_session_state")
        return PassiveSystem(subtype, frame["state"])
    _require(common | {"status"} <= frame.keys()
             and set(frame) <= common | {"status", "permissionMode"})
    _require(frame["status"] is None or (type(frame["status"]) is str and frame["status"] == "requesting"), "active_status")
    if "permissionMode" in frame:
        _require(type(frame["permissionMode"]) is str and frame["permissionMode"] == "dontAsk", "permission_change")
    return PassiveSystem(subtype, frame["status"])


def _data(value, budget, depth=0):
    """Detached bounded JSON data, including tool arguments; grants belong elsewhere."""
    _require(depth <= 16)
    kind = type(value)
    if kind is str:
        try:
            budget[0] -= len(value.encode("utf-8"))
        except UnicodeError:
            raise TelemetryError("invalid_text") from None
        _require(budget[0] >= 0)
    elif kind is dict:
        _require(len(value) <= 128 and all(type(k) is str and len(k) <= 256 for k in value))
        return {_data(k, budget, depth + 1): _data(v, budget, depth + 1) for k, v in value.items()}
    elif kind is list:
        _require(len(value) <= 128)
        return [_data(v, budget, depth + 1) for v in value]
    elif kind in (int, float):
        _require(abs(value) <= 10**15 and math.isfinite(value))
    else:
        _require(value is None or kind is bool)
    return value


def _counterevidence(frame):
    _require(not (_ACTIVE & frame.keys()), "resumed_or_aborted")
    _require(all(frame.get(name) is None for name in _CHILD), "unexpected_child")
    _require(frame.get("error") is None and frame.get("is_error", False) is False, "assistant_error")
    if "errors" in frame:
        _require(type(frame["errors"]) is list and not frame["errors"], "assistant_error")


def _count(value, maximum=10**9):
    _require(type(value) is int and 0 <= value <= maximum, "invalid_usage")
    return value


def parse_provisional_usage(value: object, max_output_tokens: int = 10**9) -> ProvisionalUsage:
    """Per-block counts are provisional. Missing/null optional counts stay unknown.

    Thinking may exceed an output placeholder. Final thinking/output consistency
    and complete usage remain the unchanged terminal parser's responsibility.
    """
    _require(type(max_output_tokens) is int and 1 <= max_output_tokens <= 10**9, "invalid_usage")
    _require(type(value) is dict and set(value) <= set(_USAGE), "unknown_usage_field")
    inputs = _count(value.get("input_tokens"))
    outputs = _count(value.get("output_tokens"), max_output_tokens)
    cache = [None if value.get(k) is None else _count(value[k])
             for k in ("cache_read_input_tokens", "cache_creation_input_tokens")]
    details = value.get("output_tokens_details")
    _require(details is None or (type(details) is dict and set(details) <= {"thinking_tokens"}), "invalid_usage")
    thinking = details.get("thinking_tokens") if details is not None else None
    if thinking is not None:
        thinking = _count(thinking)
    creation = value.get("cache_creation")
    keys = {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"}
    _require(creation is None or (type(creation) is dict and set(creation) <= keys), "invalid_cache_usage")
    if creation is not None:
        parts = [None if creation.get(k) is None else _count(creation[k]) for k in sorted(keys)]
        if cache[1] is not None and all(p is not None for p in parts):
            _require(sum(parts) == cache[1], "invalid_cache_usage")
    tools = value.get("server_tool_use")
    _require(tools is None or (type(tools) is dict and
             set(tools) <= {"web_search_requests", "web_fetch_requests"}), "unexpected_activity")
    if tools is not None:
        for number in tools.values():
            _require(number is None or (type(number) is int and number == 0), "unexpected_activity")
    iterations = value.get("iterations")
    _require(iterations is None or (type(iterations) is list and not iterations), "unexpected_activity")
    _require(value.get("fallback_credit") is None, "unexpected_activity")
    for name in ("service_tier", "speed"):
        _require(value.get(name) is None or (type(value[name]) is str and value[name] == "standard"),
                 "unsupported_usage_metadata")
    if value.get("inference_geo") is not None:
        _require(type(value["inference_geo"]) is str and len(value["inference_geo"]) <= 128,
                 "unsupported_usage_metadata")
        if value["inference_geo"]:
            _text(value["inference_geo"], 128)
    return ProvisionalUsage(inputs, outputs, cache[0], cache[1], thinking)


def _join_usage(first: ProvisionalUsage, current: ProvisionalUsage) -> ProvisionalUsage:
    _require(first.input_tokens == current.input_tokens, "multiple_or_changed_message")
    updates = {}
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        before, after = getattr(first, name), getattr(current, name)
        _require(before is None or after is None or before == after, "multiple_or_changed_message")
        updates[name] = after if before is None else before
    if first.thinking_tokens is None:
        updates["thinking_tokens"] = current.thinking_tokens
    return replace(first, **updates)  # Keep the first output placeholder; never sum.


def strip_thinking(message: object, *, max_output_tokens: int = 10**9) -> tuple[dict, ThinkingCounts]:
    """Validate/strip exact thinking blocks; return detached safe fields and counts.

    Inactive documented block metadata is validated before normalization. Missing
    citations are an explicit native compatibility policy; missing caller means
    not reported. Tool arguments remain detached data. Caller enforces identity, tools,
    capability grants and request-specific token limits. No payload fingerprint.
    """
    _require(type(message) is dict)
    _counterevidence(message)
    safe = {"id", "type", "role", "model", "stop_reason", "stop_sequence", "usage",
            "content", "error", "context_management", "container", "stop_details",
            "diagnostics", "input_transformations"}
    _require(set(message) <= safe, "unknown_message_field")
    for name, code in (("container", "server_container_activity"), ("stop_details", "stop_details_activity"),
                       ("context_management", "meaningful_message_metadata"),
                       ("diagnostics", "meaningful_message_metadata")):
        _require(message.get(name) is None, code)
    transformations = message.get("input_transformations")
    _require(transformations is None or (type(transformations) is list and not transformations),
             "input_transformation_activity")
    _require(message.get("type") == "message" and message.get("role") == "assistant")
    _text(message.get("id"), 128)
    _text(message.get("model"), 128)
    _require(message.get("stop_reason") in (None, "end_turn", "tool_use")
             and message.get("stop_sequence") is None, "unexpected_stop")
    parse_provisional_usage(message.get("usage"), max_output_tokens)
    budget = [65536]
    clean = {k: _data(v, budget) for k, v in message.items()
             if k in {"id", "type", "role", "model", "stop_reason", "stop_sequence",
                      "container", "context_management", "stop_details", "diagnostics", "input_transformations"}}
    usage = message["usage"]
    clean["usage"] = _data({k: v for k, v in usage.items() if k in _USAGE}, budget)
    blocks = message.get("content")
    _require(type(blocks) is list and 1 <= len(blocks) <= 16, "unexpected_content")
    kept, thinking, redacted = [], 0, 0
    metadata = BlockMetadataCounts()
    for block in blocks:
        _require(type(block) is dict, "unexpected_content")
        kind = block.get("type")
        if kind in ("thinking", "redacted_thinking"):
            fields = {"type", "thinking", "signature"} if kind == "thinking" else {"type", "data"}
            _require(set(block) == fields, "malformed_thinking")
            for name in fields - {"type"}:
                _require(type(block[name]) is str, "malformed_thinking")
                _data(block[name], budget)  # Check bounds only; no copy/hash/result.
            thinking += kind == "thinking"
            redacted += kind == "redacted_thinking"
        elif kind == "text":
            _require({"type", "text"} <= block.keys()
                     and set(block) <= {"type", "text", "citations"}
                     and type(block["text"]) is str, "invalid_text_block")
            citations = block.get("citations")
            _require(citations is None or (type(citations) is list and not citations), "invalid_citations")
            metadata = metadata.plus(BlockMetadataCounts(
                citations_not_reported=int("citations" not in block),
                citations_inactive=int("citations" in block)))
            kept.append(_data({"type": "text", "text": block["text"]}, budget))
        elif kind == "tool_use":
            required = {"type", "id", "name", "input"}
            _require(required <= block.keys()
                     and set(block) <= required | {"caller", "toolset_name"}, "invalid_tool_block")
            _require(type(block["input"]) is dict, "invalid_tool_input")
            if "caller" in block:
                caller = block["caller"]
                _require(type(caller) is dict and set(caller) == {"type"}
                         and type(caller["type"]) is str and caller["type"] == "direct", "invalid_tool_caller")
            _require(block.get("toolset_name") is None, "toolset_routing")
            metadata = metadata.plus(BlockMetadataCounts(
                caller_not_reported=int("caller" not in block),
                caller_direct=int("caller" in block),
                toolset_not_reported=int("toolset_name" not in block),
                toolset_inactive=int("toolset_name" in block)))
            _text(block["id"], 128)
            _text(block["name"], 256)
            kept.append(_data({k: block[k] for k in ("type", "id", "name", "input")}, budget))
        else:
            raise TelemetryError("unexpected_content")
    clean["content"] = kept
    return clean, ThinkingCounts(thinking, redacted, metadata)


def project_no_tool(raw: str, *, requested_model: str) -> NoToolProjection:
    """Validate every original frame, then delegate a text-only one-message view.

    UUIDs identify block-frame replays without storing or hashing reasoning.
    Canonical hashes cover a text-only validation view. Its internal assistant
    usage surrogate is never returned: assistant facts retain nullable provisional
    values, while finalized totals come only from the unchanged terminal parser.
    """
    _require(type(raw) is str and len(raw) <= 262144, "stream_size")
    try:
        _require(len(raw.encode("utf-8")) <= 262144, "stream_size")
    except UnicodeError:
        raise TelemetryError("malformed_json") from None
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    _require(3 <= len(lines) <= 128 and all(len(line.encode()) <= 65536 for line in lines), "stream_size")
    frames = [_frame(line) for line in lines]
    inits = [f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"]
    _require(len(inits) == 1, "missing_or_duplicate_init")
    init = inits[0]
    session = _identifier(init.get("session_id"))
    counts = {name: 0 for name in _PASSIVE}
    descriptors, uuids, rates, texts = [], set(), [], []
    first = terminal = None
    started = ended = False
    identity = provisional = None
    thinking = redacted = assistant_count = 0
    metadata = BlockMetadataCounts()
    for frame in frames:
        descriptors.append(frame_descriptor(frame))
        _require(frame.get("session_id") == session, "session_mismatch")
        _counterevidence(frame)
        if "uuid" in frame:
            uuid = _identifier(frame["uuid"])
            _require(uuid not in uuids, "duplicate_frame")
            uuids.add(uuid)
        kind = frame.get("type")
        if kind == "system":
            if frame is init:
                _require(not started and not ended)
                started = True
            else:
                passive = passive_system(frame, session)
                counts[passive.subtype] += 1
        elif kind == "assistant":
            _require(started and not ended and "uuid" in frame, "assistant_order_or_identity")
            message, count = strip_thinking(frame.get("message"), max_output_tokens=256)
            _require(message["model"] == requested_model, "model_mismatch")
            _require(message.get("stop_reason") in (None, "end_turn"), "unexpected_stop")
            observed = parse_provisional_usage(message["usage"], 256)
            if first is None:
                identity, provisional = message["id"], observed
                first = {"type": "assistant", "session_id": session, "parent_tool_use_id": None,
                         "message": message}
            _require(message["id"] == identity, "multiple_or_changed_message")
            provisional = _join_usage(provisional, observed)
            assistant_count += 1
            thinking += count.thinking_blocks
            redacted += count.redacted_blocks
            metadata = metadata.plus(count.block_metadata)
            for block in message["content"]:
                _require(block["type"] == "text", "unexpected_tool")
                texts.append(block["text"])
        elif kind == "rate_limit_event":
            rates.append(frame)  # Existing wrapper validates the exact schema/counterevidence.
        elif kind == "result":
            _require(started and first is not None and not ended, "result_order")
            terminal, ended = frame, True
        else:
            raise TelemetryError("unexpected_frame")
    _require(started and ended and first is not None and terminal is not None
             and texts and "".join(texts), "missing_text_or_terminal")
    projected = deepcopy(first)
    projected["message"]["content"] = [{"type": "text", "text": "".join(texts)}]
    # Structural validation only. No invented cache zeros escape as assistant facts.
    root = _usage(terminal.get("usage"), "terminal.usage", 256)
    projected["message"]["usage"] = {"input_tokens": root.input_tokens, "output_tokens": 0,
        "cache_read_input_tokens": root.cache_read_input_tokens,
        "cache_creation_input_tokens": root.cache_creation_input_tokens}
    canonical = "\n".join(json.dumps(f, ensure_ascii=True) for f in [init, projected, *rates, terminal]) + "\n"
    checked = parse_subscription_stream(canonical, requested_model=requested_model)
    final = checked.no_tool
    no_tool = ProjectedNoToolObservation(
        final.requested_model, final.session_id, final.assistant_frame_count,
        final.terminal_frame_count, final.num_turns, provisional, final.terminal_root_usage,
        final.model_usage, final.root_client_cost_estimate_usd, final.result_sha256, final.stream_sha256)
    observation = ProjectedSubscriptionObservation(no_tool, checked.rate_limits, checked.original_stream_sha256)
    return NoToolProjection(observation, tuple(descriptors),
                            tuple((name, counts[name]) for name in _PASSIVE),
                            ThinkingCounts(thinking, redacted, metadata), assistant_count)
