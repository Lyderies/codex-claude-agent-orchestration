from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import time
import unittest
from copy import deepcopy
from unittest import mock

from tests import test_bridge_writes as writes
from tests.learning_support import DIGEST_KEY, result
from tests.test_bridge import CLEAN, RESULT_SCHEMA, null_filled
from tests.test_learning_engine import verified_qa_inputs

from codex_agents import bridge, workspace
from codex_agents.bridge import BridgeRefused, assignment_read_paths, dispatch
from codex_agents.contracts import ContractError, validate_task
from codex_agents.learning_contracts import (
    canonical_json,
    content_digest,
    keyed_digest,
    validate_learning_record,
)
from codex_agents.learning_engine import (
    build_operational_qa_records,
    installed_execution_configuration,
)
from codex_agents.learning_store import LedgerError
from codex_agents.routing import WRITE_VERIFIER_UNAVAILABLE, RouteDenied, route_task

ACCEPTED_ATTEMPT = [{"status": "accepted", "reason_code": None, "receipt_id": None}]
READ_SRC = {
    "tools": ["shell"], "filesystem": ["src:read"],
    "network": False, "mcps": [], "external_effects": False,
}


@unittest.skipUnless(writes.GIT_NO_LAZY_FETCH, "the contained workspace needs Git 2.44 or later")
class WriteVerificationTests(writes.ClaudeWriteChildTests):
    """A Claude write child's patch, verified by a bridged, read-only Codex child."""

    def open_write(self, route: dict, assigned: dict) -> str:
        # The worker and its verifier share one orchestration, as operational QA needs.
        self.seed()
        return self.ledger.open_host_run(
            self.project_id, assignment=assigned, task=self.write_task(), route=route,
            execution_configuration=route["execution_configuration"],
            client_version="bridge-v1", orchestration_handle=self.orchestration(),
        )["run_handle"]

    def orchestration(self) -> str:
        if not hasattr(self, "orchestration_handle"):
            self.orchestration_handle = self.ledger.open_orchestration(
                self.project_id, client_version="codex-host-v1"
            )["orchestration_handle"]
        return self.orchestration_handle

    def written(self, *actions: dict) -> dict:
        """Run one write child; return its output, route, assignment and handle."""

        route = self.write_route()
        assigned = self.write_assignment(route)
        handle = self.open_write(route, assigned)
        self.frames("claude", self.write_frames(*(actions or (
            {"write": "src/app.py", "content": "print('changed')\n"},
        ))))
        output = dispatch(
            self.ledger, self.project_id, project_root=self.project, bundle_root=self.bundle,
            task=self.write_task(), route=route, assignment=assigned,
            parent_grant=self.grant(assigned), run_handle=handle, result_schema=RESULT_SCHEMA,
            environ=CLEAN,
        )
        return {"output": output, "route": route, "assignment": assigned, "handle": handle}

    def binding(self, work: dict) -> dict:
        report = work["output"]["bridge_report"]
        return {"bridge_run_id": report["bridge_run_id"], "patch_digest": report["patch_digest"]}

    def policy_with(self, *, codex: bool = True, claude: bool = True) -> dict:
        policy = deepcopy(self.policy)
        config = {
            "version": 1,
            "providers": {"codex": {"enabled": codex}, "claude": {"enabled": claude}},
            "codex_primary_may_bridge_to_claude": True,
        }
        policy["provider_routing"] = config
        policy["provider_config_digest"] = content_digest(config)
        return policy

    def verifier_task(self, binding: dict | None, **changes) -> dict:
        task = {
            **self.task(), "id": "verify-1", "objective": "Verify the patch",
            "family": "verification", "verification_for_family": "coding",
            "verification_for_provider": "claude", **changes,
        }
        if binding is not None:
            task["verification_for_write"] = binding
        return task

    def verifier_route(self, task: dict, **policy) -> dict:
        route = route_task(task, self.policy_with(**policy), primary_provider="claude")
        route["execution_configuration"] = installed_execution_configuration(
            self.bundle, route, client_capability=self.clients["bridge-v1"]
        )
        return route

    def verifier_assignment(self, route: dict) -> dict:
        return self.assignment_for(
            route, assignment_id="verify-assignment-1", task_id="verify-1",
            objective="Verify the patch", context=["src"],
            owned_paths=[], verification_required=route["verification_required"],
            permissions=dict(READ_SRC),
        )

    def open_verifier(self, task: dict, route: dict, assigned: dict) -> str:
        if not getattr(self, "codex_admitted", False):
            self.admit("codex")
            self.codex_admitted = True
        return self.ledger.open_host_run(
            self.project_id, assignment=assigned, task=task, route=route,
            execution_configuration=route["execution_configuration"],
            client_version="bridge-v1", orchestration_handle=self.orchestration(),
        )["run_handle"]

    def verdict(self, digest: str | None, *, verdict: str = "pass") -> dict:
        value = result()
        value.update(
            assignment_id="verify-assignment-1",
            status="blocked" if verdict == "blocked" else "completed",
            execution_state="completed", semantic_verdict=verdict,
            admission_state="unverified",
            artifacts=[] if digest is None else [f"reviewed_patch_digest:{digest}"],
        )
        if verdict not in {"pass", "ready"}:
            value["evidence"][0]["outcome"] = "fail"
            value["remaining_risks"] = ["the patch does not meet the task"]
        return value

    def verify(self, work: dict, reply: dict, *, binding: dict | None = None) -> dict:
        """Open, dispatch and return one Codex verifier of a write result."""

        task = self.verifier_task(binding or self.binding(work))
        route = self.verifier_route(task)
        assigned = self.verifier_assignment(route)
        handle = self.open_verifier(task, route, assigned)
        self.frames("codex", [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
        ])
        (self.tools / "codex-last-message.json").write_text(json.dumps(null_filled(reply)))
        seen: dict = {}
        original = bridge.run_codex

        def spy(**arguments):
            envelope = arguments["export"] / bridge.VERIFIER_ENVELOPE_PATH
            seen["envelope"] = envelope.read_bytes()
            seen["mode"] = envelope.stat().st_mode & 0o777
            return original(**arguments)

        with mock.patch.object(bridge, "run_codex", spy):
            output = dispatch(
                self.ledger, self.project_id, project_root=self.project,
                bundle_root=self.bundle, task=task, route=route, assignment=assigned,
                parent_grant=dict(READ_SRC), run_handle=handle,
                result_schema=RESULT_SCHEMA, environ=CLEAN,
            )
        return {"output": output, "route": route, "assignment": assigned, "handle": handle,
                "task": task, "seen": seen}

    def verification_state(self, work: dict) -> str:
        return self.ledger.write_verification_state(
            self.project_id, self.binding(work)["bridge_run_id"])

    def verifications(self) -> list[dict]:
        with self.ledger.repository(self.project_id) as repository:
            return repository.records("write_verification")

    def verifier_results(self) -> list[dict]:
        with self.ledger.repository(self.project_id) as repository:
            return repository.records("write_verifier_result")

    def commit(self, run: dict, task: dict, reply: dict | None = None) -> dict:
        return self.ledger.commit_host_result(
            self.project_id, assignment=run["assignment"], task=task, route=run["route"],
            result=reply if reply is not None else run["output"]["result"],
            observation={"run_handle": run["handle"], "result_attempts": ACCEPTED_ATTEMPT},
            execution_configuration=run["route"]["execution_configuration"],
        )

    # --- the accepted path -------------------------------------------------

    def test_codex_verifies_a_claude_patch_from_the_reserved_envelope(self) -> None:
        work = self.written()
        binding = self.binding(work)
        self.assertEqual("missing", self.verification_state(work))
        run = self.verify(work, self.verdict(binding["patch_digest"]))
        route, report = run["route"], run["output"]["bridge_report"]
        self.assertEqual(
            ("codex", "bridge", "read-only", binding),
            (route["provider"], route["dispatch"], route["sandbox_mode"],
             route["verification_for_write"]),
        )
        # The envelope at the reserved path is exactly the recorded one.
        recorded = self.ledger.write_result_artifact(
            self.project_id, binding["bridge_run_id"], "envelope")
        self.assertEqual(recorded, run["seen"]["envelope"])
        self.assertEqual(0o600, run["seen"]["mode"])
        self.assertEqual(binding["patch_digest"], json.loads(recorded)["patch_digest"])
        [invocation] = [item for item in self.invocations("codex") if "exec" in item["argv"]]
        self.assertIn(bridge.VERIFIER_ENVELOPE_PATH, invocation["files"])
        self.assertIn("src/app.py", invocation["files"])
        prompt = invocation["argv"][-1]
        self.assertIn(bridge.VERIFIER_ENVELOPE_PATH, prompt)
        self.assertIn("reviewed_patch_digest:", prompt)
        self.assertEqual(
            (bridge.VERIFIER_ENVELOPE_PATH, binding["patch_digest"], "recorded"),
            (report["verification_input"], report["reviewed_patch_digest"],
             report["write_verification"]),
        )
        [record] = self.verifications()
        self.assertEqual(
            (binding["bridge_run_id"], binding["patch_digest"], "claude", "codex",
             route["model"], report["bridge_run_id"], "pass", "completed"),
            (record["bridge_run_id"], record["patch_digest"], record["worker_provider"],
             record["verifier_provider"], record["verifier_model"],
             record["verifier_bridge_run_id"], record["semantic_verdict"],
             record["result_status"]),
        )
        self.assertEqual("accepted", self.verification_state(work))
        # Nothing is left in the dispatch directory, and no content reaches the ledger.
        self.assertEqual(([], []), self.leftovers())
        self.assertNotIn("changed", json.dumps(record))
        # The committed host result must be the verifier's own result.
        forged = {**run["output"]["result"], "summary": "edited by the primary"}
        with self.assertRaisesRegex(LedgerError, "differs from the verifier run's recorded result"):
            self.commit(run, run["task"], forged)
        committed = self.commit(run, run["task"])
        self.assertEqual("accepted", committed["write_verification_state"])

    def check_report(self) -> dict:
        check = {"check_id": "unit", "passed": True, "evidence_digest": "a" * 64}
        return {
            "deterministic_checks": [check], "security_checks": [check],
            "observed_at_ms": time.time_ns() // 1_000_000,
        }

    def test_operational_qa_of_a_write_result_through_its_verification(self) -> None:
        work = self.written()
        binding = self.binding(work)
        run = self.verify(work, self.verdict(binding["patch_digest"]))
        worker = self.commit(work, self.write_task())
        verifier = self.commit(run, run["task"])
        host_digest = content_digest(worker["outcome"])
        verifier_digest = content_digest(verifier["outcome"])
        # The verifier's context stays the repository paths it read.
        self.assertEqual(["src"], run["assignment"]["context"])
        receipt = self.ledger.issue_operational_check_receipt(
            self.project_id, host_outcome_digest=host_digest,
            verifier_host_outcome_digest=verifier_digest, check_report=self.check_report(),
        )
        self.assertEqual(
            {**binding, "tested_tree_digest": None}, receipt["write_binding"])
        inputs = {
            "host_outcome_digest": host_digest,
            "verifier_host_outcome_digest": verifier_digest,
            "evaluator_assignment": work["assignment"],
            "evaluator_result": work["output"]["result"],
            "check_receipt": receipt["check_receipt"],
            "verifier_assignment": run["assignment"],
        }
        # A verifier result other than the recorded verification is refused.
        forged = {**run["output"]["result"], "summary": "another review"}
        with self.assertRaisesRegex(LedgerError, "differs from its verification"):
            self.ledger.commit_operational_qa(
                self.project_id, **inputs, verifier_result=forged)
        committed = self.ledger.commit_operational_qa(
            self.project_id, **inputs, verifier_result=run["output"]["result"])
        self.assertTrue(committed["qa"]["accepted"])
        self.assertEqual("accepted", committed["outcome"]["status"])
        self.assertEqual({**binding, "tested_tree_digest": None}, committed["write_binding"])

    def test_operational_qa_refuses_a_same_provider_native_verifier(self) -> None:
        work = self.written()
        worker = self.commit(work, self.write_task())
        # A native Claude verifier of the Claude patch, with no write verification.
        (self.bundle / ".claude/agents/claude_agents_verification_worker.md").write_text(
            (self.bundle / ".claude/agents/claude_agents_researcher.md").read_text()
            .replace("claude_agents_researcher", "claude_agents_verification_worker")
        )
        task = self.verifier_task(None)
        route = route_task(task, self.policy_with(codex=False), primary_provider="claude")
        self.assertEqual(("claude", "native"), (route["provider"], route["dispatch"]))
        route["execution_configuration"] = installed_execution_configuration(
            self.bundle, route, client_capability=self.clients["claude-host-v1"]
        )
        assigned = self.assignment_for(
            route, assignment_id="verify-assignment-1", task_id="verify-1",
            objective="Verify the patch", context=[content_digest(worker["outcome"])],
            owned_paths=[], verification_required=False, permissions=dict(READ_SRC),
        )
        handle = self.ledger.open_host_run(
            self.project_id, assignment=assigned, task=task, route=route,
            execution_configuration=route["execution_configuration"],
            client_version="claude-host-v1", orchestration_handle=self.orchestration(),
        )["run_handle"]
        self.ledger.bind_host_agent(self.project_id, run_handle=handle, agent_id="agent-native-1")
        verifier = self.commit(
            {"assignment": assigned, "route": route, "handle": handle}, task,
            self.verdict(None),
        )
        self.assertEqual([], self.verifications())
        scope = {
            "host_outcome_digest": content_digest(worker["outcome"]),
            "verifier_host_outcome_digest": content_digest(verifier["outcome"]),
        }
        with self.assertRaisesRegex(LedgerError, "accepted verification"):
            self.ledger.validate_operational_qa_scope(self.project_id, **scope)
        with self.assertRaisesRegex(LedgerError, "accepted verification"):
            self.ledger.issue_operational_check_receipt(
                self.project_id, **scope, check_report=self.check_report())
        # An accepted Codex verification of the patch does not attribute the Claude run.
        self.verify(work, self.verdict(self.binding(work)["patch_digest"]))
        with self.assertRaisesRegex(LedgerError, "no positive other-provider verification"):
            self.ledger.issue_operational_check_receipt(
                self.project_id, **scope, check_report=self.check_report())

    # --- states other than accepted ----------------------------------------

    def test_negative_and_blocked_verdicts_are_not_accepted(self) -> None:
        work = self.written()
        digest = self.binding(work)["patch_digest"]
        self.verify(work, self.verdict(digest, verdict="blocked"))
        self.assertEqual("blocked", self.verification_state(work))
        self.verify(work, self.verdict(digest))
        # A positive verdict never outweighs a blocked one.
        self.assertEqual("blocked", self.verification_state(work))
        self.verify(work, self.verdict(digest, verdict="reject"))
        self.assertEqual("rejected", self.verification_state(work))
        self.assertEqual(
            ["blocked", "pass", "reject"],
            sorted(record["semantic_verdict"] for record in self.verifications()),
        )

    def test_not_ready_is_rejected(self) -> None:
        work = self.written()
        self.verify(work, self.verdict(self.binding(work)["patch_digest"], verdict="not-ready"))
        self.assertEqual("rejected", self.verification_state(work))

    def forged_pass(self, work: dict) -> dict:
        return self.verdict(self.binding(work)["patch_digest"])

    def test_a_reviewed_digest_that_differs_or_is_missing_records_nothing(self) -> None:
        work = self.written()
        for stated in ("0" * 64, None):
            run = self.verify(work, self.verdict(stated))
            report = run["output"]["bridge_report"]
            self.assertEqual("not_recorded", report["write_verification"])
            self.assertEqual("reviewed_patch_digest_mismatch",
                             report["write_verification_refusal"])
            # The primary cannot replace it with an invented, correctly bound pass.
            with self.assertRaisesRegex(LedgerError, "recorded result"):
                self.commit(run, run["task"], self.forged_pass(work))
            self.commit(run, run["task"])
        self.assertEqual([], self.verifications())
        self.assertEqual(
            ["reviewed_patch_digest_mismatch"] * 2,
            [record["write_verification"] for record in self.verifier_results()],
        )
        self.assertEqual("missing", self.verification_state(work))

    def test_a_failed_verifier_run_records_nothing(self) -> None:
        work = self.written()
        (self.tools / "codex-exit-code").write_text("1")
        run = self.verify(work, self.verdict(self.binding(work)["patch_digest"]))
        self.assertEqual("failed", run["output"]["result"]["status"])
        report = run["output"]["bridge_report"]
        self.assertEqual(("not_recorded", "result_not_completed"),
                         (report["write_verification"], report["write_verification_refusal"]))
        self.assertEqual("missing", self.verification_state(work))
        with self.assertRaisesRegex(LedgerError, "recorded result"):
            self.commit(run, run["task"], self.forged_pass(work))
        committed = self.commit(run, run["task"])
        self.assertEqual("missing", committed["write_verification_state"])

    def test_a_write_bound_run_without_a_recorded_result_cannot_commit(self) -> None:
        work = self.written()
        with mock.patch.object(
            type(self.ledger), "record_write_verifier_result",
            side_effect=LedgerError("unavailable"),
        ):
            run = self.verify(work, self.verdict(self.binding(work)["patch_digest"]))
        self.assertEqual("unavailable", run["output"]["bridge_report"]["write_verification_refusal"])
        with self.assertRaisesRegex(LedgerError, "recorded result"):
            self.commit(run, run["task"])

    def test_a_task_bound_to_another_digest_is_refused_at_open(self) -> None:
        work = self.written()
        binding = {**self.binding(work), "patch_digest": "1" * 64}
        task = self.verifier_task(binding)
        route = self.verifier_route(task)
        with self.assertRaisesRegex(LedgerError, "patch digest differs"):
            self.open_verifier(task, route, self.verifier_assignment(route))
        self.assertEqual("missing", self.verification_state(work))

    # --- same provider and no verifier --------------------------------------

    def test_same_provider_or_no_other_provider_verifier_is_denied(self) -> None:
        work = self.written()
        binding = self.binding(work)
        task = self.verifier_task(binding)
        # With no Codex verifier allowed, the worker's own provider is never used.
        with self.assertRaises(RouteDenied) as caught:
            self.verifier_route(task, codex=False)
        self.assertEqual(WRITE_VERIFIER_UNAVAILABLE, caught.exception.reason)
        # A Codex primary would verify natively, without the envelope: denied.
        with self.assertRaises(RouteDenied) as caught:
            route_task(task, self.policy_with(), primary_provider="codex")
        self.assertEqual(WRITE_VERIFIER_UNAVAILABLE, caught.exception.reason)
        # Naming the worker as Codex would make a Claude primary's own provider the
        # verifier, natively and without the envelope: denied.
        codex_worker = self.verifier_task(binding, verification_for_provider="codex")
        with self.assertRaises(RouteDenied) as caught:
            self.verifier_route(codex_worker)
        self.assertEqual(WRITE_VERIFIER_UNAVAILABLE, caught.exception.reason)
        # A Codex-worker binding routed to Codex is refused at open: the write
        # result was Claude's.
        codex_route = self.verifier_route(self.verifier_task(None))
        self.assertEqual("codex", codex_route["provider"])
        codex_route["verification_for_write"] = binding
        with self.assertRaisesRegex(LedgerError, WRITE_VERIFIER_UNAVAILABLE):
            self.open_verifier(codex_worker, codex_route, self.verifier_assignment(codex_route))
        # A same-provider route forced onto the binding is refused at open.
        (self.bundle / ".claude/agents/claude_agents_verification_worker.md").write_text(
            (self.bundle / ".claude/agents/claude_agents_researcher.md").read_text()
            .replace("claude_agents_researcher", "claude_agents_verification_worker")
        )
        plain = self.verifier_task(None)
        claude_route = self.verifier_route(plain, codex=False)
        self.assertEqual("claude", claude_route["provider"])
        claude_route["verification_for_write"] = binding
        with self.assertRaisesRegex(LedgerError, WRITE_VERIFIER_UNAVAILABLE):
            self.open_verifier(task, claude_route, self.verifier_assignment(claude_route))
        # The bridge never hands the envelope to the worker's own provider.
        with self.assertRaisesRegex(LedgerError, WRITE_VERIFIER_UNAVAILABLE):
            self.ledger.write_verification_envelope(
                self.project_id, binding, verifier_provider="claude")
        self.assertEqual("missing", self.verification_state(work))

    def test_write_verification_record_requires_the_other_provider(self) -> None:
        material = {
            "schema_version": 1, "record_type": "write_verification", "run_id": "run_1",
            "bridge_run_id": "brg_" + "a" * 32, "patch_digest": "b" * 64,
            "worker_provider": "claude", "verifier_bridge_run_id": "brg_" + "c" * 32,
            "verifier_provider": "codex", "verifier_model": "gpt-6.1-sol",
            "result_status": "completed", "execution_state": "completed",
            "semantic_verdict": "pass", "result_digest": "d" * 64, "observed_at_ms": 1,
            "record_id": "write_verification:x",
        }
        validate_learning_record(material)
        for changes in (
            {"verifier_provider": "claude", "verifier_model": "claude-opus-5-5"},
            {"verifier_model": "claude-opus-5-5"},
            {"verifier_bridge_run_id": "brg_" + "a" * 32},
            {"result_status": "blocked"},
            {"prompt": "never stored"},
        ):
            with self.assertRaises(ContractError):
                validate_learning_record({**material, **changes})
        # Only the ledger writes it, through the checked path.
        for producer in ("host", "lifecycle", "verifier"):
            with self.assertRaises(LedgerError):
                self.ledger.append(self.project_id, material, producer=producer)
        executed = {
            "schema_version": 1, "record_type": "write_verifier_result", "run_id": "run_1",
            "bridge_run_id": "brg_" + "a" * 32, "patch_digest": "b" * 64,
            "verifier_bridge_run_id": "brg_" + "c" * 32, "result_digest": "d" * 64,
            "write_verification": "result_not_completed", "observed_at_ms": 1,
            "record_id": "write_verifier_result:x",
        }
        validate_learning_record(executed)
        for changes in ({"write_verification": "Not Recorded"}, {"result": "never stored"}):
            with self.assertRaises(ContractError):
                validate_learning_record({**executed, **changes})
        for producer in ("host", "lifecycle", "verifier"):
            with self.assertRaises(LedgerError):
                self.ledger.append(self.project_id, executed, producer=producer)

    # --- unrecorded and failed write results --------------------------------

    def test_verification_for_a_failed_or_unrecorded_write_result_is_refused(self) -> None:
        failed = self.written({"write": "docs/guide.md", "content": "changed\n"})
        self.assertNotEqual("passed", failed["output"]["bridge_report"]["ownership"])
        for binding, message in (
            ({"bridge_run_id": failed["output"]["bridge_report"]["bridge_run_id"],
              "patch_digest": "2" * 64}, "ownership passed"),
            ({"bridge_run_id": "brg_" + "e" * 32, "patch_digest": "2" * 64},
             "recorded write result"),
        ):
            task = self.verifier_task(binding)
            route = self.verifier_route(task)
            with self.assertRaisesRegex(LedgerError, message):
                self.open_verifier(task, route, self.verifier_assignment(route))
            with self.assertRaisesRegex(LedgerError, message):
                self.ledger.write_verification_envelope(
                    self.project_id, binding, verifier_provider="codex")
            self.assertEqual(
                "missing",
                self.ledger.write_verification_state(self.project_id, binding["bridge_run_id"]),
            )

    # --- the reserved input path --------------------------------------------

    def test_assignment_context_cannot_name_the_reserved_input(self) -> None:
        for name in (".orchestration-input", ".Orchestration-Input",
                     ".orchestration-input/envelope.json"):
            assigned = {"context": [name], "owned_paths": [],
                        "permissions": {"filesystem": [f"{name}:read"]}}
            with self.assertRaisesRegex(BridgeRefused, "reserved verifier input"):
                assignment_read_paths(assigned)
            with self.assertRaises(workspace.WorkspaceRefused) as caught:
                workspace.assignment_export_paths(assigned)
            self.assertEqual("path_protected", caught.exception.reason)
        # A write child can never create it, at any depth.
        self.assertTrue(workspace.is_protected_path(".orchestration-input/envelope.json"))
        self.assertTrue(workspace.is_protected_path("src/.ORCHESTRATION-INPUT"))
        self.assertIn(".orchestration-input", bridge._protected_variants())


