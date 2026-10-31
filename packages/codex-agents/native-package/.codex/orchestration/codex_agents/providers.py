"""Non-spending provider observations and the fail-closed host-run admission gate.

Observations only ever deny. Account identity is stored as a keyed fingerprint;
raw email, organisation and account identifiers never leave this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError
from .learning_contracts import (
    CLAUDE_AUTH_METHODS,
    CLAUDE_OVERAGE_DISABLED_REASONS,
    CLAUDE_SUBSCRIPTION_TYPES,
    CLAUDE_WINDOW_SCOPES,
    CODEX_PLAN_TYPES,
    CODEX_SUBSCRIPTION_PLAN_TYPES,
    content_digest,
    keyed_digest,
    parse_semver,
    validate_admission_decision,
    validate_billing_attestation,
    validate_billing_grant,
    validate_provider_billing,
    validate_provider_headroom,
    validate_provider_identity,
)

API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
# The app-server also exposes methods that spend or change account state, so
# only these two read-only requests may ever be sent.
APP_SERVER_METHODS = ("initialize", "account/rateLimits/read")
# The protocol handshake notification; it carries no request and changes nothing.
APP_SERVER_NOTIFICATIONS = ("initialized",)
IDENTITY_MAX_AGE_MS = 30 * 60 * 1000
# Billing evidence older than this no longer admits a run, for either provider.
BILLING_MAX_AGE_MS = 30 * 60 * 1000
EVIDENCE_HORIZON_MS = 24 * 60 * 60 * 1000
CODEX_EXHAUSTED_PERCENT = 90
CODEX_CONSTRAINED_PERCENT = 75
# Provider clients are found only here, never through the caller's PATH.
TRUSTED_SEARCH_DIRECTORIES = (
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "~/.local/bin",
)
CLAUDE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10_080}
OBSERVATION_TIMEOUT_SECONDS = 15
MAX_OBSERVATION_BYTES = 262_144
MAX_VERSION_BYTES = 256
# `claude --version` prints "2.1.123 (Claude Code)"; `codex --version` prints
# "codex-cli 0.164.0". Only the plain version number is ever stored.
CLIENT_VERSION_PATTERNS = {
    "claude": re.compile(r"^([0-9.]+)(?: \(Claude Code\))?$"),
    "codex": re.compile(r"^codex-cli ([0-9.]+)$"),
}
FIVE_HOUR_WINDOW_MINUTES = 300
FIVE_HOURS_MS = FIVE_HOUR_WINDOW_MINUTES * 60 * 1000


class AdmissionDenied(ContractError):
    """A host run was denied; `reason_code` is stable and safe to print."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(f"provider admission denied: {reason_code}")
        self.reason_code = reason_code


