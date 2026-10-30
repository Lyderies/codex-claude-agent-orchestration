"""Observed subscription control events around the strict no-tool CLI profile.

Protocol source: official @anthropic-ai/claude-agent-sdk 0.3.310 sdk.d.ts.
Passive events are observations, never authentication, credit or execution grants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .telemetry import NoToolObservation, TelemetryError, _frame, parse_no_tool_stream

_STATUSES = {"allowed", "allowed_warning", "rejected"}
_TYPES = {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
          "seven_day_overage_included", "overage"}
_REASONS = {"overage_not_provisioned", "org_level_disabled", "org_level_disabled_until",
            "out_of_credits", "seat_tier_level_disabled", "member_level_disabled",
            "seat_tier_zero_credit_limit", "group_zero_credit_limit", "member_zero_credit_limit",
            "org_service_level_disabled", "no_limits_configured", "fetch_error", "unknown"}


def _optional_choice(value: object, allowed: set) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        raise TelemetryError("unsupported_rate_limit_value")
    return value


def _number(value: object, maximum: int) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= maximum:
        raise TelemetryError("invalid_rate_limit_number")
    return value


def _boolean(value: object) -> bool | None:
    if value is not None and type(value) is not bool:
        raise TelemetryError("invalid_rate_limit_boolean")
    return value


@dataclass(frozen=True, slots=True)
class RateLimitObservation:
    status: str
    rate_limit_type: str | None
    resets_at: int | float | None
    utilization: int | float | None
    overage_status: str | None
    overage_enabled: bool | None
    is_using_overage: bool | None
    overage_in_use: bool | None
    overage_disabled_reason: str | None


@dataclass(frozen=True, slots=True)
class SubscriptionStreamObservation:
    no_tool: NoToolObservation
    rate_limits: tuple[RateLimitObservation, ...]
    original_stream_sha256: str


def parse_subscription_stream(raw: str, *, requested_model: str) -> SubscriptionStreamObservation:
    """Allow only documented, bounded passive root rate events beyond the base stream."""
    if type(raw) is not str or len(raw) > 262144:
        raise TelemetryError("stream_size")
    try:
        encoded = raw.encode()
    except UnicodeError:
        raise TelemetryError("malformed_json") from None
    if len(encoded) > 262144:
        raise TelemetryError("stream_size")
    lines = raw.splitlines()
    if not 3 <= len(lines) <= 11:
        raise TelemetryError("missing_or_extra_frames")
    frames = [_frame(line) for line in lines]
    if frames[0].get("type") != "system" or frames[0].get("subtype") != "init":
        raise TelemetryError("missing_init")
    session = frames[0].get("session_id")
    base, rates = [], []
    for frame in frames:
        if frame.get("type") != "rate_limit_event":
            base.append(frame)
            continue
        if (set(frame) != {"type", "rate_limit_info", "uuid", "session_id"}
                or frame.get("session_id") != session
                or type(frame.get("uuid")) is not str or not 1 <= len(frame["uuid"]) <= 128):
            raise TelemetryError("invalid_rate_limit_frame")
        info = frame["rate_limit_info"]
        if (type(info) is not dict or type(info.get("status")) is not str
                or info["status"] not in _STATUSES):
            raise TelemetryError("invalid_rate_limit_info")
        # These observations can only deny; they never authorize credit use.
        enabled = _boolean(info.get("overageEnabled"))
        using = _boolean(info.get("isUsingOverage"))
        in_use = _boolean(info.get("overageInUse"))
        if enabled is True or using is True or in_use is True:
            raise TelemetryError("billing_counterevidence")
        rates.append(RateLimitObservation(
            info["status"], _optional_choice(info.get("rateLimitType"), _TYPES),
            _number(info.get("resetsAt"), 10**12), _number(info.get("utilization"), 100),
            _optional_choice(info.get("overageStatus"), _STATUSES), enabled, using, in_use,
            _optional_choice(info.get("overageDisabledReason"), _REASONS),
        ))
    canonical = "\n".join(json.dumps(frame) for frame in base) + "\n"
    observation = parse_no_tool_stream(canonical, requested_model=requested_model)
    return SubscriptionStreamObservation(observation, tuple(rates), hashlib.sha256(encoded).hexdigest())
