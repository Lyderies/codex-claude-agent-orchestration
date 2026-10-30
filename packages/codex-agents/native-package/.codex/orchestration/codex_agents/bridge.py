"""Bridge one admitted host run to a read-only child of the other provider.

The bridge never runs a child in the repository: it exports the committed bytes
of the assignment's read paths into a private directory, launches the child
there once, and fences any outcome it cannot prove. It records only digests,
counts and typed codes; prompts and file contents never reach the ledger.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import CatalogError, parse_claude_agent
from .contracts import ContractError, result_preflight, validate_assignment
from .learning_contracts import SUPPORTED_MODELS
from .providers import (
    api_keys_present,
    client_environment,
    observation_failure_reason,
    resolve_client,
)

MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_EXPORT_FILES = 2_000
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
CLAUDE_TOOLS = ("Read", "Glob", "Grep")
# Known built-in plugins are switched off; the init-frame check still fails
# closed if any plugin loads, so this list is a convenience, not the control.
CLAUDE_DISABLED_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
    "cc-plugin-easel@builtin",
)
CODEX_REROUTE_EVENTS = {"model/rerouted", "model_rerouted", "model.rerouted"}
# Codex features switched off for every child. `code_mode_host` must stay enabled:
# disabling it breaks Codex's own tool invocation.
# These disables plus `--ephemeral` are the pre-dispatch denial, and they are only
# demonstrated on codex-cli 0.164.0: under `--ephemeral`, `spawn_agent` fails before
# any sub-agent run because no rollout exists, and no item event is emitted for the
# attempt. The reviewed Codex range in config/routing-policy.json is therefore
# pinned to that build; widening it requires a live containment check on the new build.
CODEX_DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2",
    "image_generation", "goals", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "hooks",
)
CODEX_WEB_SEARCH_SETTING = 'web_search="disabled"'
# Collaboration tools cannot be switched off by configuration. This allowlist only
# detects emitted, typed events: anything outside it stops the child, but it does not
# prevent a call and cannot see one that emits no event.
CODEX_ALLOWED_EVENTS = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
    "item.started", "item.updated", "item.completed",
})
CODEX_ITEM_EVENTS = frozenset({"item.started", "item.updated", "item.completed"})
CODEX_ALLOWED_ITEMS = frozenset({
    "agent_message", "reasoning", "command_execution", "file_change", "todo_list",
    "error",
})
# Claude Code bills a small helper model for housekeeping (seen live in modelUsage
# while every assistant frame came from the requested model). Helper usage is
# reported, but only the assistant frames decide whether the work was rerouted.
CLAUDE_HELPER_MODELS = frozenset({"claude-haiku-5-5"})
# Claude Code reads these from its working directory and the directories above
# it, so they are never exported and must not exist above the export.
CLAUDE_CONTEXT_NAMES = {"claude.md", "claude.local.md", ".claude", ".mcp.json"}
# Enterprise managed settings apply even with --setting-sources project and are
# not visible in the init frame, so their presence refuses a Claude launch.
MANAGED_SETTINGS_PATHS = {
    "darwin": (
        "/Library/Application Support/ClaudeCode/managed-settings.json",
        "/Library/Application Support/ClaudeCode/managed-settings.d",
    ),
    "linux": (
        "/etc/claude-code/managed-settings.json",
        "/etc/claude-code/managed-settings.d",
    ),
}
# Device-management (MDM) preferences for Claude Code also apply despite
# --setting-sources and cannot be read as settings files here, so any of them
# refuses. `{user}` is the account name from the password database.
MANAGED_PREFERENCE_PATHS = {
    "darwin": (
        "/Library/Managed Preferences/com.anthropic.claudecode.plist",
        "/Library/Managed Preferences/{user}/com.anthropic.claudecode.plist",
    ),
    "linux": (),
}
# Exports live under the resolved system temp directory, not under home, so no
# user-level CLAUDE.md or .claude directory sits above them. None means that.
WORKSPACE_PARENT: str | None = None
WATCHDOG_GRACE_SECONDS = 2
PATHSPEC_SPECIAL = set("*?[\\")
MAX_TOKEN_COUNT = 2**53


class BridgeRefused(ContractError):
    """A precondition failed before any child was launched."""


class _Abort(Exception):
    """Stop the child: a frame broke the launch contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def assignment_read_paths(assignment: dict[str, Any]) -> list[str]:
    """The repository paths a child may read: context plus owned paths."""

    granted = _granted_paths(assignment)
    paths: set[str] = set()
    for value in (*assignment.get("context", []), *assignment.get("owned_paths", [])):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise BridgeRefused("assignment read path is invalid")
        # Git pathspec magic (":", globs) could widen the export to the whole
        # repository, so only plain literal paths are accepted.
        if value.startswith(":") or set(value) & PATHSPEC_SPECIAL:
            raise BridgeRefused("assignment read path must be a plain literal path")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in path.parts):
            raise BridgeRefused("assignment read path must stay inside the repository")
        if _claude_context_path(path.as_posix()):
            raise BridgeRefused("assignment read path names Claude context files")
        if not _path_granted(path.as_posix(), granted):
            raise BridgeRefused("export_path_not_granted")
        paths.add(path.as_posix())
    if not paths:
        raise BridgeRefused("assignment names no read paths to export")
    return sorted(paths)


