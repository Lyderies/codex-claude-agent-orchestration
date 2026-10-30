from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.contracts import ContractError
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_store import (
    LearningLedger,
    LearningState,
    LedgerError,
    authorize_collector_runtime,
)
from codex_agents.providers import (
    APP_SERVER_METHODS,
    APP_SERVER_NOTIFICATIONS,
    IDENTITY_MAX_AGE_MS,
    AdmissionDenied,
    AppServerSession,
    claude_rate_limit_records,
    observe_claude,
    observe_codex,
    parse_claude_auth_status,
    parse_codex_rate_limits,
    record_claude_rate_limit_event,
)
from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    admit_provider,
    assignment,
    attest_provider,
    generate_test_owner_key,
    observe_provider_fixture,
    provider_fixture,
    routing_fields,
)

KEY = b"k" * 32
NOW = 10_000_000_000
CLEAN: dict[str, str] = {}
RAW_IDENTITY = (
    b"fixture-owner@example.invalid",
    b"fixture-org-7f3a91",
    b"Fixture Organisation Name",
    b"fixture-account-5c2e08",
)

# A stand-in for `codex app-server`: it logs each method it receives and
# answers from the recorded fixture. It never contacts a provider.
FAKE_APP_SERVER = """\
import json, sys
log = open(sys.argv[1], "a")
fixture = json.load(open(sys.argv[2]))
print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    log.write(message["method"] + "\\n")
    log.flush()
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"userAgent": "fake"}
    elif message["method"] == "account/rateLimits/read":
        result = fixture
    else:
        print(json.dumps({"id": message["id"], "error": {"code": -1}}), flush=True)
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""


def fake_codex(directory: Path) -> tuple[dict[str, str], Path]:
    log = directory / "methods.log"
    script = directory / "fake_app_server.py"
    script.write_text(FAKE_APP_SERVER)
    executable = directory / "codex"
    executable.write_text(
        f"#!/bin/sh\nexec {sys.executable} {script} {log} "
        f"{Path(__file__).resolve().parent / 'fixtures/providers/codex-rate-limits.json'}\n"
    )
    executable.chmod(0o755)
    return {"PATH": str(directory)}, log


class ProviderObservationTests(unittest.TestCase):
    def test_claude_auth_status_becomes_a_fingerprint(self) -> None:
        identity = parse_claude_auth_status(
            provider_fixture("claude-auth-status.json"),
            digest_key=KEY,
            observed_at_ms=NOW,
        )
        self.assertEqual(
            ("claude", True, "claude.ai", "max"),
            (
                identity["provider"], identity["logged_in"],
                identity["auth_method"], identity["subscription_type"],
            ),
        )
        self.assertEqual(
            keyed_digest(
                KEY, "claude\x00fixture-org-7f3a91\x00fixture-owner@example.invalid"
            ),
            identity["account_fingerprint"],
        )
        serialized = canonical_json(identity)
        for raw in RAW_IDENTITY:
            self.assertNotIn(raw, serialized)
        with self.assertRaisesRegex(ContractError, "identity is incomplete"):
            parse_claude_auth_status(
                b'{"loggedIn": false}', digest_key=KEY, observed_at_ms=NOW
            )

    def test_codex_rate_limits_identify_windows_by_duration(self) -> None:
        identity, headroom, billing = parse_codex_rate_limits(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY,
            observed_at_ms=NOW,
        )
        self.assertEqual("plus", identity["plan_type"])
        self.assertEqual(
            keyed_digest(KEY, "codex\x00fixture-account-5c2e08"),
            identity["account_fingerprint"],
        )
        self.assertEqual(
            [(300, 12.0, 1_793_450_000_000), (10_080, 42.0, 1_793_500_000_000)],
            [
                (window["window_minutes"], window["used_percent"], window["resets_at"])
                for window in headroom["windows"]
            ],
        )
        self.assertEqual(
            (False, False, False, True),
            (
                billing["paid_usage_usable"], billing["has_credits"],
                billing["unlimited"], billing["ordinary_usage_allowed"],
            ),
        )
        for record in (identity, headroom, billing):
            for raw in RAW_IDENTITY:
                self.assertNotIn(raw, canonical_json(record))

    def test_claude_rate_limit_event_records_headroom_and_billing(self) -> None:
        headroom, billing = claude_rate_limit_records(
            json.loads(provider_fixture("claude-rate-limit-event.json")),
            account_fingerprint=DIGEST_A,
            observed_at_ms=NOW,
        )
        self.assertEqual("allowed", headroom["status"])
        self.assertEqual(
            [{"window_minutes": 300, "scope": "five_hour",
              "used_percent": 25.0, "resets_at": 1_793_450_000_000}],
            headroom["windows"],
        )
        self.assertEqual(
            (False, False, "rejected"),
            (
                billing["paid_usage_usable"], billing["is_using_overage"],
                billing["overage_status"],
            ),
        )
        unified, _ = claude_rate_limit_records(
            {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.5, "status": "allowed"},
                "seven_day": {"utilization": 0.9, "status": "rejected"},
            }},
            account_fingerprint=DIGEST_A,
            observed_at_ms=NOW,
        )
        self.assertEqual("rejected", unified["status"])
        self.assertEqual(
            [300, 10_080], [window["window_minutes"] for window in unified["windows"]]
        )
        with self.assertRaisesRegex(ContractError, "fraction"):
            claude_rate_limit_records(
                {"status": "allowed", "utilization": 25},
                account_fingerprint=DIGEST_A, observed_at_ms=NOW,
            )

    def test_observe_claude_uses_fixed_arguments_without_api_keys(self) -> None:
        calls = []

        def runner(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(
                arguments, 0, provider_fixture("claude-auth-status.json"), b""
            )

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\nexit 1\n")
            executable.chmod(0o755)
            environ = {"PATH": directory}
            records = observe_claude(
                digest_key=KEY, observed_at_ms=NOW, environ=environ, runner=runner
            )
            self.assertEqual("provider_identity", records[0]["record_type"])
            self.assertEqual(
                [str(executable), "auth", "status", "--json"], calls[0][0]
            )
            self.assertLessEqual(calls[0][1]["timeout"], 30)
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                with self.subTest(name=name), self.assertRaisesRegex(
                    ContractError, "API key"
                ):
                    observe_claude(
                        digest_key=KEY, observed_at_ms=NOW,
                        environ={**environ, name: "set"}, runner=runner,
                    )
            self.assertEqual(1, len(calls))

    def test_app_server_sends_only_allow_listed_methods(self) -> None:
        self.assertEqual(("initialize", "account/rateLimits/read"), APP_SERVER_METHODS)
        self.assertEqual(("initialized",), APP_SERVER_NOTIFICATIONS)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            environ, log = fake_codex(root)
            records = observe_codex(
                root, digest_key=KEY, observed_at_ms=NOW, environ=environ
            )
            self.assertEqual(
                ["provider_identity", "provider_headroom", "provider_billing"],
                [record["record_type"] for record in records],
            )
            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"], log.read_text().split()
            )
            session = AppServerSession(root, environ=environ)
            try:
                for method in ("account/logout", "thread/start", "turn/start"):
                    with self.subTest(method=method), self.assertRaisesRegex(
                        ContractError, "not allow-listed"
                    ):
                        session.request(method, {})
                for method in ("account/logout", "initialize"):
                    with self.subTest(notification=method), self.assertRaisesRegex(
                        ContractError, "not allow-listed"
                    ):
                        session.notify(method)
            finally:
                session.close()
            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"], log.read_text().split()
            )
            with self.assertRaisesRegex(ContractError, "API key"):
                AppServerSession(root, environ={**environ, "OPENAI_API_KEY": "x"})


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.project.mkdir()
        self.state = LearningState(base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, public_key = generate_test_owner_key(base)
        self.state.install_owner_public_key(public_key)
        self.clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.project_id = self.ledger.register_project(self.project, now_ms=1)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def admit(self, provider: str = "codex", **options) -> dict:
        return self.ledger.evaluate_admission(
            self.project_id, provider=provider,
            environ=options.pop("environ", CLEAN),
            now_ms=options.pop("now_ms", getattr(self, "clock", NOW) + 1),
        )

    def assert_denied(self, reason: str, provider: str = "codex", **options) -> None:
        with self.assertRaises(AdmissionDenied) as raised:
            self.admit(provider, **options)
        self.assertEqual(reason, raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")[-1]
        self.assertEqual(("deny", reason), (stored["decision"], stored["reason_code"]))

    def observe(self, records: list[dict]) -> None:
        self.ledger.record_provider_observations(self.project_id, records, now_ms=NOW)

    def codex_records(self, **changes) -> list[dict]:
        # Each observation gets its own time, as successive real reads would.
        self.clock = getattr(self, "clock", NOW) + 1
        result = json.loads(provider_fixture("codex-rate-limits.json"))
        for path, value in changes.items():
            target = result
            *parents, leaf = path.split(".")
            for parent in parents:
                target = target[parent]
            target[leaf] = value
        return parse_codex_rate_limits(
            result, digest_key=self.state.digest_key(), observed_at_ms=self.clock
        )

    def open_run(self, decision: dict, provider: str = "codex") -> dict:
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1", now_ms=NOW
        )
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
        return self.ledger.open_host_run(
            self.project_id,
            assignment=assigned,
            task={
                "id": "task-1", "objective": "Read one fixture", "family": "research",
                "complexity": "medium", "risk": "low", "meaningful": True,
                **routing_fields(),
                "deterministic_validation": True, "independent_parts": 1,
                "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            },
            route={
                "task_id": "task-1", "role": assigned["role"],
                "model": assigned["model"], "effort": assigned["effort"],
                "topology": "single", "max_parallel": 1,
                "verification_required": True, "provider": provider,
                "execution_configuration": configuration,
            },
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            admission=decision,
            now_ms=NOW,
        )

    def test_codex_denials_follow_the_fixed_order(self) -> None:
        self.assert_denied("provider_api_key_present", environ={"OPENAI_API_KEY": "x"})
        self.assert_denied(
            "provider_api_key_present", environ={"ANTHROPIC_API_KEY": "x"}
        )
        self.assert_denied("provider_identity_missing")
        identity = self.codex_records()[0]
        stale = parse_codex_rate_limits(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=self.state.digest_key(),
            observed_at_ms=NOW - IDENTITY_MAX_AGE_MS - 1,
        )[0]
        self.observe([stale])
        self.assert_denied("provider_identity_missing")
        self.observe([identity])
        # The intended compatibility break: Codex-only projects need an attestation.
        self.assert_denied("billing_attestation_missing")
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "codex",
            identity["account_fingerprint"],
        )
        usable = self.codex_records(**{"rateLimits.credits.hasCredits": True})
        self.observe([usable[2]])
        self.assert_denied("paid_usage_usable")
        unknown = self.codex_records(**{"rateLimits.credits": None})
        self.observe([unknown[2]])
        self.assert_denied("billing_unverified")
        self.observe([self.codex_records()[2]])
        exhausted = self.codex_records(**{"rateLimits.secondary.usedPercent": 90})
        self.observe([exhausted[1]])
        self.assert_denied("provider_exhausted")
        self.observe([self.codex_records()[1]])
        blocked = self.codex_records(ordinaryUsageAllowed=False)
        self.observe([blocked[2]])
        self.assert_denied("provider_exhausted")
        self.observe([self.codex_records()[2]])
        decision = self.admit()
        self.assertEqual(("pass", "admitted"), (decision["decision"], decision["reason_code"]))
        self.assertIn(content_digest(identity), decision["evidence_digests"])

    def test_claude_requires_subscription_login_and_unexhausted_windows(self) -> None:
        status = json.loads(provider_fixture("claude-auth-status.json"))
        status.update(authMethod="console", subscriptionType=None)
        console = parse_claude_auth_status(
            json.dumps(status), digest_key=self.state.digest_key(), observed_at_ms=NOW
        )
        self.observe([console])
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "claude",
            console["account_fingerprint"],
        )
        self.assert_denied("claude_subscription_missing", "claude")
        fingerprint = observe_provider_fixture(
            self.ledger, self.project_id, "claude", observed_at_ms=NOW + 1
        )[0]["account_fingerprint"]
        self.assertEqual(console["account_fingerprint"], fingerprint)
        self.admit("claude", now_ms=NOW + 2)
        rejected = claude_rate_limit_records(
            {"status": "rejected", "rateLimitType": "seven_day", "utilization": 1.0,
             "isUsingOverage": False, "overageStatus": "rejected"},
            account_fingerprint=fingerprint, observed_at_ms=NOW + 3,
        )
        self.ledger.record_provider_observations(
            self.project_id, rejected, now_ms=NOW + 3
        )
        later_five_hour = claude_rate_limit_records(
            json.loads(provider_fixture("claude-rate-limit-event.json")),
            account_fingerprint=fingerprint, observed_at_ms=NOW + 4,
        )
        self.ledger.record_provider_observations(
            self.project_id, later_five_hour, now_ms=NOW + 4
        )
        # A newer five-hour window does not hide the rejected seven-day window.
        self.assert_denied("provider_exhausted", "claude", now_ms=NOW + 5)
        overage = claude_rate_limit_records(
            {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.1,
             "isUsingOverage": True},
            account_fingerprint=fingerprint, observed_at_ms=NOW + 6,
        )
        self.ledger.record_provider_observations(
            self.project_id, overage, now_ms=NOW + 6
        )
        self.assert_denied("paid_usage_usable", "claude", now_ms=NOW + 7)

    def test_bootstrap_admits_on_identity_and_attestation_alone(self) -> None:
        identity = self.codex_records()[0]
        self.observe([identity])
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "codex",
            identity["account_fingerprint"],
        )
        decision = self.admit()
        self.assertEqual("pass", decision["decision"])
        self.assertEqual(2, len(decision["evidence_digests"]))
        usable = self.codex_records(**{"rateLimits.credits.unlimited": True})
        self.observe([usable[2]])
        self.assert_denied("paid_usage_usable")

    def test_attestation_for_another_account_does_not_count(self) -> None:
        identity = self.codex_records()[0]
        self.observe([identity])
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "codex", DIGEST_C
        )
        self.assert_denied("billing_attestation_missing")
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "claude",
            identity["account_fingerprint"],
        )
        self.assert_denied("billing_attestation_missing")

    def test_replay_reads_the_stored_decision_after_observations_go_stale(self) -> None:
        admit_provider(
            self.ledger, self.project_id, self.owner_private_key, observed_at_ms=NOW
        )
        decision = self.admit()
        opened = self.open_run(decision)
        later = NOW + IDENTITY_MAX_AGE_MS + 60_000
        self.assert_denied("provider_identity_missing", now_ms=later)
        stored = self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        self.assertEqual(
            ("pass", decision["evidence_digests"]),
            (stored["decision"], stored["evidence_digests"]),
        )
        with self.assertRaisesRegex(LedgerError, "does not admit this route"):
            self.open_run(decision, provider="claude")
        with self.assertRaisesRegex(LedgerError, "does not admit this route"):
            self.open_run({**decision, "decision": "deny"})

    def test_claude_rate_limit_events_use_the_runs_admitted_identity(self) -> None:
        fingerprint = admit_provider(
            self.ledger, self.project_id, self.owner_private_key, "claude",
            observed_at_ms=NOW,
        )
        opened = self.open_run(self.admit("claude"), provider="claude")
        records = record_claude_rate_limit_event(
            self.ledger, self.project_id,
            run_handle=opened["run_handle"],
            rate_limit_info=json.loads(provider_fixture("claude-rate-limit-event.json")),
            observed_at_ms=NOW + 1,
        )
        self.assertEqual(
            [fingerprint, fingerprint],
            [record["account_fingerprint"] for record in records],
        )
        with self.ledger.repository(self.project_id) as repository:
            self.assertEqual(
                records[1]["record_id"],
                repository.records("provider_billing")[-1]["record_id"],
            )
        admit_provider(
            self.ledger, self.project_id, self.owner_private_key, observed_at_ms=NOW
        )
        codex_run = self.open_run(self.admit())
        with self.assertRaisesRegex(ContractError, "not belong to a Claude run"):
            record_claude_rate_limit_event(
                self.ledger, self.project_id,
                run_handle=codex_run["run_handle"],
                rate_limit_info={"status": "allowed"},
            )

    def test_attestations_require_an_owner_signature(self) -> None:
        identity = self.codex_records()[0]
        self.observe([identity])
        from codex_agents.providers import billing_attestation_record

        record = billing_attestation_record({
            "version": 1, "provider": "codex",
            "account_fingerprint": identity["account_fingerprint"],
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 5,
        })
        with self.assertRaisesRegex(LedgerError, "signature is invalid"):
            self.ledger.commit_owner_record(
                self.project_id, record=record, signature=b"not a signature"
            )
        with self.assertRaisesRegex(LedgerError, "not authorized|owner records"):
            self.ledger.append(self.project_id, record, producer="owner")
        with self.assertRaisesRegex(ContractError, "no paid overage"):
            billing_attestation_record({
                "version": 1, "provider": "codex",
                "account_fingerprint": identity["account_fingerprint"],
                "no_paid_overage": False, "no_usage_credits": True, "issued_at": 5,
            })
        self.assert_denied("billing_attestation_missing")

    def test_database_never_stores_raw_identity_values(self) -> None:
        for provider in ("codex", "claude"):
            admit_provider(
                self.ledger, self.project_id, self.owner_private_key, provider,
                observed_at_ms=NOW,
            )
            self.admit(provider)
        self.ledger.close()
        stored = b"".join(
            path.read_bytes()
            for path in Path(self.state.root).rglob("*")
            if path.is_file()
        )
        self.assertIn(b"provider_identity", stored)
        for raw in RAW_IDENTITY:
            self.assertNotIn(raw, stored)
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)


if __name__ == "__main__":
    unittest.main()
