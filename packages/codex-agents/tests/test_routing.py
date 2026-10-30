from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import routing
from codex_agents.contracts import ContractError
from codex_agents.routing import (
    PolicyError,
    load_effective_policy,
    load_policy,
    route_task,
    validate_project_agent_mapping,
)


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json")

    def task(self, **overrides: object) -> dict:
        task = {
            "id": "task-1",
            "objective": "Complete one bounded task",
            "family": "coding",
            "complexity": "medium",
            "risk": "medium",
            "operation": "implementation",
            "ambiguity": "known",
            "context_breadth": "repository",
            "acceptance_strength": "executable",
            "routing_evidence_refs": [],
            "meaningful": True,
            "deterministic_validation": True,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
        }
        task.update(overrides)
        return task

    def test_bounded_deterministic_code_prefers_luna_high(self) -> None:
        route = route_task(
            self.task(complexity="low", risk="low", deterministic_validation=True),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))
        self.assertEqual("bounded_executable_work", route["reason"])
        self.assertEqual("execution", route["stage"])
        self.assertEqual(
            {"model": "gpt-6-luna", "effort": "high"},
            route["selection"]["selected"],
        )
        self.assertEqual(
            "deterministic_task_shape_v4", route["selection"]["strategy"]
        )
        self.assertEqual("codex_agents_implementation_worker", route["role"])
        self.assertTrue(route["verification_required"])

    def test_task_class_observation_deadline_is_explicit_and_not_a_kill_claim(self) -> None:
        research = route_task(
            self.task(
                family="research", operation="review",
                acceptance_strength="independent_judgment",
            ),
            self.policy,
        )
        coding = route_task(self.task(), self.policy)
        self.assertEqual(21_600, research["observation_deadline_seconds"])
        self.assertEqual(10_800, coding["observation_deadline_seconds"])
        self.assertEqual("host_observation_only", research["deadline_semantics"])

    def test_bounded_executable_code_prefers_luna_without_deterministic_flag(self) -> None:
        route = route_task(
            self.task(
                acceptance_strength="executable",
                deterministic_validation=False,
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))

    def test_exploratory_code_routes_to_gpt6_sol(self) -> None:
        route = route_task(
            self.task(
                operation="exploration",
                ambiguity="partial",
                deterministic_validation=False,
                acceptance_strength="independent_judgment",
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-sol", "medium"), (route["model"], route["effort"]))

    def test_planning_routes_to_sol(self) -> None:
        route = route_task(self.task(family="planning"), self.policy)
        self.assertEqual("gpt-6-sol", route["model"])
        self.assertEqual("codex_agents_planner", route["role"])

    def test_security_route_uses_evaluated_sol_high(self) -> None:
        route = route_task(self.task(family="security"), self.policy)
        self.assertEqual(("gpt-6-sol", "high"), (route["model"], route["effort"]))

    def test_only_evidenced_hard_end_to_end_work_uses_astra_medium(self) -> None:
        evidence = "outcome:" + "a" * 64
        route = route_task(
            self.task(
                complexity="extreme",
                operation="end_to_end",
                ambiguity="high",
                context_breadth="cross_system",
                acceptance_strength="independent_judgment",
                routing_evidence_refs=[evidence],
            ),
            self.policy,
            trusted_routing_evidence_refs=frozenset({evidence}),
        )
        self.assertEqual(("gpt-6-astra", "medium"), (route["model"], route["effort"]))
        self.assertEqual("hard_end_to_end_evidence", route["reason"])

    def test_unresolved_end_to_end_evidence_does_not_use_astra(self) -> None:
        route = route_task(
            self.task(
                complexity="extreme",
                operation="end_to_end",
                ambiguity="high",
                context_breadth="cross_system",
                acceptance_strength="independent_judgment",
                routing_evidence_refs=["outcome:" + "a" * 64],
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-sol", "high"), (route["model"], route["effort"]))
        self.assertEqual("cross_system_synthesis", route["reason"])

    def test_high_complexity_alone_keeps_bounded_luna_route(self) -> None:
        route = route_task(self.task(complexity="high"), self.policy)
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))

    def test_missing_route_facts_select_bounded_reconnaissance(self) -> None:
        route = route_task(
            self.task(ambiguity="unknown", context_breadth="unknown"), self.policy
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))
        self.assertEqual("reconnaissance", route["stage"])
        self.assertEqual("research", route["routed_family"])
        self.assertEqual("codex_agents_researcher", route["role"])

    def test_cross_system_synthesis_routes_to_sol(self) -> None:
        route = route_task(
            self.task(
                operation="synthesis",
                ambiguity="high",
                context_breadth="cross_system",
                deterministic_validation=False,
                acceptance_strength="independent_judgment",
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-sol", "high"), (route["model"], route["effort"]))

    def test_cross_system_exploration_and_diagnosis_use_gpt6_sol(self) -> None:
        for operation in ("exploration", "diagnosis", "review"):
            with self.subTest(operation=operation):
                route = route_task(
                    self.task(
                        operation=operation,
                        context_breadth="cross_system",
                        acceptance_strength="independent_judgment",
                        deterministic_validation=False,
                    ),
                    self.policy,
                )
                self.assertEqual("gpt-6-sol", route["model"])

    def test_result_contract_failure_does_not_escalate(self) -> None:
        route = route_task(
            self.task(
                failed_attempts=1,
                failure_events=[
                    {
                        "attempt": 1,
                        "reason": "result_contract",
                        "evidence_refs": ["result-contract:missing-field"],
                    },
                    {
                        "attempt": 1,
                        "reason": "result_contract",
                        "evidence_refs": ["result-contract:repair-invalid"],
                    },
                ],
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))

    def test_one_semantic_failure_does_not_escalate(self) -> None:
        route = route_task(
            self.task(
                failed_attempts=1,
                failure_events=[
                    {
                        "attempt": 1,
                        "reason": "semantic",
                        "evidence_refs": ["verification:" + "a" * 64],
                    }
                ],
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))

    def test_claimed_reasoning_failures_do_not_escalate(self) -> None:
        route = route_task(
            self.task(
                failed_attempts=2,
                failure_events=[
                    {
                        "attempt": 1,
                        "reason": "semantic",
                        "evidence_refs": ["verification:" + "a" * 64],
                    },
                    {
                        "attempt": 2,
                        "reason": "reasoning",
                        "evidence_refs": ["verification:" + "b" * 64],
                    },
                ],
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))

    def test_critical_risk_does_not_raise_model_or_authority(self) -> None:
        common = {
            "owned_paths": ["src/"],
            "independent_parts": 3,
            "parallel_safe": True,
        }
        low_route = route_task(self.task(risk="low", **common), self.policy)
        route = route_task(
            self.task(risk="critical", **common),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))
        self.assertEqual("parallel", route["topology"])
        self.assertEqual(3, route["max_parallel"])
        self.assertEqual(
            (low_route["model"], low_route["effort"], low_route["topology"]),
            (route["model"], route["effort"], route["topology"]),
        )
        self.assertTrue(route["verification_required"])
        self.assertNotIn("owned_paths", route)

    def test_parallelism_is_bounded_and_explicit(self) -> None:
        route = route_task(
            self.task(independent_parts=9, parallel_safe=True),
            self.policy,
        )
        self.assertEqual("parallel", route["topology"])
        self.assertEqual(4, route["max_parallel"])

    def test_verifier_does_not_recursively_require_verifier(self) -> None:
        route = route_task(self.task(family="verification"), self.policy)
        self.assertFalse(route["verification_required"])
        self.assertIsNone(route["verifier"])

    def test_bounded_deterministic_verification_prefers_gpt6_luna(self) -> None:
        route = route_task(
            self.task(
                family="verification",
                operation="review",
                acceptance_strength="deterministic",
                deterministic_validation=True,
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-luna", "high"), (route["model"], route["effort"]))
        self.assertEqual("bounded_deterministic_verification", route["reason"])

    def test_judgment_verification_uses_gpt6_sol(self) -> None:
        route = route_task(
            self.task(
                family="verification",
                operation="review",
                acceptance_strength="independent_judgment",
                deterministic_validation=False,
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6-sol", "high"), (route["model"], route["effort"]))
        self.assertEqual("independent_verification", route["reason"])

    def test_active_policy_contains_no_terra_and_only_sol_legacy_fallback(self) -> None:
        serialized = json.dumps(self.policy, sort_keys=True)
        self.assertNotIn("gpt-5.6-terra", serialized)
        self.assertNotIn("gpt-5.6-luna", serialized)
        self.assertEqual(
            {"gpt-5.6-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"},
            set(self.policy["models"]),
        )
        self.assertEqual(
            {"model": "gpt-5.6-sol", "effort": "high"},
            self.policy["legacy_fallback"],
        )

    def test_packaged_orchestration_guidance_never_assigns_terra(self) -> None:
        paths = (
            BUNDLE_ROOT / ".agents/skills/orchestrate-agents/SKILL.md",
            BUNDLE_ROOT / ".agents/skills/orchestrate-agents/references/routing.md",
        )
        guidance = "\n".join(path.read_text() for path in paths)
        self.assertNotIn("gpt-5.6-terra", guidance.lower())
        self.assertNotIn("terra for", guidance.lower())
        self.assertNotIn("use terra", guidance.lower())

    def test_policy_rejects_any_other_legacy_fallback(self) -> None:
        policy = deepcopy(self.policy)
        policy["legacy_fallback"] = {"model": "gpt-6-sol", "effort": "high"}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            path = Path(directory) / "routing-policy.json"
            path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(PolicyError, "sole legacy fallback"):
                load_policy(path)

    def test_unknown_task_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            route_task(self.task(model="gpt-6-astra"), self.policy)

    def test_explicit_read_only_project_agent_mapping_preserves_policy_model(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            orchestration = root / ".codex/orchestration"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            (agents / "technology-researcher.toml").write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research the bounded question."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
            (agents / "evidence-reviewer.toml").write_text(
                'name = "evidence_reviewer"\n'
                'description = "Project evidence reviewer"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Review the bounded evidence."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"research": "technology_researcher"},
                        "verifiers": {"research": "evidence_reviewer"},
                    }
                )
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Routing Tests"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "routing config"],
                check=True,
            )
            policy = load_effective_policy(
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            route = route_task(
                self.task(
                    id="codex-c3-linux-arm64-artifact-research",
                    family="research",
                    operation="exploration",
                    deterministic_validation=False,
                    acceptance_strength="independent_judgment",
                ),
                policy,
            )
            self.assertEqual("codex_agents_researcher", route["role"])
            self.assertEqual("technology_researcher", route["agent_type"])
            self.assertEqual(
                ".codex/agents/technology-researcher.toml", route["agent_path"]
            )
            self.assertEqual("evidence_reviewer", route["verifier"]["agent_type"])
            self.assertEqual(
                ".codex/agents/evidence-reviewer.toml",
                route["verifier"]["agent_path"],
            )
            self.assertEqual("read-only", route["verifier"]["sandbox_mode"])
            self.assertEqual(("gpt-6-sol", "medium"),
                             (route["model"], route["effort"]))
            verifier_route = route_task(
                self.task(
                    id="codex-c3-linux-arm64-artifact-research-verification",
                    family="verification",
                    operation="review",
                    acceptance_strength="independent_judgment",
                    deterministic_validation=False,
                    verification_for_family="research",
                ),
                policy,
            )
            self.assertEqual("evidence_reviewer", verifier_route["agent_type"])
            self.assertEqual(
                ".codex/agents/evidence-reviewer.toml",
                verifier_route["agent_path"],
            )
            self.assertEqual("read-only", verifier_route["sandbox_mode"])
            self.assertEqual(
                ("gpt-6-sol", "high"),
                (verifier_route["model"], verifier_route["effort"]),
            )

    def test_project_agent_mapping_rejects_workspace_write_researcher(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            orchestration = root / ".codex/orchestration"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            (agents / "writer.toml").write_text(
                'name = "writer"\n'
                'description = "Writer"\n'
                'sandbox_mode = "workspace-write"\n'
                'developer_instructions = "Write files."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps({"version": 1, "roles": {"research": "writer"}})
            )
            with self.assertRaisesRegex(PolicyError, "unauthorized sandbox"):
                load_effective_policy(
                    root,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_project_agent_mapping_allows_workspace_write_coding_and_verifier(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            agents.mkdir(parents=True)
            for name in ("implementation_worker", "verification_worker"):
                (agents / f"{name}.toml").write_text(
                    f'name = "{name}"\n'
                    f'description = "{name}"\n'
                    'sandbox_mode = "workspace-write"\n'
                    'developer_instructions = "Complete the bounded assignment."\n'
                    'mcp_servers = []\n'
                    'skills = []\n'
                )
            mapping = validate_project_agent_mapping(
                root,
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"coding": "implementation_worker"},
                        "verifiers": {"coding": "verification_worker"},
                    }
                ).encode(),
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            self.assertEqual(
                {"coding": "implementation_worker"},
                mapping["roles"],
            )
            self.assertEqual(
                {"coding": "verification_worker"},
                mapping["verifiers"],
            )
            self.assertEqual(
                {"coding": "workspace-write"},
                mapping["role_sandboxes"],
            )
            self.assertEqual(
                {"coding": "workspace-write"},
                mapping["verifier_sandboxes"],
            )

    def test_project_agent_mapping_rejects_model_override(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            orchestration = root / ".codex/orchestration"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            (agents / "overriding_researcher.toml").write_text(
                'name = "overriding_researcher"\n'
                'description = "Overrides package policy"\n'
                'sandbox_mode = "read-only"\n'
                'model = "gpt-6-astra"\n'
                'developer_instructions = "Research."\n'
                'mcp_servers = []\n'
                'skills = []\n'
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"research": "overriding_researcher"},
                    }
                )
            )
            with self.assertRaisesRegex(PolicyError, "must not override model or effort"):
                load_effective_policy(
                    root,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_project_agent_mapping_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            for raw in (
                b'{"version":1,"roles":{},"roles":{}}',
                b'{"version":1,"roles":{"research":"one","research":"two"}}',
            ):
                with self.subTest(raw=raw), self.assertRaisesRegex(
                    PolicyError, "mapping is invalid"
                ):
                    validate_project_agent_mapping(
                        root,
                        raw,
                        ORCHESTRATION_ROOT / "config/routing-policy.json",
                        ORCHESTRATION_ROOT / "config/discovery-policy.json",
                    )

    def test_dangling_project_agent_mapping_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            orchestration = root / ".codex/orchestration"
            orchestration.mkdir(parents=True)
            (orchestration / "project-agent-routing.json").symlink_to(
                root / "missing-routing.json"
            )
            with self.assertRaisesRegex(PolicyError, "not a regular file"):
                load_effective_policy(
                    root,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_mapped_project_agent_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            definitions = root / "agent-definitions"
            agents.mkdir(parents=True)
            definitions.mkdir()
            target = definitions / "technology_researcher.toml"
            target.write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research."\n'
                'mcp_servers = []\n'
                'skills = []\n',
                encoding="utf-8",
            )
            (agents / "technology_researcher.toml").symlink_to(target)
            raw = json.dumps(
                {
                    "version": 1,
                    "roles": {"research": "technology_researcher"},
                }
            ).encode()
            with self.assertRaisesRegex(PolicyError, "must not be a symlink"):
                validate_project_agent_mapping(
                    root,
                    raw,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_mapped_project_skill_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            skill_root = root / ".agents/skills"
            target = skill_root / "real_skill/SKILL.md"
            agents.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            target.write_text(
                "---\nname: real_skill\ndescription: Real skill.\n---\n",
                encoding="utf-8",
            )
            (skill_root / "linked_skill").symlink_to(target.parent)
            (agents / "technology_researcher.toml").write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research."\n'
                'mcp_servers = []\n'
                'skills = ["linked_skill"]\n',
                encoding="utf-8",
            )
            raw = json.dumps(
                {
                    "version": 1,
                    "roles": {"research": "technology_researcher"},
                }
            ).encode()
            with self.assertRaisesRegex(
                PolicyError, "catalog is not ready|skill is unavailable"
            ):
                validate_project_agent_mapping(
                    root,
                    raw,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_mapped_project_skill_must_be_committed_and_clean(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".codex/agents"
            orchestration = root / ".codex/orchestration"
            skill = root / ".agents/skills/project_skill/SKILL.md"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            skill.parent.mkdir(parents=True)
            (agents / "technology_researcher.toml").write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research the bounded question."\n'
                'mcp_servers = []\n'
                'skills = ["project_skill"]\n',
                encoding="utf-8",
            )
            skill.write_text(
                "---\nname: project_skill\ndescription: Project research skill.\n---\n",
                encoding="utf-8",
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps(
                    {"version": 1, "roles": {"research": "technology_researcher"}}
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Routing Tests"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "add", ".codex"], check=True
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "routing config"],
                check=True,
            )

            with self.assertRaisesRegex(PolicyError, "not committed and clean"):
                load_effective_policy(
                    root,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

            subprocess.run(["git", "-C", str(root), "add", ".agents"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "project skill"],
                check=True,
            )
            policy = load_effective_policy(
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            self.assertEqual(
                [".agents/skills/project_skill/SKILL.md"],
                policy["project_agent_role_skill_paths"]["research"],
            )

            skill.write_text(skill.read_text() + "\nChanged.\n", encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "not committed and clean"):
                load_effective_policy(
                    root,
                    ORCHESTRATION_ROOT / "config/routing-policy.json",
                    ORCHESTRATION_ROOT / "config/discovery-policy.json",
                )

    def test_project_agent_trust_uses_one_captured_byte_set(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agent = root / ".codex/agents/technology_researcher.toml"
            mapping = root / ".codex/orchestration/project-agent-routing.json"
            skill = root / ".agents/skills/project_skill/SKILL.md"
            agent.parent.mkdir(parents=True)
            mapping.parent.mkdir(parents=True)
            skill.parent.mkdir(parents=True)
            agent.write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research the bounded question."\n'
                'mcp_servers = []\n'
                'skills = ["project_skill"]\n',
                encoding="utf-8",
            )
            skill.write_text(
                "---\nname: project_skill\ndescription: Project research skill.\n---\n",
                encoding="utf-8",
            )
            mapping.write_text(
                json.dumps(
                    {"version": 1, "roles": {"research": "technology_researcher"}}
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Routing Tests"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "trusted definitions"],
                check=True,
            )
            load_effective_policy(
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            original_read = routing._read_project_regular

            def run_with_changed_bytes(relative_to_change: str, suffix: bytes) -> None:
                def changed_read(
                    definition_root: Path, relative: str, *, max_bytes: int
                ) -> bytes:
                    captured = original_read(
                        definition_root, relative, max_bytes=max_bytes
                    )
                    return (
                        captured + suffix
                        if relative == relative_to_change
                        else captured
                    )

                with patch(
                    "codex_agents.routing._read_project_regular",
                    side_effect=changed_read,
                ), self.assertRaises(PolicyError):
                    load_effective_policy(
                        root,
                        ORCHESTRATION_ROOT / "config/routing-policy.json",
                        ORCHESTRATION_ROOT / "config/discovery-policy.json",
                    )

            run_with_changed_bytes(
                ".codex/orchestration/project-agent-routing.json", b" "
            )
            run_with_changed_bytes(
                ".codex/agents/technology_researcher.toml", b"\n# transient\n"
            )
            run_with_changed_bytes(
                ".agents/skills/project_skill/SKILL.md", b"\nTransient.\n"
            )


if __name__ == "__main__":
    unittest.main()
