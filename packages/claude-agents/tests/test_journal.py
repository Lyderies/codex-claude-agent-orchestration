from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from claude_agents.journal import FakeDispatchJournal, FakeJournalError


class FakeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / "fake.sqlite3"
        self.journal = FakeDispatchJournal(self.path)

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def admit(self, attempt="attempt-1", provider="fake_claude", job="job-1"):
        return self.journal.admit_fake(
            job, attempt, provider, quota_state="known_available", available=True
        )

    def fenced(self) -> None:
        with self.assertRaisesRegex(FakeJournalError, "job_fenced"):
            self.admit("attempt-2", "fake_codex")

    def test_before_send_uncertainty_refuses_without_creating_an_attempt(self) -> None:
        for quota, available in (("unknown", True), ("known_exhausted", True),
                                 ("known_available", False), ("known_available", 1)):
            with self.assertRaises(FakeJournalError):
                self.journal.admit_fake(
                    "job-1", "attempt-1", "fake_claude",
                    quota_state=quota, available=available,
                )
        for provider in ("claude", "codex", "api", True):
            with self.assertRaises(FakeJournalError):
                self.journal.admit_fake(
                    "job-1", "attempt-1", provider,
                    quota_state="known_available", available=True,
                )
        self.assertEqual(self.admit().state, "pre-send")

    def test_pre_send_requires_exact_no_send_fact_and_new_admission(self) -> None:
        first = self.admit()
        self.fenced()
        with self.assertRaisesRegex(FakeJournalError, "fake_fact_required"):
            self.journal.reconcile_fake(first, {"outcome": "not_sent"}, receipt_id="r")
        fact = self.journal.record_fake_transport_fact(first, "fact-1", "not_sent")
        self.fenced()
        closed = self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
        self.assertTrue(closed.reconciled)
        self.assertEqual(closed.revision, 1)
        self.assertEqual(self.admit("attempt-2", "fake_codex").state, "pre-send")

    def test_after_send_limits_mismatch_cancel_and_lost_terminal_keep_fence(self) -> None:
        first = self.journal.advance_fake(self.admit(), "sent")
        self.fenced()
        with self.assertRaisesRegex(FakeJournalError, "unproved_effects"):
            self.journal.record_fake_transport_fact(first, "bad-fact", "not_sent")
        acknowledged = self.journal.advance_fake(first, "acknowledged")
        self.fenced()
        for condition in ("limit_after_ack", "model_mismatch", "cancel",
                          "lost_terminal_evidence"):
            with self.subTest(condition=condition):
                if acknowledged.state != "ambiguous":
                    acknowledged = self.journal.advance_fake(acknowledged, "ambiguous")
                self.fenced()
                self.assertFalse(acknowledged.reconciled)
        terminal = self.journal.advance_fake(acknowledged, "terminal")
        self.fenced()
        fact = self.journal.record_fake_transport_fact(
            terminal, "terminal-fact", "terminal_effects_closed"
        )
        self.journal.reconcile_fake(terminal, fact, receipt_id="terminal-receipt")
        second = self.admit("attempt-2", "fake_codex")
        self.assertNotEqual(second.attempt_id, terminal.attempt_id)

    def test_ambiguous_restart_survives_and_stale_cas_is_denied(self) -> None:
        before = self.admit()
        sent = self.journal.advance_fake(before, "sent")
        ambiguous = self.journal.advance_fake(sent, "ambiguous")
        self.journal.close()
        self.journal = FakeDispatchJournal(self.path)
        self.assertEqual(self.journal.get_fake("attempt-1"), ambiguous)
        self.fenced()
        with self.assertRaisesRegex(FakeJournalError, "stale_attempt"):
            self.journal.advance_fake(before, "sent")
        with self.assertRaisesRegex(FakeJournalError, "stale_attempt"):
            self.journal.advance_fake(replace(ambiguous, provider="fake_codex"), "terminal")

    def test_wrong_stale_forged_and_replayed_reconciliation_are_denied(self) -> None:
        first = self.admit()
        fact = self.journal.record_fake_transport_fact(first, "fact-1", "not_sent")
        for changed in (replace(fact, fact_id="unknown"),
                        replace(fact, outcome="terminal_effects_closed"),
                        replace(fact, attempt=replace(first, provider="fake_codex")),
                        replace(fact, attempt=replace(first, revision=True))):
            with self.assertRaises(FakeJournalError):
                self.journal.reconcile_fake(first, changed, receipt_id="bad-receipt")
            self.fenced()
        self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
        with self.assertRaisesRegex(FakeJournalError, "receipt_replayed"):
            self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
        second = self.admit("attempt-2", "fake_codex")
        with self.assertRaises(FakeJournalError):
            self.journal.reconcile_fake(second, fact, receipt_id="new-receipt")
        with self.assertRaises(FakeJournalError):
            self.journal.advance_fake(replace(second, revision=True), "sent")

    def test_fact_cannot_be_reused_after_state_or_revision_changes(self) -> None:
        first = self.admit()
        fact = self.journal.record_fake_transport_fact(first, "fact-1", "not_sent")
        sent = self.journal.advance_fake(first, "sent")
        with self.assertRaisesRegex(FakeJournalError, "fact_mismatch"):
            self.journal.reconcile_fake(sent, fact, receipt_id="receipt-1")
        with self.assertRaisesRegex(FakeJournalError, "stale_attempt"):
            self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
        self.fenced()

    def test_concurrent_reconciliation_consumes_exactly_one_receipt(self) -> None:
        first = self.admit()
        fact = self.journal.record_fake_transport_fact(first, "fact-1", "not_sent")
        barrier = threading.Barrier(2)
        outcomes = []
        def contender(receipt):
            journal = FakeDispatchJournal(self.path)
            try:
                barrier.wait(timeout=5)
                journal.reconcile_fake(first, fact, receipt_id=receipt)
                outcomes.append("reconciled")
            except FakeJournalError as exc:
                outcomes.append(str(exc))
            finally:
                journal.close()
        threads = [threading.Thread(target=contender, args=(receipt,))
                   for receipt in ("receipt-1", "receipt-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["reconciled", "stale_attempt"])
        self.admit("attempt-2", "fake_codex")

    def test_failed_reconciliation_rolls_back_receipt_and_fence_release(self) -> None:
        first = self.admit()
        fact = self.journal.record_fake_transport_fact(first, "fact-1", "not_sent")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "CREATE TRIGGER abort_update BEFORE UPDATE ON fake_attempts "
                "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
            )
            connection.commit()
            with self.assertRaisesRegex(FakeJournalError, "storage_failure"):
                self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
            self.assertEqual(self.journal.get_fake("attempt-1"), first)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM fake_reconciliations"
            ).fetchone()[0], 0)
            connection.execute("DROP TRIGGER abort_update")
            connection.commit()
        finally:
            connection.close()
        self.journal.reconcile_fake(first, fact, receipt_id="receipt-1")
        self.admit("attempt-2", "fake_codex")

    def test_two_connections_cannot_admit_duplicate_logical_dispatch(self) -> None:
        barrier = threading.Barrier(2)
        outcomes = []
        def contender(attempt, provider):
            journal = FakeDispatchJournal(self.path)
            try:
                barrier.wait(timeout=5)
                journal.admit_fake("job-1", attempt, provider,
                                   quota_state="known_available", available=True)
                outcomes.append("admitted")
            except FakeJournalError as exc:
                outcomes.append(str(exc))
            finally:
                journal.close()
        threads = [
            threading.Thread(target=contender, args=("a", "fake_claude")),
            threading.Thread(target=contender, args=("b", "fake_codex")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["admitted", "job_fenced"])

    def test_ids_schema_and_illegal_transitions_fail_without_content_leaks(self) -> None:
        for job in ("../escape", "SECRET VALUE", "x" * 65, True):
            with self.assertRaises(FakeJournalError) as caught:
                self.admit(job=job)
            self.assertEqual(str(caught.exception), "invalid_id")
        first = self.admit()
        with self.assertRaisesRegex(FakeJournalError, "invalid_transition"):
            self.journal.advance_fake(first, "acknowledged")
        self.assertEqual(self.journal.get_fake(first.attempt_id), first)
        other_path = Path(self.temp.name).resolve() / "other.sqlite3"
        connection = sqlite3.connect(other_path)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.close()
        with self.assertRaisesRegex(FakeJournalError, "not_fake_database"):
            FakeDispatchJournal(other_path)

    def test_restart_rejects_lax_schema_without_changing_untrusted_rows(self) -> None:
        path = Path(self.temp.name).resolve() / "lax.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE fake_meta (id INTEGER PRIMARY KEY, version INTEGER)")
        connection.execute("INSERT INTO fake_meta VALUES (1,1)")
        connection.execute(
            "CREATE TABLE fake_attempts (job_id TEXT, attempt_id TEXT PRIMARY KEY, "
            "provider TEXT, state TEXT, revision INTEGER, reconciled)"
        )
        connection.execute(
            "INSERT INTO fake_attempts VALUES ('job-1','old','fake_claude','sent',1,'unknown')"
        )
        connection.commit()
        with self.assertRaisesRegex(FakeJournalError, "unsupported_schema"):
            FakeDispatchJournal(path)
        self.assertEqual(connection.execute(
            "SELECT reconciled FROM fake_attempts WHERE attempt_id='old'"
        ).fetchone(), ("unknown",))
        connection.close()

    def test_corrupt_reconciliation_never_releases_fence_live_or_after_restart(self) -> None:
        for corrupt in ("unknown", 1):
            with self.subTest(corrupt=corrupt):
                path = Path(self.temp.name).resolve() / f"corrupt-{corrupt}.sqlite3"
                journal = FakeDispatchJournal(path)
                first = journal.admit_fake("job-1", "old", "fake_claude",
                                           quota_state="known_available", available=True)
                journal.advance_fake(first, "sent")
                connection = sqlite3.connect(path)
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute("UPDATE fake_attempts SET reconciled=?", (corrupt,))
                connection.commit()
                connection.close()
                with self.assertRaisesRegex(FakeJournalError, "invalid_storage"):
                    journal.admit_fake("job-1", "new", "fake_codex",
                                       quota_state="known_available", available=True)
                journal.close()
                with self.assertRaisesRegex(FakeJournalError, "invalid_storage"):
                    FakeDispatchJournal(path)
