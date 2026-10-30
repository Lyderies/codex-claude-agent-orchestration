from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import FIXTURES_ROOT, ORCHESTRATION_ROOT, TEMP_ROOT

import cli
from codex_agents.bridge import BridgeRefused, dispatch
from codex_agents.contracts import ContractError, validate_result
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import content_digest
from codex_agents.learning_engine import installed_execution_configuration
from codex_agents.learning_store import (
    LearningLedger,
    LearningState,
    LedgerError,
    authorize_collector_runtime,
)
from codex_agents.routing import load_policy, route_task
from tests.learning_support import (
    assignment,
    attest_provider,
    fake_provider_tools,
    generate_test_owner_key,
    result,
    routing_fields,
    trusted_tools,
)

RESULT_SCHEMA = (ORCHESTRATION_ROOT / "schemas" / "result.schema.json").read_bytes()
PARENT_GRANT = {
    "tools": ["shell"], "filesystem": ["source.txt:read"],
    "network": False, "mcps": [], "external_effects": False,
}
CLEAN: dict[str, str] = {}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def init_frame(**changes) -> dict:
    return {
        "type": "system", "subtype": "init", "session_id": "session-1",
        "apiKeySource": "none", "model": "claude-opus-5-5", "plugins": [],
        "mcp_servers": [], "tools": ["Read", "Glob", "Grep"], **changes,
    }


def result_frame(text: str | None = None) -> dict:
    return {
        "type": "result", "subtype": "success", "is_error": False,
        "result": text if text is not None else json.dumps(result()),
        "usage": {"input_tokens": 120, "output_tokens": 30,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "total_cost_usd": 0.42,
        "permission_denials": [{"tool_name": "Read", "tool_input": {"file_path": "/x"}}],
    }


RATE_LIMIT = {
    "type": "rate_limit_event", "session_id": "session-1", "uuid": "rate-1",
    "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2,
        "isUsingOverage": False, "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
    },
}


