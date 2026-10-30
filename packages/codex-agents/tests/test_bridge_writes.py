from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

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
                "owned_paths": ["src"]}

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
        patch_path = self.ledger.write_result_artifact(self.project_id, report["bridge_run_id"], "patch")
        envelope_path = self.ledger.write_result_artifact(
            self.project_id, report["bridge_run_id"], "envelope")
        for path, digest in ((patch_path, report["patch_digest"]),
                             (envelope_path, report["envelope_digest"])):
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
        patch = json.loads(patch_path.read_bytes())
        self.assertEqual(["src/app.py", "src/pkg/new.py"], [c["path"] for c in patch["changes"]])
        self.assertEqual(b"print('changed')\n", base64.b64decode(patch["changes"][0]["content_b64"]))
        envelope = json.loads(envelope_path.read_bytes())
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
        self.assertEqual(
            {"defaultMode": "dontAsk", "allow": ["Edit(src/**)", "Write(src/**)"]},
            invocation["settings"]["permissions"],
        )
        self.assertTrue(invocation["settings"]["disableAllHooks"])
        self.assertFalse(any(invocation["settings"]["enabledPlugins"].values()))
        # The workspace holds the granted paths only, without Claude context files.
        self.assertEqual(
            ["docs/guide.md", "src/app.py", "src/nested/deep.txt"], sorted(invocation["files"])
        )

    def test_allow_rules_for_file_and_directory_grants(self) -> None:
        manifest = {"files": {"src/app.py": {}, "src/nested/deep.txt": {}}, "directories": {"src": 448}}
        self.assertEqual(
            ["Edit(src/**)", "Write(src/**)", "Edit(src/app.py)", "Write(src/app.py)",
             "Edit(new.py)", "Write(new.py)"],
            bridge.claude_write_allow_rules(["src", "src/app.py", "new.py"], manifest),
        )
        for unsafe in ("src/a(b).py", "src/a\nb"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(BridgeRefused, "rule_safe"):
                bridge.claude_write_allow_rules([unsafe], manifest)

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