class WriteVerificationContractTests(unittest.TestCase):
    def task(self, **changes) -> dict:
        task = {
            "id": "verify-1", "objective": "Verify", "family": "verification",
            "complexity": "medium", "risk": "low", "operation": "review",
            "ambiguity": "known", "context_breadth": "narrow",
            "acceptance_strength": "independent_judgment", "routing_evidence_refs": [],
            "meaningful": True, "deterministic_validation": False, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            "verification_for_family": "coding", "verification_for_provider": "claude",
            "verification_for_write": {"bridge_run_id": "brg_" + "a" * 32,
                                       "patch_digest": "b" * 64},
        }
        task.update(changes)
        return {key: value for key, value in task.items() if value is not None}

    def test_verification_for_write_is_closed_and_needs_the_worker_fields(self) -> None:
        validate_task(self.task())
        for changes in (
            {"verification_for_provider": None},
            {"verification_for_family": None},
            {"family": "coding", "verification_for_family": None,
             "verification_for_provider": None},
            {"verification_for_write": {"bridge_run_id": "brg_x", "patch_digest": "b" * 64}},
            {"verification_for_write": {"bridge_run_id": "brg_" + "a" * 32,
                                        "patch_digest": "B" * 64}},
            {"verification_for_write": {"bridge_run_id": "brg_" + "a" * 32,
                                        "patch_digest": "b" * 64, "extra": 1}},
        ):
            with self.assertRaises(ContractError):
                validate_task(self.task(**changes))


