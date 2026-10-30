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
from codex_agents.learning_contracts import content_digest
from codex_agents.routing import (
    DEFAULT_PROVIDER_CONFIG_DIGEST,
    PolicyError,
    _route_task_legacy_fallback,
    admission_settings,
    load_effective_policy,
    load_policy,
    route_task,
    validate_project_agent_mapping,
    validate_provider_routing,
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

    def test_bounded_deterministic_code_prefers_sol_medium(self) -> None:
        route = route_task(
            self.task(complexity="low", risk="low", deterministic_validation=True),
            self.policy,
        )
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))
        self.assertEqual("bounded_executable_work", route["reason"])
        self.assertEqual("execution", route["stage"])
        self.assertEqual(
            {"model": "gpt-6.1-sol", "effort": "medium"},
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

    def test_bounded_executable_code_prefers_sol_without_deterministic_flag(self) -> None:
        route = route_task(
            self.task(
                acceptance_strength="executable",
                deterministic_validation=False,
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))

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
        self.assertEqual(("gpt-6.1-sol", "high"), (route["model"], route["effort"]))

    def test_planning_routes_to_sol(self) -> None:
        route = route_task(self.task(family="planning"), self.policy)
        self.assertEqual("gpt-6.1-sol", route["model"])
        self.assertEqual("codex_agents_planner", route["role"])
        # Routine planning runs Sol at high effort, as the routing table says.
        self.assertEqual(("high", "bounded_planning"), (route["effort"], route["reason"]))

    def test_security_route_uses_explicit_astra_high_candidate(self) -> None:
        route = route_task(self.task(family="security"), self.policy)
        self.assertEqual(("gpt-6-astra", "high"), (route["model"], route["effort"]))
        self.assertEqual(("codex", "native"), (route["provider"], route["dispatch"]))
        self.assertEqual("security_judgment", route["reason"])

    def test_only_evidenced_hard_end_to_end_work_uses_astra_high(self) -> None:
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
        self.assertEqual(("gpt-6-astra", "high"), (route["model"], route["effort"]))
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
        self.assertEqual(("gpt-6.1-sol", "high"), (route["model"], route["effort"]))
        self.assertEqual("cross_system_synthesis", route["reason"])

    def test_high_complexity_alone_keeps_bounded_sol_route(self) -> None:
        route = route_task(self.task(complexity="high"), self.policy)
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))

    def test_missing_route_facts_select_bounded_reconnaissance(self) -> None:
        route = route_task(
            self.task(ambiguity="unknown", context_breadth="unknown"), self.policy
        )
        self.assertEqual(("gpt-6.1-sol", "low"), (route["model"], route["effort"]))
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
        self.assertEqual(("gpt-6.1-sol", "high"), (route["model"], route["effort"]))

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
                self.assertEqual("gpt-6.1-sol", route["model"])

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
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))

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
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))

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
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))

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
        self.assertEqual(("gpt-6.1-sol", "medium"), (route["model"], route["effort"]))
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

    def test_bounded_deterministic_verification_prefers_sol_high(self) -> None:
        route = route_task(
            self.task(
                family="verification",
                operation="review",
                acceptance_strength="deterministic",
                deterministic_validation=True,
            ),
            self.policy,
        )
        self.assertEqual(("gpt-6.1-sol", "high"), (route["model"], route["effort"]))
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
        self.assertEqual(("gpt-6.1-sol", "high"), (route["model"], route["effort"]))
        self.assertEqual("independent_verification", route["reason"])

    def test_active_policy_contains_no_terra_and_only_sol_legacy_fallback(self) -> None:
        serialized = json.dumps(self.policy, sort_keys=True)
        self.assertNotIn("gpt-5.6-terra", serialized)
        self.assertNotIn("gpt-5.6-luna", serialized)
        self.assertEqual(
            {"gpt-5.6-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"},
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
        policy["legacy_fallback"] = {"model": "gpt-6.1-sol", "effort": "high"}
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
            self.assertEqual(("gpt-6.1-sol", "high"),
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
                ("gpt-6.1-sol", "high"),
                (verifier_route["model"], verifier_route["effort"]),
            )

    def test_project_agent_mapping_rejects_workspace_write_researcher(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
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
            subprocess.run(["git", "init", "-q", str(root)], check=True)
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
            subprocess.run(["git", "init", "-q", str(root)], check=True)
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


class ProviderRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json")

    task = RoutingTests.task

    def exploratory(self, **overrides: object) -> dict:
        return self.task(
            operation="exploration",
            ambiguity="partial",
            deterministic_validation=False,
            acceptance_strength="independent_judgment",
            **overrides,
        )

    def provider_policy(
        self,
        *,
        codex: dict | None = None,
        claude: dict | None = None,
        bridge: bool = False,
    ) -> dict:
        config = {
            "version": 1,
            "providers": {
                "codex": codex if codex is not None else {"enabled": True},
                "claude": claude if claude is not None else {"enabled": True},
            },
            "codex_primary_may_bridge_to_claude": bridge,
        }
        policy = deepcopy(self.policy)
        policy["provider_routing"] = config
        policy["provider_config_digest"] = content_digest(config)
        return policy

    def write_policy(self, directory: str, policy: dict) -> Path:
        path = Path(directory) / "routing-policy.json"
        path.write_text(json.dumps(policy))
        return path

    def test_default_configuration_keeps_codex_routes_native(self) -> None:
        for task in (
            self.task(),
            self.exploratory(),
            self.task(family="planning"),
            self.task(family="research", ambiguity="unknown"),
        ):
            with self.subTest(task=task):
                route = route_task(task, self.policy)
                self.assertEqual("codex", route["provider"])
                self.assertEqual("native", route["dispatch"])
                self.assertEqual(
                    DEFAULT_PROVIDER_CONFIG_DIGEST, route["provider_config_digest"]
                )
                self.assertTrue(route["role"].startswith("codex_agents_"))
                self.assertEqual("codex", route["verifier"]["provider"])
                self.assertEqual(
                    ("gpt-6.1-sol", "high"),
                    (route["verifier"]["model"], route["verifier"]["effort"]),
                )
        exploratory = route_task(self.exploratory(), self.policy)
        self.assertEqual(
            ("gpt-6.1-sol", "high"), (exploratory["model"], exploratory["effort"])
        )

    def test_claude_primary_picks_native_claude_and_bridged_codex(self) -> None:
        policy = self.provider_policy()
        exploratory = route_task(
            self.exploratory(), policy, primary_provider="claude"
        )
        self.assertEqual(
            ("claude", "native", "claude-opus-5-5", "medium"),
            (
                exploratory["provider"], exploratory["dispatch"],
                exploratory["model"], exploratory["effort"],
            ),
        )
        self.assertEqual("claude_agents_implementation_worker", exploratory["role"])
        self.assertEqual(
            "claude_agents_implementation_worker", exploratory["agent_type"]
        )
        self.assertEqual(
            ".claude/agents/claude_agents_implementation_worker.md",
            exploratory["agent_path"],
        )
        self.assertEqual("read-only", exploratory["sandbox_mode"])
        self.assertEqual(
            policy["provider_config_digest"], exploratory["provider_config_digest"]
        )

        bounded = route_task(self.task(), policy, primary_provider="claude")
        self.assertEqual(
            ("codex", "bridge", "gpt-6.1-sol", "medium"),
            (bounded["provider"], bounded["dispatch"], bounded["model"], bounded["effort"]),
        )
        self.assertEqual("codex_agents_implementation_worker", bounded["role"])

    def test_codex_primary_reaches_claude_only_through_the_bridge_flag(self) -> None:
        closed = route_task(self.exploratory(), self.provider_policy())
        self.assertEqual(
            ("codex", "native", "gpt-6.1-sol"),
            (closed["provider"], closed["dispatch"], closed["model"]),
        )
        bridged = route_task(self.exploratory(), self.provider_policy(bridge=True))
        self.assertEqual(
            ("claude", "bridge", "claude-opus-5-5", "medium"),
            (
                bridged["provider"], bridged["dispatch"],
                bridged["model"], bridged["effort"],
            ),
        )

    def test_project_model_allowlist_skips_candidates(self) -> None:
        policy = self.provider_policy(
            claude={"enabled": True, "models": ["claude-haiku-5-5"]}
        )
        route = route_task(self.task(), policy, primary_provider="claude")
        self.assertEqual("codex", route["provider"])
        policy = self.provider_policy(
            codex={"enabled": False},
            claude={"enabled": True, "models": ["claude-haiku-5-5", "claude-opus-5-5"]},
        )
        route = route_task(self.task(), policy, primary_provider="claude")
        self.assertEqual(
            ("claude", "claude-haiku-5-5", "high"),
            (route["provider"], route["model"], route["effort"]),
        )

    def test_provider_candidates_match_the_approved_routing_table(self) -> None:
        sol, opus = "gpt-6.1-sol", "claude-opus-5-5"
        haiku, sonnet = "claude-haiku-5-5", "claude-sonnet-5-5"

        def codex(model: str, effort: str) -> dict:
            return {"provider": "codex", "model": model, "effort": effort}

        def claude(model: str, effort: str) -> dict:
            return {"provider": "claude", "model": model, "effort": effort}

        cheap = [claude(haiku, "medium"), codex(sol, "low")]
        expected: dict[tuple[str, str | None], list[dict]] = {
            ("coding", "bounded"): [
                codex(sol, "medium"), claude(sonnet, "medium"), claude(haiku, "high")
            ],
            ("research", "bounded"): cheap,
            ("verification", "bounded"): [codex(sol, "high"), claude(opus, "high")],
            ("reconnaissance", None): cheap,
            ("hard_end_to_end", None): [
                claude(opus, "high"), codex("gpt-6-astra", "high")
            ],
        }
        for key in ("default", "exploratory", "judgment", "synthesis"):
            expected[("coding", key)] = [claude(opus, "medium"), codex(sol, "high")]
            expected[("research", key)] = [claude(opus, "high"), codex(sol, "high")]
        for key in ("bounded", "default", "exploratory", "judgment", "synthesis"):
            expected[("inventory", key)] = cheap
        table = self.policy["provider_candidates"]
        for (name, key), candidates in expected.items():
            with self.subTest(route=name, key=key):
                actual = table[name] if key is None else table[name][key]
                self.assertEqual(candidates, actual)
        # Rows the table already matched stay as they were.
        self.assertEqual(
            [{"provider": "codex", "model": "gpt-6-astra", "effort": "high"}, claude(opus, "high")],
            table["security"]["default"],
        )
        self.assertEqual(
            [{"provider": "codex", "route": "codex_policy"}, claude(opus, "high")],
            table["verification"]["default"],
        )
        self.assertEqual(
            [claude(opus, "medium"), {"provider": "codex", "route": "codex_policy"}],
            table["planning"]["synthesis"],
        )
        self.assertEqual(
            [{"provider": "codex", "route": "codex_policy"}, claude(opus, "medium")],
            table["planning"]["default"],
        )

    def test_no_eligible_candidate_denies_instead_of_falling_back(self) -> None:
        policy = self.provider_policy(
            codex={"enabled": False}, claude={"enabled": False}
        )
        for primary in ("codex", "claude"):
            with self.subTest(primary=primary), self.assertRaisesRegex(
                ContractError, "no provider candidate is eligible"
            ):
                route_task(self.task(), policy, primary_provider=primary)
        codex_only = self.provider_policy(
            codex={"enabled": False}, claude={"enabled": True}
        )
        with self.assertRaisesRegex(ContractError, "no provider candidate"):
            route_task(self.exploratory(), codex_only)
        with self.assertRaisesRegex(ContractError, "primary provider"):
            route_task(self.task(), self.policy, primary_provider="gemini")

    def test_verifier_prefers_the_other_provider(self) -> None:
        policy = self.provider_policy()
        claude_work = route_task(self.exploratory(), policy, primary_provider="claude")
        self.assertEqual(
            ("codex", "bridge", "gpt-6.1-sol", "high"),
            tuple(
                claude_work["verifier"][field]
                for field in ("provider", "dispatch", "model", "effort")
            ),
        )
        self.assertEqual(
            "codex_agents_verification_worker", claude_work["verifier"]["role"]
        )
        codex_work = route_task(self.task(), policy, primary_provider="claude")
        self.assertEqual(
            ("claude", "native", "claude-opus-5-5", "high"),
            tuple(
                codex_work["verifier"][field]
                for field in ("provider", "dispatch", "model", "effort")
            ),
        )
        self.assertEqual(
            ".claude/agents/claude_agents_verification_worker.md",
            codex_work["verifier"]["agent_path"],
        )
        # Without the bridge flag a Codex primary keeps its own verifier.
        native = route_task(self.task(), policy)
        self.assertEqual("codex", native["verifier"]["provider"])
        codex_disabled = self.provider_policy(codex={"enabled": False})
        same_vendor = route_task(
            self.exploratory(), codex_disabled, primary_provider="claude"
        )
        self.assertEqual("claude", same_vendor["verifier"]["provider"])

    def test_legacy_fallback_stays_on_codex_and_obeys_configuration(self) -> None:
        policy = self.provider_policy()
        with self.assertRaisesRegex(ContractError, "only to Codex routes"):
            _route_task_legacy_fallback(
                self.exploratory(), policy, primary_provider="claude"
            )
        restricted = self.provider_policy(
            codex={"enabled": True, "models": ["gpt-6.1-sol"]}
        )
        with self.assertRaisesRegex(ContractError, "legacy fallback is not allowed"):
            _route_task_legacy_fallback(self.exploratory(), restricted)
        fallback = _route_task_legacy_fallback(self.exploratory(), self.policy)
        self.assertEqual(("codex", "gpt-5.6-sol"), (fallback["provider"], fallback["model"]))

    def test_routing_is_deterministic(self) -> None:
        policy = self.provider_policy(bridge=True)
        for task in (self.task(), self.exploratory(), self.task(family="security")):
            with self.subTest(task=task):
                self.assertEqual(
                    route_task(task, policy, primary_provider="claude"),
                    route_task(deepcopy(task), policy, primary_provider="claude"),
                )

    def test_policy_rejects_invalid_provider_combinations(self) -> None:
        cases = {
            "does not offer": lambda policy: policy["provider_candidates"]["coding"][
                "default"
            ].insert(0, {"provider": "claude", "model": "gpt-6.1-sol", "effort": "high"}),
            "does not allow": lambda policy: policy["provider_candidates"]["inventory"][
                "default"
            ].insert(
                0, {"provider": "claude", "model": "claude-haiku-5-5", "effort": "xhigh"}
            ),
            "own role for every family": lambda policy: policy["providers"]["claude"][
                "roles"
            ].pop("coding"),
            "unknown provider": lambda policy: policy["provider_candidates"]["coding"][
                "default"
            ].insert(0, {"provider": "gemini", "model": "gemini-x", "effort": "high"}),
            "invalid candidate": lambda policy: policy["provider_candidates"]["coding"][
                "default"
            ].insert(0, {"provider": "codex", "route": "codex_policy", "extra": 1}),
            "unknown route key": lambda policy: policy["provider_candidates"][
                "planning"
            ].update({"bounded": [{"provider": "codex", "route": "codex_policy"}]}),
            "unknown keys": lambda policy: policy.update({"surprise": True}),
            "must be unique": lambda policy: policy["provider_candidates"]["security"][
                "default"
            ].append({"provider": "codex", "model": "gpt-6-astra", "effort": "high"}),
        }
        for message, mutate in cases.items():
            policy = deepcopy(self.policy)
            mutate(policy)
            with self.subTest(message=message), tempfile.TemporaryDirectory(
                dir=TEMP_ROOT
            ) as directory, self.assertRaisesRegex(PolicyError, message):
                load_policy(self.write_policy(directory, policy))

    def test_policy_validates_admission_settings(self) -> None:
        def versions(**changes):
            return lambda policy: policy["providers"]["codex"]["client_versions"].update(
                changes
            )

        cases = {
            "must contain exactly minimum": lambda policy: policy["providers"]["claude"][
                "client_versions"
            ].pop("maximum_exclusive"),
            "ascending semantic version range": versions(minimum="0.165.0"),
            "ascending semantic version range ": versions(maximum_exclusive="0.164"),
            " ascending semantic version range": versions(minimum="v0.164.0"),
            "ascending semantic version range  ": versions(minimum="0.164.0-alpha.1"),
            "ascending semantic version range   ": versions(minimum=164),
            "positive integer or null": lambda policy: policy["providers"]["codex"].update(
                max_admissions_per_window=0
            ),
            "positive integer or null ": lambda policy: policy["providers"]["claude"].update(
                max_admissions_per_window=True
            ),
            " positive integer or null": lambda policy: policy["providers"]["claude"].update(
                max_admissions_per_window="40"
            ),
            "providers.codex must contain exactly": lambda policy: policy["providers"][
                "codex"
            ].update(models={}),
            "providers.claude must contain exactly": lambda policy: policy["providers"][
                "claude"
            ].pop("client_versions"),
            "must describe every provider": lambda policy: policy["providers"].pop("codex"),
        }
        for message, mutate in cases.items():
            policy = deepcopy(self.policy)
            mutate(policy)
            with self.subTest(message=message), tempfile.TemporaryDirectory(
                dir=TEMP_ROOT
            ) as directory, self.assertRaisesRegex(PolicyError, message.strip()):
                load_policy(self.write_policy(directory, policy))
        uncapped = deepcopy(self.policy)
        uncapped["providers"]["codex"]["max_admissions_per_window"] = None
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            loaded = load_policy(self.write_policy(directory, uncapped))
        self.assertEqual(
            {
                "client_versions": {"minimum": "0.164.0", "maximum_exclusive": "0.165.0"},
                "max_admissions_per_window": None,
            },
            admission_settings(loaded, "codex"),
        )
        self.assertEqual(
            {
                "client_versions": {"minimum": "2.1.0", "maximum_exclusive": "2.2.0"},
                "max_admissions_per_window": 40,
            },
            admission_settings(self.policy, "claude"),
        )
        self.assertEqual(60, admission_settings(self.policy, "codex")["max_admissions_per_window"])

    def test_provider_configuration_is_closed_and_bounded(self) -> None:
        valid = {
            "version": 1,
            "providers": {
                "codex": {"enabled": True},
                "claude": {"enabled": True, "models": ["claude-opus-5-5"]},
            },
            "codex_primary_may_bridge_to_claude": False,
        }
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            self.assertEqual(
                valid,
                validate_provider_routing(
                    root, json.dumps(valid).encode(), self.policy
                ),
            )
            unknown_top = {**valid, "extra": True}
            unknown_provider = deepcopy(valid)
            unknown_provider["providers"]["claude"]["effort"] = "high"
            foreign_model = deepcopy(valid)
            foreign_model["providers"]["claude"]["models"] = ["gpt-6.1-sol"]
            for raw, message in (
                (json.dumps(unknown_top).encode(), "must contain exactly"),
                (json.dumps(unknown_provider).encode(), "for claude is invalid"),
                (json.dumps(foreign_model).encode(), "models for claude"),
                (
                    b'{"version":1,"version":1,"providers":{},'
                    b'"codex_primary_may_bridge_to_claude":false}',
                    "provider routing is invalid",
                ),
                (b" " * 65_537, "too large"),
            ):
                with self.subTest(message=message), self.assertRaisesRegex(
                    PolicyError, message
                ):
                    validate_provider_routing(root, raw, self.policy)

    def test_committed_provider_configuration_merges_with_digest(self) -> None:
        config = {
            "version": 1,
            "providers": {"codex": {"enabled": True}, "claude": {"enabled": True}},
            "codex_primary_may_bridge_to_claude": True,
        }
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            path = root / ".codex/orchestration/provider-routing.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(config))
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Routing Tests"],
                check=True,
            )
            arguments = (
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            with self.assertRaisesRegex(PolicyError, "not committed and clean"):
                load_effective_policy(*arguments)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "provider config"],
                check=True,
            )
            policy = load_effective_policy(*arguments)
            self.assertEqual(content_digest(config), policy["provider_config_digest"])
            route = route_task(self.exploratory(), policy)
            self.assertEqual(("claude", "bridge"), (route["provider"], route["dispatch"]))

            path.unlink()
            target = root / "provider-routing.json"
            target.write_text(json.dumps(config))
            path.symlink_to(target)
            with self.assertRaisesRegex(PolicyError, "not a regular file"):
                load_effective_policy(*arguments)

    def git_commit(self, root: Path, message: str) -> None:
        if not (root / ".git").exists():
            subprocess.run(["git", "init", "-q", str(root)], check=True)
        # CI runners have no global identity, so set it even when the test created the repo.
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Routing Tests"],
            check=True,
        )
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", message], check=True)

    def test_deleted_committed_provider_configuration_fails_closed(self) -> None:
        config = {
            "version": 1,
            "providers": {"codex": {"enabled": False}, "claude": {"enabled": True}},
            "codex_primary_may_bridge_to_claude": False,
        }
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            path = root / ".codex/orchestration/provider-routing.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(config))
            self.git_commit(root, "provider config")
            arguments = (
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            with self.assertRaisesRegex(ContractError, "no provider candidate"):
                route_task(self.exploratory(), load_effective_policy(*arguments))
            path.unlink()
            with self.assertRaisesRegex(ContractError, "committed but missing"):
                load_effective_policy(*arguments)
            # A committed deletion is a deliberate return to the defaults.
            self.git_commit(root, "remove provider config")
            policy = load_effective_policy(*arguments)
            self.assertNotIn("provider_routing", policy)

    def test_verification_task_matches_the_advertised_verifier(self) -> None:
        cases = (
            (self.provider_policy(), "claude"),
            (self.provider_policy(bridge=True), "codex"),
            (self.provider_policy(codex={"enabled": False}), "claude"),
        )
        for policy, primary in cases:
            for worker_task in (self.task(), self.exploratory()):
                try:
                    worker = route_task(worker_task, policy, primary_provider=primary)
                except ContractError:
                    continue
                with self.subTest(primary=primary, worker=worker["provider"]):
                    verification = route_task(
                        self.task(
                            id="verify-1",
                            family="verification",
                            operation="review",
                            acceptance_strength="independent_judgment",
                            deterministic_validation=False,
                            verification_for_family="coding",
                            verification_for_provider=worker["provider"],
                        ),
                        policy,
                        primary_provider=primary,
                    )
                    advertised = worker["verifier"]
                    self.assertEqual(
                        (advertised["provider"], advertised["model"],
                         advertised["effort"], advertised["dispatch"]),
                        (verification["provider"], verification["model"],
                         verification["effort"], verification["dispatch"]),
                    )
                    self.assertEqual(advertised["role"], verification["role"])
        # Both directions are covered: Claude work gets Codex, Codex work gets Claude.
        claude_primary = self.provider_policy()
        self.assertEqual(
            "codex",
            route_task(self.exploratory(), claude_primary, primary_provider="claude")[
                "verifier"
            ]["provider"],
        )
        self.assertEqual(
            "claude",
            route_task(self.task(), claude_primary, primary_provider="claude")[
                "verifier"
            ]["provider"],
        )

    def verification_task(self, provider: str) -> dict:
        return self.task(
            id="verify-1",
            family="verification",
            operation="review",
            acceptance_strength="independent_judgment",
            deterministic_validation=False,
            verification_for_family="coding",
            verification_for_provider=provider,
        )

    def test_verification_task_matches_the_advertised_verifier_under_fallback(
        self,
    ) -> None:
        worker = _route_task_legacy_fallback(self.exploratory(), self.policy)
        verification = _route_task_legacy_fallback(
            self.verification_task(worker["provider"]), self.policy
        )
        advertised = worker["verifier"]
        self.assertEqual(("codex", "gpt-5.6-sol", "high"), (
            advertised["provider"], advertised["model"], advertised["effort"]
        ))
        self.assertEqual(
            (advertised["provider"], advertised["model"], advertised["effort"]),
            (verification["provider"], verification["model"], verification["effort"]),
        )
        self.assertEqual(
            {"model": "gpt-6.1-sol", "effort": "high"},
            verification["fallback"]["from"],
        )
        # A Claude verifier is unaffected by a Codex host fallback, so the
        # verification task needs no fallback of its own to agree with it.
        bridged = self.provider_policy(bridge=True)
        worker = _route_task_legacy_fallback(self.task(), bridged)
        self.assertEqual("claude", worker["verifier"]["provider"])
        verification = route_task(self.verification_task("codex"), bridged)
        self.assertEqual(
            (worker["verifier"]["provider"], worker["verifier"]["model"]),
            (verification["provider"], verification["model"]),
        )

    def test_verification_for_provider_must_be_a_string(self) -> None:
        for value in ([], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                ContractError, "verification_for_provider is unsupported"
            ):
                route_task(self.verification_task(value), self.policy)

    def test_provider_trust_classifies_git_outcomes(self) -> None:
        arguments = (
            ORCHESTRATION_ROOT / "config/routing-policy.json",
            ORCHESTRATION_ROOT / "config/discovery-policy.json",
        )
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ContractError, "trust is unavailable"):
                load_effective_policy(root, *arguments)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            self.assertNotIn(
                "provider_routing", load_effective_policy(root, *arguments)
            )
            (root / "README").write_text("project\n")
            self.git_commit(root, "first commit")
            self.assertNotIn(
                "provider_routing", load_effective_policy(root, *arguments)
            )
            original_run = subprocess.run

            def failing(status: int):
                def run(arguments: list[str], **kwargs: object):
                    if "ls-tree" in arguments:
                        return subprocess.CompletedProcess(arguments, status, "", "")
                    return original_run(arguments, **kwargs)
                return run

            for side_effect in (
                failing(128),
                failing(-9),
                OSError("git unavailable"),
                subprocess.TimeoutExpired("git", 10),
            ):
                with self.subTest(side_effect=side_effect), patch(
                    "codex_agents.routing.subprocess.run", side_effect=side_effect
                ), self.assertRaisesRegex(ContractError, "trust is unavailable"):
                    load_effective_policy(root, *arguments)
            with patch(
                "codex_agents.routing.subprocess.run",
                return_value=subprocess.CompletedProcess([], 128, "", ""),
            ), self.assertRaisesRegex(ContractError, "trust is unavailable"):
                load_effective_policy(root, *arguments)

    def test_verification_task_without_provider_uses_the_verification_table(self) -> None:
        verification = self.task(
            family="verification",
            operation="review",
            acceptance_strength="independent_judgment",
            deterministic_validation=False,
        )
        self.assertEqual("codex", route_task(verification, self.policy)["provider"])
        route = route_task(
            verification,
            self.provider_policy(codex={"enabled": False}),
            primary_provider="claude",
        )
        self.assertEqual(
            ("claude", "claude-opus-5-5", "high", "claude_agents_verification_worker"),
            (route["provider"], route["model"], route["effort"], route["role"]),
        )
        with self.assertRaisesRegex(ContractError, "only for verification tasks"):
            route_task(self.task(verification_for_provider="codex"), self.policy)
        with self.assertRaisesRegex(ContractError, "verification_for_provider"):
            route_task({**verification, "verification_for_provider": "gemini"}, self.policy)

    def test_provider_configuration_type_checks_before_hashing(self) -> None:
        config = {
            "version": 1,
            "providers": {
                "codex": {"enabled": True, "models": [{}]},
                "claude": {"enabled": False},
            },
            "codex_primary_may_bridge_to_claude": False,
        }
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            with self.assertRaisesRegex(PolicyError, "models for codex"):
                validate_provider_routing(
                    Path(directory), json.dumps(config).encode(), self.policy
                )
        policy = deepcopy(self.policy)
        policy["providers"]["claude"]["models"]["claude-opus-5-5"] = [{}]
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            with self.assertRaisesRegex(PolicyError, "own models to efforts"):
                load_policy(self.write_policy(directory, policy))

    def test_mapped_claude_agent_needs_the_installed_front_matter_form(self) -> None:
        for front_matter in (
            'name: "project_reader"\ndescription: Reads\ntools: Read, Grep\n',
            "name: project_reader\ndescription: Reads\ntools: [Read, Grep]\n",
        ):
            with self.subTest(front_matter=front_matter), tempfile.TemporaryDirectory(
                dir=TEMP_ROOT
            ) as directory:
                root = Path(directory)
                agents = root / ".claude/agents"
                orchestration = root / ".codex/orchestration"
                agents.mkdir(parents=True)
                orchestration.mkdir(parents=True)
                (agents / "project_reader.md").write_text(
                    "---\n" + front_matter + "---\n\nRead the question.\n"
                )
                (orchestration / "project-agent-routing.json").write_text(
                    json.dumps({"version": 1, "roles": {"research": "project_reader"}})
                )
                self.git_commit(root, "claude agent")
                with self.assertRaisesRegex(PolicyError, "not executable"):
                    load_effective_policy(
                        root,
                        ORCHESTRATION_ROOT / "config/routing-policy.json",
                        ORCHESTRATION_ROOT / "config/discovery-policy.json",
                    )

    def test_mapped_claude_agent_with_write_tools_is_ineligible(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            agents = root / ".claude/agents"
            orchestration = root / ".codex/orchestration"
            agents.mkdir(parents=True)
            orchestration.mkdir(parents=True)
            (agents / "project_reader.md").write_text(
                "---\nname: project_reader\ndescription: Reads the project\n"
                "tools: Read, Grep\n---\n\nRead the bounded question.\n"
            )
            (agents / "project_writer.md").write_text(
                "---\nname: project_writer\ndescription: Edits the project\n"
                "tools: Read, Edit\n---\n\nEdit the bounded change.\n"
            )
            (orchestration / "project-agent-routing.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {
                            "research": "project_reader",
                            "coding": "project_writer",
                        },
                    }
                )
            )
            (orchestration / "provider-routing.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "providers": {
                            "codex": {"enabled": True},
                            "claude": {"enabled": True},
                        },
                        "codex_primary_may_bridge_to_claude": False,
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
                ["git", "-C", str(root), "commit", "-qm", "claude agents"],
                check=True,
            )
            policy = load_effective_policy(
                root,
                ORCHESTRATION_ROOT / "config/routing-policy.json",
                ORCHESTRATION_ROOT / "config/discovery-policy.json",
            )
            research = route_task(
                self.exploratory(family="research"), policy, primary_provider="claude"
            )
            self.assertEqual(
                ("claude", "claude_agents_researcher", "project_reader"),
                (research["provider"], research["role"], research["agent_type"]),
            )
            self.assertEqual(".claude/agents/project_reader.md", research["agent_path"])
            coding = route_task(self.exploratory(), policy, primary_provider="claude")
            self.assertEqual("codex", coding["provider"])
            self.assertEqual("codex_agents_implementation_worker", coding["agent_type"])


if __name__ == "__main__":
    unittest.main()
