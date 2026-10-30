"""Package-level validation that performs no model, network, or external calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tomllib

from .catalog import CatalogError, discover, load_discovery_policy
from .contracts import ContractError
from .learning_adapters import load_client_capabilities
from .learning_contracts import (
    CANARY_REPORT_FIELDS,
    CANDIDATE_FIELDS,
    CASE_FIELDS,
    COMPARISON_FIELDS,
    EVALUATION_ASSIGNMENT_FIELDS,
    EVENT_FIELDS,
    EVIDENCE_REVIEW_FIELDS,
    EXPORT_FIELDS,
    FEEDBACK_FIELDS,
    GRADER_CALIBRATION_FIELDS,
    GRANT_FIELDS,
    HOST_OBSERVATION_FIELDS,
    OUTCOME_FIELDS,
    PROMOTION_FIELDS,
    QA_EVIDENCE_FIELDS,
    TRIAL_FIELDS,
    load_released_candidate_digests,
)
from .learning_engine import (
    load_operational_check_configuration,
    operational_sandbox_preflight,
)
from .learning_store import PRODUCER_AUTHORITY
from .routing import PolicyError, load_effective_policy, load_policy, route_task

EXPECTED_ROLE_MAP = {
    "inventory": "codex_agents_researcher",
    "research": "codex_agents_researcher",
    "planning": "codex_agents_planner",
    "coding": "codex_agents_implementation_worker",
    "security": "codex_agents_security_reviewer",
    "verification": "codex_agents_verification_worker",
}
EXPECTED_CLAUDE_ROLE_MAP = {
    "inventory": "claude_agents_researcher",
    "research": "claude_agents_researcher",
    "planning": "claude_agents_planner",
    "coding": "claude_agents_implementation_worker",
    "security": "claude_agents_security_reviewer",
    "verification": "claude_agents_verification_worker",
}
EXPECTED_AGENTS = set(EXPECTED_ROLE_MAP.values())
EXPECTED_VERIFICATION_ROLE = "codex_agents_verification_worker"
RESULT_TEMPLATE_MARKER = '{"assignment_id":"<assignment_id>"'
EXPECTED_SKILLS = {
    "orchestrate-agents",
    "research-evidence",
    "plan-change",
    "implement-change",
    "review-security",
    "verify-result",
}
EXPECTED_EVALUATION_FAMILIES = {
    "research",
    "planning",
    "coding",
    "security",
    "verification",
}
EXPECTED_HOOK_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "Interrupt",
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
}
EXPECTED_HOOK_COMMAND = (
    'python3 "$(git rev-parse --show-toplevel)/.codex/orchestration/learning_hook.py" '
    "--client-version codex-hooks-v1"
)
ASYNC_HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "SubagentStart",
    "PreToolUse",
    "PostToolUse",
}


def _validate_evaluation(
    root: Path, policy: dict[str, Any], errors: list[str]
) -> None:
    if policy.get("status") != "accepted":
        errors.append("routing policy must be accepted before package verification")
        return
    metadata = policy.get("evaluation")
    if not isinstance(metadata, dict):
        errors.append("accepted routing policy requires evaluation metadata")
        return
    relative = metadata.get("evidence")
    if not isinstance(relative, str):
        errors.append("evaluation evidence path is required")
        return
    path = root / ".codex" / "orchestration" / relative
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load evaluation evidence: {exc}")
        return
    if report.get("ready") is not True:
        errors.append("evaluation evidence is not ready")
    if report.get("record_count") != metadata.get("record_count"):
        errors.append("evaluation record count does not match policy metadata")
    families = report.get("families", {})
    if set(families) != EXPECTED_EVALUATION_FAMILIES:
        errors.append("evaluation evidence family set is incomplete")
    for family in EXPECTED_EVALUATION_FAMILIES:
        if not families.get(family, {}).get("ready"):
            errors.append(f"evaluation family is not ready: {family}")


def _validate_skills(root: Path, errors: list[str]) -> None:
    skill_root = root / ".agents" / "skills"
    found: set[str] = set()
    for skill_file in sorted(skill_root.glob("*/SKILL.md")):
        text = skill_file.read_text()
        relative = skill_file.relative_to(root)
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            errors.append(f"{relative}: invalid YAML frontmatter boundary")
            continue
        header = text.split("\n---\n", 1)[0].removeprefix("---\n")
        values: dict[str, str] = {}
        for line in header.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip().strip("'\"")
        name = values.get("name", "")
        description = values.get("description", "")
        found.add(name)
        if name != skill_file.parent.name:
            errors.append(f"{relative}: skill name must match folder")
        if not description:
            errors.append(f"{relative}: description is required")
        if "[TODO:" in text:
            errors.append(f"{relative}: unfinished TODO placeholder")
        metadata = skill_file.parent / "agents" / "openai.yaml"
        if name in EXPECTED_SKILLS and not metadata.exists():
            errors.append(f"{relative}: agents/openai.yaml is required for this package")
    missing = EXPECTED_SKILLS - found
    if missing:
        errors.append(
            "missing package skills: " + ", ".join(sorted(missing))
        )


def _validate_agents(root: Path, policy: dict[str, Any], errors: list[str]) -> None:
    found: set[str] = set()
    for agent_file in sorted((root / ".codex" / "agents").glob("*.toml")):
        relative = agent_file.relative_to(root)
        try:
            data = tomllib.loads(agent_file.read_text())
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"{relative}: invalid TOML: {exc}")
            continue
        required = {"name", "description", "developer_instructions", "sandbox_mode"}
        missing = sorted(required - data.keys())
        if missing:
            errors.append(f"{relative}: missing {', '.join(missing)}")
            continue
        found.add(data["name"])
        package_agent = data["name"] in EXPECTED_AGENTS
        if package_agent and data["name"] != agent_file.stem:
            errors.append(f"{relative}: name must match file stem")
        if package_agent and ("model" in data or "model_reasoning_effort" in data):
            errors.append(f"{relative}: role files must not override routed model or effort")
        if data["sandbox_mode"] not in {"read-only", "workspace-write"}:
            errors.append(f"{relative}: unsupported sandbox_mode")
        expected_sandbox = "read-only"
        if package_agent and data["sandbox_mode"] != expected_sandbox:
            errors.append(
                f"{relative}: {data['name']} must use {expected_sandbox} sandbox_mode"
            )
        if package_agent and RESULT_TEMPLATE_MARKER not in data["developer_instructions"]:
            errors.append(f"{relative}: packaged result template is required")
    missing = EXPECTED_AGENTS - found
    if missing:
        errors.append(
            "missing package agents: " + ", ".join(sorted(missing))
        )
    verification = policy["verification"]
    if verification["role"] not in found:
        errors.append("verification role does not exist")
    if policy["roles"] != EXPECTED_ROLE_MAP:
        errors.append("routing policy role mapping must match package policy")
    if policy.get("providers", {}).get("claude", {}).get("roles") != (
        EXPECTED_CLAUDE_ROLE_MAP
    ):
        errors.append("routing policy Claude role mapping must match package policy")
    if verification["role"] != EXPECTED_VERIFICATION_ROLE:
        errors.append("verification role must match package verifier")


def _validate_schemas(root: Path, errors: list[str]) -> None:
    schema_root = root / ".codex" / "orchestration" / "schemas"
    expected = {
        "task.schema.json",
        "assignment.schema.json",
        "result.schema.json",
        "evaluation-record.schema.json",
        "agent-state.schema.json",
        "parent-grant.schema.json",
        "learning-event.schema.json",
        "learning-host-observation.schema.json",
        "learning-outcome.schema.json",
        "learning-feedback.schema.json",
        "learning-evaluation-case.schema.json",
        "learning-evaluation-assignment.schema.json",
        "learning-evidence-review.schema.json",
        "learning-qa-evidence.schema.json",
        "learning-evaluation-trial.schema.json",
        "learning-candidate.schema.json",
        "learning-comparison.schema.json",
        "learning-canary-report.schema.json",
        "learning-grader-calibration.schema.json",
        "learning-owner-grant.schema.json",
        "learning-promotion.schema.json",
        "learning-export.schema.json",
        "project-agent-routing.schema.json",
        "operational-checks.schema.json",
    }
    found = {path.name for path in schema_root.glob("*.json")}
    if found != expected:
        errors.append("schema set mismatch")
    learning_fields = {
        "learning-event.schema.json": EVENT_FIELDS,
        "learning-host-observation.schema.json": HOST_OBSERVATION_FIELDS,
        "learning-outcome.schema.json": OUTCOME_FIELDS,
        "learning-feedback.schema.json": FEEDBACK_FIELDS,
        "learning-evaluation-case.schema.json": CASE_FIELDS,
        "learning-evaluation-assignment.schema.json": EVALUATION_ASSIGNMENT_FIELDS,
        "learning-evidence-review.schema.json": EVIDENCE_REVIEW_FIELDS,
        "learning-qa-evidence.schema.json": QA_EVIDENCE_FIELDS,
        "learning-evaluation-trial.schema.json": TRIAL_FIELDS,
        "learning-candidate.schema.json": CANDIDATE_FIELDS,
        "learning-comparison.schema.json": COMPARISON_FIELDS,
        "learning-canary-report.schema.json": CANARY_REPORT_FIELDS,
        "learning-grader-calibration.schema.json": GRADER_CALIBRATION_FIELDS,
        "learning-owner-grant.schema.json": GRANT_FIELDS,
        "learning-promotion.schema.json": PROMOTION_FIELDS,
        "learning-export.schema.json": EXPORT_FIELDS,
    }
    for path in sorted(schema_root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{path.relative_to(root)}: invalid JSON: {exc}")
            continue
        if data.get("type") != "object" or data.get("additionalProperties") is not False:
            errors.append(f"{path.relative_to(root)}: schema must be a closed object")
        expected_fields = learning_fields.get(path.name)
        required_fields = set(data.get("required", []))
        if path.name == "learning-host-observation.schema.json":
            required_match = not required_fields
        elif path.name == "learning-outcome.schema.json":
            required_match = required_fields == expected_fields - {
                "execution_state", "semantic_verdict", "admission_state",
                "retry_attempt", "retry_of_assignment_id", "usage_sources",
            }
        elif path.name == "learning-candidate.schema.json":
            required_match = required_fields == expected_fields - {
                "eligibility_receipt"
            }
        elif path.name == "learning-qa-evidence.schema.json":
            required_match = required_fields == expected_fields - {
                "execution_state", "semantic_verdict"
            }
        else:
            required_match = required_fields == expected_fields
        if expected_fields is not None and (
            not required_match or set(data.get("properties", {})) != expected_fields
        ):
            errors.append(f"{path.relative_to(root)}: schema fields differ from runtime contract")
        if path.name == "task.schema.json":
            event = (
                data.get("properties", {})
                .get("failure_events", {})
                .get("items", {})
            )
            conditionals = data.get("allOf")
            try:
                verifier_reference_pattern = event["allOf"][0]["then"][
                    "properties"
                ]["evidence_refs"]["items"]["pattern"]
            except (KeyError, TypeError, IndexError):
                verifier_reference_pattern = None
            attempts = []
            if isinstance(conditionals, list):
                for conditional in conditionals:
                    try:
                        attempts.append(
                            conditional["if"]["properties"]["failed_attempts"]["const"]
                        )
                    except (KeyError, TypeError):
                        continue
            if (
                set(event.get("required", []))
                != {"attempt", "reason", "evidence_refs"}
                or attempts != [0, 1, 2]
                or event.get("properties", {})
                .get("attempt", {})
                .get("maximum") != 2
                or verifier_reference_pattern != "^verification:[0-9a-f]{64}$"
            ):
                errors.append(
                    f"{path.relative_to(root)}: failure history schema lacks runtime semantic parity"
                )
        if path.name == "learning-host-observation.schema.json":
            alternatives = data.get("oneOf")
            try:
                sequences = [
                    [
                        item["properties"]["status"]["const"]
                        for item in alternative["properties"]["result_attempts"]["prefixItems"]
                    ]
                    for alternative in alternatives
                ]
                receipt_pattern = alternatives[1]["properties"]["result_attempts"][
                    "prefixItems"
                ][0]["properties"]["receipt_id"]["pattern"]
            except (KeyError, TypeError, IndexError):
                sequences = []
                receipt_pattern = None
            if sequences != [["accepted"], ["rejected", "accepted"]] or (
                receipt_pattern != "^rpr_[0-9a-f]{48}$"
            ):
                errors.append(
                    f"{path.relative_to(root)}: result-attempt schema lacks runtime semantic parity"
                )
        if path.name == "learning-outcome.schema.json":
            alternatives = data.get("oneOf")
            try:
                repair_counts = [
                    alternative["properties"]["repairs"]["const"]
                    for alternative in alternatives
                ]
                reason_limits = [
                    alternative["properties"]["repair_reason_codes"]
                    for alternative in alternatives
                ]
            except (KeyError, TypeError):
                repair_counts = []
                reason_limits = []
            if (
                repair_counts != [0, 1]
                or reason_limits != [
                    {"maxItems": 0},
                    {"minItems": 1, "maxItems": 1},
                ]
            ):
                errors.append(
                    f"{path.relative_to(root)}: repair schema lacks runtime semantic parity"
                )
        if path.name == "learning-candidate.schema.json":
            definitions = data.get("$defs")
            conditionals = data.get("allOf")
            item_conditionals = (
                data.get("properties", {})
                .get("change_manifest", {})
                .get("items", {})
                .get("allOf")
            )
            if (
                not isinstance(definitions, dict)
                or set(definitions)
                != {
                    "identifier", "digest", "modelEffort", "digestArray",
                    "identifierArray", "bounds", "executionConfiguration",
                }
                or not isinstance(conditionals, list)
                or len(conditionals) != 8
                or not isinstance(item_conditionals, list)
                or len(item_conditionals) != 7
                or data.get("x-runtime-invariants")
                != [
                    "digests match canonical values",
                    "verification checks only grow",
                    "bounds only narrow",
                    "candidate and base digests match exact configurations",
                ]
            ):
                errors.append(
                    f"{path.relative_to(root)}: candidate schema lacks runtime semantic parity"
                )


def _validate_learning_producers(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "orchestration" / "config" / "learning-producers.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load learning producer matrix: {exc}")
        return
    producers = data.get("producers") if isinstance(data, dict) else None
    normalized = (
        {key: sorted(value) for key, value in producers.items()}
        if isinstance(producers, dict)
        and all(isinstance(value, list) for value in producers.values())
        else None
    )
    expected = {key: sorted(value) for key, value in PRODUCER_AUTHORITY.items()}
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or set(data) != {"version", "producers"}
        or normalized != expected
    ):
        errors.append("learning producer matrix does not match enforced authority")


def _validate_learning_clients(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "orchestration" / "config" / "learning-clients.json"
    try:
        clients = load_client_capabilities(path)
    except ContractError as exc:
        errors.append(str(exc))
        return
    cli = clients.get("codex-cli-0.153.0", {})
    vscode = clients.get("codex-vscode-0.153.0", {})
    hooks = clients.get("codex-hooks-v1", {})
    host = clients.get("codex-host-v1", {})
    if any(version.startswith("codex-eval-fixture-") for version in clients):
        errors.append("test fixture clients must not ship in the runtime capability matrix")
    if cli.get("app_server_coverage") != "root_turn_complete":
        errors.append("validated CLI learning coverage must remain root-turn only")
    if vscode.get("app_server_coverage") != "handshake_only":
        errors.append("VS Code learning coverage exceeds verified evidence")
    if hooks.get("producers") != ["hook"] or any(
        hooks.get(field) is not expected
        for field, expected in {
            "root_lifecycle": True,
            "child_lifecycle": True,
            "tool_lifecycle": True,
            "root_usage": False,
            "child_usage": False,
        }.items()
    ):
        errors.append("hook surface identity or coverage is invalid")
    if host.get("producers") != ["host"]:
        errors.append("trusted host identity is not producer restricted")
    live_clients = {
        version: item
        for version, item in clients.items()
        if not version.startswith("codex-eval-fixture-")
    }
    if any(item.get("child_usage") for item in live_clients.values()):
        errors.append("no current client has verified child-usage coverage")


def _validate_learning_releases(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "orchestration" / "config" / "learning-releases.json"
    try:
        load_released_candidate_digests(
            path, require_signature_verification=False
        )
    except ContractError as exc:
        errors.append(str(exc))


def _validate_learning_hooks(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "hooks.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load learning hook configuration: {exc}")
        return
    if not isinstance(data, dict) or set(data) != {"description", "hooks"}:
        errors.append("learning hook configuration is not a closed object")
        return
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or set(hooks) != EXPECTED_HOOK_EVENTS:
        errors.append("learning hook event set is incomplete or over-broad")
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list) or len(groups) != 1:
            errors.append(f"learning hook must have one group: {event}")
            continue
        group = groups[0]
        commands = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(commands, list) or len(commands) != 1:
            errors.append(f"learning hook must have one command: {event}")
            continue
        command = commands[0]
        expected_fields = {"type", "command", "timeout"}
        if event in ASYNC_HOOK_EVENTS:
            expected_fields.add("async")
        if not isinstance(command, dict) or set(command) != expected_fields:
            errors.append(f"learning hook command fields are invalid: {event}")
            continue
        if command.get("type") != "command" or command.get("timeout") != 3:
            errors.append(f"learning hook bounds are invalid: {event}")
        if command.get("command") != EXPECTED_HOOK_COMMAND:
            errors.append(f"learning hook command target is invalid: {event}")
        if event in ASYNC_HOOK_EVENTS and command.get("async") is not True:
            errors.append(f"learning hook must be asynchronous: {event}")


def _validate_learning_overhead(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "orchestration" / "config" / "learning-overhead.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load learning overhead limits: {exc}")
        return
    expected_fields = {
        "normalize",
        "ledger_commit",
        "collector_round_trip",
        "cold_hook_process",
    }
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "client", "derivation", "baseline_p95_ms", "p95_ms"}
        or data.get("version") != 1
        or data.get("client") != "codex-hooks-v1"
        or not isinstance(data.get("derivation"), str)
    ):
        errors.append("learning overhead configuration is invalid")
        return
    for section in ("baseline_p95_ms", "p95_ms"):
        values = data.get(section)
        if not isinstance(values, dict) or set(values) != expected_fields or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
            for value in values.values()
        ):
            errors.append(f"learning overhead {section} is invalid")


def validate_package(root: Path) -> dict[str, Any]:
    root = root.resolve()
    package_errors: list[str] = []
    project_errors: list[str] = []
    warnings: list[str] = []
    orchestration = root / ".codex" / "orchestration"
    try:
        policy = load_policy(orchestration / "config" / "routing-policy.json")
    except PolicyError as exc:
        return {
            "ready": False,
            "package_ready": False,
            "project_ready": False,
            "errors": [str(exc)],
            "package_errors": [str(exc)],
            "project_errors": [],
            "warnings": [],
        }
    _validate_agents(root, policy, package_errors)
    _validate_skills(root, package_errors)
    _validate_schemas(root, package_errors)
    _validate_learning_producers(root, package_errors)
    _validate_learning_clients(root, package_errors)
    _validate_learning_releases(root, package_errors)
    _validate_learning_hooks(root, package_errors)
    _validate_learning_overhead(root, package_errors)
    _validate_evaluation(root, policy, package_errors)

    mapping_path = orchestration / "project-agent-routing.json"
    if mapping_path.is_symlink():
        project_errors.append("project agent routing configuration is not a regular file")
    elif not mapping_path.exists():
        project_errors.append(
            "project agent routing configuration is unavailable; provide "
            ".codex/orchestration/project-agent-routing.json"
        )
    operational_checks = orchestration / "operational-checks.json"
    if not operational_checks.exists() and not operational_checks.is_symlink():
        project_errors.append(
            "operational check configuration is unavailable; provide and commit "
            ".codex/orchestration/operational-checks.json"
        )
    else:
        try:
            load_operational_check_configuration(root)
            if not operational_sandbox_preflight():
                project_errors.append(
                    "operational check sandbox preflight failed; validate from an "
                    "owner shell or approved unsandboxed command boundary"
                )
        except ContractError as exc:
            project_errors.append(str(exc))
    effective_policy = policy
    try:
        discovery_policy = load_discovery_policy(
            orchestration / "config" / "discovery-policy.json"
        )
        inventory = discover(root, discovery_policy)
        for item in inventory["errors"]:
            project_errors.extend(
                f"{item['key']}: {message}" for message in item["messages"]
            )
        if inventory["conflicts"]:
            project_errors.append(
                f"unresolved discovery conflicts: {inventory['conflicts']}"
            )
        for item in inventory["candidates"]:
            warnings.extend(
                f"{item['kind']}:{item['name']}: {message}" for message in item["warnings"]
            )
        effective_policy = load_effective_policy(
            root,
            orchestration / "config" / "routing-policy.json",
            orchestration / "config" / "discovery-policy.json",
        )
    except CatalogError as exc:
        project_errors.append(str(exc))
    except (ContractError, PolicyError) as exc:
        project_errors.append(str(exc))

    smoke_tasks = [
        {
            "id": "smoke-luna",
            "objective": "inventory files",
            "family": "inventory",
            "complexity": "low",
            "risk": "low",
            "operation": "mechanical",
            "ambiguity": "known",
            "context_breadth": "narrow",
            "acceptance_strength": "deterministic",
            "routing_evidence_refs": [],
            "meaningful": True,
            "deterministic_validation": True,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
        },
        {
            "id": "smoke-risk-separation",
            "objective": "plan one critical cross-system change",
            "family": "planning",
            "complexity": "high",
            "risk": "critical",
            "operation": "synthesis",
            "ambiguity": "high",
            "context_breadth": "cross_system",
            "acceptance_strength": "independent_judgment",
            "routing_evidence_refs": [],
            "meaningful": True,
            "deterministic_validation": False,
            "independent_parts": 2,
            "parallel_safe": True,
            "failed_attempts": 0,
            "failure_events": [],
        },
        {
            "id": "smoke-astra",
            "objective": "resolve an evidenced hard end-to-end failure",
            "family": "coding",
            "complexity": "extreme",
            "risk": "medium",
            "operation": "end_to_end",
            "ambiguity": "high",
            "context_breadth": "cross_system",
            "acceptance_strength": "independent_judgment",
            "routing_evidence_refs": ["outcome:" + "a" * 64],
            "meaningful": True,
            "deterministic_validation": False,
            "independent_parts": 1,
            "parallel_safe": False,
            "failed_attempts": 0,
            "failure_events": [],
        },
    ]
    # The smoke routes check the package policy, so a project that enables
    # another provider does not change what they expect.
    smoke_policy = {
        key: value
        for key, value in effective_policy.items()
        if key not in {"provider_routing", "provider_config_digest"}
    }
    try:
        routes = [
            route_task(smoke_tasks[0], smoke_policy),
            route_task(smoke_tasks[1], smoke_policy),
            route_task(
                smoke_tasks[2],
                smoke_policy,
                trusted_routing_evidence_refs=frozenset(
                    smoke_tasks[2]["routing_evidence_refs"]
                ),
            ),
        ]
        if (routes[0]["model"], routes[0]["effort"]) != ("gpt-6-luna", "high"):
            package_errors.append("bounded inventory smoke task did not prefer Luna/high")
        if (routes[1]["model"], routes[1]["effort"]) != ("gpt-6.1-sol", "high"):
            package_errors.append("critical risk changed the evidence-based planning route")
        if routes[1]["topology"] != "parallel" or not routes[1]["verification_required"]:
            package_errors.append("critical risk changed topology or bypassed verification")
        if (routes[2]["model"], routes[2]["effort"]) != ("gpt-6-astra", "medium"):
            package_errors.append("evidenced hard end-to-end task did not route to Astra/medium")
        if any(
            (route["provider"], route["dispatch"]) != ("codex", "native")
            or (route["verifier"] or {}).get("provider", "codex") != "codex"
            for route in routes
        ):
            package_errors.append("default routing selected a provider other than Codex")
    except (PolicyError, ValueError) as exc:
        package_errors.append(f"routing smoke test failed: {exc}")
    package_ready = not package_errors
    project_ready = not project_errors
    return {
        "ready": package_ready and project_ready,
        "package_ready": package_ready,
        "project_ready": project_ready,
        "errors": [*package_errors, *project_errors],
        "package_errors": package_errors,
        "project_errors": project_errors,
        "warnings": sorted(set(warnings)),
    }
