"""Deterministic outcome, case, candidate, comparison, and promotion logic."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import random
import re
import resource
import secrets
import select
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib

from .catalog import CLAUDE_READ_ONLY_TOOLS, CatalogError, parse_claude_agent
from .contracts import (
    ContractError,
    result_axes,
    routing_task_slice,
    validate_assignment_shape,
    validate_result,
    validate_task,
)
from .learning_contracts import (
    CANDIDATE_EFFECTS,
    CANDIDATE_TARGETS,
    CORPUS_STATES,
    SUPPORTED_MODELS,
    canonical_json,
    content_digest,
    external_id,
    keyed_digest,
    parse_bounded_json_value,
    provider_for_model,
    validate_billing_attestation,
    validate_canary_report,
    validate_candidate,
    validate_comparison,
    validate_evaluation_assignment,
    validate_evaluation_case,
    validate_evaluation_trial,
    validate_event,
    validate_execution_configuration,
    validate_feedback,
    validate_grader_calibration,
    validate_host_observation,
    validate_outcome,
    validate_owner_grant,
    validate_promotion,
    validate_qa_evidence,
)
from .git_plumbing import XCRUN_NO_CACHE
from .git_plumbing import git_executable as find_git
from .git_plumbing import repository_layout_problem, run_git
from .host_paths import HostPathError, private_directory, remove_private_directory
from .routing import provider_model_allowed

COMPARISON_MIN_CASES = 20
MATERIAL_IMPROVEMENT = 0.10
BOOTSTRAP_SAMPLES = 10_000
GRADER_MIN_LABELS = 30
GRADER_MIN_AGREEMENT = 0.90
GRADER_MIN_SWAP_STABILITY = 0.95
RUNTIME_BOUND_CANDIDATE_PATHS = {"routing.model_effort"}


def _read_managed_bytes(
    root: Path,
    relative: str,
    *,
    allowed_roots: frozenset[str] = frozenset({".agents", ".codex"}),
) -> bytes:
    """Read one package file through no-follow descriptors."""

    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] not in allowed_roots
    ):
        raise ContractError("installed package manifest contains an unsafe path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
        )
        try:
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ContractError("installed package path is not a regular file")
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                return handle.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    except OSError as exc:
        raise ContractError("installed execution artifacts are unavailable") from exc
    finally:
        os.close(descriptor)


def _read_project_bytes(root: Path, relative: str) -> bytes:
    """Read one validated project file without following path components."""

    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] == ".git"
    ):
        raise ContractError("project skill path is unsafe")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
        )
        try:
            return os.read(file_descriptor, 1_048_577)
        finally:
            os.close(file_descriptor)
    except OSError as exc:
        raise ContractError("project skill is unavailable") from exc
    finally:
        os.close(descriptor)


def _source_managed_paths(root: Path) -> list[str]:
    """List only package-owned source roots and reject every symlink."""

    paths: list[str] = []
    claude_skill = root / CLAUDE_ORCHESTRATION_SKILL
    for package_root in (root / ".agents", root / ".codex", claude_skill):
        if package_root == claude_skill and not os.path.lexists(package_root):
            continue
        if package_root.is_symlink() or not package_root.is_dir():
            raise ContractError("installed package root is unavailable")
        for current, directories, files in os.walk(package_root, followlinks=False):
            current_path = Path(current)
            kept = []
            for directory in directories:
                child = current_path / directory
                if child.is_symlink():
                    raise ContractError("installed package contains a symlink")
                if directory != "__pycache__":
                    kept.append(directory)
            directories[:] = kept
            for filename in files:
                child = current_path / filename
                if child.is_symlink():
                    raise ContractError("installed package contains a symlink")
                if filename == ".DS_Store" or child.suffix == ".pyc":
                    continue
                relative = child.relative_to(root).as_posix()
                if relative == ".codex/orchestration/install-manifest.json":
                    continue
                paths.append(relative)
    return sorted(paths)


# The one path under .claude that this package installs.
CLAUDE_ORCHESTRATION_SKILL = ".claude/skills/agent-orchestration"


def _read_package_bytes(root: Path, relative: str) -> bytes:
    if relative.startswith(CLAUDE_ORCHESTRATION_SKILL + "/"):
        return _read_managed_bytes(root, relative, allowed_roots=frozenset({".claude"}))
    return _read_managed_bytes(root, relative)


def _installed_managed_files(root: Path) -> tuple[list[str], dict[str, bytes]]:
    manifest_relative = ".codex/orchestration/install-manifest.json"
    manifest_path = root / manifest_relative
    if manifest_path.is_symlink():
        raise ContractError("installed package manifest is invalid")
    if not manifest_path.exists():
        paths = _source_managed_paths(root)
        return paths, {path: _read_package_bytes(root, path) for path in paths}
    manifest_raw = _read_managed_bytes(root, manifest_relative)
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("installed package manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "version", "files"}
        or manifest["format"] != 1
        or not isinstance(manifest["version"], str)
        or not isinstance(manifest["files"], dict)
        or len(manifest["files"]) > 4096
    ):
        raise ContractError("installed package manifest is invalid")
    paths = sorted(manifest["files"])
    contents: dict[str, bytes] = {}
    for relative in paths:
        expected = manifest["files"][relative]
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or len(expected) != 64
        ):
            raise ContractError("installed package manifest is invalid")
        content = _read_package_bytes(root, relative)
        if (
            Path(relative).name != "learning-releases.json"
            and hashlib.sha256(content).hexdigest() != expected
        ):
            raise ContractError("installed package integrity check failed")
        contents[relative] = content
    return paths, contents


CLAUDE_SKILL_PATH = ".claude/skills/claude-agents/SKILL.md"
# Mirrors the router's default; only a committed project configuration changes it.
DEFAULT_PROVIDER_CONFIG_DIGEST = content_digest(
    {
        "version": 1,
        "providers": {"codex": {"enabled": True}, "claude": {"enabled": False}},
        "codex_primary_may_bridge_to_claude": False,
    }
)


def _claude_agent_definition(raw: bytes) -> tuple[dict[str, Any], str]:
    """Parse one Claude agent with the discovery parser and require read-only tools."""

    try:
        metadata, prompt = parse_claude_agent(raw)
    except CatalogError as exc:
        raise ContractError(f"installed Claude agent is invalid: {exc}") from exc
    if not set(metadata["tools"]) <= CLAUDE_READ_ONLY_TOOLS:
        raise ContractError("installed Claude agent must use read-only tools")
    return metadata, prompt


def _claude_agent_artifacts(
    root: Path, route: dict[str, Any], agent_type: str
) -> tuple[bytes, str, dict[str, bytes]]:
    agent_relative = f".claude/agents/{agent_type}.md"
    if route.get("agent_path", agent_relative) != agent_relative:
        raise ContractError("Claude agent path is invalid")
    claude_roots = frozenset({".claude"})
    agent_bytes = _read_managed_bytes(root, agent_relative, allowed_roots=claude_roots)
    if len(agent_bytes) > 1_048_576:
        raise ContractError("installed Claude agent is too large")
    metadata, prompt = _claude_agent_definition(agent_bytes)
    if metadata["name"] != agent_type:
        raise ContractError("installed agent identity does not match route")
    skill_contents: dict[str, bytes] = {}
    if os.path.lexists(root / CLAUDE_SKILL_PATH):
        skill_contents[CLAUDE_SKILL_PATH] = _read_managed_bytes(
            root, CLAUDE_SKILL_PATH, allowed_roots=claude_roots
        )
    return agent_bytes, prompt, skill_contents


def installed_execution_configuration(
    bundle_root: Path,
    route: dict[str, Any],
    *,
    client_capability: dict[str, Any],
) -> dict[str, Any]:
    """Derive the replay snapshot from installed, read-only package artifacts."""

    root = bundle_root.absolute()
    role = route.get("role")
    agent_type = route.get("agent_type", role)
    if not isinstance(role, str) or not isinstance(agent_type, str):
        raise ContractError("route role is invalid")
    policy_relative = ".codex/orchestration/config/routing-policy.json"
    provider = route.get("provider", "codex")
    if provider == "claude":
        try:
            package_paths, package_contents = _installed_managed_files(root)
            policy_bytes = package_contents[policy_relative]
        except (KeyError, OSError) as exc:
            raise ContractError("installed execution artifacts are unavailable") from exc
        agent_bytes, prompt, skill_contents = _claude_agent_artifacts(
            root, route, agent_type
        )
        return _execution_configuration_record(
            route,
            role=role,
            package_paths=package_paths,
            package_contents=package_contents,
            policy_bytes=policy_bytes,
            agent_bytes=agent_bytes,
            skill_contents=skill_contents,
            prompt=prompt,
            client_capability=client_capability,
        )
    if provider != "codex":
        raise ContractError("route provider is invalid")
    routed_agent_path = route.get("agent_path")
    if routed_agent_path is None:
        agent_relative = f".codex/agents/{agent_type}.toml"
    elif not isinstance(routed_agent_path, str):
        raise ContractError("project agent path is invalid")
    else:
        agent_path = Path(routed_agent_path)
        if (
            agent_path.parent != Path(".codex/agents")
            or agent_path.suffix != ".toml"
        ):
            raise ContractError("project agent path is invalid")
        agent_relative = routed_agent_path
    try:
        package_paths, package_contents = _installed_managed_files(root)
        policy_bytes = package_contents[policy_relative]
        package_agent = agent_relative in package_contents
        if not package_agent and routed_agent_path is None:
            raise ContractError("project agent path is unavailable")
        agent_bytes = (
            package_contents[agent_relative]
            if package_agent
            else _read_managed_bytes(root, agent_relative)
        )
        agent = tomllib.loads(agent_bytes.decode("utf-8"))
        if package_agent:
            skill_paths = sorted(
                path for path in package_paths
                if path.startswith(".agents/skills/") and path.endswith("/SKILL.md")
            )
            skill_contents = {path: package_contents[path] for path in skill_paths}
        else:
            skill_paths = route.get("skill_paths")
            if not isinstance(skill_paths, list) or not all(
                isinstance(item, str) for item in skill_paths
            ):
                raise ContractError("project agent skills are invalid")
            skill_paths = sorted(set(skill_paths))
            skill_contents = {
                path: _read_project_bytes(root, path) for path in skill_paths
            }
    except (KeyError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError("installed execution artifacts are unavailable") from exc
    prompt = agent.get("developer_instructions")
    if agent.get("name") != agent_type:
        raise ContractError("installed agent identity does not match route")
    return _execution_configuration_record(
        route,
        role=role,
        package_paths=package_paths,
        package_contents=package_contents,
        policy_bytes=policy_bytes,
        agent_bytes=agent_bytes,
        skill_contents={path: skill_contents[path] for path in skill_paths},
        prompt=prompt,
        client_capability=client_capability,
    )


def _execution_configuration_record(
    route: dict[str, Any],
    *,
    role: str,
    package_paths: list[str],
    package_contents: dict[str, bytes],
    policy_bytes: bytes,
    agent_bytes: bytes,
    skill_contents: dict[str, bytes],
    prompt: Any,
    client_capability: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt:
        raise ContractError("installed agent prompt is unavailable")
    manifest = [
        {
            "path": path,
            "digest": hashlib.sha256(package_contents[path]).hexdigest(),
        }
        for path in package_paths
        if Path(path).name != "learning-releases.json"
    ]
    policy_digest = hashlib.sha256(policy_bytes).hexdigest()
    mapping_digest = route.get("project_agent_mapping_digest")
    if mapping_digest is not None:
        if (
            not isinstance(mapping_digest, str)
            or len(mapping_digest) != 64
            or any(character not in "0123456789abcdef" for character in mapping_digest)
        ):
            raise ContractError("project agent mapping digest is invalid")
        policy_digest = content_digest(
            {"routing_policy": policy_digest, "project_agent_mapping": mapping_digest}
        )
    provider_digest = route.get("provider_config_digest", DEFAULT_PROVIDER_CONFIG_DIGEST)
    if provider_digest != DEFAULT_PROVIDER_CONFIG_DIGEST:
        # Only a committed provider configuration changes the replay binding, so
        # Codex-only projects keep their existing execution digests.
        if (
            not isinstance(provider_digest, str)
            or len(provider_digest) != 64
            or any(character not in "0123456789abcdef" for character in provider_digest)
        ):
            raise ContractError("provider configuration digest is invalid")
        policy_digest = content_digest(
            {"routing_policy": policy_digest, "provider_routing": provider_digest}
        )
    return validate_execution_configuration(
        {
            "model": route["model"],
            "effort": route["effort"],
            "role": role,
            "topology": route["topology"],
            "max_parallel": route["max_parallel"],
            "verification_required": route["verification_required"],
            "verifier_digest": (
                content_digest(route["verifier"])
                if route["verifier"] is not None else None
            ),
            "bounds_digest": content_digest(route["bounds"]),
            "package_digest": content_digest(manifest),
            "policy_digest": policy_digest,
            "agent_digest": hashlib.sha256(agent_bytes).hexdigest(),
            "skill_digests": sorted(
                hashlib.sha256(content).hexdigest()
                for content in skill_contents.values()
            ),
            "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "client_digest": content_digest(client_capability),
        }
    )


def _now_ms(value: int | None) -> int:
    return value if value is not None else time.time_ns() // 1_000_000


def runtime_fixture_digest(digest_key: bytes, task: dict[str, Any]) -> str:
    """Bind a frozen case to the exact runtime task without storing task text."""

    return keyed_digest(digest_key, canonical_json(validate_task(task)))


def _usage(value: dict[str, Any] | None) -> dict[str, int | None]:
    fields = {
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "model_calls",
        "tool_calls",
        "latency_ms",
        "cost_microunits",
    }
    if value is None:
        return {field: None for field in fields}
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("workflow usage must contain the closed usage field set")
    for field, item in value.items():
        if item is not None and (
            not isinstance(item, int) or isinstance(item, bool) or item < 0
        ):
            raise ContractError(f"workflow usage field is invalid: {field}")
    return dict(value)


def build_outcome(
    *,
    assignment: dict[str, Any],
    result: dict[str, Any],
    digest_key: bytes,
    run_id: str,
    task_slice: str,
    fixture_digest: str,
    topology: str,
    max_parallel: int,
    verification_required: bool,
    verifier_digest: str | None,
    bounds_digest: str,
    package_digest: str,
    policy_digest: str,
    agent_digest: str,
    skill_digests: list[str],
    prompt_digest: str,
    client_digest: str,
    deterministic_pass: bool,
    security_pass: bool,
    quality_score: float,
    repairs: int,
    repair_reason_codes: list[str],
    delegation_overhead_tokens: int,
    no_progress_events: int,
    terminal_failure: bool,
    verifier_disagreement: bool,
    accepted: bool,
    coverage: str,
    workflow_usage: dict[str, Any] | None,
    usage_source: str = "host_observed",
    observed_at_ms: int | None = None,
    evaluation_candidate_digest: str | None = None,
    evaluation_case_digest: str | None = None,
) -> dict[str, Any]:
    """Join a validated result to hard-gate evidence without persisting result text."""

    checked_result = validate_result(result, assignment=assignment)
    axes = result_axes(
        checked_result,
        attempt=assignment.get("attempt", 1),
        retry_of_assignment_id=assignment.get("retry_of_assignment_id"),
    )
    if not isinstance(run_id, str) or not run_id:
        raise ContractError("observed run_id is required")
    if usage_source not in {"host_observed", "client_observed"}:
        raise ContractError("workflow usage source is unsupported")
    if not isinstance(quality_score, (int, float)) or isinstance(quality_score, bool):
        raise ContractError("quality_score must be numeric")
    if not 0 <= quality_score <= 1:
        raise ContractError("quality_score must be from zero to one")
    if not isinstance(repairs, int) or isinstance(repairs, bool) or repairs < 0:
        raise ContractError("repairs must be a non-negative integer")
    required = assignment.get("required_evidence", [])
    passed = {
        item["criterion_id"]
        for item in checked_result["evidence"]
        if item["outcome"] in {"pass", "observed"}
    }
    evidence_digests = sorted(
        {
            keyed_digest(digest_key, canonical_json(checked_result)),
            *(
                content_digest(
                    {
                        "criterion_id": item["criterion_id"],
                        "type": item["type"],
                        "outcome": item["outcome"],
                    }
                )
                for item in checked_result["evidence"]
            ),
        }
    )
    status_map = {
        "completed": "accepted" if accepted else "rejected",
        "failed": "failed",
        "timed_out": "failed",
        "invalid": "failed",
        "interrupted": "interrupted",
        "cancelled": "cancelled",
    }
    timestamp = _now_ms(observed_at_ms)
    configuration = {
        "model": assignment["model"],
        "effort": assignment["effort"],
        "role": assignment["role"],
        "topology": topology,
        "max_parallel": max_parallel,
        "verification_required": verification_required,
        "verifier_digest": verifier_digest,
        "bounds_digest": bounds_digest,
        "package_digest": package_digest,
        "policy_digest": policy_digest,
        "agent_digest": agent_digest,
        "skill_digests": sorted(skill_digests),
        "prompt_digest": prompt_digest,
        "client_digest": client_digest,
    }
    record_id = external_id(
        digest_key,
        "out",
        f"{assignment['assignment_id']}\x00{checked_result['status']}\x00{timestamp}",
    )
    normalized_usage = _usage(workflow_usage)
    record = {
        "schema_version": 1,
        "record_type": "outcome",
        "record_id": record_id,
        "run_id": run_id,
        "assignment_id": assignment["assignment_id"],
        "task_slice": task_slice,
        "fixture_digest": fixture_digest,
        **configuration,
        "configuration_digest": content_digest(configuration),
        "observed_at_ms": timestamp,
        "status": status_map[axes["execution_state"]],
        "execution_state": axes["execution_state"],
        "semantic_verdict": axes["semantic_verdict"],
        "admission_state": (
            "accepted"
            if accepted
            else "rejected"
        ),
        "retry_attempt": axes["retry"]["attempt"],
        "retry_of_assignment_id": axes["retry"][
            "retry_of_assignment_id"
        ],
        "deterministic_pass": deterministic_pass,
        "security_pass": security_pass,
        "required_criteria_total": len(required),
        "required_criteria_passed": len(passed),
        "quality_micros": round(float(quality_score) * 1_000_000),
        "repairs": repairs,
        "repair_reason_codes": list(repair_reason_codes),
        "delegation_overhead_tokens": delegation_overhead_tokens,
        "no_progress_events": no_progress_events,
        "terminal_failure": terminal_failure,
        "verifier_disagreement": verifier_disagreement,
        "accepted": accepted,
        "coverage": coverage,
        "evaluation_candidate_digest": evaluation_candidate_digest,
        "evaluation_case_digest": evaluation_case_digest,
        "evidence_digests": evidence_digests,
        "usage": normalized_usage,
        "usage_sources": {
            field: {
                "source": usage_source if value is not None else "unavailable",
                "coverage": coverage if value is not None else "unknown",
            }
            for field, value in normalized_usage.items()
        },
    }
    return validate_outcome(record)


def build_host_result_records(
    *,
    assignment: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    observation: dict[str, Any],
    host_evidence: dict[str, Any],
    execution_configuration: dict[str, Any],
    digest_key: bytes,
    client_capability: dict[str, Any],
    evaluation_candidate_digest: str | None = None,
    evaluation_case_digest: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Derive diagnostic result records from admitted host events and installed config."""

    observation = validate_host_observation(observation)
    checked_result = validate_result(result, assignment=assignment)
    host_axes = result_axes(
        checked_result,
        attempt=assignment.get("attempt", 1),
        retry_of_assignment_id=assignment.get("retry_of_assignment_id"),
    )
    configuration = validate_execution_configuration(execution_configuration)
    task_slice = routing_task_slice(task)
    if observation["run_id"] != host_evidence.get("run_id"):
        raise ContractError("host evidence run does not match the result")
    if configuration["client_digest"] != content_digest(client_capability):
        raise ContractError("host client does not match installed execution configuration")
    if any(
        assignment[field] != configuration[field]
        for field in ("model", "effort", "role")
    ):
        raise ContractError("assignment does not match installed execution configuration")
    result_digest = keyed_digest(digest_key, canonical_json(checked_result))
    outcome = build_outcome(
        assignment=assignment,
        result=checked_result,
        digest_key=digest_key,
        run_id=observation["run_id"],
        task_slice=task_slice,
        fixture_digest=runtime_fixture_digest(digest_key, task),
        topology=configuration["topology"],
        max_parallel=configuration["max_parallel"],
        verification_required=configuration["verification_required"],
        verifier_digest=configuration["verifier_digest"],
        bounds_digest=configuration["bounds_digest"],
        package_digest=configuration["package_digest"],
        policy_digest=configuration["policy_digest"],
        agent_digest=configuration["agent_digest"],
        skill_digests=configuration["skill_digests"],
        prompt_digest=configuration["prompt_digest"],
        client_digest=configuration["client_digest"],
        deterministic_pass=False,
        security_pass=False,
        quality_score=0.0,
        repairs=sum(
            attempt["status"] == "rejected"
            for attempt in observation["result_attempts"]
        ),
        repair_reason_codes=[
            attempt["reason_code"]
            for attempt in observation["result_attempts"]
            if attempt["status"] == "rejected"
        ],
        delegation_overhead_tokens=0,
        no_progress_events=host_evidence["no_progress_events"],
        terminal_failure=host_axes["execution_state"] in {
            "failed", "timed_out", "invalid", "interrupted", "cancelled"
        },
        verifier_disagreement=False,
        accepted=False,
        coverage=host_evidence["coverage"],
        workflow_usage=host_evidence["usage"],
        usage_source=host_evidence.get("usage_source", "host_observed"),
        observed_at_ms=host_evidence["observed_at_ms"],
        evaluation_candidate_digest=evaluation_candidate_digest,
        evaluation_case_digest=evaluation_case_digest,
    )
    if host_axes["execution_state"] == "completed":
        outcome["status"] = "unverified"
        outcome["admission_state"] = "unverified"
    outcome["required_criteria_passed"] = 0
    assignment_digest = keyed_digest(
        digest_key, canonical_json(validate_assignment_shape(assignment))
    )
    outcome["evidence_digests"] = sorted(
        {*outcome["evidence_digests"], assignment_digest, result_digest}
    )
    outcome = validate_outcome(outcome)
    status = host_axes["execution_state"]
    event_code = {
        "completed": "item_completed",
        "failed": "turn_failed",
        "timed_out": "turn_failed",
        "invalid": "turn_failed",
        "interrupted": "turn_interrupted",
        "cancelled": "turn_interrupted",
    }[status]
    event_status = {
        "completed": "completed",
        "failed": "failed",
        "timed_out": "failed",
        "invalid": "failed",
        "interrupted": "interrupted",
        "cancelled": "cancelled",
    }[status]
    stop_reason = {
        "completed": "completed",
        "failed": "failed",
        "timed_out": "timeout",
        "invalid": "failed",
        "interrupted": "interrupted",
        "cancelled": "cancelled",
    }[status]
    event = validate_event(
        {
            "schema_version": 1,
            "record_type": "event",
            "record_id": external_id(
                digest_key,
                "evt",
                f"{observation['run_id']}\x00{assignment['assignment_id']}\x00{result_digest}",
            ),
            "run_id": observation["run_id"],
            "turn_id": None,
            "assignment_id": assignment["assignment_id"],
            "parent_run_id": None,
            "observed_at_ms": host_evidence["observed_at_ms"],
            "event_code": event_code,
            "model": assignment["model"],
            "effort": assignment["effort"],
            "role": assignment["role"],
            "task_family": task["family"],
            "task_slice": task_slice,
            "topology": configuration["topology"],
            "configuration_digest": outcome["configuration_digest"],
            "status": event_status,
            "stop_reason": stop_reason,
            "tool_digest": None,
            "item_digest": result_digest,
            "coverage": host_evidence["coverage"],
            "usage": outcome["usage"],
        }
    )
    return event, outcome, "evaluator"


