from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.test_bridge import (
    CLEAN,
    RATE_LIMIT,
    RESULT_SCHEMA,
    BridgeTestCase,
    init_frame,
    result_frame,
)

from codex_agents import bridge, routing, workspace
from codex_agents.bridge import BridgeRefused, dispatch
from codex_agents.learning_contracts import content_digest
from codex_agents.learning_engine import installed_execution_configuration
from codex_agents.learning_store import LearningLedger, LedgerError
from codex_agents.routing import route_task

WRITE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]
WRITER = (
    "---\nname: project_writer\ndescription: Make one bounded change\n"
    "tools: Read, Glob, Grep, Edit, Write\nmaxTurns: 8\n---\n\nMake the bounded change.\n"
)
GIT_NO_LAZY_FETCH = subprocess.run(
    ["git", "--no-lazy-fetch", "version"], capture_output=True, check=False
).returncode == 0


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


@unittest.skipUnless(GIT_NO_LAZY_FETCH, "the contained workspace needs Git 2.44 or later")
class ClaudeWriteChildTests(BridgeTestCase):
    """A Claude primary's coding task, dispatched to a Claude write child through the bridge."""

    def setUp(self) -> None:
        super().setUp()
        for relative, content in (
            ("src/app.py", "print('app')\n"),
            ("src/nested/deep.txt", "deep\n"),
            ("docs/guide.md", "guide\n"),
        ):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "write fixtures")
        (self.bundle / ".claude/agents/project_writer.md").write_text(WRITER)
        # Write children run only on macOS; elsewhere the routing and lsof are
        # stood in for, so the bridge's own logic is still exercised.
        if sys.platform != "darwin":
            for target, name, value in (
                (routing, "write_children_supported", lambda: True),
                (bridge, "_processes_using", lambda path: []),
            ):
                patcher = mock.patch.object(target, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.seeded = False
        self.task_owned_paths = ["src"]
        self.pidfile = Path(self.temporary.name) / "holder.pid"
        self.addCleanup(self.kill_holder)

    def kill_holder(self) -> None:
        if self.pidfile.exists():
            try:
                os.kill(int(self.pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def write_task(self) -> dict:
        return {**self.task(), "objective": "Change one file", "family": "coding",
                "owned_paths": getattr(self, "task_owned_paths", ["src"])}

    def write_route(self) -> dict:
        policy = deepcopy(self.policy)
        config = {
            "version": 1,
            "providers": {"codex": {"enabled": False}, "claude": {"enabled": True}},
            "codex_primary_may_bridge_to_claude": True,
        }
        policy["provider_routing"] = config
        policy["provider_config_digest"] = content_digest(config)
        policy["project_agent_mapping_digest"] = "a" * 64
        policy["project_agent_roles"] = {}
        policy["project_claude_agent_roles"] = {"coding": "project_writer"}
        policy["project_claude_agent_role_sandboxes"] = {"coding": "workspace-write"}
        route = route_task(self.write_task(), policy, primary_provider="claude")
        self.assertEqual(
            ("claude", "workspace-write", "bridge"),
            (route["provider"], route["sandbox_mode"], route["dispatch"]),
        )
        route["execution_configuration"] = installed_execution_configuration(
            self.bundle, route, client_capability=self.clients["bridge-v1"]
        )
        return route

    def write_assignment(self, route: dict, **changes) -> dict:
        assigned = self.assignment_for(route)
        assigned.update(
            task_id="task-1", objective="Change one file", owned_paths=["src"], context=["docs"],
        )
        assigned["permissions"] = {
            "tools": list(WRITE_TOOLS), "filesystem": ["src:write", "docs:read"],
            "network": False, "mcps": [], "external_effects": False,
        }
        assigned.update(changes)
        return assigned

    def grant(self, assigned: dict) -> dict:
        return {**assigned["permissions"]}

    def seed(self) -> None:
        if getattr(self, "seeded", False):
            return
        self.admit("claude")
        seed_route = self.bridged_route("claude")
        seed = self.open(seed_route, self.assignment_for(seed_route))
        self.ledger.record_claude_rate_limit_event(
            self.project_id, run_handle=seed, rate_limit_info=RATE_LIMIT["rate_limit_info"],
        )
        self.seeded = True

    def open_write(self, route: dict, assigned: dict) -> str:
        self.seed()
        orchestration = self.ledger.open_orchestration(self.project_id, client_version="codex-host-v1")
        return self.ledger.open_host_run(
            self.project_id, assignment=assigned, task=self.write_task(), route=route,
            execution_configuration=route["execution_configuration"],
            client_version="bridge-v1",
            orchestration_handle=orchestration["orchestration_handle"],
        )["run_handle"]

    def run_write(self, frames: list[dict], **changes) -> dict:
        route = self.write_route()
        assigned = self.write_assignment(route, **changes)
        handle = self.open_write(route, assigned)
        self.frames("claude", frames)
        return dispatch(
            self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
            task=self.write_task(), route=route, assignment=assigned,
            parent_grant=self.grant(assigned), run_handle=handle, result_schema=RESULT_SCHEMA,
            environ=CLEAN,
        )

    def write_frames(self, *actions: dict) -> list[dict]:
        return [init_frame(tools=list(WRITE_TOOLS)), *actions, RATE_LIMIT, result_frame()]

    def write_records(self) -> list[dict]:
        with self.ledger.repository(self.project_id) as repository:
            return repository.records("write_result")

    def leftovers(self) -> tuple[list[str], list[str]]:
        names = sorted(path.name for path in self.dispatch_parent.iterdir())
        fenced = [name for name in names if workspace.FENCED_MARKER in name]
        return [name for name in names if name not in fenced], fenced

    def assert_no_patch(self, output: dict, reason: str) -> None:
        report = output["bridge_report"]
        self.assertEqual(reason, report["ownership"])
        self.assertEqual((None, None, 0), (
            report["patch_digest"], report["envelope_digest"], report["changes"]))
        self.assertEqual("failed", output["result"]["status"])
        self.assertEqual("failed", report["terminal_state"])
        [record] = self.write_records()
        self.assertEqual(reason, record["ownership"])
        self.assertIsNone(record["patch_digest"])
        self.assertIsNone(self.ledger.write_result_artifact(
            self.project_id, report["bridge_run_id"], "patch"))
        self.assertEqual([], self.leftovers()[0])

    def test_happy_path_records_a_patch_and_envelope(self) -> None:
        output = self.run_write(self.write_frames(
            {"write": "src/app.py", "content": "print('changed')\n"},
            {"write": "src/pkg/new.py", "content": "x = 1\n"},
        ))
        report = output["bridge_report"]
        self.assertEqual(("result", "passed", 2), (report["outcome"], report["ownership"], report["changes"]))
        self.assertEqual("completed", output["result"]["status"])
        self.assertEqual("proved", report["quiescence"])
        [record] = self.write_records()
        self.assertEqual(
            (report["patch_digest"], report["envelope_digest"], 2, "passed", "proved"),
            (record["patch_digest"], record["envelope_digest"], record["changes"],
             record["ownership"], record["quiescence"]),
        )
        self.assertEqual(report["source_commit"], record["source_commit"])
        patch_bytes = self.ledger.write_result_artifact(
            self.project_id, report["bridge_run_id"], "patch")
        envelope_bytes = self.ledger.write_result_artifact(
            self.project_id, report["bridge_run_id"], "envelope")
        for content, digest in ((patch_bytes, report["patch_digest"]),
                                (envelope_bytes, report["envelope_digest"])):
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        for path in self.artifact_paths(report["bridge_run_id"]):
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
        patch = json.loads(patch_bytes)
        self.assertEqual(["src/app.py", "src/pkg/new.py"], [c["path"] for c in patch["changes"]])
        self.assertEqual(b"print('changed')\n", base64.b64decode(patch["changes"][0]["content_b64"]))
        envelope = json.loads(envelope_bytes)
        self.assertEqual(report["patch_digest"], envelope["patch_digest"])
        self.assertEqual(
            {"bridge_run_id", "project_id", "route_digest", "run_handle_sha256", "source_commit"},
            set(envelope["run_binding"]),
        )
        # The child never wrote into the repository, and nothing is left behind.
        self.assertEqual("print('app')\n", (self.project / "src/app.py").read_text())
        self.assertEqual(([], []), self.leftovers())
        # The ledger record holds digests and counts only, never content.
        self.assertNotIn("changed", json.dumps(record))

    def test_child_launch_profile(self) -> None:
        self.run_write(self.write_frames())
        [invocation] = [item for item in self.invocations("claude") if "-p" in item["argv"]]
        argv = invocation["argv"]
        self.assertEqual("Read,Glob,Grep,Edit,Write", argv[argv.index("--tools") + 1])
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual("dontAsk", argv[argv.index("--permission-mode") + 1])
        self.assertEqual("project", argv[argv.index("--setting-sources") + 1])
        self.assertNotIn("--bare", argv)
        self.assertEqual(0o022, invocation["umask"])
        self.assertEqual("workspace", Path(invocation["cwd"]).name)
        settings_path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual("control", settings_path.parent.name)
        self.assertNotEqual(Path(invocation["cwd"]), settings_path.parent)
        anchor = "//" + os.path.realpath(invocation["cwd"]).lstrip("/")
        permissions = invocation["settings"]["permissions"]
        self.assertEqual("dontAsk", permissions["defaultMode"])
        self.assertEqual([f"Edit({anchor}/src/**)", f"Write({anchor}/src/**)"], permissions["allow"])
        # Protected names are denied at any depth, for both tools, in several spellings.
        for name in ("AGENTS.md", "agents.md", "CLAUDE.md", "claude.md", ".mcp.json", ".MCP.JSON",
                     ".git", ".GIT", ".claude", ".codex", ".agents", "CLAUDE.local.md"):
            for tool in ("Edit", "Write"):
                with self.subTest(name=name, tool=tool):
                    self.assertIn(f"{tool}({anchor}/**/{name})", permissions["deny"])
                    self.assertIn(f"{tool}({anchor}/**/{name}/**)", permissions["deny"])
        self.assertTrue(all(rule.split("(", 1)[1].startswith(anchor + "/")
                            for rule in permissions["allow"] + permissions["deny"]))
        self.assertTrue(invocation["settings"]["disableAllHooks"])
        self.assertFalse(any(invocation["settings"]["enabledPlugins"].values()))
        # The workspace holds the granted paths only, without Claude context files.
        self.assertEqual(
            ["docs/guide.md", "src/app.py", "src/nested/deep.txt"], sorted(invocation["files"])
        )

    def artifact_paths(self, bridge_run_id: str) -> list[Path]:
        partition = self.state.artifacts / self.project_id
        return sorted(partition.glob(f"write_*_{bridge_run_id}.json"))

    def test_rules_are_absolute_and_anchored_to_the_workspace(self) -> None:
        workspace_path = Path(self.temporary.name) / "rule-workspace"
        workspace_path.mkdir()
        anchor = "//" + os.path.realpath(workspace_path).lstrip("/")
        manifest = {"files": {"src/app.py": {}, "src/nested/deep.txt": {}}, "directories": {"src": 448}}
        rules = bridge.claude_write_allow_rules(["src", "src/app.py", "new.py"], manifest, workspace_path)
        self.assertEqual(
            [f"Edit({anchor}/src/**)", f"Write({anchor}/src/**)",
             f"Edit({anchor}/src/app.py)", f"Write({anchor}/src/app.py)",
             # A bare filename is anchored, so it cannot match that name at another depth.
             f"Edit({anchor}/new.py)", f"Write({anchor}/new.py)"],
            rules,
        )
        for rule in rules:
            self.assertTrue(rule.split("(", 1)[1].startswith("//"))
            self.assertNotIn("~", rule)
        deny = bridge.claude_write_deny_rules(workspace_path)
        self.assertIn(f"Write({anchor}/**/AGENTS.md)", deny)
        self.assertIn(f"Edit({anchor}/**/.git/**)", deny)

    def test_paths_the_rule_syntax_reads_specially_are_refused(self) -> None:
        workspace_path = Path(self.temporary.name)
        manifest = {"files": {}, "directories": {}}
        for unsafe in (
            "~/Code/primary/injected.py", "~", "~user/x", "src/a(b).py", "src/a\nb", "src/a*b",
            "src/a?b", "src/[ab]", "src/{a,b}", "src\\a", "src/a b", "src/a!b", "src/a#b",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(BridgeRefused, "rule_safe"):
                bridge.claude_write_allow_rules([unsafe], manifest, workspace_path)
        with self.assertRaisesRegex(BridgeRefused, "rule_safe"):
            bridge.claude_write_allow_rules(["src"], manifest, workspace_path / "odd dir")

    def test_tilde_owned_path_is_refused_before_launch(self) -> None:
        route = self.write_route()
        self.task_owned_paths = ["~/Code/primary/injected.py"]
        route = self.write_route()
        assigned = self.write_assignment(route)
        assigned["owned_paths"] = ["~/Code/primary/injected.py"]
        assigned["permissions"]["filesystem"] = ["~/Code/primary/injected.py:write", "docs:read"]
        handle = self.open_write(route, assigned)
        self.frames("claude", self.write_frames())
        with self.assertRaisesRegex(BridgeRefused, "write_path_not_rule_safe"):
            dispatch(
                self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
                task=self.write_task(),
                route=route, assignment=assigned, parent_grant=self.grant(assigned),
                run_handle=handle, result_schema=RESULT_SCHEMA, environ=CLEAN,
            )
        self.assertFalse([item for item in self.invocations("claude") if "-p" in item["argv"]])
        self.assertEqual(([], []), self.leftovers())

    def test_each_ownership_refusal_leaves_no_patch(self) -> None:
        cases = {
            "outside_write_set": {"write": "docs/new.md", "content": "x\n"},
            "protected_name": {"write": "src/CLAUDE.md", "content": "x\n"},
            "symlink_entry": {"symlink": "src/link", "target": "/etc/passwd"},
            "mode_change": {"chmod": "src/app.py", "mode": 0o666},
        }
        for reason, action in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                output = self.run_write(self.write_frames(action))
                self.assertEqual(reason, output["bridge_report"]["outcome"])
                self.assert_no_patch(output, reason)

    def test_init_tool_check_requires_exactly_the_derived_tools(self) -> None:
        for tools, outcome in (
            (["Read", "Glob", "Grep", "Edit"], "init_tool_missing"),
            ([*WRITE_TOOLS, "Bash"], "init_tool_not_allowed"),
        ):
            with self.subTest(outcome=outcome):
                self.setUp()
                output = self.run_write([init_frame(tools=tools), RATE_LIMIT, result_frame()])
                self.assertEqual(outcome, output["bridge_report"]["outcome"])
                self.assert_no_patch(output, "child_failed")

    def test_surviving_descendant_fails_quiescence_and_fences_the_layout(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("the quiescence check uses lsof on macOS, the only write platform")
        output = self.run_write(self.write_frames(
            {"write": "src/app.py", "content": "print('changed')\n"},
            {"holder": 60, "pidfile": str(self.pidfile)},
        ))
        report = output["bridge_report"]
        self.assertEqual(("unproved", 1), (report["quiescence"], report["survivors"]))
        self.assertEqual("quiescence_unproved", report["outcome"])
        self.assert_no_patch(output, "quiescence_unproved")
        live, fenced = self.leftovers()
        self.assertEqual([], live)
        self.assertEqual(1, len(fenced))

    def test_quiescence_is_unproved_when_lsof_cannot_answer(self) -> None:
        with mock.patch.object(bridge, "_processes_using", return_value=None):
            output = self.run_write(self.write_frames({"write": "src/app.py", "content": "y\n"}))
        self.assert_no_patch(output, "quiescence_unproved")
        self.assertEqual(1, len(self.leftovers()[1]))

    def test_cleanup_on_a_refusal_before_launch(self) -> None:
        route = self.write_route()
        assigned = self.write_assignment(route)
        handle = self.open_write(route, assigned)
        (self.tools / "claude-status.json").write_text("{}")
        with self.assertRaises(BridgeRefused):
            dispatch(
                self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
                task=self.write_task(), route=route, assignment=assigned,
                parent_grant=self.grant(assigned), run_handle=handle,
                result_schema=RESULT_SCHEMA, environ=CLEAN,
            )
        self.assertEqual(([], []), self.leftovers())

    def test_cleanup_after_a_timeout(self) -> None:
        route = self.write_route()
        route["bounds"]["timeout_seconds"] = 2
        route["execution_configuration"] = installed_execution_configuration(
            self.bundle, route, client_capability=self.clients["bridge-v1"]
        )
        assigned = self.write_assignment(route)
        assigned["bounds"]["timeout_seconds"] = 2
        handle = self.open_write(route, assigned)
        self.frames("claude", [init_frame(tools=list(WRITE_TOOLS)), {"sleep": 30}])
        output = dispatch(
            self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
            task=self.write_task(), route=route, assignment=assigned,
            parent_grant=self.grant(assigned), run_handle=handle, result_schema=RESULT_SCHEMA,
            environ=CLEAN,
        )
        self.assertEqual("timed_out", output["bridge_report"]["outcome"])
        self.assert_no_patch_after_timeout(output)

    def assert_no_patch_after_timeout(self, output: dict) -> None:
        self.assertEqual("child_failed", output["bridge_report"]["ownership"])
        self.assertEqual("timed_out", output["result"]["execution_state"])
        self.assertEqual([], self.leftovers()[0])

    def test_assignment_must_fit_the_write_profile(self) -> None:
        route = self.write_route()
        cases = {
            "write_profile_mismatch": {"tools": ["Read", "Glob", "Grep", "Edit", "Write", "shell"]},
            "write_set_empty": {"filesystem": ["src:read", "docs:read"]},
        }
        for reason, permissions in cases.items():
            with self.subTest(reason=reason):
                assigned = self.write_assignment(route)
                assigned["permissions"] = {**assigned["permissions"], **permissions}
                with self.assertRaisesRegex(BridgeRefused, reason):
                    bridge.claude_write_tools(assigned)
        narrowed = self.write_assignment(route)
        narrowed["permissions"] = {**narrowed["permissions"], "tools": ["Read", "Edit"]}
        with self.assertRaisesRegex(BridgeRefused, "write_profile_mismatch"):
            bridge.claude_write_tools(narrowed)

    def test_write_routes_to_codex_or_off_macos_are_refused_by_the_bridge(self) -> None:
        route = self.write_route()
        assigned = self.write_assignment(route)
        handle = self.open_write(route, assigned)

        def call(changed_route):
            return dispatch(
                self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
                task=self.write_task(), route=changed_route, assignment=assigned,
                parent_grant=self.grant(assigned), run_handle=handle,
                result_schema=RESULT_SCHEMA, environ=CLEAN,
            )

        with self.assertRaisesRegex(BridgeRefused, "codex_write_children_unavailable"):
            call({**route, "provider": "codex", "model": "gpt-6.1-sol"})
        with mock.patch.object(routing, "write_children_supported", return_value=False), \
                self.assertRaisesRegex(BridgeRefused, "write_children_unsupported_platform"):
            call(route)
        self.assertFalse([item for item in self.invocations("claude") if "-p" in item["argv"]])
        self.assertEqual(([], []), self.leftovers())


    # --- recording (finding 4), staging (finding 6) and retrieval (finding 5) ---

    def captured_write(self) -> tuple[dict, dict]:
        """Run a passing child but capture, not record, the write result."""

        captured: dict = {}

        def capture(project_id, **arguments):
            captured.update(deepcopy(arguments))
            raise LedgerError("captured for the test")

        with mock.patch.object(self.ledger, "record_write_result", side_effect=capture):
            output = self.run_write(self.write_frames(
                {"write": "src/app.py", "content": "print('changed')\n"}))
        self.assertEqual("write_result_unrecorded", output["bridge_report"]["ownership"])
        return captured, output

    def record(self, captured: dict, **changes) -> dict:
        return self.ledger.record_write_result(self.project_id, **{**captured, **changes})

    def partition_files(self) -> list[str]:
        partition = self.state.artifacts / self.project_id
        return sorted(path.name for path in partition.iterdir()) if partition.exists() else []

    def test_recording_requires_a_claude_write_route_and_verified_artifacts(self) -> None:
        captured, _ = self.captured_write()
        result = captured["result"]
        codex_route = {**captured["route"], "provider": "codex", "model": "gpt-6.1-sol"}
        with self.assertRaisesRegex(LedgerError, "route does not match"):
            self.record(captured, route=codex_route)
        with self.assertRaisesRegex(LedgerError, "proved quiescence"):
            self.record(captured, result={**result, "quiescence": "unproved"})
        junk = b'{"not":"a patch"}'
        with self.assertRaisesRegex(LedgerError, "patch is invalid"):
            self.record(captured, patch=junk,
                        result={**result, "patch_digest": hashlib.sha256(junk).hexdigest()})
        envelope = json.loads(captured["envelope"])
        envelope["manifest"]["write_set"] = ["docs"]
        tampered = workspace.canonical_json(envelope)
        with self.assertRaisesRegex(LedgerError, "does not bind"):
            self.record(captured, envelope=tampered,
                        result={**result, "envelope_digest": hashlib.sha256(tampered).hexdigest()})
        with self.assertRaisesRegex(LedgerError, "carries no artifacts"):
            self.record(captured, result={
                **result, "ownership": "outside_write_set", "patch_digest": None,
                "envelope_digest": None, "changes": 0,
            })
        self.assertEqual([], self.write_records())
        self.assertEqual([], self.partition_files())
        record = self.record(captured)
        self.assertEqual("passed", record["ownership"])
        self.assertEqual(captured["patch"], self.ledger.write_result_artifact(
            self.project_id, captured["bridge_run_id"], "patch"))

    def test_failure_between_staging_and_commit_leaves_nothing(self) -> None:
        captured, _ = self.captured_write()
        with mock.patch.object(self.ledger, "_append_checked_locked", side_effect=RuntimeError("crash")), \
                self.assertRaises(RuntimeError):
            self.record(captured)
        self.assertEqual(([], []), (self.write_records(), self.partition_files()))
        original = self.ledger._insert_stored_file_locked
        calls = []

        def second_fails(*arguments, **keywords):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("crash between registrations")
            return original(*arguments, **keywords)

        with mock.patch.object(self.ledger, "_insert_stored_file_locked", side_effect=second_fails), \
                self.assertRaises(RuntimeError):
            self.record(captured)
        self.assertEqual(2, len(calls))
        self.assertEqual(([], []), (self.write_records(), self.partition_files()))
        with self.ledger.repository(self.project_id) as repository:
            self.assertEqual([], [row for row in repository.records("write_result")])
        self.assertIsNone(self.ledger.write_result_artifact(
            self.project_id, captured["bridge_run_id"], "patch"))
        # The run can still record once the failure is gone.
        self.record(captured)
        self.assertEqual(2, len(self.partition_files()))

    def test_startup_reconciliation_removes_staged_files_without_a_record(self) -> None:
        self.seed()
        staged = self.ledger._stage_owner_file(self.project_id, "write_patch_brg_" + "0" * 32, b"{}")
        self.assertTrue(staged.exists())
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.addCleanup(self.ledger.close)
        self.assertFalse(staged.exists())

    def test_replaced_artifact_is_refused(self) -> None:
        output = self.run_write(self.write_frames({"write": "src/app.py", "content": "y\n"}))
        bridge_run_id = output["bridge_report"]["bridge_run_id"]
        patch_path = next(path for path in self.artifact_paths(bridge_run_id) if "patch" in path.name)
        original = patch_path.read_bytes()
        patch_path.write_bytes(original.replace(b"src/app.py", b"src/bad.py"))
        with self.assertRaisesRegex(LedgerError, "does not match"):
            self.ledger.write_result_artifact(self.project_id, bridge_run_id, "patch")
        decoy = patch_path.with_name("decoy.json")
        decoy.write_bytes(original)
        os.chmod(decoy, 0o600)
        patch_path.unlink()
        os.symlink(decoy, patch_path)
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.write_result_artifact(self.project_id, bridge_run_id, "patch")

    def test_reviewer_probe_outside_the_planned_write_set_is_refused(self) -> None:
        captured, _ = self.captured_write()
        patch = json.loads(captured["patch"])
        envelope = json.loads(captured["envelope"])
        manifest = envelope["manifest"]
        content = b"injected = True\n"
        patch["changes"] = [{
            "path": "docs/injected.py", "op": "add", "old_blob": None, "old_mode": None,
            "new_sha256": hashlib.sha256(content).hexdigest(), "mode": "100644",
            "content_b64": base64.b64encode(content).decode(),
        }]

        def forged(patch_value, binding):
            # Assembled by hand, as a forger would, without the builder's own checks.
            patch_bytes = workspace.encode_patch(patch_value)
            built = {
                "schema_version": 1, "run_binding": binding,
                "source_commit": patch_value["source_commit"],
                "manifest_digest": patch_value["manifest_digest"], "manifest": manifest,
                "patch_digest": hashlib.sha256(patch_bytes).hexdigest(), "patch": patch_value,
                "resulting_files": [
                    {"path": c["path"], "sha256": c["new_sha256"], "content_b64": c["content_b64"]}
                    for c in patch_value["changes"] if c["content_b64"] is not None
                ],
            }
            envelope_bytes = workspace.canonical_json(built)
            return {
                "patch": patch_bytes, "envelope": envelope_bytes,
                "result": {**captured["result"], "changes": len(patch_value["changes"]),
                           "patch_digest": hashlib.sha256(patch_bytes).hexdigest(),
                           "envelope_digest": hashlib.sha256(envelope_bytes).hexdigest()},
            }

        other_run = {**envelope["run_binding"], "bridge_run_id": "brg_" + "f" * 32,
                     "run_handle_sha256": "e" * 64}
        self.assertEqual(["src"], manifest["write_set"])
        with self.assertRaisesRegex(LedgerError, "names another run|outside its write plan"):
            self.record(captured, **forged(patch, other_run))
        with self.assertRaisesRegex(LedgerError, "outside its write plan"):
            self.record(captured, **forged(patch, envelope["run_binding"]))
        inside = json.loads(captured["patch"])
        with self.assertRaisesRegex(LedgerError, "names another run"):
            self.record(captured, **forged(inside, other_run))
        protected = {**inside, "changes": [{**patch["changes"][0], "path": "src/AGENTS.md"}]}
        with self.assertRaisesRegex(LedgerError, "patch is invalid|outside its write plan"):
            self.record(captured, **forged(protected, envelope["run_binding"]))
        self.assertEqual([], self.write_records())
        self.record(captured)

    def test_write_plan_is_fixed_before_launch(self) -> None:
        captured, _ = self.captured_write()
        with self.ledger.repository(self.project_id) as repository:
            [plan] = repository.records("write_plan")
        self.assertEqual(["src"], plan["write_set"])
        self.assertEqual(captured["bridge_run_id"], plan["bridge_run_id"])
        self.assertEqual(captured["result"]["manifest_digest"], plan["manifest_digest"])
        with self.assertRaisesRegex(LedgerError, "already recorded its write plan"):
            self.ledger.record_write_plan(
                self.project_id, run_handle=captured["run_handle"],
                bridge_run_id=captured["bridge_run_id"], route=captured["route"],
                manifest=json.loads(captured["envelope"])["manifest"],
            )

    # --- the layout sweep (finding 7) ---

    def test_sweep_fences_the_run_before_removing_its_layout(self) -> None:
        captured, _ = self.captured_write()
        handle = captured["run_handle"]
        fenced = self.leased_layout(handle)
        expires = self.ledger.write_run_lease(self.project_id, handle)["expires_at_ms"]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=expires + 1):
            report = bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertEqual(1, report["removed"])
        self.assertFalse(os.path.lexists(fenced))
        lease = self.ledger.write_run_lease(self.project_id, handle)
        self.assertEqual("terminal", self.ledger.write_run_state(
            self.project_id, lease["handle_digest"], lease["expires_at_ms"]))
        with self.assertRaisesRegex(LedgerError, "no longer open"):
            self.record(captured)

    def test_sweep_keeps_a_lease_for_an_unbound_run(self) -> None:
        self.seed()
        layout = workspace.create_layout(self.dispatch_parent)
        bridge._write_lease(layout, {
            "project_id": self.project_id, "handle_digest": "0" * 64, "expires_at_ms": 0,
        })
        report = bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertEqual({layout.root.name: "run_unknown"}, report["kept"])
        self.assertTrue(layout.root.exists())

    def leased_layout(self, handle: str, *, fence: bool = True) -> Path:
        layout = workspace.create_layout(self.dispatch_parent)
        bridge._write_lease(layout, self.ledger.write_run_lease(self.project_id, handle))
        (layout.workspace / "left.txt").write_text("x")
        return workspace.fence_leftover(layout.root) if fence else layout.root

    def test_sweep_removes_a_fenced_layout_only_after_its_run_expires(self) -> None:
        route = self.write_route()
        assigned = self.write_assignment(route)
        handle = self.open_write(route, assigned)
        fenced = self.leased_layout(handle)
        unleased = workspace.create_layout(self.dispatch_parent).root
        other = self.dispatch_parent / "unrelated-directory"
        other.mkdir()
        report = bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertEqual(0, report["removed"])
        self.assertEqual("run_live", report["kept"][fenced.name])
        self.assertEqual("lease_unreadable", report["kept"][unleased.name])
        self.assertTrue(fenced.exists())
        expires = self.ledger.write_run_lease(self.project_id, handle)["expires_at_ms"]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=expires + 1):
            report = bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertEqual(1, report["removed"])
        self.assertFalse(os.path.lexists(fenced))
        self.assertTrue(unleased.exists())
        self.assertTrue(other.exists())

    def test_sweep_keeps_layouts_it_cannot_attribute_or_prove(self) -> None:
        route = self.write_route()
        assigned = self.write_assignment(route)
        handle = self.open_write(route, assigned)
        foreign = workspace.create_layout(self.dispatch_parent)
        bridge._write_lease(foreign, {
            "project_id": "prj_unknown", "handle_digest": "0" * 64, "expires_at_ms": 1,
        })
        busy = self.leased_layout(handle)
        expires = self.ledger.write_run_lease(self.project_id, handle)["expires_at_ms"]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=expires + 1), \
                mock.patch.object(bridge, "_processes_using", return_value=None):
            report = bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertEqual(
            {foreign.root.name: "run_unknown", busy.name: "quiescence_unproved"}, report["kept"])
        self.assertTrue(foreign.root.exists() and busy.exists())

    def test_dispatch_sweeps_before_it_creates_a_layout(self) -> None:
        route = self.write_route()
        assigned = self.write_assignment(route)
        old_handle = self.open_write(route, assigned)
        fenced = self.leased_layout(old_handle)
        expires = self.ledger.write_run_lease(self.project_id, old_handle)["expires_at_ms"]
        real_clock = self.ledger._clock_ms
        with mock.patch.object(bridge, "sweep_write_layouts", wraps=bridge.sweep_write_layouts) as sweep:
            output = self.run_write(self.write_frames())
        sweep.assert_called_once()
        self.assertIn(fenced.name, output["bridge_report"]["layout_sweep"]["kept"])
        with mock.patch.object(self.ledger, "_clock_ms", return_value=max(expires + 1, real_clock())):
            bridge.sweep_write_layouts(self.ledger, self.dispatch_parent)
        self.assertFalse(os.path.lexists(fenced))


class QuiescenceTests(unittest.TestCase):
    """The quiescence check's real subprocess and identity paths."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        status = os.lstat(self.workspace)
        self.identity = (status.st_dev, status.st_ino)

    def fake_lsof(self, body: str) -> None:
        tools = self.base / "fake-lsof"
        tools.mkdir(exist_ok=True)
        script = tools / "lsof"
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        patcher = mock.patch.object(bridge, "LSOF_SEARCH_PATH", str(tools))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_or_substituted_directory_is_unproved(self) -> None:
        missing = self.base / "missing"
        self.assertEqual((False, 0), bridge.prove_quiescent(missing, self.identity))
        self.assertEqual((False, 0), bridge.prove_quiescent(self.workspace, None))
        self.assertEqual((False, 0), bridge.prove_quiescent(self.workspace, (0, 0)))
        link = self.base / "link"
        os.symlink(self.workspace, link)
        self.assertEqual((False, 0), bridge.prove_quiescent(link, self.identity))

    @unittest.skipUnless(shutil.which("lsof", path=bridge.LSOF_SEARCH_PATH), "needs lsof")
    def test_real_lsof_on_a_missing_directory_never_proves(self) -> None:
        missing = self.base / "missing"
        # lsof itself reports nothing for a missing directory; the identity check refuses it.
        self.assertIn(bridge._processes_using(missing), ([], None))
        self.assertEqual((False, 0), bridge.prove_quiescent(missing, self.identity))
        self.assertEqual((True, 0), bridge.prove_quiescent(self.workspace, self.identity))

    @unittest.skipUnless(shutil.which("lsof", path=bridge.LSOF_SEARCH_PATH), "needs lsof")
    def test_real_lsof_warning_on_an_unreadable_directory_is_unproved(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read any directory, so lsof prints no warning")
        locked = self.workspace / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        self.assertIsNone(bridge._processes_using(self.workspace))
        self.assertEqual((False, 0), bridge.prove_quiescent(self.workspace, self.identity))

    def test_failing_lsof_is_unproved(self) -> None:
        for body in (
            'echo "lsof: something went wrong" >&2; exit 1',
            'echo "lsof: WARNING: can\'t opendir(x): Permission denied" >&2; exit 1',
            "exit 2",
            "echo garbage; exit 1",
            "echo p12x; exit 0",
            "exit 0",
            "sleep 60",
        ):
            with self.subTest(body=body), mock.patch.object(bridge, "LSOF_TIMEOUT_SECONDS", 1):
                self.fake_lsof(body)
                self.assertEqual((False, 0), bridge.prove_quiescent(self.workspace, self.identity))

    def test_directory_replaced_during_the_scan_is_unproved(self) -> None:
        self.fake_lsof('for last; do :; done; mv "$last" "$last.moved"; mkdir "$last"; exit 1')
        self.assertEqual((False, 0), bridge.prove_quiescent(self.workspace, self.identity))

    def test_matches_reported_with_status_one_are_survivors(self) -> None:
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(holder.kill)
        self.fake_lsof(f'printf "p{holder.pid}\\nfcwd\\n"; exit 1')
        self.assertEqual((False, 1), bridge.prove_quiescent(self.workspace, self.identity))
        self.assertIsNotNone(holder.wait(timeout=10))
        self.fake_lsof("exit 1")
        self.assertEqual((True, 0), bridge.prove_quiescent(self.workspace, self.identity))


class WriteResultContractTests(unittest.TestCase):
    def test_write_result_is_closed_and_ledger_only(self) -> None:
        from codex_agents.learning_contracts import validate_learning_record

        base = {
            "schema_version": 1, "record_type": "write_result", "record_id": "write_result:x",
            "run_id": "run_1", "bridge_run_id": "brg_" + "0" * 32, "source_commit": "a" * 40,
            "manifest_digest": "b" * 64, "patch_digest": "c" * 64, "envelope_digest": "d" * 64,
            "changes": 1, "ownership": "passed", "quiescence": "proved", "observed_at_ms": 1,
        }
        validate_learning_record(base)
        for changes in (
            {"ownership": "outside_write_set"},
            {"patch_digest": None},
            {"ownership": "Bad Code"},
            {"extra": 1},
            {"changes": 201},
        ):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                validate_learning_record({**base, **changes})
        validate_learning_record({
            **base, "ownership": "quiescence_unproved", "patch_digest": None,
            "envelope_digest": None, "changes": 0, "quiescence": "unproved",
        })


if __name__ == "__main__":
    unittest.main()
