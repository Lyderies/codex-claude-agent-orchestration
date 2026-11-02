"""Deterministic state transitions for native subagent supervision records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


STATES = {
    "pending",
    "active",
    "waiting",
    "blocked",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "stale",
    "paused",
    "timed_out",
}
TERMINAL = {"completed", "failed", "timed_out", "interrupted", "cancelled"}


class StateError(ValueError):
    """Raised when an event is invalid for the observed state or configured bounds."""


def new_state(
    assignment_id: str,
    *,
    observation_deadline_seconds: int | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if not assignment_id.strip():
        raise StateError("assignment_id must be non-empty")
    if observation_deadline_seconds is not None and (
        not isinstance(observation_deadline_seconds, int)
        or isinstance(observation_deadline_seconds, bool)
        or observation_deadline_seconds < 1
    ):
        raise StateError("observation deadline must be a positive integer")
    if now_ms is not None and (
        not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0
    ):
        raise StateError("observation time must be a non-negative integer")
    return {
        "assignment_id": assignment_id,
        "state": "pending",
        "attempt": 1,
        "steers": 0,
        "no_progress_events": 0,
        "last_event": "created",
        "dependency": None,
        "observation_started_at_ms": now_ms,
        "last_progress_at_ms": now_ms,
        "observation_deadline_at_ms": (
            None
            if observation_deadline_seconds is None or now_ms is None
            else now_ms + observation_deadline_seconds * 1000
        ),
        "deadline_observed": False,
    }


def transition(
    current: dict[str, Any],
    event: str,
    bounds: dict[str, int],
    *,
    dependency: str | None = None,
    retry_reason: str | None = None,
    now_ms: int | None = None,
    authoritative_terminal_state: str | None = None,
) -> dict[str, Any]:
    state = current.get("state")
    if state not in STATES:
        raise StateError(f"unknown state: {state}")
    result = deepcopy(current)
    if now_ms is not None and (
        not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0
    ):
        raise StateError("observation time must be a non-negative integer")

    if event == "start":
        if state != "pending":
            raise StateError("start requires pending state")
        result.update(state="active", last_event=event)
    elif event == "progress":
        if state not in {"active", "stale"}:
            raise StateError("progress requires active or stale state")
        result.update(
            state="active",
            no_progress_events=0,
            last_event=event,
            last_progress_at_ms=now_ms if now_ms is not None else result.get("last_progress_at_ms"),
        )
    elif event == "wait":
        if state != "active" or not dependency:
            raise StateError("wait requires active state and a named dependency")
        result.update(state="waiting", dependency=dependency, last_event=event)
    elif event == "resume":
        if state not in {"waiting", "paused"}:
            raise StateError("resume requires waiting or paused state")
        result.update(state="active", dependency=None, last_event=event)
    elif event == "pause":
        if state not in {"active", "waiting", "stale"}:
            raise StateError("pause requires a live state")
        result.update(state="paused", dependency=dependency, last_event=event)
    elif event == "observe_deadline":
        deadline = result.get("observation_deadline_at_ms")
        if state not in {"active", "waiting", "stale", "paused"}:
            raise StateError("deadline observation requires a live state")
        if deadline is None or now_ms is None or now_ms < deadline:
            raise StateError("observation deadline has not elapsed")
        result.update(deadline_observed=True, last_event=event)
        if authoritative_terminal_state is None:
            result.update(state="stale")
        elif authoritative_terminal_state == "timed_out":
            result.update(state="timed_out", dependency=None)
        elif authoritative_terminal_state in TERMINAL:
            result.update(state=authoritative_terminal_state, dependency=None)
        else:
            raise StateError("authoritative terminal state is unsupported")
    elif event == "block":
        if state not in {"active", "waiting", "stale"} or not dependency:
            raise StateError("block requires a live state and named blocker")
        result.update(state="blocked", dependency=dependency, last_event=event)
    elif event == "no_progress":
        if state not in {"active", "stale"}:
            raise StateError("no_progress requires active or stale state")
        count = result["no_progress_events"] + 1
        next_state = "stale" if count >= bounds["max_no_progress_events"] else "active"
        result.update(state=next_state, no_progress_events=count, last_event=event)
    elif event == "steer":
        if state not in {"active", "waiting", "stale"}:
            raise StateError("steer requires a live state")
        if result["steers"] >= bounds["max_steers_per_attempt"]:
            raise StateError("steer bound exhausted")
        result.update(
            state="active",
            steers=result["steers"] + 1,
            no_progress_events=0,
            dependency=None,
            last_event=event,
        )
    elif event in {"complete", "fail", "interrupt", "cancel"}:
        if state in TERMINAL:
            raise StateError("terminal state cannot transition again")
        mapped = {
            "complete": "completed",
            "fail": "failed",
            "interrupt": "interrupted",
            "cancel": "cancelled",
        }
        result.update(state=mapped[event], dependency=None, last_event=event)
    elif event == "retry":
        if state not in {"failed", "interrupted", "stale"}:
            raise StateError("retry requires failed, interrupted, or stale state")
        if retry_reason not in {"transient_failure", "corrected_input"}:
            raise StateError("retry requires transient_failure or corrected_input")
        if result["attempt"] >= bounds["max_attempts"]:
            raise StateError("attempt bound exhausted")
        result.update(
            state="active",
            attempt=result["attempt"] + 1,
            steers=0,
            no_progress_events=0,
            dependency=None,
            last_event=event,
        )
    else:
        raise StateError(f"unsupported event: {event}")
    return result
