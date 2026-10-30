"""Bridge one admitted host run to a read-only child of the other provider.

The bridge never runs a child in the repository: it exports the committed bytes
of the assignment's read paths into a private directory, launches the child
there once, and fences any outcome it cannot prove. It records only digests,
counts and typed codes; prompts and file contents never reach the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import CatalogError, parse_claude_agent
from .contracts import ContractError, result_preflight, validate_assignment
from .providers import api_keys_present, client_environment, resolve_client

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


class BridgeRefused(ContractError):
    """A precondition failed before any child was launched."""


class _Abort(Exception):
    """Stop the child: a frame broke the launch contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def assignment_read_paths(assignment: dict[str, Any]) -> list[str]:
    """The repository paths a child may read: context plus owned paths."""

    paths: set[str] = set()
    for value in (*assignment.get("context", []), *assignment.get("owned_paths", [])):
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise BridgeRefused("assignment read path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in path.parts):
            raise BridgeRefused("assignment read path must stay inside the repository")
        paths.add(path.as_posix())
    if not paths:
        raise BridgeRefused("assignment names no read paths to export")
    return sorted(paths)


def _git(root: Path, *arguments: str, text: bool = False) -> subprocess.CompletedProcess:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise BridgeRefused("git is unavailable")
    try:
        return subprocess.run(
            [executable, "-C", str(root), *arguments],
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
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

    Only regular blobs are exported. A symlink, submodule, missing path or an
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
    deadline = time.monotonic() + deadline_seconds
    buffer = b""
    received = 0
    try:
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
                    if not line.strip():
                        continue
                    _deliver(line, on_frame)
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
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    model = route["model"]
    executable, _ = resolve_client("claude")
    settings = _private_file(
        control / "settings.json", json.dumps(claude_settings(model)).encode()
    )
    system_prompt = _private_file(control / "role-prompt.md", role_prompt.encode())
    max_turns = max(1, int(route["bounds"]["max_tool_calls"]))
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
        "init": None, "result": None, "rate_limit_recorded": False, "frames": 0,
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
        if kind == "rate_limit_event" and not state["rate_limit_recorded"]:
            state["rate_limit_recorded"] = True
            try:
                ledger.record_claude_rate_limit_event(
                    project_id,
                    run_handle=run_handle,
                    rate_limit_info=frame.get("rate_limit_info"),
                )
            except ContractError as exc:
                raise _Abort("rate_limit_event_invalid") from exc
        elif kind == "result":
            state["result"] = frame

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
        "rate_limit_event_recorded": state["rate_limit_recorded"],
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
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    model = route["model"]
    executable, _ = resolve_client("codex")
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
        if kind in CODEX_REROUTE_EVENTS:
            state["rerouted"] = True
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
    deadline_seconds = int(route["bounds"]["timeout_seconds"])
    state_root = Path(ledger.state.root)
    bridge_root = state_root / "bridge"
    bridge_root.mkdir(mode=0o700, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="dispatch-", dir=bridge_root))
    try:
        os.chmod(workspace, 0o700)
        export = workspace / "export"
        control = workspace / "control"
        export.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        file_count = export_committed_paths(project_root, paths, export)
        # Re-check the account immediately before launch: it must still be the
        # admitted one, and the environment must still be key-free.
        identity = ledger.observe_provider(
            project_id, provider, project_root=export, environ=environ
        )[0]
        if identity["account_fingerprint"] != admission["account_fingerprint"]:
            raise BridgeRefused("provider account differs from the admitted account")
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
                deadline_seconds=deadline_seconds,
            )
        else:
            result, report = run_codex(
                ledger=ledger, project_id=project_id, route=route,
                assignment=assignment, export=export, control=control,
                schema=result_schema, environ=environ,
                deadline_seconds=deadline_seconds,
            )
            try:
                ledger.observe_provider(
                    project_id, "codex", project_root=export, environ=environ
                )
                report["observation_after"] = "recorded"
            except ContractError:
                report["observation_after"] = "unavailable"
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
