"""Privacy-preserving normalizers for documented Codex telemetry surfaces."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import ContractError
from .learning_contracts import (
    COVERAGE_CLASSES,
    external_id,
    keyed_digest,
    validate_event,
)


SUPPORTED_MODELS = {
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-6-astra",
}
HOOK_EVENTS = {
    "SessionStart": ("session_started", "started"),
    "SessionEnd": ("session_ended", "completed"),
    "UserPromptSubmit": ("turn_started", "started"),
    "Stop": ("turn_stop_observed", "observed"),
    "Interrupt": ("turn_interrupted", "interrupted"),
    "SubagentStart": ("subagent_started", "started"),
    "SubagentStop": ("subagent_stop_observed", "observed"),
    "PreToolUse": ("tool_started", "started"),
    "PostToolUse": ("tool_output_observed", "observed"),
}
APP_METHODS = {
    "turn/started",
    "turn/completed",
    "item/completed",
    "model/rerouted",
    "thread/tokenUsage/updated",
}
CLIENT_CAPABILITY_FIELDS = {
    "producers",
    "hook_coverage",
    "app_server_coverage",
    "root_lifecycle",
    "child_lifecycle",
    "tool_lifecycle",
    "root_usage",
    "child_usage",
    "ordering_proven",
}


def empty_usage() -> dict[str, None]:
    return {
        "input_tokens": None,
        "cached_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "model_calls": None,
        "tool_calls": None,
        "latency_ms": None,
        "cost_microunits": None,
    }


def _string(data: dict[str, Any], field: str, *, required: bool = True) -> str | None:
    value = data.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ContractError("telemetry field is invalid")
    return value


def _event(
    *,
    key: bytes,
    identity: str,
    run_id: str,
    turn_id: str | None,
    assignment_id: str | None,
    parent_run_id: str | None,
    observed_at_ms: int,
    event_code: str,
    model: str | None,
    role: str | None,
    status: str,
    stop_reason: str | None,
    tool_digest: str | None,
    item_digest: str | None,
    coverage: str,
    usage: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    if coverage not in COVERAGE_CLASSES:
        raise ContractError("coverage class is unsupported")
    record = {
        "schema_version": 1,
        "record_type": "event",
        "record_id": external_id(key, "evt", identity),
        "run_id": run_id,
        "turn_id": turn_id,
        "assignment_id": assignment_id,
        "parent_run_id": parent_run_id,
        "observed_at_ms": observed_at_ms,
        "event_code": event_code,
        "model": model if model in SUPPORTED_MODELS else None,
        "effort": None,
        "role": role,
        "task_family": None,
        "task_slice": None,
        "topology": None,
        "configuration_digest": None,
        "status": status,
        "stop_reason": stop_reason,
        "tool_digest": tool_digest,
        "item_digest": item_digest,
        "coverage": coverage,
        "usage": usage or empty_usage(),
    }
    return validate_event(record)


def normalize_hook(
    payload: Any,
    *,
    digest_key: bytes,
    client_version: str,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    """Map one hook payload without retaining prompts, paths, arguments, or output."""

    if not isinstance(payload, dict):
        raise ContractError("hook payload must be an object")
    event_name = _string(payload, "hook_event_name")
    mapping = HOOK_EVENTS.get(event_name)
    if mapping is None:
        raise ContractError("hook event is unsupported")
    session = _string(payload, "session_id")
    assert session is not None and event_name is not None
    turn = _string(payload, "turn_id", required=False)
    agent = _string(payload, "agent_id", required=False)
    tool_use = _string(payload, "tool_use_id", required=False)
    model = _string(payload, "model", required=False)
    tool_name = _string(payload, "tool_name", required=False)
    timestamp = observed_at_ms if observed_at_ms is not None else time.time_ns() // 1_000_000
    run_material = f"{client_version}\x00{session}\x00{agent or 'root'}"
    run_id = external_id(digest_key, "run", run_material)
    parent_run = external_id(digest_key, "run", f"{client_version}\x00{session}\x00root") if agent else None
    turn_id = external_id(digest_key, "turn", turn) if turn else None
    assignment_id = external_id(digest_key, "asg", agent) if agent else None
    identity_parts = [event_name, session, turn or "", agent or "", tool_use or ""]
    for field in ("source", "reason", "trigger", "stop_hook_active"):
        value = payload.get(field)
        if isinstance(value, (str, bool, int)):
            identity_parts.append(str(value))
    if not any((turn, agent, tool_use)):
        identity_parts.append(str(timestamp))
    event_code, status = mapping
    stop_reason = {
        "SessionEnd": "completed",
        "Interrupt": "interrupted",
    }.get(event_name)
    tool_digest = keyed_digest(digest_key, tool_name) if tool_name else None
    return _event(
        key=digest_key,
        identity="\x00".join(identity_parts),
        run_id=run_id,
        turn_id=turn_id,
        assignment_id=assignment_id,
        parent_run_id=parent_run,
        observed_at_ms=timestamp,
        event_code=event_code,
        model=model,
        role=None,
        status=status,
        stop_reason=stop_reason,
        tool_digest=tool_digest,
        item_digest=None,
        coverage="lifecycle_partial",
    )


def _app_ids(
    params: dict[str, Any], key: bytes, client_version: str
) -> tuple[str, str | None, str]:
    thread = _string(params, "threadId")
    assert thread is not None
    turn_raw = _string(params, "turnId", required=False)
    run_id = external_id(key, "run", f"{client_version}\x00{thread}\x00root")
    turn_id = external_id(key, "turn", turn_raw) if turn_raw else None
    return run_id, turn_id, thread


def normalize_app_server(
    message: Any,
    *,
    digest_key: bytes,
    client_version: str,
    coverage: str = "partial",
    observed_at_ms: int | None = None,
) -> dict[str, Any] | None:
    """Map stable final App Server notifications and ignore text/delta surfaces."""

    if not isinstance(message, dict):
        raise ContractError("App Server message must be an object")
    method = message.get("method")
    if method not in APP_METHODS:
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        raise ContractError("App Server notification params must be an object")
    timestamp = observed_at_ms if observed_at_ms is not None else time.time_ns() // 1_000_000
    if method == "turn/started":
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise ContractError("turn notification is invalid")
        merged = {**params, "turnId": _string(turn, "id")}
        run_id, turn_id, thread = _app_ids(merged, digest_key, client_version)
        return _event(
            key=digest_key,
            identity=f"{method}\x00{thread}\x00{turn['id']}",
            run_id=run_id,
            turn_id=turn_id,
            assignment_id=None,
            parent_run_id=None,
            observed_at_ms=timestamp,
            event_code="turn_started",
            model=None,
            role=None,
            status="started",
            stop_reason=None,
            tool_digest=None,
            item_digest=None,
            coverage=coverage,
        )
    if method == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise ContractError("turn notification is invalid")
        turn_raw = _string(turn, "id")
        status_raw = turn.get("status")
        mapped = {
            "completed": ("turn_completed", "completed", "completed"),
            "failed": ("turn_failed", "failed", "failed"),
            "interrupted": ("turn_interrupted", "interrupted", "interrupted"),
        }.get(status_raw)
        if mapped is None:
            raise ContractError("turn completion status is unsupported")
        merged = {**params, "turnId": turn_raw}
        run_id, turn_id, thread = _app_ids(merged, digest_key, client_version)
        return _event(
            key=digest_key,
            identity=f"{method}\x00{thread}\x00{turn_raw}\x00{status_raw}",
            run_id=run_id,
            turn_id=turn_id,
            assignment_id=None,
            parent_run_id=None,
            observed_at_ms=timestamp,
            event_code=mapped[0],
            model=None,
            role=None,
            status=mapped[1],
            stop_reason=mapped[2],
            tool_digest=None,
            item_digest=None,
            coverage=coverage,
        )
    run_id, turn_id, thread = _app_ids(params, digest_key, client_version)
    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        total = token_usage.get("total") if isinstance(token_usage, dict) else None
        if not isinstance(total, dict):
            raise ContractError("token usage notification is invalid")
        field_map = {
            "input_tokens": "inputTokens",
            "cached_tokens": "cachedInputTokens",
            "output_tokens": "outputTokens",
            "reasoning_tokens": "reasoningOutputTokens",
        }
        usage = empty_usage()
        for target, source in field_map.items():
            value = total.get(source)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ContractError("token usage value is invalid")
            usage[target] = value
        usage["model_calls"] = None
        identity = f"{method}\x00{thread}\x00{params.get('turnId')}\x00{total.get('totalTokens')}"
        return _event(
            key=digest_key,
            identity=identity,
            run_id=run_id,
            turn_id=turn_id,
            assignment_id=None,
            parent_run_id=None,
            observed_at_ms=timestamp,
            event_code="usage_snapshot",
            model=None,
            role=None,
            status="observed",
            stop_reason=None,
            tool_digest=None,
            item_digest=None,
            coverage=coverage,
            usage=usage,
        )
    if method == "model/rerouted":
        from_model = _string(params, "fromModel")
        to_model = _string(params, "toModel")
        reason = _string(params, "reason")
        assert from_model and to_model and reason
        return _event(
            key=digest_key,
            identity=f"{method}\x00{thread}\x00{params.get('turnId')}\x00{from_model}\x00{to_model}\x00{reason}",
            run_id=run_id,
            turn_id=turn_id,
            assignment_id=None,
            parent_run_id=None,
            observed_at_ms=timestamp,
            event_code="model_rerouted",
            model=to_model,
            role=None,
            status="observed",
            stop_reason=None,
            tool_digest=None,
            item_digest=keyed_digest(digest_key, f"{from_model}\x00{to_model}\x00{reason}"),
            coverage=coverage,
        )
    item = params.get("item")
    if not isinstance(item, dict):
        raise ContractError("item completion notification is invalid")
    item_id = _string(item, "id")
    item_type = _string(item, "type")
    assert item_id and item_type
    item_status = item.get("status")
    status = item_status if item_status in {"completed", "failed"} else "observed"
    latency = item.get("durationMs")
    usage = empty_usage()
    if isinstance(latency, int) and not isinstance(latency, bool) and latency >= 0:
        usage["latency_ms"] = latency
    usage["tool_calls"] = 1 if item_type in {"commandExecution", "mcpToolCall", "dynamicToolCall"} else 0
    return _event(
        key=digest_key,
        identity=f"{method}\x00{thread}\x00{params.get('turnId')}\x00{item_id}\x00{item_type}\x00{status}",
        run_id=run_id,
        turn_id=turn_id,
        assignment_id=None,
        parent_run_id=None,
        observed_at_ms=timestamp,
        event_code="item_completed",
        model=None,
        role=None,
        status=status,
        stop_reason=None,
        tool_digest=keyed_digest(digest_key, item_type),
        item_digest=keyed_digest(digest_key, item_id),
        coverage=coverage,
        usage=usage,
    )


def load_client_capabilities(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("client capability matrix is invalid") from exc
    if not isinstance(data, dict) or set(data) != {"version", "clients"} or data["version"] != 1:
        raise ContractError("client capability matrix is invalid")
    clients = data["clients"]
    if not isinstance(clients, dict) or not clients:
        raise ContractError("client capability matrix is empty")
    for client, capability in clients.items():
        if not isinstance(client, str) or not client or not isinstance(capability, dict):
            raise ContractError("client capability entry is invalid")
        if set(capability) != CLIENT_CAPABILITY_FIELDS:
            raise ContractError("client capability entry has unknown or missing fields")
        if capability["hook_coverage"] not in COVERAGE_CLASSES:
            raise ContractError("client hook coverage is invalid")
        if capability["app_server_coverage"] not in COVERAGE_CLASSES:
            raise ContractError("client App Server coverage is invalid")
        producers = capability["producers"]
        if (
            not isinstance(producers, list)
            or not producers
            or producers != sorted(set(producers))
            or not set(producers).issubset({"hook", "app_server", "host"})
        ):
            raise ContractError("client producers are invalid")
        for field in CLIENT_CAPABILITY_FIELDS - {
            "producers", "hook_coverage", "app_server_coverage"
        }:
            if not isinstance(capability[field], bool):
                raise ContractError("client capability flag must be boolean")
    return clients


def reconcile_run(
    envelopes: list[dict[str, Any]], *, capability: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile trusted-source envelopes without selecting a favorable conflict."""

    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    terminal: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    conflict = False
    run_id: str | None = None
    for envelope in envelopes:
        if not isinstance(envelope, dict) or set(envelope) != {"source", "record"}:
            raise ContractError("event envelope is invalid")
        source = envelope["source"]
        if source not in {"hook", "app_server"}:
            raise ContractError("event source is invalid")
        record = validate_event(envelope["record"])
        expected_coverage = capability.get(
            "app_server_coverage" if source == "app_server" else "hook_coverage"
        )
        if record["coverage"] != expected_coverage:
            conflict = True
            continue
        if run_id is None:
            run_id = record["run_id"]
        elif run_id != record["run_id"]:
            raise ContractError("reconciliation contains multiple runs")
        existing = seen.get(record["record_id"])
        if existing:
            conflict = conflict or existing != (source, record)
            continue
        seen[record["record_id"]] = (source, record)
        if source == "app_server" and record["event_code"] in {
            "turn_completed",
            "turn_failed",
            "turn_interrupted",
        }:
            if terminal is not None and terminal["status"] != record["status"]:
                conflict = True
            terminal = record
        if source == "app_server" and record["event_code"] == "usage_snapshot":
            if usage is not None:
                for field in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                    previous = usage["usage"][field]
                    current = record["usage"][field]
                    if previous is not None and current is not None and current < previous:
                        conflict = True
            usage = record
    if conflict:
        coverage = "conflicting"
    elif terminal and usage and capability.get("app_server_coverage") == "end_to_end_complete":
        coverage = "end_to_end_complete"
    elif terminal and usage and capability.get("app_server_coverage") == "root_turn_complete":
        coverage = "root_turn_complete"
    elif seen:
        coverage = "partial"
    else:
        coverage = "unknown"
    return {
        "run_id": run_id,
        "coverage": coverage,
        "terminal_status": terminal["status"] if terminal else None,
        "terminal_stop_reason": terminal["stop_reason"] if terminal else None,
        "usage": usage["usage"] if usage else None,
        "eligible_for_efficiency": coverage == "end_to_end_complete",
        "conflicting": conflict,
        "record_count": len(seen),
    }
