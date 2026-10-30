"""Deterministic task-to-role, model, effort, and topology routing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib

from .catalog import discover, load_discovery_policy
from .contracts import ContractError, validate_task
from .learning_contracts import parse_bounded_json_value


class PolicyError(ValueError):
    """Raised when routing policy is internally inconsistent."""


BOUND_FIELDS = {
    "max_attempts",
    "max_steers_per_attempt",
    "max_no_progress_events",
    "wait_seconds",
    "timeout_seconds",
    "max_tool_calls",
}

PROJECT_AGENT_MAPPING = Path(".codex/orchestration/project-agent-routing.json")
WRITABLE_PROJECT_FAMILIES = {"coding", "verification"}


def _read_project_regular(root: Path, relative: str, *, max_bytes: int) -> bytes:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise PolicyError("project definition path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        result = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
        )
    except OSError as exc:
        raise PolicyError("project definition is unavailable") from exc
    finally:
        os.close(descriptor)
    try:
        metadata = os.fstat(result)
        if not stat.S_ISREG(metadata.st_mode):
            raise PolicyError("project definition is not a regular file")
        chunks: list[bytes] = []
        byte_count = 0
        while chunk := os.read(result, min(65_536, max_bytes + 1 - byte_count)):
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise PolicyError("project definition is too large")
        return b"".join(chunks)
    finally:
        os.close(result)


def _bind_committed_project_bytes(
    root: Path, contents: dict[str, bytes]
) -> None:
    git_executable = shutil.which("git", path=os.defpath)
    if git_executable is None:
        raise PolicyError("project agent mapping trust is unavailable")
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}

    def run(arguments: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git_executable, "-C", str(root), *arguments],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=text,
            check=False,
        )

    try:
        top = run(["rev-parse", "--show-toplevel"], text=True)
        head_before = run(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
        commit = head_before.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise PolicyError(
                "project agent mapping and mapped agents are not committed and clean"
            )
        for relative, expected in sorted(contents.items()):
            reference = f"{commit}:{relative}"
            size = run(["cat-file", "-s", reference], text=True)
            try:
                byte_count = int(size.stdout.strip())
            except ValueError:
                byte_count = -1
            blob = run(["cat-file", "blob", reference]) if (
                0 <= byte_count <= 1_048_576
            ) else None
            clean = run(["diff", "--quiet", commit, "--", relative])
            if (
                size.returncode != 0
                or blob is None
                or blob.returncode != 0
                or blob.stdout != expected
                or clean.returncode != 0
            ):
                raise PolicyError(
                    "project agent mapping and mapped agents are not committed and clean"
                )
        head_after = run(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
    except OSError as exc:
        raise PolicyError("project agent mapping trust is unavailable") from exc
    try:
        exact_root = Path(top.stdout.strip()).resolve(strict=True) == root.resolve(
            strict=True
        )
    except OSError:
        exact_root = False
    if (
        top.returncode != 0
        or not exact_root
        or head_before.returncode != 0
        or head_after.returncode != 0
        or head_after.stdout.strip() != commit
    ):
        raise PolicyError(
            "project agent mapping and mapped agents are not committed and clean"
        )


def validate_project_agent_mapping(
    root: Path,
    raw: bytes,
    policy_path: Path,
    discovery_policy_path: Path,
    *,
    require_committed: bool = False,
    fallback_root: Path | None = None,
) -> dict[str, Any]:
    """Validate one explicit mapping against agents in the exact project."""

    if len(raw) > 65_536:
        raise PolicyError("project agent mapping is too large")
    try:
        mapping = parse_bounded_json_value(raw, max_bytes=65_536)
    except ContractError as exc:
        raise PolicyError("project agent mapping is invalid") from exc
    if not isinstance(mapping, dict) or not set(mapping) <= {
        "version", "roles", "verifiers"
    } or set(mapping) < {"version", "roles"}:
        raise PolicyError(
            "project agent mapping must contain version and roles, with optional verifiers"
        )
    roles = mapping["roles"]
    verifiers = mapping.get("verifiers", {})
    if (
        mapping["version"] != 1
        or not isinstance(roles, dict)
        or not isinstance(verifiers, dict)
    ):
        raise PolicyError("project agent mapping version, roles, or verifiers are invalid")

    policy = load_policy(policy_path)
    supported = set(policy["roles"])
    unknown_roles = sorted(set(roles) - supported)
    unknown_verifiers = sorted(set(verifiers) - (supported - {"verification"}))
    if unknown_roles or unknown_verifiers:
        raise PolicyError("project agent mapping contains an unsupported task family")
    if any(
        not isinstance(agent_type, str) or not agent_type
        for agent_type in (*roles.values(), *verifiers.values())
    ):
        raise PolicyError("project agent mapping values must be non-empty strings")

    discovery_policy = load_discovery_policy(discovery_policy_path)
    catalog = discover(root, discovery_policy)
    if not catalog["ready"]:
        raise PolicyError("project agent catalog is not ready")
    selected: dict[str, tuple[dict[str, Any], Path]] = {}
    for item in catalog["selected"]:
        if item["kind"] == "agent" and item["format"] == "codex-toml":
            selected[item["name"]] = (item, root)
    if fallback_root is not None:
        fallback = fallback_root.resolve(strict=True)
        fallback_catalog = discover(fallback, discovery_policy)
        if not fallback_catalog["ready"]:
            raise PolicyError("package agent catalog is not ready")
        for item in fallback_catalog["selected"]:
            if item["kind"] == "agent" and item["format"] == "codex-toml":
                selected.setdefault(item["name"], (item, fallback))
    selected_paths: set[str] = set()
    definition_contents: dict[str, bytes] = {}
    role_sandboxes: dict[str, str] = {}
    verifier_sandboxes: dict[str, str] = {}
    role_skill_paths: dict[str, list[str]] = {}
    verifier_skill_paths: dict[str, list[str]] = {}
    agent_paths: dict[str, str] = {}

    def selected_skill_paths(
        agent_path: str, definition_root: Path, expected_digest: str
    ) -> tuple[list[str], dict[str, bytes]]:
        try:
            agent_bytes = _read_project_regular(
                definition_root, agent_path, max_bytes=1_048_576
            )
            if hashlib.sha256(agent_bytes).hexdigest() != expected_digest:
                raise PolicyError("mapped project agent changed during validation")
            data = tomllib.loads(agent_bytes.decode("utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise PolicyError("mapped project agent skills are invalid") from exc
        skill_config = data.get("skills", [])
        configured_paths: list[Path] = []
        if isinstance(skill_config, list):
            configured_paths = [
                definition_root / ".agents" / "skills" / name / "SKILL.md"
                for name in skill_config
            ]
        elif isinstance(skill_config, dict) and set(skill_config) == {"config"}:
            for entry in skill_config["config"]:
                if not entry.get("enabled", True):
                    continue
                configured = Path(entry["path"])
                configured_paths.append(
                    configured
                    if configured.is_absolute()
                    else definition_root / configured
                )
        else:
            raise PolicyError("mapped project agent skills are invalid")

        resolved_paths: list[str] = []
        trust_contents: dict[str, bytes] = {agent_path: agent_bytes}
        for configured in configured_paths:
            try:
                configured_relative = configured.absolute().relative_to(
                    definition_root.absolute()
                )
                relative_text = configured_relative.as_posix()
                content = _read_project_regular(
                    definition_root, relative_text, max_bytes=1_048_576
                )
            except (OSError, PolicyError, ValueError) as exc:
                raise PolicyError("mapped project agent skill is unavailable") from exc
            if not relative_text.endswith("/SKILL.md"):
                raise PolicyError("mapped project agent skill is unavailable")
            resolved_paths.append(relative_text)
            trust_contents[relative_text] = content
        return sorted(set(resolved_paths)), trust_contents

    def validate_agent(
        family: str, agent_type: str, *, verifier: bool
    ) -> list[str]:
        selection = selected.get(agent_type)
        if selection is None:
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        candidate, definition_root = selection
        if candidate["source"] != "project":
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        candidate_path = definition_root / candidate["path"]
        current = definition_root
        try:
            relative_candidate = candidate_path.absolute().relative_to(
                definition_root.absolute()
            )
        except ValueError as exc:
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}") from exc
        for part in relative_candidate.parts:
            current /= part
            if current.is_symlink():
                raise PolicyError(
                    f"mapped project agent must not be a symlink: {agent_type}"
                )
        has_unreviewed_keys = any(
            warning.startswith("unreviewed Codex config keys:")
            for warning in candidate["warnings"]
        )
        if (
            candidate["errors"]
            or candidate["requested_mcps"]
            or candidate["requested_tools"]
            or has_unreviewed_keys
        ):
            raise PolicyError(f"mapped project agent is not executable: {agent_type}")
        if (
            candidate["configured_model"] is not None
            or candidate["configured_effort"] is not None
        ):
            raise PolicyError(
                f"mapped project agent must not override model or effort: {agent_type}"
            )
        allowed_sandboxes = (
            {"read-only", "workspace-write"}
            if verifier or family in WRITABLE_PROJECT_FAMILIES
            else {"read-only"}
        )
        if candidate["sandbox_mode"] not in allowed_sandboxes:
            raise PolicyError(
                f"mapped project agent has an unauthorized sandbox: {agent_type}"
            )
        selected_paths.add(candidate["path"])
        agent_paths[agent_type] = candidate["path"]
        candidate_digest = candidate.get("content_sha256")
        if not isinstance(candidate_digest, str):
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        skill_paths, skill_trust_contents = selected_skill_paths(
            candidate["path"], definition_root, candidate_digest
        )
        selected_paths.update(skill_trust_contents)
        definition_contents.update(skill_trust_contents)
        return skill_paths
    for family, agent_type in roles.items():
        role_skill_paths[family] = validate_agent(
            family, agent_type, verifier=False
        )
        role_sandboxes[family] = selected[agent_type][0]["sandbox_mode"]
    for family, agent_type in verifiers.items():
        verifier_skill_paths[family] = validate_agent(
            family, agent_type, verifier=True
        )
        verifier_sandboxes[family] = selected[agent_type][0]["sandbox_mode"]
    if require_committed:
        _bind_committed_project_bytes(
            root,
            {PROJECT_AGENT_MAPPING.as_posix(): raw, **definition_contents},
        )
    definition_hasher = hashlib.sha256()
    definition_hasher.update(raw)
    for relative in sorted(selected_paths):
        content = definition_contents[relative]
        definition_hasher.update(relative.encode("utf-8"))
        definition_hasher.update(b"\0")
        definition_hasher.update(content)
        definition_hasher.update(b"\0")
    return {
        "roles": dict(roles),
        "verifiers": dict(verifiers),
        "role_sandboxes": role_sandboxes,
        "verifier_sandboxes": verifier_sandboxes,
        "role_skill_paths": role_skill_paths,
        "verifier_skill_paths": verifier_skill_paths,
        "agent_paths": agent_paths,
        "digest": definition_hasher.hexdigest(),
    }


def load_effective_policy(
    root: Path,
    policy_path: Path,
    discovery_policy_path: Path,
) -> dict[str, Any]:
    """Apply an explicit project-agent mapping to the accepted base policy.

    Discovery alone never selects an agent for execution. A project agent becomes a
    route candidate only when the project mapping names it for a supported family.
    Model and effort remain owned by the base routing policy.
    """

    policy = load_policy(policy_path)
    resolved_root = root.resolve()
    mapping_path = resolved_root / PROJECT_AGENT_MAPPING
    if mapping_path.is_symlink():
        raise PolicyError("project agent mapping is not a regular file")
    if not mapping_path.exists() and not mapping_path.is_symlink():
        return policy
    try:
        raw = _read_project_regular(
            resolved_root, PROJECT_AGENT_MAPPING.as_posix(), max_bytes=65_536
        )
    except PolicyError as exc:
        raise PolicyError("project agent mapping is invalid") from exc
    mapping = validate_project_agent_mapping(
        root,
        raw,
        policy_path,
        discovery_policy_path,
        require_committed=True,
    )

    effective = deepcopy(policy)
    effective["project_agent_roles"] = mapping["roles"]
    effective["project_agent_verifiers"] = mapping["verifiers"]
    effective["project_agent_role_sandboxes"] = mapping["role_sandboxes"]
    effective["project_agent_verifier_sandboxes"] = mapping["verifier_sandboxes"]
    effective["project_agent_role_skill_paths"] = mapping["role_skill_paths"]
    effective["project_agent_verifier_skill_paths"] = mapping[
        "verifier_skill_paths"
    ]
    effective["project_agent_paths"] = mapping["agent_paths"]
    effective["project_agent_mapping_digest"] = mapping["digest"]
    return effective


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load routing policy {path}: {exc}") from exc
    required = {
        "version",
        "status",
        "models",
        "roles",
        "routes",
        "verification",
        "bounds",
        "max_parallel",
    }
    missing = sorted(required - policy.keys())
    if missing:
        raise PolicyError(f"routing policy missing: {', '.join(missing)}")
    if policy["status"] not in {"candidate", "accepted"}:
        raise PolicyError("routing policy status must be candidate or accepted")
    if not isinstance(policy["version"], int) or policy["version"] < 1:
        raise PolicyError("routing policy version must be a positive integer")
    if not isinstance(policy.get("selection_policy"), str) or re.fullmatch(
        r"[a-z0-9_]+", policy["selection_policy"]
    ) is None:
        raise PolicyError("selection_policy must be a typed identifier")
    if not isinstance(policy["models"], dict) or not policy["models"]:
        raise PolicyError("routing policy models must be a non-empty object")
    if any(
        not isinstance(model, str)
        or not isinstance(efforts, list)
        or not efforts
        or not all(isinstance(effort, str) for effort in efforts)
        for model, efforts in policy["models"].items()
    ):
        raise PolicyError("each policy model needs a non-empty effort string array")
    legacy_fallback = policy.get("legacy_fallback")
    if (
        not isinstance(legacy_fallback, dict)
        or set(legacy_fallback) != {"model", "effort"}
    ):
        raise PolicyError("legacy_fallback must contain exactly model and effort")
    _validate_model_effort(
        policy, legacy_fallback.get("model"), legacy_fallback.get("effort")
    )
    if legacy_fallback["model"] != "gpt-5.6-sol":
        raise PolicyError("gpt-5.6-sol must be the sole legacy fallback")
    if not isinstance(policy["max_parallel"], int) or policy["max_parallel"] < 1:
        raise PolicyError("max_parallel must be at least 1")
    verification = policy["verification"]
    if not isinstance(verification, dict):
        raise PolicyError("verification must be an object")
    if verification.get("mode") != "all_meaningful":
        raise PolicyError("initial policy must verify all meaningful results")
    _validate_model_effort(policy, verification.get("model"), verification.get("effort"))
    if not isinstance(policy["roles"], dict) or not policy["roles"]:
        raise PolicyError("roles must be a non-empty object")
    if not all(isinstance(role, str) and role for role in policy["roles"].values()):
        raise PolicyError("each role mapping must be a non-empty string")
    if not isinstance(policy["routes"], dict):
        raise PolicyError("routes must be an object")
    families = policy["routes"].get("families", {})
    if not isinstance(families, dict):
        raise PolicyError("route families must be an object")
    if set(families) != set(policy["roles"]):
        raise PolicyError("routing families and role families must match")

    def validate_route(route: Any, label: str) -> None:
        if (
            not isinstance(route, dict)
            or not isinstance(route.get("reason"), str)
            or re.fullmatch(r"[a-z0-9_]+", route["reason"]) is None
        ):
            raise PolicyError(f"{label}: route needs a typed reason code")
        _validate_model_effort(policy, route.get("model"), route.get("effort"))
        alternatives = route.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > 8:
            raise PolicyError(f"{label}: alternatives must be a bounded array")
        seen = {(route["model"], route["effort"])}
        for alternative in alternatives:
            if not isinstance(alternative, dict) or set(alternative) != {
                "model", "effort"
            }:
                raise PolicyError(f"{label}: route alternative is invalid")
            _validate_model_effort(
                policy, alternative.get("model"), alternative.get("effort")
            )
            key = (alternative["model"], alternative["effort"])
            if key in seen:
                raise PolicyError(f"{label}: route alternatives must be unique")
            seen.add(key)

    for family, routes in families.items():
        if not isinstance(routes, dict):
            raise PolicyError(f"{family}: routes must be an object")
        if "default" not in routes:
            raise PolicyError(f"{family}: default route is required")
        for name, route in routes.items():
            validate_route(route, f"{family}.{name}")
    for name in ("reconnaissance", "hard_end_to_end"):
        validate_route(policy["routes"].get(name), name)
    bounds = policy["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != BOUND_FIELDS:
        raise PolicyError("bounds must contain exactly the supported fields")
    for field, value in bounds.items():
        minimum = 0 if field in {"max_steers_per_attempt", "max_tool_calls"} else 1
        if not isinstance(value, int) or value < minimum:
            raise PolicyError(f"bounds.{field} must be an integer of at least {minimum}")
    deadlines = policy.get("observation_deadlines")
    if deadlines is None and policy["version"] < 3:
        deadlines = {
            family: bounds["timeout_seconds"] for family in policy["roles"]
        }
        policy = deepcopy(policy)
        policy["observation_deadlines"] = deadlines
    if (
        not isinstance(deadlines, dict)
        or set(deadlines) != set(policy["roles"])
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > bounds["timeout_seconds"]
            for value in deadlines.values()
        )
    ):
        raise PolicyError(
            "observation_deadlines must bound every task family within timeout_seconds"
        )
    return policy


def _validate_model_effort(policy: dict[str, Any], model: Any, effort: Any) -> None:
    if model not in policy["models"]:
        raise PolicyError(f"policy selected unknown model: {model}")
    if effort not in policy["models"][model]:
        raise PolicyError(f"effort {effort!r} is unsupported for {model}")


def _candidate(
    task: dict[str, Any],
    policy: dict[str, Any],
    trusted_routing_evidence_refs: frozenset[str],
) -> tuple[dict[str, Any], str, str]:
    family = task["family"]
    complexity = task["complexity"]
    operation = task["operation"]
    ambiguity = task["ambiguity"]
    context_breadth = task["context_breadth"]
    acceptance_strength = task["acceptance_strength"]
    deterministic = task["deterministic_validation"]
    family_routes = policy["routes"]["families"].get(family)
    if family_routes is None:
        raise ContractError(f"unsupported family: {family}")
    if ambiguity == "unknown" or context_breadth == "unknown":
        return policy["routes"]["reconnaissance"], "reconnaissance", "research"
    hard_end_to_end = (
        operation == "end_to_end"
        and complexity == "extreme"
        and ambiguity == "high"
        and context_breadth == "cross_system"
        and bool(task["routing_evidence_refs"])
        and set(task["routing_evidence_refs"]) <= trusted_routing_evidence_refs
    )
    if hard_end_to_end:
        return policy["routes"]["hard_end_to_end"], "execution", family
    if family == "security":
        return family_routes["default"], "execution", family
    if family == "planning":
        name = (
            "synthesis"
            if complexity in {"high", "extreme"}
            or ambiguity == "high"
            or context_breadth == "cross_system"
            or operation in {"synthesis", "end_to_end"}
            else "default"
        )
        return family_routes[name], "execution", family
    strong_acceptance = (
        acceptance_strength == "executable"
        or (acceptance_strength == "deterministic" and deterministic)
    )
    if family == "verification":
        bounded_verification = (
            ambiguity in {"known", "partial"}
            and context_breadth in {"narrow", "repository"}
            and strong_acceptance
            and complexity != "extreme"
        )
        name = "bounded" if bounded_verification else "default"
        return family_routes[name], "execution", family
    bounded = (
        operation in {"mechanical", "implementation"}
        and ambiguity in {"known", "partial"}
        and context_breadth in {"narrow", "repository"}
        and strong_acceptance
        and complexity != "extreme"
    )
    if bounded:
        return family_routes["bounded"], "execution", family
    if operation in {"exploration", "diagnosis", "review"}:
        name = "judgment" if complexity in {"high", "extreme"} else "exploratory"
        return family_routes[name], "execution", family
    if operation in {"synthesis", "end_to_end"} or context_breadth == "cross_system":
        return family_routes["synthesis"], "execution", family
    if ambiguity == "high":
        name = "judgment" if complexity in {"high", "extreme"} else "exploratory"
        return family_routes[name], "execution", family
    return family_routes["default"], "execution", family


def _selection_metadata(route: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    selected = {"model": route["model"], "effort": route["effort"]}
    return {
        "policy_version": policy["version"],
        "strategy": policy["selection_policy"],
        "selected": selected,
        "eligible": [selected, *deepcopy(route.get("alternatives", []))],
        "selection_probability_micros": 1_000_000,
    }


def _route_task(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
    use_legacy_fallback: bool = False,
) -> dict[str, Any]:
    if not isinstance(use_legacy_fallback, bool):
        raise ContractError("legacy fallback selection must be boolean")
    task = validate_task(task_data)
    candidate, stage, routed_family = _candidate(
        task, policy, trusted_routing_evidence_refs
    )
    primary = {"model": candidate["model"], "effort": candidate["effort"]}
    selected = primary
    reason = candidate["reason"]
    fallback: dict[str, Any] | None = None
    if use_legacy_fallback:
        selected = deepcopy(policy["legacy_fallback"])
        if selected == primary:
            raise ContractError("selected route is already the legacy fallback")
        reason = "selected_model_unavailable_fallback"
        fallback = {
            "trigger": "host_model_unavailable",
            "from": primary,
            "to": deepcopy(selected),
        }
    model = selected["model"]
    effort = selected["effort"]
    _validate_model_effort(policy, model, effort)

    can_parallelize = task["parallel_safe"] and task["independent_parts"] > 1
    max_parallel = min(task["independent_parts"], policy["max_parallel"])
    topology = "parallel" if can_parallelize else "single"
    if topology == "single":
        max_parallel = 1

    verification_required = task["meaningful"] and routed_family != "verification"
    verifier: dict[str, str] | None = None
    if verification_required:
        verification = policy["verification"]
        verification_model = (
            policy["legacy_fallback"]["model"]
            if use_legacy_fallback
            else verification["model"]
        )
        verification_effort = (
            policy["legacy_fallback"]["effort"]
            if use_legacy_fallback
            else verification["effort"]
        )
        verifier = {
            "role": verification["role"],
            "model": verification_model,
            "effort": verification_effort,
            "sandbox_mode": "read-only",
        }
        verifier_agent_type = policy.get("project_agent_verifiers", {}).get(
            routed_family
        )
        if verifier_agent_type is None:
            verifier_agent_type = policy.get("project_agent_roles", {}).get("verification")
        if verifier_agent_type is not None:
            verifier["agent_type"] = verifier_agent_type
            verifier_agent_path = policy.get("project_agent_paths", {}).get(
                verifier_agent_type
            )
            if verifier_agent_path is not None:
                verifier["agent_path"] = verifier_agent_path
            verifier["sandbox_mode"] = policy.get(
                "project_agent_verifier_sandboxes", {}
            ).get(
                routed_family,
                policy.get("project_agent_role_sandboxes", {}).get(
                    "verification", "read-only"
                ),
            )
            verifier["skill_paths"] = policy.get(
                "project_agent_verifier_skill_paths", {}
            ).get(
                routed_family,
                policy.get("project_agent_role_skill_paths", {}).get(
                    "verification", []
                ),
            )

    route_bounds = dict(policy["bounds"])
    route_bounds["timeout_seconds"] = policy["observation_deadlines"][routed_family]
    selection = _selection_metadata(candidate, policy)
    selection["selected"] = deepcopy(selected)
    if use_legacy_fallback:
        selection["fallback_from"] = deepcopy(primary)
        if selected not in selection["eligible"]:
            selection["eligible"].append(deepcopy(selected))
    result = {
        "task_id": task["id"],
        "role": policy["roles"][routed_family],
        "model": model,
        "effort": effort,
        "stage": stage,
        "routed_family": routed_family,
        "topology": topology,
        "max_parallel": max_parallel,
        "verification_required": verification_required,
        "verifier": verifier,
        "bounds": route_bounds,
        "observation_deadline_seconds": route_bounds["timeout_seconds"],
        "deadline_semantics": "host_observation_only",
        "reason": reason,
        "selection": selection,
        "sandbox_mode": "read-only",
    }
    if fallback is not None:
        fallback["verifier_substituted"] = verification_required
        result["fallback"] = fallback
    mapping_digest = policy.get("project_agent_mapping_digest")
    if mapping_digest is not None:
        result["project_agent_mapping_digest"] = mapping_digest
        verification_for_family = task.get("verification_for_family")
        if task["family"] == "verification" and verification_for_family is not None:
            result["agent_type"] = policy.get("project_agent_verifiers", {}).get(
                verification_for_family,
                policy["project_agent_roles"].get("verification", result["role"]),
            )
            result["sandbox_mode"] = policy.get(
                "project_agent_verifier_sandboxes", {}
            ).get(
                verification_for_family,
                policy.get("project_agent_role_sandboxes", {}).get(
                    "verification", "read-only"
                ),
            )
            result["skill_paths"] = policy.get(
                "project_agent_verifier_skill_paths", {}
            ).get(
                verification_for_family,
                policy.get("project_agent_role_skill_paths", {}).get(
                    "verification", []
                ),
            )
        else:
            result["agent_type"] = policy["project_agent_roles"].get(
                routed_family, result["role"]
            )
            result["sandbox_mode"] = policy.get(
                "project_agent_role_sandboxes", {}
            ).get(routed_family, "read-only")
            result["skill_paths"] = policy.get(
                "project_agent_role_skill_paths", {}
            ).get(routed_family, [])
        agent_path = policy.get("project_agent_paths", {}).get(result["agent_type"])
        if agent_path is not None:
            result["agent_path"] = agent_path
    return result


def route_task(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Select the primary policy route; fallback requires trusted lifecycle evidence."""

    return _route_task(
        task_data,
        policy,
        trusted_routing_evidence_refs=trusted_routing_evidence_refs,
    )


def _route_task_legacy_fallback(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve the private fallback after the ledger authorizes host unavailability."""

    return _route_task(
        task_data,
        policy,
        trusted_routing_evidence_refs=trusted_routing_evidence_refs,
        use_legacy_fallback=True,
    )