class ProviderAwareQaTests(unittest.TestCase):
    def inputs(self, *, role: str, model: str) -> dict:
        inputs = verified_qa_inputs()
        inputs.pop("evaluation_assignment")
        inputs.pop("evaluation_case")
        inputs["host_outcome"]["evaluation_candidate_digest"] = None
        inputs["host_outcome"]["evaluation_case_digest"] = None
        inputs["verifier_assignment"].update(
            role=role, model=model, context=[content_digest(inputs["host_outcome"])])
        verifier_host = inputs["verifier_host_outcome"]
        verifier_host.update(role=role, model=model)
        verifier_host["configuration_digest"] = content_digest({
            key: verifier_host[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest",
                "package_digest", "policy_digest", "agent_digest", "skill_digests",
                "prompt_digest", "client_digest",
            )
        })
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted({
            keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_assignment"])),
            keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
        })
        return inputs

    def test_qa_accepts_the_claude_verifier_role(self) -> None:
        qa, record = build_operational_qa_records(
            **self.inputs(role="claude_agents_verification_worker", model="claude-opus-5-5"))
        self.assertTrue(qa["accepted"])
        self.assertTrue(record["accepted"])

    def test_qa_refuses_a_non_verifier_role(self) -> None:
        with self.assertRaisesRegex(ContractError, "independently bound"):
            build_operational_qa_records(
                **self.inputs(role="claude_agents_researcher", model="claude-opus-5-5"))


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None):
    # Only this module's own tests: the write child tests it builds on run in
    # their own module.
    suite = unittest.TestSuite()
    for case in (WriteVerificationTests, WriteVerificationContractTests, ProviderAwareQaTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite
