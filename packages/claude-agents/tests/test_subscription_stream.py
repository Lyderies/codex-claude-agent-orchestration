from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from claude_agents.subscription_stream import parse_subscription_stream
from claude_agents.telemetry import TelemetryError
from tests.test_telemetry import MODEL, frames, stream


def event():
    return {"type": "rate_limit_event", "session_id": "session-1", "uuid": "event-1",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                "utilization": 0.25, "overageEnabled": False}}


class SubscriptionStreamTests(unittest.TestCase):
    def parse(self, control=None):
        base = frames()
        return parse_subscription_stream(stream([base[0], event() if control is None else control,
                                                 *base[1:]]), requested_model=MODEL)

    def test_passive_rate_observations_do_not_count_tokens_or_grant_authority(self):
        result = self.parse()
        self.assertEqual(result.no_tool.model_usage.tokens.input_tokens, 20)
        self.assertEqual(result.rate_limits[0].utilization, 0.25)
        self.assertIsNone(result.rate_limits[0].resets_at)
        self.assertFalse(result.no_tool.runtime_enabled)
        self.assertFalse(result.no_tool.billing_verified)
        with self.assertRaises(FrozenInstanceError):
            result.rate_limits = ()

    def test_original_base_profile_still_passes(self):
        result = parse_subscription_stream(stream(), requested_model=MODEL)
        self.assertEqual(result.rate_limits, ())

    def test_unknown_active_cross_session_or_child_frames_are_not_filtered(self):
        for change in ({"type": "hook_response"}, {"session_id": "other"},
                       {"parent_tool_use_id": "child"}, {"uuid": None}):
            data = event()
            data.update(change)
            with self.assertRaises(TelemetryError):
                self.parse(data)

    def test_credit_counterevidence_denies_instead_of_authorizing(self):
        for field in ("overageEnabled", "isUsingOverage", "overageInUse"):
            data = event()
            data["rate_limit_info"][field] = True
            with self.assertRaisesRegex(TelemetryError, "billing_counterevidence"):
                self.parse(data)

    def test_wrong_types_unknown_values_and_excess_controls_fail(self):
        for field, value in (("utilization", True), ("utilization", -1),
                             ("resetsAt", 10**13), ("overageEnabled", 0),
                             ("status", "unknown"), ("status", []),
                             ("status", {}), ("rateLimitType", "custom")):
            data = event()
            data["rate_limit_info"][field] = value
            with self.assertRaises(TelemetryError):
                self.parse(data)
        base = frames()
        with self.assertRaises(TelemetryError):
            parse_subscription_stream(stream([base[0], *[event() for _ in range(9)], *base[1:]]),
                                      requested_model=MODEL)

    def test_nonfinite_duplicates_and_missing_terminal_remain_denied(self):
        raw = stream([frames()[0], event(), *frames()[1:]])
        for broken in (raw.replace('"utilization": 0.25', '"utilization": NaN'),
                       raw.replace('"status": "allowed"', '"status": "allowed", "status": "allowed"'),
                       stream([frames()[0], event(), frames()[1]])):
            with self.assertRaises(TelemetryError):
                parse_subscription_stream(broken, requested_model=MODEL)
