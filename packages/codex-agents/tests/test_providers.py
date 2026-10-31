from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import contextlib
import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
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
from codex_agents.routing import load_policy
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
    codex_exec_options,
    evaluate_admission,
    evaluate_launch,
    observe_claude,
    parse_client_version,
    window_start_ms,
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
CODEX_LIMITS = {
    "admission_policy": {
        "client_versions": {"minimum": "0.164.0", "maximum_exclusive": "0.164.1"},
        "max_admissions_per_window": 60,
    },
    "prior_run_admissions": [],
    "window_headroom": [],
}
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
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                    environ=environ, popen=popen,
                )
                self.assertEqual("provider_identity", records[0]["record_type"])
                real = os.path.realpath(executable)
                self.assertEqual([real, "auth", "status", "--json"], calls[0][0])
                # The version is read from the same resolved client, the same way.
                self.assertEqual([real, "--version"], calls[1][0])
                self.assertEqual(calls[0][1]["env"], calls[1][1]["env"])
                # This stand-in prints the auth JSON for every command, which
                # is not a version, so none is recorded.
                self.assertIsNone(records[0]["client_version"])
                self.assertTrue(calls[0][1]["start_new_session"])
                # The child gets only a minimal environment; overrides are dropped.
                self.assertEqual(
                    {"HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR"}, set(calls[0][1]["env"])
                )
                # TMPDIR is the caller's private directory, never the system one.
                self.assertEqual(directory, calls[0][1]["env"]["TMPDIR"])
                for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                    with self.subTest(name=name), self.assertRaisesRegex(
                        ContractError, "API key"
                    ):
                        observe_claude(
                            tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                            environ={**environ, name: "set"}, popen=popen,
                        )
            self.assertEqual(2, len(calls))

    def test_codex_exec_options_are_read_from_the_clients_help(self) -> None:
        sample = (
            "Options:\n  -c, --config <key=value>\n          Examples: - `-c model=\"o3\"`\n"
            "      --ignore-user-config\n          Do not load `$CODEX_HOME/config.toml`\n"
            "      --ignore-rules\n          Do not load user or project execpolicy files\n"
        )
        calls = []

        def popen(arguments, **options):
            calls.append((arguments, options))
            return subprocess.Popen(
                [sys.executable, "-c", f"import sys; sys.stdout.write({sample!r})"], **options
            )

        def failing(arguments, **options):
            return subprocess.Popen(["/usr/bin/false"], **options)

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            options = codex_exec_options(
                "/fake/codex", environ={"CODEX_HOME": "/elsewhere"},
                tmpdir=Path(directory), popen=popen,
            )
            self.assertEqual({"--config", "--ignore-user-config", "--ignore-rules"}, options)
            self.assertEqual(["/fake/codex", "exec", "--help"], calls[0][0])
            self.assertNotIn("CODEX_HOME", calls[0][1]["env"])
            self.assertTrue(calls[0][1]["start_new_session"])
            # A failed help run lists nothing, so the bridge refuses the launch.
            self.assertEqual(frozenset(), codex_exec_options(
                "/fake/codex", environ={}, tmpdir=Path(directory), popen=failing,
            ))

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
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
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
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                    environ={"PATH": str(trusted)},
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
                            tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW, environ={}, timeout=1,
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
                    root, digest_key=KEY, observed_at_ms=NOW, environ=environ,
                    tmpdir=Path(directory),
                )
            self.assertEqual(
                ["provider_identity", "provider_headroom", "provider_billing"],
                [record["record_type"] for record in records],
            )
            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"], log.read_text().split()
            )
            with trusted_tools(root):
                session = AppServerSession(root, environ=environ, tmpdir=root)
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
                AppServerSession(root, environ={**environ, "OPENAI_API_KEY": "x"}, tmpdir=root)