class BridgeTestCase(unittest.TestCase):
    """A git project, an installed bundle with a Claude agent, and fake clients."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "source.txt").write_text("marker\n")
        (self.project / "secret.txt").write_text("not exported\n")
        git(self.project, "init", "-q")
        git(self.project, "config", "user.email", "tests@example.invalid")
        git(self.project, "config", "user.name", "Bridge Tests")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "project")
        self.bundle = base / "bundle"
        shutil.copytree(ORCHESTRATION_ROOT.parent.parent, self.bundle)
        agent = self.bundle / ".claude/agents/claude_agents_researcher.md"
        agent.parent.mkdir(parents=True)
        agent.write_text(
            (FIXTURES_ROOT / "claude/claude_agents_researcher.md")
            .read_text()
            .replace("\n---\n", "\nmaxTurns: 8\n---\n", 1)
        )
        self.tools = base / "tools"
        fake_provider_tools(self.tools)
        trusted = trusted_tools(self.tools)
        trusted.__enter__()
        self.addCleanup(trusted.__exit__, None, None, None)
        self.state = LearningState(base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_key, public_key = generate_test_owner_key(base)
        self.state.install_owner_public_key(public_key)
        self.clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.addCleanup(self.ledger.close)
        self.project_id = self.ledger.register_project(self.project, now_ms=1)
        self.policy = load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def frames(self, name: str, frames: list[dict]) -> None:
        (self.tools / f"{name}-frames.jsonl").write_text(
            "".join(json.dumps(frame) + "\n" for frame in frames)
        )

    def invocations(self, name: str) -> list[dict]:
        path = self.tools / f"{name}-invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if (
            path.exists()
        ) else []

    def admit(self, provider: str) -> str:
        identity = self.ledger.observe_provider(
            self.project_id, provider, project_root=self.tools, environ=CLEAN
        )[0]
        attest_provider(
            self.ledger, self.project_id, self.owner_key, provider,
            identity["account_fingerprint"],
        )
        return identity["account_fingerprint"]

    def task(self) -> dict:
        return {
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "medium", "risk": "low", "meaningful": True,
            **routing_fields(acceptance_strength="independent_judgment"),
            "deterministic_validation": False, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "owned_paths": [], "prohibited_actions": ["network"],
        }

    def bridged_route(self, provider: str, *, timeout_seconds: int | None = None) -> dict:
        policy = deepcopy(self.policy)
        config = {
            "version": 1,
            "providers": {
                "codex": {"enabled": provider == "codex"},
                "claude": {"enabled": provider == "claude"},
            },
            "codex_primary_may_bridge_to_claude": True,
        }
        policy["provider_routing"] = config
        policy["provider_config_digest"] = content_digest(config)
        # A Claude child is bridged from a Codex primary, and the reverse.
        primary = "codex" if provider == "claude" else "claude"
        route = route_task(self.task(), policy, primary_provider=primary)
        self.assertEqual((provider, "bridge"), (route["provider"], route["dispatch"]))
        if timeout_seconds is not None:
            route["bounds"]["timeout_seconds"] = timeout_seconds
        route["execution_configuration"] = installed_execution_configuration(
            self.bundle, route, client_capability=self.clients["bridge-v1"]
        )
        return route

    def assignment_for(self, route: dict, **changes) -> dict:
        assigned = assignment()
        assigned.update(role=route["role"], model=route["model"], effort=route["effort"])
        assigned["bounds"]["timeout_seconds"] = min(
            assigned["bounds"]["timeout_seconds"], route["bounds"]["timeout_seconds"]
        )
        assigned.update(changes)
        return assigned

    def open(self, route: dict, assigned: dict) -> str:
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1"
        )
        return self.ledger.open_host_run(
            self.project_id,
            assignment=assigned,
            task=self.task(),
            route=route,
            execution_configuration=route["execution_configuration"],
            client_version="bridge-v1",
            orchestration_handle=orchestration["orchestration_handle"],
        )["run_handle"]

    def dispatch(self, route: dict, assigned: dict, handle: str, **options) -> dict:
        return dispatch(
            self.ledger, self.project_id,
            project_root=self.project, bundle_root=self.bundle,
            task=options.pop("task", self.task()), route=route, assignment=assigned,
            parent_grant=options.pop("parent_grant", PARENT_GRANT),
            run_handle=handle, result_schema=RESULT_SCHEMA,
            environ=options.pop("environ", CLEAN),
        )

    def claude_run(self, **route_options) -> tuple[dict, dict, str]:
        if not getattr(self, "claude_seeded", False):
            # The first Claude run is a bootstrap; its own rate-limit event gives
            # the billing evidence every later admission needs.
            self.admit("claude")
            route = self.bridged_route("claude")
            seed = self.open(route, self.assignment_for(route))
            self.ledger.record_claude_rate_limit_event(
                self.project_id, run_handle=seed,
                rate_limit_info=RATE_LIMIT["rate_limit_info"],
            )
            self.claude_seeded = True
        route = self.bridged_route("claude", **route_options)
        assigned = self.assignment_for(route)
        return route, assigned, self.open(route, assigned)

    def assert_process_gone(self, invocation: dict) -> None:
        with self.assertRaises(ProcessLookupError):
            os.killpg(invocation["pgid"], 0)

    def workspace_entries(self) -> list[Path]:
        bridge = Path(self.state.root) / "bridge"
        return list(bridge.iterdir()) if bridge.exists() else []


class ClaudeBridgeTests(BridgeTestCase):
    def test_happy_path_runs_contained_and_records_one_rate_limit_event(self) -> None:
        route, assigned, handle = self.claude_run()
        self.frames("claude", [
            init_frame(), RATE_LIMIT,
            {**RATE_LIMIT, "uuid": "rate-2",
             "rate_limit_info": {**RATE_LIMIT["rate_limit_info"], "status": "rejected"}},
            result_frame(),
        ])
        output = self.dispatch(route, assigned, handle)
        self.assertEqual(result(), output["result"])
        report = output["bridge_report"]
        self.assertEqual(
            ("claude", "claude-opus-5-5", "claude-opus-5-5", "enforced", "result", 1),
            (report["provider"], report["requested_model"], report["observed_model"],
             report["read_containment"], report["outcome"], report["export_file_count"]),
        )
        self.assertIsNone(report["terminal_state"])
        self.assertEqual(0.42, report["usage"]["api_equivalent_cost_usd_estimate"])
        self.assertEqual({"count": 1, "tools": ["Read"]}, report["permission_denials"])
        self.assertNotIn("/x", json.dumps(output))
        invocation = [item for item in self.invocations("claude") if "-p" in item["argv"]][0]
        argv = invocation["argv"]
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--verbose"):
            self.assertIn(flag, argv)
        self.assertNotIn("--bare", argv)
        self.assertEqual("Read,Glob,Grep", argv[argv.index("--tools") + 1])
        self.assertEqual("dontAsk", argv[argv.index("--permission-mode") + 1])
        self.assertEqual("project", argv[argv.index("--setting-sources") + 1])
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        # The child ran in a private export holding only the committed read paths.
        self.assertEqual(["source.txt"], invocation["files"])
        self.assertNotEqual(str(self.project), invocation["cwd"])
        self.assertTrue(invocation["cwd"].startswith(str(self.state.root)))
        self.assertEqual(
            {"HOME", "LANG", "LOGNAME", "PATH", "TMPDIR", "USER"},
            set(invocation["env"]) - {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"},
        )
        self.assertEqual([], self.workspace_entries())
        self.assert_process_gone(invocation)
        with self.ledger.repository(self.project_id) as repository:
            intents = repository.records("dispatch_intent")
            run_events = [
                record for record in repository.records("provider_headroom")
                if record["run_id"] == intents[0]["run_id"]
            ]
        self.assertEqual(["allowed"], [record["status"] for record in run_events])
        self.assertEqual(
            [report["bridge_run_id"]], [intent["bridge_run_id"] for intent in intents]
        )
        # The launch is never resent for the same run handle.
        with self.assertRaisesRegex(LedgerError, "already dispatched"):
            self.dispatch(route, assigned, handle)

    def test_every_init_frame_violation_fails_closed_and_kills_the_child(self) -> None:
        cases = {
            "init_api_key_source": init_frame(apiKeySource="ANTHROPIC_API_KEY"),
            "init_model_mismatch": init_frame(model="claude-sonnet-5-5"),
            "init_plugin_loaded": init_frame(plugins=[{"name": "cc-plugin-easel"}]),
            "init_mcp_server_loaded": init_frame(mcp_servers=[{"name": "x"}]),
            "init_tool_not_allowed": init_frame(tools=["Read", "Bash"]),
            "missing_init_frame": result_frame(),
        }
        for reason, first in cases.items():
            with self.subTest(reason=reason):
                route, assigned, handle = self.claude_run()
                (self.tools / "claude-invocations.jsonl").unlink(missing_ok=True)
                self.frames("claude", [first, {"sleep": 30}, result_frame()])
                output = self.dispatch(route, assigned, handle)
                report = output["bridge_report"]
                self.assertEqual(reason, report["outcome"])
                self.assertEqual("failed", report["terminal_state"])
                self.assertEqual(
                    ("failed", "failed"),
                    (output["result"]["status"], output["result"]["execution_state"]),
                )
                validate_result(output["result"], assignment=assigned)
                self.assertLess(report["wall_seconds"], 20)
                child = [i for i in self.invocations("claude") if "-p" in i["argv"]][0]
                self.assert_process_gone(child)

    def test_timeout_kills_the_group_and_is_ambiguous(self) -> None:
        route, assigned, handle = self.claude_run(timeout_seconds=1)
        self.frames("claude", [init_frame(), {"sleep": 60}, result_frame()])
        output = self.dispatch(route, assigned, handle)
        self.assertEqual(
            ("timed_out", "timed_out"),
            (output["bridge_report"]["outcome"], output["bridge_report"]["terminal_state"]),
        )
        self.assertEqual("timed_out", output["result"]["execution_state"])
        validate_result(output["result"], assignment=assigned)
        child = [i for i in self.invocations("claude") if "-p" in i["argv"]][0]
        self.assert_process_gone(child)
        self.assertEqual([], self.workspace_entries())

    def test_unparseable_output_fails_closed(self) -> None:
        for frames, reason in (
            ([init_frame(), {"raw": "not json"}], "unparseable_stream"),
            ([init_frame(), result_frame("I could not finish.")], "unparseable_result"),
            ([init_frame()], "missing_result"),
            ([init_frame(), {**result_frame(), "is_error": True}], "child_reported_error"),
        ):
            with self.subTest(reason=reason):
                route, assigned, handle = self.claude_run()
                self.frames("claude", frames)
                output = self.dispatch(route, assigned, handle)
                self.assertEqual(reason, output["bridge_report"]["outcome"])
                self.assertEqual("failed", output["result"]["execution_state"])
                self.assertNotEqual("completed", output["result"]["status"])

    def test_account_change_before_launch_aborts(self) -> None:
        route, assigned, handle = self.claude_run()
        status = json.loads((self.tools / "claude-status.json").read_text())
        status["email"] = "someone-else@example.invalid"
        (self.tools / "claude-status.json").write_text(json.dumps(status))
        self.frames("claude", [init_frame(), result_frame()])
        with self.assertRaisesRegex(BridgeRefused, "differs from the admitted account"):
            self.dispatch(route, assigned, handle)
        self.assertFalse([i for i in self.invocations("claude") if "-p" in i["argv"]])
        self.assertEqual([], self.workspace_entries())


class DispatchRefusalTests(BridgeTestCase):
    def test_each_precondition_refuses_before_launch(self) -> None:
        route, assigned, handle = self.claude_run()
        self.frames("claude", [init_frame(), result_frame()])
        native = {**route, "dispatch": "native"}
        writable = self.assignment_for(route)
        writable["permissions"] = {
            **writable["permissions"], "filesystem": ["source.txt:write"],
        }
        changed_route = {**route, "reason": "changed_after_open"}
        cases = (
            (lambda: self.dispatch(native, assigned, handle), BridgeRefused,
             "not a bridge dispatch"),
            (lambda: self.dispatch({**route, "sandbox_mode": "workspace-write"},
                                   assigned, handle), BridgeRefused, "read-only"),
            (lambda: self.dispatch(route, writable, handle), BridgeRefused, "read-only"),
            (lambda: self.dispatch(route, {**assigned, "model": "claude-haiku-5-5"},
                                   handle), ContractError, "does not match"),
            (lambda: self.dispatch(route, assigned, "hrn_" + "0" * 48), LedgerError,
             "unavailable"),
            (lambda: self.dispatch(changed_route, assigned, handle), LedgerError,
             "does not match the opened host run"),
            (lambda: self.dispatch(route, assigned, handle,
                                   environ={"OPENAI_API_KEY": "x"}),
             BridgeRefused, "API key"),
        )
        for call, error, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(error, message):
                call()
        self.assertFalse([i for i in self.invocations("claude") if "-p" in i["argv"]])
        with self.ledger.repository(self.project_id) as repository:
            self.assertEqual([], repository.records("dispatch_intent"))
        # The handle is still usable once the request is correct.
        self.assertEqual(
            "result", self.dispatch(route, assigned, handle)["bridge_report"]["outcome"]
        )

    def test_admission_for_another_provider_refuses(self) -> None:
        self.admit("codex")
        route, assigned, handle = self.claude_run()
        codex_route = {**route, "provider": "codex"}
        with self.assertRaisesRegex(BridgeRefused, "admission differs"):
            self.dispatch(codex_route, assigned, handle)

    def test_export_rejects_symlinks_traversal_and_absolute_paths(self) -> None:
        (self.project / "link.txt").symlink_to(self.project / "secret.txt")
        git(self.project, "add", "link.txt")
        git(self.project, "commit", "-qm", "link")
        route, assigned, handle = self.claude_run()
        self.frames("claude", [init_frame(), result_frame()])
        for context, message in (
            (["link.txt"], "symlinks"),
            (["../outside.txt"], "inside the repository"),
            ([str(self.project / "source.txt")], "inside the repository"),
            (["missing.txt"], "not committed"),
        ):
            with self.subTest(context=context):
                changed = {**assigned, "context": context}
                with mock.patch(
                    "codex_agents.bridge.validate_assignment", return_value=changed
                ), self.assertRaisesRegex(BridgeRefused, message), mock.patch.object(
                    self.ledger, "record_dispatch_intent", return_value={"run_id": "x"}
                ):
                    self.dispatch(route, changed, handle)
                self.assertEqual([], self.workspace_entries())
        self.assertFalse([i for i in self.invocations("claude") if "-p" in i["argv"]])

    def test_export_cap_refuses(self) -> None:
        from codex_agents import bridge

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory, \
                self.assertRaisesRegex(BridgeRefused, "too large"):
            bridge.export_committed_paths(
                self.project, ["source.txt"], Path(directory), max_bytes=3
            )


class CodexBridgeTests(BridgeTestCase):
    def codex_run(self) -> tuple[dict, dict, str]:
        self.admit("codex")
        route = self.bridged_route("codex")
        assigned = self.assignment_for(route)
        return route, assigned, self.open(route, assigned)

    def identities(self) -> int:
        with self.ledger.repository(self.project_id) as repository:
            return len([
                record for record in repository.records("provider_identity")
                if record["provider"] == "codex"
            ])

    def test_happy_path_observes_before_and_after(self) -> None:
        route, assigned, handle = self.codex_run()
        self.frames("codex", [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed",
             "usage": {"input_tokens": 50, "cached_input_tokens": 5,
                       "output_tokens": 9, "reasoning_output_tokens": 3}},
        ])
        (self.tools / "codex-last-message.json").write_text(json.dumps(result()))
        before = self.identities()
        output = self.dispatch(route, assigned, handle)
        self.assertEqual(before + 2, self.identities())
        report = output["bridge_report"]
        self.assertEqual(
            ("codex", "advisory", "result", "recorded", True),
            (report["provider"], report["read_containment"], report["outcome"],
             report["observation_after"], report["reviewed"]),
        )
        self.assertEqual(50, report["usage"]["input_tokens"])
        self.assertEqual(result(), output["result"])
        invocation = [i for i in self.invocations("codex") if "exec" in i["argv"]][0]
        argv = invocation["argv"]
        self.assertEqual("read-only", argv[argv.index("-s") + 1])
        for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                     "--json"):
            self.assertIn(flag, argv)
        self.assertEqual(invocation["cwd"], argv[argv.index("-C") + 1])
        self.assertEqual(["source.txt"], invocation["files"])
        self.assertEqual(
            f"model_reasoning_effort={route['effort']}", argv[argv.index("-c") + 1]
        )
        self.assertEqual([], self.workspace_entries())

    def test_reroute_or_model_mismatch_marks_the_result_unreviewed(self) -> None:
        for event in (
            {"type": "model/rerouted", "from_model": "gpt-6.1-sol"},
            {"type": "turn.started", "model": "gpt-6-luna"},
        ):
            with self.subTest(event=event):
                route, assigned, handle = self.codex_run()
                self.frames("codex", [event, {"type": "turn.completed", "usage": {}}])
                (self.tools / "codex-last-message.json").write_text(json.dumps(result()))
                output = self.dispatch(route, assigned, handle)
                self.assertFalse(output["bridge_report"]["reviewed"])
                self.assertTrue(output["bridge_report"]["rerouted"])
                self.assertEqual("not-applicable", output["result"]["semantic_verdict"])
                validate_result(output["result"], assignment=assigned)

    def test_missing_last_message_is_ambiguous(self) -> None:
        route, assigned, handle = self.codex_run()
        self.frames("codex", [{"type": "turn.completed", "usage": {}}])
        (self.tools / "codex-last-message.json").unlink(missing_ok=True)
        output = self.dispatch(route, assigned, handle)
        self.assertEqual("missing_result", output["bridge_report"]["outcome"])
        self.assertEqual("failed", output["result"]["execution_state"])


class BridgeLifecycleTests(BridgeTestCase):
    def cli(self, command: str, *arguments: str, expected: int = 0) -> dict:
        stdout = io.StringIO()
        argv = [
            "cli.py", command, "--state-root", str(self.state.root),
            "--project-root", str(self.project), *arguments,
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            cli, "BUNDLE_ROOT", self.bundle
        ), contextlib.redirect_stdout(stdout):
            code = cli.main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def write(self, name: str, value: object) -> str:
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps(value))
        return str(path)

    def test_claude_open_dispatch_validate_and_record_through_the_cli(self) -> None:
        config = self.project / ".codex/orchestration/provider-routing.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "version": 1,
            "providers": {"codex": {"enabled": True}, "claude": {"enabled": True}},
            "codex_primary_may_bridge_to_claude": True,
        }))
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "bridge configuration")
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.admit("claude")
        self.ledger.close()
        task = self.write("task.json", self.task())
        route = self.cli("learning-route", "--task", task)
        self.assertEqual(("claude", "bridge"), (route["provider"], route["dispatch"]))
        assigned = self.assignment_for(route)
        context = (
            "--task", task,
            "--assignment", self.write("assignment.json", assigned),
            "--parent-grant", self.write("grant.json", PARENT_GRANT),
        )
        route_path = self.write("route.json", route)
        orchestration = self.cli("learning-open-orchestration")
        opened = self.cli(
            "learning-open-host-run", *context, "--route", route_path,
            "--orchestration-handle", orchestration["orchestration_handle"],
        )
        self.frames("claude", [init_frame(), RATE_LIMIT, result_frame()])
        dispatched = self.cli(
            "dispatch", *context, "--route", route_path,
            "--run-handle", opened["run_handle"],
        )
        self.assertEqual("result", dispatched["bridge_report"]["outcome"])
        refused = self.cli(
            "dispatch", *context, "--route", route_path,
            "--run-handle", opened["run_handle"], expected=2,
        )
        self.assertIn("already dispatched", refused["error"])
        result_path = self.write("result.json", dispatched["result"])
        validated = self.cli("validate-result", *context, "--result", result_path)
        self.assertEqual(dispatched["result"]["assignment_id"], validated["assignment_id"])
        recorded = self.cli(
            "learning-record-host-result", *context, "--result", result_path,
            "--observation", self.write("observation.json", {
                "run_handle": opened["run_handle"],
                "result_attempts": [
                    {"status": "accepted", "reason_code": None, "receipt_id": None}
                ],
            }),
        )
        self.assertEqual("claude-opus-5-5", recorded["outcome"]["model"])
        self.assertEqual("claude_agents_researcher", recorded["outcome"]["role"])
        with self.assertRaisesRegex(LedgerError, "no longer open"):
            LearningLedger(self.state, client_capabilities=self.clients) \
                .record_claude_rate_limit_event(
                    self.project_id, run_handle=opened["run_handle"],
                    rate_limit_info=RATE_LIMIT["rate_limit_info"],
                )

    def test_codex_open_dispatch_validate_and_record(self) -> None:
        # A Codex child is bridged only from a Claude primary, whose host client
        # arrives in S3; this exercises the same lifecycle through the ledger.
        self.admit("codex")
        route = self.bridged_route("codex")
        assigned = self.assignment_for(route)
        handle = self.open(route, assigned)
        self.frames("codex", [{"type": "turn.completed", "usage": {"input_tokens": 1}}])
        (self.tools / "codex-last-message.json").write_text(json.dumps(result()))
        output = self.dispatch(route, assigned, handle)
        validate_result(output["result"], assignment=assigned)
        committed = self.ledger.commit_host_result(
            self.project_id,
            assignment=assigned, task=self.task(), route=route,
            result=output["result"],
            observation={
                "run_handle": handle,
                "result_attempts": [
                    {"status": "accepted", "reason_code": None, "receipt_id": None}
                ],
            },
            execution_configuration=route["execution_configuration"],
        )
        self.assertEqual("gpt-6.1-sol", committed["outcome"]["model"])

    def test_fenced_outcome_records_as_failed(self) -> None:
        route, assigned, handle = self.claude_run(timeout_seconds=1)
        self.frames("claude", [init_frame(), {"sleep": 60}])
        output = self.dispatch(route, assigned, handle)
        committed = self.ledger.commit_host_result(
            self.project_id,
            assignment=assigned, task=self.task(), route=route,
            result=output["result"],
            observation={
                "run_handle": handle,
                "terminal_state": output["bridge_report"]["terminal_state"],
                "result_attempts": [
                    {"status": "accepted", "reason_code": None, "receipt_id": None}
                ],
            },
            execution_configuration=route["execution_configuration"],
        )
        self.assertEqual("timed_out", committed["outcome"]["execution_state"])
        self.assertFalse(committed["outcome"]["accepted"])


if __name__ == "__main__":
    unittest.main()
