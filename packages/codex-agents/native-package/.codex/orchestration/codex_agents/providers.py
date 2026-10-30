"""Non-spending provider observations and the fail-closed host-run admission gate.

Observations only ever deny. Account identity is stored as a keyed fingerprint;
raw email, organisation and account identifiers never leave this module.
"""

from __future__ import annotations

import json
import os
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
    content_digest,
    keyed_digest,
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
CODEX_EXHAUSTED_PERCENT = 90
CLAUDE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10_080}
OBSERVATION_TIMEOUT_SECONDS = 15
MAX_OBSERVATION_BYTES = 262_144


class AdmissionDenied(ContractError):
    """A host run was denied; `reason_code` is stable and safe to print."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(f"provider admission denied: {reason_code}")
        self.reason_code = reason_code


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


def _label(value: Any) -> str | None:
    # Unknown or malformed labels are dropped rather than stored as free text.
    if isinstance(value, str) and 0 < len(value) <= 64 and value.replace(
        "_", ""
    ).replace("-", "").replace(".", "").isalnum():
        return value
    return None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    # Providers report reset times in epoch seconds; store milliseconds.
    return int(value * 1000) if value < 10**11 else int(value)


def parse_claude_auth_status(
    raw: bytes | str, *, digest_key: bytes, observed_at_ms: int
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
            "auth_method": _label(data.get("authMethod")),
            "subscription_type": _label(data.get("subscriptionType")),
            "plan_type": None,
            "observed_at_ms": observed_at_ms,
        },
    )
    return validate_provider_identity(record)


def observe_claude(
    *,
    digest_key: bytes,
    observed_at_ms: int,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict[str, Any]]:
    environment = dict(os.environ if environ is None else environ)
    _require_no_api_keys(environment)
    executable = shutil.which("claude", path=environment.get("PATH"))
    if executable is None:
        raise ContractError("Claude client is unavailable")
    try:
        completed = runner(
            [executable, "auth", "status", "--json"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=OBSERVATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("Claude auth status is unavailable") from exc
    output = completed.stdout
    if (
        completed.returncode != 0
        or not isinstance(output, bytes)
        or len(output) > MAX_OBSERVATION_BYTES
    ):
        raise ContractError("Claude auth status is unavailable")
    return [
        parse_claude_auth_status(
            output, digest_key=digest_key, observed_at_ms=observed_at_ms
        )
    ]


class AppServerSession:
    """A stdio app-server session that can send only the allow-listed reads."""

    def __init__(
        self,
        project_root: Path,
        *,
        environ: Mapping[str, str],
        popen: Callable[..., Any] = subprocess.Popen,
        timeout: float = OBSERVATION_TIMEOUT_SECONDS,
    ) -> None:
        environment = dict(environ)
        _require_no_api_keys(environment)
        executable = shutil.which("codex", path=environment.get("PATH"))
        if executable is None:
            raise ContractError("Codex client is unavailable")
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
            raise ContractError("Codex app-server is unavailable") from exc

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
                    raise ContractError("Codex app-server did not respond in time")
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
    result: dict[str, Any], *, digest_key: bytes, observed_at_ms: int
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
            "plan_type": _label(field("planType")),
            "observed_at_ms": observed_at_ms,
        })),
        validate_provider_headroom(_record("provider_headroom", {
            **common, "windows": windows, "status": None,
            "observed_at_ms": observed_at_ms,
        })),
        validate_provider_billing(_record("provider_billing", {
            **common,
            "paid_usage_usable": paid_usable,
            "has_credits": has_credits,
            "unlimited": unlimited,
            "ordinary_usage_allowed": ordinary if isinstance(ordinary, bool) else None,
            "is_using_overage": None,
            "overage_status": None,
            "observed_at_ms": observed_at_ms,
        })),
    ]


def observe_codex(
    project_root: Path,
    *,
    digest_key: bytes,
    observed_at_ms: int,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> list[dict[str, Any]]:
    session = AppServerSession(
        project_root,
        environ=os.environ if environ is None else environ,
        popen=popen,
    )
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
    return parse_codex_rate_limits(
        result, digest_key=digest_key, observed_at_ms=observed_at_ms
    )


def claude_rate_limit_records(
    rate_limit_info: dict[str, Any], *, account_fingerprint: str, observed_at_ms: int
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
        label = _label(scope)
        windows.append({
            "window_minutes": CLAUDE_WINDOW_MINUTES.get(label or ""),
            "scope": label,
            "used_percent": None if utilization is None else float(utilization) * 100,
            "resets_at": _timestamp_ms(entry.get("resetsAt")),
        })
    using = rate_limit_info.get("isUsingOverage")
    using = using if isinstance(using, bool) else None
    overage = rate_limit_info.get("overageStatus")
    if overage not in {"allowed", "allowed_warning", "rejected"}:
        overage = "disabled" if rate_limit_info.get("overageDisabledReason") else None
    # Overage is unusable only when it is off and not in use; anything else
    # is either usable or unknown, and both deny admission.
    if using is True or overage in {"allowed", "allowed_warning"}:
        paid_usable: bool | None = True
    elif using is False and overage in {"rejected", "disabled"}:
        paid_usable = False
    else:
        paid_usable = None
    common = {"provider": "claude", "account_fingerprint": account_fingerprint}
    return [
        validate_provider_headroom(_record("provider_headroom", {
            **common, "windows": windows, "status": status,
            "observed_at_ms": observed_at_ms,
        })),
        validate_provider_billing(_record("provider_billing", {
            **common,
            "paid_usage_usable": paid_usable,
            "has_credits": None,
            "unlimited": None,
            "ordinary_usage_allowed": None,
            "is_using_overage": using,
            "overage_status": overage,
            "observed_at_ms": observed_at_ms,
        })),
    ]


def record_claude_rate_limit_event(
    ledger: Any,
    project_id: str,
    *,
    run_handle: str,
    rate_limit_info: dict[str, Any],
    observed_at_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Record a run's Claude rate-limit event against its admitted identity."""

    decision = ledger.host_run_admission(project_id, run_handle)
    if decision["provider"] != "claude":
        raise ContractError("rate-limit event does not belong to a Claude run")
    timestamp = observed_at_ms if observed_at_ms is not None else time.time_ns() // 1_000_000
    records = claude_rate_limit_records(
        rate_limit_info,
        account_fingerprint=decision["account_fingerprint"],
        observed_at_ms=timestamp,
    )
    ledger.record_provider_observations(project_id, records, now_ms=timestamp)
    return records


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


