"""Bounded no-tool CLI observations, not billing proof or execution authority.

Shape evidence: current CLI synthetic captures include assistant.message.model and
assistant.message.usage; terminal root usage and modelUsage have distinct coverage.
Never sum those duplicate observation surfaces into a workflow total.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

_MAX_BYTES = 262144
_MAX_LINE = 65536
_MAX_DEPTH = 16
_MAX_NUMBER = 10**15
_MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.-]{1,100}\Z")


class TelemetryError(ValueError):
    """Content-free parser rejection code."""


def _fail(code: str) -> None:
    raise TelemetryError(code)


def _text(value: object, maximum: int = 8192) -> str:
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        _fail("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("invalid_text")
    return value


def _integer(value: object, maximum: int = 10**9) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail("invalid_usage")
    return value


def _money(value: object) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 10**6):
        _fail("invalid_cost_estimate")
    return float(value)


def _bounded(value: object, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        _fail("stream_depth")
    if type(value) is dict:
        if len(value) > 128:
            _fail("stream_size")
        for key, item in value.items():
            _text(key, 256)
            _bounded(item, depth + 1)
    elif type(value) is list:
        if len(value) > 128:
            _fail("stream_size")
        for item in value:
            _bounded(item, depth + 1)
    elif type(value) is str:
        if value:
            _text(value, _MAX_LINE)
    elif type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > _MAX_NUMBER:
            _fail("invalid_number")


def _frame(line: str) -> dict:
    if not line or len(line) > _MAX_LINE:
        _fail("stream_size")
    depth, quoted, escaped = 0, False, False
    for character in line:
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
            if depth > _MAX_DEPTH:
                _fail("stream_depth")
        elif character in "]}":
            depth -= 1
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = value
        return result
    def invalid_number(_):
        _fail("invalid_number")
    try:
        value = json.loads(line, object_pairs_hook=pairs, parse_constant=invalid_number)
        _bounded(value)
    except (ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, TelemetryError):
            raise
        raise TelemetryError("malformed_json") from None
    if type(value) is not dict:
        _fail("invalid_frame")
    return value


def _object(value: object) -> dict:
    if type(value) is not dict:
        _fail("invalid_usage")
    return value


def _zero_tree(value: object) -> None:
    if type(value) is dict:
        for child in value.values():
            _zero_tree(child)
    elif type(value) is not int or value != 0:
        _fail("unexpected_activity")


@dataclass(frozen=True, slots=True)
class TokenObservation:
    source: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    thinking_tokens: int | None


def _usage(value: object, source: str, output_limit: int) -> TokenObservation:
    data = _object(value)
    names = ("input_tokens", "output_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens")
    if not set(names) <= data.keys():
        _fail("missing_usage")
    counts = tuple(_integer(data[name], output_limit if name == "output_tokens"
                            else 10**9) for name in names)
    thinking = None
    if "output_tokens_details" in data:
        details = _object(data["output_tokens_details"])
        if "thinking_tokens" in details:
            thinking = _integer(details["thinking_tokens"], counts[1])
    if "server_tool_use" in data:
        _zero_tree(_object(data["server_tool_use"]))
    if "cache_creation" in data:
        cache = _object(data["cache_creation"])
        hours = _integer(cache.get("ephemeral_1h_input_tokens"))
        minutes = _integer(cache.get("ephemeral_5m_input_tokens"))
        if hours + minutes != counts[3]:
            _fail("invalid_cache_usage")
    if "iterations" in data and data["iterations"] != []:
        _fail("unexpected_activity")
    if data.get("fallback_credit") is not None:
        _fail("unexpected_activity")
    return TokenObservation(source, *counts, thinking)


@dataclass(frozen=True, slots=True)
class ModelObservation:
    model: str
    canonical_model: str
    provider_observation: str
    cost_basis: str
    client_list_cost_usd: float
    tokens: TokenObservation


def _model_usage(value: object, model: str, output_limit: int) -> ModelObservation:
    data = _object(value)
    if set(data) != {model}:
        _fail("model_mismatch")
    entry = _object(data[model])
    if (entry.get("canonicalModel") != model or entry.get("provider") != "firstParty"
            or entry.get("costBasis") != "list"):
        _fail("model_surface_mismatch")
    pairs = {
        "input_tokens": "inputTokens", "output_tokens": "outputTokens",
        "cache_read_input_tokens": "cacheReadInputTokens",
        "cache_creation_input_tokens": "cacheCreationInputTokens",
    }
    if not set(pairs.values()) <= entry.keys():
        _fail("missing_usage")
    mapped = {key: entry[name] for key, name in pairs.items()}
    if "thinkingTokens" in entry:
        mapped["output_tokens_details"] = {"thinking_tokens": entry["thinkingTokens"]}
    if "webSearchRequests" in entry:
        _integer(entry["webSearchRequests"], 0)
    for name in ("contextWindow", "maxOutputTokens"):
        if name in entry and _integer(entry[name]) == 0:
            _fail("invalid_usage")
    tokens = _usage(mapped, "terminal.modelUsage", output_limit)
    return ModelObservation(model, model, "firstParty", "list",
                            _money(entry.get("costUSD")), tokens)


@dataclass(frozen=True, slots=True)
class NoToolObservation:
    requested_model: str
    session_id: str
    assistant_frame_count: int
    terminal_frame_count: int
    num_turns: int
    assistant_usage: TokenObservation
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


def parse_no_tool_stream(raw: str, *, requested_model: str, expected_plugins: tuple = (),
                         max_turns: int = 1, max_output_tokens: int = 256) -> NoToolObservation:
    """Parse the strict single-turn profile; only empty-plugin expectation is supported.

    Unknown metadata is bounded and ignored. Session/model/provider/cost claims remain
    observations, and are not authenticated account, billing or effect-closure proof.
    The caller must independently bind its approved binary/configuration/account grant.
    """
    if (type(requested_model) is not str or not _MODEL.fullmatch(requested_model)
            or type(expected_plugins) is not tuple or expected_plugins
            or type(max_turns) is not int or max_turns != 1
            or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 256):
        _fail("invalid_expectation")
    if type(raw) is not str or len(raw) > _MAX_BYTES:
        _fail("stream_size")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        _fail("malformed_json")
    if len(encoded) > _MAX_BYTES:
        _fail("stream_size")
    lines = raw.splitlines()
    if not 3 <= len(lines) <= 16:
        _fail("missing_or_extra_frames")
    frames = [_frame(line) for line in lines]
    if len(frames) != 3:
        _fail("missing_or_extra_frames")
    init, assistant, terminal = frames
    if init.get("type") != "system" or init.get("subtype") != "init":
        _fail("missing_init")
    if assistant.get("type") != "assistant" or terminal.get("type") != "result":
        _fail("unexpected_frame")
    if init.get("model") != requested_model:
        _fail("model_mismatch")
    for name in ("tools", "mcp_servers", "skills", "plugins"):
        if type(init.get(name)) is not list or init[name]:
            _fail("unexpected_customization")
    session = _text(init.get("session_id"), 128)
    for frame in frames:
        if frame.get("session_id") != session:
            _fail("session_mismatch")
        for name in ("parent_tool_use_id", "parent_id", "parent_agent_id", "agent_id"):
            if frame.get(name) is not None:
                _fail("unexpected_child")
        if frame.get("error") is not None:
            _fail("assistant_error")
    message = _object(assistant.get("message"))
    if (message.get("model") != requested_model or message.get("role") != "assistant"
            or message.get("type") != "message"):
        _fail("model_mismatch")
    if message.get("error") is not None:
        _fail("assistant_error")
    if message.get("stop_reason") not in (None, "end_turn") or message.get("stop_sequence") is not None:
        _fail("unexpected_stop")
    content = message.get("content")
    if type(content) is not list or not 1 <= len(content) <= 16:
        _fail("unexpected_content")
    texts = []
    for block in content:
        if type(block) is not dict or block.get("type") != "text":
            _fail("unexpected_content")
        texts.append(_text(block.get("text")))
    assistant_usage = _usage(message.get("usage"), "assistant.message.usage", max_output_tokens)
    if (terminal.get("subtype") != "success" or terminal.get("is_error") is not False
            or terminal.get("stop_reason") != "end_turn"
            or terminal.get("terminal_reason", "completed") != "completed"
            or terminal.get("api_error_status") is not None):
        _fail("result_error")
    turns = _integer(terminal.get("num_turns"), max_turns)
    if turns == 0:
        _fail("invalid_turn_count")
    for name in ("permission_denials", "errors"):
        if name in terminal and (type(terminal[name]) is not list or terminal[name]):
            _fail("unexpected_activity")
    for name in ("safety_stops", "queued_turn_count", "result_index"):
        if name in terminal:
            _integer(terminal[name], 0)
    if "subagent_stats" in terminal:
        _zero_tree(_object(terminal["subagent_stats"]))
    model_usage = _model_usage(terminal.get("modelUsage"), requested_model, max_output_tokens)
    if "model_usage_since_last_result" in terminal:
        _model_usage(terminal["model_usage_since_last_result"], requested_model, max_output_tokens)
    root_usage = _usage(terminal.get("usage"), "terminal.usage", max_output_tokens)
    root_cost = (_money(terminal["total_cost_usd"]) if "total_cost_usd" in terminal else None)
    result = _text(terminal.get("result"))
    if result != "".join(texts):
        _fail("result_content_mismatch")
    return NoToolObservation(
        requested_model, session, 1, 1, turns, assistant_usage, root_usage, model_usage,
        root_cost, hashlib.sha256(result.encode()).hexdigest(), hashlib.sha256(encoded).hexdigest(),
    )
