"""Checks for runs opened by a Claude Code primary.

A native Claude child runs on its parent session's backend and model settings,
which a child process cannot observe directly. These checks deny the known
switches that would make it metered or change its model. They are best effort:
a parent session's backend cannot be proven from a child process.
"""

from __future__ import annotations

import json
import os
import pwd
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError

# Variables that move Claude Code off the subscription login onto a metered
# backend or another credential.
BACKEND_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
)
BACKEND_PREFIX = "CLAUDE_CODE_USE_"
# Settings keys that supply credentials outside the subscription login.
BACKEND_SETTINGS_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")
# Variables that change which model or effort a native child actually runs.
MODEL_OVERRIDE_VARIABLES = (
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)
# The Agent tool takes only aliases; the client resolves them to a model.
CLAUDE_MODEL_ALIASES = {
    "claude-opus-5-5": "opus",
    "claude-sonnet-5-5": "sonnet",
    "claude-haiku-5-5": "haiku",
}
# None means the account's own ~/.claude; tests point it elsewhere.
USER_SETTINGS_DIRECTORY: str | None = None
MAX_SETTINGS_BYTES = 1024 * 1024


class ClaudeHostDenied(ContractError):
    """A Claude-host open was refused; `reason_code` is stable and safe to print."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(f"Claude host open denied: {reason_code}")
        self.reason_code = reason_code


def user_settings_directory() -> Path:
    if USER_SETTINGS_DIRECTORY is not None:
        return Path(USER_SETTINGS_DIRECTORY)
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude"


def _backend_variable(name: str) -> bool:
    return name in BACKEND_VARIABLES or name.startswith(BACKEND_PREFIX)


def settings_files(project_root: Path) -> list[Path]:
    """The settings files a parent session would load, from fixed locations."""

    from .bridge import managed_settings_paths

    files = [
        user_settings_directory() / "settings.json",
        project_root / ".claude" / "settings.json",
        project_root / ".claude" / "settings.local.json",
    ]
    for managed in managed_settings_paths():
        path = Path(managed)
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            files.append(path)
    return files


def _settings(path: Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
        value = json.loads(raw) if len(raw) <= MAX_SETTINGS_BYTES else None
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict):
        # A settings file that cannot be read cannot be shown to be safe.
        raise ClaudeHostDenied("claude_backend_not_subscription")
    return value


def check_backend(environ: Mapping[str, str], project_root: Path) -> None:
    """Deny a Claude-host open whose session could run on a metered backend."""

    from .bridge import managed_preference_paths

    # MDM preferences can set any setting, including a metered backend, and
    # are not read here, so their presence refuses.
    if any(os.path.lexists(path) for path in managed_preference_paths()):
        raise ClaudeHostDenied("claude_backend_not_subscription")
    if any(_backend_variable(name) and value for name, value in environ.items()):
        raise ClaudeHostDenied("claude_backend_not_subscription")
    config_directory = environ.get("CLAUDE_CONFIG_DIR")
    if config_directory and os.path.realpath(config_directory) != os.path.realpath(
        user_settings_directory()
    ):
        # The identity observation read the default directory, not this one.
        raise ClaudeHostDenied("claude_backend_not_subscription")
    for path in settings_files(project_root):
        settings = _settings(path)
        environment = settings.get("env")
        if any(key in settings for key in BACKEND_SETTINGS_KEYS) or (
            isinstance(environment, dict)
            and any(_backend_variable(str(name)) for name in environment)
        ):
            raise ClaudeHostDenied("claude_backend_not_subscription")


def check_model_overrides(environ: Mapping[str, str], project_root: Path) -> None:
    if any(environ.get(name) for name in MODEL_OVERRIDE_VARIABLES):
        raise ClaudeHostDenied("claude_model_override_present")
    for path in settings_files(project_root):
        environment = _settings(path).get("env")
        if isinstance(environment, dict) and any(
            name in MODEL_OVERRIDE_VARIABLES for name in environment
        ):
            raise ClaudeHostDenied("claude_model_override_present")


def native_spawn(route: dict[str, Any]) -> dict[str, str]:
    """The Agent tool parameters for a native Claude child, from the route alone."""

    alias = CLAUDE_MODEL_ALIASES.get(route.get("model"))
    agent = route.get("agent_type", route.get("role"))
    if alias is None or not isinstance(agent, str) or not isinstance(route.get("effort"), str):
        raise ContractError("route has no native Claude spawn parameters")
    return {"subagent_type": agent, "model_alias": alias, "effort": route["effort"]}


def is_native_claude(route: dict[str, Any]) -> bool:
    return route.get("provider") == "claude" and route.get("dispatch") == "native"