class ObservationError(ContractError):
    """A provider reading failed; `reason_code` says how, for the ledger."""

    def __init__(self, message: str, reason_code: str = "observation_failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def observation_failure_reason(error: BaseException) -> str:
    """The stable unavailable-reading code for a failed observation."""

    return getattr(error, "reason_code", None) or "observation_failed"


def api_keys_present(environ: Mapping[str, str]) -> bool:
    return any(environ.get(name) for name in API_KEY_VARIABLES)


def _require_no_api_keys(environ: Mapping[str, str]) -> None:
    if api_keys_present(environ):
        raise ContractError("provider observation refuses an API key environment")


def _fingerprint(digest_key: bytes, provider: str, *parts: str) -> str:
    if not parts or not all(isinstance(part, str) and part for part in parts):
        raise ContractError("provider identity is incomplete")
    return keyed_digest(digest_key, "\x00".join((provider, *parts)))


def _record(record_type: str, material: dict[str, Any]) -> dict[str, Any]:
    body = {"schema_version": 1, "record_type": record_type, **material}
    return {**body, "record_id": f"{record_type}:{content_digest(body)}"}


def _allowed(value: Any, allowed: frozenset[str]) -> str | None:
    # Labels are allow-listed, never stored as free text: a misplaced identifier
    # in one of these fields becomes null rather than reaching the ledger.
    return value if isinstance(value, str) and value in allowed else None


def _scope(value: Any) -> str | None:
    return value if value in CLAUDE_WINDOW_SCOPES else None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    # Providers report reset times in epoch seconds; store milliseconds.
    return int(value * 1000) if value < 10**11 else int(value)


def parse_claude_auth_status(
    raw: bytes | str,
    *,
    digest_key: bytes,
    observed_at_ms: int,
    client: tuple[str, str],
    client_version: str | None = None,
) -> dict[str, Any]:
    """Turn `claude auth status --json` output into a fingerprinted identity."""

    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("Claude auth status is not JSON") from exc
    if not isinstance(data, dict):
        raise ContractError("Claude auth status is not an object")
    logged_in = data.get("loggedIn")
    if not isinstance(logged_in, bool):
        raise ContractError("Claude auth status lacks a login state")
    record = _record(
        "provider_identity",
        {
            "provider": "claude",
            "account_fingerprint": _fingerprint(
                digest_key, "claude", data.get("orgId"), data.get("email")
            ),
            "logged_in": logged_in,
            "auth_method": _allowed(data.get("authMethod"), CLAUDE_AUTH_METHODS),
            "subscription_type": _allowed(
                data.get("subscriptionType"), CLAUDE_SUBSCRIPTION_TYPES
            ),
            "plan_type": None,
            "client_path": client[0],
            "client_sha256": client[1],
            "client_version": _version_or_none(client_version),
            "observed_at_ms": observed_at_ms,
        },
    )
    return validate_provider_identity(record)


def _version_or_none(value: Any) -> str | None:
    return value if parse_semver(value) is not None else None


def parse_client_version(provider: str, raw: bytes | str) -> str | None:
    """The plain version a client's `--version` printed, or None if unrecognised."""

    if isinstance(raw, bytes):
        if len(raw) > MAX_VERSION_BYTES:
            return None
        try:
            raw = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or len(raw) > MAX_VERSION_BYTES:
        return None
    pattern = CLIENT_VERSION_PATTERNS.get(provider)
    match = pattern.fullmatch(raw.strip()) if pattern is not None else None
    return _version_or_none(match.group(1)) if match else None


def _home() -> str:
    # The account's home from the password database, not the caller's HOME.
    return pwd.getpwuid(os.getuid()).pw_dir


def trusted_search_path() -> str:
    home = _home()
    return os.pathsep.join(
        home + directory[1:] if directory.startswith("~") else directory
        for directory in TRUSTED_SEARCH_DIRECTORIES
    )


def resolve_client(name: str) -> tuple[str, str]:
    """Find a provider client on the trusted path; return its real path and digest."""

    found = shutil.which(name, path=trusted_search_path())
    if found is None:
        raise ObservationError(
            f"{name} client is unavailable on the trusted path", "client_unavailable"
        )
    real = os.path.realpath(found)
    digest = hashlib.sha256()
    try:
        with open(real, "rb") as handle:
            for chunk in iter(lambda: handle.read(1_048_576), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"{name} client is unreadable") from exc
    return real, digest.hexdigest()


def client_environment(environ: Mapping[str, str], tmpdir: Path) -> dict[str, str]:
    """A minimal child environment: no caller overrides such as CODEX_HOME.

    TMPDIR is a host-private directory the caller created, never the system
    temp directory, which a sandboxed Codex primary can write: a client run
    outside the sandbox must not load or trust files from there.
    """

    account = pwd.getpwuid(os.getuid()).pw_name
    return {
        "HOME": _home(),
        # Claude Code finds its macOS keychain login by account name; without these it
        # reports logged out (verified live), which would only ever fail closed.
        "USER": account,
        "LOGNAME": account,
        "PATH": trusted_search_path(),
        "LANG": environ.get("LANG") or "C.UTF-8",
        "TMPDIR": str(tmpdir),
    }


def _stop_process_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _bounded_output(
    arguments: list[str],
    *,
    environment: dict[str, str],
    timeout: float,
    popen: Callable[..., Any],
) -> bytes:
    """Read a child's stdout while enforcing the size cap and the deadline."""

    try:
        process = popen(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ObservationError(
            "provider observation is unavailable", "client_unavailable"
        ) from exc
    chunks: list[bytes] = []
    received = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ObservationError(
                        "provider observation timed out", "observation_timed_out"
                    )
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_OBSERVATION_BYTES:
                    raise ContractError("provider observation output is too large")
                chunks.append(chunk)
        try:
            returncode = process.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired as exc:
            raise ObservationError(
                "provider observation timed out", "observation_timed_out"
            ) from exc
    except BaseException:
        _stop_process_group(process)
        raise
    finally:
        process.stdout.close()
    if returncode != 0:
        raise ContractError("provider observation failed")
    return b"".join(chunks)


def read_client_version(
    provider: str,
    executable: str,
    *,
    environ: Mapping[str, str],
    tmpdir: Path,
    popen: Callable[..., Any] = subprocess.Popen,
    timeout: float = OBSERVATION_TIMEOUT_SECONDS,
) -> str | None:
    """Run the resolved client's `--version`; None when it fails or is unrecognised.

    A missing version is recorded as such, so admission denies it by reason.
    """

    try:
        output = _bounded_output(
            [executable, "--version"],
            environment=client_environment(environ, tmpdir),
            timeout=timeout,
            popen=popen,
        )
    except ContractError:
        return None
    return parse_client_version(provider, output)


# An option line in clap's help: indented, an optional short form, then `--name`.
HELP_OPTION_PATTERN = re.compile(rb"^[ \t]+(?:-[A-Za-z0-9], )?(--[a-z0-9][a-z0-9-]*)", re.MULTILINE)


def codex_exec_options(
    executable: str,
    *,
    environ: Mapping[str, str],
    tmpdir: Path,
    popen: Callable[..., Any] = subprocess.Popen,
    timeout: float = OBSERVATION_TIMEOUT_SECONDS,
) -> frozenset[str]:
    """The long options the resolved client's `codex exec --help` lists; empty on failure.

    Help makes no model call and loads no configuration.
    """

    try:
        output = _bounded_output(
            [executable, "exec", "--help"],
            environment=client_environment(environ, tmpdir),
            timeout=timeout,
            popen=popen,
        )
    except ContractError:
        return frozenset()
    return frozenset(
        match.decode("ascii") for match in HELP_OPTION_PATTERN.findall(output)
    )


def observe_claude(
    *,
    digest_key: bytes,
    observed_at_ms: int,
    tmpdir: Path,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    timeout: float = OBSERVATION_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    caller = os.environ if environ is None else environ
    _require_no_api_keys(caller)
    executable, client_sha256 = resolve_client("claude")
    output = _bounded_output(
        [executable, "auth", "status", "--json"],
        environment=client_environment(caller, tmpdir),
        timeout=timeout,
        popen=popen,
    )
    version = read_client_version(
        "claude", executable, environ=caller, tmpdir=tmpdir, popen=popen, timeout=timeout
    )
    return [
        parse_claude_auth_status(
            output,
            digest_key=digest_key,
            observed_at_ms=observed_at_ms,
            client=(executable, client_sha256),
            client_version=version,
        )
    ]


class AppServerSession:
    """A stdio app-server session that can send only the allow-listed reads."""

    def __init__(
        self,
        project_root: Path,
        *,
        environ: Mapping[str, str],
        tmpdir: Path,
        popen: Callable[..., Any] = subprocess.Popen,
        timeout: float = OBSERVATION_TIMEOUT_SECONDS,
    ) -> None:
        _require_no_api_keys(environ)
        executable, self.client_sha256 = resolve_client("codex")
        self.client_path = executable
        environment = client_environment(environ, tmpdir)
        self._timeout = timeout
        self._next_id = 0
        self._buffer = b""
        self._received = 0
        try:
            self._process = popen(
                [executable, "app-server", "--listen", "stdio://"],
                cwd=project_root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise ObservationError(
                "Codex app-server is unavailable", "client_unavailable"
            ) from exc

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in APP_SERVER_METHODS:
            raise ContractError(f"app-server method is not allow-listed: {method}")
        identifier = self._next_id
        self._next_id += 1
        message = {"method": method, "id": identifier, "params": params}
        try:
            self._process.stdin.write((json.dumps(message) + "\n").encode())
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ContractError("Codex app-server request failed") from exc
        response = self._response(identifier)
        if "error" in response or not isinstance(response.get("result"), dict):
            raise ContractError(f"Codex app-server {method} failed")
        return response["result"]

    def notify(self, method: str) -> None:
        if method not in APP_SERVER_NOTIFICATIONS:
            raise ContractError(f"app-server notification is not allow-listed: {method}")
        try:
            self._process.stdin.write((json.dumps({"method": method}) + "\n").encode())
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ContractError("Codex app-server notification failed") from exc

    def _response(self, identifier: int) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout
        stdout = self._process.stdout
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError as exc:
                        raise ContractError("Codex app-server output is invalid") from exc
                    # Server notifications are ignored; only our reply matters.
                    if isinstance(message, dict) and message.get("id") == identifier:
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ObservationError(
                        "Codex app-server did not respond in time", "observation_timed_out"
                    )
                chunk = os.read(stdout.fileno(), 65_536)
                if not chunk:
                    raise ContractError("Codex app-server closed early")
                self._received += len(chunk)
                if self._received > MAX_OBSERVATION_BYTES:
                    raise ContractError("Codex app-server output is too large")
                self._buffer += chunk

    def close(self) -> None:
        process = self._process
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, AttributeError):
                pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            process.wait(timeout=3)


def parse_codex_rate_limits(
    result: dict[str, Any],
    *,
    digest_key: bytes,
    observed_at_ms: int,
    client: tuple[str, str],
    client_version: str | None = None,
    run_id: str | None = None,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """Turn an `account/rateLimits/read` result into identity, headroom and billing."""

    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise ContractError("Codex rate limits are unavailable")

    def field(name: str) -> Any:
        return snapshot.get(name, result.get(name))

    fingerprint = _fingerprint(digest_key, "codex", field("accountId"))
    windows = []
    # Windows are identified by duration; primary and secondary are only labels.
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ContractError("Codex rate-limit window is invalid")
        used = window.get("usedPercent")
        minutes = window.get("windowDurationMins")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            raise ContractError("Codex rate-limit window lacks used percent")
        windows.append(
            {
                "window_minutes": (
                    minutes
                    if isinstance(minutes, int) and not isinstance(minutes, bool)
                    and minutes > 0 else None
                ),
                "scope": None,
                "used_percent": min(max(float(used), 0.0), 100.0),
                "resets_at": _timestamp_ms(window.get("resetsAt")),
            }
        )
    windows.sort(key=lambda item: (item["window_minutes"] is None, item["window_minutes"] or 0))
    credits = snapshot.get("credits")
    has_credits = unlimited = None
    if isinstance(credits, dict):
        has_credits = credits.get("hasCredits")
        unlimited = credits.get("unlimited")
    has_credits = has_credits if isinstance(has_credits, bool) else None
    unlimited = unlimited if isinstance(unlimited, bool) else None
    spend_control = snapshot.get("spendControlReached", result.get("spendControlReached"))
    spend_control = spend_control if isinstance(spend_control, bool) else None
    ordinary = field("ordinaryUsageAllowed")
    if has_credits is True or unlimited is True:
        paid_usable: bool | None = True
    elif has_credits is False and unlimited is False:
        paid_usable = False
    else:
        paid_usable = None
    common = {"provider": "codex", "account_fingerprint": fingerprint}
    return [
        validate_provider_identity(_record("provider_identity", {
            **common,
            "logged_in": True,
            "auth_method": None,
            "subscription_type": None,
            "plan_type": _allowed(field("planType"), CODEX_PLAN_TYPES),
            "client_path": client[0],
            "client_sha256": client[1],
            "client_version": _version_or_none(client_version),
            "observed_at_ms": observed_at_ms,
        })),
        validate_provider_headroom(_record("provider_headroom", {
            **common, "windows": windows, "status": None, "run_id": run_id,
            "phase": phase, "observed_at_ms": observed_at_ms,
        })),
        validate_provider_billing(_record("provider_billing", {
            **common,
            "paid_usage_usable": paid_usable,
            "has_credits": has_credits,
            "unlimited": unlimited,
            "spend_control_reached": spend_control,
            "ordinary_usage_allowed": ordinary if isinstance(ordinary, bool) else None,
            "is_using_overage": None,
            "overage_status": None,
            "overage_disabled_reason": None,
            "run_id": run_id,
            "phase": phase,
            "observed_at_ms": observed_at_ms,
        })),
    ]


def observe_codex(
    project_root: Path,
    *,
    digest_key: bytes,
    observed_at_ms: int,
    tmpdir: Path,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    run_id: str | None = None,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    caller = os.environ if environ is None else environ
    session = AppServerSession(project_root, environ=caller, tmpdir=tmpdir, popen=popen)
    try:
        session.request(
            "initialize",
            {"clientInfo": {
                "name": "codex_agents_admission",
                "title": "codex-agents provider observation",
                "version": "0.2.0",
            }},
        )
        session.notify("initialized")
        result = session.request("account/rateLimits/read", {})
    finally:
        session.close()
    # The version comes from the same resolved client the session ran.
    version = read_client_version(
        "codex", session.client_path, environ=caller, tmpdir=tmpdir, popen=popen
    )
    return parse_codex_rate_limits(
        result,
        digest_key=digest_key,
        observed_at_ms=observed_at_ms,
        client=(session.client_path, session.client_sha256),
        client_version=version,
        run_id=run_id,
        phase=phase,
    )


def claude_rate_limit_records(
    rate_limit_info: dict[str, Any],
    *,
    account_fingerprint: str,
    observed_at_ms: int,
    run_id: str | None = None,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """Turn one stream `rate_limit_info` object into headroom and billing records."""

    if not isinstance(rate_limit_info, dict):
        raise ContractError("Claude rate-limit event is invalid")
    status = rate_limit_info.get("status")
    if status not in {"allowed", "allowed_warning", "rejected"}:
        raise ContractError("Claude rate-limit status is invalid")
    windows: list[dict[str, Any]] = []
    unified = rate_limit_info.get("unifiedWindows")
    entries = (
        [(scope, value) for scope, value in sorted(unified.items())]
        if isinstance(unified, dict)
        else [(rate_limit_info.get("rateLimitType"), rate_limit_info)]
    )
    for scope, entry in entries:
        if not isinstance(entry, dict):
            raise ContractError("Claude rate-limit window is invalid")
        utilization = entry.get("utilization")
        if utilization is not None and (
            isinstance(utilization, bool)
            or not isinstance(utilization, (int, float))
            or not 0 <= utilization <= 1
        ):
            raise ContractError("Claude utilization must be a fraction")
        if entry.get("status") == "rejected":
            status = "rejected"
        label = _scope(scope)
        windows.append({
            "window_minutes": CLAUDE_WINDOW_MINUTES.get(label or ""),
            "scope": label,
            "used_percent": None if utilization is None else float(utilization) * 100,
            "resets_at": _timestamp_ms(entry.get("resetsAt")),
        })
    using = rate_limit_info.get("isUsingOverage")
    using = using if isinstance(using, bool) else None
    overage = rate_limit_info.get("overageStatus")
    overage = overage if overage in {"allowed", "allowed_warning", "rejected"} else None
    reason = _allowed(
        rate_limit_info.get("overageDisabledReason"), CLAUDE_OVERAGE_DISABLED_REASONS
    )
    # Overage counts as unusable only on exact evidence: not in use, rejected,
    # and an allow-listed reason. Anything else is usable or unknown, and both
    # deny admission.
    if using is True or overage in {"allowed", "allowed_warning"}:
        paid_usable: bool | None = True
    elif using is False and overage == "rejected" and reason is not None:
        paid_usable = False
    else:
        paid_usable = None
    common = {"provider": "claude", "account_fingerprint": account_fingerprint}
    if run_id is not None and phase is None:
        phase = "during"
    return [
        validate_provider_headroom(_record("provider_headroom", {
            **common, "windows": windows, "status": status, "run_id": run_id,
            "phase": phase, "observed_at_ms": observed_at_ms,
        })),
        validate_provider_billing(_record("provider_billing", {
            **common,
            "paid_usage_usable": paid_usable,
            "has_credits": None,
            "spend_control_reached": None,
            "unlimited": None,
            "ordinary_usage_allowed": None,
            "is_using_overage": using,
            "overage_status": overage,
            "overage_disabled_reason": reason,
            "run_id": run_id,
            "phase": phase,
            "observed_at_ms": observed_at_ms,
        })),
    ]


def headroom_record(
    *,
    provider: str,
    account_fingerprint: str,
    windows: list[dict[str, Any]],
    status: str | None,
    run_id: str | None,
    phase: str | None,
    observed_at_ms: int,
) -> dict[str, Any]:
    """A validated headroom record built by the ledger from its own records."""

    return validate_provider_headroom(_record("provider_headroom", {
        "provider": provider, "account_fingerprint": account_fingerprint,
        "windows": windows, "status": status, "run_id": run_id, "phase": phase,
        "observed_at_ms": observed_at_ms,
    }))


def record_claude_rate_limit_event(
    ledger: Any,
    project_id: str,
    *,
    run_handle: str,
    rate_limit_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """Record a Claude rate-limit event against the run's admitted identity.

    The trusted bridge calls this for each event in a run. The ledger keeps an
    event only where it is more restrictive than what the run already recorded,
    derives the fingerprint from the run's stored admission and stamps its own
    time.
    """

    return ledger.record_claude_rate_limit_event(
        project_id, run_handle=run_handle, rate_limit_info=rate_limit_info
    )


def billing_attestation_record(grant: Any) -> dict[str, Any]:
    checked = validate_billing_grant(grant)
    material = {
        field: checked[field] for field in sorted(checked) if field != "version"
    }
    return validate_billing_attestation(
        _record(
            "billing_attestation",
            {**material, "observed_at_ms": checked["issued_at"]},
        )
    )


def _latest(records: list[tuple[dict[str, Any], str]]) -> tuple[dict[str, Any], str] | None:
    # Records arrive in ledger order, so a later record wins an observation-time tie.
    ranked = max(
        enumerate(records),
        key=lambda item: (item[1][0]["observed_at_ms"], item[0]),
        default=None,
    )
    return None if ranked is None else ranked[1]


def _window_live(window: dict[str, Any], observed_at_ms: int, now_ms: int) -> bool:
    # A window counts until it resets; one with no reset time counts for a day.
    if window["resets_at"] is not None:
        return window["resets_at"] > now_ms
    return 0 <= now_ms - observed_at_ms <= EVIDENCE_HORIZON_MS


def _headroom_state(provider: str, record: dict[str, Any], now_ms: int) -> str | None:
    """Rate one headroom record: exhausted, constrained or ample; None once reset."""

    windows = [
        window for window in record["windows"]
        if _window_live(window, record["observed_at_ms"], now_ms)
    ]
    if not windows:
        return None
    used = [window["used_percent"] for window in windows if window["used_percent"] is not None]
    if provider == "codex":
        if any(value >= CODEX_EXHAUSTED_PERCENT for value in used):
            return "exhausted"
        if any(value >= CODEX_CONSTRAINED_PERCENT for value in used):
            return "constrained"
        return "ample"
    # Under a Claude primary, Claude children spend the primary's own allowance,
    # so Claude windows use the same thresholds as Codex.
    if record["status"] == "rejected" or any(
        value >= CODEX_EXHAUSTED_PERCENT for value in used
    ):
        return "exhausted"
    if record["status"] == "allowed_warning" or any(
        value >= CODEX_CONSTRAINED_PERCENT for value in used
    ):
        return "constrained"
    return "ample"


def _admission_limits(settings: Any) -> tuple[tuple[int, int, int], tuple[int, int, int], int | None] | None:
    """The reviewed version range and run cap, or None when the settings are unusable."""

    if not isinstance(settings, dict):
        return None
    versions = settings.get("client_versions")
    cap = settings.get("max_admissions_per_window", False)
    if not isinstance(versions, dict):
        return None
    minimum = parse_semver(versions.get("minimum"))
    maximum = parse_semver(versions.get("maximum_exclusive"))
    if minimum is None or maximum is None or not minimum < maximum:
        return None
    if cap is not None and (
        not isinstance(cap, int) or isinstance(cap, bool) or cap < 1
    ):
        return None
    return minimum, maximum, cap


def window_start_ms(
    provider: str,
    fingerprint: str,
    headroom: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> tuple[int, str | None]:
    """Where the current five-hour usage window began, and the record that says so.

    A recorded five-hour reset still ahead, and no more than one window away,
    places a window that began five hours before it. The earliest such start
    among every observation still live is used, so a later reading can never
    move the start forward and free places while an earlier window has not
    reset. With no such reset known, the window is the last five hours.
    """

    starts = [
        (window["resets_at"] - FIVE_HOURS_MS, digest)
        for record, digest in headroom
        if record["provider"] == provider
        and record["account_fingerprint"] == fingerprint
        and record["observed_at_ms"] <= now_ms
        for window in record["windows"]
        if window["window_minutes"] == FIVE_HOUR_WINDOW_MINUTES
        and window["resets_at"] is not None
        and now_ms < window["resets_at"] <= now_ms + FIVE_HOURS_MS
    ]
    if starts:
        return min(starts, key=lambda item: item[0])
    return now_ms - FIVE_HOURS_MS + 1, None


def evaluate_admission(
    *,
    provider: str,
    identities: list[tuple[dict[str, Any], str]],
    attestations: list[tuple[dict[str, Any], str]],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    prior_claude_admissions: set[str],
    environ: Mapping[str, str],
    now_ms: int,
    admission_policy: Any = None,
    prior_run_admissions: list[tuple[str, int]] | None = None,
    window_headroom: list[tuple[dict[str, Any], str]] | None = None,
    cited_digests: set[str] | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Apply the admission checks in their fixed order; first failure wins.

    Each input is a list of (record, content digest) pairs for one project.
    Restrictive evidence wins: a later favourable record never clears an earlier
    restriction that still applies. `prior_claude_admissions` holds the account
    fingerprints that already have an admitted Claude run.
    `admission_policy` is the provider's committed admission settings: its
    reviewed client range and its run cap. `prior_run_admissions` lists the
    (account fingerprint, decision time) of earlier passing run admissions for
    this provider; billing probes are not runs and are not in it.
    `window_headroom` is the account's headroom from every project, which
    places the cap window; both cap inputs are ledger-wide.
    Billing, headroom and prior admissions are the account's, from every
    project; `cited_digests` lists the records this project holds, and only
    those are cited as evidence. Attestations stay per project.
    `probe` evaluates a Claude billing probe, whose purpose is to refresh
    stale billing, so it does not need fresh billing itself.
    The result is an unsaved decision; the ledger binds it to a run.
    """

    def decision(
        reason: str,
        fingerprint: str | None = None,
        evidence: list[str] | None = None,
        basis: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "decision": "pass" if reason == "admitted" else "deny",
            "reason_code": reason,
            "admission_basis": basis,
            "account_fingerprint": fingerprint,
            "evidence_digests": sorted(
                set(evidence or [])
                if cited_digests is None else set(evidence or []) & cited_digests
            ),
            "observed_at_ms": now_ms,
        }

    if api_keys_present(environ):
        return decision("provider_api_key_present")
    limits = _admission_limits(admission_policy)
    if limits is None or prior_run_admissions is None or window_headroom is None:
        return decision("admission_policy_unavailable")
    minimum_version, maximum_version, run_cap = limits
    current = [
        item for item in identities
        if item[0]["provider"] == provider
        and 0 <= now_ms - item[0]["observed_at_ms"] <= IDENTITY_MAX_AGE_MS
    ]
    identity_item = _latest(current)
    if identity_item is None:
        return decision("provider_identity_missing")
    identity, identity_digest = identity_item
    fingerprint = identity["account_fingerprint"]
    evidence = [identity_digest]
    version = parse_semver(identity.get("client_version"))
    if version is None or not minimum_version <= version < maximum_version:
        return decision("client_version_unreviewed", fingerprint, evidence)
    attestation = _latest([
        item for item in attestations
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ])
    if attestation is None:
        return decision("billing_attestation_missing", fingerprint, evidence)
    evidence.append(attestation[1])
    if provider == "claude" and not (
        identity["logged_in"]
        and identity["auth_method"] == "claude.ai"
        and identity["subscription_type"] in CLAUDE_SUBSCRIPTION_TYPES
    ):
        return decision("claude_subscription_missing", fingerprint, evidence)
    if provider == "codex" and identity["plan_type"] not in CODEX_SUBSCRIPTION_PLAN_TYPES:
        return decision("codex_plan_not_subscription", fingerprint, evidence)
    spent = spend_control_digest(provider, fingerprint, billing, now_ms)
    if spent is not None:
        return decision("codex_spend_control_reached", fingerprint, [*evidence, spent])
    paid = paid_usage_digest(provider, fingerprint, billing, now_ms, attestation[0])
    if paid is not None:
        # Any recent sign of paid usage denies; a later favourable record
        # cannot clear it.
        return decision("paid_usage_usable", fingerprint, [*evidence, paid])
    latest_billing = _latest([
        item for item in billing
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
        and item[0]["observed_at_ms"] <= now_ms
    ])
    fresh = (
        latest_billing is not None
        and now_ms - latest_billing[0]["observed_at_ms"] <= BILLING_MAX_AGE_MS
    )
    basis = "evidence"
    if provider == "codex":
        # Codex billing comes from the same non-spending read, so it is required.
        if not fresh:
            return decision("billing_unverified", fingerprint, evidence)
    elif probe and latest_billing is not None:
        # A probe refreshes billing, so older evidence is enough to admit it;
        # that evidence must still show paid usage off.
        fresh = True
    elif not fresh:
        # Claude has no non-spending billing source. One bootstrap run per
        # account may open on the attestation and identity; its own rate-limit
        # event, or a probe, must record fresh billing before another Claude
        # run is admitted.
        if fingerprint in prior_claude_admissions:
            return decision("billing_unverified", fingerprint, evidence)
        basis = "bootstrap"
    if fresh:
        evidence.append(latest_billing[1])
        if latest_billing[0]["paid_usage_usable"] is not False and not credits_waived(
            provider, latest_billing[0], attestation[0]
        ):
            return decision("billing_unverified", fingerprint, evidence)
    state, state_digest = headroom_state(provider, fingerprint, headroom, billing, now_ms)
    if state_digest is not None:
        evidence.append(state_digest)
    if state == "exhausted":
        return decision("provider_exhausted", fingerprint, evidence)
    if run_cap is not None:
        # A backstop on run count per usage window, whatever headroom says.
        # A reading from another project still places the window; the
        # decision cites only this project's records.
        start, window_digest = window_start_ms(
            provider, fingerprint, window_headroom, now_ms
        )
        # An admission stamped after now still counts, so a clock that steps
        # back never frees places under the cap.
        counted = sum(
            1 for admitted, at in prior_run_admissions
            if admitted == fingerprint and start <= at
        )
        if counted >= run_cap:
            return decision(
                "window_run_cap_reached", fingerprint,
                [*evidence, *([window_digest] if window_digest else [])],
            )
    return decision("admitted", fingerprint, evidence, basis)


def credits_waived(provider: str, record: dict[str, Any], attestation: Any) -> bool:
    """Whether the owner's attestation turns a Codex credits reading into unusable.

    Only when the current attestation says credit spending is disabled, the
    reading shows credits that are not unlimited, and spend control has not been
    reached. Any other reading keeps its observed billing meaning.
    """

    return (
        provider == "codex"
        and attestation is not None
        and attestation.get("codex_credits_spending_disabled") is True
        and record.get("has_credits") is True
        and record.get("unlimited") is False
        and record.get("spend_control_reached") is not True
    )


def spend_control_digest(
    provider: str,
    fingerprint: str,
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> str | None:
    """The digest of a Codex billing record from the last day reporting spend control reached."""

    if provider != "codex":
        return None
    for record, digest in billing:
        if (
            record["provider"] == provider
            and record["account_fingerprint"] == fingerprint
            and now_ms - record["observed_at_ms"] <= EVIDENCE_HORIZON_MS
            and record.get("spend_control_reached") is True
        ):
            return digest
    return None


def paid_usage_digest(
    provider: str,
    fingerprint: str,
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
    attestation: Any = None,
) -> str | None:
    """The digest of a billing record from the last day showing paid usage, if any.

    A record stamped after now also counts, so a clock that steps back never
    hides paid usage.
    """

    for record, digest in billing:
        if (
            record["provider"] == provider
            and record["account_fingerprint"] == fingerprint
            and now_ms - record["observed_at_ms"] <= EVIDENCE_HORIZON_MS
            and (
                (
                    record["paid_usage_usable"] is True
                    and not credits_waived(provider, record, attestation)
                )
                or record["is_using_overage"] is True
            )
        ):
            return digest
    return None


def headroom_state(
    provider: str,
    fingerprint: str,
    headroom: list[tuple[dict[str, Any], str]],
    billing: list[tuple[dict[str, Any], str]],
    now_ms: int,
) -> tuple[str, str | None]:
    """The most restrictive unreset headroom, and the latest record at that level.

    Only the latest record at the deciding level is named: a decision's digest
    list is bounded, and runs keep adding headroom records. For Codex, the
    latest billing reading disallowing ordinary usage also exhausts it, and so
    does any such reading stamped after now, so a clock step back never hides it.
    """

    rank = {"ample": 0, "constrained": 1, "exhausted": 2}
    state = "ample"
    state_digest: str | None = None
    for record, digest in headroom:
        # A record stamped after now still restricts, so a clock step back
        # never hides an exhausted window.
        if record["provider"] != provider or record["account_fingerprint"] != fingerprint:
            continue
        rated = _headroom_state(provider, record, now_ms)
        if rated is None:
            continue
        if rank[rated] >= rank[state]:
            state, state_digest = rated, digest
    if provider == "codex":
        own = [
            item for item in billing
            if item[0]["provider"] == provider and item[0]["account_fingerprint"] == fingerprint
        ]
        latest = _latest([item for item in own if item[0]["observed_at_ms"] <= now_ms])
        if (latest is not None and latest[0]["ordinary_usage_allowed"] is False) or any(
            record["observed_at_ms"] > now_ms and record["ordinary_usage_allowed"] is False
            for record, _ in own
        ):
            state = "exhausted"
    return state, state_digest


def evaluate_launch(
    *,
    provider: str,
    identity: dict[str, Any],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    now_ms: int,
    bootstrap: bool = False,
    attestations: list[tuple[dict[str, Any], str]] | None = None,
) -> str | None:
    """Re-check an admitted run's eligibility just before its child launches.

    The account and client were already compared with the admitted ones. This
    re-reads the account-wide billing and headroom, including the fresh
    before-launch reading, and the plan the fresh identity reports. The run
    cap is not counted again. Billing must be from the last 30 minutes, for
    either provider; only a run admitted as the Claude bootstrap, on an account
    with no billing at all yet, launches without it. Returns a denial reason,
    or None to launch.
    """

    fingerprint = identity["account_fingerprint"]
    if provider == "codex" and identity["plan_type"] not in CODEX_SUBSCRIPTION_PLAN_TYPES:
        return "codex_plan_not_subscription"
    if provider == "claude" and not (
        identity["logged_in"]
        and identity["auth_method"] == "claude.ai"
        and identity["subscription_type"] in CLAUDE_SUBSCRIPTION_TYPES
    ):
        return "claude_subscription_missing"
    attestation = _latest([
        item for item in attestations or []
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ])
    current = None if attestation is None else attestation[0]
    if spend_control_digest(provider, fingerprint, billing, now_ms) is not None:
        return "codex_spend_control_reached"
    if paid_usage_digest(provider, fingerprint, billing, now_ms, current) is not None:
        return "paid_usage_usable"
    latest = _latest([
        item for item in billing
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
        and item[0]["observed_at_ms"] <= now_ms
    ])
    if latest is None:
        if not (provider == "claude" and bootstrap):
            return "billing_unverified"
    elif now_ms - latest[0]["observed_at_ms"] > BILLING_MAX_AGE_MS:
        # A dispatch long after its open must refresh billing first (for
        # Claude, with a billing probe).
        return "billing_unverified"
    elif latest[0]["paid_usage_usable"] is not False and not credits_waived(
        provider, latest[0], current
    ):
        return "billing_unverified"
    state, _ = headroom_state(provider, fingerprint, headroom, billing, now_ms)
    if state == "exhausted":
        return "provider_exhausted"
    return None


def admission_record(decision: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    return validate_admission_decision(
        _record("admission_decision", {**decision, "run_id": run_id})
    )
