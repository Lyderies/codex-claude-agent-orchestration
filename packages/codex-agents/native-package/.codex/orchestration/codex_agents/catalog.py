"""Inert discovery and compatibility checks for trusted project definitions."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import tomllib

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AGENT_ALLOWED_KEYS = {
    "name",
    "description",
    "developer_instructions",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
    "mcp_servers",
    "skills",
}


CLAUDE_AGENT_ALLOWED_KEYS = {"name", "description", "tools", "model", "maxTurns"}
CLAUDE_READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
# Edit and Write on top of the read tools make a contained write child.
CLAUDE_WRITE_TOOLS = {"Edit", "Write"}


def claude_sandbox_mode(tools: list[str]) -> str | None:
    """Map Claude agent tools onto the contract sandbox; None when no profile fits."""

    if set(tools) <= CLAUDE_READ_ONLY_TOOLS:
        return "read-only"
    if set(tools) <= CLAUDE_READ_ONLY_TOOLS | CLAUDE_WRITE_TOOLS:
        return "workspace-write"
    return None


class CatalogError(ValueError):
    """Raised for invalid discovery policy or irreconcilable candidates."""


def _require_contained_file(path: Path, source_root: Path) -> None:
    try:
        path.resolve().relative_to(source_root)
    except ValueError as exc:
        raise CatalogError(f"discovered file escapes source root: {path}") from exc


def load_discovery_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot load discovery policy {path}: {exc}") from exc
    if policy.get("duplicate_policy") != "error":
        raise CatalogError("duplicate_policy must be 'error'; use explicit resolutions")
    sources = policy.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CatalogError("discovery policy needs at least one source")
    ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not {"id", "kind", "root"} <= source.keys():
            raise CatalogError("each source needs id, kind, and root")
        if set(source) != {"id", "kind", "root"}:
            raise CatalogError("source entries cannot contain unknown fields")
        if (
            not isinstance(source["id"], str)
            or not NAME_RE.match(source["id"])
            or source["kind"] not in {"project", "catalog"}
            or not isinstance(source["root"], str)
            or not source["root"]
        ):
            raise CatalogError("source id, kind, or root is invalid")
        if source["id"] in ids:
            raise CatalogError(f"duplicate source id: {source['id']}")
        ids.add(source["id"])
    resolutions = policy.get("resolutions", {})
    if not isinstance(resolutions, dict):
        raise CatalogError("resolutions must be an object")
    unknown = sorted(set(resolutions.values()) - ids)
    if unknown:
        raise CatalogError(f"resolution refers to unknown sources: {', '.join(unknown)}")
    values = policy.get("allowed_legacy_tools", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise CatalogError("allowed_legacy_tools must be a string array")
    if policy.get("allowed_mcp_servers") != {}:
        raise CatalogError(
            "version 1 requires allowed_mcp_servers to be empty; pinned MCP identity "
            "enforcement is not implemented"
        )
    return policy


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    data: dict[str, Any] = {}
    lines = text[4:end].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if ":" not in line or line.startswith((" ", "\t")):
            index += 1
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if raw:
            if raw.startswith("[") and raw.endswith("]"):
                data[key] = [part.strip().strip("'\"") for part in raw[1:-1].split(",") if part.strip()]
            else:
                data[key] = raw.strip("'\"")
            index += 1
            continue
        values: list[str] = []
        index += 1
        while index < len(lines) and lines[index].startswith((" ", "\t")):
            item = lines[index].strip()
            if item.startswith("-"):
                values.append(item[1:].strip().strip("'\""))
            index += 1
        data[key] = values
    return data


def _local_link_issues(
    path: Path, text: str, source_root: Path
) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    escaped: list[str] = []
    for raw in LINK_RE.findall(text):
        target = raw.strip().split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        target = target.split(" ", 1)[0].strip("<>")
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(source_root)
        except ValueError:
            escaped.append(raw)
            continue
        if not resolved.exists():
            missing.append(raw)
    return sorted(set(missing)), sorted(set(escaped))


def _agent_candidate(
    path: Path,
    source_id: str,
    source_root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return {
            "source": source_id,
            "kind": "agent",
            "name": path.stem,
            "format": "codex-toml",
            "path": str(path.relative_to(source_root)),
            "requested_tools": [],
            "requested_mcps": [],
            "requested_skills": [],
            "content_sha256": None,
            "errors": [f"invalid TOML: {exc}"],
            "warnings": [],
        }
    required = {"name", "description", "developer_instructions"}
    missing = sorted(required - data.keys())
    if missing:
        errors.append(f"missing required keys: {', '.join(missing)}")
    name = data.get("name", path.stem)
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("name must use lowercase letters, numbers, underscore, or hyphen")
        name = path.stem
    if name != path.stem:
        warnings.append("file stem differs from agent name")
    unknown = sorted(data.keys() - AGENT_ALLOWED_KEYS)
    if unknown:
        warnings.append(f"unreviewed Codex config keys: {', '.join(unknown)}")
    sandbox = data.get("sandbox_mode")
    if sandbox is not None and sandbox not in {"read-only", "workspace-write"}:
        errors.append(f"unsupported sandbox_mode: {sandbox}")
    mcp_config = data.get("mcp_servers", [])
    if isinstance(mcp_config, list) and all(isinstance(item, str) for item in mcp_config):
        mcps = mcp_config
    elif isinstance(mcp_config, dict):
        errors.append("inline MCP definitions are rejected")
        mcps = list(mcp_config) if all(isinstance(name, str) for name in mcp_config) else []
    else:
        errors.append("mcp_servers must be a string array")
        mcps = []
    if mcps:
        errors.append(
            "MCP requests are inert in version 1 until pinned identity enforcement exists: "
            + ", ".join(sorted(set(mcps)))
        )
    skill_config = data.get("skills", [])
    skills: list[str] = []
    if isinstance(skill_config, list) and all(
        isinstance(item, str) for item in skill_config
    ):
        skills = skill_config
        missing_skills = sorted(
            skill
            for skill in skills
            if not (source_root / ".agents" / "skills" / skill / "SKILL.md").is_file()
        )
        if missing_skills:
            errors.append(f"referenced skills not found: {', '.join(missing_skills)}")
    elif isinstance(skill_config, dict) and set(skill_config) == {"config"}:
        entries = skill_config["config"]
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            errors.append("skills.config must be an array of tables")
        else:
            for entry in entries:
                skill_path = entry.get("path")
                enabled = entry.get("enabled", True)
                if not isinstance(skill_path, str) or not isinstance(enabled, bool):
                    errors.append("each skills.config entry needs a path and boolean enabled")
                    continue
                skills.append(skill_path)
                if not enabled:
                    continue
                resolved = Path(skill_path)
                if not resolved.is_absolute():
                    resolved = source_root / resolved
                resolved = resolved.resolve()
                try:
                    resolved.relative_to(source_root)
                except ValueError:
                    errors.append(f"enabled skill path escapes source root: {skill_path}")
                    continue
                if not resolved.is_file():
                    errors.append(f"referenced skill not found: {skill_path}")
    else:
        errors.append("skills must be a string array or a config table")
    return {
        "source": source_id,
        "kind": "agent",
        "name": name,
        "format": "codex-toml",
        "path": str(path.relative_to(source_root)),
        "requested_tools": [],
        "requested_mcps": sorted(mcps),
        "requested_skills": sorted(skills),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "sandbox_mode": sandbox,
        "configured_model": data.get("model"),
        "configured_effort": data.get("model_reasoning_effort"),
        "errors": errors,
        "warnings": warnings,
    }


def _markdown_candidate(
    path: Path,
    source_id: str,
    source_root: Path,
    kind: str,
    format_name: str,
    allowed_tools: set[str],
) -> dict[str, Any]:
    text = path.read_text()
    metadata = _frontmatter(text)
    errors: list[str] = []
    warnings: list[str] = []
    name = metadata.get("name") or (path.parent.name if kind == "skill" else path.name.removesuffix(".agent.md"))
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("name must use lowercase letters, numbers, underscore, or hyphen")
        name = str(name)
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("missing description")
    tools = metadata.get("tools", [])
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        errors.append("tools must be a string array")
        tools = []
    denied = sorted(set(tools) - allowed_tools)
    if denied:
        errors.append(f"tools not allowed by discovery policy: {', '.join(denied)}")
    missing_links, escaped_links = _local_link_issues(path, text, source_root)
    if missing_links:
        errors.append(f"broken local references: {', '.join(missing_links)}")
    if escaped_links:
        errors.append(f"local references escape source root: {', '.join(escaped_links)}")
    if format_name == "legacy-agent-markdown":
        warnings.append("requires explicit translation to Codex TOML before use")
    return {
        "source": source_id,
        "kind": kind,
        "name": name,
        "format": format_name,
        "path": str(path.relative_to(source_root)),
        "requested_tools": tools,
        "requested_mcps": [],
        "requested_skills": [],
        "errors": errors,
        "warnings": warnings,
    }


def parse_claude_agent(raw: bytes) -> tuple[dict[str, Any], str]:
    """Parse one Claude agent in the exact form the Claude package installs.

    Discovery and execution share this parser, so an agent that a mapping
    accepts is one that execution can bind. Values are plain text: quoted
    strings and YAML arrays or objects are rejected rather than reinterpreted.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise CatalogError("Claude agent is not UTF-8") from exc
    end = text.find("\n---\n", 4)
    if not text.startswith("---\n") or end < 0:
        raise CatalogError("Claude agent front matter is missing")
    metadata: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not separator or line.startswith((" ", "\t")):
            raise CatalogError("Claude agent front matter is not flat key: value text")
        if key not in CLAUDE_AGENT_ALLOWED_KEYS:
            raise CatalogError(f"unreviewed Claude agent keys: {key}")
        if key in metadata:
            raise CatalogError(f"Claude agent key is repeated: {key}")
        if not value or value[0] in "\"'[{":
            raise CatalogError(f"Claude agent {key} must be unquoted plain text")
        metadata[key] = value
    missing = sorted({"name", "description", "tools"} - metadata.keys())
    if missing:
        raise CatalogError(f"Claude agent is missing: {', '.join(missing)}")
    if not NAME_RE.match(metadata["name"]):
        raise CatalogError(
            "name must use lowercase letters, numbers, underscore, or hyphen"
        )
    tools = [item.strip() for item in metadata["tools"].split(",")]
    if not all(tools) or len(tools) != len(set(tools)):
        raise CatalogError("Claude agent tools must be a comma-separated list")
    metadata["tools"] = tools
    turns = metadata.get("maxTurns")
    if turns is not None and re.fullmatch(r"[1-9][0-9]{0,3}", turns) is None:
        raise CatalogError("Claude agent maxTurns must be a positive integer")
    prompt = text[end + 5:].strip()
    if not prompt:
        raise CatalogError("Claude agent prompt is empty")
    return metadata, prompt