class HeadroomThresholdTests(unittest.TestCase):
    def test_claude_windows_use_the_codex_thresholds(self) -> None:
        from codex_agents.providers import _headroom_state

        def rate(used: float | None, status: str = "allowed") -> str | None:
            record = {
                "status": status, "observed_at_ms": NOW,
                "windows": [{"window_minutes": 300, "scope": "five_hour",
                             "used_percent": used, "resets_at": NOW + 1000}],
            }
            return _headroom_state("claude", record, NOW)

        for used, expected in (
            (74.9, "ample"), (75, "constrained"), (89.9, "constrained"),
            (90, "exhausted"), (100, "exhausted"), (None, "ample"),
        ):
            with self.subTest(used=used):
                self.assertEqual(expected, rate(used))
        self.assertEqual("exhausted", rate(10, "rejected"))
        self.assertEqual("constrained", rate(10, "allowed_warning"))
        record = {
            "status": "rejected", "observed_at_ms": NOW,
            "windows": [{"window_minutes": 300, "scope": "five_hour",
                         "used_percent": 95, "resets_at": NOW - 1}],
        }
        self.assertIsNone(_headroom_state("claude", record, NOW))


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

    def clean_claude_billing(self, at_ms: int) -> None:
        """Fresh, clean Claude billing at a given time, as a probe would record.

        Claude admission needs billing from the last 30 minutes, so tests that
        jump past a reset record it again at the new time.
        """

        identity = self.observe("claude", now_ms=at_ms)[0]
        _, billing = claude_rate_limit_records(
            {"status": "allowed", "isUsingOverage": False, "overageStatus": "rejected",
             "overageDisabledReason": "org_level_disabled"},
            account_fingerprint=identity["account_fingerprint"], observed_at_ms=at_ms,
        )
        self.ledger._store_provider_observations(self.project_id, [billing], timestamp=at_ms)

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

    def credits_attest(self, fingerprint: str, issued_at: int = 1) -> None:
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "codex", fingerprint,
            issued_at=issued_at, credits_spending_disabled=True,
        )

    def test_attested_disabled_credits_are_admitted_and_rechecked(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.credits_attest(fingerprint)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        decision = self.admit()
        self.assertEqual(("pass", "admitted"), (decision["decision"], decision["reason_code"]))
        # The observation itself still records credits as usable.
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("provider_billing")[-1]
        self.assertEqual(
            (True, True, False, None),
            (stored["has_credits"], stored["paid_usage_usable"], stored["unlimited"],
             stored["spend_control_reached"]),
        )
        run = self.open_run()
        after = self.observe(**{"rateLimits.credits.hasCredits": True})[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertIsNone(self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))
        self.observe(**{"rateLimits.credits.unlimited": True})
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertEqual("paid_usage_usable", self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))

    def test_credits_without_the_attested_field_are_denied(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")

    def test_unlimited_credits_are_denied_even_with_the_field(self) -> None:
        identity = self.observe()[0]
        self.credits_attest(identity["account_fingerprint"])
        self.observe(**{
            "rateLimits.credits.hasCredits": True, "rateLimits.credits.unlimited": True,
        })
        self.assert_denied("paid_usage_usable")

    def test_spend_control_reached_is_denied_with_its_own_reason(self) -> None:
        identity = self.observe()[0]
        self.credits_attest(identity["account_fingerprint"])
        self.observe(**{
            "rateLimits.credits.hasCredits": True, "rateLimits.spendControlReached": True,
        })
        self.assert_denied("codex_spend_control_reached")
        # Without credits, and without the field, it denies the same way.
        self.observe(**{"rateLimits.spendControlReached": True})
        self.assert_denied("codex_spend_control_reached")
        with self.ledger.repository(self.project_id) as repository:
            self.assertIs(True, repository.records("provider_billing")[-1]["spend_control_reached"])

    def test_credits_attestation_for_another_account_does_not_apply(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.credits_attest("0" * 64, issued_at=2)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")

    def test_the_credits_field_is_for_a_codex_grant_and_must_be_true(self) -> None:
        grant = {
            "version": 1, "provider": "claude", "account_fingerprint": "0" * 64,
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 5,
            "codex_credits_spending_disabled": True,
        }
        with self.assertRaisesRegex(ContractError, "only for a Codex grant"):
            billing_attestation_record(grant)
        with self.assertRaisesRegex(ContractError, "must be true"):
            billing_attestation_record(
                {**grant, "provider": "codex", "codex_credits_spending_disabled": False}
            )
        record = billing_attestation_record({**grant, "provider": "codex"})
        self.assertIs(True, record["codex_credits_spending_disabled"])
        older = {key: value for key, value in grant.items()
                 if key != "codex_credits_spending_disabled"}
        self.assertNotIn(
            "codex_credits_spending_disabled", billing_attestation_record(older)
        )

    def test_launch_recheck_function_honours_the_field(self) -> None:
        fingerprint = "a" * 64
        identity = {"account_fingerprint": fingerprint, "plan_type": "plus"}

        def billing(**changes) -> list:
            record = {
                "provider": "codex", "account_fingerprint": fingerprint,
                "paid_usage_usable": True, "has_credits": True, "unlimited": False,
                "spend_control_reached": False, "is_using_overage": None,
                "ordinary_usage_allowed": True, "observed_at_ms": NOW, **changes,
            }
            return [(record, DIGEST_C)]

        def attestation(fingerprint: str = fingerprint, **changes) -> list:
            record = {"provider": "codex", "account_fingerprint": fingerprint,
                      "observed_at_ms": 1, "codex_credits_spending_disabled": True, **changes}
            return [(record, DIGEST_B)]

        def launch(records, attestations):
            return evaluate_launch(
                provider="codex", identity=identity, billing=records, headroom=[],
                now_ms=NOW + 1, attestations=attestations,
            )

        self.assertIsNone(launch(billing(), attestation()))
        self.assertEqual("paid_usage_usable", launch(billing(), attestation(**{
            "codex_credits_spending_disabled": None})))
        self.assertEqual("paid_usage_usable", launch(billing(), None))
        self.assertEqual("paid_usage_usable", launch(billing(), attestation("b" * 64)))
        self.assertEqual("paid_usage_usable", launch(billing(unlimited=True), attestation()))
        self.assertEqual(
            "codex_spend_control_reached",
            launch(billing(spend_control_reached=True), attestation()),
        )

    def test_codex_without_recent_billing_is_denied(self) -> None:
        identity = parse_claude(
            provider_fixture("claude-auth-status.json"), digest_key=KEY, observed_at_ms=NOW
        )
        codex_identity = {**identity, "provider": "codex", "auth_method": None,
                          "subscription_type": None, "plan_type": "plus",
                          "client_version": "0.164.0"}
        attestation = {"provider": "codex",
                       "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        decision = evaluate_admission(
            provider="codex",
            identities=[(codex_identity, DIGEST_A)],
            attestations=[(attestation, DIGEST_B)],
            billing=[], headroom=[], prior_claude_admissions=set(),
            environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
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
            prior_claude_admissions=set(), environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
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
        # A run keeps only events more restrictive than its own earlier ones,
        # so a later favourable event is dropped rather than recorded.
        self.assertEqual([], self.event(
            real, status="allowed", rateLimitType="seven_day", utilization=0.0,
            resetsAt=future, isUsingOverage=False, overageStatus="rejected",
            overageDisabledReason="org_level_disabled",
        ))
        self.clock += 3_600_000
        self.clean_claude_billing(self.tick())
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

    # S3b: client version, Codex plan, the run-count backstop, restrictive
    # rate-limit events.

    def version(self, provider: str, text: str) -> None:
        (self.tools / f"{provider}-version.txt").write_text(text)

    def limit(self, provider: str, **settings) -> None:
        policy = deepcopy(load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json"))
        policy["providers"][provider].update(settings)
        self.ledger._admission_policy = policy

    def test_client_version_must_be_inside_the_reviewed_range(self) -> None:
        identity = self.observe()[0]
        self.assertEqual("0.164.0", identity["client_version"])
        self.attest("codex", identity["account_fingerprint"])
        for text, admitted in (
            ("codex-cli 0.164.0\n", True),   # the lower boundary is inside
            ("codex-cli 0.164.1\n", False),  # the upper boundary is outside
            ("codex-cli 0.164.99\n", False),
            ("codex-cli 0.165.0\n", False),
            ("codex-cli 0.163.9\n", False),
            ("codex-cli 1.0.0\n", False),
            ("", False),                       # missing
            ("codex-cli\n", False),
            ("codex-cli 0.164.0-alpha.1\n", False),
            ("codex-cli 0.164\n", False),
            ("codex-cli 0.164.0 (extra)\n", False),
            ("0.164.0\n", False),
            ("\xff\xfe garbage", False),
        ):
            with self.subTest(text=text):
                self.version("codex", text)
                observed = self.observe()[0]
                if admitted:
                    self.assertEqual("pass", self.admit()["decision"])
                else:
                    self.assert_denied("client_version_unreviewed")
                    if observed["client_version"] is not None:
                        self.assertNotEqual("0.164.0", observed["client_version"])
        claude = self.observe("claude")[0]
        self.attest("claude", claude["account_fingerprint"])
        for text, admitted in (
            ("2.1.0 (Claude Code)\n", True),
            ("2.1.123 (Claude Code)\n", True),
            ("2.2.0 (Claude Code)\n", False),
            ("2.0.99 (Claude Code)\n", False),
            ("Claude Code\n", False),
            ("", False),
        ):
            with self.subTest(text=text):
                self.version("claude", text)
                self.observe("claude")
                if admitted:
                    self.assertEqual("pass", self.admit("claude")["decision"])
                else:
                    self.assert_denied("client_version_unreviewed", "claude")

    def test_client_version_parsing_keeps_only_a_plain_version(self) -> None:
        self.assertEqual("2.1.123", parse_client_version("claude", b"2.1.123 (Claude Code)\n"))
        self.assertEqual("2.1.123", parse_client_version("claude", "2.1.123"))
        self.assertEqual("0.164.0", parse_client_version("codex", b"codex-cli 0.164.0\n"))
        for provider, raw in (
            ("codex", b"0.164.0"), ("claude", b"codex-cli 0.164.0"),
            ("claude", b"2.1.123 (Claude Code)\n2.1.124"), ("claude", b"\xff"),
            ("claude", b"9" * 300), ("claude", b"02.1.0"), ("gemini", b"1.0.0"),
            ("claude", None), ("codex", b"codex-cli 0.164.0 fixture-account-5c2e08"),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_client_version(provider, raw))
        with self.assertRaisesRegex(ContractError, "plain semantic version"):
            validate_learning_record({
                **self.observe()[0], "client_version": "0.164.0 fixture-account-5c2e08",
            })

    def test_admission_without_usable_settings_is_denied(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.ledger._admission_policy = {"providers": {}}
        self.assert_denied("admission_policy_unavailable")
        self.limit("codex", client_versions={"minimum": "1.0.0", "maximum_exclusive": "0.1.0"})
        self.assert_denied("admission_policy_unavailable")
        self.ledger._admission_policy = None
        self.assertEqual("pass", self.admit()["decision"])

    def test_codex_requires_a_subscription_plan(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        for plan in (
            "free", "fixture-account-5c2e08", "unknown", None,
            "self_serve_business_usage_based", "enterprise_cbp_usage_based",
        ):
            with self.subTest(plan=plan):
                self.observe(**{"rateLimits.planType": plan})
                self.assert_denied("codex_plan_not_subscription")
        result = json.loads(provider_fixture("codex-rate-limits.json"))
        del result["rateLimits"]["planType"]
        (self.tools / "codex-result.json").write_text(json.dumps(result))
        with trusted_tools(self.tools), mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.tick()
        ):
            absent = self.ledger.observe_provider(
                self.project_id, "codex", project_root=self.tools, environ=CLEAN
            )[0]
        self.assertIsNone(absent["plan_type"])
        self.assert_denied("codex_plan_not_subscription")
        for plan in ("plus", "pro", "team", "business", "enterprise"):
            with self.subTest(plan=plan):
                self.observe(**{"rateLimits.planType": plan})
                self.assertEqual("pass", self.admit()["decision"])

    def test_run_cap_counts_admissions_in_the_current_window(self) -> None:
        self.limit("codex", max_admissions_per_window=2)
        resets = self.clock + 3_600_000
        reset_at = {"rateLimits.secondary.resetsAt": resets // 1000}
        identity = self.observe(**reset_at)[0]
        fingerprint = identity["account_fingerprint"]
        self.attest("codex", fingerprint)
        self.open_run()   # under the cap
        self.open_run()   # reaches the cap
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")
        self.assertEqual(
            ("deny", "window_run_cap_reached", None),
            (stored[-1]["decision"], stored[-1]["reason_code"], stored[-1]["run_id"]),
        )
        self.assertEqual(2, len([item for item in stored if item["decision"] == "pass"]))
        # Over the cap stays denied; a denial is not an admission and never counts.
        self.assert_denied("window_run_cap_reached")
        # Another account's admissions are its own.
        other = self.observe(accountId="fixture-account-other", **reset_at)[0]
        self.assertNotEqual(fingerprint, other["account_fingerprint"])
        self.attest("codex", other["account_fingerprint"])
        self.assertEqual("pass", self.ledger.host_run_admission(
            self.project_id, self.open_run()["run_handle"]
        )["decision"])
        # The window resets: the next window starts five hours before its reset.
        self.clock = resets + 1
        self.observe(**{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000})
        self.assertEqual("pass", self.ledger.host_run_admission(
            self.project_id, self.open_run()["run_handle"]
        )["decision"])
        self.limit("codex", max_admissions_per_window=None)
        self.open_run()
        self.open_run()

    def test_run_cap_window_falls_back_to_the_last_five_hours(self) -> None:
        identity = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        fingerprint = identity[0]["account_fingerprint"]
        attestation = {"provider": "codex", "account_fingerprint": fingerprint,
                       "observed_at_ms": 1}
        five_hours = 5 * 60 * 60 * 1000

        def decide(prior, headroom=()):
            return evaluate_admission(
                provider="codex",
                identities=[(identity[0], DIGEST_A)],
                attestations=[(attestation, DIGEST_B)],
                billing=[(identity[2], DIGEST_C)], headroom=list(headroom),
                prior_claude_admissions=set(), environ={}, now_ms=NOW + 1,
                admission_policy={
                    "client_versions": {"minimum": "0.164.0", "maximum_exclusive": "0.164.1"},
                    "max_admissions_per_window": 2,
                },
                prior_run_admissions=prior,
                window_headroom=list(headroom),
            )["reason_code"]

        now = NOW + 1
        inside, edge = now - five_hours + 1, now - five_hours
        self.assertEqual("admitted", decide([(fingerprint, inside)]))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, inside)] * 2))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, inside)] * 3))
        self.assertEqual("admitted", decide([(fingerprint, edge)] * 2))
        self.assertEqual("admitted", decide([(DIGEST_C, inside)] * 2))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, now + 1)] * 2))
        # The fixture's five-hour reset is years away, so it is not trusted.
        self.assertEqual(
            (now - five_hours + 1, None),
            window_start_ms("codex", fingerprint, [(identity[1], DIGEST_C)], now),
        )
        soon = {**identity[1], "windows": [
            {**identity[1]["windows"][0], "resets_at": now + 60_000},
        ]}
        self.assertEqual(
            (now + 60_000 - five_hours, DIGEST_C),
            window_start_ms("codex", fingerprint, [(soon, DIGEST_C)], now),
        )
        self.assertEqual(
            "window_run_cap_reached",
            decide([(fingerprint, now + 60_000 - five_hours)] * 2, [(soon, DIGEST_C)]),
        )
        self.assertEqual(
            "admitted",
            decide([(fingerprint, now + 60_000 - five_hours - 1)] * 2, [(soon, DIGEST_C)]),
        )

    def test_many_headroom_records_keep_the_decision_bounded(self) -> None:
        from codex_agents.providers import admission_record

        records = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        identity, headroom, billing = records
        attestation = {"provider": "codex", "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        many = [
            ({**headroom, "observed_at_ms": NOW - index}, f"{index:064x}")
            for index in range(200)
        ]
        exhausted = {**headroom, "windows": [
            {**headroom["windows"][0], "used_percent": 95.0, "resets_at": NOW + 60_000},
        ]}

        def decide(items):
            return evaluate_admission(
                provider="codex", identities=[(identity, DIGEST_A)],
                attestations=[(attestation, DIGEST_B)], billing=[(billing, DIGEST_C)],
                headroom=items, prior_claude_admissions=set(), environ={},
                now_ms=NOW + 1, **CODEX_LIMITS,
            )

        passed = decide(many)
        self.assertEqual("admitted", passed["reason_code"])
        self.assertIn(many[-1][1], passed["evidence_digests"])
        self.assertLessEqual(len(passed["evidence_digests"]), 5)
        admission_record(passed, "run-1")
        denied = decide([*many[:100], (exhausted, "e" * 64), *many[100:]])
        self.assertEqual("provider_exhausted", denied["reason_code"])
        self.assertIn("e" * 64, denied["evidence_digests"])
        admission_record(denied, None)

    def test_restrictive_evidence_stamped_after_now_still_counts(self) -> None:
        identity, headroom, billing = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        attestation = {"provider": "codex", "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        ahead = NOW + 60_000

        def decide(more_billing=(), more_headroom=()):
            return evaluate_admission(
                provider="codex", identities=[(identity, DIGEST_A)],
                attestations=[(attestation, DIGEST_B)],
                billing=[(billing, DIGEST_C), *more_billing],
                headroom=[(headroom, DIGEST_C), *more_headroom],
                prior_claude_admissions=set(), environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
            )["reason_code"]

        self.assertEqual("admitted", decide())
        paid = {**billing, "paid_usage_usable": True, "observed_at_ms": ahead}
        self.assertEqual("paid_usage_usable", decide(more_billing=[(paid, "a" * 64)]))
        blocked = {**billing, "ordinary_usage_allowed": False, "observed_at_ms": ahead}
        self.assertEqual("provider_exhausted", decide(more_billing=[(blocked, "c" * 64)]))
        full = {**headroom, "observed_at_ms": ahead, "windows": [
            {**headroom["windows"][0], "used_percent": 95.0, "resets_at": ahead + 60_000},
        ]}
        self.assertEqual("provider_exhausted", decide(more_headroom=[(full, "b" * 64)]))

    def test_billing_probe_admissions_do_not_count_toward_the_cap(self) -> None:
        self.limit("claude", max_admissions_per_window=2)
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        self.event(first, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        with self.ledger._lock, mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.tick()
        ):
            probe = self.ledger._evaluate_admission_locked(
                self.project_id, provider="claude", environ=CLEAN, timestamp=self.clock
            )
            self.assertEqual("pass", probe["decision"])
            for _ in range(3):
                self.ledger._store_probe_admission_locked(
                    self.project_id, {**probe, "observed_at_ms": self.tick()},
                    timestamp=self.clock,
                )
        self.open_run("claude")
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run("claude")
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)

    def test_a_run_keeps_every_more_restrictive_rate_limit_event(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        later = (self.clock + 3_600_000) // 1000

        def kept(**info):
            return [record["record_type"] for record in self.event(run, **info)]

        base = {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2,
                "resetsAt": later, **quiet}
        self.assertEqual(["provider_headroom", "provider_billing"], kept(**base))
        self.assertEqual([], kept(**base))
        self.assertEqual([], kept(**{**base, "utilization": 0.1}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "utilization": 0.5}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "status": "allowed_warning"}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "rateLimitType": "seven_day"}))
        self.assertEqual(
            ["provider_billing"], kept(**{**base, "status": "allowed_warning",
                                         "overageDisabledReason": None}),
        )
        # Once billing is unknown, a favourable event cannot restore it.
        self.assertEqual([], kept(**{**base, "status": "allowed_warning"}))
        self.assert_denied("billing_unverified", "claude")
        with self.ledger.repository(self.project_id) as repository:
            headroom = [record for record in repository.records("provider_headroom")
                        if record["run_id"] is not None]
        self.assertEqual({"during"}, {record["phase"] for record in headroom})
        self.assertEqual(4, len(headroom))

    def test_a_run_cannot_record_rate_limit_events_without_bound(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        first_reset = (self.clock + 3_600_000) // 1000
        later_reset = (self.clock + 7_200_000) // 1000
        recorded = len(self.event(run, status="rejected", rateLimitType="five_hour",
                                  utilization=1.0, resetsAt=first_reset, **quiet))
        # Allowed readings that run longer than the rejection keep arriving
        # until the run reaches the cap.
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            for step in range(200):
                recorded += len(self.event(
                    run, status="allowed", rateLimitType="five_hour",
                    utilization=step / 1000, resetsAt=later_reset, **quiet,
                ))
        with self.ledger.repository(self.project_id) as repository:
            stored = [
                record for kind in ("provider_headroom", "provider_billing")
                for record in repository.records(kind) if record["phase"] == "during"
            ]
            merged = [record for record in repository.records("provider_headroom")
                      if record["phase"] == "during"][-1]
        self.assertEqual(63, recorded)
        self.assertEqual(64, len(stored))
        # At the cap the run's whole restriction is merged: the highest status
        # with the latest reset.
        self.assertEqual(
            ("rejected", 100.0, later_reset * 1000),
            (merged["status"], merged["windows"][0]["used_percent"],
             merged["windows"][0]["resets_at"]),
        )
        # The run is fenced: later events are refused and nothing is stored.
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            self.event(run, status="rejected", rateLimitType="five_hour",
                       utilization=1.0, resetsAt=later_reset + 60, **quiet)
        with self.ledger.repository(self.project_id) as repository:
            self.assertEqual(64, len([
                record for kind in ("provider_headroom", "provider_billing")
                for record in repository.records(kind) if record["phase"] == "during"
            ]))
        # After the earlier reset the merged restriction still applies...
        after_first = first_reset * 1000 + 1
        self.clean_claude_billing(after_first)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_first + 1)
        # ...and it lifts at the later reset.
        after_later = later_reset * 1000 + 1
        self.clean_claude_billing(after_later)
        self.assertEqual("pass", self.admit("claude", now_ms=after_later + 1)["decision"])

    def test_a_rejection_is_not_covered_by_a_longer_allowed_reading(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        first_reset = (self.clock + 3_600_000) // 1000
        later_reset = (self.clock + 7_200_000) // 1000
        self.event(run, status="rejected", rateLimitType="five_hour", utilization=1.0,
                   resetsAt=first_reset, **quiet)
        self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.5,
                   resetsAt=later_reset, **quiet)
        kept = self.event(run, status="rejected", rateLimitType="five_hour",
                          utilization=1.0, resetsAt=later_reset, **quiet)
        self.assertEqual(["provider_headroom"], [item["record_type"] for item in kept])
        after_first = first_reset * 1000 + 1
        self.clean_claude_billing(after_first)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_first + 1)

    def test_a_later_reset_reading_cannot_free_counted_admissions(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        first = {"rateLimits.secondary.resetsAt": (self.clock + 3_600_000) // 1000}
        identity = self.observe(**first)[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        # A later reading reports a reset five hours away, which alone would
        # start the window now; the earlier window is still live, so it counts.
        self.observe(**{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000})
        self.assert_denied("window_run_cap_reached")
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        headroom = [
            ({"provider": "codex", "account_fingerprint": DIGEST_A, "observed_at_ms": NOW,
              "windows": [{"window_minutes": 300, "scope": None, "used_percent": 1.0,
                           "resets_at": reset}]}, digest)
            for reset, digest in ((NOW + 3_600_000, DIGEST_B), (NOW + 5 * 3_600_000, DIGEST_C))
        ]
        self.assertEqual(
            (NOW + 3_600_000 - 5 * 3_600_000, DIGEST_B),
            window_start_ms("codex", DIGEST_A, headroom, NOW),
        )
        # Once the earlier window resets, the later reading places the window.
        self.assertEqual(
            (NOW, DIGEST_C), window_start_ms("codex", DIGEST_A, headroom, NOW + 3_600_000 + 1)
        )

    def test_the_run_cap_counts_the_account_across_projects(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        other_root = Path(self.temporary.name) / "other-project"
        other_root.mkdir()
        first_project = self.project_id
        self.project_id = self.ledger.register_project(other_root, now_ms=1)
        try:
            other = self.observe()[0]
            self.assertEqual(identity["account_fingerprint"], other["account_fingerprint"])
            self.attest("codex", other["account_fingerprint"])
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
            self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        finally:
            self.project_id = first_project

    def test_the_cap_window_is_placed_from_every_projects_readings(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        identity = self.observe(
            **{"rateLimits.secondary.resetsAt": (self.clock + 3_600_000) // 1000}
        )[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        other_root = Path(self.temporary.name) / "other-project"
        other_root.mkdir()
        first_project = self.project_id
        self.project_id = self.ledger.register_project(other_root, now_ms=1)
        try:
            self.clock += 10_000
            # Project B alone sees a reset five hours away, which would start
            # its window after project A's admission.
            other = self.observe(
                **{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000}
            )[0]
            self.attest("codex", other["account_fingerprint"])
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
            self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
            # The denial is stored in this project, citing only its own records
            # (the ledger refuses evidence from another project).
            with self.ledger.repository(self.project_id) as repository:
                stored = repository.records("admission_decision")[-1]
            self.assertEqual(
                ("deny", "window_run_cap_reached"), (stored["decision"], stored["reason_code"])
            )
        finally:
            self.project_id = first_project

    def test_a_reset_less_rejection_survives_the_overflow_merge(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        reset = (self.clock + 3_600_000) // 1000
        recorded = 0
        for step in range(62):
            recorded += len(self.event(
                run, status="allowed", rateLimitType="five_hour",
                utilization=step / 1000, resetsAt=reset, **quiet,
            ))
        self.assertEqual(63, recorded)
        rejected_at = self.clock + 1
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            self.event(run, status="rejected", rateLimitType="five_hour",
                       utilization=1.0, **quiet)
        with self.ledger.repository(self.project_id) as repository:
            merged = [record for record in repository.records("provider_headroom")
                      if record["phase"] == "during"][-1]
        self.assertEqual("rejected", merged["status"])
        # The reset-less rejection lasts its day, not the allowed readings' hour.
        self.assertEqual(rejected_at + 24 * 3_600_000, merged["windows"][0]["resets_at"])
        after_reset = reset * 1000 + 1
        self.clean_claude_billing(after_reset)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_reset + 1)

    @contextlib.contextmanager
    def other_project(self):
        """Run the block in a second project of the same ledger and account."""

        root = Path(self.temporary.name) / f"project-{self.tick()}"
        root.mkdir()
        first = self.project_id
        self.project_id = self.ledger.register_project(root, now_ms=1)
        try:
            fingerprint = self.observe("claude")[0]["account_fingerprint"]
            self.attest("claude", fingerprint)
            yield
        finally:
            self.project_id = first

    def test_overage_in_one_project_denies_another(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.1,
                   isUsingOverage=True)
        with self.other_project():
            self.assert_denied("paid_usage_usable", "claude")
            with self.ledger.repository(self.project_id) as repository:
                cited = repository.records("admission_decision")[-1]["evidence_digests"]
            with self.ledger._lock:
                rows = self.ledger._connection.execute(
                    "SELECT content_digest,record_type FROM records WHERE project_id=?",
                    (self.project_id,),
                ).fetchall()
            own = {str(row[0]) for row in rows}
            billing_here = {str(row[0]) for row in rows if row[1] == "provider_billing"}
            # The other project's billing decides, but only this project's
            # records are cited, and this project holds no billing at all.
            self.assertTrue(cited and set(cited) <= own)
            self.assertEqual(set(), billing_here)

    def test_the_bootstrap_is_once_per_account(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        stored = self.ledger.host_run_admission(
            self.project_id, self.open_run("claude")["run_handle"]
        )
        self.assertEqual("bootstrap", stored["admission_basis"])
        with self.other_project():
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run("claude")
            self.assertEqual("billing_unverified", raised.exception.reason_code)

    def test_a_rejection_in_one_project_blocks_another_until_reset(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        reset = (self.clock + 3_600_000) // 1000
        self.event(run, status="rejected", rateLimitType="five_hour", utilization=1.0,
                   resetsAt=reset, isUsingOverage=False, overageStatus="rejected",
                   overageDisabledReason="org_level_disabled")
        with self.other_project():
            self.assert_denied("provider_exhausted", "claude")
            after = reset * 1000 + 1
            self.clean_claude_billing(after)
            self.assertEqual("pass", self.admit("claude", now_ms=after + 1)["decision"])

    def test_claude_billing_must_be_fresh_except_for_the_bootstrap(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        self.event(run, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        self.assertEqual("pass", self.admit("claude")["decision"])
        day_later = self.clock + 24 * 60 * 60 * 1000
        self.observe("claude", now_ms=day_later)
        self.assert_denied("billing_unverified", "claude", now_ms=day_later + 1)
        # A probe is still admitted on that older clean evidence, to refresh it.
        with self.ledger._lock:
            probe = self.ledger._evaluate_admission_locked(
                self.project_id, provider="claude", environ=CLEAN,
                timestamp=day_later + 2, probe=True,
            )
        self.assertEqual("pass", probe["decision"])
        self.clean_claude_billing(day_later + 3)
        self.assertEqual("pass", self.admit("claude", now_ms=day_later + 4)["decision"])

    def test_a_project_holding_account_restrictions_cannot_be_deleted(self) -> None:
        with self.other_project():
            run = self.open_run("claude")
            self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.1,
                       isUsingOverage=True)
            held = self.project_id
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                self.ledger.delete_project(held)
            # A caller's time cannot age the restriction out; only the ledger clock.
            with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                self.ledger.delete_project(held, now_ms=self.clock + 10 * 24 * 3_600_000)
        with mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.clock + 24 * 3_600_000 + 1
        ):
            self.assertTrue(self.ledger.delete_project(held).startswith("del_"))

    def test_spend_control_denial_survives_deleting_the_recording_project(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.credits_attest(fingerprint)
        first = self.project_id
        # Another project of the same account records spend control reached.
        with self.other_project():
            self.attest("codex", fingerprint)
            self.credits_attest(fingerprint, issued_at=2)
            self.observe(**{
                "rateLimits.credits.hasCredits": True,
                "rateLimits.spendControlReached": True,
            })
            held = self.project_id
            with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                    self.ledger.delete_project(held)
        self.assertEqual(first, self.project_id)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("codex_spend_control_reached")
        # A reading stamped in the future counts too.
        with self.other_project():
            future = self.clock + 3_600_000
            self.observe(now_ms=future, **{"rateLimits.spendControlReached": True})
            with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                    self.ledger.delete_project(self.project_id)

    def test_billing_records_without_spend_control_verify_in_a_backup(self) -> None:
        identity = self.observe()[0]
        legacy = self.observe()[2]
        del legacy["spend_control_reached"]
        legacy = {**legacy, "record_id": "provider_billing:legacy"}
        self.assertNotIn("spend_control_reached", validate_learning_record(legacy))
        self.ledger._store_provider_observations(
            self.project_id, [legacy], timestamp=self.tick()
        )
        self.assertEqual(identity["provider"], "codex")
        self.ledger.create_sanitized_backup(
            self.project_id, file_id="legacy_billing",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        self.ledger.verify_sanitized_backup(self.project_id, file_id="legacy_billing")
        with self.ledger.repository(self.project_id) as repository:
            stored = [r for r in repository.records("provider_billing")
                      if r["record_id"] == "provider_billing:legacy"]
        self.assertEqual(1, len(stored))
        self.assertNotIn("spend_control_reached", stored[0])
        with self.assertRaises(ContractError):
            validate_learning_record({**legacy, "spend_control_reached": "yes"})

    def test_the_locked_recheck_reads_the_clock_it_holds(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1", now_ms=NOW
        )
        fresh = self.clock + 1
        # The opener passes its first check, then waits for the lock past the
        # point where the identity is too old to admit.
        stale = identity["observed_at_ms"] + IDENTITY_MAX_AGE_MS + 1
        times = iter([fresh, stale])
        with mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: next(times)), \
                self.assertRaises(AdmissionDenied) as raised:
            self._open_run("codex", orchestration, fresh)
        self.assertEqual("provider_identity_missing", raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")[-1]
        self.assertEqual(
            ("provider_identity_missing", stale), (stored["reason_code"], stored["observed_at_ms"])
        )
        # A pass is stamped with the time the lock was held, too.
        self.observe()
        later = self.clock + 5
        times = iter([self.clock + 1, later])
        with mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: next(times)):
            opened = self._open_run("codex", orchestration, self.clock + 1)
        self.assertEqual(
            later,
            self.ledger.host_run_admission(self.project_id, opened["run_handle"])["observed_at_ms"],
        )

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