def evaluate_admission(
    *,
    provider: str,
    identities: list[tuple[dict[str, Any], str]],
    attestations: list[tuple[dict[str, Any], str]],
    billing: list[tuple[dict[str, Any], str]],
    headroom: list[tuple[dict[str, Any], str]],
    environ: Mapping[str, str],
    now_ms: int,
) -> dict[str, Any]:
    """Apply the admission checks in their fixed order; first failure wins.

    Each input is a list of (record, content digest) pairs for one project. The
    result is an unsaved decision; the caller binds it to a run.
    """

    def decision(
        reason: str, fingerprint: str | None = None, evidence: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "decision": "pass" if reason == "admitted" else "deny",
            "reason_code": reason,
            "account_fingerprint": fingerprint,
            "evidence_digests": sorted(set(evidence or [])),
            "observed_at_ms": now_ms,
        }

    if api_keys_present(environ):
        return decision("provider_api_key_present")
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
        and identity["subscription_type"] is not None
    ):
        return decision("claude_subscription_missing", fingerprint, evidence)
    own = [
        item for item in billing
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ]
    latest_billing = _latest(own)
    # Bootstrap: with no billing observation yet, the attestation and identity
    # admit the run; that run's own observations then gate the next one.
    if latest_billing is not None:
        evidence.append(latest_billing[1])
        usable = latest_billing[0]["paid_usage_usable"]
        if usable is True:
            return decision("paid_usage_usable", fingerprint, evidence)
        if usable is None:
            return decision("billing_unverified", fingerprint, evidence)
    exhausted = False
    own_headroom = [
        item for item in headroom
        if item[0]["provider"] == provider
        and item[0]["account_fingerprint"] == fingerprint
    ]
    if provider == "codex":
        latest = _latest(own_headroom)
        if latest is not None:
            evidence.append(latest[1])
            exhausted = any(
                window["used_percent"] is not None
                and window["used_percent"] >= CODEX_EXHAUSTED_PERCENT
                for window in latest[0]["windows"]
            )
        if (
            latest_billing is not None
            and latest_billing[0]["ordinary_usage_allowed"] is False
        ):
            exhausted = True
    else:
        # Claude reports one window per event, so keep the latest per window.
        by_scope: dict[Any, tuple[dict[str, Any], str]] = {}
        for item in sorted(own_headroom, key=lambda entry: entry[0]["observed_at_ms"]):
            scopes = {window["scope"] for window in item[0]["windows"]} or {None}
            for scope in scopes:
                by_scope[scope] = item
        for record, digest in by_scope.values():
            evidence.append(digest)
            exhausted = exhausted or record["status"] == "rejected"
    if exhausted:
        return decision("provider_exhausted", fingerprint, evidence)
    return decision("admitted", fingerprint, evidence)


def admission_record(decision: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    return validate_admission_decision(
        _record("admission_decision", {**decision, "run_id": run_id})
    )
