from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, REPOSITORY_ROOT, TEMP_ROOT

from codex_agents.contracts import routing_task_slice

from codex_agents.learning_store import (
    CollectorClient,
    CollectorBuildConflictError,
    CollectorError,
    CollectorMaintenance,
    CollectorNewerVersionError,
    CollectorUpgradeRequired,
    CollectorServer,
    DEFAULT_RETENTION_MS,
    INGESTION_FRESH_MS,
    HOST_RUN_TTL_MS,
    COLLECTOR_RUNTIME_DIGEST,
    PACKAGE_VERSION,
    LearningLedger,
    LearningState,
    empty_ingestion_snapshot,
    LedgerError,
    REPAIR_RECEIPT_TTL_MS,
    StateError,
    authorize_collector_runtime,
    authorized_collector_runtime,
    client_health_clear,
    client_health_increment,
    client_health_snapshot,
    ensure_collector,
    _snapshot_project_partition,
    _start_collector_locked,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import (
    candidate_base_digest,
    canonical_json,
    content_digest,
    external_id,
    keyed_digest,
    validate_learning_record,
)
from codex_agents.learning_engine import (
    CandidateLifecycle,
    OwnerGrantAuthority,
    build_feedback,
    compare_candidate,
    generate_candidate,
    mine_evaluation_case,
    review_evaluation_case,
)
from tests.learning_support import (
    admit_provider,
    fake_provider_tools,
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    END_TO_END_CAPABILITY,
    FIXTURE_CLIENT_VERSION,
    assignment,
    candidate,
    evaluation_assignment,
    eligibility_receipt,
    event_record,
    generate_test_owner_key,
    heldout_cases,
    full_usage,
    champion_configuration,
    matched_trials,
    result,
    routing_fields,
    sign_owner_grant,
    sign_owner_payload,
    trial_outcome,
    verification_outcome,
)


class LearningStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        self.project_a = base / "project-a"
        self.project_b = base / "project-b"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.state = LearningState(self.state_root)
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, owner_public_key = generate_test_owner_key(base)
        self.state.install_owner_public_key(owner_public_key)
        clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        clients[FIXTURE_CLIENT_VERSION] = END_TO_END_CAPABILITY
        self.clients = clients
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.project_a_id = self.ledger.register_project(self.project_a, now_ms=1)
        self.project_b_id = self.ledger.register_project(self.project_b, now_ms=1)

    def _admit(self, project_id: str, *observed_at_ms: int) -> None:
        """Give a project a fresh identity and an owner attestation for admission.

        Tests use both small fixed clocks and the real clock, so the identity is
        observed at each clock the test needs.
        """

        tools = Path(self.temporary.name) / "fake-tools"
        if not tools.exists():
            fake_provider_tools(tools)
        for value in observed_at_ms or (1, time.time_ns() // 1_000_000):
            admit_provider(
                self.ledger, project_id, self.owner_private_key,
                tools=tools, observed_at_ms=value,
            )

    def _installed_bundle(self, name: str, *, version: str = PACKAGE_VERSION) -> Path:
        bundle = Path(self.temporary.name) / name
        shutil.copytree(
            REPOSITORY_ROOT / "native-package",
            bundle,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        files = {}
        for path in sorted(bundle.rglob("*")):
            if not path.is_file() or path == bundle / "VERSION":
                continue
            relative = path.relative_to(bundle).as_posix()
            if relative == ".codex/orchestration/install-manifest.json":
                continue
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = bundle / ".codex" / "orchestration" / "install-manifest.json"
        manifest.write_text(
            json.dumps(
                {"format": 1, "version": version, "files": files},
                sort_keys=True,
            )
        )
        os.chmod(manifest, 0o600)
        return bundle

    def _admit_outcome(
        self, project_id: str, record: dict, *, now_ms: int, producer: str = "evaluator"
    ) -> None:
        event = event_record(f"evt_{record['record_id']}", observed_at_ms=now_ms)
        event["run_id"] = record["run_id"]
        event["assignment_id"] = record["assignment_id"]
        event["model"] = record["model"]
        event["effort"] = record["effort"]
        event["role"] = record["role"]
        event["task_slice"] = record["task_slice"]
        event["topology"] = record["topology"]
        event["configuration_digest"] = record["configuration_digest"]
        event["coverage"] = record["coverage"]
        event["usage"] = record["usage"]
        event["event_code"] = "item_completed"
        event["status"] = "completed"
        event["item_digest"] = record["evidence_digests"][0]
        with self.ledger._lock, self.ledger._connection:
            self.ledger._append_checked_locked(
                project_id, event, producer="host", timestamp=now_ms
            )
            self.ledger._validate_evidence_links_locked(project_id, record)
            self.ledger._append_checked_locked(
                project_id, record, producer=producer, timestamp=now_ms
            )

    def _admit_reviewed_cases(self, project_id: str, cases: list[dict]) -> None:
        for index, reviewed in enumerate(cases):
            source = verification_outcome(reviewed["source_outcome_id"])
            source.update(
                **champion_configuration(),
                task_slice=reviewed["task_slice"],
                fixture_digest=reviewed["fixture_digest"],
            )
            source["configuration_digest"] = content_digest(
                champion_configuration()
            )
            self._admit_outcome(project_id, source, now_ms=30 + index)
            development = deepcopy(reviewed)
            development.update(
                record_id=reviewed["supersedes_case_id"],
                supersedes_case_id=None,
                corpus_state="development",
                similarity_group_digest=None,
                reviewed=False,
                review_digest=None,
                frozen=False,
            )
            self.ledger.append(project_id, development, producer="evaluator", now_ms=30 + index)
            self.ledger.append(project_id, reviewed, producer="evaluator", now_ms=30 + index)

    def _admit_evidence_review(
        self, project_id: str, subjects: list[str], *, now_ms: int
    ) -> dict:
        material = {
            "schema_version": 1,
            "record_type": "evidence_review",
            "subject_digests": sorted(subjects),
            "accepted": True,
            "observed_at_ms": now_ms,
        }
        record = validate_learning_record(
            {
                **material,
                "record_id": f"evidence_review:{content_digest(material)}",
            }
        )
        with self.ledger._lock, self.ledger._connection:
            self.ledger._append_checked_locked(
                project_id, record, producer="verifier", timestamp=now_ms
            )
        return record

    def _candidate_cohort(
        self,
        project_id: str,
        *,
        proposed: dict | None = None,
        accepted_qa: bool = True,
        now_ms: int = 100,
    ) -> dict:
        """Admit one synthetic 20-fixture cohort through the internal test seam."""

        template = proposed or candidate()
        evidence: list[str] = []
        for index in range(20):
            fixture_digest = f"{index + 1000:064x}"
            for cohort_name, configuration in (
                ("champion", template["champion_configuration"]),
                ("candidate", template["candidate_configuration"]),
            ):
                identifier = f"cohort-{now_ms}-{index}-{cohort_name}"
                qa_digest = content_digest(
                    {"fixture": fixture_digest, "cohort": cohort_name, "at": now_ms}
                )
                outcome = verification_outcome(identifier)
                outcome.update(
                    **configuration,
                    task_slice=template["task_slice"],
                    fixture_digest=fixture_digest,
                    observed_at_ms=now_ms + index,
                    evidence_digests=[DIGEST_A, qa_digest],
                )
                outcome["configuration_digest"] = content_digest(configuration)
                self._admit_outcome(
                    project_id, outcome, now_ms=now_ms + index, producer="verifier"
                )
                if accepted_qa:
                    with self.ledger._connection:
                        self.ledger._connection.execute(
                            "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                            (
                                project_id,
                                f"qa:{identifier}",
                                qa_digest,
                                content_digest(outcome),
                                DIGEST_B,
                                1,
                                now_ms + index,
                                canonical_json({"accepted": True}),
                            ),
                        )
                evidence.append(content_digest(outcome))
        return generate_candidate(
            digest_key=self.state.digest_key(),
            scope=template["scope"],
            task_slice=template["task_slice"],
            target=template["target"],
            effect=template["effect"],
            base_digest=template["base_digest"],
            champion_configuration=template["champion_configuration"],
            change_manifest=template["change_manifest"],
            evidence_digests=evidence,
            eligibility_receipt=eligibility_receipt(
                template["task_slice"], evidence
            ),
        )

    def _accept_outcome_qa(
        self, project_id: str, outcome: dict, qa_digest: str, *, now_ms: int
    ) -> None:
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    f"qa:{outcome['record_id']}",
                    qa_digest,
                    content_digest(outcome),
                    DIGEST_B,
                    1,
                    now_ms,
                    canonical_json({"accepted": True}),
                ),
            )

    def _downgrade_fixture_to_schema_five(self) -> None:
        with self.ledger._connection:
            self.ledger._connection.execute("DROP TABLE orchestration_checkpoints")
            for column in (
                "deadline_observed",
                "last_progress_at_ms",
                "observation_deadline_at_ms",
                "observation_started_at_ms",
                "abandoned_reason",
                "binding_state",
            ):
                self.ledger._connection.execute(
                    f"ALTER TABLE host_run_bindings DROP COLUMN {column}"
                )
            self.ledger._connection.execute("PRAGMA user_version = 5")

    def _register_schema_five_backup(
        self,
        *,
        file_id: str = "schema5_backup",
        project_id: str | None = None,
        expires_at_ms: int | None = None,
    ) -> Path:
        target_project_id = project_id or self.project_a_id
        body = _snapshot_project_partition(
            self.ledger._connection,
            target_project_id,
            ledger_schema_version=5,
        )
        payload = canonical_json({
            "backup_sha256": hashlib.sha256(canonical_json(body)).hexdigest(),
            "body": body,
        })
        path = self.ledger._write_owner_file(
            "backup", target_project_id, file_id, payload
        )
        self.ledger._register_stored_file(
            target_project_id,
            file_id=file_id,
            kind="backup",
            path=path,
            digest=hashlib.sha256(payload).hexdigest(),
            expires_at_ms=(
                expires_at_ms
                if expires_at_ms is not None
                else time.time_ns() // 1_000_000 + 60_000
            ),
        )
        return path

    def _leave_ledger_closed(self) -> None:
        self.ledger = LearningLedger.__new__(LearningLedger)
        self.ledger.close = lambda: None  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def test_state_root_is_owner_only_and_outside_git(self) -> None:
        self.assertEqual(0o700, self.state.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.state.key_path.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.state.allowed_signers_path.stat().st_mode & 0o777)
        self.assertNotIn("PRIVATE", self.state.allowed_signers_path.read_text())
        with self.assertRaisesRegex(StateError, "Git working tree"):
            LearningState(ORCHESTRATION_ROOT / "bad-learning-state")

    def test_owner_public_key_cannot_be_silently_replaced(self) -> None:
        replacement_root = Path(self.temporary.name) / "replacement"
        replacement_root.mkdir()
        _, replacement = generate_test_owner_key(replacement_root)
        with self.assertRaisesRegex(StateError, "key rotation"):
            self.state.install_owner_public_key(replacement)

    def test_symlinked_state_component_is_rejected(self) -> None:
        link = Path(self.temporary.name) / "link"
        link.symlink_to(self.state.root, target_is_directory=True)
        with self.assertRaisesRegex(StateError, "symlink"):
            LearningState(link, create=False)

    def test_symlinked_managed_subdirectory_is_rejected(self) -> None:
        self.ledger.close()
        outside = Path(self.temporary.name) / "outside-artifacts"
        outside.mkdir()
        self.state.artifacts.rmdir()
        self.state.artifacts.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(StateError, "symlink"):
            LearningState(self.state_root, create=False)
        self.ledger = LearningLedger.__new__(LearningLedger)
        self.ledger.close = lambda: None  # type: ignore[method-assign]

    def test_existing_state_root_with_shared_mode_is_rejected(self) -> None:
        unsafe = Path(self.temporary.name) / "unsafe-state"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        with self.assertRaisesRegex(StateError, "unsafe permissions"):
            LearningState(unsafe)

    def test_database_symlink_is_rejected_before_sqlite_opens_it(self) -> None:
        other_state = LearningState(Path(self.temporary.name) / "other-state")
        outside = Path(self.temporary.name) / "outside.sqlite3"
        outside.write_bytes(b"untouched")
        outside.chmod(0o600)
        other_state.database.unlink()
        other_state.database.symlink_to(outside)
        with self.assertRaisesRegex(StateError, "database path"):
            LearningLedger(other_state)
        self.assertEqual(b"untouched", outside.read_bytes())

    def test_registration_and_scoped_reads_are_partitioned(self) -> None:
        self.ledger.append(self.project_a_id, event_record("evt_a"), producer="hook", now_ms=2)
        self.ledger.append(self.project_b_id, event_record("evt_b"), producer="hook", now_ms=2)
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertEqual(["evt_a"], [item["record_id"] for item in repository.records()])
        with self.ledger.repository(self.project_b_id) as repository:
            self.assertEqual(["evt_b"], [item["record_id"] for item in repository.records()])

    def test_client_health_is_content_free_and_project_partitioned(self) -> None:
        client_health_increment(
            self.state,
            self.project_a,
            "collector_unavailable",
            now_ms=5,
        )
        self.assertEqual(
            {"collector_unavailable": {"count": 1, "updated_at_ms": 5}},
            client_health_snapshot(self.state, self.project_a),
        )
        health_bytes = self.state.client_health.read_bytes()
        self.assertNotIn(str(self.project_a).encode(), health_bytes)
        self.assertEqual(0o600, self.state.client_health.stat().st_mode & 0o777)
        client_health_clear(self.state, self.project_a)
        self.assertEqual({}, client_health_snapshot(self.state, self.project_a))

    def test_ingestion_snapshot_is_exact_project_aggregate_only(self) -> None:
        event_codes = [
            "session_started", "session_ended", "turn_started",
            "turn_stop_observed", "turn_interrupted", "subagent_started",
            "subagent_stop_observed", "tool_started", "tool_output_observed",
        ]
        for index, code in enumerate(event_codes):
            event = event_record(f"hook_{index}", observed_at_ms=100 + index)
            event["event_code"] = code
            self.ledger.append(
                self.project_a_id, event, producer="hook", now_ms=200 + index
            )
        isolated = event_record("other_project", observed_at_ms=300)
        self.ledger.append(
            self.project_b_id, isolated, producer="hook", now_ms=300
        )
        for code, observed in (
            ("hook_normalization_rejected", 310),
            ("hook_binding_rejected", 320),
            ("hook_ingest_rejected", 330),
        ):
            client_health_increment(self.state, self.project_a, code, now_ms=observed)
        failures = client_health_snapshot(self.state, self.project_a)
        statements: list[str] = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            fresh = self.ledger.ingestion_snapshot(
                self.project_a_id, client_failures=failures, now_ms=400
            )
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual(9, fresh["accepted_hook_event_count"])
        self.assertEqual({code: 1 for code in event_codes}, fresh["hook_event_counts"])
        self.assertEqual(208, fresh["last_accepted_hook_event_at_ms"])
        self.assertEqual(3, fresh["rejected_hook_event_count"])
        self.assertEqual(330, fresh["last_rejected_hook_event_at_ms"])
        self.assertEqual({"state": "fresh", "age_ms": 192}, fresh["freshness"])
        self.assertNotIn("run_1", json.dumps(fresh))
        self.assertNotIn("hook_0", json.dumps(fresh))
        snapshot_selects = [
            statement for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertEqual(4, len(snapshot_selects))
        self.assertTrue(any("FROM hook_ingestion" in value for value in snapshot_selects))
        self.assertTrue(all("FROM records" not in value for value in snapshot_selects))
        stale = self.ledger.ingestion_snapshot(
            self.project_a_id, now_ms=208 + INGESTION_FRESH_MS + 1
        )
        self.assertEqual("stale", stale["freshness"]["state"])
        empty_root = Path(self.temporary.name) / "empty-project"
        empty_root.mkdir()
        empty_id = self.ledger.register_project(empty_root, now_ms=1)
        awaiting = self.ledger.ingestion_snapshot(empty_id, now_ms=400)
        self.assertEqual(
            "awaiting_hook_review_or_first_event", awaiting["freshness"]["state"]
        )
        self.assertEqual(1, self.ledger.ingestion_snapshot(
            self.project_b_id, now_ms=400
        )["accepted_hook_event_count"])
        self.assertEqual(
            "unregistered",
            empty_ingestion_snapshot(registered=False)["registered_project_state"],
        )

    def test_goal_statistics_migration_uses_schema_six(self) -> None:
        version = self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            str(row[0])
            for row in self.ledger._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertEqual(6, version)
        self.assertIn("host_run_bindings", tables)
        self.assertIn("hook_ingestion", tables)
        self.assertIn("orchestrations", tables)
        self.assertIn("operational_qa", tables)
        self.assertIn("operational_check_receipts", tables)

    def test_hook_ingestion_aggregate_reconciles_after_expiry(self) -> None:
        event = event_record("expiring_hook", observed_at_ms=1)
        self.ledger.append(self.project_a_id, event, producer="hook", now_ms=1)
        self.assertEqual(
            1,
            self.ledger.ingestion_snapshot(
                self.project_a_id, now_ms=2
            )["accepted_hook_event_count"],
        )
        self.assertEqual(
            1,
            self.ledger.expire(
                self.project_a_id, now_ms=1 + 90 * 24 * 60 * 60 * 1000
            ),
        )
        self.assertEqual(
            0,
            self.ledger.ingestion_snapshot(
                self.project_a_id, now_ms=2
            )["accepted_hook_event_count"],
        )

    def test_additive_hook_ingestion_migration_backfills_existing_events(self) -> None:
        event = event_record("before_031", observed_at_ms=10)
        event["event_code"] = "turn_started"
        self.ledger.append(self.project_a_id, event, producer="hook", now_ms=20)
        self.ledger.close()
        connection = sqlite3.connect(self.state.database)
        connection.execute("DROP TABLE hook_ingestion")
        connection.commit()
        connection.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        snapshot = self.ledger.ingestion_snapshot(self.project_a_id, now_ms=30)
        self.assertEqual(6, self.ledger._connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0])
        self.assertEqual(1, snapshot["accepted_hook_event_count"])
        self.assertEqual(1, snapshot["hook_event_counts"]["turn_started"])
        self.assertEqual(20, snapshot["last_accepted_hook_event_at_ms"])

    def test_030_record_writes_keep_031_hook_aggregates_exact(self) -> None:
        event = validate_learning_record(
            event_record("legacy_runtime_event", observed_at_ms=10)
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id,
                    event["record_id"],
                    "event",
                    "hook",
                    1,
                    10,
                    20,
                    20 + 90 * 24 * 60 * 60 * 1000,
                    canonical_json(event),
                    content_digest(event),
                    0,
                ),
            )
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertEqual(
            1,
            self.ledger.ingestion_snapshot(
                self.project_a_id, now_ms=30
            )["accepted_hook_event_count"],
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "DELETE FROM records WHERE project_id=? AND record_id=?",
                (self.project_a_id, event["record_id"]),
            )
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertEqual(
            0,
            self.ledger.ingestion_snapshot(
                self.project_a_id, now_ms=40
            )["accepted_hook_event_count"],
        )

    def test_incomplete_hook_aggregate_migration_is_reconciled_atomically(self) -> None:
        self.ledger.append(
            self.project_a_id,
            event_record("migration_recovery", observed_at_ms=10),
            producer="hook",
            now_ms=20,
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "DELETE FROM additive_migrations WHERE feature_key='hook_ingestion_v1'"
            )
            self.ledger._connection.execute(
                "UPDATE hook_ingestion SET accepted_count=99"
            )
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        snapshot = self.ledger.ingestion_snapshot(self.project_a_id, now_ms=30)
        marker = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM additive_migrations "
            "WHERE feature_key='hook_ingestion_v1'"
        ).fetchone()[0]
        self.assertEqual(1, marker)
        self.assertEqual(1, snapshot["accepted_hook_event_count"])

    def test_030_project_deletion_trigger_cleans_031_additive_state(self) -> None:
        self.ledger.append(
            self.project_a_id,
            event_record("legacy_delete", observed_at_ms=10),
            producer="hook",
            now_ms=20,
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,"
                "expires_at_ms,consumed_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    self.project_a_id, "a" * 64, "legacy-delete-run", "b" * 64,
                    "c" * 64, "d" * 64, "e" * 64, "codex-host-v1", 1, 100,
                ),
            )
            # This is the 0.3.0 deletion sequence: remove only tables known to
            # that runtime, then retain the project row as a tombstone.
            self.ledger._connection.execute(
                "DELETE FROM records WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "DELETE FROM sessions WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "DELETE FROM run_provenance WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "DELETE FROM stored_files WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "DELETE FROM collector_health WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "DELETE FROM repair_receipts WHERE project_id=?", (self.project_a_id,)
            )
            self.ledger._connection.execute(
                "UPDATE projects SET canonical_root=NULL,root_digest=NULL,status='deleted',"
                "deleted_at_ms=? WHERE project_id=? AND status='active'",
                (30, self.project_a_id),
            )
        for table in ("host_run_bindings", "hook_ingestion"):
            self.assertEqual(
                0,
                self.ledger._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (self.project_a_id,),
                ).fetchone()[0],
            )

    def test_host_event_expiry_retains_lifecycle_binding_history(self) -> None:
        event = validate_learning_record(
            event_record("legacy_host_expiry", observed_at_ms=10)
        )
        event["run_id"] = "legacy-host-run"
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,"
                "expires_at_ms,consumed_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    self.project_a_id, "a" * 64, event["run_id"], "b" * 64,
                    "c" * 64, "d" * 64, "e" * 64, "codex-host-v1", 1, 100,
                ),
            )
            self.ledger._connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, event["record_id"], "event", "host", 1,
                    10, 20, 20 + 90 * 24 * 60 * 60 * 1000,
                    canonical_json(event), content_digest(event), 0,
                ),
            )
            # Content retention must not rewrite lifecycle totals.
            self.ledger._connection.execute(
                "DELETE FROM records WHERE project_id=? AND record_id=?",
                (self.project_a_id, event["record_id"]),
            )
        self.assertEqual(
            1,
            self.ledger._connection.execute(
                "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=?",
                (self.project_a_id,),
            ).fetchone()[0],
        )

    def test_orchestration_report_retains_terminal_total_without_outcome_content(self) -> None:
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=1
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,"
                "expires_at_ms,consumed_at_ms,orchestration_id,native_assignment_id,"
                "agent_type,usage_complete,binding_state,abandoned_reason,"
                "observation_started_at_ms,observation_deadline_at_ms,"
                "last_progress_at_ms,deadline_observed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, "a" * 64, "retained-terminal-run",
                    "b" * 64, "c" * 64, "d" * 64, "e" * 64,
                    "codex-host-v1", 2, 100, 4,
                    orchestration["orchestration_id"], "asg_retained",
                    "technology_researcher", 0, "terminal", None,
                    2, 100, 3, 1,
                ),
            )

        report = self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )

        self.assertEqual(1, report["runs"]["terminal"])
        self.assertEqual(0, report["runs"]["pending"])
        self.assertEqual(
            {"terminal_content_unavailable": 1}, report["outcome_status"]
        )
        self.assertEqual(0, report["qa"]["pending"])

    def test_bulk_hook_expiry_has_bounded_database_work(self) -> None:
        row_count = 1000
        expires_at = 90 * 24 * 60 * 60 * 1000
        plan = self.ledger._connection.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(ingested_at_ms) FROM records "
            "WHERE project_id=? AND source='hook' AND record_type='event' "
            "AND json_extract(CAST(content AS TEXT),'$.event_code')=?",
            (self.project_b_id, "turn_started"),
        ).fetchall()
        self.assertTrue(any(
            "USING INDEX hook_event_latest" in str(row[3]) for row in plan
        ))
        rows = []
        for index in range(row_count):
            event = validate_learning_record(
                event_record(f"bulk_hook_{index}", observed_at_ms=index)
            )
            rows.append(
                (
                    self.project_b_id, event["record_id"], "event", "hook",
                    index + 1, index, index, expires_at,
                    canonical_json(event), content_digest(event), 0,
                )
            )
        with self.ledger._connection:
            self.ledger._connection.executemany(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows
            )
        progress_calls = 0

        def count_progress() -> int:
            nonlocal progress_calls
            progress_calls += 1
            return 0

        self.ledger._connection.set_progress_handler(count_progress, 1000)
        try:
            self.assertEqual(
                row_count,
                self.ledger.expire(self.project_b_id, now_ms=expires_at),
            )
        finally:
            self.ledger._connection.set_progress_handler(None, 0)
        self.assertLess(progress_calls, 1000)
        self.assertEqual(
            0,
            self.ledger.ingestion_snapshot(
                self.project_b_id, now_ms=expires_at
            )["accepted_hook_event_count"],
        )

    def test_mined_and_reviewed_case_versions_persist_as_immutable_rows(self) -> None:
        source = verification_outcome("verification_case_source")
        self._admit_outcome(self.project_a_id, source, now_ms=2)
        mined = mine_evaluation_case(
            digest_key=self.state.digest_key(),
            outcome=source,
            fixture_digest=DIGEST_A,
            criteria_digest=DIGEST_B,
            lineage_digest=DIGEST_C,
        )
        reviewed = review_evaluation_case(
            mined,
            digest_key=self.state.digest_key(),
            review_digest=DIGEST_C,
            similarity_group_digest="d" * 64,
            destination="promotion_holdout",
            freeze=True,
        )
        self.ledger.append(self.project_a_id, mined, producer="evaluator", now_ms=3)
        self.ledger.append(self.project_a_id, reviewed, producer="evaluator", now_ms=4)
        with self.ledger.repository(self.project_a_id) as repository:
            cases = repository.records("evaluation_case")
        self.assertEqual([], cases)
        with self.assertRaisesRegex(LedgerError, "not found"):
            with self.ledger.repository(self.project_a_id) as repository:
                repository.record(reviewed["record_id"], "evaluation_case")

    def test_reviewer_similarity_group_cannot_cross_corpus_boundaries(self) -> None:
        records = []
        for index in range(2):
            source = verification_outcome(f"similarity-source-{index}")
            self._admit_outcome(self.project_a_id, source, now_ms=2 + index)
            mined = mine_evaluation_case(
                digest_key=self.state.digest_key(),
                outcome=source,
                fixture_digest=source["fixture_digest"],
                criteria_digest=f"{index + 11:064x}",
                lineage_digest=f"{index + 21:064x}",
            )
            records.append(
                (mined, f"{index + 31:064x}")
            )
        first, first_review = records[0]
        self.ledger.append(
            self.project_a_id, first, producer="evaluator", now_ms=4
        )
        reviewed = review_evaluation_case(
            first,
            digest_key=self.state.digest_key(),
            review_digest=first_review,
            similarity_group_digest="f" * 64,
            destination="promotion_holdout",
            freeze=True,
        )
        self.ledger.append(
            self.project_a_id, reviewed, producer="evaluator", now_ms=5
        )
        second, second_review = records[1]
        self.ledger.append(
            self.project_a_id, second, producer="evaluator", now_ms=6
        )
        second_reviewed = review_evaluation_case(
            second,
            digest_key=self.state.digest_key(),
            review_digest=second_review,
            similarity_group_digest="f" * 64,
            destination="canary",
            freeze=True,
        )
        with self.assertRaisesRegex(LedgerError, "similarity group"):
            self.ledger.append(
                self.project_a_id, second_reviewed, producer="evaluator", now_ms=7
            )

    def test_forged_project_field_and_cross_project_link_are_rejected(self) -> None:
        record = event_record("evt_a")
        record["project_id"] = self.project_b_id
        with self.assertRaisesRegex(LedgerError, "admission"):
            self.ledger.append(self.project_a_id, record, producer="hook", now_ms=2)
        self.ledger.append(self.project_a_id, event_record("evt_a"), producer="hook", now_ms=2)
        self.ledger.append(self.project_b_id, event_record("evt_b"), producer="hook", now_ms=2)
        with self.assertRaisesRegex(LedgerError, "link"):
            self.ledger.link(self.project_a_id, "evt_a", "evt_b", "derived_from")

    def test_exact_duplicate_is_idempotent_and_conflict_is_rejected(self) -> None:
        first = self.ledger.append(
            self.project_a_id, event_record("evt_a"), producer="hook", now_ms=2
        )
        duplicate = self.ledger.append(
            self.project_a_id, event_record("evt_a"), producer="hook", now_ms=3
        )
        self.assertTrue(first["stored"])
        self.assertTrue(duplicate["duplicate"])
        changed = event_record("evt_a")
        changed["status"] = "observed"
        with self.assertRaisesRegex(LedgerError, "conflicts"):
            self.ledger.append(self.project_a_id, changed, producer="hook", now_ms=4)

    def test_late_event_is_preserved_and_marked(self) -> None:
        self.ledger.append(
            self.project_a_id,
            event_record("evt_new", observed_at_ms=200),
            producer="hook",
            now_ms=2,
        )
        result = self.ledger.append(
            self.project_a_id,
            event_record("evt_old", observed_at_ms=100),
            producer="hook",
            now_ms=3,
        )
        self.assertTrue(result["late"])
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertTrue(repository.records()[1]["_late"])

    def test_producer_authority_is_enforced(self) -> None:
        with self.assertRaisesRegex(LedgerError, "not authorized"):
            self.ledger.append(
                self.project_a_id, event_record("evt_a"), producer="owner", now_ms=2
            )

    def test_outcome_requires_exact_host_assignment_provenance(self) -> None:
        outcome = verification_outcome("exact-provenance")
        event = event_record("exact-provenance-event")
        event.update(
            run_id=outcome["run_id"],
            assignment_id=outcome["assignment_id"],
            model=outcome["model"],
            effort=outcome["effort"],
            role=outcome["role"],
            task_slice=outcome["task_slice"],
            topology=outcome["topology"],
            configuration_digest=outcome["configuration_digest"],
            coverage=outcome["coverage"],
            usage=outcome["usage"],
            event_code="item_completed",
            status="completed",
            item_digest=outcome["evidence_digests"][0],
        )
        changed = deepcopy(outcome)
        changed["effort"] = "low"
        configuration = {
            key: changed[key]
            for key in (
                "model", "effort", "role", "topology", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                "client_digest",
            )
        }
        changed["configuration_digest"] = content_digest(configuration)
        with self.assertRaisesRegex(LedgerError, "host result provenance"):
            with self.ledger._lock, self.ledger._connection:
                self.ledger._append_checked_locked(
                    self.project_a_id, event, producer="host", timestamp=3
                )
                self.ledger._validate_evidence_links_locked(
                    self.project_a_id, changed
                )

    def test_trusted_host_run_binds_exact_route_and_keeps_usage_unknown(self) -> None:
        self._admit(self.project_a_id)
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=90
        )
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        assigned = assignment()
        capability = self.clients["codex-host-v1"]
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(capability),
        }
        route = {
            "task_id": "task-1", "role": assigned["role"],
            "model": assigned["model"], "effort": assigned["effort"],
            "topology": "single", "max_parallel": 1,
            "verification_required": True,
            "execution_configuration": configuration,
        }
        opened = self.ledger.open_host_run(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            now_ms=100,
        )
        self.ledger.bind_host_agent(
            self.project_a_id,
            run_handle=opened["run_handle"],
            agent_id="native-first-child",
        )
        self.assertRegex(opened["run_handle"], r"^hrn_[0-9a-f]{48}$")
        with self.ledger.repository(self.project_a_id) as repository:
            started = repository.records("event")[-1]
        self.assertEqual("subagent_started", started["event_code"])
        self.assertEqual(assigned["assignment_id"], started["assignment_id"])
        self.assertEqual(assigned["role"], started["role"])
        self.assertEqual(assigned["model"], started["model"])
        self.assertEqual(assigned["effort"], started["effort"])
        self.assertTrue(all(value is None for value in started["usage"].values()))
        observation = {
            "run_handle": opened["run_handle"],
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        invalid_receipt_observation = {
            "run_handle": opened["run_handle"],
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "invalid_shape",
                    "receipt_id": "rpr_" + "f" * 48,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        with self.assertRaisesRegex(LedgerError, "repair receipt"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=result(),
                observation=invalid_receipt_observation,
                execution_configuration=configuration,
                now_ms=199,
            )
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertEqual(
                ["subagent_started"],
                [record["event_code"] for record in repository.records("event")],
            )
        committed = self.ledger.commit_host_result(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            result=result(),
            observation=observation,
            execution_configuration=configuration,
            now_ms=200,
        )
        self.assertEqual(assigned["assignment_id"], committed["outcome"]["assignment_id"])
        self.assertTrue(
            all(value is None for value in committed["outcome"]["usage"].values())
        )
        with self.assertRaisesRegex(LedgerError, "assigned route|already consumed"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=result(),
                observation=observation,
                execution_configuration=configuration,
                now_ms=201,
            )
        repair_opened = self.ledger.open_host_run(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            now_ms=220,
        )
        self.ledger.bind_host_agent(
            self.project_a_id,
            run_handle=repair_opened["run_handle"],
            agent_id="native-repair-child",
        )
        assignment_digest = keyed_digest(
            self.state.digest_key(), canonical_json(assigned)
        )
        receipt_id = self.ledger.issue_result_repair_receipt(
            self.project_a_id, assignment_digest, "invalid_shape", now_ms=221
        )
        repaired_observation = {
            "run_handle": repair_opened["run_handle"],
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "invalid_shape",
                    "receipt_id": receipt_id,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        before = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        with mock.patch.object(
            self.ledger,
            "_append_checked_locked",
            side_effect=LedgerError("forced terminal admission failure"),
        ):
            with self.assertRaisesRegex(LedgerError, "forced terminal"):
                self.ledger.commit_host_result(
                    self.project_a_id,
                    assignment=assigned,
                    task=task,
                    route=route,
                    result=result(),
                    observation=repaired_observation,
                    execution_configuration=configuration,
                    now_ms=222,
                )
        handle_digest = keyed_digest(
            self.state.digest_key(), repair_opened["run_handle"]
        )
        handle_consumed = self.ledger._connection.execute(
            "SELECT consumed_at_ms FROM host_run_bindings "
            "WHERE project_id=? AND handle_digest=?",
            (self.project_a_id, handle_digest),
        ).fetchone()[0]
        receipt_consumed = self.ledger._connection.execute(
            "SELECT consumed_at_ms FROM repair_receipts "
            "WHERE project_id=? AND receipt_id=?",
            (self.project_a_id, receipt_id),
        ).fetchone()[0]
        after = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertIsNone(handle_consumed)
        self.assertIsNone(receipt_consumed)
        self.assertEqual(before, after)
        repaired = self.ledger.commit_host_result(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            result=result(),
            observation=repaired_observation,
            execution_configuration=configuration,
            now_ms=223,
        )
        self.assertEqual(1, repaired["outcome"]["repairs"])
        changed_route = dict(route)
        changed_route["model"] = "gpt-6-astra"
        with self.assertRaisesRegex(LedgerError, "assigned route"):
            other = self.ledger.open_host_run(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                execution_configuration=configuration,
                client_version="codex-host-v1",
                orchestration_handle=orchestration["orchestration_handle"],
                now_ms=300,
            )
            self.ledger.bind_host_agent(
                self.project_a_id,
                run_handle=other["run_handle"],
                agent_id="native-invalid-route-child",
            )
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=changed_route,
                result=result(),
                observation={**observation, "run_handle": other["run_handle"]},
                execution_configuration=configuration,
                now_ms=301,
            )
        self.ledger.set_orchestration_paused(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            paused=True,
            now_ms=302,
        )
        abandoned = self.ledger.classify_host_run_abandoned(
            self.project_a_id,
            run_handle=other["run_handle"],
            reason="host_confirmed_gone",
        )
        self.assertEqual("abandoned", abandoned["binding_state"])
        paused_report = self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )
        self.assertEqual(1, paused_report["runs"]["abandoned"])
        self.assertEqual(0, paused_report["runs"]["current_active"])
        self.ledger.set_orchestration_paused(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            paused=False,
            now_ms=303,
        )
        timeout_opened = self.ledger.open_host_run(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            now_ms=304,
        )
        self.ledger.bind_host_agent(
            self.project_a_id,
            run_handle=timeout_opened["run_handle"],
            agent_id="native-timeout-child",
        )
        timeout_result = result()
        timeout_result.update(
            status="failed",
            execution_state="timed_out",
            semantic_verdict="not-applicable",
            admission_state="unverified",
            retry={"attempt": 1, "retry_of_assignment_id": None},
        )
        timeout_observation = {
            "run_handle": timeout_opened["run_handle"],
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        with self.assertRaisesRegex(LedgerError, "authoritative host fact"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=timeout_result,
                observation=timeout_observation,
                execution_configuration=configuration,
                now_ms=305,
            )
        timed_out = self.ledger.commit_host_result(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            result=timeout_result,
            observation={**timeout_observation, "terminal_state": "timed_out"},
            execution_configuration=configuration,
            now_ms=306,
        )
        self.assertEqual("timed_out", timed_out["outcome"]["execution_state"])
        self.assertEqual(0, self.ledger.expire(
            self.project_a_id, now_ms=300 + HOST_RUN_TTL_MS + 1
        ))
        remaining = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(4, remaining)
        self.ledger.expire(
            self.project_a_id, now_ms=223 + DEFAULT_RETENTION_MS + 1
        )
        remaining = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(4, remaining)

    def test_runtime_ledger_has_no_prebuilt_outcome_admission(self) -> None:
        runtime_state = LearningState(Path(self.temporary.name) / "runtime-state")
        runtime_project = Path(self.temporary.name) / "runtime-project"
        runtime_project.mkdir()
        runtime = LearningLedger(runtime_state)
        try:
            runtime.register_project(runtime_project, now_ms=1)
            self.assertFalse(hasattr(runtime, "commit_fixture_outcome"))
            self.assertFalse(hasattr(runtime, "_commit_derived_outcome"))
        finally:
            runtime.close()

    def test_legacy_host_timeout_requires_reconciled_timeout_stop_reason(self) -> None:
        self._admit(self.project_a_id)
        client_version = "codex-cli-0.153.0"
        capability = self.clients[client_version]
        _, _, run_id = self.ledger.bind_session(
            self.project_a,
            "legacy-timeout-session",
            "app_server",
            client_version,
            now_ms=100,
        )
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        assigned = assignment()
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(capability),
        }
        route = {
            "task_id": task["id"], "role": assigned["role"],
            "model": assigned["model"], "effort": assigned["effort"],
            "topology": "single", "max_parallel": 1,
            "verification_required": True,
            "execution_configuration": configuration,
        }
        timeout_result = result()
        timeout_result.update(
            status="failed",
            execution_state="timed_out",
            semantic_verdict="not-applicable",
            admission_state="unverified",
            retry={"attempt": 1, "retry_of_assignment_id": None},
        )
        observation = {
            "run_id": run_id,
            "client_version": client_version,
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }

        def terminal(identifier: str, stop_reason: str, observed_at_ms: int) -> dict:
            event = event_record(identifier, observed_at_ms=observed_at_ms)
            event.update(
                run_id=run_id,
                turn_id="legacy-timeout-turn",
                event_code="turn_failed",
                status="failed",
                stop_reason=stop_reason,
                coverage="root_turn_complete",
            )
            return event

        self.ledger.append(
            self.project_a_id,
            terminal("legacy-generic-failure", "failed", 110),
            producer="app_server",
            now_ms=110,
        )
        with self.assertRaisesRegex(LedgerError, "authoritative host timeout fact"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=timeout_result,
                observation=observation,
                execution_configuration=configuration,
                now_ms=111,
            )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "DELETE FROM records WHERE project_id=? AND record_id=?",
                (self.project_a_id, "legacy-generic-failure"),
            )
        self.ledger.append(
            self.project_a_id,
            terminal("legacy-timeout-failure", "timeout", 112),
            producer="app_server",
            now_ms=112,
        )

        committed = self.ledger.commit_host_result(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            result=timeout_result,
            observation=observation,
            execution_configuration=configuration,
            now_ms=113,
        )

        self.assertEqual("timed_out", committed["outcome"]["execution_state"])
        self.assertEqual(routing_task_slice(task), committed["outcome"]["task_slice"])

    def test_legacy_result_without_session_admission_is_refused(self) -> None:
        # No identity or attestation exists when the session binds, so the
        # legacy path has no admission and its otherwise valid result is refused.
        client_version = "codex-cli-0.153.0"
        capability = self.clients[client_version]
        _, _, run_id = self.ledger.bind_session(
            self.project_a,
            "unadmitted-legacy-session",
            "app_server",
            client_version,
            now_ms=100,
        )
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        assigned = assignment()
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(capability),
        }
        route = {
            "task_id": task["id"], "role": assigned["role"],
            "model": assigned["model"], "effort": assigned["effort"],
            "topology": "single", "max_parallel": 1,
            "verification_required": True,
            "execution_configuration": configuration,
        }
        timeout_result = result()
        timeout_result.update(
            status="failed",
            execution_state="timed_out",
            semantic_verdict="not-applicable",
            admission_state="unverified",
            retry={"attempt": 1, "retry_of_assignment_id": None},
        )
        observation = {
            "run_id": run_id,
            "client_version": client_version,
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }

        def terminal(identifier: str, stop_reason: str, observed_at_ms: int) -> dict:
            event = event_record(identifier, observed_at_ms=observed_at_ms)
            event.update(
                run_id=run_id,
                turn_id="legacy-timeout-turn",
                event_code="turn_failed",
                status="failed",
                stop_reason=stop_reason,
                coverage="root_turn_complete",
            )
            return event

        self.ledger.append(
            self.project_a_id,
            terminal("unadmitted-timeout-failure", "timeout", 112),
            producer="app_server",
            now_ms=112,
        )
        # Evidence recorded after binding does not admit the existing session.
        self._admit(self.project_a_id)
        with self.assertRaisesRegex(LedgerError, "no stored admission decision"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=timeout_result,
                observation=observation,
                execution_configuration=configuration,
                now_ms=113,
            )

    def test_orchestration_report_counts_each_host_run_once_and_keeps_usage_unknown(self) -> None:
        self._admit(self.project_a_id)
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        assigned = assignment()
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(self.clients["codex-host-v1"]),
        }
        route = {
            "task_id": "task-1", "role": assigned["role"],
            "agent_type": "technology_researcher",
            "model": assigned["model"], "effort": assigned["effort"],
            "topology": "single", "max_parallel": 1,
            "verification_required": True, "execution_configuration": configuration,
        }
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=100
        )
        opened = self.ledger.open_host_run(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            now_ms=110,
        )
        self.assertEqual(110, opened["observation_started_at_ms"])
        self.assertEqual(600_110, opened["observation_deadline_at_ms"])
        progress = self.ledger.observe_host_run(
            self.project_a_id,
            run_handle=opened["run_handle"],
            event="progress",
            now_ms=120,
        )
        self.assertEqual("active", progress["state"])
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        persisted = self.ledger._connection.execute(
            "SELECT observation_started_at_ms,observation_deadline_at_ms,"
            "last_progress_at_ms,deadline_observed FROM host_run_bindings "
            "WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()
        self.assertEqual((110, 600_110, 120, 0), tuple(persisted))
        with self.assertRaisesRegex(LedgerError, "has not elapsed"):
            self.ledger.observe_host_run(
                self.project_a_id,
                run_handle=opened["run_handle"],
                event="deadline",
                now_ms=130,
            )
        with self.assertRaisesRegex(LedgerError, "not bound to a native agent"):
            self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=result(),
                observation={
                    "run_handle": opened["run_handle"],
                    "result_attempts": [
                        {"status": "accepted", "reason_code": None,
                         "receipt_id": None}
                    ],
                },
                execution_configuration=configuration,
                now_ms=115,
            )
        binding = self.ledger.bind_host_agent(
            self.project_a_id,
            run_handle=opened["run_handle"],
            agent_id="native-child-1",
        )
        self.assertTrue(binding["bound"])
        with self.assertRaisesRegex(LedgerError, "unfinished host runs"):
            self.ledger.close_orchestration(
                self.project_a_id,
                orchestration_handle=orchestration["orchestration_handle"],
                status="completed",
                now_ms=120,
            )
        self.ledger.commit_host_result(
            self.project_a_id,
            assignment=assigned,
            task=task,
            route=route,
            result=result(),
            observation={
                "run_handle": opened["run_handle"],
                "result_attempts": [
                    {"status": "accepted", "reason_code": None, "receipt_id": None}
                ],
            },
            execution_configuration=configuration,
            now_ms=160,
        )
        checkpoint = self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            goal_tokens_used=300,
            goal_time_used_seconds=1,
            now_ms=165,
        )
        self.assertEqual("active", checkpoint["status"])
        self.assertEqual(300, checkpoint["usage"]["goal_total_tokens"])
        self.assertEqual(1, checkpoint["usage"]["goal_time_used_seconds"])
        paused = self.ledger.set_orchestration_paused(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            paused=True,
            now_ms=165,
        )
        self.assertEqual("paused", paused["status"])
        self.assertEqual(3, len(paused["checkpoints"]))
        resumed = self.ledger.set_orchestration_paused(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            paused=False,
            now_ms=165,
        )
        self.assertEqual("active", resumed["status"])
        self.assertEqual(4, len(resumed["checkpoints"]))
        with self.assertRaisesRegex(LedgerError, "cannot decrease"):
            self.ledger.checkpoint_orchestration(
                self.project_a_id,
                orchestration_handle=orchestration["orchestration_handle"],
                goal_tokens_used=299,
                goal_time_used_seconds=2,
                now_ms=166,
            )
        report = self.ledger.close_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            status="completed",
            goal_tokens_used=321,
            goal_time_used_seconds=2,
            now_ms=170,
        )
        self.assertEqual({
            "opened": 1, "terminal": 1, "pending": 0, "abandoned": 0,
            "current_active": 0, "agent_bound": 1,
            "deadlines_observed": 0,
        }, report["runs"])
        self.assertEqual({"gpt-6.1-sol": 1}, report["by_model"])
        self.assertEqual({"technology_researcher": 1}, report["by_agent_type"])
        self.assertEqual(321, report["usage"]["goal_total_tokens"])
        self.assertEqual("unknown", report["usage"]["child_attribution"])
        self.assertIsNone(report["usage"]["host_run_totals"]["input_tokens"])
        binding_expiry = self.ledger._connection.execute(
            "SELECT expires_at_ms FROM host_run_bindings WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(160 + DEFAULT_RETENTION_MS, binding_expiry)

    def _pending_upgrade_run(self) -> tuple[dict, dict]:
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=100
        )
        assigned = assignment()
        task = {
            "id": "upgrade-task", "objective": "Inspect one fixture",
            "family": "research", "complexity": "medium", "risk": "low",
            "meaningful": True, **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(self.clients["codex-host-v1"]),
        }
        route = {
            "task_id": task["id"], "role": assigned["role"],
            "model": assigned["model"], "effort": assigned["effort"],
            "topology": "single", "max_parallel": 1,
            "verification_required": True,
            "execution_configuration": configuration,
        }
        opened = self.ledger.open_host_run(
            self.project_a_id, assignment=assigned, task=task, route=route,
            execution_configuration=configuration, client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"], now_ms=110,
        )
        return orchestration, opened

    def test_signed_upgrade_reconciliation_is_exact_and_single_use(self) -> None:
        self._admit(self.project_a_id)
        orchestration, opened = self._pending_upgrade_run()
        self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            goal_tokens_used=123, goal_time_used_seconds=4, now_ms=120,
        )
        request = self.ledger.prepare_upgrade_reconciliation(
            self.project_a_id, orchestration_id=orchestration["orchestration_id"],
            run_handle=opened["run_handle"], now_ms=130,
        )
        self.assertNotIn(opened["run_handle"], canonical_json(request).decode())
        signature_path = sign_owner_payload(
            self.owner_private_key, canonical_json(request), "upgrade-reconcile"
        )
        signature = signature_path.read_bytes()
        signature_path.unlink()
        with self.assertRaisesRegex(LedgerError, "signature"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=request, signature=b"invalid", now_ms=140
            )
        with self.assertRaisesRegex(LedgerError, "invalid"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_b_id, request=request, signature=signature, now_ms=140
            )
        changed = {**request, "route_digest": DIGEST_A}
        with self.assertRaisesRegex(LedgerError, "signature"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=changed, signature=signature, now_ms=140
            )
        changed_signature_path = sign_owner_payload(
            self.owner_private_key, canonical_json(changed), "upgrade-wrong-route"
        )
        changed_signature = changed_signature_path.read_bytes()
        changed_signature_path.unlink()
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=changed,
                signature=changed_signature, now_ms=140,
            )
        with self.assertRaisesRegex(LedgerError, "expired"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=request, signature=signature,
                now_ms=request["expires_at_ms"],
            )
        append_checkpoint = self.ledger._append_orchestration_checkpoint_locked
        checkpoint_calls = 0

        def fail_second_checkpoint(*args: object, **kwargs: object) -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            if checkpoint_calls == 2:
                raise sqlite3.OperationalError("injected checkpoint failure")
            append_checkpoint(*args, **kwargs)

        with mock.patch.object(
            self.ledger, "_append_orchestration_checkpoint_locked",
            side_effect=fail_second_checkpoint,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                self.ledger.commit_upgrade_reconciliation(
                    self.project_a_id, request=request,
                    signature=signature, now_ms=140,
                )
        unchanged = self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )
        self.assertEqual("active", unchanged["status"])
        self.assertEqual(1, unchanged["runs"]["pending"])
        self.assertEqual(2, len(unchanged["checkpoints"]))
        report = self.ledger.commit_upgrade_reconciliation(
            self.project_a_id, request=request, signature=signature, now_ms=140
        )
        self.assertEqual("interrupted", report["status"])
        self.assertEqual(0, report["runs"]["pending"])
        self.assertEqual(1, report["runs"]["abandoned"])
        self.assertEqual(123, report["usage"]["goal_total_tokens"])
        self.assertEqual(["active", "active", "paused", "interrupted"], [
            item["status"] for item in report["checkpoints"]
        ])
        binding = self.ledger._connection.execute(
            "SELECT binding_state,abandoned_reason,consumed_at_ms FROM host_run_bindings "
            "WHERE project_id=? AND run_id=?",
            (self.project_a_id, request["run_id"]),
        ).fetchone()
        self.assertEqual(("abandoned", "upgrade_superseded", None), tuple(binding))
        with self.assertRaisesRegex(LedgerError, "fallback requires"):
            self.ledger.issue_legacy_fallback_receipt(
                self.project_a_id, run_handle=opened["run_handle"],
                task={
                    "id": "upgrade-task", "objective": "Inspect one fixture",
                    "family": "research", "complexity": "medium", "risk": "low",
                    "meaningful": True, **routing_fields(),
                    "deterministic_validation": True, "independent_parts": 1,
                    "parallel_safe": False, "failed_attempts": 0,
                    "failure_events": [],
                },
                primary_route={}, client_version="codex-host-v1", now_ms=150,
            )
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=request, signature=signature, now_ms=150
            )
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertEqual([], repository.records("outcome"))

    def test_upgrade_reconciliation_rejects_changed_boundary_and_other_pending(self) -> None:
        self._admit(self.project_a_id)
        orchestration, opened = self._pending_upgrade_run()
        request = self.ledger.prepare_upgrade_reconciliation(
            self.project_a_id, orchestration_id=orchestration["orchestration_id"],
            run_handle=opened["run_handle"], now_ms=120,
        )
        signature_path = sign_owner_payload(
            self.owner_private_key, canonical_json(request), "upgrade-reconcile-2"
        )
        signature = signature_path.read_bytes()
        signature_path.unlink()
        self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            goal_tokens_used=1, goal_time_used_seconds=1, now_ms=125,
        )
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=request, signature=signature, now_ms=130
            )
        self.assertEqual("active", self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )["status"])
        self.assertEqual(1, self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )["runs"]["pending"])
        refreshed = self.ledger.prepare_upgrade_reconciliation(
            self.project_a_id, orchestration_id=orchestration["orchestration_id"],
            run_handle=opened["run_handle"], now_ms=130,
        )
        refreshed_signature_path = sign_owner_payload(
            self.owner_private_key, canonical_json(refreshed), "upgrade-pending-2"
        )
        refreshed_signature = refreshed_signature_path.read_bytes()
        refreshed_signature_path.unlink()
        columns = [
            str(row[1]) for row in self.ledger._connection.execute(
                "PRAGMA table_info(host_run_bindings)"
            )
        ]
        row = self.ledger._connection.execute(
            "SELECT * FROM host_run_bindings WHERE project_id=? AND run_id=?",
            (self.project_a_id, refreshed["run_id"]),
        ).fetchone()
        values = dict(zip(columns, row))
        values["handle_digest"] = DIGEST_A
        values["run_id"] = "run:" + "a" * 64
        with self.ledger._connection:
            self.ledger._connection.execute(
                f"INSERT INTO host_run_bindings({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=refreshed,
                signature=refreshed_signature, now_ms=140,
            )
        self.assertEqual(2, self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )["runs"]["pending"])

    def test_upgrade_reconciliation_rejects_existing_host_outcome(self) -> None:
        self._admit(self.project_a_id)
        orchestration, opened = self._pending_upgrade_run()
        request = self.ledger.prepare_upgrade_reconciliation(
            self.project_a_id, orchestration_id=orchestration["orchestration_id"],
            run_handle=opened["run_handle"], now_ms=120,
        )
        signature_path = sign_owner_payload(
            self.owner_private_key, canonical_json(request), "upgrade-outcome"
        )
        signature = signature_path.read_bytes()
        signature_path.unlink()
        content = canonical_json({"run_id": request["run_id"]})
        sequence = self.ledger._connection.execute(
            "SELECT COALESCE(MAX(source_sequence),0)+1 FROM records WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO records(project_id,record_id,record_type,source,"
                "source_sequence,observed_at_ms,ingested_at_ms,expires_at_ms,"
                "content,content_digest,late) VALUES(?,?,'outcome','host',?,?,?,?,?,?,0)",
                (
                    self.project_a_id, "out:existing-host-outcome", sequence,
                    115, 115, None, content,
                    content_digest({"run_id": request["run_id"]}),
                ),
            )
        with self.assertRaisesRegex(LedgerError, "already has a host outcome"):
            self.ledger.commit_upgrade_reconciliation(
                self.project_a_id, request=request, signature=signature, now_ms=130
            )
        status = self.ledger._connection.execute(
            "SELECT status FROM orchestrations WHERE project_id=? "
            "AND orchestration_id=?",
            (self.project_a_id, orchestration["orchestration_id"]),
        ).fetchone()[0]
        binding_state = self.ledger._connection.execute(
            "SELECT binding_state FROM host_run_bindings WHERE project_id=? "
            "AND run_id=?", (self.project_a_id, request["run_id"]),
        ).fetchone()[0]
        self.assertEqual(("active", "pending"), (status, binding_state))

    def test_orchestration_close_preserves_latest_goal_checkpoint(self) -> None:
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=100
        )
        self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            goal_tokens_used=123,
            goal_time_used_seconds=4,
            now_ms=110,
        )
        report = self.ledger.close_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            status="completed",
            now_ms=120,
        )
        self.assertEqual(123, report["usage"]["goal_total_tokens"])
        self.assertEqual(4, report["usage"]["goal_time_used_seconds"])

        other = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=130
        )
        self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=other["orchestration_handle"],
            goal_tokens_used=10,
            goal_time_used_seconds=2,
            now_ms=140,
        )
        with self.assertRaisesRegex(LedgerError, "cannot decrease"):
            self.ledger.close_orchestration(
                self.project_a_id,
                orchestration_handle=other["orchestration_handle"],
                status="completed",
                goal_tokens_used=9,
                goal_time_used_seconds=3,
                now_ms=150,
            )

    def test_orchestration_checkpoint_time_cannot_move_backward(self) -> None:
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=100
        )
        self.ledger.checkpoint_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            goal_tokens_used=1,
            goal_time_used_seconds=1,
            now_ms=110,
        )

        with self.assertRaisesRegex(LedgerError, "cannot move backward"):
            self.ledger.checkpoint_orchestration(
                self.project_a_id,
                orchestration_handle=orchestration["orchestration_handle"],
                goal_tokens_used=2,
                goal_time_used_seconds=2,
                now_ms=105,
            )

        checkpoints = self.ledger.orchestration_report(
            self.project_a_id, orchestration["orchestration_id"]
        )["checkpoints"]
        self.assertEqual(2, len(checkpoints))
        self.assertEqual(100, checkpoints[0]["observed_at_ms"])
        self.assertEqual(110, checkpoints[1]["observed_at_ms"])

    def test_operational_qa_supersedes_unverified_run_and_surfaces_no_owner_review(self) -> None:
        self._admit(self.project_a_id)
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=100
        )

        def execute(
            assigned: dict, task: dict, configuration: dict, output: dict, now_ms: int,
            *, boundary: str = orchestration["orchestration_handle"],
        ) -> dict:
            route = {
                "task_id": task["id"], "role": assigned["role"],
                "model": assigned["model"], "effort": assigned["effort"],
                "topology": "single", "max_parallel": 1,
                "verification_required": assigned["verification_required"],
                "execution_configuration": configuration,
                "agent_type": (
                    "evidence_reviewer"
                    if task["family"] == "verification"
                    else "technology_researcher"
                ),
            }
            opened = self.ledger.open_host_run(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                execution_configuration=configuration,
                client_version="codex-host-v1",
                orchestration_handle=boundary,
                now_ms=now_ms,
            )
            self.ledger.bind_host_agent(
                self.project_a_id,
                run_handle=opened["run_handle"],
                agent_id=f"native-{assigned['assignment_id']}-{now_ms}",
            )
            return self.ledger.commit_host_result(
                self.project_a_id,
                assignment=assigned,
                task=task,
                route=route,
                result=output,
                observation={
                    "run_handle": opened["run_handle"],
                    "result_attempts": [
                        {"status": "accepted", "reason_code": None, "receipt_id": None}
                    ],
                },
                execution_configuration=configuration,
                now_ms=now_ms + 10,
            )

        evaluator = assignment()
        evaluator_task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "ambiguity": "unknown",
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }
        evaluator_configuration = {
            "model": evaluator["model"], "effort": evaluator["effort"],
            "role": evaluator["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(self.clients["codex-host-v1"]),
        }
        evaluator_result = result()
        evaluator_run = execute(
            evaluator, evaluator_task, evaluator_configuration, evaluator_result, 110
        )
        host = evaluator_run["outcome"]

        verifier = assignment()
        verifier.update(
            assignment_id="verify-assignment",
            task_id="verify-task",
            objective="Independently verify the result",
            role="codex_agents_verification_worker",
            model="gpt-6-sol",
            effort="high",
            context=[content_digest(host)],
            verification_required=False,
        )
        verifier_result = result()
        verifier_result["assignment_id"] = verifier["assignment_id"]
        verifier_task = {
            **evaluator_task,
            "id": "verify-task",
            "objective": "Independently verify the result",
            "family": "verification",
        }
        # QA accepts only a verifier opened for this exact worker outcome.
        bound_verifier_task = {
            **verifier_task,
            "verification_for_family": "research",
            "verification_for_provider": "codex",
            "verification_for_outcome": content_digest(host),
        }
        verifier_configuration = {
            **evaluator_configuration,
            "model": verifier["model"],
            "effort": verifier["effort"],
            "role": verifier["role"],
            "verification_required": False,
            "verifier_digest": None,
        }
        other_orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=129
        )
        wrong_goal_verifier = execute(
            verifier, verifier_task, verifier_configuration, verifier_result, 130,
            boundary=other_orchestration["orchestration_handle"],
        )
        with self.assertRaisesRegex(LedgerError, "one orchestration"):
            self.ledger.issue_operational_check_receipt(
                self.project_a_id,
                host_outcome_digest=content_digest(host),
                verifier_host_outcome_digest=content_digest(
                    wrong_goal_verifier["outcome"]
                ),
                check_report={
                    "deterministic_checks": [
                        {"check_id": "tests", "passed": True,
                         "evidence_digest": DIGEST_A}
                    ],
                    "security_checks": [
                        {"check_id": "authority", "passed": True,
                         "evidence_digest": DIGEST_B}
                    ],
                    "observed_at_ms": 150,
                },
                now_ms=150,
            )
        verifier_run = execute(
            verifier, bound_verifier_task, verifier_configuration, verifier_result, 140
        )
        check_receipt = self.ledger.issue_operational_check_receipt(
            self.project_a_id,
            host_outcome_digest=content_digest(host),
            verifier_host_outcome_digest=content_digest(verifier_run["outcome"]),
            check_report={
                "deterministic_checks": [
                    {"check_id": "tests", "passed": True, "evidence_digest": DIGEST_A}
                ],
                "security_checks": [
                    {"check_id": "authority", "passed": True,
                     "evidence_digest": DIGEST_B}
                ],
                "observed_at_ms": 160,
            },
            now_ms=160,
        )
        committed = self.ledger.commit_operational_qa(
            self.project_a_id,
            host_outcome_digest=content_digest(host),
            verifier_host_outcome_digest=content_digest(verifier_run["outcome"]),
            evaluator_assignment=evaluator,
            evaluator_result=evaluator_result,
            check_receipt=check_receipt["check_receipt"],
            verifier_assignment=verifier,
            verifier_result=verifier_result,
            now_ms=161,
        )
        self.assertTrue(committed["qa"]["accepted"])
        self.assertFalse(committed["attention_required"])
        routing_reference = "outcome:" + content_digest(host)
        rerouted_task = {
            **evaluator_task,
            "ambiguity": "known",
            "routing_evidence_refs": [routing_reference],
        }
        self.assertEqual(
            frozenset({routing_reference}),
            self.ledger.trusted_routing_evidence_refs(
                self.project_a_id, rerouted_task
            ),
        )
        negative_host = {**host, "semantic_verdict": "not-ready"}
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE records SET content=? WHERE project_id=? AND content_digest=?",
                (
                    canonical_json(negative_host),
                    self.project_a_id,
                    content_digest(host),
                ),
            )
        with self.assertRaisesRegex(
            LedgerError, "not positive completed reconnaissance"
        ):
            self.ledger.trusted_routing_evidence_refs(
                self.project_a_id, rerouted_task
            )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE records SET content=? WHERE project_id=? AND content_digest=?",
                (canonical_json(host), self.project_a_id, content_digest(host)),
            )
        with self.assertRaisesRegex(LedgerError, "task-bound reconnaissance"):
            self.ledger.trusted_routing_evidence_refs(
                self.project_a_id, {**rerouted_task, "id": "different-task"}
            )
        with self.assertRaisesRegex(LedgerError, "not QA accepted"):
            self.ledger.trusted_routing_evidence_refs(
                self.project_a_id,
                {
                    **rerouted_task,
                    "routing_evidence_refs": [
                        "outcome:" + content_digest(verifier_run["outcome"])
                    ],
                },
            )
        with self.assertRaisesRegex(LedgerError, "check receipt is unavailable"):
            self.ledger.commit_operational_qa(
                self.project_a_id,
                host_outcome_digest=content_digest(host),
                verifier_host_outcome_digest=content_digest(verifier_run["outcome"]),
                evaluator_assignment=evaluator,
                evaluator_result=evaluator_result,
                check_receipt=check_receipt["check_receipt"],
                verifier_assignment=verifier,
                verifier_result=verifier_result,
                now_ms=162,
            )
        cycle = self.ledger.run_learning_cycle(self.project_a_id, as_of_ms=161)
        self.assertEqual(1, len(cycle["mined_case_ids"]))
        report = self.ledger.close_orchestration(
            self.project_a_id,
            orchestration_handle=orchestration["orchestration_handle"],
            status="completed",
            now_ms=170,
        )
        self.assertEqual({"passed": 1, "failed": 0, "pending": 0,
                          "attention_required": False}, report["qa"])
        self.assertEqual(
            {"evidence_reviewer": 1, "technology_researcher": 1},
            report["by_agent_type"],
        )
        feedback = build_feedback(
            digest_key=self.state.digest_key(),
            run_id=host["run_id"],
            label="rejected",
            reason_code="incorrect",
            observed_at_ms=171,
        )
        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        signature_path = sign_owner_payload(
            self.owner_private_key,
            authority.owner_record_payload(self.project_a_id, feedback),
            "routing-evidence-feedback",
        )
        try:
            self.ledger.commit_owner_record(
                self.project_a_id,
                record=feedback,
                signature=signature_path.read_bytes(),
                now_ms=171,
            )
        finally:
            signature_path.unlink(missing_ok=True)
        with self.assertRaisesRegex(LedgerError, "invalidated by owner feedback"):
            self.ledger.trusted_routing_evidence_refs(
                self.project_a_id, rerouted_task
            )

    def test_owner_feedback_requires_an_admitted_run(self) -> None:
        feedback = build_feedback(
            digest_key=self.state.digest_key(),
            run_id="missing-run",
            label="rejected",
            reason_code="incorrect",
            observed_at_ms=3,
        )
        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        signature_path = sign_owner_payload(
            self.owner_private_key,
            authority.owner_record_payload(self.project_a_id, feedback),
            "missing-feedback-payload",
        )
        try:
            with self.assertRaisesRegex(LedgerError, "run is not admitted"):
                self.ledger.commit_owner_record(
                    self.project_a_id,
                    record=feedback,
                    signature=signature_path.read_bytes(),
                    now_ms=3,
                )
        finally:
            signature_path.unlink(missing_ok=True)

    def test_candidate_champion_must_match_durable_outcome_configuration(self) -> None:
        source = verification_outcome("candidate-baseline")
        source.update(
            **champion_configuration(),
            task_slice="coding-medium-low",
        )
        source["configuration_digest"] = content_digest(champion_configuration())
        self._admit_outcome(self.project_a_id, source, now_ms=10, producer="host")
        fictitious = {
            **champion_configuration(),
            "topology": "parallel",
            "max_parallel": 64,
        }
        before = {"model": "gpt-6-sol", "effort": "medium"}
        after = {"model": "gpt-6-luna", "effort": "high"}
        proposed = generate_candidate(
            digest_key=self.state.digest_key(),
            scope="general",
            task_slice="coding-medium-low",
            target="model_effort",
            effect="optimize",
            base_digest=candidate_base_digest(fictitious),
            champion_configuration=fictitious,
            change_manifest=[{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(before),
                "after_digest": content_digest(after),
                "before_value": before,
                "after_value": after,
            }],
            evidence_digests=[content_digest(source)],
            eligibility_receipt=eligibility_receipt(
                "coding-medium-low", [content_digest(source)]
            ),
        )
        with self.assertRaisesRegex(LedgerError, "champion"):
            self.ledger.append(
                self.project_a_id, proposed, producer="evaluator", now_ms=11
            )

    def test_candidate_common_persistence_gate_requires_accepted_qa(self) -> None:
        forged = self._candidate_cohort(
            self.project_a_id, accepted_qa=False, now_ms=300
        )
        with self.ledger._lock, self.ledger._connection:
            with self.assertRaisesRegex(LedgerError, "verified eligibility"):
                self.ledger._append_checked_locked(
                    self.project_a_id,
                    forged,
                    producer="evaluator",
                    timestamp=400,
                )

        valid = self._candidate_cohort(self.project_b_id, now_ms=500)
        committed = self.ledger.append(
            self.project_b_id, valid, producer="evaluator", now_ms=600
        )
        self.assertTrue(committed["stored"])

    def test_candidate_retry_predecessor_must_be_earlier_and_task_bound(self) -> None:
        proposed = self._candidate_cohort(self.project_a_id, now_ms=700)
        evidence_digest = proposed["evidence_digests"][0]
        row = self.ledger._connection.execute(
            "SELECT record_id,content FROM records WHERE project_id=? "
            "AND content_digest=?",
            (self.project_a_id, evidence_digest),
        ).fetchone()
        forged_outcome = json.loads(bytes(row["content"]))
        forged_outcome["retry_attempt"] = 2
        forged_outcome["retry_of_assignment_id"] = "missing-predecessor"
        forged_digest = content_digest(forged_outcome)
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE records SET content=?,content_digest=? "
                "WHERE project_id=? AND record_id=?",
                (
                    canonical_json(forged_outcome),
                    forged_digest,
                    self.project_a_id,
                    row["record_id"],
                ),
            )
        evidence = [
            forged_digest if digest == evidence_digest else digest
            for digest in proposed["evidence_digests"]
        ]
        forged = generate_candidate(
            digest_key=self.state.digest_key(),
            scope=proposed["scope"],
            task_slice=proposed["task_slice"],
            target=proposed["target"],
            effect=proposed["effect"],
            base_digest=proposed["base_digest"],
            champion_configuration=proposed["champion_configuration"],
            change_manifest=proposed["change_manifest"],
            evidence_digests=evidence,
            eligibility_receipt=eligibility_receipt(
                proposed["task_slice"], evidence
            ),
        )
        with self.assertRaisesRegex(LedgerError, "earlier persisted"):
            self.ledger.append(
                self.project_a_id, forged, producer="evaluator", now_ms=800
            )

    def test_unobserved_candidate_target_cannot_enter_evaluation(self) -> None:
        source = verification_outcome("unbound-target-source")
        source.update(
            **champion_configuration(),
            task_slice="coding-medium-low",
        )
        source["configuration_digest"] = content_digest(champion_configuration())
        self._admit_outcome(self.project_a_id, source, now_ms=10, producer="host")
        champion = champion_configuration()
        proposed = generate_candidate(
            digest_key=self.state.digest_key(),
            scope="general",
            task_slice="coding-medium-low",
            target="skill",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "skills.selection",
                "before_digest": content_digest(champion["skill_digests"]),
                "after_digest": content_digest([DIGEST_B]),
                "before_value": champion["skill_digests"],
                "after_value": [DIGEST_B],
            }],
            evidence_digests=[content_digest(source)],
            eligibility_receipt=eligibility_receipt(
                "coding-medium-low", [content_digest(source)]
            ),
        )
        proposed = self._candidate_cohort(
            self.project_a_id, proposed=proposed, now_ms=100
        )
        self.ledger.append(
            self.project_a_id, proposed, producer="evaluator", now_ms=11
        )

        assignment_record = evaluation_assignment(
            heldout_cases(1), candidate()
        )
        assignment_record["candidate_digest"] = proposed["candidate_digest"]
        for trial in assignment_record["trial_plan"]:
            trial["configuration_digest"] = (
                proposed["candidate_digest"]
                if trial["cohort"] == "candidate"
                else proposed["base_digest"]
            )
        assignment_record["record_id"] = "evaluation_assignment:" + content_digest(
            {key: value for key, value in assignment_record.items() if key != "record_id"}
        )
        with self.assertRaisesRegex(LedgerError, "exact evaluation"):
            self.ledger.append(
                self.project_a_id,
                assignment_record,
                producer="evaluator",
                now_ms=12,
            )

    def test_expiry_deletes_linked_lineage(self) -> None:
        first = event_record("evt_a")
        second = event_record("evt_b")
        self.ledger.append(self.project_a_id, first, producer="hook", now_ms=0)
        self.ledger.append(self.project_a_id, second, producer="hook", now_ms=0)
        self.ledger.link(self.project_a_id, "evt_a", "evt_b", "derived_from")
        backup = self.ledger.create_sanitized_backup(
            self.project_a_id,
            file_id="future_backup",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        deleted = self.ledger.expire(
            self.project_a_id, now_ms=90 * 24 * 60 * 60 * 1000
        )
        self.assertEqual(2, deleted)
        self.assertFalse(backup.exists())
        with self.assertRaisesRegex(LedgerError, "backup is unavailable"):
            self.ledger.verify_sanitized_backup(
                self.project_a_id, file_id="future_backup"
            )
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertEqual([], repository.records())

    def test_backup_roundtrips_complete_project_authority_and_lifecycle_rows(self) -> None:
        self.ledger.bind_session(
            self.project_a, "backup-session", "hook", "codex-hooks-v1", now_ms=5
        )
        first = event_record("backup-first", observed_at_ms=6)
        second = event_record("backup-second", observed_at_ms=7)
        self.ledger.append(self.project_a_id, first, producer="hook", now_ms=6)
        self.ledger.append(self.project_a_id, second, producer="hook", now_ms=7)
        self.ledger.link(
            self.project_a_id, first["record_id"], second["record_id"], "evidence"
        )
        self.ledger.write_sanitized_record_file(
            self.project_a_id,
            file_id="backup_artifact",
            kind="artifact",
            record=first,
        )
        self.ledger.issue_result_repair_receipt(
            self.project_a_id, "a" * 64, "missing_evidence", now_ms=8
        )
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=9
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO collector_health VALUES(?,?,?,?)",
                (self.project_a_id, "backup_probe", 2, 10),
            )
            self.ledger._connection.execute(
                "INSERT INTO consumed_grants VALUES(?,?,?,?,?,?)",
                (
                    self.project_a_id, first["record_id"], second["record_id"],
                    "b" * 64, "released", 11,
                ),
            )
            self.ledger._connection.execute(
                "INSERT INTO owner_attestations VALUES(?,?,?,?)",
                (self.project_a_id, first["record_id"], b"\x00\xff", 12),
            )
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "task_id_digest,route_digest,configuration_digest,client_version,"
                "created_at_ms,expires_at_ms,consumed_at_ms,orchestration_id,"
                "native_assignment_id,agent_type,usage_complete,binding_state,"
                "abandoned_reason,observation_started_at_ms,"
                "observation_deadline_at_ms,last_progress_at_ms,deadline_observed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, "c" * 64, "backup-run", "d" * 64,
                    "e" * 64, None, "f" * 64, "1" * 64, "codex-host-v1",
                    13, 1000, None, orchestration["orchestration_id"],
                    "assignment-backup", "technology_researcher", 0, "pending",
                    None, 13, 100, 13, 0,
                ),
            )
            self.ledger._connection.execute(
                "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, "qa-backup", "2" * 64, "3" * 64,
                    "4" * 64, 1, 14, b'{"accepted":true}',
                ),
            )
            self.ledger._connection.execute(
                "INSERT INTO operational_check_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, "receipt-backup", "5" * 64, "3" * 64,
                    "4" * 64, orchestration["orchestration_id"], 15, 1000,
                    None, b'{"checks":[]}',
                ),
            )
            self.ledger._connection.execute(
                "INSERT INTO tombstones VALUES(?,?,?)",
                (self.project_a_id, "historical-delete", 16),
            )

        path = self.ledger.create_sanitized_backup(
            self.project_a_id,
            file_id="complete_backup",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        body = json.loads(path.read_bytes())["body"]

        self.assertEqual(set(body["tables"]), {
            "sessions", "run_provenance", "records", "record_links",
            "stored_files", "collector_health", "consumed_grants",
            "owner_attestations", "repair_receipts", "host_run_bindings",
            "hook_ingestion", "orchestrations", "orchestration_checkpoints",
            "operational_qa", "operational_check_receipts", "tombstones",
        })
        self.assertEqual(
            {"$codex_agents_bytes_base64": "AP8="},
            body["tables"]["owner_attestations"][0]["signature"],
        )
        report = self.ledger.verify_sanitized_backup(
            self.project_a_id, file_id="complete_backup"
        )
        self.assertEqual("ok", report["isolated_restore_integrity"])
        self.assertEqual("ok", report["isolated_restore_foreign_keys"])

    def test_backup_relational_verifier_rejects_constraint_corruption(self) -> None:
        path = self.ledger.create_sanitized_backup(
            self.project_a_id,
            file_id="corrupt_backup",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        envelope = json.loads(path.read_bytes())
        envelope["body"]["tables"]["record_links"].append({
            "from_record_id": "missing-a",
            "to_record_id": "missing-b",
            "relation": "evidence",
        })
        envelope["body"]["counts"]["record_links"] = 1
        envelope["backup_sha256"] = hashlib.sha256(
            canonical_json(envelope["body"])
        ).hexdigest()
        payload = canonical_json(envelope)
        path.write_bytes(payload)
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE stored_files SET content_digest=? "
                "WHERE project_id=? AND file_id='corrupt_backup'",
                (hashlib.sha256(payload).hexdigest(), self.project_a_id),
            )

        with self.assertRaisesRegex(LedgerError, "restoration rejected"):
            self.ledger.verify_sanitized_backup(
                self.project_a_id, file_id="corrupt_backup"
            )

    def test_expiry_removes_unused_result_repair_receipts(self) -> None:
        self.ledger.issue_result_repair_receipt(
            self.project_a_id,
            "a" * 64,
            "missing_evidence",
            now_ms=10,
        )

        self.assertEqual(
            0,
            self.ledger.expire(
                self.project_a_id,
                now_ms=10 + REPAIR_RECEIPT_TTL_MS,
            ),
        )
        count = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM repair_receipts WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_record_expiry_removes_partition_backup_that_retains_content(self) -> None:
        self.ledger.append(
            self.project_a_id, event_record("expiry-backup-event"),
            producer="hook", now_ms=2,
        )
        backup = self.ledger.create_sanitized_backup(
            self.project_a_id,
            file_id="retained_content_backup",
            expires_at_ms=time.time_ns() // 1_000_000 + DEFAULT_RETENTION_MS,
        )

        deleted = self.ledger.expire(
            self.project_a_id, now_ms=2 + DEFAULT_RETENTION_MS,
        )

        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(backup.exists())
        self.assertEqual(
            0,
            self.ledger._connection.execute(
                "SELECT COUNT(*) FROM stored_files WHERE project_id=? AND kind='backup'",
                (self.project_a_id,),
            ).fetchone()[0],
        )

    def test_expiry_follows_automatically_recorded_derivation_lineage(self) -> None:
        proposed = self._candidate_cohort(self.project_a_id, now_ms=0)
        row = self.ledger._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND content_digest=?",
            (self.project_a_id, proposed["evidence_digests"][0]),
        ).fetchone()
        source = json.loads(bytes(row["content"]))
        mined = mine_evaluation_case(
            digest_key=self.state.digest_key(),
            outcome=source,
            fixture_digest=source["fixture_digest"],
            criteria_digest=DIGEST_B,
            lineage_digest=DIGEST_C,
        )
        self.ledger.append(self.project_a_id, mined, producer="evaluator", now_ms=20)
        self.ledger.append(
            self.project_a_id, proposed, producer="evaluator", now_ms=21
        )
        deleted = self.ledger.expire(
            self.project_a_id, now_ms=90 * 24 * 60 * 60 * 1000 + 1000
        )
        self.assertGreaterEqual(deleted, 82)
        with self.ledger.repository(self.project_a_id) as repository:
            self.assertEqual([], repository.records())

    def test_schema_one_migrates_and_legacy_owner_records_remain_unattested(self) -> None:
        self.ledger.close()
        connection = sqlite3.connect(self.state.database)
        connection.execute("DROP TABLE owner_attestations")
        connection.execute("DROP TABLE repair_receipts")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
        self.ledger = LearningLedger(
            self.state, client_capabilities=self.clients
        )
        version = self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in self.ledger._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        attestations = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM owner_attestations"
        ).fetchone()[0]
        self.assertEqual(6, version)
        self.assertIn("owner_attestations", tables)
        self.assertIn("repair_receipts", tables)
        self.assertEqual(0, attestations)

    def test_schema_five_refuses_migration_without_current_verified_backups(self) -> None:
        self._downgrade_fixture_to_schema_five()
        self.ledger.close()

        with self.assertRaisesRegex(LedgerError, "registered verified schema 5"):
            LearningLedger(self.state, client_capabilities=self.clients)

        connection = sqlite3.connect(self.state.database)
        try:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(host_run_bindings)"
                ).fetchall()
            }
            self.assertNotIn("binding_state", columns)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='orchestration_checkpoints'"
            ).fetchone())
        finally:
            connection.close()
        self._leave_ledger_closed()

    def test_schema_five_migration_uses_exact_backup_and_seeds_history(self) -> None:
        orchestration = self.ledger.open_orchestration(
            self.project_a_id, client_version="codex-host-v1", now_ms=10
        )
        self._downgrade_fixture_to_schema_five()
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "task_id_digest,route_digest,configuration_digest,client_version,"
                "created_at_ms,expires_at_ms,consumed_at_ms,orchestration_id,"
                "native_assignment_id,agent_type,usage_complete) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_a_id, "a" * 64, "legacy-run", "b" * 64,
                    "c" * 64, None, "d" * 64, "e" * 64, "codex-host-v1",
                    11, 1000, None, orchestration["orchestration_id"],
                    "assignment-legacy", "technology_researcher", 0,
                ),
            )
        self._register_schema_five_backup(project_id=self.project_a_id)
        self._register_schema_five_backup(
            project_id=self.project_b_id, file_id="schema5_backup_b"
        )
        self.ledger.close()

        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)

        self.assertEqual(
            6, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        checkpoint = self.ledger._connection.execute(
            "SELECT sequence,status,observed_at_ms FROM orchestration_checkpoints "
            "WHERE project_id=? AND orchestration_id=?",
            (self.project_a_id, orchestration["orchestration_id"]),
        ).fetchone()
        self.assertEqual((1, "active", 10), tuple(checkpoint))
        binding = self.ledger._connection.execute(
            "SELECT binding_state,observation_started_at_ms,"
            "observation_deadline_at_ms,last_progress_at_ms,deadline_observed "
            "FROM host_run_bindings WHERE project_id=? AND run_id='legacy-run'",
            (self.project_a_id,),
        ).fetchone()
        self.assertEqual(("pending", 11, None, 11, 0), tuple(binding))

    def test_schema_five_rejects_stale_or_nonmatching_backup(self) -> None:
        self._downgrade_fixture_to_schema_five()
        self._register_schema_five_backup(
            project_id=self.project_a_id,
            expires_at_ms=time.time_ns() // 1_000_000 - 1,
        )
        self._register_schema_five_backup(
            project_id=self.project_b_id,
            file_id="schema5_backup_b",
        )
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO collector_health VALUES(?,?,?,?)",
                (self.project_a_id, "changed_after_backup", 1, 100),
            )
        self.ledger.close()

        with self.assertRaisesRegex(LedgerError, "registered verified schema 5"):
            LearningLedger(self.state, client_capabilities=self.clients)
        self._leave_ledger_closed()

    def test_schema_five_migration_rolls_back_an_injected_schema_failure(self) -> None:
        self._downgrade_fixture_to_schema_five()
        with self.ledger._connection:
            self.ledger._connection.execute(
                "CREATE VIEW orchestration_checkpoints AS SELECT 1 AS marker"
            )
        self._register_schema_five_backup(project_id=self.project_a_id)
        self._register_schema_five_backup(
            project_id=self.project_b_id, file_id="schema5_backup_b"
        )
        self.ledger.close()

        with self.assertRaises(sqlite3.OperationalError):
            LearningLedger(self.state, client_capabilities=self.clients)

        connection = sqlite3.connect(self.state.database)
        try:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='orchestrations'"
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='orchestrations_v5'"
            ).fetchone())
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(host_run_bindings)"
                ).fetchall()
            }
            self.assertNotIn("binding_state", columns)
        finally:
            connection.close()
        self._leave_ledger_closed()

    def test_learning_cycle_mines_cases_and_generates_only_inert_dominant_proposal(self) -> None:
        self._admit_reviewed_cases(self.project_a_id, heldout_cases())

        def observed(
            identifier: str, model: str, tokens: tuple[int, int], fixture: str
        ) -> dict:
            record = verification_outcome(identifier)
            record.update(champion_configuration())
            record.update(
                task_slice="coding-medium-low", fixture_digest=fixture, model=model,
                usage=full_usage(
                    input_tokens=tokens[0], output_tokens=tokens[1], model_calls=2
                ),
            )
            configuration = {
                key: record[key]
                for key in (
                    "model", "effort", "role", "topology", "max_parallel",
                    "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                    "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                    "client_digest",
                )
            }
            record["configuration_digest"] = content_digest(configuration)
            return record

        for index in range(20):
            fixture = f"{index + 2000:064x}"
            champion = observed(
                f"observed-terra-{index}", "gpt-5.6-terra", (140, 60), fixture
            )
            challenger = observed(
                f"observed-luna-{index}", "gpt-5.6-luna", (70, 30), fixture
            )
            champion_qa = content_digest({"qa": champion["record_id"]})
            challenger_qa = content_digest({"qa": challenger["record_id"]})
            champion["evidence_digests"] = [DIGEST_A, champion_qa]
            challenger["evidence_digests"] = [DIGEST_A, challenger_qa]
            self._admit_outcome(self.project_a_id, champion, now_ms=100 + index * 2)
            self._admit_outcome(
                self.project_a_id, challenger, now_ms=101 + index * 2
            )
            self._accept_outcome_qa(
                self.project_a_id, champion, champion_qa, now_ms=100 + index * 2
            )
            self._accept_outcome_qa(
                self.project_a_id,
                challenger,
                challenger_qa,
                now_ms=101 + index * 2,
            )
        first = self.ledger.run_learning_cycle(self.project_a_id, as_of_ms=200)
        self.assertEqual(40, len(first["mined_case_ids"]))
        self.assertEqual(1, len(first["proposed_candidate_digests"]))
        proposed, transitions = self.ledger.lifecycle_records(
            self.project_a_id, first["proposed_candidate_digests"][0]
        )
        self.assertEqual("project", proposed["scope"])
        self.assertEqual("gpt-5.6-terra", proposed["champion_configuration"]["model"])
        self.assertEqual("gpt-5.6-luna", proposed["candidate_configuration"]["model"])
        self.assertEqual([], transitions)
        second = self.ledger.run_learning_cycle(self.project_a_id, as_of_ms=201)
        self.assertEqual([], second["mined_case_ids"])
        self.assertEqual([], second["proposed_candidate_digests"])

    def test_learning_cycle_excludes_frozen_case_lineage_from_proposals(self) -> None:
        def observed(identifier: str, model: str, tokens: tuple[int, int]) -> dict:
            record = verification_outcome(identifier)
            record.update(
                task_slice="coding-medium-low",
                model=model,
                effort="medium",
                role="codex_agents_implementation_worker",
                usage=full_usage(
                    input_tokens=tokens[0], output_tokens=tokens[1], model_calls=2
                ),
            )
            configuration = {
                key: record[key]
                for key in (
                    "model", "effort", "role", "topology", "max_parallel",
                    "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                    "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                    "client_digest",
                )
            }
            record["configuration_digest"] = content_digest(configuration)
            return record

        self._admit_outcome(
            self.project_a_id, observed("heldout-terra", "gpt-5.6-terra", (140, 60)),
            now_ms=100,
        )
        self._admit_outcome(
            self.project_a_id, observed("heldout-luna", "gpt-5.6-luna", (70, 30)),
            now_ms=101,
        )
        mined = self.ledger.run_learning_cycle(self.project_a_id, as_of_ms=102)
        self.assertEqual([], mined["proposed_candidate_digests"])
        with self.ledger.repository(self.project_a_id) as repository:
            development = [
                repository.record(record_id, "evaluation_case")
                for record_id in mined["mined_case_ids"]
            ]
        for index, case in enumerate(development):
            reviewed = review_evaluation_case(
                case,
                digest_key=self.state.digest_key(),
                review_digest=f"{index + 800:064x}",
                similarity_group_digest=f"{index + 900:064x}",
                destination="promotion_holdout",
                freeze=True,
            )
            self.ledger.append(
                self.project_a_id, reviewed, producer="evaluator", now_ms=103 + index
            )
        self._admit_reviewed_cases(self.project_a_id, heldout_cases())
        result_value = self.ledger.run_learning_cycle(self.project_a_id, as_of_ms=200)
        self.assertEqual([], result_value["proposed_candidate_digests"])

    def test_expiry_fails_before_deletion_while_external_reader_is_open(self) -> None:
        self.ledger.append(
            self.project_a_id, event_record("evt_reader"), producer="hook", now_ms=0
        )
        reader = sqlite3.connect(self.state.database)
        reader.execute("BEGIN")
        self.assertEqual(1, reader.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        try:
            with self.assertRaisesRegex(LedgerError, "readers"):
                self.ledger.expire(
                    self.project_a_id, now_ms=90 * 24 * 60 * 60 * 1000
                )
            with self.ledger.repository(self.project_a_id) as repository:
                self.assertEqual(["evt_reader"], [r["record_id"] for r in repository.records()])
        finally:
            reader.close()
        self.assertEqual(
            1,
            self.ledger.expire(
                self.project_a_id, now_ms=90 * 24 * 60 * 60 * 1000
            ),
        )

    def test_project_delete_requires_closed_readers_and_leaves_only_tombstone(self) -> None:
        with self.ledger._connection:
            self.ledger._connection.execute(
                "INSERT INTO host_run_bindings("
                "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,"
                "expires_at_ms,consumed_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    self.project_a_id, "a" * 64, "run-delete", "b" * 64,
                    "c" * 64, "d" * 64, "e" * 64, "codex-host-v1", 1, 100,
                ),
            )
        with self.ledger.repository(self.project_a_id):
            with self.assertRaisesRegex(LedgerError, "closed scoped readers"):
                self.ledger.delete_project(self.project_a_id, now_ms=10)
        deletion_id = self.ledger.delete_project(self.project_a_id, now_ms=10)
        self.assertTrue(deletion_id.startswith("del_"))
        with self.assertRaisesRegex(LedgerError, "not active"):
            with self.ledger.repository(self.project_a_id):
                pass
        self.assertEqual([], self.ledger.storage_residuals())
        self.assertEqual(
            0,
            self.ledger._connection.execute(
                "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=?",
                (self.project_a_id,),
            ).fetchone()[0],
        )

    def test_typed_artifact_export_and_backup_are_partitioned_and_deleted(self) -> None:
        event = event_record("evt_file")
        self.ledger.append(self.project_a_id, event, producer="hook", now_ms=2)
        artifact = self.ledger.write_sanitized_record_file(
            self.project_a_id, file_id="file_artifact", kind="artifact", record=event
        )
        export_record = {
            "schema_version": 1,
            "record_type": "learning_export",
            "record_id": "export_1",
            "export_digest": DIGEST_A,
            "scope": "project",
            "candidate_digest": DIGEST_B,
            "artifact_id": "artifact_1",
            "schema_digest": DIGEST_C,
        }
        self.ledger.append(self.project_a_id, export_record, producer="host", now_ms=3)
        exported = self.ledger.write_sanitized_record_file(
            self.project_a_id,
            file_id="file_export",
            kind="export",
            record=export_record,
        )
        backup = self.ledger.create_sanitized_backup(
            self.project_a_id,
            file_id="file_backup",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        self.assertTrue(all(path.exists() for path in (artifact, exported, backup)))
        with self.assertRaisesRegex(LedgerError, "must already exist"):
            self.ledger.write_sanitized_record_file(
                self.project_b_id,
                file_id="cross_project",
                kind="artifact",
                record=event,
            )
        self.ledger.delete_project(self.project_a_id, now_ms=10)
        for root in (self.state.artifacts, self.state.exports, self.state.backups):
            self.assertFalse((root / self.project_a_id).exists())

    def test_interrupted_sqlite_transaction_recovers_without_partial_row(self) -> None:
        program = (
            "import os,sqlite3,sys; "
            "c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA foreign_keys=ON'); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute(\"INSERT INTO collector_health VALUES(?,?,1,?)\",(sys.argv[2],'collector_error',5)); "
            "os._exit(23)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program, str(self.state.database), self.project_a_id],
            check=False,
        )
        self.assertEqual(23, completed.returncode)
        count = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM collector_health WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_restart_removes_crash_orphaned_managed_file(self) -> None:
        partition = self.state.artifacts / self.project_a_id
        partition.mkdir(mode=0o700)
        orphan = partition / "orphan.json"
        orphan.write_text("sanitized-but-unregistered")
        orphan.chmod(0o600)
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertFalse(orphan.exists())

    def test_restart_removes_registration_for_a_missing_managed_file(self) -> None:
        event = event_record("missing_registered_file")
        self.ledger.append(self.project_a_id, event, producer="hook", now_ms=2)
        path = self.ledger.write_sanitized_record_file(
            self.project_a_id,
            file_id="missing_registered_file",
            kind="artifact",
            record=event,
        )
        path.unlink()
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        count = self.ledger._connection.execute(
            "SELECT COUNT(*) FROM stored_files WHERE project_id=?",
            (self.project_a_id,),
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_expiry_preserves_promoted_route_and_connected_audit(self) -> None:
        evidence = verification_outcome("promoted_evidence")
        evidence.update(
            **champion_configuration(),
            task_slice="coding-medium-low",
        )
        evidence["configuration_digest"] = content_digest(champion_configuration())
        self._admit_outcome(self.project_a_id, evidence, now_ms=0, producer="host")
        proposed = self._candidate_cohort(self.project_a_id, now_ms=100)
        self.ledger.append(self.project_a_id, proposed, producer="evaluator", now_ms=1)
        lifecycle = CandidateLifecycle(
            candidate=proposed,
            champion_digest=proposed["base_digest"],
            project_id=self.project_a_id,
            authority=OwnerGrantAuthority(self.state.owner_allowed_signers()),
            digest_key=self.state.digest_key(),
        )
        promoted = lifecycle._audit(
            action="promoted",
            now_ms=2,
            grant_id=evidence["record_id"],
            evidence_digest=content_digest(evidence),
        )
        with self.ledger._lock, self.ledger._connection:
            self.ledger._append_checked_locked(
                self.project_a_id, promoted, producer="lifecycle", timestamp=2
            )
        deleted = self.ledger.expire(
            self.project_a_id, now_ms=90 * 24 * 60 * 60 * 1000
        )
        self.assertEqual(0, deleted)
        with self.ledger.repository(self.project_a_id) as repository:
            retained = {record["record_id"] for record in repository.records()}
            self.assertTrue(
                {
                    proposed["record_id"],
                    promoted["record_id"],
                }.issubset(retained)
            )

    def test_grant_consumption_is_durable_and_atomic(self) -> None:
        candidate_evidence = verification_outcome("candidate-evidence")
        candidate_evidence.update(
            **champion_configuration(),
            task_slice="coding-medium-low",
        )
        candidate_evidence["configuration_digest"] = content_digest(
            champion_configuration()
        )
        self._admit_outcome(self.project_a_id, candidate_evidence, now_ms=9)
        proposed = self._candidate_cohort(self.project_a_id, now_ms=100)
        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        lifecycle = CandidateLifecycle(
            candidate=proposed,
            champion_digest=proposed["base_digest"],
            project_id=self.project_a_id,
            authority=authority,
            digest_key=self.state.digest_key(),
        )
        cases, trials = matched_trials(candidate_record=proposed)
        self._admit_reviewed_cases(self.project_a_id, cases)
        self.ledger.append(self.project_a_id, proposed, producer="evaluator", now_ms=10)
        assigned = evaluation_assignment(cases, proposed)
        self.ledger.append(
            self.project_a_id, assigned, producer="evaluator", now_ms=17
        )
        by_case = {
            (item["case_id"], item["cohort"]): item
            for item in assigned["trial_plan"]
        }
        for case in cases:
            for cohort in ("champion", "candidate"):
                planned = by_case[(case["record_id"], cohort)]
                self._admit_outcome(
                    self.project_a_id,
                    trial_outcome(
                        case, cohort, proposed, trial_id=planned["trial_id"]
                    ),
                    now_ms=18,
                )
        for trial_record in trials:
            self.ledger.append(
                self.project_a_id, trial_record, producer="evaluator", now_ms=18
            )
        review = self._admit_evidence_review(
            self.project_a_id,
            [content_digest(item) for item in trials],
            now_ms=18,
        )
        canary_case = deepcopy(cases[0])
        canary_case.update(
            record_id="durable-canary-case",
            source_outcome_id="durable-canary-source",
            supersedes_case_id="durable-canary-development",
            corpus_state="canary",
            fixture_digest="d" * 64,
            criteria_digest="e" * 64,
            lineage_digest="f" * 64,
            similarity_group_digest="9" * 64,
        )
        self._admit_reviewed_cases(self.project_a_id, [canary_case])
        canary_digest = content_digest(canary_case)
        independent_review_digest = content_digest(review)
        comparison = compare_candidate(
            candidate=proposed,
            trials=trials,
            cases=cases,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=independent_review_digest,
            evaluation_assignment_digest=content_digest(assigned),
            bootstrap_samples=1000,
        )
        review_digest = content_digest(comparison)
        self.ledger.append(self.project_a_id, comparison, producer="evaluator", now_ms=19)
        evaluated = lifecycle.mark_evaluated(
            comparison, evidence_digest=review_digest, now_ms=20
        )
        self.ledger.append(
            self.project_a_id, evaluated, producer="lifecycle", now_ms=20
        )
        grant = sign_owner_grant(
            authority,
            self.owner_private_key,
            digest_key=self.state.digest_key(),
            kind="canary",
            project_id=self.project_a_id,
            candidate_digest_value=proposed["candidate_digest"],
            champion_digest=proposed["base_digest"],
            review_digest=review_digest,
            bounds_digest=content_digest([canary_digest]),
            issued_at_ms=int(time.time() * 1000) - 1_000,
            expires_at_ms=int(time.time() * 1000) + 60_000,
            nonce=b"c" * 32,
        )
        transition = lifecycle.start_canary(
            grant=grant,
            case_digests=[canary_digest],
            evidence_digest=review_digest,
            now_ms=int(time.time() * 1000),
            commit=lambda signed, promotion: self.ledger.commit_granted_promotion(
                self.project_a_id, grant=signed, promotion=promotion
            ),
        )
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        substituted = deepcopy(transition)
        substituted["record_id"] = "promo_substituted"
        with self.assertRaisesRegex(LedgerError, "already consumed"):
            self.ledger.commit_granted_promotion(
                self.project_a_id, grant=grant, promotion=substituted, now_ms=23
            )

    def test_authorized_lifecycle_actions_cannot_bypass_grant_consumption(self) -> None:
        proposed = candidate()
        forged = {
            "schema_version": 1,
            "record_type": "promotion",
            "record_id": "promotion_forged",
            "candidate_digest": proposed["candidate_digest"],
            "champion_digest": proposed["base_digest"],
            "action": "promoted",
            "observed_at_ms": 10,
            "grant_id": "grant:forged",
            "evidence_digest": DIGEST_A,
            "canary_case_digests": [DIGEST_B],
        }
        with self.assertRaisesRegex(LedgerError, "atomic grant"):
            self.ledger.append(
                self.project_a_id, forged, producer="lifecycle", now_ms=10
            )

    def test_forward_database_schema_is_rejected(self) -> None:
        self.ledger.close()
        connection = sqlite3.connect(self.state.database)
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        with self.assertRaisesRegex(LedgerError, "newer"):
            LearningLedger(self.state)
        self.ledger = LearningLedger.__new__(LearningLedger)
        self.ledger.close = lambda: None  # type: ignore[method-assign]

    def test_collector_serializes_concurrent_clients(self) -> None:
        server = CollectorServer(self.ledger)
        server.start()
        errors: list[Exception] = []

        def send(index: int) -> None:
            try:
                client = CollectorClient(self.state.socket, timeout=2)
                session = f"session-{index}"
                token = client.bind(
                    self.project_a, session, "hook", "codex-hooks-v1"
                )
                record = event_record(f"evt_{index}", observed_at_ms=100 + index)
                record["run_id"] = external_id(
                    self.state.digest_key(),
                    "run",
                    f"codex-hooks-v1\x00{session}\x00root",
                )
                client.ingest(token, record)
                with self.assertRaisesRegex(CollectorError, "binding_invalid"):
                    client.ingest(token, event_record(f"evt_reuse_{index}"))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=send, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        server.stop()
        self.assertEqual([], errors)
        with self.ledger.repository(self.project_a_id) as repository:
            records = repository.records()
        self.assertEqual(16, len(records))
        self.assertEqual(list(range(1, 17)), sorted(item["_source_sequence"] for item in records))

    def test_collector_rejects_forged_binding_and_unavailability_is_bounded(self) -> None:
        server = CollectorServer(self.ledger)
        server.start()
        client = CollectorClient(self.state.socket, timeout=0.2)
        health = client.health(self.project_a)
        self.assertEqual(6, health["schema_version"])
        self.assertEqual(PACKAGE_VERSION, health["runtime_version"])
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, health["runtime_digest"])
        self.assertIsInstance(health["started_at_ms"], int)
        self.assertEqual(
            "awaiting_hook_review_or_first_event",
            health["ingestion"]["freshness"]["state"],
        )
        with self.assertRaisesRegex(CollectorError, "binding_invalid"):
            client.ingest("bnd_forged", event_record())
        with self.assertRaisesRegex(CollectorError, "client_unvalidated"):
            client.bind(self.project_a, "session", "hook", "unvalidated-client")
        with self.assertRaisesRegex(CollectorError, "producer_rejected"):
            client.bind(self.project_a, "session", "owner", "codex-cli-0.153.0")
        with self.assertRaisesRegex(CollectorError, "producer_rejected"):
            client.bind(self.project_a, "session", "hook", "codex-cli-0.153.0")
        with self.assertRaisesRegex(CollectorError, "producer_rejected"):
            client.bind(self.project_a, "session", "app_server", "codex-hooks-v1")
        token = client.bind(
            self.project_a, "session", "hook", "codex-hooks-v1"
        )
        with self.assertRaisesRegex(CollectorError, "provenance_mismatch"):
            client.ingest(token, event_record("evt_wrong_run"))
        token = client.bind(
            self.project_a, "coverage-session", "hook", "codex-hooks-v1"
        )
        inflated = event_record("evt_inflated")
        inflated["run_id"] = external_id(
            self.state.digest_key(),
            "run",
            "codex-hooks-v1\x00coverage-session\x00root",
        )
        inflated["coverage"] = "end_to_end_complete"
        with self.assertRaisesRegex(CollectorError, "capability_mismatch"):
            client.ingest(token, inflated)
        server.stop()
        started = time.monotonic()
        with self.assertRaisesRegex(CollectorError, "unavailable"):
            client.bind(self.project_a, "session", "hook", "codex-hooks-v1")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_client_rejects_a_conflicting_build_without_replacement(self) -> None:
        client = CollectorClient(self.state.socket)
        with mock.patch.object(
            client,
            "_request",
            return_value={
                "ok": True,
                "schema_version": 6,
                "runtime_version": PACKAGE_VERSION,
                "runtime_digest": "0" * 64,
            },
        ):
            with self.assertRaisesRegex(CollectorBuildConflictError, "build conflicts"):
                client.health()

    def test_collector_authorization_is_owner_only_and_exact(self) -> None:
        self.assertEqual(
            {
                "schema_version": 1,
                "runtime_version": PACKAGE_VERSION,
                "runtime_digest": COLLECTOR_RUNTIME_DIGEST,
            },
            authorized_collector_runtime(self.state),
        )
        self.assertEqual(
            0o600, os.stat(self.state.collector_authorization).st_mode & 0o777
        )
        self.assertEqual(0o700, os.stat(self.state.root).st_mode & 0o777)
        before = self.state.collector_authorization.read_bytes()
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.assertEqual(before, self.state.collector_authorization.read_bytes())

    def test_missing_or_spoofed_authorization_cannot_start_collector(self) -> None:
        self.state.collector_authorization.unlink()
        with mock.patch(
            "codex_agents.learning_store.subprocess.Popen"
        ) as popen, self.assertRaisesRegex(CollectorError, "not authorized"):
            ensure_collector(
                self.state,
                ORCHESTRATION_ROOT / "cli.py",
                project_root=self.project_a,
            )
        popen.assert_not_called()
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        with (
            mock.patch(
                "codex_agents.learning_store.PACKAGE_VERSION", "99.0.0"
            ),
            mock.patch(
                "codex_agents.learning_store.CollectorClient.health"
            ) as health,
            mock.patch(
                "codex_agents.learning_store.subprocess.Popen"
            ) as popen,
            self.assertRaisesRegex(
                CollectorError, "differs from the owner-authorized runtime"
            ),
        ):
            ensure_collector(
                self.state,
                ORCHESTRATION_ROOT / "cli.py",
                project_root=self.project_a,
            )
        health.assert_not_called()
        popen.assert_not_called()

    def test_upgrade_quiescence_blocks_restart_until_owner_registration(self) -> None:
        authorization = json.loads(self.state.collector_authorization.read_bytes())
        authorization["quiesced_for_upgrade"] = True
        self.state.collector_authorization.write_bytes(
            canonical_json(authorization) + b"\n"
        )
        with (
            mock.patch(
                "codex_agents.learning_store.subprocess.Popen"
            ) as popen,
            self.assertRaisesRegex(CollectorError, "quiesced"),
        ):
            ensure_collector(
                self.state,
                ORCHESTRATION_ROOT / "cli.py",
                project_root=self.project_a,
            )
        popen.assert_not_called()

        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.assertNotIn(
            "quiesced_for_upgrade",
            json.loads(self.state.collector_authorization.read_bytes()),
        )
        self.assertEqual(PACKAGE_VERSION, authorized_collector_runtime(self.state)["runtime_version"])

    def test_registration_rejects_modified_installed_package(self) -> None:
        bundle = self._installed_bundle("modified-package")
        cli_path = bundle / ".codex" / "orchestration" / "cli.py"
        cli_path.write_bytes(cli_path.read_bytes() + b"\n# modified\n")
        state_root = Path(self.temporary.name) / "other-state"
        completed = subprocess.run(
            [
                sys.executable,
                str(cli_path),
                "learning-register",
                "--state-root",
                str(state_root),
                "--project-root",
                str(self.project_a),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertFalse(json.loads(completed.stdout)["ready"])
        self.assertFalse(
            state_root.joinpath("collector-authorization.json").exists()
        )

    def test_spoofed_project_manifest_cannot_replace_active_collector(self) -> None:
        bundle = self._installed_bundle("spoofed-package", version="99.0.0")
        before_authorization = self.state.collector_authorization.read_bytes()
        server = CollectorServer(self.ledger, client_capabilities=self.clients)
        server.start()
        client = CollectorClient(self.state.socket)
        try:
            before = client.health(self.project_a)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(bundle / ".codex" / "orchestration" / "cli.py"),
                    "learning-status",
                    "--state-root",
                    str(self.state.root),
                    "--project-root",
                    str(self.project_a),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertFalse(json.loads(completed.stdout)["ready"])
            hook_payload = json.loads(
                (
                    REPOSITORY_ROOT
                    / "tests"
                    / "fixtures"
                    / "learning"
                    / "hook-pre-tool.json"
                ).read_text()
            )
            hook_payload.update(cwd=str(self.project_a))
            hook = subprocess.run(
                [
                    sys.executable,
                    str(
                        bundle
                        / ".codex"
                        / "orchestration"
                        / "learning_hook.py"
                    ),
                    "--state-root",
                    str(self.state.root),
                    "--client-version",
                    "codex-hooks-v1",
                ],
                input=json.dumps(hook_payload),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, hook.returncode, hook.stderr)
            after = client.health(self.project_a)
            self.assertEqual(before["started_at_ms"], after["started_at_ms"])
            self.assertEqual(PACKAGE_VERSION, after["runtime_version"])
            self.assertEqual(COLLECTOR_RUNTIME_DIGEST, after["runtime_digest"])
            self.assertEqual(
                before_authorization,
                self.state.collector_authorization.read_bytes(),
            )
        finally:
            server.stop()

    def test_only_owner_registration_maintenance_may_replace_older_runtime(
        self,
    ) -> None:
        with (
            mock.patch(
                "codex_agents.learning_store.CollectorClient.health",
                side_effect=CollectorUpgradeRequired("older runtime"),
            ) as health,
            mock.patch(
                "codex_agents.learning_store.CollectorClient.shutdown"
            ) as shutdown,
            self.assertRaises(CollectorUpgradeRequired),
        ):
            CollectorMaintenance.acquire(
                self.state,
                ORCHESTRATION_ROOT / "cli.py",
                self.project_a,
            )
        health.assert_called_once_with()
        shutdown.assert_not_called()

        healthy = {
            "schema_version": 6,
            "runtime_version": PACKAGE_VERSION,
            "runtime_digest": COLLECTOR_RUNTIME_DIGEST,
        }
        with (
            mock.patch(
                "codex_agents.learning_store.CollectorClient.health",
                side_effect=CollectorUpgradeRequired("older runtime"),
            ),
            mock.patch(
                "codex_agents.learning_store.CollectorClient.shutdown"
            ) as shutdown,
            mock.patch(
                "codex_agents.learning_store._wait_for_collector_shutdown",
                return_value=True,
            ),
            mock.patch(
                "codex_agents.learning_store._start_collector_locked",
                return_value=healthy,
            ) as start,
        ):
            maintenance = CollectorMaintenance.acquire(
                self.state,
                ORCHESTRATION_ROOT / "cli.py",
                self.project_a,
                owner_registration=True,
            )
            maintenance.resume()
        shutdown.assert_called_once_with()
        start.assert_called_once_with(
            self.state,
            ORCHESTRATION_ROOT / "cli.py",
            project_root=self.project_a,
        )

    def test_client_orders_collector_versions_without_downgrade(self) -> None:
        client = CollectorClient(self.state.socket)
        with mock.patch.object(
            client,
            "_request",
            return_value={
                "ok": True,
                "schema_version": 3,
                "runtime_digest": "0" * 64,
            },
        ):
            with self.assertRaises(CollectorUpgradeRequired):
                client.health()
        with mock.patch.object(
            client,
            "_request",
            return_value={
                "ok": True,
                "schema_version": 6,
                "runtime_version": "99.0.0",
                "runtime_digest": "0" * 64,
            },
        ):
            with self.assertRaises(CollectorNewerVersionError):
                client.health()

    def test_newer_collector_is_never_replaced(self) -> None:
        with (
            mock.patch(
                "codex_agents.learning_store.CollectorClient.health",
                side_effect=CollectorNewerVersionError("newer runtime"),
            ),
            mock.patch("codex_agents.learning_store.subprocess.Popen") as popen,
        ):
            with self.assertRaises(CollectorNewerVersionError):
                _start_collector_locked(
                    self.state,
                    ORCHESTRATION_ROOT / "cli.py",
                    project_root=self.project_a,
                )
        popen.assert_not_called()

    def test_detached_collector_receives_no_inherited_environment(self) -> None:
        healthy = {
            "ok": True,
            "schema_version": 6,
            "runtime_version": PACKAGE_VERSION,
            "runtime_digest": COLLECTOR_RUNTIME_DIGEST,
            "started_at_ms": 1,
        }
        with (
            mock.patch(
                "codex_agents.learning_store.CollectorClient.health",
                side_effect=[CollectorError("unavailable"), healthy],
            ),
            mock.patch("codex_agents.learning_store.subprocess.Popen") as popen,
        ):
            self.assertEqual(
                healthy,
                _start_collector_locked(
                    self.state,
                    ORCHESTRATION_ROOT / "cli.py",
                    project_root=self.project_a,
                ),
            )
        self.assertEqual({"PATH": os.defpath}, popen.call_args.kwargs["env"])

    def test_second_collector_cannot_replace_the_active_socket(self) -> None:
        first = CollectorServer(self.ledger)
        second = CollectorServer(self.ledger)
        first.start()
        try:
            with self.assertRaisesRegex(CollectorError, "another collector"):
                second.start()
            self.assertTrue(self.state.socket.exists())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "cli.py"),
                    "learning-analyze",
                    "--state-root",
                    str(self.state.root),
                    "--project-root",
                    str(self.project_a),
                    "--as-of-ms",
                    "10",
                    "--max-age-ms",
                    "10",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, completed.returncode)
            self.assertIn("collector did not stop for maintenance", completed.stdout)
        finally:
            first.stop()

    def test_app_server_cli_normalizes_and_ingests_through_collector(self) -> None:
        messages = json.loads(
            (
                REPOSITORY_ROOT
                / "tests"
                / "fixtures"
                / "learning"
                / "app-server-events.json"
            ).read_text()
        )
        message_path = Path(self.temporary.name) / "message.json"
        message_path.write_text(json.dumps(messages[0]))
        server = CollectorServer(self.ledger)
        server.start()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "cli.py"),
                    "learning-ingest-app",
                    "--state-root",
                    str(self.state.root),
                    "--project-root",
                    str(self.project_a),
                    "--message",
                    str(message_path),
                    "--client-version",
                    "codex-cli-0.153.0",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            server.stop()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["stored"])
        with self.ledger.repository(self.project_a_id) as repository:
            records = repository.records("event")
        self.assertEqual("turn_started", records[0]["event_code"])


if __name__ == "__main__":
    unittest.main()
