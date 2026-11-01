from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT, REPOSITORY_ROOT, TEMP_ROOT

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
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
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



# Runs the hook as Codex would, then reports which codex_agents modules it imported.
IMPORT_PROBE = (
    "import runpy, sys\n"
    "hook = sys.argv[1]\n"
    "sys.argv = sys.argv[1:]\n"
    "try:\n"
    "    runpy.run_path(hook, run_name='__main__')\n"
    "except SystemExit:\n"
    "    pass\n"
    "sys.stderr.write(repr(sorted(m for m in sys.modules if m.split('.')[0] == 'codex_agents')))\n"
)
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


class CheckoutGuardTests(unittest.TestCase):
    """A rendered hook acts only for events from its own checkout (`--checkout`)."""

    def setUp(self) -> None:
        self.base = Path(os.path.realpath(tempfile.mkdtemp(dir=TEMP_ROOT)))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.checkout = self.base / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q")
        (self.checkout / "README.md").write_text("checkout\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "initial")
        shutil.copytree(BUNDLE_ROOT / ".codex" / "orchestration", self.checkout / ".codex" / "orchestration",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(BUNDLE_ROOT / "VERSION", self.checkout / "VERSION")  # the version an install manifest would give
        self.hook = self.checkout / ".codex" / "orchestration" / "learning_hook.py"
        self.payload = json.loads((FIXTURES / "hook-pre-tool.json").read_text())

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", "-C", str(cwd or self.checkout), *args], env=GIT_ENV, check=True,
                       capture_output=True)

    def probe(self, cwd: Path, state: Path, event: str = "Stop") -> tuple[str, list[str]]:
        payload = {**self.payload, "cwd": str(cwd), "hook_event_name": event}
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", IMPORT_PROBE, str(self.hook),
             "--state-root", str(state), "--checkout", str(self.checkout),
             "--client-version", "codex-hooks-v1"],
            input=json.dumps(payload), text=True, capture_output=True, check=False, timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout, eval(completed.stderr)  # noqa: S307 - the probe's own repr

    def test_events_from_worktrees_and_nested_repositories_are_silent_no_ops(self) -> None:
        state = self.base / "state"  # never created: nothing may start or record
        inner = self.checkout / ".worktrees" / "feature"
        self.git("worktree", "add", "-q", "-b", "feature", str(inner))
        outer = self.base / "outer-worktree"
        self.git("worktree", "add", "-q", "-b", "outer", str(outer))
        nested = self.checkout / "vendor" / "lib"
        nested.mkdir(parents=True)
        self.git("init", "-q", cwd=nested)
        (self.checkout / "synthetic").mkdir()
        (self.checkout / "synthetic" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        for label, cwd in (("linked worktree inside", inner / "src"), ("linked worktree inside, top", inner),
                           ("linked worktree outside", outer), ("nested repository", nested),
                           ("synthetic .git file", self.checkout / "synthetic"), ("unrelated", self.base)):
            (cwd if cwd.exists() else cwd).mkdir(parents=True, exist_ok=True)
            with self.subTest(label):
                stdout, imported = self.probe(cwd, state)
                self.assertEqual("{}\n", stdout)
                self.assertEqual([], imported)
                stdout, imported = self.probe(cwd, state, event="PreToolUse")
                self.assertEqual("", stdout)
                self.assertEqual([], imported)
        self.assertFalse(state.exists())

    def test_an_unreadable_nested_worktree_is_a_silent_no_op(self) -> None:
        # A session in nested worktree B removes search permission from B: its .git can no
        # longer be seen, which must refuse the event, never walk on to the checkout's .git.
        state = self.base / "state"
        inner = self.checkout / ".worktrees" / "feature"
        self.git("worktree", "add", "-q", "-b", "feature", str(inner))
        (inner / "src").mkdir()
        for mode in (0o000, 0o600):
            inner.chmod(mode)
            self.addCleanup(inner.chmod, 0o755)
            if os.access(inner / ".git", os.F_OK):
                self.skipTest("chmod does not take search permission away here (running as root?)")
            for cwd in (inner, inner / "src"):
                with self.subTest(mode=oct(mode), cwd=cwd.name):
                    stdout, imported = self.probe(cwd, state)
                    self.assertEqual("{}\n", stdout)
                    self.assertEqual([], imported)
            inner.chmod(0o755)
        self.assertFalse(state.exists())

    def test_the_working_folder_must_exist_and_be_absolute(self) -> None:
        state = self.base / "state"
        for cwd in (self.checkout / "missing", self.checkout / "missing" / "deeper", Path("src")):
            with self.subTest(cwd=str(cwd)):
                stdout, imported = self.probe(cwd, state)
                self.assertEqual("{}\n", stdout)
                self.assertEqual([], imported)
        # A link inside the checkout that leads out of it resolves outside: refused.
        (self.checkout / "escape").symlink_to(self.base)
        stdout, imported = self.probe(self.checkout / "escape", state)
        self.assertEqual([], imported)
        self.assertFalse(state.exists())

    def test_a_subfolder_of_the_checkout_records_for_the_checkout(self) -> None:
        state = LearningState(self.base / "state")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(state)
        ledger.register_project(self.checkout)
        server = CollectorServer(ledger)
        server.start()
        try:
            subfolder = self.checkout / "src" / "deep"
            subfolder.mkdir(parents=True)
            payload = {**self.payload, "cwd": str(subfolder), "session_id": "subfolder-session"}
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(self.hook), "--state-root", str(state.root),
                 "--checkout", str(self.checkout), "--client-version", "codex-hooks-v1"],
                input=json.dumps(payload), text=True, capture_output=True, check=False, timeout=60)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual({}, client_health_snapshot(state, self.checkout))
        finally:
            server.stop()
            ledger.close()
        connection = sqlite3.connect(f"{state.database.as_uri()}?mode=ro", uri=True)
        try:
            ingested = connection.execute("SELECT COUNT(*) FROM hook_ingestion").fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(ingested, 0)
        # The same event from the checkout's own top folder imports the package as before.
        stdout, imported = self.probe(self.checkout, self.base / "no-state")
        self.assertEqual("{}\n", stdout)
        self.assertIn("codex_agents.learning_store", imported)

if __name__ == "__main__":
    unittest.main()
