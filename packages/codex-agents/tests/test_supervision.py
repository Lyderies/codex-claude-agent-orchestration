from __future__ import annotations

import unittest

from tests.support import ORCHESTRATION_ROOT  # noqa: F401

from codex_agents.supervision import StateError, new_state, transition


class SupervisionTests(unittest.TestCase):
    bounds = {"max_attempts": 2, "max_steers_per_attempt": 1, "max_no_progress_events": 2}

    def active(self) -> dict:
        return transition(new_state("assignment-1"), "start", self.bounds)

    def test_named_wait_and_resume(self) -> None:
        waiting = transition(
            self.active(), "wait", self.bounds, dependency="review assignment-2"
        )
        self.assertEqual("waiting", waiting["state"])
        self.assertEqual("review assignment-2", waiting["dependency"])
        resumed = transition(waiting, "resume", self.bounds)
        self.assertEqual("active", resumed["state"])

    def test_no_progress_becomes_stale_then_allows_one_steer(self) -> None:
        first = transition(self.active(), "no_progress", self.bounds)
        stale = transition(first, "no_progress", self.bounds)
        self.assertEqual("stale", stale["state"])
        steered = transition(stale, "steer", self.bounds)
        self.assertEqual("active", steered["state"])
        with self.assertRaisesRegex(StateError, "steer bound exhausted"):
            transition(steered, "steer", self.bounds)

    def test_interrupt_and_cancel_are_terminal(self) -> None:
        interrupted = transition(self.active(), "interrupt", self.bounds)
        self.assertEqual("interrupted", interrupted["state"])
        with self.assertRaisesRegex(StateError, "terminal"):
            transition(interrupted, "cancel", self.bounds)
        cancelled = transition(self.active(), "cancel", self.bounds)
        self.assertEqual("cancelled", cancelled["state"])

    def test_retry_is_bounded_and_requires_reason(self) -> None:
        failed = transition(self.active(), "fail", self.bounds)
        with self.assertRaisesRegex(StateError, "requires transient"):
            transition(failed, "retry", self.bounds)
        retried = transition(
            failed, "retry", self.bounds, retry_reason="corrected_input"
        )
        self.assertEqual(2, retried["attempt"])
        failed_again = transition(retried, "fail", self.bounds)
        with self.assertRaisesRegex(StateError, "attempt bound exhausted"):
            transition(
                failed_again, "retry", self.bounds, retry_reason="transient_failure"
            )

    def test_block_requires_named_dependency(self) -> None:
        with self.assertRaisesRegex(StateError, "named blocker"):
            transition(self.active(), "block", self.bounds)

    def test_observation_deadline_survives_pause_and_needs_host_terminal_fact(self) -> None:
        state = new_state(
            "assignment-1", observation_deadline_seconds=21_600, now_ms=1_000
        )
        state = transition(state, "start", self.bounds, now_ms=1_000)
        paused = transition(state, "pause", self.bounds, now_ms=2_000)
        resumed = transition(paused, "resume", self.bounds, now_ms=3_000)
        self.assertEqual(21_601_000, resumed["observation_deadline_at_ms"])
        observed = transition(
            resumed, "observe_deadline", self.bounds, now_ms=21_601_000
        )
        self.assertEqual("stale", observed["state"])
        self.assertTrue(observed["deadline_observed"])
        timed_out = transition(
            resumed,
            "observe_deadline",
            self.bounds,
            now_ms=21_601_000,
            authoritative_terminal_state="timed_out",
        )
        self.assertEqual("timed_out", timed_out["state"])


if __name__ == "__main__":
    unittest.main()