def _granted_paths(assignment: dict[str, Any]) -> list[str]:
    """The paths the assignment's filesystem permissions name, read or write.

    Permissions are written `<path>:read` or `<path>:write` and are already a
    subset of the parent grant. Anything else grants nothing.
    """

    permissions = assignment.get("permissions")
    entries = permissions.get("filesystem") if isinstance(permissions, dict) else None
    granted = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, str):
            continue
        path, _, access = entry.rpartition(":")
        if access in {"read", "write"} and path:
            granted.append(PurePosixPath(path).as_posix())
    return granted


def _path_granted(relative: str, granted: list[str]) -> bool:
    """A permission covers its own path and, for a directory, what lies beneath.

    Coverage is by whole path components, so `src` never covers `src2/x`.
    """

    return any(relative == path or relative.startswith(path + "/") for path in granted)


def _claude_context_path(relative: str) -> bool:
    return any(part.lower() in CLAUDE_CONTEXT_NAMES for part in PurePosixPath(relative).parts)


def _git(root: Path, *arguments: str, text: bool = False) -> subprocess.CompletedProcess:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise BridgeRefused("git is unavailable")
    try:
        return subprocess.run(
            [executable, "--literal-pathspecs", "-C", str(root), *arguments],
            env={
                "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "GIT_LITERAL_PATHSPECS": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=text,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeRefused("git export failed") from exc


def export_committed_paths(
    repository_root: Path,
    paths: list[str],
    destination: Path,
    *,
    max_bytes: int = MAX_EXPORT_BYTES,
    max_files: int = MAX_EXPORT_FILES,
) -> int:
    """Copy the committed bytes of each path from HEAD; return the file count.

    Only regular blobs are exported, and Claude context files are dropped. A
    symlink, submodule, missing path, a listing outside the request, or an
    export over the caps refuses the whole dispatch.
    """

    root = repository_root.resolve()
    top = _git(root, "rev-parse", "--show-toplevel", text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        raise BridgeRefused("project root is not the top of a git repository")
    entries: dict[str, tuple[str, str]] = {}
    for requested in paths:
        listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", requested)
        if listing.returncode != 0:
            raise BridgeRefused("assignment read path is not committed")
        found = [item for item in listing.stdout.split(b"\x00") if item]
        if not found:
            raise BridgeRefused("assignment read path is not committed")
        for item in found:
            header, _, name = item.partition(b"\t")
            try:
                mode, kind, sha = header.decode("ascii").split(" ")
                relative = name.decode("utf-8", "strict")
            except ValueError as exc:
                raise BridgeRefused("assignment read path is not exportable") from exc
            # Every listed file must be the requested file or sit beneath it.
            if relative != requested and not relative.startswith(requested + "/"):
                raise BridgeRefused("git listed a path outside the requested paths")
            if _claude_context_path(relative):
                continue
            if mode == "120000":
                raise BridgeRefused("assignment read paths must not contain symlinks")
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise BridgeRefused("assignment read paths must be regular files")
            entries[relative] = (sha, mode)
    if len(entries) > max_files:
        raise BridgeRefused("assignment export has too many files")
    total = 0
    for relative, (sha, _) in sorted(entries.items()):
        parts = PurePosixPath(relative).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise BridgeRefused("assignment read path must stay inside the repository")
        blob = _git(root, "cat-file", "blob", sha)
        if blob.returncode != 0:
            raise BridgeRefused("assignment read path is unavailable")
        total += len(blob.stdout)
        if total > max_bytes:
            raise BridgeRefused("assignment export is too large")
        target = destination.joinpath(*parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob.stdout)
    return len(entries)


_ACTIVE_GROUPS: set[int] = set()
# A detached watchdog enforces the deadline even if the bridge is SIGKILLed: it
# polls the child's process group, kills it at the deadline, and exits as soon
# as the group is gone.
WATCHDOG_SOURCE = """
import os, signal, sys, time
group, deadline = int(sys.argv[1]), float(sys.argv[2])
while True:
    try:
        os.killpg(group, 0)
    except OSError:
        sys.exit(0)
    if time.time() >= deadline:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
        sys.exit(0)
    time.sleep(0.2)
"""


def _start_watchdog(group: int, deadline_epoch: float) -> None:
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-c", WATCHDOG_SOURCE, str(group), repr(deadline_epoch)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={"PATH": os.defpath},
        )
    except OSError as exc:
        # Without the watchdog the deadline would rest on this process alone.
        os.killpg(group, signal.SIGKILL)
        raise _Abort("watchdog_unavailable") from exc


class BridgeInterrupted(SystemExit):
    """The bridge received a termination signal; children are already killed."""


@contextlib.contextmanager
def signal_guard():
    """Kill every running child group on SIGTERM, SIGINT or SIGHUP, then exit.

    Raising out of the wait lets each dispatch's own cleanup remove its export.
    """

    def handler(signum: int, _frame: Any) -> None:
        for group in list(_ACTIVE_GROUPS):
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        raise BridgeInterrupted(128 + signum)

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {signum: signal.signal(signum, handler) for signum in signals}
    try:
        yield
    finally:
        for signum, value in previous.items():
            signal.signal(signum, value)


def _stop_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_child(
    arguments: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    deadline_seconds: float,
    on_frame: Callable[[dict[str, Any]], None],
    popen: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Run one child in its own session, feeding each JSON line to `on_frame`.

    The whole process group is killed on timeout, overflow, an unparseable line
    or an aborting frame, and always once the child has exited.
    """

    outcome: dict[str, Any] = {"returncode": None, "abort": None}
    try:
        process = popen(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return {"returncode": None, "abort": "launch_failed"}
    _ACTIVE_GROUPS.add(process.pid)
    deadline = time.monotonic() + deadline_seconds
    buffer = b""
    received = 0
    try:
        _start_watchdog(
            process.pid, time.time() + deadline_seconds + WATCHDOG_GRACE_SECONDS
        )
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise _Abort("timed_out")
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_STREAM_BYTES:
                    raise _Abort("stream_too_large")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > MAX_LINE_BYTES:
                        raise _Abort("stream_too_large")
                    if not line.strip():
                        continue
                    _deliver(line, on_frame)
                if len(buffer) > MAX_LINE_BYTES:
                    raise _Abort("stream_too_large")
            if len(buffer) > MAX_LINE_BYTES:
                raise _Abort("stream_too_large")
            if buffer.strip():
                _deliver(buffer, on_frame)
        try:
            outcome["returncode"] = process.wait(
                timeout=max(deadline - time.monotonic(), 0.1)
            )
        except subprocess.TimeoutExpired as exc:
            raise _Abort("timed_out") from exc
    except _Abort as abort:
        outcome["abort"] = abort.reason
    finally:
        _stop_group(process)
        _ACTIVE_GROUPS.discard(process.pid)
        process.stdout.close()
    return outcome


def _deliver(line: bytes, on_frame: Callable[[dict[str, Any]], None]) -> None:
    try:
        frame = json.loads(line)
    except ValueError as exc:
        raise _Abort("unparseable_stream") from exc
    if not isinstance(frame, dict):
        raise _Abort("unparseable_stream")
    on_frame(frame)


def _result_object(text: Any) -> dict[str, Any] | None:
    """Parse the child's final message as one JSON object, tolerating a fence."""

    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        return None
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _fenced_result(assignment: dict[str, Any], state: str, reason: str) -> dict[str, Any]:
    # An outcome the bridge cannot prove is recorded as a failure, never success.
    return {
        "assignment_id": assignment["assignment_id"],
        "status": "failed",
        "execution_state": state,
        "semantic_verdict": "not-applicable",
        "admission_state": "unverified",
        "retry": {
            "attempt": assignment.get("attempt", 1),
            "retry_of_assignment_id": assignment.get("retry_of_assignment_id"),
        },
        "summary": f"Bridged child produced no usable result ({reason}).",
        "artifacts": [],
        "evidence": [],
        "unverified_claims": [],
        "remaining_risks": [f"bridge outcome is ambiguous: {reason}"],
        "usage": None,
    }


def _is_object_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    return "properties" in schema or kind == "object" or (
        isinstance(kind, list) and "object" in kind
    )


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Derive the strict structured-output variant Codex requires.

    Every object lists all its properties as required and closes them, each
    originally optional property becomes nullable, and the top-level `$schema`
    and `$id` are dropped. The input is not modified.
    """

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        converted = {key: convert(value) for key, value in node.items()
                     if key != "properties"}
        if _is_object_schema(node):
            properties = node.get("properties", {})
            required = set(node.get("required", []))
            converted["properties"] = {
                name: (
                    convert(value) if name in required
                    else {"anyOf": [convert(value), {"type": "null"}]}
                )
                for name, value in properties.items()
            }
            converted["required"] = list(properties)
            converted["additionalProperties"] = False
        return converted

    strict = convert(schema)
    strict.pop("$schema", None)
    strict.pop("$id", None)
    return strict


def remove_optional_nulls(value: Any, schema: dict[str, Any]) -> Any:
    """Undo the strict variant's nulls: drop null values of optional keys only.

    A null in a required key is kept, so normal result validation still fails.
    """

    if isinstance(value, list):
        items = schema.get("items") if isinstance(schema, dict) else None
        return [remove_optional_nulls(item, items or {}) for item in value]
    if not isinstance(value, dict) or not _is_object_schema(schema):
        return value
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    return {
        key: remove_optional_nulls(item, properties.get(key, {}))
        for key, item in value.items()
        if not (item is None and key in properties and key not in required)
    }


def child_prompt(assignment: dict[str, Any]) -> str:
    template = result_preflight(assignment)["template"]
    return (
        f"{assignment['objective']}\n\n"
        "Work only with the files in the current directory. When you finish, reply "
        "with exactly one JSON object and no other text, filling in this template:\n"
        f"{json.dumps(template, indent=2)}\n"
    )


def claude_settings(model: str) -> dict[str, Any]:
    return {
        "disableAllHooks": True,
        "availableModels": [model],
        "switchModelsOnFlag": False,
        "permissions": {"defaultMode": "dontAsk"},
        "enabledPlugins": {name: False for name in CLAUDE_DISABLED_PLUGINS},
    }


def _private_file(path: Path, content: bytes) -> Path:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    return path


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        return None
    return value


def _claude_model_usage(model_usage: Any) -> list[dict[str, Any]]:
    """Per-model token counts from Claude's `modelUsage`; unknown names are unnamed."""

    if not isinstance(model_usage, dict):
        return []
    merged: dict[str, dict[str, Any]] = {}
    for name, entry in model_usage.items():
        if not isinstance(entry, dict):
            continue
        model = name if name in SUPPORTED_MODELS and name.startswith("claude-") else None
        counts = {
            "input_tokens": _token_count(entry.get("inputTokens")),
            "cached_input_tokens": _token_count(entry.get("cacheReadInputTokens")),
            "cache_creation_input_tokens": _token_count(entry.get("cacheCreationInputTokens")),
            "output_tokens": _token_count(entry.get("outputTokens")),
            "reasoning_output_tokens": None,
        }
        key = model or ""
        if key in merged:
            # Unnamed models are summed; a missing count makes the sum unknown.
            for field, value in counts.items():
                previous = merged[key][field]
                merged[key][field] = (
                    None if previous is None or value is None else previous + value
                )
        else:
            merged[key] = {"model": model, **counts}
    return [merged[key] for key in sorted(merged)]


def _check_claude_init(frame: dict[str, Any], model: str) -> str | None:
    """Return the first init-frame violation, or None when the frame is clean."""

    if frame.get("apiKeySource") != "none":
        return "api_key_source"
    if frame.get("model") != model:
        return "model_mismatch"
    if frame.get("plugins") != []:
        return "plugin_loaded"
    if frame.get("mcp_servers") != []:
        return "mcp_server_loaded"
    tools = frame.get("tools")
    if not isinstance(tools, list) or not set(tools) <= set(CLAUDE_TOOLS):
        return "tool_not_allowed"
    return None


def run_claude(
    *,
    ledger: Any,
    project_id: str,
    run_handle: str,
    route: dict[str, Any],
    assignment: dict[str, Any],
    role_prompt: str,
    export: Path,
    control: Path,
    environ: Mapping[str, str],
    deadline_seconds: float,
    max_turns: int,
    executable: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    from .learning_store import LedgerError

    model = route["model"]
    settings = _private_file(
        control / "settings.json", json.dumps(claude_settings(model)).encode()
    )
    system_prompt = _private_file(control / "role-prompt.md", role_prompt.encode())
    arguments = [
        executable, "-p",
        "--model", model,
        "--effort", route["effort"],
        "--tools", ",".join(CLAUDE_TOOLS),
        "--permission-mode", "dontAsk",
        "--setting-sources", "project",
        "--settings", str(settings),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
        "--append-system-prompt-file", str(system_prompt),
        child_prompt(assignment),
    ]
    state: dict[str, Any] = {
        "init": None, "result": None, "rate_limit_events": 0, "rate_limit_kept": 0,
        "rate_limit_failed": False, "frames": 0,
        "models": set(),
        "helper_models": set(),
    }

    def on_frame(frame: dict[str, Any]) -> None:
        state["frames"] += 1
        kind = frame.get("type")
        if state["init"] is None:
            if kind != "system" or frame.get("subtype") != "init":
                raise _Abort("missing_init_frame")
            violation = _check_claude_init(frame, model)
            if violation is not None:
                raise _Abort(f"init_{violation}")
            state["init"] = frame
            return
        if kind == "rate_limit_event":
            # Every event goes to the ledger, which keeps each one that is more
            # restrictive than the run's earlier events.
            state["rate_limit_events"] += 1
            try:
                kept = ledger.record_claude_rate_limit_event(
                    project_id,
                    run_handle=run_handle,
                    rate_limit_info=frame.get("rate_limit_info"),
                )
            except (ContractError, LedgerError) as exc:
                # The reading is lost, so say so in the ledger before stopping.
                state["rate_limit_failed"] = True
                with contextlib.suppress(ContractError, LedgerError):
                    ledger.record_observation_unavailable(
                        project_id, run_handle=run_handle, provider="claude",
                        phase="during", reason_code="observation_failed",
                    )
                raise _Abort("rate_limit_event_invalid") from exc
            state["rate_limit_kept"] += len(kept)
            # Paid usage or a rejection seen mid-run stops the child at once.
            if any(
                record["record_type"] == "provider_billing"
                and (record["paid_usage_usable"] is True or record["is_using_overage"] is True)
                for record in kept
            ):
                raise _Abort("paid_usage_observed")
            if any(
                record["record_type"] == "provider_headroom" and record["status"] == "rejected"
                for record in kept
            ):
                raise _Abort("provider_rejected")
        elif kind == "result":
            state["result"] = frame
            usage = frame.get("modelUsage")
            if isinstance(usage, dict):
                for name in usage:
                    name = str(name)
                    if name == model:
                        continue
                    if name in CLAUDE_HELPER_MODELS:
                        state["helper_models"].add(name)
                    else:
                        state["models"].add(name)
        if kind == "assistant":
            message = frame.get("message")
            served = message.get("model") if isinstance(message, dict) else None
            if served is not None:
                state["models"].add(str(served))

    started = time.monotonic()
    outcome = run_child(
        arguments,
        environment=client_environment(environ),
        cwd=export,
        deadline_seconds=deadline_seconds,
        on_frame=on_frame,
    )
    report: dict[str, Any] = {
        "provider": "claude",
        "requested_model": model,
        "observed_model": (state["init"] or {}).get("model"),
        "effort": route["effort"],
        "wall_seconds": round(time.monotonic() - started, 3),
        "read_containment": "enforced",
        # A fallback after a clean init serves another model; any working model
        # other than the requested one leaves the result unreviewed.
        "rerouted": bool(state["models"] - {model}),
        "helper_models": sorted(state["helper_models"]),
        "rate_limit_event_recorded": state["rate_limit_kept"] > 0,
        "rate_limit_events": state["rate_limit_events"],
        "rate_limit_records_kept": state["rate_limit_kept"],
        "rate_limit_record_failed": state["rate_limit_failed"],
        "usage": None,
        "permission_denials": None,
    }
    result_frame = state["result"]
    if isinstance(result_frame, dict):
        usage = result_frame.get("usage") if isinstance(result_frame.get("usage"), dict) else {}
        report["usage"] = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            # An API-list-price estimate reported by the client; never a charge.
            "api_equivalent_cost_usd_estimate": result_frame.get("total_cost_usd"),
        }
        report["model_usage"] = _claude_model_usage(result_frame.get("modelUsage"))
        denials = result_frame.get("permission_denials")
        if isinstance(denials, list):
            report["permission_denials"] = {
                "count": len(denials),
                "tools": sorted({
                    str(item.get("tool_name")) for item in denials
                    if isinstance(item, dict) and isinstance(item.get("tool_name"), str)
                }),
            }
    if outcome["abort"] is not None:
        return None, {**report, "outcome": outcome["abort"]}
    if outcome["returncode"] != 0 or not isinstance(result_frame, dict):
        return None, {**report, "outcome": "missing_result"}
    if result_frame.get("is_error") is not False or result_frame.get("subtype") != "success":
        return None, {**report, "outcome": "child_reported_error"}
    result = _result_object(result_frame.get("result"))
    if result is None:
        return None, {**report, "outcome": "unparseable_result"}
    return result, {**report, "outcome": "result"}


def _event_label(value: Any) -> str:
    """A bounded, printable event or item type for an abort reason."""

    if not isinstance(value, str) or not value:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_./-]", "?", value[:64])


def run_codex(
    *,
    ledger: Any,
    project_id: str,
    route: dict[str, Any],
    assignment: dict[str, Any],
    export: Path,
    control: Path,
    schema: bytes,
    environ: Mapping[str, str],
    deadline_seconds: float,
    executable: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    model = route["model"]
    try:
        original_schema = json.loads(schema)
    except ValueError as exc:
        raise BridgeRefused("result schema is invalid") from exc
    schema_path = _private_file(
        control / "result.schema.json",
        json.dumps(strict_output_schema(original_schema), sort_keys=True).encode(),
    )
    last_message = control / "last-message.json"
    arguments = [
        executable, "exec",
        "-m", model,
        "-c", f"model_reasoning_effort={route['effort']}",
        "-s", "read-only",
        "--ephemeral",
        "--ignore-user-config",
        *(
            part
            for feature in CODEX_DISABLED_FEATURES
            for part in ("--disable", feature)
        ),
        "-c", CODEX_WEB_SEARCH_SETTING,
        "--skip-git-repo-check",
        "-C", str(export),
        "--json",
        "--output-schema", str(schema_path),
        "-o", str(last_message),
        child_prompt(assignment),
    ]
    state: dict[str, Any] = {
        "usage": None, "rerouted": False, "observed_model": None, "failed": False,
    }

    def on_frame(frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if not isinstance(kind, str):
            raise _Abort("codex_tool_not_allowed:unknown")
        if kind in CODEX_REROUTE_EVENTS:
            state["rerouted"] = True
        elif kind not in CODEX_ALLOWED_EVENTS:
            raise _Abort(f"codex_tool_not_allowed:{_event_label(kind)}")
        elif kind in CODEX_ITEM_EVENTS:
            item = frame.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if not isinstance(item_type, str) or item_type not in CODEX_ALLOWED_ITEMS:
                raise _Abort(f"codex_tool_not_allowed:{_event_label(item_type)}")
        for key in ("model", "to_model"):
            value = frame.get(key)
            if isinstance(value, str):
                state["observed_model"] = value
                if value != model:
                    state["rerouted"] = True
        if kind == "turn.completed" and isinstance(frame.get("usage"), dict):
            usage = frame["usage"]
            state["usage"] = {
                name: usage.get(name)
                for name in (
                    "input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens",
                )
            }
        if kind in {"turn.failed", "error"}:
            state["failed"] = True

    started = time.monotonic()
    outcome = run_child(
        arguments,
        environment=client_environment(environ),
        cwd=export,
        deadline_seconds=deadline_seconds,
        on_frame=on_frame,
    )
    report = {
        "provider": "codex",
        "requested_model": model,
        "observed_model": state["observed_model"] or model,
        "effort": route["effort"],
        "wall_seconds": round(time.monotonic() - started, 3),
        "read_containment": "advisory",
        "rerouted": state["rerouted"],
        "usage": state["usage"],
    }
    if outcome["abort"] is not None:
        return None, {**report, "outcome": outcome["abort"]}
    if outcome["returncode"] != 0 or state["failed"]:
        return None, {**report, "outcome": "child_reported_error"}
    try:
        descriptor = os.open(last_message, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None, {**report, "outcome": "missing_result"}
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None, {**report, "outcome": "missing_result"}
        raw = handle.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        return None, {**report, "outcome": "unparseable_result"}
    result = _result_object(raw.decode("utf-8", "replace"))
    if result is None:
        return None, {**report, "outcome": "unparseable_result"}
    # The strict schema makes Codex fill optional keys with null; remove those
    # so the result goes through the ordinary validation unchanged.
    return remove_optional_nulls(result, original_schema), {**report, "outcome": "result"}


def managed_settings_paths() -> tuple[str, ...]:
    platform = "darwin" if sys.platform == "darwin" else "linux"
    return MANAGED_SETTINGS_PATHS[platform]


def managed_preference_paths() -> tuple[str, ...]:
    import pwd

    platform = "darwin" if sys.platform == "darwin" else "linux"
    user = pwd.getpwuid(os.getuid()).pw_name
    return tuple(
        path.format(user=user) for path in MANAGED_PREFERENCE_PATHS[platform]
    )


def managed_configuration_present() -> bool:
    """Whether enterprise managed settings or MDM preferences exist for Claude Code."""

    return any(
        os.path.lexists(path)
        for path in (*managed_settings_paths(), *managed_preference_paths())
    )


def check_context_boundary(workspace: Path) -> None:
    """Refuse when Claude would load context from a directory above the export.

    The rule: no directory from the workspace up to `/` may hold CLAUDE.md,
    CLAUDE.local.md or a .claude directory. There is no exception for the
    user's own ~/.claude; exports live under the system temp directory, so a
    workspace under home (an unusual TMPDIR) is refused rather than trusted.
    """

    current = workspace.resolve()
    while True:
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude"):
            if os.path.lexists(current / name):
                raise BridgeRefused("context_boundary_violation")
        if current.parent == current:
            return
        current = current.parent


PROBE_MODEL = "claude-haiku-5-5"
PROBE_EFFORT = "low"
PROBE_PROMPT = "Reply with the single word OK."
PROBE_DEADLINE_SECONDS = 120
MAX_PROBE_EVENTS = 64


def claude_billing_probe(environ: Mapping[str, str]) -> tuple[list[Any], str | None]:
    """Run one minimal locked Claude turn; return every rate_limit_info it reported
    and why it failed, if it did.

    The events seen are returned even when the probe aborts or times out, so
    the ledger never loses a restriction the probe already received.

    It uses a little subscription allowance but is never metered: it refuses an
    API-key or alternate-backend environment, runs Haiku at low effort with no
    tools in an empty private directory, and keeps only the rate-limit events.
    """

    from .claude_host import check_backend

    if api_keys_present(environ):
        raise BridgeRefused("a provider API key is set")
    parent = Path(os.path.realpath(WORKSPACE_PARENT or tempfile.gettempdir()))
    workspace = Path(tempfile.mkdtemp(prefix="codex-agents-probe-", dir=parent))
    try:
        os.chmod(workspace, 0o700)
        check_context_boundary(workspace)
        if managed_configuration_present():
            raise BridgeRefused("managed_settings_present")
        empty = workspace / "empty"
        control = workspace / "control"
        empty.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        check_backend(environ, empty)
        executable, _ = resolve_client("claude")
        settings = _private_file(
            control / "settings.json", json.dumps(claude_settings(PROBE_MODEL)).encode()
        )
        arguments = [
            executable, "-p",
            "--model", PROBE_MODEL,
            "--effort", PROBE_EFFORT,
            "--tools", "",
            "--permission-mode", "dontAsk",
            "--setting-sources", "project",
            "--settings", str(settings),
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "1",
            PROBE_PROMPT,
        ]
        state: dict[str, Any] = {"init": False, "infos": []}

        def on_frame(frame: dict[str, Any]) -> None:
            if not state["init"]:
                if frame.get("type") != "system" or frame.get("subtype") != "init":
                    raise _Abort("missing_init_frame")
                violation = _check_claude_init(frame, PROBE_MODEL)
                if violation is not None or frame.get("tools") != []:
                    raise _Abort(f"init_{violation or 'tool_not_allowed'}")
                state["init"] = True
            elif frame.get("type") == "rate_limit_event":
                # Every event is kept; the ledger merges them conservatively.
                # The event that reaches the cap is kept before the probe stops.
                state["infos"].append(frame.get("rate_limit_info"))
                if len(state["infos"]) >= MAX_PROBE_EVENTS:
                    raise _Abort("too_many_rate_limit_events")

        outcome = run_child(
            arguments,
            environment=client_environment(environ),
            cwd=empty,
            deadline_seconds=PROBE_DEADLINE_SECONDS,
            on_frame=on_frame,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return state["infos"], outcome["abort"]


def dispatch(
    ledger: Any,
    project_id: str,
    *,
    project_root: Path,
    bundle_root: Path,
    task: dict[str, Any],
    route: dict[str, Any],
    assignment: dict[str, Any],
    parent_grant: dict[str, Any],
    run_handle: str,
    result_schema: bytes,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one bridged child for an opened, admitted host run.

    Preconditions are checked before anything is exported or launched. After
    the durable dispatch intent is written the run can never be dispatched again,
    whatever happens to the child.
    """

    environ = os.environ if environ is None else environ
    if route.get("dispatch") != "bridge":
        raise BridgeRefused("route is not a bridge dispatch")
    provider = route.get("provider")
    if provider not in {"codex", "claude"}:
        raise BridgeRefused("route provider is invalid")
    permissions = assignment.get("permissions", {}) if isinstance(assignment, dict) else {}
    if (
        route.get("sandbox_mode") != "read-only"
        or permissions.get("network") is not False
        or any(not item.endswith(":read") for item in permissions.get("filesystem", []))
    ):
        raise BridgeRefused("bridged children are read-only")
    validate_assignment(assignment, task=task, route=route, parent_grant=parent_grant)
    admission = ledger.host_run_admission(project_id, run_handle)
    if admission["provider"] != provider:
        raise BridgeRefused("host run admission differs from the route provider")
    ledger.record_dispatch_intent(
        project_id, run_handle=run_handle, assignment=assignment, task=task,
        route=route, deadline_seconds=1, claim=False,
    )
    if api_keys_present(environ):
        raise BridgeRefused("a provider API key is set")
    paths = assignment_read_paths(assignment)
    role_prompt = ""
    if provider == "claude":
        role_prompt = _claude_role_prompt(bundle_root, route)
    # The tighter of the route's and the assignment's bounds applies.
    route_bounds, assigned_bounds = route["bounds"], assignment["bounds"]
    deadline_seconds = max(1, min(
        int(route_bounds["timeout_seconds"]), int(assigned_bounds["timeout_seconds"])
    ))
    max_turns = max(1, min(
        int(route_bounds["max_tool_calls"]), int(assigned_bounds["max_tool_calls"])
    ))
    if provider == "claude":
        if managed_configuration_present():
            raise BridgeRefused("managed_settings_present")
    parent = Path(os.path.realpath(WORKSPACE_PARENT or tempfile.gettempdir()))
    workspace = Path(tempfile.mkdtemp(prefix="codex-agents-dispatch-", dir=parent))
    try:
        os.chmod(workspace, 0o700)
        if provider == "claude":
            check_context_boundary(workspace)
        export = workspace / "export"
        control = workspace / "control"
        export.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        file_count = export_committed_paths(project_root, paths, export)
        # Re-check the account immediately before launch: it must still be the
        # admitted one, on the admitted reviewed client, and the environment
        # must still be key-free.
        try:
            identity = ledger.observe_provider(
                project_id, provider, project_root=export, environ=environ,
                run_handle=run_handle, phase="before",
            )[0]
        except ContractError as exc:
            ledger.record_observation_unavailable(
                project_id, run_handle=run_handle, provider=provider, phase="before",
                reason_code=observation_failure_reason(exc),
            )
            raise BridgeRefused("provider observation before launch is unavailable") from exc
        if identity["account_fingerprint"] != admission["account_fingerprint"]:
            raise BridgeRefused("provider account differs from the admitted account")
        admitted = ledger.admitted_client(project_id, run_handle)
        if (
            identity["client_version"] is None
            or identity["client_version"] != admitted["client_version"]
        ):
            raise BridgeRefused("provider client version differs from the admitted client")
        if not ledger.client_version_reviewed(provider, identity["client_version"]):
            raise BridgeRefused("provider client version is not reviewed")
        # The child is launched from the client the re-check just read.
        executable, digest = resolve_client(provider)
        if (executable, digest) != (identity["client_path"], identity["client_sha256"]):
            raise BridgeRefused("provider client changed during the launch re-check")
        # Billing, headroom and plan are re-read too, account-wide and including
        # the fresh reading above; the run cap is not counted again.
        denied = ledger.launch_recheck(project_id, run_handle=run_handle, identity=identity)
        if denied is not None:
            raise BridgeRefused(f"launch denied: {denied}")
        if api_keys_present(environ):
            raise BridgeRefused("a provider API key is set")
        intent = ledger.record_dispatch_intent(
            project_id, run_handle=run_handle, assignment=assignment, task=task,
            route=route, deadline_seconds=deadline_seconds,
        )
        ledger.bind_host_agent(
            project_id, run_handle=run_handle, agent_id=intent["bridge_run_id"]
        )
        if provider == "claude":
            result, report = run_claude(
                ledger=ledger, project_id=project_id, run_handle=run_handle,
                route=route, assignment=assignment, role_prompt=role_prompt,
                export=export, control=control, environ=environ,
                deadline_seconds=deadline_seconds, max_turns=max_turns,
                executable=executable,
            )
            if report["rate_limit_record_failed"]:
                report.setdefault("observations_unavailable", []).append(
                    {"phase": "during", "reason_code": "observation_failed"}
                )
            elif report["rate_limit_events"] == 0:
                # Without an event the run left no headroom or billing reading.
                _record_unavailable(
                    ledger, project_id, run_handle, provider, "during",
                    "no_rate_limit_event", report,
                )
        else:
            result, report = run_codex(
                ledger=ledger, project_id=project_id, route=route,
                assignment=assignment, export=export, control=control,
                schema=result_schema, environ=environ,
                deadline_seconds=deadline_seconds, executable=executable,
            )
            try:
                ledger.observe_provider(
                    project_id, "codex", project_root=export, environ=environ,
                    run_handle=run_handle, phase="after",
                )
                report["observation_after"] = "recorded"
            except ContractError as exc:
                report["observation_after"] = "unavailable"
                _record_unavailable(
                    ledger, project_id, run_handle, provider, "after",
                    observation_failure_reason(exc), report,
                )
        _record_usage(ledger, project_id, run_handle, provider, route["model"], report)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    reviewed = True
    if result is None:
        state = "timed_out" if report["outcome"] == "timed_out" else "failed"
        result = _fenced_result(assignment, state, report["outcome"])
    elif report.get("rerouted"):
        # A reroute means the work was not done by the routed model: its verdict
        # is unknown, and the result stays unreviewed.
        reviewed = False
        result = {
            **result,
            "semantic_verdict": "not-applicable",
            "unverified_claims": [
                *result.get("unverified_claims", []),
                "the provider rerouted this run to another model; verdict unknown",
            ],
        }
    fenced = report["outcome"] != "result"
    report.update(
        bridge_run_id=intent["bridge_run_id"],
        export_file_count=file_count,
        reviewed=reviewed,
        # A fenced terminal state is an authoritative host fact for the
        # observation; a child's own result carries its state in the result.
        terminal_state=result["execution_state"] if fenced else None,
    )
    return {"result": result, "bridge_report": report}


def _record_unavailable(
    ledger: Any,
    project_id: str,
    run_handle: str,
    provider: str,
    phase: str,
    reason_code: str,
    report: dict[str, Any],
) -> None:
    from .learning_store import LedgerError

    try:
        ledger.record_observation_unavailable(
            project_id, run_handle=run_handle, provider=provider, phase=phase,
            reason_code=reason_code,
        )
        report.setdefault("observations_unavailable", []).append(
            {"phase": phase, "reason_code": reason_code}
        )
    except (ContractError, LedgerError):
        report["observation_record_failed"] = True


def _record_usage(
    ledger: Any,
    project_id: str,
    run_handle: str,
    provider: str,
    model: str,
    report: dict[str, Any],
) -> None:
    """Persist the run's client-observed usage, or that it was unavailable."""

    from .learning_store import LedgerError

    usage = report.get("usage")
    if not isinstance(usage, dict):
        _record_unavailable(
            ledger, project_id, run_handle, provider, "after", "usage_unavailable", report
        )
        report["usage_recorded"] = False
        return
    totals = {
        "input_tokens": _token_count(usage.get("input_tokens")),
        "cached_input_tokens": _token_count(
            usage.get("cached_input_tokens", usage.get("cache_read_input_tokens"))
        ),
        "cache_creation_input_tokens": _token_count(usage.get("cache_creation_input_tokens")),
        "output_tokens": _token_count(usage.get("output_tokens")),
        "reasoning_output_tokens": _token_count(usage.get("reasoning_output_tokens")),
    }
    claude = provider == "claude"
    try:
        ledger.record_provider_usage(
            project_id, run_handle=run_handle, provider=provider,
            usage={
                "requested_model": model,
                "models": report.get("model_usage", []) if claude else [],
                "helper_models": sorted(
                    name for name in report.get("helper_models", [])
                    if name in SUPPORTED_MODELS
                ) if claude else [],
                **totals,
            },
        )
        report["usage_recorded"] = True
    except (ContractError, LedgerError):
        report["usage_recorded"] = False


def _claude_role_prompt(bundle_root: Path, route: dict[str, Any]) -> str:
    from .learning_engine import _claude_agent_artifacts

    agent_type = route.get("agent_type", route.get("role"))
    if not isinstance(agent_type, str):
        raise BridgeRefused("route agent is invalid")
    agent_bytes, prompt, _ = _claude_agent_artifacts(bundle_root, route, agent_type)
    expected = route.get("execution_configuration", {}).get("agent_digest")
    if hashlib.sha256(agent_bytes).hexdigest() != expected:
        raise BridgeRefused("installed Claude agent differs from the routed configuration")
    try:
        parse_claude_agent(agent_bytes)
    except CatalogError as exc:
        raise BridgeRefused("installed Claude agent is invalid") from exc
    return prompt
