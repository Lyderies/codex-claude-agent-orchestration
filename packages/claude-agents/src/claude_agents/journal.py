"""Durable FAKE dispatch fencing; never use as a live provider authority.

The host chooses a private database path. Facts and receipts are synthetic host
records only, not authenticated provider evidence or a model-output protocol.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PROVIDERS = frozenset({"fake_claude", "fake_codex"})
_STATES = frozenset({"pre-send", "sent", "acknowledged", "terminal", "ambiguous"})
_TRANSITIONS = {
    "pre-send": {"sent", "ambiguous"},
    "sent": {"acknowledged", "terminal", "ambiguous"},
    "acknowledged": {"terminal", "ambiguous"},
    "terminal": {"ambiguous"},
    "ambiguous": {"terminal"},
}


class FakeJournalError(ValueError):
    """Bounded reason code without database paths, SQL or untrusted content."""


def _fail(code: str) -> None:
    raise FakeJournalError(code)


def _identifier(value: object) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("invalid_id")
    return value


@dataclass(frozen=True, slots=True)
class FakeAttempt:
    job_id: str
    attempt_id: str
    provider: str
    state: str
    revision: int
    reconciled: bool


@dataclass(frozen=True, slots=True)
class FakeTransportFact:
    fact_id: str
    attempt: FakeAttempt
    outcome: str


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fake_meta "
    "(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL CHECK(version=1))",
    """CREATE TABLE IF NOT EXISTS fake_attempts (
        job_id TEXT NOT NULL CHECK(length(job_id) BETWEEN 1 AND 64),
        attempt_id TEXT PRIMARY KEY NOT NULL CHECK(length(attempt_id) BETWEEN 1 AND 64),
        provider TEXT NOT NULL CHECK(provider IN ('fake_claude','fake_codex')),
        state TEXT NOT NULL CHECK(state IN
            ('pre-send','sent','acknowledged','terminal','ambiguous')),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer'
            AND revision BETWEEN 0 AND 1024),
        reconciled INTEGER NOT NULL CHECK(reconciled IN (0,1))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS fake_one_unresolved "
    "ON fake_attempts(job_id) WHERE reconciled=0",
    """CREATE TABLE IF NOT EXISTS fake_transport_facts (
        fact_id TEXT PRIMARY KEY NOT NULL CHECK(length(fact_id) BETWEEN 1 AND 64),
        job_id TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES fake_attempts,
        provider TEXT NOT NULL CHECK(provider IN ('fake_claude','fake_codex')),
        state TEXT NOT NULL CHECK(state IN ('pre-send','terminal')),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer'
            AND revision BETWEEN 0 AND 1024),
        outcome TEXT NOT NULL CHECK(
            (state='pre-send' AND outcome='not_sent') OR
            (state='terminal' AND outcome='terminal_effects_closed')),
        UNIQUE(attempt_id, revision, outcome)
    )""",
    """CREATE TABLE IF NOT EXISTS fake_reconciliations (
        receipt_id TEXT PRIMARY KEY NOT NULL CHECK(length(receipt_id) BETWEEN 1 AND 64),
        fact_id TEXT NOT NULL UNIQUE REFERENCES fake_transport_facts,
        job_id TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES fake_attempts,
        provider TEXT NOT NULL CHECK(provider IN ('fake_claude','fake_codex')),
        state TEXT NOT NULL CHECK(state IN ('pre-send','terminal')),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer'
            AND revision BETWEEN 0 AND 1024),
        outcome TEXT NOT NULL CHECK(
            (state='pre-send' AND outcome='not_sent') OR
            (state='terminal' AND outcome='terminal_effects_closed'))
    )""",
)


