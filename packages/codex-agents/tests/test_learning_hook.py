from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT, REPOSITORY_ROOT

from codex_agents.learning_adapters import (
    load_client_capabilities,
    normalize_app_server,
    normalize_hook,
    reconcile_run,
)
from codex_agents.learning_store import (
    CollectorServer,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    client_health_increment,
    client_health_snapshot,
)


FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "learning"
DIGEST_KEY = b"fixture-digest-key".ljust(32, b"0")


class LearningHookTests(unittest.TestCase):
    def test_fixture_replay_is_redacted_and_reconciles_root_turn(self) -> None:
        hook_payload = json.loads((FIXTURES / "hook-pre-tool.json").read_text())
        hook = normalize_hook(
            hook_payload,
            digest_key=DIGEST_KEY,
            client_version="codex-cli-0.153.0",
            observed_at_ms=1,
        )
        self.assertNotIn("TEST_SECRET", json.dumps(hook))

        messages = json.loads((FIXTURES / "app-server-events.json").read_text())
        records = [
            record
            for message in messages
            if (
                record := normalize_app_server(
                    message,
                    digest_key=DIGEST_KEY,
                    client_version="codex-cli-0.153.0",
                    coverage="root_turn_complete",
                    observed_at_ms=2,
                )
            )
            is not None
        ]
        encoded = json.dumps(records)
        self.assertNotIn("TEST_SECRET", encoded)
        self.assertEqual(
            ["turn_started", "item_completed", "usage_snapshot", "turn_completed"],
            [record["event_code"] for record in records],
        )
        reconciled = reconcile_run(
            [{"source": "app_server", "record": record} for record in records],
            capability={"app_server_coverage": "root_turn_complete"},
        )
        self.assertEqual("root_turn_complete", reconciled["coverage"])
        self.assertFalse(reconciled["eligible_for_efficiency"])

    def test_unregistered_state_exits_zero_and_emits_no_model_context(self) -> None:
        payload = json.loads((FIXTURES / "hook-pre-tool.json").read_text())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            missing = Path(directory) / "missing-state"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "learning_hook.py"),
                    "--state-root",
                    str(missing),
                    "--client-version",
                    "codex-hooks-v1",
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_deleted_project_hook_does_not_recreate_client_failures(self) -> None:
        payload = json.loads((FIXTURES / "hook-pre-tool.json").read_text())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            base = Path(directory)
            state = LearningState(base / "state")
            authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
            project = base / "project"
            project.mkdir()
            payload.update(cwd=str(project), session_id="deleted-project-session")
            ledger = LearningLedger(state)
            project_id = ledger.register_project(project)
            client_health_increment(state, project, "collector_unavailable", now_ms=1)
            ledger.delete_project(project_id)
            server = CollectorServer(ledger)
            server.start()
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ORCHESTRATION_ROOT / "learning_hook.py"),
                        "--state-root",
                        str(state.root),
                        "--client-version",
                        "codex-hooks-v1",
                    ],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode)
                self.assertEqual({}, client_health_snapshot(state, project))
            finally:
                server.stop()
                ledger.close()

    def test_normalization_rejection_is_counted_without_retaining_input(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            base = Path(directory)
            state = LearningState(base / "state")
            authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
            project = base / "project"
            project.mkdir()
            ledger = LearningLedger(state)
            ledger.register_project(project)
            ledger.close()
            payload = {
                "cwd": str(project),
                "session_id": "safe-session",
                "hook_event_name": "UnsupportedEvent",
                "prompt": "SENTINEL_REJECTED_CONTENT",
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ORCHESTRATION_ROOT / "learning_hook.py"),
                    "--state-root", str(state.root),
                    "--client-version", "codex-hooks-v1",
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            self.assertEqual(
                {
                    "hook_normalization_rejected": {
                        "count": 1,
                        "updated_at_ms": client_health_snapshot(
                            state, project
                        )["hook_normalization_rejected"]["updated_at_ms"],
                    }
                },
                client_health_snapshot(state, project),
            )
            self.assertNotIn(
                b"SENTINEL_REJECTED_CONTENT", state.client_health.read_bytes()
            )

    def test_binding_and_ingest_rejections_are_counted_by_stage(self) -> None:
        payload = json.loads((FIXTURES / "hook-pre-tool.json").read_text())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            base = Path(directory)
            state = LearningState(base / "state")
            authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
            project = base / "project"
            project.mkdir()
            payload.update(cwd=str(project), session_id="rejection-session")
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            clients["bad-hook-fixture"] = {
                **clients["codex-hooks-v1"],
                "hook_coverage": "unknown",
            }
            ledger = LearningLedger(state, client_capabilities=clients)
            ledger.register_project(project)
            server = CollectorServer(ledger, client_capabilities=clients)
            server.start()
            try:
                for identity in ("codex-host-v1", "bad-hook-fixture"):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(ORCHESTRATION_ROOT / "learning_hook.py"),
                            "--state-root", str(state.root),
                            "--client-version", identity,
                        ],
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(0, completed.returncode)
                health = client_health_snapshot(state, project)
                self.assertEqual(1, health["hook_binding_rejected"]["count"])
                self.assertEqual(1, health["hook_ingest_rejected"]["count"])
                self.assertNotIn("rejection-session", json.dumps(health))
            finally:
                server.stop()
                ledger.close()


if __name__ == "__main__":
    unittest.main()