def _check_report_digests(value: Any, label: str) -> tuple[list[str], bool]:
    if not isinstance(value, list) or not value or len(value) > 256:
        raise ContractError(f"{label} must be a non-empty bounded array")
    digests: list[str] = []
    identifiers: set[str] = set()
    passed = True
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "check_id", "passed", "evidence_digest"
        }:
            raise ContractError(f"{label} contains an invalid check")
        check_id = item["check_id"]
        if (
            not isinstance(check_id, str)
            or not check_id
            or len(check_id) > 160
            or check_id in identifiers
        ):
            raise ContractError(f"{label} contains an invalid check identifier")
        if not isinstance(item["passed"], bool):
            raise ContractError(f"{label} contains an invalid check result")
        evidence_digest = item["evidence_digest"]
        if (
            not isinstance(evidence_digest, str)
            or len(evidence_digest) != 64
            or any(character not in "0123456789abcdef" for character in evidence_digest)
        ):
            raise ContractError(f"{label} contains an invalid evidence digest")
        identifiers.add(check_id)
        passed = passed and item["passed"]
        digests.append(content_digest(item))
    return sorted(digests), passed


FORBIDDEN_CHECK_EXECUTABLE_NAMES = {
    "bash", "csh", "dash", "env", "fish", "ksh", "sh", "tcsh", "zsh"
}
MAX_RUNTIME_TREE_FILES = 20_000
MAX_RUNTIME_TREE_ENTRIES = 25_000
MAX_RUNTIME_TREE_DEPTH = 32
MAX_RUNTIME_TREE_BYTES = 512 * 1024 * 1024
MAX_QA_TEMP_ENTRIES = 10_000
MAX_QA_TEMP_BYTES = 128 * 1024 * 1024
MAX_QA_TRANSIENT_TREE_SCANS = 8
MAX_QA_FILE_BYTES = 8 * 1024 * 1024
MAX_QA_OPEN_FILES = 64
MAX_RUNTIME_LIBRARIES = 16
MAX_RUNTIME_LIBRARY_BYTES = 64 * 1024 * 1024
HOMEBREW_CELLAR_ROOT = Path("/opt/homebrew/Cellar")
QA_WORK_DIRECTORY_TOKEN = "{codex_agents_qa_work}"
RUFF_QA_RULES = "E4,E7,E9,F"
PYTHON_EXECUTABLE_NAME = re.compile(r"python(?:3(?:\.\d+)?)?")


def _open_regular_no_follow(path: Path) -> int:
    """Open one canonical regular file without following any path component."""

    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ContractError("operational check executable is invalid")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        result = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
    except OSError as exc:
        raise ContractError("operational check executable is unavailable") from exc
    finally:
        os.close(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(result).st_mode):
            raise ContractError("operational check executable is invalid")
    except Exception:
        os.close(result)
        raise
    return result


def _descriptor_digest(descriptor: int) -> str:
    hasher = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 65_536):
        hasher.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return hasher.hexdigest()


def _canonical_file_identity(path: Path, *, invalid_label: str) -> dict[str, str]:
    try:
        resolved = path.resolve(strict=True)
        descriptor = _open_regular_no_follow(resolved)
    except (ContractError, OSError) as exc:
        raise ContractError(f"operational check {invalid_label} is invalid") from exc
    try:
        digest = _descriptor_digest(descriptor)
        first_line = os.read(descriptor, 4_096).splitlines()[0:1]
    finally:
        os.close(descriptor)
    return {
        "configured": str(path),
        "resolved": str(resolved),
        "digest": digest,
        "first_line": first_line[0].decode("utf-8", errors="surrogateescape")
        if first_line else "",
    }


def _project_path_has_symlink(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _copy_and_digest_runtime_tree(source: Path, target: Path | None = None) -> str:
    """Digest, and optionally copy, one bounded no-symlink Python package tree."""

    hasher = hashlib.sha256()
    file_count = 0
    entry_count = 0
    byte_count = 0
    try:
        if target is not None:
            target.mkdir(parents=True, mode=0o700)
        for current_text, directories, files, directory_fd in os.fwalk(
            source, topdown=True, follow_symlinks=False
        ):
            current = Path(current_text)
            relative_directory = current.relative_to(source)
            if len(relative_directory.parts) > MAX_RUNTIME_TREE_DEPTH:
                raise ContractError(
                    "operational check runtime tree exceeds its bounds"
                )
            directories.sort()
            files.sort()
            for name in directories:
                mode = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                ).st_mode
                if not stat.S_ISDIR(mode):
                    raise ContractError("operational check runtime tree is invalid")
                entry_count += 1
                if entry_count > MAX_RUNTIME_TREE_ENTRIES:
                    raise ContractError(
                        "operational check runtime tree exceeds its bounds"
                    )
                relative = relative_directory / name
                relative_bytes = relative.as_posix().encode("utf-8")
                hasher.update(b"D")
                hasher.update(len(relative_bytes).to_bytes(4, "big"))
                hasher.update(relative_bytes)
                if target is not None:
                    (target / relative_directory / name).mkdir(mode=0o700)
            for name in files:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                output = -1
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ContractError(
                            "operational check runtime tree is invalid"
                        )
                    file_count += 1
                    entry_count += 1
                    if (
                        file_count > MAX_RUNTIME_TREE_FILES
                        or entry_count > MAX_RUNTIME_TREE_ENTRIES
                        or byte_count > MAX_RUNTIME_TREE_BYTES
                    ):
                        raise ContractError(
                            "operational check runtime tree exceeds its bounds"
                        )
                    relative = relative_directory / name
                    relative_bytes = relative.as_posix().encode("utf-8")
                    file_hasher = hashlib.sha256()
                    destination = None if target is None else target / relative
                    if destination is not None:
                        output = os.open(
                            destination,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o400,
                        )
                    while chunk := os.read(descriptor, 65_536):
                        byte_count += len(chunk)
                        if byte_count > MAX_RUNTIME_TREE_BYTES:
                            raise ContractError(
                                "operational check runtime tree exceeds its bounds"
                            )
                        file_hasher.update(chunk)
                        if output >= 0:
                            view = memoryview(chunk)
                            while view:
                                written = os.write(output, view)
                                view = view[written:]
                    if output >= 0:
                        os.fsync(output)
                    hasher.update(b"F")
                    hasher.update(len(relative_bytes).to_bytes(4, "big"))
                    hasher.update(relative_bytes)
                    hasher.update(file_hasher.digest())
                finally:
                    os.close(descriptor)
                    if output >= 0:
                        os.close(output)
        if target is not None:
            for current, directories, _files in os.walk(target, topdown=False):
                for name in directories:
                    (Path(current) / name).chmod(0o500)
            target.chmod(0o500)
    except ContractError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError("operational check runtime tree is invalid") from exc
    return hasher.hexdigest()