class FakeDispatchJournal:
    """Fake-only persistence with one unresolved attempt per logical job.

    Every admitted attempt fences its Job until reconcile_fake records an exact,
    unused transport fact and receipt in the same transaction as fence release.
    State transitions, including terminal, do not themselves release that fence.
    """

    def __init__(self, host_db_path: Path) -> None:
        if not isinstance(host_db_path, Path) or not host_db_path.is_absolute():
            _fail("invalid_host_path")
        if host_db_path.is_symlink():
            _fail("invalid_host_path")
        try:
            self._db = sqlite3.connect(host_db_path, timeout=1, isolation_level=None)
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                tables = {r[0] for r in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if tables - {"fake_meta", "fake_attempts", "fake_transport_facts",
                             "fake_reconciliations"}:
                    _fail("not_fake_database")
                for statement in _SCHEMA:
                    self._db.execute(statement)
                reference = sqlite3.connect(":memory:")
                try:
                    for statement in _SCHEMA:
                        reference.execute(statement)
                    query = ("SELECT type,name,tbl_name,sql FROM sqlite_master "
                             "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
                    if self._db.execute(query).fetchall() != reference.execute(query).fetchall():
                        _fail("unsupported_schema")
                finally:
                    reference.close()
                self._db.execute("INSERT OR IGNORE INTO fake_meta VALUES (1,1)")
                if self._db.execute("SELECT id,version FROM fake_meta").fetchall() != [(1, 1)]:
                    _fail("unsupported_schema")
                if (self._db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                        or self._db.execute("PRAGMA foreign_key_check").fetchall()):
                    _fail("invalid_storage")
                for row in self._db.execute("SELECT attempt_id FROM fake_attempts").fetchall():
                    self._read(row[0])
        except (sqlite3.Error, OSError, FakeJournalError) as exc:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(exc, FakeJournalError):
                raise
            raise FakeJournalError("storage_failure") from None

    @contextmanager
    def _transaction(self):
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except BaseException as exc:
            try:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise FakeJournalError("storage_failure") from None
            raise

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _validate_snapshot(expected: FakeAttempt) -> None:
        if type(expected) is not FakeAttempt:
            _fail("invalid_snapshot")
        _identifier(expected.job_id)
        _identifier(expected.attempt_id)
        if (type(expected.provider) is not str or expected.provider not in _PROVIDERS
                or type(expected.state) is not str or expected.state not in _STATES
                or type(expected.revision) is not int or not 0 <= expected.revision <= 1024
                or type(expected.reconciled) is not bool):
            _fail("invalid_snapshot")

    def _read(self, attempt_id: str) -> FakeAttempt:
        row = self._db.execute(
            "SELECT job_id,attempt_id,provider,state,revision,reconciled "
            "FROM fake_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            _fail("unknown_attempt")
        if type(row[-1]) is not int or row[-1] not in (0, 1):
            _fail("invalid_storage")
        result = FakeAttempt(*row[:-1], row[-1] == 1)
        self._validate_snapshot(result)
        receipts = self._db.execute(
            "SELECT r.job_id,r.attempt_id,r.provider,r.state,r.revision,r.outcome,"
            "f.job_id,f.attempt_id,f.provider,f.state,f.revision,f.outcome "
            "FROM fake_reconciliations r JOIN fake_transport_facts f ON f.fact_id=r.fact_id "
            "WHERE r.attempt_id=?", (attempt_id,)
        ).fetchall()
        if result.reconciled:
            outcome = {"pre-send": "not_sent", "terminal": "terminal_effects_closed"}.get(
                result.state
            )
            key = (result.job_id, result.attempt_id, result.provider, result.state,
                   result.revision - 1, outcome)
            if outcome is None or receipts != [(*key, *key)]:
                _fail("invalid_storage")
        elif receipts:
            _fail("invalid_storage")
        return result

    def _expect(self, expected: FakeAttempt) -> None:
        self._validate_snapshot(expected)
        if self._read(expected.attempt_id) != expected or expected.reconciled:
            _fail("stale_attempt")

    @staticmethod
    def _key(expected: FakeAttempt) -> tuple:
        return (expected.job_id, expected.attempt_id, expected.provider,
                expected.state, expected.revision)

    def get_fake(self, attempt_id: str) -> FakeAttempt:
        _identifier(attempt_id)
        with self._transaction():
            return self._read(attempt_id)

    def admit_fake(self, job_id: str, attempt_id: str, provider: str, *,
                   quota_state: str, available: bool) -> FakeAttempt:
        _identifier(job_id)
        _identifier(attempt_id)
        if type(provider) is not str or provider not in _PROVIDERS:
            _fail("fake_provider_required")
        if type(available) is not bool or not available:
            _fail("provider_unavailable")
        if type(quota_state) is not str or quota_state != "known_available":
            _fail("quota_unknown_or_exhausted")
        with self._transaction():
            for row in self._db.execute(
                "SELECT attempt_id FROM fake_attempts WHERE job_id=?", (job_id,)
            ).fetchall():
                self._read(row[0])
            if self._db.execute(
                "SELECT 1 FROM fake_attempts WHERE job_id=? AND reconciled=0", (job_id,)
            ).fetchone():
                _fail("job_fenced")
            if self._db.execute(
                "SELECT 1 FROM fake_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                _fail("attempt_reused")
            if self._db.execute(
                "SELECT count(*) FROM fake_attempts WHERE job_id=?", (job_id,)
            ).fetchone()[0] >= 16:
                _fail("attempt_bound")
            self._db.execute(
                "INSERT INTO fake_attempts VALUES (?,?,?,'pre-send',0,0)",
                (job_id, attempt_id, provider),
            )
            return self._read(attempt_id)

    def advance_fake(self, expected: FakeAttempt, new_state: str) -> FakeAttempt:
        with self._transaction():
            self._expect(expected)
            if (type(new_state) is not str
                    or new_state not in _TRANSITIONS[expected.state]
                    or expected.revision >= 1024):
                _fail("invalid_transition")
            changed = self._db.execute(
                "UPDATE fake_attempts SET state=?,revision=revision+1 "
                "WHERE job_id=? AND attempt_id=? AND provider=? AND state=? "
                "AND revision=? AND reconciled=0", (new_state, *self._key(expected))
            ).rowcount
            if changed != 1:
                _fail("stale_attempt")
            return self._read(expected.attempt_id)

    def record_fake_transport_fact(self, expected: FakeAttempt, fact_id: str,
                                   outcome: str) -> FakeTransportFact:
        _identifier(fact_id)
        with self._transaction():
            self._expect(expected)
            if (type(outcome) is not str or (expected.state, outcome) not in {
                ("pre-send", "not_sent"), ("terminal", "terminal_effects_closed")
            }):
                _fail("unproved_effects")
            self._db.execute(
                "INSERT INTO fake_transport_facts VALUES (?,?,?,?,?,?,?)",
                (fact_id, *self._key(expected), outcome),
            )
            return FakeTransportFact(fact_id, expected, outcome)

    def reconcile_fake(self, expected: FakeAttempt, fact: FakeTransportFact, *,
                       receipt_id: str) -> FakeAttempt:
        _identifier(receipt_id)
        if type(fact) is not FakeTransportFact:
            _fail("fake_fact_required")
        _identifier(fact.fact_id)
        self._validate_snapshot(fact.attempt)
        if type(fact.outcome) is not str:
            _fail("fake_fact_required")
        with self._transaction():
            if self._db.execute(
                "SELECT 1 FROM fake_reconciliations WHERE receipt_id=?", (receipt_id,)
            ).fetchone():
                _fail("receipt_replayed")
            self._expect(expected)
            stored = self._db.execute(
                "SELECT fact_id,job_id,attempt_id,provider,state,revision,outcome "
                "FROM fake_transport_facts WHERE fact_id=?", (fact.fact_id,)
            ).fetchone()
            exact = (fact.fact_id, *self._key(expected), fact.outcome)
            if fact.attempt != expected or stored != exact or expected.revision >= 1024:
                _fail("fact_mismatch")
            self._db.execute(
                "INSERT INTO fake_reconciliations VALUES (?,?,?,?,?,?,?,?)",
                (receipt_id, *exact),
            )
            changed = self._db.execute(
                "UPDATE fake_attempts SET reconciled=1,revision=revision+1 "
                "WHERE job_id=? AND attempt_id=? AND provider=? AND state=? "
                "AND revision=? AND reconciled=0", self._key(expected)
            ).rowcount
            if changed != 1:
                _fail("stale_attempt")
            return self._read(expected.attempt_id)
