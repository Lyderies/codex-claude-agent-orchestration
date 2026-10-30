from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.contracts import ContractError
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import (
    canonical_json,
    content_digest,
    keyed_digest,
    validate_learning_record,
)
from codex_agents.learning_store import (
    PRODUCER_AUTHORITY,
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
    billing_attestation_record,
    claude_rate_limit_records,
    evaluate_admission,
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
    assignment,
    attest_provider,
    fake_provider_tools,
    generate_test_owner_key,
    provider_fixture,
    trusted_tools,
    routing_fields,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers"
CLIENT = ("/usr/bin/fake-client", "f" * 64)
parse_claude = functools.partial(parse_claude_auth_status, client=CLIENT)
parse_codex = functools.partial(parse_codex_rate_limits, client=CLIENT)
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
        identity = parse_claude(
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
            parse_claude(
                b'{"loggedIn": false}', digest_key=KEY, observed_at_ms=NOW
            )

    def test_codex_rate_limits_identify_windows_by_duration(self) -> None:
        identity, headroom, billing = parse_codex(
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

        def popen(arguments, **options):
            calls.append((arguments, options))
            return subprocess.Popen(
                ["/bin/cat", str(FIXTURES / "claude-auth-status.json")], **options
            )

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\nexit 1\n")
            executable.chmod(0o755)
            environ = {"CLAUDE_CONFIG_DIR": "/elsewhere", "CODEX_HOME": "/elsewhere"}
            with trusted_tools(Path(directory)):
                records = observe_claude(
                    digest_key=KEY, observed_at_ms=NOW, environ=environ, popen=popen
                )
                self.assertEqual("provider_identity", records[0]["record_type"])
                real = os.path.realpath(executable)
                self.assertEqual([real, "auth", "status", "--json"], calls[0][0])
                self.assertTrue(calls[0][1]["start_new_session"])
                # The child gets only a minimal environment; overrides are dropped.
                self.assertEqual(
                    {"HOME", "PATH", "LANG", "TMPDIR"}, set(calls[0][1]["env"])
                )
                for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                    with self.subTest(name=name), self.assertRaisesRegex(
                        ContractError, "API key"
                    ):
                        observe_claude(
                            digest_key=KEY, observed_at_ms=NOW,
                            environ={**environ, name: "set"}, popen=popen,
                        )
            self.assertEqual(1, len(calls))

    def test_clients_resolve_only_from_the_trusted_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            trusted = root / "trusted"
            planted = root / "planted"
            fake_provider_tools(trusted)
            marker = root / "planted-ran"
            planted.mkdir()
            impostor = planted / "claude"
            impostor.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 1\n")
            impostor.chmod(0o755)
            with trusted_tools(trusted):
                records = observe_claude(
                    digest_key=KEY, observed_at_ms=NOW,
                    environ={"PATH": f"{planted}{os.pathsep}{trusted}"},
                )
            self.assertFalse(marker.exists())
            real = os.path.realpath(trusted / "claude")
            self.assertEqual(real, records[0]["client_path"])
            self.assertEqual(
                hashlib.sha256(Path(real).read_bytes()).hexdigest(),
                records[0]["client_sha256"],
            )
            with trusted_tools(planted / "missing"), self.assertRaisesRegex(
                ContractError, "trusted path"
            ):
                observe_claude(
                    digest_key=KEY, observed_at_ms=NOW, environ={"PATH": str(trusted)}
                )

    def test_claude_output_is_bounded_while_streaming(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            marker = root / "still-running"
            # Each stand-in records its process and keeps running unless killed.
            scripts = {
                "flood": "import sys\nwhile True: sys.stdout.write('x' * 65536)\n",
                "stall": "import time\ntime.sleep(60)\n",
            }
            for name, body in scripts.items():
                script = root / f"{name}.py"
                script.write_text(
                    f"import os\nopen({str(marker)!r} + '-{name}', 'w')"
                    f".write(str(os.getpid()))\n{body}"
                )
                executable = root / name / "claude"
                executable.parent.mkdir()
                executable.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
                executable.chmod(0o755)
            for name, message in (("flood", "too large"), ("stall", "timed out")):
                with self.subTest(name=name):
                    started = time.monotonic()
                    with trusted_tools(root / name), self.assertRaisesRegex(
                        ContractError, message
                    ):
                        observe_claude(
                            digest_key=KEY, observed_at_ms=NOW, environ={}, timeout=1,
                        )
                    self.assertLess(time.monotonic() - started, 10)
                    pid = int(Path(f"{marker}-{name}").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_labels_are_allow_listed_and_identifiers_never_leak(self) -> None:
        status = json.loads(provider_fixture("claude-auth-status.json"))
        status.update(subscriptionType="fixture-org-7f3a91", authMethod="fixture-org-7f3a91")
        identity = parse_claude(
            json.dumps(status), digest_key=KEY, observed_at_ms=NOW
        )
        self.assertEqual((None, None), (identity["subscription_type"], identity["auth_method"]))
        self.assertNotIn(b"fixture-org-7f3a91", canonical_json(identity))
        result = json.loads(provider_fixture("codex-rate-limits.json"))
        result["rateLimits"]["planType"] = "fixture-account-5c2e08"
        records = parse_codex(result, digest_key=KEY, observed_at_ms=NOW)
        self.assertIsNone(records[0]["plan_type"])
        for record in records:
            self.assertNotIn(b"fixture-account-5c2e08", canonical_json(record))
        result["rateLimits"]["planType"] = "prolite"
        self.assertEqual(
            "prolite",
            parse_codex(result, digest_key=KEY, observed_at_ms=NOW)[0][
                "plan_type"
            ],
        )

    def test_claude_billing_is_unusable_only_on_exact_evidence(self) -> None:
        def usable(**info) -> object:
            return claude_rate_limit_records(
                {"status": "allowed", **info},
                account_fingerprint=DIGEST_A, observed_at_ms=NOW,
            )[1]["paid_usage_usable"]

        rejected = {
            "isUsingOverage": False, "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled",
        }
        self.assertIs(False, usable(**rejected))
        for change in (
            {"overageDisabledReason": "anything at all"},
            {"overageDisabledReason": "fetch_error"},
            {"overageDisabledReason": None},
            {"overageStatus": None, "overageDisabledReason": "org_level_disabled"},
            {"overageStatus": "disabled"},
            {"isUsingOverage": None},
            {"isUsingOverage": "false"},
        ):
            with self.subTest(change=change):
                self.assertIsNone(usable(**{**rejected, **change}))
        self.assertIs(True, usable(**{**rejected, "isUsingOverage": True}))
        self.assertIs(True, usable(**{**rejected, "overageStatus": "allowed"}))

    def test_app_server_sends_only_allow_listed_methods(self) -> None:
        self.assertEqual(("initialize", "account/rateLimits/read"), APP_SERVER_METHODS)
        self.assertEqual(("initialized",), APP_SERVER_NOTIFICATIONS)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            _, log = fake_codex(root)
            environ: dict[str, str] = {}
            with trusted_tools(root):
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
            with trusted_tools(root):
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
            with trusted_tools(root), self.assertRaisesRegex(ContractError, "API key"):
                AppServerSession(root, environ={**environ, "OPENAI_API_KEY": "x"})


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.project.mkdir()
        self.tools = base / "fake-tools"
        self.environ = fake_provider_tools(self.tools)
        self.state = LearningState(base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, public_key = generate_test_owner_key(base)
        self.state.install_owner_public_key(public_key)
        self.clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.project_id = self.ledger.register_project(self.project, now_ms=1)
        self.clock = NOW

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def tick(self) -> int:
        # Each observation gets its own time, as successive real reads would.
        self.clock += 1
        return self.clock

    def admit(self, provider: str = "codex", **options) -> dict:
        return self.ledger.evaluate_admission(
            self.project_id, provider=provider,
            environ=options.pop("environ", CLEAN),
            now_ms=options.pop("now_ms", self.clock + 1),
        )

    def assert_denied(self, reason: str, provider: str = "codex", **options) -> None:
        with self.assertRaises(AdmissionDenied) as raised:
            self.admit(provider, **options)
        self.assertEqual(reason, raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")[-1]
        self.assertEqual(("deny", reason), (stored["decision"], stored["reason_code"]))

    def observe(self, provider: str = "codex", *, now_ms: int | None = None, **changes):
        """Run the ledger's own observation against the fake client's output."""

        if provider == "codex":
            result = json.loads(provider_fixture("codex-rate-limits.json"))
            for path, value in changes.items():
                target = result
                *parents, leaf = path.split(".")
                for parent in parents:
                    target = target[parent]
                target[leaf] = value
            (self.tools / "codex-result.json").write_text(json.dumps(result))
        else:
            status = {**json.loads(provider_fixture("claude-auth-status.json")), **changes}
            (self.tools / "claude-status.json").write_text(json.dumps(status))
        stamp = now_ms if now_ms is not None else self.tick()
        with trusted_tools(self.tools), mock.patch.object(
            self.ledger, "_clock_ms", return_value=stamp
        ):
            return self.ledger.observe_provider(
                self.project_id, provider, project_root=self.tools, environ=CLEAN
            )

    def event(self, run: dict, **info) -> list[dict]:
        """Record a run's rate-limit event; the ledger's clock stamps it."""

        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.tick()):
            return record_claude_rate_limit_event(
                self.ledger, self.project_id,
                run_handle=run["run_handle"], rate_limit_info=info,
            )

    def attest(self, provider: str, fingerprint: str) -> None:
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, provider, fingerprint
        )

    def open_run(self, provider: str = "codex", *, now_ms: int | None = None) -> dict:
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1", now_ms=NOW
        )
        stamp = now_ms if now_ms is not None else self.clock + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp):
            return self._open_run(provider, orchestration, stamp)

    def _open_run(self, provider: str, orchestration: dict, stamp: int) -> dict:
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
            now_ms=stamp,
        )

    def test_codex_denials_follow_the_fixed_order(self) -> None:
        self.assert_denied("provider_api_key_present", environ={"OPENAI_API_KEY": "x"})
        self.assert_denied(
            "provider_api_key_present", environ={"ANTHROPIC_API_KEY": "x"}
        )
        self.assert_denied("provider_identity_missing")
        self.observe(now_ms=NOW - IDENTITY_MAX_AGE_MS - 1)
        self.assert_denied("provider_identity_missing")
        identity = self.observe()[0]
        # The intended compatibility break: Codex-only projects need an attestation.
        self.assert_denied("billing_attestation_missing")
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits": None})
        self.assert_denied("billing_unverified")
        soon = (self.clock + 5_000) // 1000
        self.observe(**{
            "rateLimits.secondary.usedPercent": 90, "rateLimits.secondary.resetsAt": soon,
        })
        self.assert_denied("provider_exhausted")
        # A later, favourable read does not clear a window that has not reset.
        self.observe()
        self.assert_denied("provider_exhausted")
        self.clock += 10_000
        self.observe(ordinaryUsageAllowed=False)
        self.assert_denied("provider_exhausted")
        latest = self.observe()
        decision = self.admit()
        self.assertEqual(
            ("pass", "admitted", "evidence"),
            (decision["decision"], decision["reason_code"], decision["admission_basis"]),
        )
        for record in latest:
            self.assertIn(content_digest(record), decision["evidence_digests"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")
        # Paid usage seen within a day keeps denying, whatever comes after it.
        self.observe()
        self.assert_denied("paid_usage_usable")
        self.clock += 24 * 60 * 60 * 1000 + 1
        self.observe()
        self.assertEqual("pass", self.admit()["decision"])

    def test_codex_without_recent_billing_is_denied(self) -> None:
        identity = parse_claude(
            provider_fixture("claude-auth-status.json"), digest_key=KEY, observed_at_ms=NOW
        )
        codex_identity = {**identity, "provider": "codex", "auth_method": None,
                          "subscription_type": None}
        attestation = {"provider": "codex",
                       "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        decision = evaluate_admission(
            provider="codex",
            identities=[(codex_identity, DIGEST_A)],
            attestations=[(attestation, DIGEST_B)],
            billing=[], headroom=[], prior_claude_admissions=set(),
            environ={}, now_ms=NOW + 1,
        )
        self.assertEqual("billing_unverified", decision["reason_code"])
        stale_billing = {"provider": "codex",
                         "account_fingerprint": identity["account_fingerprint"],
                         "paid_usage_usable": False, "is_using_overage": None,
                         "ordinary_usage_allowed": True,
                         "observed_at_ms": NOW - IDENTITY_MAX_AGE_MS}
        decision = evaluate_admission(
            provider="codex",
            identities=[(codex_identity, DIGEST_A)],
            attestations=[(attestation, DIGEST_B)],
            billing=[(stale_billing, DIGEST_C)], headroom=[],
            prior_claude_admissions=set(), environ={}, now_ms=NOW + 1,
        )
        self.assertEqual("billing_unverified", decision["reason_code"])

    def test_claude_requires_subscription_login(self) -> None:
        console = self.observe("claude", authMethod="console", subscriptionType=None)[0]
        self.attest("claude", console["account_fingerprint"])
        self.assert_denied("claude_subscription_missing", "claude")
        self.observe("claude", subscriptionType="fixture-org-7f3a91")
        self.assert_denied("claude_subscription_missing", "claude")
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.assertEqual(console["account_fingerprint"], fingerprint)
        self.assertEqual("pass", self.admit("claude")["decision"])

    def test_claude_bootstrap_admits_one_run_until_it_records_billing(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        stored = self.ledger.host_run_admission(self.project_id, first["run_handle"])
        self.assertEqual("bootstrap", stored["admission_basis"])
        # A second concurrent bootstrap is denied until the first records billing.
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run("claude")
        self.assertEqual("billing_unverified", raised.exception.reason_code)
        self.event(first, status="allowed", rateLimitType="five_hour",
                   utilization=0.1, isUsingOverage=True)
        self.assert_denied("paid_usage_usable", "claude")

    def test_bootstrap_then_evidence_based_admission(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        self.event(first, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        second = self.open_run("claude")
        stored = self.ledger.host_run_admission(self.project_id, second["run_handle"])
        self.assertEqual("evidence", stored["admission_basis"])

    def test_fabricated_favourable_event_cannot_clear_a_restriction(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        bootstrap = self.open_run("claude")
        self.event(bootstrap, status="allowed", rateLimitType="five_hour",
                   utilization=0.2, isUsingOverage=False, overageStatus="rejected",
                   overageDisabledReason="org_level_disabled")
        real, forger = self.open_run("claude"), self.open_run("claude")
        future = (self.clock + 3_600_000) // 1000
        self.event(real, status="rejected", rateLimitType="seven_day",
                   utilization=1.0, resetsAt=future, isUsingOverage=False,
                   overageStatus="rejected", overageDisabledReason="org_level_disabled")
        self.assert_denied("provider_exhausted", "claude")
        self.event(forger, status="allowed", rateLimitType="seven_day",
                   utilization=0.0, resetsAt=future, isUsingOverage=False,
                   overageStatus="rejected", overageDisabledReason="org_level_disabled")
        self.assert_denied("provider_exhausted", "claude")
        # Each run records one event, and only while it is open.
        with self.assertRaisesRegex(LedgerError, "already recorded"):
            self.event(real, status="allowed", rateLimitType="seven_day",
                       utilization=0.0, isUsingOverage=False, overageStatus="rejected",
                       overageDisabledReason="org_level_disabled")
        self.clock += 3_600_000
        self.observe("claude")
        self.assertEqual("pass", self.admit("claude")["decision"])

    def test_committed_or_closed_runs_cannot_record_events(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        # Committing a result consumes the binding; emulate that final state.
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET consumed_at_ms=?,binding_state='terminal' "
                "WHERE project_id=?",
                (self.clock, self.project_id),
            )
        with self.assertRaisesRegex(LedgerError, "no longer open"):
            self.event(run, status="allowed")
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.event({"run_handle": "hrn_" + "0" * 48}, status="allowed")
        with self.assertRaises(TypeError):
            record_claude_rate_limit_event(
                self.ledger, self.project_id, run_handle=run["run_handle"],
                rate_limit_info={"status": "allowed"}, observed_at_ms=1,
            )

    def test_bootstrap_admits_on_identity_and_attestation_alone(self) -> None:
        identity = self.observe("claude")[0]
        self.attest("claude", identity["account_fingerprint"])
        decision = self.admit("claude")
        self.assertEqual(("pass", "bootstrap"), (decision["decision"], decision["admission_basis"]))
        self.assertEqual(2, len(decision["evidence_digests"]))

    def test_attestation_for_another_account_does_not_count(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", DIGEST_C)
        self.assert_denied("billing_attestation_missing")
        self.attest("claude", identity["account_fingerprint"])
        self.assert_denied("billing_attestation_missing")

    def test_open_host_run_always_admits_inside_the_ledger(self) -> None:
        # Blocker 1: no caller-supplied decision is accepted, and none is needed.
        with self.assertRaises(TypeError):
            self.ledger.open_host_run(
                self.project_id, assignment={}, task={}, route={},
                execution_configuration={}, client_version="codex-host-v1",
                orchestration_handle="x",
                admission={"decision": "pass", "provider": "codex"},
            )
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("provider_identity_missing", raised.exception.reason_code)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
        self.assertEqual("provider_api_key_present", raised.exception.reason_code)
        opened = self.open_run()
        self.assertEqual("admitted", opened["admission"])
        stored = self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        self.assertEqual(identity["account_fingerprint"], stored["account_fingerprint"])

    def test_forged_pass_without_evidence_is_rejected(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        opened = self.open_run()
        forged = {
            "schema_version": 1, "record_type": "admission_decision",
            "record_id": "admission_decision:forged", "run_id": "run:forged",
            "provider": "codex", "decision": "pass", "reason_code": "admitted",
            "admission_basis": "evidence",
            "account_fingerprint": None, "evidence_digests": [],
            "observed_at_ms": NOW,
        }
        with self.assertRaisesRegex(ContractError, "fingerprint, and evidence"):
            validate_learning_record(forged)
        with self.assertRaises(LedgerError):
            self.ledger.append(self.project_id, forged, producer="lifecycle")
        plausible = {**forged, "account_fingerprint": DIGEST_A, "evidence_digests": [DIGEST_B]}
        for producer in ("lifecycle", "host", "evaluator"):
            with self.subTest(producer=producer), self.assertRaises(LedgerError):
                self.ledger.append(self.project_id, plausible, producer=producer)
        # Even a row written straight into storage does not pass the replay check.
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE records SET content=json_set(CAST(content AS TEXT),"
                "'$.account_fingerprint',NULL,'$.evidence_digests',json('[]')) "
                "WHERE project_id=? AND record_type='admission_decision'",
                (self.project_id,),
            )
        with self.assertRaisesRegex(LedgerError, "admission is invalid"):
            self.ledger.host_run_admission(self.project_id, opened["run_handle"])

    def test_forged_provider_observations_cannot_be_appended(self) -> None:
        # Blocker 3: a producer cannot copy a fingerprint into favourable records.
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")
        favourable = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=self.state.digest_key(), observed_at_ms=self.tick(),
        )
        self.assertEqual(identity["account_fingerprint"], favourable[2]["account_fingerprint"])
        for producer in ("provider", "hook", "app_server", "host", "evaluator",
                         "verifier", "owner", "lifecycle"):
            for record in favourable:
                with self.subTest(producer=producer, record=record["record_type"]):
                    with self.assertRaises(LedgerError):
                        self.ledger.append(self.project_id, record, producer=producer)
        self.assertFalse(hasattr(self.ledger, "record_provider_observations"))
        self.assertNotIn("provider", PRODUCER_AUTHORITY)
        producers = json.loads(
            (ORCHESTRATION_ROOT / "config/learning-producers.json").read_text()
        )["producers"]
        self.assertFalse(
            any(set(types) & {"provider_identity", "provider_headroom", "provider_billing"}
                for types in producers.values())
        )
        self.assert_denied("paid_usage_usable")

    def test_collector_sessions_need_admission_to_commit(self) -> None:
        # Blocker 2: the legacy result path is tied to an admission taken at bind.
        clock = mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: self.clock + 1)
        clock.start()
        self.addCleanup(clock.stop)
        _, _, unadmitted = self.ledger.bind_session(
            self.project, "session-before-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 1,
        )
        with self.ledger._lock, self.assertRaisesRegex(
            LedgerError, "no stored admission decision"
        ):
            self.ledger._run_admission_locked(self.project_id, unadmitted)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        _, _, admitted = self.ledger.bind_session(
            self.project, "session-after-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 1,
        )
        with self.ledger._lock:
            decision = self.ledger._run_admission_locked(self.project_id, admitted)
        self.assertEqual(identity["account_fingerprint"], decision["account_fingerprint"])
        # Rebinding an existing session never upgrades its admission.
        self.ledger.bind_session(
            self.project, "session-before-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 2,
        )
        with self.ledger._lock, self.assertRaisesRegex(
            LedgerError, "no stored admission decision"
        ):
            self.ledger._run_admission_locked(self.project_id, unadmitted)

    def test_replay_reads_the_stored_decision_after_observations_go_stale(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        opened = self.open_run()
        decision = self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        later = self.clock + IDENTITY_MAX_AGE_MS + 60_000
        self.assert_denied("provider_identity_missing", now_ms=later)
        self.assertEqual(
            decision, self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        )
        self.assertEqual("pass", decision["decision"])

    def test_claude_rate_limit_events_use_the_runs_admitted_identity(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        opened = self.open_run("claude")
        records = self.event(
            opened, **json.loads(provider_fixture("claude-rate-limit-event.json"))
        )
        self.assertEqual(
            [fingerprint, fingerprint],
            [record["account_fingerprint"] for record in records],
        )
        self.assertEqual(self.clock, records[0]["observed_at_ms"])
        codex_identity = self.observe()[0]
        self.attest("codex", codex_identity["account_fingerprint"])
        codex_run = self.open_run()
        with self.assertRaisesRegex(ContractError, "not belong to a Claude run"):
            self.event(codex_run, status="allowed")

    def test_attestations_require_an_owner_signature(self) -> None:
        identity = self.observe()[0]
        record = billing_attestation_record({
            "version": 1, "provider": "codex",
            "account_fingerprint": identity["account_fingerprint"],
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 5,
        })
        with self.assertRaisesRegex(LedgerError, "signature is invalid"):
            self.ledger.commit_owner_record(
                self.project_id, record=record, signature=b"not a signature"
            )
        with self.assertRaises(LedgerError):
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
            fingerprint = self.observe(provider)[0]["account_fingerprint"]
            self.attest(provider, fingerprint)
            self.admit(provider)
        # Identifiers placed in label fields are dropped too.
        self.observe("claude", subscriptionType="fixture-org-7f3a91")
        self.observe(**{"rateLimits.planType": "fixture-account-5c2e08"})
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