def _runtime_tree_identity(
    root: Path, configured: Any, *, interpreter: dict[str, str] | None
) -> dict[str, str] | None:
    if configured is None:
        return None
    if (
        interpreter is None
        or not isinstance(configured, dict)
        or set(configured) != {"kind", "path", "sha256"}
        or configured.get("kind") != "python-site-packages"
        or not isinstance(configured.get("path"), str)
        or not configured["path"]
        or not isinstance(configured.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", configured["sha256"])
        or not PYTHON_EXECUTABLE_NAME.fullmatch(Path(interpreter["resolved"]).name)
    ):
        raise ContractError("operational check runtime tree is invalid")
    path = Path(configured["path"])
    if not path.is_absolute() and not configured["path"].startswith("./"):
        raise ContractError("operational check runtime tree is invalid")
    source = path if path.is_absolute() else root / path
    if not path.is_absolute() and _project_path_has_symlink(root, source):
        raise ContractError("operational check runtime tree is invalid")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ContractError("operational check runtime tree is invalid") from exc
    if (
        not resolved.is_dir()
        or resolved.name != "site-packages"
        or resolved.parent.parent.name != "lib"
        or not re.fullmatch(r"python3\.\d+", resolved.parent.name)
    ):
        raise ContractError("operational check runtime tree is invalid")
    digest = _copy_and_digest_runtime_tree(resolved)
    if digest != configured["sha256"]:
        raise ContractError("operational check runtime tree digest does not match")
    return {
        "kind": "python-site-packages",
        "configured": str(source),
        "resolved": str(resolved),
        "sha256": digest,
        "python_directory": resolved.parent.name,
    }


def _runtime_library_identities(
    configured: Any,
    *,
    driver: str,
    interpreter: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Validate explicitly pinned Homebrew libraries loaded by a Python check."""

    if configured is None:
        return []
    if (
        not isinstance(configured, list)
        or len(configured) > MAX_RUNTIME_LIBRARIES
        or configured
        and (driver != "python-wrapper" or interpreter is None)
    ):
        raise ContractError("operational check runtime libraries are invalid")
    cellar = HOMEBREW_CELLAR_ROOT
    identities: list[dict[str, str]] = []
    names: set[str] = set()
    total_bytes = 0
    for item in configured:
        if (
            not isinstance(item, dict)
            or not {"path", "sha256"}.issubset(item)
            or not set(item).issubset({"path", "sha256", "load_name"})
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ContractError("operational check runtime libraries are invalid")
        path = Path(item["path"])
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(cellar)
        except (OSError, ValueError) as exc:
            raise ContractError(
                "operational check runtime libraries are invalid"
            ) from exc
        load_name = item.get("load_name", resolved.name)
        if (
            not path.is_absolute()
            or path != resolved
            or len(relative.parts) < 4
            or relative.parts[2] != "lib"
            or not resolved.name.endswith(".dylib")
            or not isinstance(load_name, str)
            or not re.fullmatch(r"[A-Za-z0-9._+-]{1,128}\.dylib", load_name)
            or Path(load_name).name != load_name
            or load_name in names
        ):
            raise ContractError("operational check runtime libraries are invalid")
        identity = _canonical_file_identity(resolved, invalid_label="runtime library")
        if identity["digest"] != item["sha256"]:
            raise ContractError(
                "operational check runtime library digest does not match"
            )
        try:
            total_bytes += resolved.stat().st_size
        except OSError as exc:
            raise ContractError(
                "operational check runtime libraries are invalid"
            ) from exc
        if total_bytes > MAX_RUNTIME_LIBRARY_BYTES:
            raise ContractError("operational check runtime libraries exceed their bounds")
        names.add(load_name)
        identities.append(
            {
                "configured": str(path),
                "resolved": str(resolved),
                "sha256": identity["digest"],
                "name": load_name,
            }
        )
    return sorted(identities, key=lambda value: value["name"])


def _check_executable_identity(root: Path, configured: str) -> dict[str, Any]:
    executable = Path(configured)
    if not executable.is_absolute() and not configured.startswith("./"):
        raise ContractError(
            "operational check executable must be absolute or project-relative"
        )
    path = executable if executable.is_absolute() else root / executable
    if not executable.is_absolute() and _project_path_has_symlink(root, path):
        raise ContractError("operational check executable is invalid")
    executable_identity = _canonical_file_identity(path, invalid_label="executable")
    resolved = Path(executable_identity["resolved"])
    if (
        resolved.name in FORBIDDEN_CHECK_EXECUTABLE_NAMES
    ):
        raise ContractError("operational check executable is invalid")
    identity: dict[str, Any] = {
        "configured": str(path),
        "resolved": str(resolved),
        "digest": executable_identity["digest"],
        "interpreter": None,
    }
    first_line = executable_identity["first_line"].encode(
        "utf-8", errors="surrogateescape"
    )
    if first_line.startswith(b"#!"):
        try:
            interpreter_parts = first_line[2:].decode("utf-8").strip().split()
        except (UnicodeError, IndexError) as exc:
            raise ContractError("operational check interpreter is invalid") from exc
        if len(interpreter_parts) != 1:
            raise ContractError("operational check interpreter is invalid")
        interpreter_text = interpreter_parts[0]
        interpreter = Path(interpreter_text)
        if not interpreter.is_absolute():
            raise ContractError("operational check interpreter is invalid")
        interpreter_identity = _canonical_file_identity(
            interpreter, invalid_label="interpreter"
        )
        if (
            Path(interpreter_identity["resolved"]).name
            in FORBIDDEN_CHECK_EXECUTABLE_NAMES
            or interpreter_identity["first_line"].startswith("#!")
        ):
            raise ContractError("operational check interpreter is invalid")
        interpreter_identity.pop("first_line")
        identity["interpreter"] = interpreter_identity
    return identity


def _validate_check_driver(
    driver: str, argv: list[str], identity: dict[str, Any]
) -> None:
    """Allow only command shapes whose code-loading behavior is understood."""

    interpreter = identity["interpreter"]
    work_token_positions = [
        index for index, item in enumerate(argv)
        if item == QA_WORK_DIRECTORY_TOKEN
    ]
    if work_token_positions and (
        driver != "python-wrapper"
        or len(work_token_positions) != 1
        or work_token_positions[0] == 0
        or argv[work_token_positions[0] - 1] != "--basetemp"
    ):
        raise ContractError("operational check driver is invalid")
    if driver == "python-wrapper":
        if (
            interpreter is None
            or not PYTHON_EXECUTABLE_NAME.fullmatch(
                Path(interpreter["resolved"]).name
            )
        ):
            raise ContractError("operational check driver is invalid")
        return
    if driver == "ruff":
        arguments = argv[1:]
        if arguments[:3] != ["check", "--isolated", "--no-cache"]:
            raise ContractError("operational check driver is invalid")
        if arguments[3:5] == ["--select", RUFF_QA_RULES]:
            operands = arguments[5:]
        else:
            operands = arguments[3:]
        if (
            interpreter is not None
            or Path(identity["resolved"]).name != "ruff"
            or not operands
            or any(
                operand != "."
                and (
                    operand.startswith(("-", "@"))
                    or Path(operand).is_absolute()
                    or ".." in Path(operand).parts
                )
                for operand in operands
            )
        ):
            raise ContractError("operational check driver is invalid")
        return
    raise ContractError("operational check driver is invalid")


def _snapshot_file(path: Path, expected_digest: str, target: Path) -> None:
    """Copy the exact opened bytes that match an owner-approved digest."""

    descriptor = _open_regular_no_follow(path)
    output = -1
    try:
        output = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o500,
        )
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            hasher.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                view = view[written:]
        os.fsync(output)
        if hasher.hexdigest() != expected_digest:
            raise ContractError("operational check executable identity changed")
    except OSError as exc:
        raise ContractError("operational check snapshot failed") from exc
    except ContractError:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
        if output >= 0:
            os.close(output)


def _snapshot_runtime_libraries(
    temporary: Path, runtime_libraries: list[dict[str, str]]
) -> None:
    if not runtime_libraries:
        return
    destination = temporary / "runtime-libraries"
    destination.mkdir(mode=0o700)
    for library in runtime_libraries:
        _snapshot_file(
            Path(library["resolved"]),
            library["sha256"],
            destination / library["name"],
        )
    if sum(path.stat().st_size for path in destination.iterdir()) > MAX_RUNTIME_LIBRARY_BYTES:
        raise ContractError("operational check runtime libraries exceed their bounds")
    destination.chmod(0o500)


def _snapshot_check_argv(
    temporary: Path,
    argv: list[str],
    identity: dict[str, Any],
    runtime_tree: dict[str, str] | None,
    runtime_libraries: list[dict[str, str]],
) -> list[str]:
    executable = temporary / "approved-check"
    _snapshot_file(Path(identity["resolved"]), identity["digest"], executable)
    _snapshot_runtime_libraries(temporary, runtime_libraries)
    interpreter = identity["interpreter"]
    if interpreter is None:
        return [str(executable), *argv[1:]]
    if runtime_tree is None:
        runtime = temporary / "approved-runtime"
        _snapshot_file(Path(interpreter["resolved"]), interpreter["digest"], runtime)
        return [str(runtime), str(executable), *argv[1:]]
    environment = temporary / "runtime-environment"
    runtime = environment / "bin" / "python"
    runtime.parent.mkdir(parents=True, mode=0o700)
    _snapshot_file(Path(interpreter["resolved"]), interpreter["digest"], runtime)
    packages = (
        environment / "lib" / runtime_tree["python_directory"] / "site-packages"
    )
    digest = _copy_and_digest_runtime_tree(
        Path(runtime_tree["resolved"]), packages
    )
    if digest != runtime_tree["sha256"]:
        raise ContractError("operational check runtime tree identity changed")
    home = Path(interpreter["resolved"]).parent
    config = environment / "pyvenv.cfg"
    config.write_text(
        f"home = {home}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    config.chmod(0o400)
    runtime.parent.chmod(0o500)
    environment.chmod(0o500)
    return [str(runtime), str(executable), *argv[1:]]


class _SnapshotGuard:
    """Retain vnode watches so same-user tampering invalidates the QA result."""

    def __init__(self, root: Path) -> None:
        if sys.platform != "darwin" or not hasattr(select, "kqueue"):
            raise ContractError("operational check snapshot guard is unavailable")
        self._queue = select.kqueue()
        self._descriptors: list[int] = []
        event_only = getattr(os, "O_EVTONLY", os.O_RDONLY)
        notes = (
            select.KQ_NOTE_DELETE
            | select.KQ_NOTE_EXTEND
            | select.KQ_NOTE_LINK
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_REVOKE
            | select.KQ_NOTE_WRITE
        )
        try:
            paths = [root]
            for current, directories, files in os.walk(root, topdown=True):
                directories.sort()
                files.sort()
                paths.extend(Path(current) / name for name in directories)
                paths.extend(Path(current) / name for name in files)
            for path in paths:
                descriptor = os.open(path, event_only | os.O_NOFOLLOW)
                mode = os.fstat(descriptor).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    os.close(descriptor)
                    raise ContractError("operational check snapshot is invalid")
                event = select.kevent(
                    descriptor,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                    fflags=notes,
                )
                self._queue.control([event], 0, 0)
                self._descriptors.append(descriptor)
        except (OSError, ValueError) as exc:
            self.close()
            raise ContractError("operational check snapshot guard failed") from exc

    def changed(self) -> bool:
        try:
            return bool(
                self._queue.control(None, max(1, len(self._descriptors)), 0)
            )
        except OSError as exc:
            raise ContractError("operational check snapshot guard failed") from exc

    def close(self) -> None:
        for descriptor in self._descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._descriptors = []
        try:
            self._queue.close()
        except (AttributeError, OSError):
            pass


def _verify_check_snapshot(
    temporary: Path,
    identity: dict[str, Any],
    runtime_tree: dict[str, str] | None,
    runtime_libraries: list[dict[str, str]],
) -> None:
    def names(path: Path) -> set[str]:
        try:
            return {item.name for item in path.iterdir()}
        except OSError as exc:
            raise ContractError(
                "operational check snapshot identity changed"
            ) from exc

    executable = _canonical_file_identity(
        temporary / "approved-check", invalid_label="snapshot"
    )
    if executable["digest"] != identity["digest"]:
        raise ContractError("operational check snapshot identity changed")
    library_directory = temporary / "runtime-libraries"
    expected_top_level = {"approved-check"}
    if runtime_libraries:
        expected_top_level.add("runtime-libraries")
        if names(library_directory) != {
            library["name"] for library in runtime_libraries
        }:
            raise ContractError("operational check snapshot identity changed")
        for library in runtime_libraries:
            current = _canonical_file_identity(
                library_directory / library["name"], invalid_label="snapshot"
            )
            if current["digest"] != library["sha256"]:
                raise ContractError("operational check snapshot identity changed")
    interpreter = identity["interpreter"]
    if interpreter is None:
        if names(temporary) != expected_top_level:
            raise ContractError("operational check snapshot identity changed")
        return
    runtime = (
        temporary / "approved-runtime"
        if runtime_tree is None
        else temporary / "runtime-environment" / "bin" / "python"
    )
    runtime_identity = _canonical_file_identity(runtime, invalid_label="snapshot")
    if runtime_identity["digest"] != interpreter["digest"]:
        raise ContractError("operational check snapshot identity changed")
    if runtime_tree is None:
        if names(temporary) != expected_top_level | {"approved-runtime"}:
            raise ContractError("operational check snapshot identity changed")
        return
    environment = temporary / "runtime-environment"
    python_directory = runtime_tree["python_directory"]
    if (
        names(temporary) != expected_top_level | {"runtime-environment"}
        or names(environment) != {"bin", "lib", "pyvenv.cfg"}
        or names(environment / "bin") != {"python"}
        or names(environment / "lib") != {python_directory}
        or names(environment / "lib" / python_directory) != {"site-packages"}
    ):
        raise ContractError("operational check snapshot identity changed")
    packages = (
        environment
        / "lib"
        / python_directory
        / "site-packages"
    )
    if _copy_and_digest_runtime_tree(packages) != runtime_tree["sha256"]:
        raise ContractError("operational check snapshot identity changed")
    expected_config = (
        f"home = {Path(interpreter['resolved']).parent}\n"
        "include-system-site-packages = false\n"
    ).encode()
    config = environment / "pyvenv.cfg"
    try:
        if config.read_bytes() != expected_config:
            raise ContractError("operational check snapshot identity changed")
    except OSError as exc:
        raise ContractError("operational check snapshot identity changed") from exc


def _homebrew_runtime_paths(
    identity: dict[str, Any],
) -> tuple[list[str], list[str]]:
    cellar = HOMEBREW_CELLAR_ROOT
    roots: set[str] = set()
    traversal: set[str] = set()
    for item in (identity, identity.get("interpreter")):
        if item is None:
            continue
        try:
            relative = Path(item["resolved"]).relative_to(cellar)
        except (KeyError, TypeError, ValueError):
            continue
        if len(relative.parts) >= 2:
            roots.add(str(cellar / relative.parts[0] / relative.parts[1]))
        current = Path(item["resolved"])
        while current != cellar:
            traversal.add(str(current))
            current = current.parent
    return sorted(roots), sorted(traversal)


def _sandboxed_check_argv(
    root: Path,
    temporary: Path,
    work: Path,
    argv: list[str],
    identity: dict[str, Any],
    driver: str,
    runtime_library_directory: Path | None,
) -> list[str]:
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.is_file() or sandbox.is_symlink():
        raise ContractError("operational check sandbox is unavailable")
    homebrew_roots, homebrew_traversal = _homebrew_runtime_paths(identity)
    temporary_traversal = [
        str(path)
        for path in reversed(temporary.parents)
        if str(path) != "/"
    ]
    project_traversal = [
        str(path) for path in reversed(root.parents) if str(path) != "/"
    ]
    profile = "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            *(["(allow sysctl-read)"] if driver == "ruff" else []),
            "(allow file-read-metadata",
            *(
                f"  (literal {json.dumps(path)})"
                for path in project_traversal
            ),
            ")",
            "(allow file-read*",
            f"  (subpath {json.dumps(str(root))})",
            f"  (subpath {json.dumps(str(temporary))})",
            *(
                f"  (literal {json.dumps(path)})"
                for path in temporary_traversal
            ),
            '  (literal "/")',
            '  (literal "/tmp")',
            '  (subpath "/System")',
            '  (subpath "/Library")',
            '  (subpath "/usr")',
            '  (subpath "/bin")',
            '  (subpath "/sbin")',
            '  (literal "/opt")',
            '  (literal "/opt/homebrew")',
            '  (literal "/opt/homebrew/Cellar")',
            *(
                f"  (subpath {json.dumps(path)})"
                for path in homebrew_roots
            ),
            *(
                f"  (literal {json.dumps(path)})"
                for path in homebrew_traversal
            ),
            '  (subpath "/private/etc")',
            '  (literal "/dev/null")',
            '  (literal "/dev/urandom"))',
            f"(allow network-bind (subpath {json.dumps(str(work))}))",
            f"(allow network-outbound (subpath {json.dumps(str(work))}))",
            "(allow file-write*",
            '  (literal "/dev/null")',
            f"  (subpath {json.dumps(str(work))}))",
        ]
    )
    command = [str(sandbox), "-p", profile]
    if runtime_library_directory is not None:
        command.extend(
            [
                "/usr/bin/env",
                f"DYLD_LIBRARY_PATH={runtime_library_directory}",
            ]
        )
    return [*command, *argv]


def operational_sandbox_preflight() -> bool:
    """Prove that this process may create the required nested Seatbelt sandbox."""

    sandbox = Path("/usr/bin/sandbox-exec")
    executable = Path("/usr/bin/true")
    if (
        sys.platform != "darwin"
        or not sandbox.is_file()
        or sandbox.is_symlink()
        or not executable.is_file()
        or executable.is_symlink()
    ):
        return False
    profile = (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec)\n"
        "(allow file-read*)"
    )
    try:
        completed = subprocess.run(
            [str(sandbox), "-p", profile, str(executable)],
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _qa_resource_limiter(timeout_seconds: int) -> Callable[[], None]:
    def apply() -> None:
        file_bytes = min(
            MAX_QA_FILE_BYTES, MAX_QA_TEMP_BYTES // MAX_QA_OPEN_FILES
        )
        limits = {
            resource.RLIMIT_CORE: 0,
            resource.RLIMIT_CPU: max(5, min(timeout_seconds, 600)),
            resource.RLIMIT_FSIZE: file_bytes,
            resource.RLIMIT_NOFILE: MAX_QA_OPEN_FILES,
        }
        for kind, requested in limits.items():
            _soft, hard = resource.getrlimit(kind)
            value = min(requested, hard)
            resource.setrlimit(kind, (value, value))

    return apply


class _TransientQATreeScan(Exception):
    """A listed entry disappeared or changed during a temporary-tree scan."""


def _bounded_tree_size(
    root: Path, *, allowed_symlink_root: Path | None = None
) -> int:
    resolved_root = root.resolve(strict=True)
    if allowed_symlink_root is not None:
        try:
            allowed_relative = allowed_symlink_root.relative_to(resolved_root)
        except ValueError as exc:
            raise ContractError(
                "operational check temporary storage is invalid"
            ) from exc
        if (
            not allowed_relative.parts
            or any(part in {"", ".", ".."} for part in allowed_relative.parts)
        ):
            raise ContractError("operational check temporary storage is invalid")

    def entry_changed(
        name: str, descriptor: int, metadata: os.stat_result
    ) -> bool:
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return (
            current.st_dev, current.st_ino, current.st_mode, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        ) != (
            metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )

    def listed_stat(name: str, descriptor: int) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            # The entry was listed by fwalk but was absent at this stat. A
            # successful recheck means it reappeared; either way, rescan.
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError as confirmed:
                raise _TransientQATreeScan from confirmed
            raise _TransientQATreeScan from exc

    def checked_readlink(
        name: str, descriptor: int, metadata: os.stat_result
    ) -> str:
        try:
            return os.readlink(name, dir_fd=descriptor)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.EINVAL}:
                try:
                    if entry_changed(name, descriptor, metadata):
                        raise _TransientQATreeScan from exc
                except OSError:
                    pass
            raise

    def internal_symlink_bytes(
        current: Path, name: str, descriptor: int, metadata: os.stat_result
    ) -> int:
        if allowed_symlink_root is None:
            raise ContractError("operational check temporary storage is invalid")
        link_path = current / name
        try:
            link_path.relative_to(allowed_symlink_root)
            target_text = checked_readlink(name, descriptor, metadata)
            target = Path(target_text)
            if not target.is_absolute():
                raise ValueError
            relative = target.relative_to(resolved_root)
            target.relative_to(allowed_symlink_root)
            if (
                not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError
            components = (*resolved_root.parts[1:], *relative.parts)
            target_descriptor = os.open(
                "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                for part in components[:-1]:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=target_descriptor,
                    )
                    os.close(target_descriptor)
                    target_descriptor = child
                child = os.open(
                    components[-1],
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=target_descriptor,
                )
                os.close(target_descriptor)
                target_descriptor = child
                target_mode = os.fstat(target_descriptor).st_mode
                if not (
                    stat.S_ISDIR(target_mode) or stat.S_ISREG(target_mode)
                ):
                    raise ValueError
            finally:
                os.close(target_descriptor)
            after = listed_stat(name, descriptor)
            if (
                checked_readlink(name, descriptor, metadata) != target_text
                or (
                    after.st_dev, after.st_ino, after.st_mode, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns,
                ) != (
                    metadata.st_dev, metadata.st_ino, metadata.st_mode,
                    metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
                )
            ):
                raise _TransientQATreeScan
        except _TransientQATreeScan:
            raise
        except (OSError, ValueError) as exc:
            raise ContractError(
                "operational check temporary storage is invalid"
            ) from exc
        return metadata.st_size

    byte_count = 0
    entry_count = 0
    for current_text, directories, files, descriptor in os.fwalk(
        root, topdown=True, follow_symlinks=False
    ):
        directories.sort()
        files.sort()
        entry_count += len(directories) + len(files)
        if entry_count > MAX_QA_TEMP_ENTRIES:
            raise ContractError("operational check temporary storage exceeds its bounds")
        current = Path(current_text)
        for name in directories:
            metadata = listed_stat(name, descriptor)
            if stat.S_ISLNK(metadata.st_mode):
                byte_count += internal_symlink_bytes(
                    current, name, descriptor, metadata
                )
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ContractError("operational check temporary storage is invalid")
        for name in files:
            metadata = listed_stat(name, descriptor)
            if stat.S_ISSOCK(metadata.st_mode):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                byte_count += internal_symlink_bytes(
                    current, name, descriptor, metadata
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ContractError("operational check temporary storage is invalid")
            byte_count += metadata.st_size
        if byte_count > MAX_QA_TEMP_BYTES:
            raise ContractError(
                "operational check temporary storage exceeds its bounds"
            )
    return byte_count


def _wait_for_qa_process(
    process: subprocess.Popen[bytes], *, work: Path, timeout_seconds: int,
    allowed_symlink_root: Path | None = None,
) -> tuple[str, int | None]:
    deadline = time.monotonic() + timeout_seconds
    transient_scans = 0
    retrying = False
    while True:
        exited = process.poll() is not None
        if retrying and time.monotonic() >= deadline:
            if exited:
                raise ContractError("operational check temporary storage is unstable")
            return "timeout", None
        try:
            _bounded_tree_size(
                work, allowed_symlink_root=allowed_symlink_root
            )
        except _TransientQATreeScan as exc:
            transient_scans += 1
            if transient_scans > MAX_QA_TRANSIENT_TREE_SCANS:
                raise ContractError(
                    "operational check temporary storage is unstable"
                ) from exc
            retrying = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if exited:
                    raise ContractError(
                        "operational check temporary storage is unstable"
                    ) from exc
                return "timeout", None
            time.sleep(min(0.02, remaining))
            continue
        if retrying and time.monotonic() >= deadline:
            if exited:
                raise ContractError("operational check temporary storage is unstable")
            return "timeout", None
        transient_scans = 0
        retrying = False
        if exited:
            return (
                "passed" if process.returncode == 0 else "failed",
                process.returncode,
            )
        if time.monotonic() >= deadline:
            return "timeout", None
        time.sleep(0.02)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        if process.poll() is not None:
            break
        try:
            os.killpg(process.pid, signal_number)
        except OSError:
            pass
        try:
            process.send_signal(signal_number)
        except OSError:
            pass
        if signal_number == signal.SIGTERM:
            time.sleep(0.05)
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ContractError("operational check process cleanup failed") from exc
    if process.poll() is None:
        raise ContractError("operational check process cleanup failed")


def validate_operational_check_configuration_input(
    project_root: Path, raw: bytes
) -> dict[str, list[dict[str, Any]]]:
    """Validate explicit check input without treating it as committed authority."""

    root = project_root.resolve(strict=True)
    if len(raw) > 65_536:
        raise ContractError("operational check configuration is too large")
    try:
        config = parse_bounded_json_value(raw, max_bytes=65_536)
    except ContractError as exc:
        raise ContractError("operational check configuration is invalid") from exc
    if not isinstance(config, dict) or set(config) != {
        "version", "deterministic_checks", "security_checks"
    } or config["version"] != 1:
        raise ContractError("operational check configuration is invalid")

    seen: set[str] = set()
    normalized: dict[str, list[dict[str, Any]]] = {}
    for group in ("deterministic_checks", "security_checks"):
        checks = config[group]
        if not isinstance(checks, list) or not checks or len(checks) > 32:
            raise ContractError("operational check group must be non-empty and bounded")
        normalized[group] = []
        for check in checks:
            required_fields = {
                "check_id", "driver", "argv", "executable_sha256",
                "interpreter_sha256", "runtime_tree", "timeout_seconds"
            }
            allowed_fields = required_fields | {"runtime_libraries"}
            if (
                not isinstance(check, dict)
                or not required_fields.issubset(check)
                or not set(check).issubset(allowed_fields)
            ):
                raise ContractError("operational check definition is invalid")
            check_id = check["check_id"]
            driver = check["driver"]
            argv = check["argv"]
            executable_sha256 = check["executable_sha256"]
            interpreter_sha256 = check["interpreter_sha256"]
            runtime_tree = check["runtime_tree"]
            runtime_libraries = check.get("runtime_libraries", [])
            timeout_seconds = check["timeout_seconds"]
            if (
                not isinstance(check_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", check_id)
                or check_id in seen
                or not isinstance(argv, list)
                or not 1 <= len(argv) <= 32
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > 1_024
                    or "\x00" in item
                    for item in argv
                )
                or not isinstance(executable_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", executable_sha256)
                or not (
                    interpreter_sha256 is None
                    or isinstance(interpreter_sha256, str)
                    and re.fullmatch(r"[0-9a-f]{64}", interpreter_sha256)
                )
                or not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or not 1 <= timeout_seconds <= 1_800
            ):
                raise ContractError("operational check definition is invalid")
            seen.add(check_id)
            identity = _check_executable_identity(root, argv[0])
            _validate_check_driver(driver, argv, identity)
            if identity["digest"] != executable_sha256:
                raise ContractError("operational check executable digest does not match")
            interpreter = identity["interpreter"]
            if (
                interpreter is None and interpreter_sha256 is not None
                or interpreter is not None
                and interpreter["digest"] != interpreter_sha256
            ):
                raise ContractError("operational check interpreter digest does not match")
            runtime_identity = _runtime_tree_identity(
                root, runtime_tree, interpreter=interpreter
            )
            runtime_library_identities = _runtime_library_identities(
                runtime_libraries,
                driver=driver,
                interpreter=interpreter,
            )
            normalized[group].append(
                {
                    "check_id": check_id,
                    "driver": driver,
                    "argv": list(argv),
                    "executable_sha256": executable_sha256,
                    "interpreter_sha256": interpreter_sha256,
                    "runtime_tree": runtime_identity,
                    "runtime_libraries": runtime_library_identities,
                    "executable_identity": identity,
                    "timeout_seconds": timeout_seconds,
                }
            )
    return normalized


def load_operational_check_configuration(
    project_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Load checks only from a committed, clean file in the exact project root."""

    root = project_root.resolve(strict=True)
    config_path = root / ".codex" / "orchestration" / "operational-checks.json"
    descriptor = -1
    try:
        descriptor = _open_regular_no_follow(config_path)
        chunks: list[bytes] = []
        byte_count = 0
        while chunk := os.read(descriptor, 65_537 - byte_count):
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > 65_536:
                raise ContractError("operational check configuration is too large")
        raw = b"".join(chunks)
    except (ContractError, OSError) as exc:
        raise ContractError("operational check configuration is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    git_executable = find_git(untrusted=[root])
    if git_executable is None:
        raise ContractError("operational check configuration trust is unavailable")
    if repository_layout_problem(git_executable, root) is not None:
        raise ContractError("operational check configuration is not committed and clean")
    # Only plumbing: the committed blob is compared with the bytes read above, so no
    # command refreshes the index or runs a helper the repository configures.
    try:
        top = run_git(git_executable, root, "rev-parse", "--show-toplevel", text=True)
        head_before = run_git(
            git_executable, root, "rev-parse", "--verify", "HEAD^{commit}", text=True
        )
        commit = head_before.stdout.strip()
        config_ref = f"{commit}:.codex/orchestration/operational-checks.json"
        blob_size = run_git(git_executable, root, "cat-file", "-s", config_ref, text=True)
        try:
            committed_size = int(blob_size.stdout.strip())
        except ValueError:
            committed_size = -1
        committed = (
            run_git(git_executable, root, "cat-file", "blob", config_ref)
            if 0 <= committed_size <= 65_536 else None
        )
        head_after = run_git(
            git_executable, root, "rev-parse", "--verify", "HEAD^{commit}", text=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("operational check configuration trust is unavailable") from exc
    try:
        exact_root = Path(top.stdout.strip()).resolve(strict=True) == root
    except OSError:
        exact_root = False
    if (
        top.returncode != 0
        or not exact_root
        or head_before.returncode != 0
        or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
        or blob_size.returncode != 0
        or committed is None
        or committed.returncode != 0
        or committed.stdout != raw
        or head_after.returncode != 0
        or head_after.stdout.strip() != commit
    ):
        raise ContractError("operational check configuration is not committed and clean")
    normalized = validate_operational_check_configuration_input(root, raw)
    for check in (
        *normalized["deterministic_checks"],
        *normalized["security_checks"],
    ):
        executable = check["argv"][0]
        if not executable.startswith("./"):
            continue
        relative = executable.removeprefix("./")
        if not _committed_and_clean(git_executable, root, commit, relative):
            raise ContractError("operational check executable is not committed and clean")
    return normalized


MAX_COMMITTED_EXECUTABLE_BYTES = 16 * 1024 * 1024


def _committed_and_clean(git_executable: str, root: Path, commit: str, relative: str) -> bool:
    """Whether a working-tree file equals its blob and mode in `commit`, by plumbing only.

    The file is opened without following any component; its bytes and executable
    bit must match the committed regular blob exactly.
    """

    try:
        listing = run_git(git_executable, root, "ls-tree", "-z", commit, "--", relative)
        entries = [item for item in listing.stdout.split(b"\x00") if item]
        if listing.returncode != 0 or len(entries) != 1:
            return False
        header, _, name = entries[0].partition(b"\t")
        mode, kind, object_id = header.decode("ascii").split(" ")
        if name.decode("utf-8") != relative or kind != "blob":
            return False
        if mode not in {"100644", "100755"}:
            return False
        size = run_git(git_executable, root, "cat-file", "-s", object_id, text=True)
        if size.returncode != 0 or int(size.stdout.strip()) > MAX_COMMITTED_EXECUTABLE_BYTES:
            return False
        blob = run_git(git_executable, root, "cat-file", "blob", object_id)
        if blob.returncode != 0:
            return False
        descriptor = _open_regular_no_follow(root / relative)
    except (ContractError, OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return False
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size != len(blob.stdout):
            return False
        if bool(metadata.st_mode & stat.S_IXUSR) != (mode == "100755"):
            return False
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
            total += len(chunk)
            if total > len(blob.stdout):
                return False
        return b"".join(chunks) == blob.stdout
    finally:
        os.close(descriptor)


def run_operational_check_suite(project_root: Path, *, scratch_root: Path) -> dict[str, Any]:
    """Run the exact project-approved QA commands without a shell or inherited secrets.

    Each check's sealed snapshot and work directory are a fresh owner-only
    directory under `scratch_root` (the learning state), never the system temp
    directory, which a sandboxed Codex primary could write and race.
    """

    if not operational_sandbox_preflight():
        raise ContractError(
            "operational check sandbox preflight failed; run through an approved "
            "unsandboxed command boundary"
        )
    root = project_root.resolve(strict=True)
    normalized = load_operational_check_configuration(root)

    environment = {
        "PATH": os.defpath,
        # A check that reaches a macOS xcrun shim (/usr/bin/python3, git, ...)
        # must not read the shim's lookup cache.
        **XCRUN_NO_CACHE,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    report: dict[str, Any] = {
        "deterministic_checks": [],
        "security_checks": [],
        "observed_at_ms": 0,
    }
    for group, checks in normalized.items():
        for check in checks:
            status = "failed"
            return_code: int | None = None
            process: subprocess.Popen[bytes] | None = None
            try:
                temporary = private_directory(scratch_root, "scratch", "qa-")
            except HostPathError as exc:
                raise ContractError("a private operational check directory is unavailable") from exc
            try:
                sealed = temporary / "sealed"
                work = temporary / "work"
                sealed.mkdir(mode=0o700)
                work.mkdir(mode=0o700)
                check_environment = {**environment, "TMPDIR": str(work)}
                guard: _SnapshotGuard | None = None
                try:
                    effective_argv = [
                        str(work / "pytest")
                        if item == QA_WORK_DIRECTORY_TOKEN else item
                        for item in check["argv"]
                    ]
                    snapshot_argv = _snapshot_check_argv(
                        sealed,
                        effective_argv,
                        check["executable_identity"],
                        check["runtime_tree"],
                        check["runtime_libraries"],
                    )
                    sealed.chmod(0o500)
                    guard = _SnapshotGuard(sealed)
                    _verify_check_snapshot(
                        sealed,
                        check["executable_identity"],
                        check["runtime_tree"],
                        check["runtime_libraries"],
                    )
                    if guard.changed():
                        raise ContractError(
                            "operational check snapshot identity changed"
                        )
                    command = _sandboxed_check_argv(
                        root,
                        temporary,
                        work,
                        snapshot_argv,
                        check["executable_identity"],
                        check["driver"],
                        sealed / "runtime-libraries"
                        if check["runtime_libraries"] else None,
                    )
                    process = subprocess.Popen(
                        command,
                        cwd=root,
                        env=check_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        preexec_fn=_qa_resource_limiter(  # noqa: PLW1509
                            check["timeout_seconds"]
                        ),
                    )
                    status, return_code = _wait_for_qa_process(
                        process,
                        work=work,
                        timeout_seconds=check["timeout_seconds"],
                        allowed_symlink_root=(
                            work / "pytest"
                            if QA_WORK_DIRECTORY_TOKEN in check["argv"] else None
                        ),
                    )
                except (ContractError, OSError, subprocess.SubprocessError):
                    status = "unavailable"
                finally:
                    cleanup_error: ContractError | None = None
                    if process is not None:
                        try:
                            _terminate_process_group(process)
                        except ContractError as exc:
                            cleanup_error = exc
                            status = "unavailable"
                            return_code = None
                    if guard is not None:
                        try:
                            changed = guard.changed()
                        except ContractError:
                            changed = True
                        if changed:
                            status = "unavailable"
                            return_code = None
                        guard.close()
                    if cleanup_error is not None:
                        raise cleanup_error
            finally:
                remove_private_directory(temporary)
            evidence_digest = content_digest(
                {
                    "check_id": check["check_id"],
                    "argv_digest": content_digest(check["argv"]),
                    "executable_identity_digest": content_digest(
                        {
                            "executable_sha256": check["executable_sha256"],
                            "driver": check["driver"],
                            "interpreter_sha256": check["interpreter_sha256"],
                            "runtime_tree": check["runtime_tree"],
                            "runtime_libraries": check["runtime_libraries"],
                        }
                    ),
                    "status": status,
                    "return_code": return_code,
                }
            )
            report[group].append(
                {
                    "check_id": check["check_id"],
                    "passed": status == "passed",
                    "evidence_digest": evidence_digest,
                }
            )
    report["observed_at_ms"] = time.time_ns() // 1_000_000
    _check_report_digests(report["deterministic_checks"], "deterministic_checks")
    _check_report_digests(report["security_checks"], "security_checks")
    return report


def build_verified_qa_records(
    *,
    digest_key: bytes,
    host_outcome: dict[str, Any],
    verifier_host_outcome: dict[str, Any],
    evaluation_assignment: dict[str, Any],
    evaluation_case: dict[str, Any],
    evaluator_assignment: dict[str, Any],
    evaluator_result: dict[str, Any],
    check_report: dict[str, Any],
    verifier_assignment: dict[str, Any],
    verifier_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive verified QA from a durable host run and closed check contracts."""

    host = validate_outcome(host_outcome)
    verifier_host = validate_outcome(verifier_host_outcome)
    assignment_record = validate_evaluation_assignment(evaluation_assignment)
    case = validate_evaluation_case(evaluation_case)
    evaluator = validate_assignment_shape(evaluator_assignment)
    evaluator_output = validate_result(evaluator_result, assignment=evaluator)
    verifier = validate_assignment_shape(verifier_assignment)
    verifier_output = validate_result(verifier_result, assignment=verifier)
    if not isinstance(check_report, dict) or set(check_report) != {
        "deterministic_checks", "security_checks", "observed_at_ms"
    }:
        raise ContractError("QA check report must use the closed field set")
    observed_at_ms = check_report["observed_at_ms"]
    if (
        not isinstance(observed_at_ms, int)
        or isinstance(observed_at_ms, bool)
        or observed_at_ms < max(
            host["observed_at_ms"], verifier_host["observed_at_ms"]
        )
    ):
        raise ContractError("QA check time is invalid")
    deterministic_digests, deterministic_pass = _check_report_digests(
        check_report["deterministic_checks"], "deterministic_checks"
    )
    security_digests, security_pass = _check_report_digests(
        check_report["security_checks"], "security_checks"
    )
    planned = [
        item for item in assignment_record["trial_plan"]
        if item["trial_id"] == evaluator["assignment_id"]
    ]
    if len(planned) != 1:
        raise ContractError("host result is not a pre-assigned evaluation trial")
    plan = planned[0]
    case_digest = content_digest(case)
    if (
        plan["case_id"] != case["record_id"]
        or plan["configuration_digest"] != host["evaluation_candidate_digest"]
        or host["evaluation_case_digest"] != case_digest
        or host["assignment_id"] != evaluator["assignment_id"]
        or host["task_slice"] != assignment_record["task_slice"]
        or host["task_slice"] != case["task_slice"]
        or host["client_digest"] != assignment_record["client_digest"]
        or any(
            evaluator[field] != host[field]
            for field in ("model", "effort", "role")
        )
    ):
        raise ContractError("QA evidence is not bound to the durable assigned run")
    evaluator_result_digest = keyed_digest(digest_key, canonical_json(evaluator_output))
    evaluator_assignment_digest = keyed_digest(
        digest_key, canonical_json(evaluator)
    )
    if (
        evaluator_result_digest not in host["evidence_digests"]
        or evaluator_assignment_digest not in host["evidence_digests"]
    ):
        raise ContractError("evaluator result does not match the admitted host result")
    evaluator_criteria = {item["id"] for item in evaluator["required_evidence"]}
    verifier_criteria = {item["id"] for item in verifier["required_evidence"]}
    if (
        verifier["role"] != "codex_agents_verification_worker"
        or verifier["assignment_id"] == evaluator["assignment_id"]
        or verifier_criteria != evaluator_criteria
        or verifier["context"] != [content_digest(host)]
        or verifier_host["run_id"] == host["run_id"]
        or verifier_host["assignment_id"] != verifier["assignment_id"]
        or verifier_host["role"] != "codex_agents_verification_worker"
        or any(
            verifier[field] != verifier_host[field]
            for field in ("model", "effort", "role")
        )
    ):
        raise ContractError("independent verifier assignment is not bound to the host outcome")
    verifier_result_digest = keyed_digest(
        digest_key, canonical_json(verifier_output)
    )
    verifier_assignment_digest = keyed_digest(
        digest_key, canonical_json(verifier)
    )
    if (
        verifier_result_digest not in verifier_host["evidence_digests"]
        or verifier_assignment_digest not in verifier_host["evidence_digests"]
    ):
        raise ContractError("verifier result does not match its admitted host result")

    def assessment(result: dict[str, Any]) -> tuple[set[str], bool, str, dict[str, str]]:
        axes = result_axes(result)
        outcomes = {
            item["criterion_id"]: (
                "positive" if item["outcome"] in {"pass", "observed"}
                else item["outcome"]
            )
            for item in result["evidence"]
        }
        passed = {
            criterion for criterion, outcome in outcomes.items()
            if outcome == "positive"
        }
        positive = axes["semantic_verdict"] in {
            "pass", "ready", "ready-with-corrections"
        }
        complete = (
            axes["execution_state"] == "completed"
            and set(outcomes) == evaluator_criteria
            and not result["unverified_claims"]
            and (not positive or (passed == evaluator_criteria and not result["remaining_risks"]))
        )
        return passed, complete, axes["semantic_verdict"], outcomes

    evaluator_passed, evaluator_complete, evaluator_verdict, evaluator_outcomes = (
        assessment(evaluator_output)
    )
    verifier_passed, verifier_complete, verifier_verdict, verifier_outcomes = (
        assessment(verifier_output)
    )
    verifier_disagreement = (
        evaluator_complete != verifier_complete
        or evaluator_verdict != verifier_verdict
        or evaluator_outcomes != verifier_outcomes
        or host.get("semantic_verdict", "pass") != evaluator_verdict
        or verifier_host.get("semantic_verdict", "pass") != verifier_verdict
    )
    coverage = (
        "end_to_end_complete"
        if host["coverage"] == verifier_host["coverage"] == "end_to_end_complete"
        else "partial"
    )

    def combined_usage() -> dict[str, int | None]:
        combined: dict[str, int | None] = {}
        for field in host["usage"]:
            left = host["usage"][field]
            right = verifier_host["usage"][field]
            combined[field] = (
                int(left) + int(right)
                if left is not None and right is not None
                else None
            )
        return combined

    accepted = (
        host["status"] == "unverified"
        and verifier_host["status"] == "unverified"
        and evaluator_complete
        and verifier_complete
        and not verifier_disagreement
        and deterministic_pass
        and security_pass
    )
    qa_material = {
        "schema_version": 1,
        "record_type": "qa_evidence",
        "evaluation_assignment_digest": content_digest(assignment_record),
        "trial_id": evaluator["assignment_id"],
        "case_digest": case_digest,
        "host_outcome_digest": content_digest(host),
        "evaluator_assignment_digest": evaluator_assignment_digest,
        "evaluator_result_digest": evaluator_result_digest,
        "verifier_host_outcome_digest": content_digest(verifier_host),
        "verifier_assignment_digest": verifier_assignment_digest,
        "verifier_result_digest": verifier_result_digest,
        "deterministic_checks": deepcopy(check_report["deterministic_checks"]),
        "security_checks": deepcopy(check_report["security_checks"]),
        "deterministic_check_digests": deterministic_digests,
        "security_check_digests": security_digests,
        "deterministic_pass": deterministic_pass,
        "security_pass": security_pass,
        "required_criteria_total": len(evaluator_criteria),
        "required_criteria_passed": len(evaluator_passed & verifier_passed),
        "verifier_disagreement": verifier_disagreement,
        "accepted": accepted,
        "execution_state": result_axes(evaluator_output)["execution_state"],
        "semantic_verdict": evaluator_verdict,
        "observed_at_ms": observed_at_ms,
    }
    qa = validate_qa_evidence(
        {**qa_material, "record_id": f"qa_evidence:{content_digest(qa_material)}"}
    )
    if accepted:
        status = "accepted"
    elif host["status"] in {"failed", "cancelled", "interrupted"}:
        status = host["status"]
    else:
        status = "rejected"
    required_passed = len(evaluator_passed & verifier_passed)
    quality_micros = round(
        required_passed / len(evaluator_criteria) * 1_000_000
    )
    verified_usage = combined_usage()
    verified_usage_sources = {}
    for field, value in verified_usage.items():
        if value is None:
            verified_usage_sources[field] = {
                "source": "unavailable", "coverage": "unknown"
            }
            continue
        observed_sources = {
            outcome.get("usage_sources", {}).get(field, {}).get(
                "source", "host_observed"
            )
            for outcome in (host, verifier_host)
        }
        verified_usage_sources[field] = {
            "source": (
                observed_sources.pop()
                if len(observed_sources) == 1
                else "client_observed"
            ),
            "coverage": coverage,
        }
    outcome = validate_outcome(
        {
            **host,
            "record_id": external_id(
                digest_key,
                "out",
                f"verified\x00{host['record_id']}\x00{content_digest(qa)}",
            ),
            "observed_at_ms": observed_at_ms,
            "status": status,
            "execution_state": host.get(
                "execution_state",
                "completed" if host["status"] in {"accepted", "rejected", "unverified"}
                else host["status"],
            ),
            "semantic_verdict": evaluator_verdict,
            "admission_state": (
                status if status in {"accepted", "rejected", "unverified"}
                else "rejected"
            ),
            "retry_attempt": host.get("retry_attempt", 1),
            "retry_of_assignment_id": host.get("retry_of_assignment_id"),
            "deterministic_pass": deterministic_pass,
            "security_pass": security_pass,
            "required_criteria_total": len(evaluator_criteria),
            "required_criteria_passed": required_passed,
            "quality_micros": quality_micros,
            "terminal_failure": status in {"failed", "cancelled", "interrupted"},
            "verifier_disagreement": verifier_disagreement,
            "accepted": accepted,
            "coverage": coverage,
            "usage": verified_usage,
            "usage_sources": verified_usage_sources,
            "evidence_digests": sorted(
                {
                    *host["evidence_digests"],
                    content_digest(host),
                    content_digest(verifier_host),
                    content_digest(qa),
                }
            ),
        }
    )
    return qa, outcome


# Operational QA accepts the routed verifier of either provider: the policy's
# Codex verification role and its Claude verification role.
OPERATIONAL_VERIFIER_ROLES = frozenset({
    "codex_agents_verification_worker",
    "claude_agents_verification_worker",
})


def build_operational_qa_records(
    *,
    digest_key: bytes,
    host_outcome: dict[str, Any],
    verifier_host_outcome: dict[str, Any],
    evaluator_assignment: dict[str, Any],
    evaluator_result: dict[str, Any],
    check_report: dict[str, Any],
    verifier_assignment: dict[str, Any],
    verifier_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive routine automatic QA without granting release authority."""

    host = validate_outcome(host_outcome)
    verifier_host = validate_outcome(verifier_host_outcome)
    evaluator = validate_assignment_shape(evaluator_assignment)
    evaluator_output = validate_result(evaluator_result, assignment=evaluator)
    verifier = validate_assignment_shape(verifier_assignment)
    verifier_output = validate_result(verifier_result, assignment=verifier)
    if not isinstance(check_report, dict) or set(check_report) != {
        "deterministic_checks", "security_checks", "observed_at_ms"
    }:
        raise ContractError("operational QA check report must use the closed field set")
    observed_at_ms = check_report["observed_at_ms"]
    if (
        not isinstance(observed_at_ms, int)
        or isinstance(observed_at_ms, bool)
        or observed_at_ms < max(host["observed_at_ms"], verifier_host["observed_at_ms"])
    ):
        raise ContractError("operational QA check time is invalid")
    deterministic_digests, deterministic_pass = _check_report_digests(
        check_report["deterministic_checks"], "deterministic_checks"
    )
    security_digests, security_pass = _check_report_digests(
        check_report["security_checks"], "security_checks"
    )
    evaluator_assignment_digest = keyed_digest(
        digest_key, canonical_json(evaluator)
    )
    evaluator_result_digest = keyed_digest(
        digest_key, canonical_json(evaluator_output)
    )
    if (
        host["assignment_id"] != evaluator["assignment_id"]
        or any(evaluator[field] != host[field] for field in ("model", "effort", "role"))
        or evaluator_assignment_digest not in host["evidence_digests"]
        or evaluator_result_digest not in host["evidence_digests"]
    ):
        raise ContractError("operational QA is not bound to the assigned host run")
    evaluator_criteria = {item["id"] for item in evaluator["required_evidence"]}
    verifier_assignment_digest = keyed_digest(
        digest_key, canonical_json(verifier)
    )
    verifier_result_digest = keyed_digest(
        digest_key, canonical_json(verifier_output)
    )
    if (
        verifier["assignment_id"] == evaluator["assignment_id"]
        or verifier["required_evidence"] != evaluator["required_evidence"]
        or verifier["context"] != [content_digest(host)]
        or verifier["role"] not in OPERATIONAL_VERIFIER_ROLES
        or evaluator["verification_required"] is not True
        or verifier["verification_required"] is not False
        or not verifier_host["task_slice"].startswith("verification-")
        or verifier_host["run_id"] == host["run_id"]
        or verifier_host["assignment_id"] != verifier["assignment_id"]
        or any(
            verifier[field] != verifier_host[field]
            for field in ("model", "effort", "role")
        )
        or verifier_assignment_digest not in verifier_host["evidence_digests"]
        or verifier_result_digest not in verifier_host["evidence_digests"]
    ):
        raise ContractError("operational verifier is not independently bound")

    def assessment(result: dict[str, Any]) -> tuple[set[str], bool, str, dict[str, str]]:
        axes = result_axes(result)
        outcomes = {
            item["criterion_id"]: (
                "positive" if item["outcome"] in {"pass", "observed"}
                else item["outcome"]
            )
            for item in result["evidence"]
        }
        passed = {
            criterion for criterion, outcome in outcomes.items()
            if outcome == "positive"
        }
        positive = axes["semantic_verdict"] in {
            "pass", "ready", "ready-with-corrections"
        }
        complete = (
            axes["execution_state"] == "completed"
            and set(outcomes) == evaluator_criteria
            and not result["unverified_claims"]
            and (not positive or (passed == evaluator_criteria and not result["remaining_risks"]))
        )
        return passed, complete, axes["semantic_verdict"], outcomes

    evaluator_passed, evaluator_complete, evaluator_verdict, evaluator_outcomes = (
        assessment(evaluator_output)
    )
    verifier_passed, verifier_complete, verifier_verdict, verifier_outcomes = (
        assessment(verifier_output)
    )
    verifier_disagreement = (
        evaluator_complete != verifier_complete
        or evaluator_verdict != verifier_verdict
        or evaluator_outcomes != verifier_outcomes
        or host.get("semantic_verdict", "pass") != evaluator_verdict
        or verifier_host.get("semantic_verdict", "pass") != verifier_verdict
    )
    accepted = (
        host["status"] == "unverified"
        and verifier_host["status"] == "unverified"
        and evaluator_complete
        and verifier_complete
        and not verifier_disagreement
        and deterministic_pass
        and security_pass
    )
    required_passed = len(evaluator_passed & verifier_passed)
    qa_material = {
        "schema_version": 1,
        "record_type": "operational_qa",
        "host_outcome_digest": content_digest(host),
        "evaluator_assignment_digest": evaluator_assignment_digest,
        "evaluator_result_digest": evaluator_result_digest,
        "verifier_host_outcome_digest": content_digest(verifier_host),
        "verifier_assignment_digest": verifier_assignment_digest,
        "verifier_result_digest": verifier_result_digest,
        "deterministic_check_digests": deterministic_digests,
        "security_check_digests": security_digests,
        "deterministic_pass": deterministic_pass,
        "security_pass": security_pass,
        "required_criteria_total": len(evaluator_criteria),
        "required_criteria_passed": required_passed,
        "verifier_disagreement": verifier_disagreement,
        "accepted": accepted,
        "observed_at_ms": observed_at_ms,
    }
    qa = {
        **qa_material,
        "record_id": f"operational_qa:{content_digest(qa_material)}",
    }
    status = "accepted" if accepted else "rejected"
    outcome = validate_outcome(
        {
            **host,
            "record_id": external_id(
                digest_key,
                "out",
                f"operational\x00{host['record_id']}\x00{content_digest(qa)}",
            ),
            "observed_at_ms": observed_at_ms,
            "status": status,
            "execution_state": host.get(
                "execution_state",
                "completed" if host["status"] in {"accepted", "rejected", "unverified"}
                else host["status"],
            ),
            "semantic_verdict": evaluator_verdict,
            "admission_state": status,
            "retry_attempt": host.get("retry_attempt", 1),
            "retry_of_assignment_id": host.get("retry_of_assignment_id"),
            "deterministic_pass": deterministic_pass,
            "security_pass": security_pass,
            "required_criteria_total": len(evaluator_criteria),
            "required_criteria_passed": required_passed,
            "quality_micros": round(
                required_passed / len(evaluator_criteria) * 1_000_000
            ),
            "terminal_failure": False,
            "verifier_disagreement": verifier_disagreement,
            "accepted": accepted,
            "evidence_digests": sorted(
                {
                    *host["evidence_digests"],
                    content_digest(host),
                    content_digest(verifier_host),
                    content_digest(qa),
                }
            ),
        }
    )
    return qa, outcome


def build_feedback(
    *,
    digest_key: bytes,
    run_id: str,
    label: str,
    reason_code: str,
    correction: bytes | str | None = None,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    timestamp = _now_ms(observed_at_ms)
    correction_digest = keyed_digest(digest_key, correction) if correction is not None else None
    record = {
        "schema_version": 1,
        "record_type": "feedback",
        "record_id": external_id(
            digest_key, "fb", f"{run_id}\x00{label}\x00{reason_code}\x00{timestamp}"
        ),
        "run_id": run_id,
        "observed_at_ms": timestamp,
        "label": label,
        "reason_code": reason_code,
        "correction_digest": correction_digest,
    }
    return validate_feedback(record)


def mine_evaluation_case(
    *,
    digest_key: bytes,
    outcome: dict[str, Any],
    fixture_digest: str,
    criteria_digest: str,
    lineage_digest: str,
) -> dict[str, Any]:
    checked = validate_outcome(outcome)
    record = {
        "schema_version": 1,
        "record_type": "evaluation_case",
        "record_id": external_id(
            digest_key,
            "case",
            f"{checked['record_id']}\x00{fixture_digest}\x00{criteria_digest}",
        ),
        "source_outcome_id": checked["record_id"],
        "supersedes_case_id": None,
        "task_slice": checked["task_slice"],
        "corpus_state": "development",
        "fixture_digest": fixture_digest,
        "criteria_digest": criteria_digest,
        "lineage_digest": lineage_digest,
        "similarity_group_digest": None,
        "reviewed": False,
        "review_digest": None,
        "frozen": False,
    }
    return validate_evaluation_case(record)


def review_evaluation_case(
    case: dict[str, Any],
    *,
    digest_key: bytes,
    review_digest: str,
    similarity_group_digest: str,
    destination: str,
    freeze: bool,
) -> dict[str, Any]:
    checked = deepcopy(validate_evaluation_case(case))
    if checked["frozen"]:
        raise ContractError("frozen evaluation case cannot change")
    if destination not in CORPUS_STATES or destination == "development":
        raise ContractError("review destination must be promotion_holdout or canary")
    previous_id = checked["record_id"]
    checked.update(
        record_id=external_id(
            digest_key,
            "case",
            f"{previous_id}\x00{review_digest}\x00{similarity_group_digest}"
            f"\x00{destination}\x00{freeze}",
        ),
        supersedes_case_id=previous_id,
        corpus_state=destination,
        reviewed=True,
        review_digest=review_digest,
        similarity_group_digest=similarity_group_digest,
        frozen=freeze,
    )
    return validate_evaluation_case(checked)


def deduplicate_cases(cases: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    exact: dict[tuple[str, str], list[str]] = defaultdict(list)
    lineage: dict[str, list[str]] = defaultdict(list)
    similarity: dict[str, list[str]] = defaultdict(list)
    for raw in cases:
        case = validate_evaluation_case(raw)
        exact[(case["fixture_digest"], case["criteria_digest"])].append(case["record_id"])
        lineage[case["lineage_digest"]].append(case["record_id"])
        if case["similarity_group_digest"] is not None:
            similarity[case["similarity_group_digest"]].append(case["record_id"])
    return {
        "exact_duplicates": sorted(
            record_id
            for ids in exact.values()
            if len(ids) > 1
            for record_id in ids[1:]
        ),
        "lineage_groups": sorted(
            content_digest(sorted(ids)) for ids in lineage.values() if len(ids) > 1
        ),
        "similarity_groups": sorted(
            content_digest(sorted(ids)) for ids in similarity.values() if len(ids) > 1
        ),
    }


def analyze_outcomes(
    outcomes: Iterable[dict[str, Any]],
    *,
    as_of_ms: int,
    max_age_ms: int,
    owner_invalidated_run_ids: Iterable[str] = (),
) -> dict[str, Any]:
    invalidated = frozenset(owner_invalidated_run_ids)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for raw in outcomes:
        outcome = validate_outcome(raw)
        if outcome["run_id"] in invalidated:
            excluded += 1
            continue
        grouped[(outcome["task_slice"], outcome["configuration_digest"])].append(outcome)
    slices: dict[str, Any] = {}
    configuration_counts: dict[str, int] = defaultdict(int)
    for task_slice, _ in grouped:
        configuration_counts[task_slice] += 1
    for (task_slice, configuration_digest), records in sorted(grouped.items()):
        usage_complete = all(
            record["coverage"] == "end_to_end_complete"
            and record["usage"]["input_tokens"] is not None
            and record["usage"]["output_tokens"] is not None
            and record["usage"]["model_calls"] is not None
            for record in records
        )
        token_volume = (
            sum(
                int(record["usage"]["input_tokens"])
                + int(record["usage"]["output_tokens"])
                for record in records
            )
            if usage_complete
            else None
        )
        model_calls = (
            sum(int(record["usage"]["model_calls"]) for record in records)
            if usage_complete
            else None
        )
        latency_complete = all(record["usage"]["latency_ms"] is not None for record in records)
        tool_complete = all(record["usage"]["tool_calls"] is not None for record in records)
        workflow_latency = (
            sum(int(record["usage"]["latency_ms"]) for record in records)
            if latency_complete
            else None
        )
        tool_calls = (
            sum(int(record["usage"]["tool_calls"]) for record in records)
            if tool_complete
            else None
        )
        stale = any(as_of_ms - record["observed_at_ms"] > max_age_ms for record in records)
        confounded = configuration_counts[task_slice] > 1
        insufficient = len(records) < COMPARISON_MIN_CASES
        diagnoses = []
        alternatives = []
        if not usage_complete or not latency_complete or not tool_complete:
            diagnoses.append("telemetry_incomplete")
            alternatives.append("client_coverage_or_collection_gap")
        if stale:
            diagnoses.append("stale_evidence")
            alternatives.append("historical_behavior_may_not_match_current_package")
        if insufficient:
            diagnoses.append("small_sample")
            alternatives.append("task_mix_variance")
        if confounded:
            diagnoses.append("version_confounding")
            alternatives.append("configuration_change")
        section = {
            "observations": {
                "trials": len(records),
                "accepted": sum(record["accepted"] for record in records),
                "terminal_failures": sum(record["terminal_failure"] for record in records),
                "repairs": sum(record["repairs"] for record in records),
                "verifier_disagreements": sum(
                    record["verifier_disagreement"] for record in records
                ),
                "mean_quality_micros": round(
                    statistics.mean(record["quality_micros"] for record in records)
                ),
                "workflow_token_volume": token_volume,
                "workflow_model_calls": model_calls,
                "workflow_latency_ms": workflow_latency,
                "workflow_tool_calls": tool_calls,
                "delegation_overhead_tokens": sum(
                    record["delegation_overhead_tokens"] for record in records
                ),
                "no_progress_events": sum(record["no_progress_events"] for record in records),
            },
            "evidence_flags": {
                "usage_complete": usage_complete,
                "stale": stale,
                "latency_complete": latency_complete,
                "tool_usage_complete": tool_complete,
                "insufficient_sample": insufficient,
                "version_confounding": confounded,
            },
            "diagnoses": diagnoses,
            "alternative_explanations": alternatives,
        }
        slices.setdefault(task_slice, {"configurations": {}})["configurations"][
            configuration_digest
        ] = section
    return {
        "schema_version": 1,
        "as_of_ms": as_of_ms,
        "owner_feedback_excluded": excluded,
        "slices": slices,
    }


def generate_candidate(
    *,
    digest_key: bytes,
    scope: str,
    task_slice: str,
    target: str,
    effect: str,
    base_digest: str,
    champion_configuration: dict[str, Any],
    change_manifest: list[dict[str, str]],
    evidence_digests: list[str],
    eligibility_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if target not in CANDIDATE_TARGETS or effect not in CANDIDATE_EFFECTS:
        raise ContractError("candidate target or effect is unsupported")
    candidate_configuration = deepcopy(champion_configuration)
    for change in change_manifest:
        path = change["path_code"]
        if path == "routing.model_effort":
            candidate_configuration.update(change["after_value"])
        elif path == "orchestration.topology":
            candidate_configuration["topology"] = change["after_value"]
            if change["after_value"] == "single":
                candidate_configuration["max_parallel"] = 1
        elif path == "skills.selection":
            candidate_configuration["skill_digests"] = change["after_value"]
        elif path == "prompts.template_digest":
            candidate_configuration["prompt_digest"] = change["after_value"]
    material = {
        "schema_version": 1,
        "record_type": "candidate",
        "scope": scope,
        "task_slice": task_slice,
        "target": target,
        "effect": effect,
        "base_digest": base_digest,
        "patch_digest": content_digest(change_manifest),
        "change_manifest": deepcopy(change_manifest),
        "champion_configuration": deepcopy(champion_configuration),
        "candidate_configuration": candidate_configuration,
        "evidence_digests": sorted(evidence_digests),
    }
    if eligibility_receipt is not None:
        material["eligibility_receipt"] = deepcopy(eligibility_receipt)
    digest = content_digest(material)
    record = {
        **material,
        "record_id": external_id(digest_key, "cand", digest),
        "candidate_digest": digest,
        "status": "proposed",
    }
    return validate_candidate(record)


def _trial(raw: Any) -> dict[str, Any]:
    return validate_evaluation_trial(raw)


def create_evaluation_assignment(
    *,
    candidate: dict[str, Any],
    cases: Iterable[dict[str, Any]],
    client_version: str,
    client_capability: dict[str, Any],
    observed_at_ms: int,
) -> dict[str, Any]:
    """Freeze the complete matched trial population before evaluation begins."""

    checked_candidate = validate_candidate(candidate)
    if any(
        change["path_code"] not in RUNTIME_BOUND_CANDIDATE_PATHS
        for change in checked_candidate["change_manifest"]
    ):
        raise ContractError(
            "candidate target lacks an installed host-observed execution binding"
        )
    checked_cases = sorted(
        (validate_evaluation_case(case) for case in cases),
        key=lambda item: item["record_id"],
    )
    case_ids = [case["record_id"] for case in checked_cases]
    plan = []
    for case in checked_cases:
        for cohort, configuration_digest in (
            ("candidate", checked_candidate["candidate_digest"]),
            ("champion", checked_candidate["base_digest"]),
        ):
            trial_id = "evaluation:" + content_digest(
                {
                    "candidate_digest": checked_candidate["candidate_digest"],
                    "case_id": case["record_id"],
                    "cohort": cohort,
                    "evaluation_started_at_ms": observed_at_ms,
                }
            )
            plan.append(
                {
                    "trial_id": trial_id,
                    "case_id": case["record_id"],
                    "cohort": cohort,
                    "configuration_digest": configuration_digest,
                }
            )
    material = {
        "schema_version": 1,
        "record_type": "evaluation_assignment",
        "candidate_digest": checked_candidate["candidate_digest"],
        "task_slice": checked_candidate["task_slice"],
        "case_ids": case_ids,
        "case_digests": [content_digest(case) for case in checked_cases],
        "case_set_digest": content_digest(case_ids),
        "client_version": client_version,
        "client_digest": content_digest(client_capability),
        "trial_plan": plan,
        "observed_at_ms": observed_at_ms,
    }
    return validate_evaluation_assignment(
        {**material, "record_id": f"evaluation_assignment:{content_digest(material)}"}
    )


def candidate_matches_outcome(
    candidate: dict[str, Any], cohort: str, outcome: dict[str, Any]
) -> bool:
    """Bind a cohort marker to the host-observed fields changed by a candidate."""

    checked_candidate = validate_candidate(candidate)
    checked_outcome = validate_outcome(
        {key: value for key, value in outcome.items() if not key.startswith("_")}
    )
    if cohort not in {"champion", "candidate"}:
        return False
    expected = checked_candidate[
        "champion_configuration" if cohort == "champion" else "candidate_configuration"
    ]
    observed = {
        key: checked_outcome[key]
        for key in expected
    }
    return observed == expected


def validate_runtime_candidate(
    candidate: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Require an executable candidate to remain allowed by the installed policy."""

    checked = validate_candidate(candidate)
    if any(
        change["path_code"] not in RUNTIME_BOUND_CANDIDATE_PATHS
        for change in checked["change_manifest"]
    ):
        raise ContractError(
            "candidate target lacks an installed host-observed execution binding"
        )
    models = policy.get("models") if isinstance(policy, dict) else None
    if not isinstance(models, dict) or not models:
        raise ContractError("installed routing policy has no model authority")
    for label in ("champion_configuration", "candidate_configuration"):
        configuration = checked[label]
        provider = provider_for_model(configuration["model"])
        provider_models = (
            models
            if provider == "codex"
            else policy.get("providers", {}).get(provider, {}).get("models", {})
        )
        efforts = provider_models.get(configuration["model"])
        if (
            not isinstance(efforts, list)
            or configuration["effort"] not in efforts
        ):
            raise ContractError(
                f"{label} model and effort are not allowed by installed policy"
            )
    return checked


def apply_candidate_to_route(
    route: dict[str, Any],
    candidate: dict[str, Any],
    current_configuration: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Apply one approved routing candidate to a deterministic base route."""

    checked = validate_runtime_candidate(candidate, policy)
    if (
        current_configuration != checked["champion_configuration"]
        or content_digest(current_configuration) != checked["base_digest"]
    ):
        raise ContractError("candidate base does not match the complete execution configuration")
    result = deepcopy(route)
    for change in checked["change_manifest"]:
        path = change["path_code"]
        before = change["before_value"]
        after = change["after_value"]
        if path == "routing.model_effort":
            if {"model": result["model"], "effort": result["effort"]} != before:
                raise ContractError("candidate base does not match the routed configuration")
            route_provider = result.get("provider", "codex")
            if provider_for_model(after["model"]) != route_provider:
                raise ContractError(
                    "learning candidates must not move a route to another provider"
                )
            if not provider_model_allowed(policy, route_provider, after["model"]):
                raise ContractError(
                    "learning candidate model is not allowed by the project provider "
                    "configuration"
                )
            if (
                after["model"] == "gpt-6-astra"
                and result.get("reason") != "hard_end_to_end_evidence"
            ):
                raise ContractError(
                    "Astra candidates require the hard end-to-end route boundary"
                )
            result["model"] = after["model"]
            result["effort"] = after["effort"]
        else:
            raise ContractError("candidate target lacks an installed runtime binding")
    result["execution_configuration"] = deepcopy(checked["candidate_configuration"])
    result["max_parallel"] = checked["candidate_configuration"]["max_parallel"]
    result["learning_candidate_digest"] = checked["candidate_digest"]
    selection = result.get("selection")
    if isinstance(selection, dict):
        selected = {"model": result["model"], "effort": result["effort"]}
        eligible = selection.get("eligible")
        if isinstance(eligible, list) and selected not in eligible:
            eligible.append(selected)
        selection["selected"] = selected
        selection["strategy"] = "released_matched_candidate"
        selection["selection_probability_micros"] = 1_000_000
    result["reason"] = "released_learning_candidate"
    return result


def build_evaluation_trial(
    *,
    case: dict[str, Any],
    outcome: dict[str, Any],
    assignment: dict[str, Any],
    trial_id: str,
) -> dict[str, Any]:
    """Derive one immutable comparison trial from a persisted outcome."""

    checked_case = validate_evaluation_case(case)
    checked_outcome = validate_outcome(outcome)
    checked_assignment = validate_evaluation_assignment(assignment)
    planned = [
        item for item in checked_assignment["trial_plan"]
        if item["trial_id"] == trial_id
    ]
    if len(planned) != 1:
        raise ContractError("trial is not in the evaluation assignment")
    plan = planned[0]
    case_digest = content_digest(checked_case)
    if (
        plan["case_id"] != checked_case["record_id"]
        or checked_outcome["assignment_id"] != trial_id
        or checked_outcome["evaluation_case_digest"] != case_digest
        or checked_outcome["evaluation_candidate_digest"]
        != plan["configuration_digest"]
        or checked_outcome["task_slice"] != checked_case["task_slice"]
        or checked_outcome["client_digest"] != checked_assignment["client_digest"]
    ):
        raise ContractError("trial outcome is not bound to its evaluation case")
    usage = checked_outcome["usage"]
    material = {
        "schema_version": 1,
        "record_type": "evaluation_trial",
        "case_id": checked_case["record_id"],
        "trial_id": trial_id,
        "cohort": plan["cohort"],
        "task_slice": checked_case["task_slice"],
        "configuration_digest": checked_outcome["evaluation_candidate_digest"],
        "coverage": checked_outcome["coverage"],
        "assigned": True,
        "status": checked_outcome["status"],
        "accepted": checked_outcome["accepted"],
        "deterministic_pass": checked_outcome["deterministic_pass"],
        "security_pass": checked_outcome["security_pass"],
        "required_criteria_pass": checked_outcome["required_criteria_passed"]
        == checked_outcome["required_criteria_total"],
        "quality_micros": checked_outcome["quality_micros"],
        "terminal_failure": checked_outcome["terminal_failure"],
        "repairs": checked_outcome["repairs"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "model_calls": usage["model_calls"],
        "client_version": checked_assignment["client_version"],
        "client_digest": checked_outcome["client_digest"],
        "outcome_digest": content_digest(checked_outcome),
        "evaluation_assignment_digest": content_digest(checked_assignment),
        "host_configuration_digest": checked_outcome["configuration_digest"],
        "observed_at_ms": checked_outcome["observed_at_ms"],
    }
    return validate_evaluation_trial(
        {**material, "record_id": f"trial:{content_digest(material)}"}
    )


def calibrate_grader(
    labels: Iterable[dict[str, Any]], *, configuration: dict[str, Any], observed_at_ms: int
) -> dict[str, Any]:
    expected_configuration = {
        "model", "effort", "prompt_digest", "rubric_digest", "masking",
        "drift_digest", "owner_override_digest",
    }
    if not isinstance(configuration, dict) or set(configuration) != expected_configuration:
        raise ContractError("grader configuration must use the closed field set")
    if configuration["model"] not in SUPPORTED_MODELS or configuration["effort"] not in {
        "low", "medium", "high", "xhigh", "max", "ultra"
    }:
        raise ContractError("grader model or effort is unsupported")
    for field in (
        "prompt_digest", "rubric_digest", "drift_digest", "owner_override_digest"
    ):
        value = configuration[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ContractError(f"grader {field} is invalid")
    if configuration["masking"] is not True:
        raise ContractError("grader calibration requires masked candidate ordering")
    records = list(labels)
    for item in records:
        if not isinstance(item, dict) or set(item) != {"owner", "grader", "swapped"}:
            raise ContractError("grader label must contain owner, grader, and swapped")
        if item["owner"] not in {"a", "b", "tie"}:
            raise ContractError("owner label is invalid")
        if item["grader"] not in {"a", "b", "tie"} or item["swapped"] not in {
            "a",
            "b",
            "tie",
        }:
            raise ContractError("grader label is invalid")
    agreement = (
        sum(item["owner"] == item["grader"] for item in records) / len(records)
        if records else 0
    )
    swapped_back = {"a": "b", "b": "a", "tie": "tie"}
    stability = (sum(
        item["grader"] == swapped_back[item["swapped"]] for item in records
    ) / len(records)) if records else 0
    material = {
        "schema_version": 1,
        "record_type": "grader_calibration",
        "ready": len(records) >= GRADER_MIN_LABELS
        and agreement >= GRADER_MIN_AGREEMENT
        and stability >= GRADER_MIN_SWAP_STABILITY,
        "labels": deepcopy(records),
        "label_count": len(records),
        "agreement_micros": round(agreement * 1_000_000),
        "swap_stability_micros": round(stability * 1_000_000),
        "disagreements": sum(item["owner"] != item["grader"] for item in records),
        "configuration": deepcopy(configuration),
        "labels_digest": content_digest(records),
        "observed_at_ms": observed_at_ms,
    }
    return validate_grader_calibration(
        {**material, "record_id": f"calibration:{content_digest(material)}"}
    )


def _validated_grader_calibration(value: Any) -> dict[str, Any]:
    return validate_grader_calibration(value)


def _bootstrap_ratio_upper(
    pairs: list[tuple[int, int]], *, seed: int, samples: int = BOOTSTRAP_SAMPLES
) -> float | None:
    if not pairs or samples < 100:
        return None
    if sum(champion for champion, _ in pairs) <= 0:
        return None
    randomizer = random.Random(seed)
    ratios: list[float] = []
    for _ in range(samples):
        selected = [pairs[randomizer.randrange(len(pairs))] for _ in pairs]
        denominator = sum(champion for champion, _ in selected)
        if denominator <= 0:
            continue
        ratios.append(sum(candidate for _, candidate in selected) / denominator)
    if len(ratios) < samples * 0.95:
        return None
    ratios.sort()
    return ratios[min(len(ratios) - 1, math.ceil(0.975 * len(ratios)) - 1)]


def compare_candidate(
    *,
    candidate: dict[str, Any],
    trials: Iterable[dict[str, Any]],
    cases: Iterable[dict[str, Any]],
    client_version: str,
    client_capability: dict[str, Any],
    independent_review_digest: str,
    evaluation_assignment_digest: str = "0" * 64,
    grader_calibration: dict[str, Any] | None = None,
    subjective_grading_required: bool = False,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    checked_candidate = validate_candidate(candidate)
    checked_cases = [validate_evaluation_case(case) for case in cases]
    expected = sorted(case["record_id"] for case in checked_cases)
    checked_calibration = (
        _validated_grader_calibration(grader_calibration)
        if grader_calibration is not None
        else None
    )
    grader_digest = content_digest(checked_calibration) if checked_calibration else None

    records = [_trial(raw) for raw in trials]

    def report(
        decision: str, reasons: list[str], metrics: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        values = {
            "champion_tokens": None,
            "candidate_tokens": None,
            "champion_model_calls": None,
            "candidate_model_calls": None,
            "token_ratio_micros": None,
            "model_call_ratio_micros": None,
            "proven_improvements": [],
        }
        if metrics:
            values.update(metrics)
        material = {
            "schema_version": 1,
            "record_type": "comparison",
            "candidate_digest": checked_candidate["candidate_digest"],
            "champion_digest": checked_candidate["base_digest"],
            "task_slice": checked_candidate["task_slice"],
            "case_ids": expected,
            "case_set_digest": content_digest(expected),
            "case_digests": [
                content_digest(case)
                for case in sorted(checked_cases, key=lambda item: item["record_id"])
            ],
            "trial_digests": sorted(content_digest(record) for record in records),
            "client_version": client_version,
            "client_digest": content_digest(client_capability),
            "grader_digest": grader_digest,
            "decision": decision,
            "reasons": sorted(set(reasons)),
            "metrics": values,
            "independent_review_digest": independent_review_digest,
            "evaluation_assignment_digest": evaluation_assignment_digest,
        }
        return validate_comparison(
            {**material, "record_id": f"comparison:{content_digest(material)}"}
        )
    if len(expected) != len(set(expected)):
        return report("insufficient_evidence", ["duplicate_case"])
    if len(expected) < COMPARISON_MIN_CASES:
        return report("insufficient_evidence", ["minimum_cases"])
    if any(
        not case["reviewed"]
        or not case["frozen"]
        or case["corpus_state"] != "promotion_holdout"
        or case["task_slice"] != checked_candidate["task_slice"]
        for case in checked_cases
    ):
        return report("insufficient_evidence", ["holdout_invalid"])
    duplicates = deduplicate_cases(checked_cases)
    if (
        duplicates["exact_duplicates"]
        or duplicates["lineage_groups"]
        or duplicates["similarity_groups"]
    ):
        return report("insufficient_evidence", ["corpus_leakage"])
    if (
        not isinstance(client_capability, dict)
        or client_capability.get("app_server_coverage") != "end_to_end_complete"
        or client_capability.get("root_usage") is not True
        or client_capability.get("child_usage") is not True
        or client_capability.get("ordering_proven") is not True
    ):
        return report("insufficient_evidence", ["client_coverage"])
    if any(
        record["client_version"] != client_version
        or record["client_digest"] != content_digest(client_capability)
        for record in records
    ):
        return report("insufficient_evidence", ["client_coverage"])
    if any(not record["assigned"] for record in records):
        return report("insufficient_evidence", ["population_incomplete"])
    if any(record["task_slice"] != checked_candidate["task_slice"] for record in records):
        return report("insufficient_evidence", ["task_slice_mismatch"])
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate = False
    for record in records:
        key = (record["case_id"], record["cohort"])
        duplicate = duplicate or key in by_pair
        by_pair[key] = record
    if duplicate:
        return report("insufficient_evidence", ["duplicate_trial"])
    if sorted({record["case_id"] for record in records}) != expected or any(
        (case_id, cohort) not in by_pair
        for case_id in expected
        for cohort in ("champion", "candidate")
    ):
        return report("insufficient_evidence", ["matched_population"])
    ordered = [
        (by_pair[(case_id, "champion")], by_pair[(case_id, "candidate")])
        for case_id in expected
    ]
    champion_configurations = {pair[0]["configuration_digest"] for pair in ordered}
    candidate_configurations = {pair[1]["configuration_digest"] for pair in ordered}
    if champion_configurations != {checked_candidate["base_digest"]} or candidate_configurations != {
        checked_candidate["candidate_digest"]
    }:
        return report("insufficient_evidence", ["configuration_mismatch"])
    if any(
        champion["coverage"] != "end_to_end_complete"
        or candidate_trial["coverage"] != "end_to_end_complete"
        for champion, candidate_trial in ordered
    ):
        return report("insufficient_evidence", ["coverage"])
    if subjective_grading_required and not (checked_calibration or {}).get("ready"):
        return report("insufficient_evidence", ["grader_calibration"])
    quality_failures: list[str] = []
    for champion, candidate_trial in ordered:
        if not candidate_trial["accepted"]:
            quality_failures.append("acceptance")
        if champion["accepted"] and not candidate_trial["accepted"]:
            quality_failures.append("case_acceptance_regression")
        if not candidate_trial["deterministic_pass"]:
            quality_failures.append("deterministic")
        if not candidate_trial["security_pass"]:
            quality_failures.append("security")
        if not candidate_trial["required_criteria_pass"]:
            quality_failures.append("required_criteria")
        if champion["required_criteria_pass"] and not candidate_trial["required_criteria_pass"]:
            quality_failures.append("case_criterion_regression")
        if candidate_trial["quality_micros"] < champion["quality_micros"]:
            quality_failures.append("case_quality_regression")
    champion_mean = statistics.mean(pair[0]["quality_micros"] for pair in ordered)
    candidate_mean = statistics.mean(pair[1]["quality_micros"] for pair in ordered)
    champion_failures = sum(pair[0]["terminal_failure"] for pair in ordered)
    candidate_failures = sum(pair[1]["terminal_failure"] for pair in ordered)
    champion_repair_rate = sum(pair[0]["repairs"] > 0 for pair in ordered) / len(ordered)
    candidate_repair_rate = sum(pair[1]["repairs"] > 0 for pair in ordered) / len(ordered)
    if candidate_mean < champion_mean:
        quality_failures.append("mean_quality")
    if candidate_mean < 900_000:
        quality_failures.append("absolute_quality")
    if candidate_failures > champion_failures:
        quality_failures.append("terminal_failure")
    if candidate_repair_rate > max(champion_repair_rate, 0.10):
        quality_failures.append("repair_rate")
    if quality_failures:
        return report("reject", quality_failures)
    if any(
        trial[field] is None
        for pair in ordered
        for trial in pair
        for field in ("input_tokens", "output_tokens", "model_calls")
    ):
        return report("insufficient_evidence", ["usage_missing"])
    token_pairs = [
        (
            int(champion["input_tokens"]) + int(champion["output_tokens"]),
            int(candidate_trial["input_tokens"]) + int(candidate_trial["output_tokens"]),
        )
        for champion, candidate_trial in ordered
    ]
    call_pairs = [
        (int(champion["model_calls"]), int(candidate_trial["model_calls"]))
        for champion, candidate_trial in ordered
    ]
    champion_tokens = sum(item[0] for item in token_pairs)
    candidate_tokens = sum(item[1] for item in token_pairs)
    champion_calls = sum(item[0] for item in call_pairs)
    candidate_calls = sum(item[1] for item in call_pairs)
    if candidate_tokens > champion_tokens or candidate_calls > champion_calls:
        return report("reject", ["usage_regression"])
    token_ratio = candidate_tokens / champion_tokens if champion_tokens else None
    call_ratio = candidate_calls / champion_calls if champion_calls else None
    seed = int(checked_candidate["candidate_digest"][:16], 16)
    improvements: list[tuple[str, float, float | None]] = []
    if token_ratio is not None and token_ratio <= 1 - MATERIAL_IMPROVEMENT:
        improvements.append(
            (
                "tokens",
                token_ratio,
                _bootstrap_ratio_upper(token_pairs, seed=seed, samples=bootstrap_samples),
            )
        )
    if call_ratio is not None and call_ratio <= 1 - MATERIAL_IMPROVEMENT:
        improvements.append(
            (
                "model_calls",
                call_ratio,
                _bootstrap_ratio_upper(call_pairs, seed=seed ^ 0xA5A5, samples=bootstrap_samples),
            )
        )
    proven = [item for item in improvements if item[2] is not None and item[2] < 1.0]
    metrics = {
        "champion_tokens": champion_tokens,
        "candidate_tokens": candidate_tokens,
        "champion_model_calls": champion_calls,
        "candidate_model_calls": candidate_calls,
        "token_ratio_micros": round(token_ratio * 1_000_000) if token_ratio is not None else None,
        "model_call_ratio_micros": round(call_ratio * 1_000_000) if call_ratio is not None else None,
        "proven_improvements": sorted(item[0] for item in proven),
    }
    if not improvements:
        return report("reject", ["material_improvement"], metrics)
    if not proven:
        return report("insufficient_evidence", ["paired_confidence"], metrics)
    return report("promote", [], metrics)


def build_canary_report(
    *,
    candidate_digest: str,
    champion_digest: str,
    case_results: list[dict[str, Any]],
    comparison_digest: str,
    independent_review_digest: str,
    observed_at_ms: int,
) -> dict[str, Any]:
    """Build immutable canary QA evidence from explicit hard-gate results."""

    if not case_results:
        raise ContractError("canary requires paired case results")
    pairs = []
    candidate_outcomes = []
    champion_outcomes = []
    for item in case_results:
        if not isinstance(item, dict) or set(item) != {
            "case_digest", "champion_outcome", "candidate_outcome"
        }:
            raise ContractError("canary case result must use the closed field set")
        champion = validate_outcome(item["champion_outcome"])
        candidate = validate_outcome(item["candidate_outcome"])
        case_digest = item["case_digest"]
        if (
            champion["evaluation_candidate_digest"] != champion_digest
            or candidate["evaluation_candidate_digest"] != candidate_digest
            or champion["evaluation_case_digest"] != case_digest
            or candidate["evaluation_case_digest"] != case_digest
            or champion["task_slice"] != candidate["task_slice"]
        ):
            raise ContractError("canary outcomes are not bound to the case and configurations")
        champion_outcomes.append(champion)
        candidate_outcomes.append(candidate)
        pairs.append(
            {
                "case_digest": case_digest,
                "champion_outcome_digest": content_digest(champion),
                "candidate_outcome_digest": content_digest(candidate),
            }
        )
    cases = sorted(item["case_digest"] for item in pairs)
    if len(cases) != len(set(cases)):
        raise ContractError("canary case results must be unique")
    deterministic_pass = all(item["deterministic_pass"] for item in candidate_outcomes)
    security_pass = all(item["security_pass"] for item in candidate_outcomes)
    required_criteria_pass = all(
        item["required_criteria_passed"] == item["required_criteria_total"]
        for item in candidate_outcomes
    )
    quality_regressions = sum(
        candidate["quality_micros"] < champion["quality_micros"]
        for champion, candidate in zip(champion_outcomes, candidate_outcomes)
    )
    material = {
        "schema_version": 1,
        "record_type": "canary_report",
        "candidate_digest": candidate_digest,
        "champion_digest": champion_digest,
        "case_digests": cases,
        "case_set_digest": content_digest(cases),
        "deterministic_pass": deterministic_pass,
        "security_pass": security_pass,
        "required_criteria_pass": required_criteria_pass,
        "quality_regressions": quality_regressions,
        "passed": deterministic_pass
        and security_pass
        and required_criteria_pass
        and quality_regressions == 0,
        "comparison_digest": comparison_digest,
        "case_outcomes": sorted(pairs, key=lambda item: item["case_digest"]),
        "observed_at_ms": observed_at_ms,
        "independent_review_digest": independent_review_digest,
    }
    return validate_canary_report(
        {**material, "record_id": f"canary:{content_digest(material)}"}
    )


OWNER_SIGNER_IDENTITY = "codex-agents-owner"
OWNER_SIGNATURE_NAMESPACE = "codex-agents-owner-v1"
OWNER_RELEASE_NAMESPACE = "codex-agents-release-v1"
SSH_KEYGEN_PATH = Path("/usr/bin/ssh-keygen")
OWNER_GRANT_REQUEST_FIELDS = {
    "grant_kind", "project_id", "candidate_digest", "champion_digest",
    "review_digest", "bounds_digest", "issued_at_ms", "expires_at_ms", "nonce_digest",
}


class OwnerGrantAuthority:
    """Verify detached owner signatures using public keys only."""

    def __init__(self, allowed_signers: Path) -> None:
        self.allowed_signers = allowed_signers
        if allowed_signers.is_symlink() or not allowed_signers.is_file():
            raise ContractError("owner allowed-signers file is unavailable")

    def request(
        self,
        *,
        digest_key: bytes,
        kind: str,
        project_id: str,
        candidate_digest_value: str,
        champion_digest: str,
        review_digest: str,
        bounds_digest: str,
        issued_at_ms: int,
        expires_at_ms: int,
        nonce: bytes | None = None,
    ) -> dict[str, Any]:
        nonce_value = nonce or secrets.token_bytes(32)
        nonce_digest = keyed_digest(digest_key, nonce_value)
        material = {
            "grant_kind": kind,
            "project_id": project_id,
            "candidate_digest": candidate_digest_value,
            "champion_digest": champion_digest,
            "review_digest": review_digest,
            "bounds_digest": bounds_digest,
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
            "nonce_digest": nonce_digest,
        }
        if kind not in {"canary", "promotion"}:
            raise ContractError("owner grant kind is unsupported")
        if expires_at_ms <= issued_at_ms:
            raise ContractError("owner grant must expire after issue")
        return material

    @staticmethod
    def signing_payload(request: dict[str, Any]) -> bytes:
        if not isinstance(request, dict) or set(request) != OWNER_GRANT_REQUEST_FIELDS:
            raise ContractError("owner grant request is invalid")
        provisional = {
            "schema_version": 1,
            "record_type": "owner_grant",
            "record_id": "grant:" + "0" * 64,
            **request,
            "signature_b64": base64.b64encode(b"signature").decode("ascii"),
        }
        try:
            validate_owner_grant(provisional)
        except ContractError as exc:
            if "record_id" not in str(exc):
                raise
        return canonical_json(request)

    def attach_signature(
        self, request: dict[str, Any], signature: bytes
    ) -> dict[str, Any]:
        payload = self.signing_payload(request)
        if not signature or len(signature) > 16_384:
            raise ContractError("owner signature is invalid")
        material = {
            "schema_version": 1,
            "record_type": "owner_grant",
            **request,
            "signature_b64": base64.b64encode(signature).decode("ascii"),
        }
        record = {
            **material,
            "record_id": f"grant:{content_digest(material)}",
        }
        checked = validate_owner_grant(record)
        if not self._verify_payload(payload, signature):
            raise ContractError("owner grant signature is invalid")
        return checked

    def _verify_payload(
        self,
        payload: bytes,
        signature: bytes,
        *,
        namespace: str = OWNER_SIGNATURE_NAMESPACE,
    ) -> bool:
        try:
            executable = SSH_KEYGEN_PATH
            executable_stat = executable.lstat()
            if (
                executable.is_symlink()
                or not stat.S_ISREG(executable_stat.st_mode)
                or executable_stat.st_uid != 0
                or executable_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return False
            for parent in (executable.parent, executable.parent.parent):
                parent_stat = parent.lstat()
                if (
                    parent.is_symlink()
                    or not stat.S_ISDIR(parent_stat.st_mode)
                    or parent_stat.st_uid != 0
                    or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    return False
            with tempfile.NamedTemporaryFile(
                dir=self.allowed_signers.parent, prefix="owner-signature-", delete=False
            ) as handle:
                handle.write(signature)
                signature_path = Path(handle.name)
            signature_path.chmod(0o600)
            completed = subprocess.run(
                [
                    str(executable), "-Y", "verify", "-f", str(self.allowed_signers),
                    "-I", OWNER_SIGNER_IDENTITY, "-n", namespace,
                    "-s", str(signature_path),
                ],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            if "signature_path" in locals():
                signature_path.unlink(missing_ok=True)

    @staticmethod
    def owner_record_payload(project_id: str, record: dict[str, Any]) -> bytes:
        """Return exact bytes for an owner feedback or calibration signature."""

        if not isinstance(project_id, str) or not project_id:
            raise ContractError("owner record project is invalid")
        record_type = record.get("record_type") if isinstance(record, dict) else None
        if record_type == "feedback":
            checked = validate_feedback(record)
        elif record_type == "grader_calibration":
            checked = validate_grader_calibration(record)
        elif record_type == "billing_attestation":
            checked = validate_billing_attestation(record)
        else:
            raise ContractError("owner record type is unsupported")
        return canonical_json(
            {
                "purpose": "authenticated_owner_record",
                "project_id": project_id,
                "record_type": record_type,
                "record_digest": content_digest(checked),
            }
        )

    def verify_owner_record(
        self, project_id: str, record: dict[str, Any], signature: bytes
    ) -> bool:
        if not signature or len(signature) > 16_384:
            return False
        return self._verify_payload(
            self.owner_record_payload(project_id, record), signature
        )

    @staticmethod
    def qa_attestation_payload(project_id: str, qa_evidence: dict[str, Any]) -> bytes:
        """Bind owner acceptance to exact host, verifier, and check evidence."""

        if not isinstance(project_id, str) or not project_id:
            raise ContractError("QA attestation project is invalid")
        checked = validate_qa_evidence(qa_evidence)
        return canonical_json(
            {
                "purpose": "authenticated_verified_qa",
                "project_id": project_id,
                "qa_evidence_digest": content_digest(checked),
            }
        )

    def verify_qa_attestation(
        self, project_id: str, qa_evidence: dict[str, Any], signature: bytes
    ) -> bool:
        if not signature or len(signature) > 16_384:
            return False
        return self._verify_payload(
            self.qa_attestation_payload(project_id, qa_evidence), signature
        )

    def verify_release_signature(self, payload: bytes, signature: bytes) -> bool:
        """Verify a release registry signed outside the agent session."""

        return self._verify_payload(
            payload, signature, namespace=OWNER_RELEASE_NAMESPACE
        )

    def verify_upgrade_reconciliation_signature(
        self, request: dict[str, Any], signature: bytes
    ) -> bool:
        """Verify one separately scoped owner lifecycle authorization."""

        if not signature or len(signature) > 16_384:
            return False
        return self._verify_payload(canonical_json(request), signature)

    def verify(self, grant: dict[str, Any]) -> bool:
        checked = validate_owner_grant(grant)
        request = {
            key: checked[key]
            for key in OWNER_GRANT_REQUEST_FIELDS
        }
        try:
            signature = base64.b64decode(checked["signature_b64"], validate=True)
        except ValueError:
            return False
        return self._verify_payload(self.signing_payload(request), signature)


class CandidateLifecycle:
    """Inert candidate state machine with separate single-use grants."""

    def __init__(
        self,
        *,
        candidate: dict[str, Any],
        champion_digest: str,
        project_id: str,
        authority: OwnerGrantAuthority,
        digest_key: bytes,
    ) -> None:
        self.candidate = deepcopy(validate_candidate(candidate))
        self.champion_digest = champion_digest
        self.project_id = project_id
        self.authority = authority
        self.digest_key = digest_key
        if self.candidate["status"] != "proposed":
            raise ContractError("lifecycle requires an immutable proposed candidate")
        if self.champion_digest != self.candidate["base_digest"]:
            raise ContractError("lifecycle champion must match the candidate base")
        self._status = "proposed"
        self.used_grants: set[str] = set()
        self.canary_cases: list[str] = []
        self.audit: list[dict[str, Any]] = []

    @property
    def status(self) -> str:
        return self._status

    @classmethod
    def restore(
        cls,
        *,
        candidate: dict[str, Any],
        champion_digest: str,
        project_id: str,
        authority: OwnerGrantAuthority,
        digest_key: bytes,
        promotions: Iterable[dict[str, Any]],
    ) -> CandidateLifecycle:
        """Rebuild lifecycle state from validated durable transition records."""

        lifecycle = cls(
            candidate=candidate,
            champion_digest=champion_digest,
            project_id=project_id,
            authority=authority,
            digest_key=digest_key,
        )
        transitions = [validate_promotion(record) for record in promotions]
        allowed = {
            "proposed": {"evaluated", "rejected"},
            "evaluated": {"canary_started"},
            "canary_approved": {"canary_passed", "canary_failed"},
            "canary_passed": {"promoted"},
            "promoted": {"drift_detected", "reverted"},
            "drift_detected": {"reverted"},
        }
        status_by_action = {
            "evaluated": "evaluated",
            "rejected": "rejected",
            "canary_started": "canary_approved",
            "canary_passed": "canary_passed",
            "canary_failed": "canary_failed",
            "promoted": "promoted",
            "drift_detected": "drift_detected",
            "reverted": "reverted",
        }
        for transition in transitions:
            action = transition["action"]
            if action not in allowed.get(lifecycle._status, set()):
                raise ContractError("durable lifecycle transition order is invalid")
            if (
                transition["candidate_digest"] != lifecycle.candidate["candidate_digest"]
                or transition["champion_digest"] != lifecycle.champion_digest
            ):
                raise ContractError("durable lifecycle transition binding is invalid")
            if action == "canary_started":
                lifecycle.canary_cases = list(transition["canary_case_digests"])
            elif lifecycle.canary_cases and (
                transition["canary_case_digests"] != lifecycle.canary_cases
            ):
                raise ContractError("durable lifecycle canary bounds changed")
            if transition["grant_id"] is not None:
                lifecycle.used_grants.add(transition["grant_id"])
            lifecycle.audit.append(deepcopy(transition))
            lifecycle._status = status_by_action[action]
        return lifecycle

    def mark_evaluated(
        self,
        comparison: dict[str, Any],
        *,
        evidence_digest: str,
        now_ms: int,
    ) -> dict[str, Any]:
        if self.status != "proposed":
            raise ContractError("candidate is not proposed")
        checked_comparison = validate_comparison(comparison)
        if (
            checked_comparison["candidate_digest"] != self.candidate["candidate_digest"]
            or checked_comparison["champion_digest"] != self.champion_digest
            or content_digest(checked_comparison) != evidence_digest
        ):
            raise ContractError("comparison is not bound to this candidate")
        if checked_comparison["decision"] == "reject":
            self._status = "rejected"
        elif checked_comparison["decision"] == "promote":
            self._status = "evaluated"
        else:
            raise ContractError("insufficient evidence cannot advance a candidate")
        return self._audit(
            action=self._status,
            now_ms=now_ms,
            grant_id=None,
            evidence_digest=evidence_digest,
        )

    def _verify_grant(
        self,
        grant: dict[str, Any],
        *,
        kind: str,
        review_digest: str,
        bounds_digest: str,
        now_ms: int,
    ) -> str:
        checked = validate_owner_grant(grant)
        if not self.authority.verify(checked):
            raise ContractError("owner grant signature is invalid")
        if checked["record_id"] in self.used_grants:
            raise ContractError("owner grant was already used")
        if checked["grant_kind"] != kind:
            raise ContractError("owner grant kind is invalid for transition")
        if checked["project_id"] != self.project_id:
            raise ContractError("owner grant project does not match")
        if checked["candidate_digest"] != self.candidate["candidate_digest"]:
            raise ContractError("owner grant candidate digest does not match")
        if checked["champion_digest"] != self.champion_digest:
            raise ContractError("owner grant champion digest does not match")
        if checked["review_digest"] != review_digest:
            raise ContractError("owner grant review digest does not match")
        if checked["bounds_digest"] != bounds_digest:
            raise ContractError("owner grant bounds digest does not match")
        if not checked["issued_at_ms"] <= now_ms < checked["expires_at_ms"]:
            raise ContractError("owner grant is not active")
        return checked["record_id"]

    def _audit(
        self,
        *,
        action: str,
        now_ms: int,
        grant_id: str | None,
        evidence_digest: str,
        case_digests: list[str] | None = None,
        append: bool = True,
    ) -> dict[str, Any]:
        material = f"{self.candidate['candidate_digest']}\x00{action}\x00{now_ms}\x00{len(self.audit)}"
        record = {
            "schema_version": 1,
            "record_type": "promotion",
            "record_id": external_id(self.digest_key, "promo", material),
            "candidate_digest": self.candidate["candidate_digest"],
            "champion_digest": self.champion_digest,
            "action": action,
            "observed_at_ms": now_ms,
            "grant_id": grant_id,
            "evidence_digest": evidence_digest,
            "canary_case_digests": sorted(
                self.canary_cases if case_digests is None else case_digests
            ),
        }
        checked = validate_promotion(record)
        if append:
            self.audit.append(checked)
        return checked

    def start_canary(
        self,
        *,
        grant: dict[str, Any],
        case_digests: list[str],
        evidence_digest: str,
        now_ms: int,
        commit: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> dict[str, Any]:
        if self.status != "evaluated" or not case_digests:
            raise ContractError("evaluated candidate and bounded canary cases are required")
        bounded_cases = sorted(set(case_digests))
        grant_id = self._verify_grant(
            grant,
            kind="canary",
            review_digest=evidence_digest,
            bounds_digest=content_digest(bounded_cases),
            now_ms=now_ms,
        )
        transition = self._audit(
            action="canary_started",
            now_ms=now_ms,
            grant_id=grant_id,
            evidence_digest=evidence_digest,
            case_digests=bounded_cases,
            append=False,
        )
        commit(grant, transition)
        self.used_grants.add(grant_id)
        self.canary_cases = bounded_cases
        self._status = "canary_approved"
        self.audit.append(transition)
        return transition

    def resolve_digest(self, case_digest: str) -> str:
        if self.status == "canary_approved" and case_digest in self.canary_cases:
            return str(self.candidate["candidate_digest"])
        if self.status == "promoted":
            return str(self.candidate["candidate_digest"])
        return self.champion_digest

    def complete_canary(
        self, *, report: dict[str, Any], now_ms: int
    ) -> dict[str, Any]:
        if self.status != "canary_approved":
            raise ContractError("candidate is not in canary")
        checked = validate_canary_report(report)
        if (
            checked["candidate_digest"] != self.candidate["candidate_digest"]
            or checked["champion_digest"] != self.champion_digest
            or checked["case_digests"] != self.canary_cases
        ):
            raise ContractError("canary report is not bound to this canary")
        passed = checked["passed"]
        evidence_digest = content_digest(checked)
        self._status = "canary_passed" if passed else "canary_failed"
        return self._audit(
            action="canary_passed" if passed else "canary_failed",
            now_ms=now_ms,
            grant_id=None,
            evidence_digest=evidence_digest,
        )

    def promote(
        self,
        *,
        grant: dict[str, Any],
        evidence_digest: str,
        now_ms: int,
        commit: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> dict[str, Any]:
        if self.status != "canary_passed":
            raise ContractError("successful canary is required before promotion")
        grant_id = self._verify_grant(
            grant,
            kind="promotion",
            review_digest=evidence_digest,
            bounds_digest=content_digest(self.canary_cases),
            now_ms=now_ms,
        )
        transition = self._audit(
            action="promoted",
            now_ms=now_ms,
            grant_id=grant_id,
            evidence_digest=evidence_digest,
            append=False,
        )
        commit(grant, transition)
        self.used_grants.add(grant_id)
        self._status = "promoted"
        self.audit.append(transition)
        return transition

    def rollback(
        self, *, active_digest: str, evidence_digest: str, now_ms: int
    ) -> dict[str, Any]:
        if self.status not in {"promoted", "drift_detected"} or active_digest != self.candidate["candidate_digest"]:
            raise ContractError("rollback target does not match the active candidate")
        self._status = "reverted"
        return self._audit(
            action="reverted",
            now_ms=now_ms,
            grant_id=None,
            evidence_digest=evidence_digest,
        )

    def record_drift(
        self,
        *,
        comparison: dict[str, Any],
        evidence_digest: str,
        now_ms: int,
    ) -> dict[str, Any]:
        """Fail back to the champion when later matched evidence shows regression."""

        if self.status != "promoted":
            raise ContractError("drift monitoring requires a promoted candidate")
        checked = validate_comparison(comparison)
        if (
            checked["candidate_digest"] != self.candidate["candidate_digest"]
            or checked["champion_digest"] != self.champion_digest
            or checked["decision"] != "reject"
            or content_digest(checked) != evidence_digest
        ):
            raise ContractError("drift evidence is not a rejecting matched comparison")
        self._status = "drift_detected"
        return self._audit(
            action="drift_detected",
            now_ms=now_ms,
            grant_id=None,
            evidence_digest=evidence_digest,
        )