def _claude_agent_candidate(
    path: Path,
    source_id: str,
    source_root: Path,
) -> dict[str, Any]:
    """Describe a Claude agent definition without granting it any authority.

    Claude definitions are inert: every problem, including a symlink or a file
    outside the project, is reported as a warning so that unrelated Claude files
    never block Codex readiness. A project mapping that names one rejects any
    warning before it can be routed.
    """

    candidate: dict[str, Any] = {
        "source": source_id,
        "kind": "agent",
        "name": path.stem,
        "format": "claude-md",
        "path": str(path.relative_to(source_root)),
        "requested_tools": [],
        "requested_mcps": [],
        "requested_skills": [],
        "content_sha256": None,
        "sandbox_mode": None,
        "configured_model": None,
        "configured_effort": None,
        "errors": [],
        "warnings": [],
    }
    try:
        if path.is_symlink():
            candidate["warnings"].append("Claude agent is a symlink")
            return candidate
        path.resolve().relative_to(source_root)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        candidate["warnings"].append(f"Claude agent is unavailable: {exc}")
        return candidate
    candidate["content_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        metadata, _ = parse_claude_agent(raw)
    except CatalogError as exc:
        candidate["warnings"].append(str(exc))
        return candidate
    if metadata["name"] != path.stem:
        candidate["warnings"].append("file stem differs from agent name")
    tools = metadata["tools"]
    candidate.update(
        name=metadata["name"],
        requested_tools=sorted(tools),
        sandbox_mode=claude_sandbox_mode(tools),
        configured_model=metadata.get("model"),
    )
    return candidate


def _group_key(candidate: dict[str, Any]) -> str:
    # Claude definitions are grouped apart, so they never conflict with a Codex
    # agent of the same name that a project already relies on.
    if candidate["format"] == "claude-md":
        return f"{candidate['kind']}:claude-md:{candidate['name']}"
    return f"{candidate['kind']}:{candidate['name']}"


def discover(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    allowed_tools = set(policy.get("allowed_legacy_tools", []))
    candidates: list[dict[str, Any]] = []
    for source in policy["sources"]:
        source_root = (root / source["root"]).resolve()
        try:
            source_root.relative_to(root)
        except ValueError as exc:
            raise CatalogError(f"source {source['id']} escapes trusted root") from exc
        if not source_root.exists():
            raise CatalogError(f"source root does not exist: {source_root}")
        for path in sorted((source_root / ".codex" / "agents").glob("*.toml")):
            _require_contained_file(path, source_root)
            candidates.append(_agent_candidate(path, source["id"], source_root))
        for path in sorted((source_root / ".agents" / "skills").glob("*/SKILL.md")):
            _require_contained_file(path, source_root)
            candidates.append(
                _markdown_candidate(
                    path,
                    source["id"],
                    source_root,
                    "skill",
                    "agent-skill",
                    allowed_tools,
                )
            )
        for path in sorted((source_root / ".claude" / "agents").glob("*.md")):
            # Containment is checked inside, as an inert warning.
            candidates.append(
                _claude_agent_candidate(path, source["id"], source_root)
            )
        for path in sorted((source_root / ".github" / "agents").glob("*.agent.md")):
            _require_contained_file(path, source_root)
            candidates.append(
                _markdown_candidate(
                    path,
                    source["id"],
                    source_root,
                    "agent",
                    "legacy-agent-markdown",
                    allowed_tools,
                )
            )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(_group_key(candidate), []).append(candidate)
    selected: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    resolutions = policy.get("resolutions", {})
    for key, items in sorted(grouped.items()):
        if len(items) == 1:
            selected.append(items[0])
            continue
        chosen_source = resolutions.get(key)
        matches = [item for item in items if item["source"] == chosen_source]
        if len(matches) == 1:
            selected.append(matches[0])
        else:
            conflicts.append(
                {"key": key, "sources": sorted(item["source"] for item in items)}
            )
    errors = [
        {"key": _group_key(item), "messages": item["errors"]}
        for item in candidates
        if item["errors"]
    ]
    unknown_resolution_keys = sorted(set(resolutions) - set(grouped))
    if unknown_resolution_keys:
        errors.append(
            {
                "key": "discovery-policy",
                "messages": [
                    "resolutions refer to undiscovered definitions: "
                    + ", ".join(unknown_resolution_keys)
                ],
            }
        )
    return {
        "candidates": candidates,
        "selected": selected,
        "conflicts": conflicts,
        "errors": errors,
        "ready": not conflicts and not errors,
    }
