from __future__ import annotations

import json
import base64
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT

from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import (
    candidate_base_digest,
    canonical_json,
    content_digest,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_store import (
    CollectorClient,
    CollectorError,
    CollectorMaintenance,
    CollectorRestartError,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    authorized_collector_runtime,
)
from cli import _RestartingLedger, _close_ledger
from codex_agents.learning_engine import generate_candidate, runtime_fixture_digest
from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    GRADER_CONFIG,
    assignment,
    champion_configuration,
    event_record,
    full_usage,
    generate_test_owner_key,
    sign_owner_payload,
    result,
    routing_fields,
    verification_outcome,
)


class LearningCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        self.project = base / "project"
        self.project.mkdir()
        state = LearningState(self.state_root)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, owner_public_key = generate_test_owner_key(base)
        state.install_owner_public_key(owner_public_key)
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        self.project_id = ledger.register_project(self.project, now_ms=1)
        self.fixture_task = {
            "id": "cli-fixture", "objective": "Verify one fixed fixture",
            "family": "verification", "complexity": "low", "risk": "low",
            **routing_fields(operation="review"),
            "meaningful": True, "deterministic_validation": True,
            "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0,
            "failure_events": [],
        }
        self.outcome = verification_outcome("cli_outcome")
        self.outcome["task_slice"] = (
            "verification-low-low-review-known-repository-executable"
        )
        self.outcome["fixture_digest"] = runtime_fixture_digest(
            state.digest_key(), self.fixture_task
        )
        event = event_record("cli_event")
        event["run_id"] = self.outcome["run_id"]
        event["assignment_id"] = self.outcome["assignment_id"]
        event["model"] = self.outcome["model"]
        event["effort"] = self.outcome["effort"]
        event["role"] = self.outcome["role"]
        event["task_slice"] = self.outcome["task_slice"]
        event["topology"] = self.outcome["topology"]
        event["configuration_digest"] = self.outcome["configuration_digest"]
        event["coverage"] = self.outcome["coverage"]
        event["usage"] = self.outcome["usage"]
        event["event_code"] = "item_completed"
        event["status"] = "completed"
        event["item_digest"] = self.outcome["evidence_digests"][0]
        self._admit_fixture_outcome(
            ledger, event=event, outcome=self.outcome, producer="evaluator", now_ms=1
        )
        verifier = verification_outcome("cli_verifier")
        verifier_event = event_record("cli_verifier_event")
        verifier_event["run_id"] = verifier["run_id"]
        verifier_event["assignment_id"] = verifier["assignment_id"]
        verifier_event["model"] = verifier["model"]
        verifier_event["effort"] = verifier["effort"]
        verifier_event["role"] = verifier["role"]
        verifier_event["task_slice"] = verifier["task_slice"]
        verifier_event["topology"] = verifier["topology"]
        verifier_event["configuration_digest"] = verifier["configuration_digest"]
        verifier_event["coverage"] = verifier["coverage"]
        verifier_event["usage"] = verifier["usage"]
        verifier_event["event_code"] = "item_completed"
        verifier_event["status"] = "completed"
        verifier_event["item_digest"] = verifier["evidence_digests"][0]
        self._admit_fixture_outcome(
            ledger, event=verifier_event, outcome=verifier, producer="verifier", now_ms=1
        )
        ledger.close()
        self.verifier = verifier

    def _admit_fixture_outcome(
        self,
        ledger: LearningLedger,
        *,
        event: dict,
        outcome: dict,
        producer: str,
        now_ms: int,
    ) -> None:
        with ledger._lock, ledger._connection:
            ledger._append_checked_locked(
                self.project_id, event, producer="host", timestamp=now_ms
            )
            ledger._validate_evidence_links_locked(self.project_id, outcome)
            ledger._append_checked_locked(
                self.project_id, outcome, producer=producer, timestamp=now_ms
            )

    def _seed_candidate_cli_cohort(self) -> tuple[dict, list[tuple]]:
        champion = champion_configuration()
        before = {"model": champion["model"], "effort": champion["effort"]}
        after = {"model": "gpt-6-luna", "effort": "high"}
        spec = {
            "scope": "project",
            "task_slice": "coding-medium-low",
            "target": "model_effort",
            "effect": "optimize",
            "base_digest": candidate_base_digest(champion),
            "change_manifest": [{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(before),
                "after_digest": content_digest(after),
                "before_value": before,
                "after_value": after,
            }],
            "champion_configuration": champion,
        }
        provisional = generate_candidate(
            digest_key=b"c" * 32,
            **spec,
            evidence_digests=[DIGEST_A],
        )
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        qa_rows: list[tuple] = []
        try:
            for index in range(20):
                fixture = f"{index + 3000:064x}"
                for cohort, configuration in (
                    ("champion", provisional["champion_configuration"]),
                    ("candidate", provisional["candidate_configuration"]),
                ):
                    identifier = f"cli-cohort-{index}-{cohort}"
                    qa_digest = content_digest({"qa": identifier})
                    outcome = verification_outcome(identifier)
                    outcome.update(
                        **configuration,
                        task_slice=spec["task_slice"],
                        fixture_digest=fixture,
                        observed_at_ms=100 + index,
                        evidence_digests=[DIGEST_A, qa_digest],
                    )
                    outcome["configuration_digest"] = content_digest(configuration)
                    event = event_record(f"event-{identifier}")
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
                        item_digest=DIGEST_A,
                    )
                    self._admit_fixture_outcome(
                        ledger,
                        event=event,
                        outcome=outcome,
                        producer="verifier",
                        now_ms=100 + index,
                    )
                    qa_rows.append(
                        (
                            self.project_id,
                            f"qa:{identifier}",
                            qa_digest,
                            content_digest(outcome),
                            DIGEST_B,
                            1,
                            100 + index,
                            canonical_json({"accepted": True}),
                        )
                    )
        finally:
            ledger.close()
        return spec, qa_rows

    def tearDown(self) -> None:
        try:
            if self.state_root.exists():
                self._stop_collector()
        finally:
            self.temporary.cleanup()

    def _write(self, name: str, value: object) -> Path:
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps(value))
        return path

    def _run(self, command: str, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                command,
                "--state-root",
                str(self.state_root),
                "--project-root",
                str(self.project),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _run_state(self, command: str, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                command,
                "--state-root",
                str(self.state_root),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _run_raw(self, command: str, *arguments: str, expected: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), command, *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _sign_prepared(self, report: dict, name: str) -> Path:
        return sign_owner_payload(
            self.owner_private_key,
            base64.b64decode(report["signing_payload_b64"], validate=True),
            name,
        )

    def test_committed_ledger_operation_reports_restart_failure_explicitly(self) -> None:
        ledger = mock.Mock()
        maintenance = mock.Mock()
        maintenance.resume.side_effect = CollectorError("restart unavailable")
        managed = _RestartingLedger(ledger, maintenance)

        with self.assertRaisesRegex(
            CollectorRestartError,
            "ledger operation completed; collector restart failed",
        ):
            managed.close(operation_completed=True)

        ledger.close.assert_called_once_with()
        maintenance.resume.assert_called_once_with()

    def test_operation_error_is_not_masked_by_restart_failure(self) -> None:
        ledger = mock.Mock()
        maintenance = mock.Mock()
        maintenance.resume.side_effect = CollectorError("restart unavailable")
        managed = _RestartingLedger(ledger, maintenance)

        with self.assertRaisesRegex(ContractError, "operation failed"):
            try:
                raise ContractError("operation failed")
            finally:
                _close_ledger(managed)

    def _stop_collector(self) -> None:
        client = CollectorClient(self.state_root / "collector.sock", timeout=0.1)
        try:
            client.shutdown()
        except CollectorError:
            maintenance = CollectorMaintenance.acquire(
                LearningState(self.state_root, create=False),
                ORCHESTRATION_ROOT / "cli.py",
                self.project,
            )
            maintenance.resume()
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                client.health()
            except CollectorError:
                maintenance = CollectorMaintenance.acquire(
                    LearningState(self.state_root, create=False),
                    ORCHESTRATION_ROOT / "cli.py",
                    self.project,
                )
                maintenance.resume()
                return
            time.sleep(0.02)
        self.fail("collector did not stop")

    def test_status_recovers_collector_and_direct_command_restarts_it(self) -> None:
        try:
            first = self._run("learning-status")
            self.assertTrue(first["ready"])
            self.assertTrue(first["active"])
            self.assertEqual(
                "awaiting_hook_review_or_first_event",
                first["ingestion"]["freshness"]["state"],
            )
            self.assertEqual("active", first["ingestion"]["registered_project_state"])

            self._run(
                "learning-analyze",
                "--as-of-ms",
                "1000",
                "--max-age-ms",
                "1000",
            )
            after_maintenance = self._run("learning-status")
            self.assertTrue(after_maintenance["active"])

            self._stop_collector()
            recovered = self._run("learning-status")
            self.assertTrue(recovered["ready"])
            self.assertTrue(recovered["active"])
        finally:
            self._stop_collector()

    def test_only_learning_register_creates_missing_runtime_authorization(self) -> None:
        state = LearningState(self.state_root, create=False)
        ledger = LearningLedger(state)
        try:
            orchestration = ledger.open_orchestration(
                self.project_id,
                client_version="codex-host-v1",
                now_ms=2,
            )
        finally:
            ledger.close()
        self.state_root.joinpath("collector-authorization.json").unlink()
        missing = self._run("learning-status", expected=1)
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["active"])
        registered = self._run("learning-register")
        self.assertTrue(registered["ready"])
        self.assertEqual(
            registered["collector_runtime_version"],
            authorized_collector_runtime(
                LearningState(self.state_root, create=False)
            )["runtime_version"],
        )
        status = self._run("learning-status")
        self.assertEqual(
            1, status["ingestion"]["managed_lifecycle"]["orchestrations_active"]
        )
        self.assertEqual("active", orchestration["status"])

    def test_status_reports_unregistered_project_without_identifiers(self) -> None:
        unregistered = Path(self.temporary.name) / "unregistered"
        unregistered.mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                "learning-status",
                "--state-root", str(self.state_root),
                "--project-root", str(unregistered),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            "unregistered", report["ingestion"]["registered_project_state"]
        )
        self.assertEqual("inactive", report["ingestion"]["ingestion_state"])
        encoded = json.dumps(report["ingestion"])
        self.assertNotIn(str(unregistered), encoded)
        self.assertNotIn("project_id", encoded)

    def test_concurrent_direct_commands_serialize_and_leave_collector_active(self) -> None:
        try:
            self.assertTrue(self._run("learning-status")["active"])
            command = [
                sys.executable,
                str(ORCHESTRATION_ROOT / "cli.py"),
                "learning-analyze",
                "--state-root",
                str(self.state_root),
                "--project-root",
                str(self.project),
                "--as-of-ms",
                "1000",
                "--max-age-ms",
                "1000",
            ]
            processes = [
                subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)
            ]
            completed = [process.communicate(timeout=10) for process in processes]
            for process, (stdout, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(0, process.returncode, stderr or stdout)
            self.assertTrue(self._run("learning-status")["active"])
        finally:
            self._stop_collector()

    def test_detached_owner_grant_workflow_never_reads_private_key(self) -> None:
        request_report = self._run(
            "learning-create-grant-request",
            "--kind", "canary",
            "--candidate-digest", DIGEST_A,
            "--champion-digest", DIGEST_B,
            "--review-digest", DIGEST_C,
            "--bounds-digest", "d" * 64,
            "--issued-at-ms", "10",
            "--expires-at-ms", "100",
        )
        payload = Path(self.temporary.name) / "owner-request-payload"
        payload.write_bytes(base64.b64decode(request_report["signing_payload_b64"]))
        signed = subprocess.run(
            [
                "ssh-keygen", "-Y", "sign", "-f", str(self.owner_private_key),
                "-n", request_report["namespace"], str(payload),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, signed.returncode, signed.stderr)
        attached = self._run_state(
            "learning-attach-grant-signature",
            "--request", str(self._write("grant-request.json", request_report["request"])),
            "--signature", str(Path(str(payload) + ".sig")),
        )
        self.assertEqual("owner_grant", attached["record"]["record_type"])
        self.assertNotIn(str(self.owner_private_key), json.dumps(attached))

    def test_installed_l3_l4_commands_persist_closed_records(self) -> None:
        feedback_arguments = (
            "--run-id",
            self.outcome["run_id"],
            "--label",
            "accepted",
            "--reason-code",
            "quality",
            "--observed-at-ms",
            "10",
        )
        prepared_feedback = self._run(
            "learning-record-feedback",
            *feedback_arguments,
        )
        feedback_signature = self._sign_prepared(
            prepared_feedback, "feedback-payload"
        )
        feedback = self._run(
            "learning-record-feedback",
            *feedback_arguments,
            "--signature",
            str(feedback_signature),
        )
        feedback_signature.unlink(missing_ok=True)
        self.assertEqual(self.outcome["run_id"], feedback["record"]["run_id"])
        mined = self._run(
            "learning-mine-case",
            "--source-outcome-id",
            self.outcome["record_id"],
            "--task",
            str(self._write("mine-task.json", self.fixture_task)),
            "--criteria-digest",
            DIGEST_B,
            "--lineage-digest",
            DIGEST_C,
        )
        reviewed = self._run(
            "learning-review-case",
            "--case-id",
            mined["record"]["record_id"],
            "--review-digest",
            DIGEST_C,
            "--similarity-group-digest",
            "d" * 64,
            "--destination",
            "promotion_holdout",
            "--freeze",
        )
        self.assertTrue(reviewed["record"]["frozen"])
        analysis = self._run(
            "learning-analyze", "--as-of-ms", "1000", "--max-age-ms", "1000"
        )
        self.assertIn("verification-learning", analysis["analysis"]["slices"])

        before = {"model": self.outcome["model"], "effort": self.outcome["effort"]}
        after = {"model": "gpt-6-sol", "effort": "high"}
        champion = {
            key: self.outcome[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest",
                "package_digest", "policy_digest", "agent_digest", "skill_digests",
                "prompt_digest", "client_digest",
            )
        }
        proposed = generate_candidate(
            digest_key=b"c" * 32,
            scope="general",
            task_slice=self.outcome["task_slice"],
            target="model_effort",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(before),
                "after_digest": content_digest(after),
                "before_value": before,
                "after_value": after,
            }],
            evidence_digests=[content_digest(self.outcome)],
        )
        spec = {
            key: proposed[key]
            for key in (
                "scope", "task_slice", "target", "effect", "base_digest",
                "change_manifest", "champion_configuration",
            )
        }
        generated = self._run(
            "learning-generate-candidate",
            "--spec",
            str(self._write("candidate.json", spec)),
            expected=2,
        )
        self.assertEqual("input validation failed", generated["error"])

        labels = [{"owner": "a", "grader": "a", "swapped": "b"} for _ in range(30)]
        calibration_arguments = (
            "--labels",
            str(self._write("labels.json", labels)),
            "--configuration",
            str(self._write("grader.json", GRADER_CONFIG)),
            "--observed-at-ms",
            "100",
        )
        prepared_calibration = self._run(
            "learning-calibrate-grader",
            *calibration_arguments,
        )
        calibration_signature = self._sign_prepared(
            prepared_calibration, "calibration-payload"
        )
        calibrated = self._run(
            "learning-calibrate-grader",
            *calibration_arguments,
            "--signature",
            str(calibration_signature),
        )
        calibration_signature.unlink(missing_ok=True)
        self.assertTrue(calibrated["record"]["ready"])
        self.assertEqual(content_digest(labels), calibrated["record"]["labels_digest"])

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            with ledger.repository(self.project_id) as repository:
                self.assertEqual(1, len(repository.records("grader_calibration")))
                self.assertEqual(1, len(repository.records("evaluation_case")))
        finally:
            ledger.close()

    def test_installed_host_result_derives_provenance_and_runs_learning_cycle(self) -> None:
        task = {
            "id": "task-1",
            "objective": "Read one fixture",
            "family": "research",
            "complexity": "medium",
            "risk": "low",
            **routing_fields(),
            "meaningful": True,
            "deterministic_validation": True,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
            "owned_paths": [],
            "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent_grant = {
            "tools": ["shell"],
            "filesystem": ["source.txt:read"],
            "network": False,
            "mcps": [],
            "external_effects": False,
        }
        def admit_run(session_id: str, status: str, observed_at_ms: int) -> str:
            state = LearningState(self.state_root, create=False)
            maintenance = CollectorMaintenance.acquire(
                state,
                ORCHESTRATION_ROOT / "cli.py",
                self.project,
            )
            ledger = LearningLedger(state)
            try:
                _, _, run_id = ledger.bind_session(
                    self.project,
                    session_id,
                    "app_server",
                    "codex-cli-0.153.0",
                    now_ms=observed_at_ms,
                )
                terminal = event_record(f"terminal-{session_id}", observed_at_ms=observed_at_ms)
                terminal.update(
                    run_id=run_id,
                    event_code="turn_completed" if status == "completed" else "turn_failed",
                    status=status,
                    stop_reason=status,
                    coverage="root_turn_complete",
                    item_digest=DIGEST_A,
                )
                usage = event_record(f"usage-{session_id}", observed_at_ms=observed_at_ms)
                usage.update(
                    run_id=run_id,
                    event_code="usage_snapshot",
                    status="observed",
                    stop_reason=None,
                    coverage="root_turn_complete",
                    usage=full_usage(),
                )
                ledger.append(self.project_id, terminal, producer="app_server", now_ms=observed_at_ms)
                ledger.append(self.project_id, usage, producer="app_server", now_ms=observed_at_ms)
                return run_id
            finally:
                ledger.close()
                maintenance.resume()

        run_id = admit_run("host-result-session", "completed", 1_000)
        task_path = self._write("host-task.json", task)
        assignment_path = self._write("host-assignment.json", assigned)
        parent_path = self._write("host-parent.json", parent_grant)
        invalid_result = result()
        invalid_result["summary"] = "TOP_SECRET rejected result"
        invalid_result["evidence"] = []
        rejection = self._run(
            "validate-result",
            "--task",
            str(task_path),
            "--assignment",
            str(assignment_path),
            "--parent-grant",
            str(parent_path),
            "--result",
            str(self._write("invalid-host-result.json", invalid_result)),
            expected=2,
        )
        self.assertEqual("missing_evidence", rejection["reason_code"])
        self.assertTrue(rejection["learning_ready"])
        self.assertRegex(rejection["repair_receipt"], r"^rpr_[0-9a-f]{48}$")
        observation = {
            "run_id": run_id,
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": rejection["repair_receipt"],
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        host_result_path = self._write("host-result.json", result())
        observation_path = self._write("host-observation.json", observation)
        report = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(observation_path),
        )
        self.assertEqual(run_id, report["outcome"]["run_id"])
        self.assertEqual("unverified", report["outcome"]["status"])
        self.assertFalse(report["outcome"]["accepted"])
        self.assertFalse(report["outcome"]["deterministic_pass"])
        self.assertEqual("root_turn_complete", report["outcome"]["coverage"])
        self.assertEqual(1, report["outcome"]["repairs"])
        self.assertEqual(
            ["missing_evidence"], report["outcome"]["repair_reason_codes"]
        )
        for field, value in report["outcome"]["usage"].items():
            self.assertEqual(
                "client_observed" if value is not None else "unavailable",
                report["outcome"]["usage_sources"][field]["source"],
            )
        replay = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(observation_path),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", replay["error"])

        forged_observation = {
            "run_id": admit_run("host-forged-session", "completed", 1_001),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": "rpr_" + "f" * 48,
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        forged = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(host_result_path),
            "--observation", str(
                self._write("forged-host-observation.json", forged_observation)
            ),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", forged["error"])

        second_rejection = self._run(
            "validate-result",
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("second-invalid-result.json", invalid_result)),
            expected=2,
        )
        other_task = dict(task)
        other_task["id"] = "task-2"
        other_assigned = assignment()
        other_assigned.update(assignment_id="assignment-2", task_id="task-2")
        other_result = result()
        other_result["assignment_id"] = "assignment-2"
        wrong_assignment_observation = {
            "run_id": admit_run("host-wrong-assignment", "completed", 1_002),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {
                    "status": "rejected",
                    "reason_code": "missing_evidence",
                    "receipt_id": second_rejection["repair_receipt"],
                },
                {"status": "accepted", "reason_code": None, "receipt_id": None},
            ],
        }
        wrong_assignment = self._run(
            "learning-record-host-result",
            "--task", str(self._write("other-task.json", other_task)),
            "--assignment", str(self._write("other-assignment.json", other_assigned)),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("other-result.json", other_result)),
            "--observation", str(
                self._write(
                    "wrong-assignment-observation.json",
                    wrong_assignment_observation,
                )
            ),
            expected=2,
        )
        self.assertEqual("result repair receipt is invalid", wrong_assignment["error"])
        state = LearningState(self.state_root, create=False)
        maintenance = CollectorMaintenance.acquire(
            state,
            ORCHESTRATION_ROOT / "cli.py",
            self.project,
        )
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        try:
            with ledger.repository(self.project_id) as repository:
                mined_sources = {
                    repository.record(record_id, "evaluation_case")["source_outcome_id"]
                    for record_id in report["learning_cycle"]["mined_case_ids"]
                }
            self.assertNotIn(report["outcome"]["record_id"], mined_sources)
        finally:
            ledger.close()

            maintenance.resume()
        database_bytes = self.state_root.joinpath("learning.sqlite3").read_bytes()
        for secret in (b"sensitive result text", b"private/path", b"secret command"):
            self.assertNotIn(secret, database_bytes)

        failed_result = result()
        failed_result.update(
            status="failed",
            summary="failed secret summary",
            evidence=[],
            remaining_risks=["private failure detail"],
        )
        mismatched_observation = {
            "run_id": admit_run("host-mismatch-session", "completed", 1_003),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        mismatched = self._run(
            "learning-record-host-result",
            "--task", str(self._write("mismatch-task.json", task)),
            "--assignment", str(self._write("mismatch-assignment.json", assigned)),
            "--parent-grant", str(self._write("mismatch-parent.json", parent_grant)),
            "--result", str(self._write("mismatch-result.json", failed_result)),
            "--observation", str(self._write("mismatch-observation.json", mismatched_observation)),
            expected=2,
        )
        self.assertEqual(
            "host result conflicts with the trusted terminal event",
            mismatched["error"],
        )
        failed_observation = {
            "run_id": admit_run("host-failed-session", "failed", 1_004),
            "client_version": "codex-cli-0.153.0",
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        failed = self._run(
            "learning-record-host-result",
            "--task", str(self._write("failed-task.json", task)),
            "--assignment", str(self._write("failed-assignment.json", assigned)),
            "--parent-grant", str(self._write("failed-parent.json", parent_grant)),
            "--result", str(self._write("failed-result.json", failed_result)),
            "--observation", str(self._write("failed-observation.json", failed_observation)),
        )
        self.assertEqual("failed", failed["outcome"]["status"])
        self.assertEqual("turn_failed", failed["event"]["event_code"])
        self.assertTrue(failed["outcome"]["terminal_failure"])

    def test_generate_candidate_requires_qa_and_selects_exact_paired_cohorts(self) -> None:
        spec, qa_rows = self._seed_candidate_cli_cohort()
        spec_path = self._write("paired-candidate.json", spec)

        missing_qa = self._run(
            "learning-generate-candidate",
            "--spec",
            str(spec_path),
            expected=2,
        )
        self.assertEqual(
            "candidate evidence fails a verified eligibility gate",
            missing_qa["error"],
        )

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            with ledger._connection:
                ledger._connection.executemany(
                    "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                    qa_rows,
                )
        finally:
            ledger.close()
        generated = self._run(
            "learning-generate-candidate", "--spec", str(spec_path)
        )
        self.assertTrue(generated["ready"])
        self.assertEqual(40, len(generated["record"]["evidence_digests"]))
        self.assertEqual(
            20,
            generated["record"]["eligibility_receipt"]["independent_case_count"],
        )
        self.assertEqual(
            spec["champion_configuration"],
            generated["record"]["champion_configuration"],
        )

    def test_trusted_host_lifecycle_opens_and_records_exact_assignment(self) -> None:
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        task_path = self._write("trusted-task.json", task)
        assignment_path = self._write("trusted-assignment.json", assigned)
        parent_path = self._write("trusted-parent.json", parent)
        route = self._run("learning-route", "--task", str(task_path))
        route_path = self._write("trusted-route.json", route)
        orchestration = self._run("learning-open-orchestration")
        checkpoint = self._run(
            "learning-checkpoint-orchestration",
            "--orchestration-handle", orchestration["orchestration_handle"],
            "--goal-tokens-used", "101",
            "--goal-time-used-seconds", "3",
        )
        self.assertEqual("active", checkpoint["status"])
        self.assertEqual(101, checkpoint["usage"]["goal_total_tokens"])
        changed_route = dict(route)
        changed_route["model"] = "gpt-6-astra"
        rejected_route = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(self._write("changed-trusted-route.json", changed_route)),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
            expected=2,
        )
        self.assertEqual("input validation failed", rejected_route["error"])
        opened = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(route_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        self.assertRegex(opened["run_handle"], r"^hrn_[0-9a-f]{48}$")
        self._run(
            "learning-bind-host-agent",
            "--run-handle", opened["run_handle"],
            "--agent-id", "native-child-1",
        )
        observation = {
            "run_handle": opened["run_handle"],
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        arguments = (
            "--task", str(task_path),
            "--assignment", str(assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("trusted-result.json", result())),
            "--observation", str(self._write("trusted-observation.json", observation)),
        )
        recorded = self._run("learning-record-host-result", *arguments)
        self.assertEqual(assigned["assignment_id"], recorded["outcome"]["assignment_id"])
        self.assertEqual(assigned["role"], recorded["outcome"]["role"])
        self.assertEqual(assigned["model"], recorded["outcome"]["model"])
        self.assertEqual(assigned["effort"], recorded["outcome"]["effort"])
        self.assertTrue(all(value is None for value in recorded["outcome"]["usage"].values()))
        replay = self._run("learning-record-host-result", *arguments, expected=2)
        self.assertIn("host run handle", replay["error"])
        self.assertNotIn(
            opened["run_handle"].encode(),
            self.state_root.joinpath("learning.sqlite3").read_bytes(),
        )

    def test_upgrade_reconciliation_cli_requires_owner_signature(self) -> None:
        task = {
            "id": "task-1", "objective": "Inspect one fixture",
            "family": "research", "complexity": "medium", "risk": "low",
            "meaningful": True, **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        assigned = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        task_path = self._write("upgrade-task.json", task)
        assigned_path = self._write("upgrade-assignment.json", assigned)
        parent_path = self._write("upgrade-parent.json", parent)
        route = self._run("learning-route", "--task", str(task_path))
        route_path = self._write("upgrade-route.json", route)
        orchestration = self._run("learning-open-orchestration")
        opened = self._run(
            "learning-open-host-run", "--task", str(task_path),
            "--route", str(route_path), "--assignment", str(assigned_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        prepared = self._run(
            "learning-prepare-upgrade-reconciliation",
            "--orchestration-id", orchestration["orchestration_id"],
            "--run-handle", opened["run_handle"],
        )
        request_path = self._write("upgrade-request.json", prepared["request"])
        invalid_signature = Path(self.temporary.name) / "invalid-upgrade.sig"
        invalid_signature.write_bytes(b"invalid")
        rejected = self._run(
            "learning-commit-upgrade-reconciliation",
            "--request", str(request_path),
            "--signature", str(invalid_signature), expected=2,
        )
        self.assertIn("signature", rejected["error"])
        signature_path = sign_owner_payload(
            self.owner_private_key,
            base64.b64decode(prepared["signing_payload_b64"]),
            "upgrade-cli-payload",
        )
        committed = self._run(
            "learning-commit-upgrade-reconciliation",
            "--request", str(request_path),
            "--signature", str(signature_path),
        )
        self.assertEqual("interrupted", committed["status"])
        self.assertEqual(1, committed["runs"]["abandoned"])
        self.assertEqual(0, committed["runs"]["pending"])

    def test_legacy_fallback_requires_one_abandoned_primary_host_run(self) -> None:
        task = {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(),
            "deterministic_validation": True, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }
        primary_assignment = assignment()
        parent = {
            "tools": ["shell"], "filesystem": ["source.txt:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        task_path = self._write("fallback-task.json", task)
        primary_assignment_path = self._write(
            "fallback-primary-assignment.json", primary_assignment
        )
        parent_path = self._write("fallback-parent.json", parent)
        primary_route = self._run("learning-route", "--task", str(task_path))
        primary_route_path = self._write("fallback-primary-route.json", primary_route)
        orchestration = self._run("learning-open-orchestration")
        primary_run = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(primary_route_path),
            "--assignment", str(primary_assignment_path),
            "--parent-grant", str(parent_path),
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        naked = self._run(
            "learning-route",
            "--task", str(task_path),
            "--fallback-from-run-handle", primary_run["run_handle"],
            expected=2,
        )
        self.assertIn("paused orchestration", naked["error"])
        self._run(
            "learning-pause-orchestration",
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        self._run(
            "learning-classify-host-run-abandoned",
            "--run-handle", primary_run["run_handle"],
            "--reason", "recovery_unavailable",
        )
        fallback_route = self._run(
            "learning-route",
            "--task", str(task_path),
            "--fallback-from-run-handle", primary_run["run_handle"],
        )
        self.assertEqual(
            ("gpt-5.6-sol", "high"),
            (fallback_route["model"], fallback_route["effort"]),
        )
        self.assertEqual(
            "selected_model_unavailable_fallback", fallback_route["reason"]
        )
        receipt = fallback_route["fallback"]["receipt_id"]
        self.assertRegex(receipt, r"^fbr_[0-9a-f]{48}$")
        replay = self._run(
            "learning-route",
            "--task", str(task_path),
            "--fallback-from-run-handle", primary_run["run_handle"],
            expected=2,
        )
        self.assertIn("unbound recovery-unavailable run", replay["error"])
        self._run(
            "learning-resume-orchestration",
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        fallback_assignment = dict(primary_assignment)
        fallback_assignment.update(
            assignment_id="fallback-assignment-1",
            model="gpt-5.6-sol",
            effort="high",
        )
        fallback_assignment_path = self._write(
            "fallback-assignment.json", fallback_assignment
        )
        fallback_route_path = self._write("fallback-route.json", fallback_route)
        fallback_run = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(fallback_route_path),
            "--assignment", str(fallback_assignment_path),
            "--parent-grant", str(parent_path),
            "--fallback-receipt", receipt,
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        receipt_replay = self._run(
            "learning-open-host-run",
            "--task", str(task_path),
            "--route", str(fallback_route_path),
            "--assignment", str(fallback_assignment_path),
            "--parent-grant", str(parent_path),
            "--fallback-receipt", receipt,
            "--orchestration-handle", orchestration["orchestration_handle"],
            expected=2,
        )
        self.assertIn("fallback receipt", receipt_replay["error"])
        self._run(
            "learning-bind-host-agent",
            "--run-handle", fallback_run["run_handle"],
            "--agent-id", "native-fallback-child-1",
        )
        fallback_result = result()
        fallback_result["assignment_id"] = fallback_assignment["assignment_id"]
        observation = {
            "run_handle": fallback_run["run_handle"],
            "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}
            ],
        }
        recorded = self._run(
            "learning-record-host-result",
            "--task", str(task_path),
            "--assignment", str(fallback_assignment_path),
            "--parent-grant", str(parent_path),
            "--result", str(self._write("fallback-result.json", fallback_result)),
            "--observation", str(self._write("fallback-observation.json", observation)),
            "--fallback-receipt", receipt,
        )
        self.assertEqual("gpt-5.6-sol", recorded["outcome"]["model"])

    def test_runtime_rejects_test_fixture_client_identity(self) -> None:
        clients = json.loads(
            (ORCHESTRATION_ROOT / "config" / "learning-clients.json").read_text()
        )["clients"]
        self.assertNotIn("codex-eval-fixture-1", clients)

    def test_cli_rejections_are_bounded_and_do_not_echo_sensitive_paths(self) -> None:
        sensitive = Path(self.temporary.name) / "TOP_SECRET_customer_token"
        missing = self._run_raw(
            "learning-validate", "--record", str(sensitive), expected=2
        )
        self.assertEqual("input validation failed", missing["error"])
        self.assertNotIn("TOP_SECRET", json.dumps(missing))

        duplicate = Path(self.temporary.name) / "duplicate.json"
        duplicate.write_text('{"schema_version":1,"schema_version":1}')
        duplicate_report = self._run_raw(
            "learning-validate", "--record", str(duplicate), expected=2
        )
        self.assertEqual("input validation failed", duplicate_report["error"])

        oversized = Path(self.temporary.name) / "oversized.json"
        oversized.write_bytes(b"[" + b"0," * 40_000 + b"0]")
        oversized_report = self._run_raw(
            "learning-validate", "--record", str(oversized), expected=2
        )
        self.assertEqual("input validation failed", oversized_report["error"])

        rejected = self._write(
            "rejected-secret.json", {"TOP_SECRET_customer_token": "TOP_SECRET_value"}
        )
        rejected_report = self._run_raw(
            "learning-validate", "--record", str(rejected), expected=2
        )
        self.assertEqual("input validation failed", rejected_report["error"])
        self.assertNotIn("TOP_SECRET", json.dumps(rejected_report))


if __name__ == "__main__":
    unittest.main()
