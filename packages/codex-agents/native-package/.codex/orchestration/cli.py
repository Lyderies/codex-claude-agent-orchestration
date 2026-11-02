#!/usr/bin/env python3
"""Command-line entry point for deterministic native orchestration support."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
# The project this CLI serves. A protected runtime copy (the one the owner's Codex
# rule runs) replaces it below with the project its binding names.
BUNDLE_ROOT = ORCHESTRATION_ROOT.parent.parent
# Present only in an installed project or its protected copy; the installer
# writes it next to this file.
INSTALL_MANIFEST = ORCHESTRATION_ROOT / "install-manifest.json"
# Present only in a protected runtime copy.
RUNTIME_BINDING = ORCHESTRATION_ROOT / "runtime-binding.json"


def _copy_holds_only_its_runtime() -> bool:
    """Whether a protected copy's tree is exactly the files its manifest lists.

    It runs before this directory goes on the import path, with only modules
    already loaded, so an extra module (a top-level `socket.py`, a package,
    bytecode, an extension module) is refused before anything could import it.
    """

    try:
        with open(INSTALL_MANIFEST, "rb") as handle:
            listed = json.loads(handle.read(1_048_577))["files"]
        expected = {
            name.removeprefix(".codex/orchestration/") for name in listed
        } | {INSTALL_MANIFEST.name, RUNTIME_BINDING.name}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    directories = {
        "/".join(item.split("/")[:index])
        for item in expected for index in range(1, item.count("/") + 1)
    }
    for current, subdirectories, names in os.walk(ORCHESTRATION_ROOT, followlinks=False):
        base = os.path.relpath(current, ORCHESTRATION_ROOT)
        for name in [*subdirectories, *names]:
            relative = name if base == "." else f"{base}/{name}"
            path = os.path.join(current, name)
            if os.path.islink(path) or relative not in (
                directories if name in subdirectories else expected
            ):
                return False
    return True


if os.path.lexists(RUNTIME_BINDING) and not _copy_holds_only_its_runtime():
    print(json.dumps({
        "ready": False,
        "error": "the protected runtime copy holds files it should not; ask the owner "
        "to run ./setup.sh (Codex commands step)",
    }, indent=2, sort_keys=True))
    raise SystemExit(2)
# Appended, never first: run isolated (-I), the script's folder is not on the path, so a
# module planted beside this file (a `socket.py`) can never shadow the standard library,
# in the project's copy (which the hooks' collector runs) as in the protected one.
sys.path.append(str(ORCHESTRATION_ROOT))

from codex_agents.bridge import BridgeRefused  # noqa: E402
from codex_agents.bridge import signal_guard as bridge_signal_guard  # noqa: E402
from codex_agents.bridge import dispatch as dispatch_bridge  # noqa: E402
from codex_agents.catalog import CatalogError, discover, load_discovery_policy  # noqa: E402
from codex_agents.contracts import (  # noqa: E402
    ContractError,
    ResultContractError,
    model_selection_slice,
    result_preflight,
    routing_task_slice,
    validate_assignment,
    validate_assignment_shape,
    validate_result,
)
from codex_agents.evaluation import evaluate, load_jsonl  # noqa: E402
from codex_agents.learning_adapters import (  # noqa: E402
    load_client_capabilities,
    normalize_app_server,
)
from codex_agents.learning_contracts import (  # noqa: E402
    canonical_json,
    content_digest,
    keyed_digest,
    load_released_candidate_digests,
    parse_bounded_json,
    parse_bounded_json_value,
    validate_learning_record,
)
from codex_agents.learning_engine import (  # noqa: E402
    CandidateLifecycle,
    OwnerGrantAuthority,
    analyze_outcomes,
    apply_candidate_to_route,
    build_feedback,
    build_evaluation_trial,
    build_canary_report,
    calibrate_grader,
    compare_candidate,
    create_evaluation_assignment,
    generate_candidate,
    installed_execution_configuration,
    mine_evaluation_case,
    review_evaluation_case,
    run_operational_check_suite,
    runtime_fixture_digest,
    validate_runtime_candidate,
)
from codex_agents.learning_store import (  # noqa: E402
    CollectorError,
    CollectorClient,
    CollectorMaintenance,
    CollectorRestartError,
    CollectorServer,
    CollectorVersionError,
    LearningLedger,
    LearningState,
    LedgerError,
    StateError,
    authorize_collector_runtime,
    client_health_clear,
    client_health_snapshot,
    default_state_root,
    empty_ingestion_snapshot,
    ensure_collector,
    migrate_legacy_state,
    registered_project_roots,
    require_authorized_collector_runtime,
    validate_collector_runtime,
)
from codex_agents.hosts import (  # noqa: E402
    CLAUDE_HOST_CLIENT,
    CODEX_HOST_CLIENT,
    CODEX_THREAD_MARKERS,
    HOST_CLIENTS,
    HostClientMismatch,
    HostClientUnknown,
    resolve_host_client,
)
from codex_agents.host_commands import (  # noqa: E402
    PRIMARY_HOST_COMMANDS,
    CodexHomeUnsupported,
    HostCommandError,
    OwnRootRefused,
    argv_prefix as codex_argv_prefix,
    bound_project_root,
    configured_codex_home,
    install_rule as install_codex_rule,
    installer_root,
    remove_rule as remove_codex_rule,
    render_rule as render_codex_rule,
    rule_path as codex_rule_path,
    rule_status as codex_rule_status,
    runtime_binding,
    runtime_copy_problem,
    runtime_copy_root,
)
from codex_agents.claude_host import (  # noqa: E402
    ClaudeHostDenied,
    check_backend,
    check_model_overrides,
    is_native_claude,
    native_spawn,
)
from codex_agents.providers import (  # noqa: E402
    AdmissionDenied,
    billing_attestation_record,
)
from codex_agents.routing import (  # noqa: E402
    PolicyError,
    _route_task_legacy_fallback,
    load_effective_policy,
    load_policy,
    route_task,
)
from codex_agents.runtime_files import RELEASES_FILE  # noqa: E402
from codex_agents.session_guard import (  # noqa: E402
    SessionRefused,
    check_current_session,
    scan_recent_sessions,
    session_guard_status,
)
from codex_agents.validation import validate_package  # noqa: E402

# A protected runtime copy serves the project its owner-only binding names, never
# the place it sits; a binding that does not verify refuses every command.
_RUNTIME_BINDING_ERROR: str | None = None
_BINDING: dict[str, str] | None = None
try:
    _BOUND_PROJECT = bound_project_root(ORCHESTRATION_ROOT)
    _BINDING = runtime_binding(ORCHESTRATION_ROOT)
except HostCommandError as _binding_error:
    _BOUND_PROJECT, _RUNTIME_BINDING_ERROR = None, str(_binding_error)
PROTECTED_RUNTIME = _BOUND_PROJECT is not None or _RUNTIME_BINDING_ERROR is not None
if _BOUND_PROJECT is not None:
    BUNDLE_ROOT = _BOUND_PROJECT
# Signed release data is not runtime code: a protected copy reads its project's.
RELEASES_PATH = BUNDLE_ROOT / ".codex" / "orchestration" / RELEASES_FILE
# The copy runs only what the rule allows, plus the collector it starts.
PROTECTED_RUNTIME_COMMANDS = frozenset(PRIMARY_HOST_COMMANDS) | {"learning-serve"}


def _bounded_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read(1_048_577)
    with Path(path).open("rb") as stream:
        return stream.read(1_048_577)


def _json_input(path: str) -> object:
    try:
        raw = _bounded_input(path)
    except OSError as exc:
        raise ContractError("input file is unavailable") from exc
    return parse_bounded_json_value(raw, max_bytes=1_048_576)


def _result_input(path: str) -> object:
    raw = _bounded_input(path)
    try:
        return parse_bounded_json_value(raw, max_bytes=1_048_576)
    except ContractError as exc:
        raise ResultContractError(
            "invalid_shape", "result must be one bounded JSON value"
        ) from exc


def _restore_lifecycle(
    ledger: LearningLedger, project_id: str, candidate_digest: str
) -> CandidateLifecycle:
    candidate, promotions = ledger.lifecycle_records(project_id, candidate_digest)
    return CandidateLifecycle.restore(
        candidate=candidate,
        champion_digest=candidate["base_digest"],
        project_id=project_id,
        authority=OwnerGrantAuthority(ledger.state.owner_allowed_signers()),
        digest_key=ledger.state.digest_key(),
        promotions=promotions,
    )


def _bounded_json_input(path: str) -> dict:
    try:
        raw = sys.stdin.buffer.read(65_537) if path == "-" else Path(path).read_bytes()
    except OSError as exc:
        raise ContractError("input file is unavailable") from exc
    return parse_bounded_json(raw)


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _add_assignment_context(
    parser: argparse.ArgumentParser, *, allow_policy_override: bool = True
) -> None:
    parser.add_argument("--assignment", required=True, help="JSON file")
    parser.add_argument("--task", required=True, help="routed task JSON file")
    parser.add_argument("--parent-grant", required=True, help="parent authority JSON file")
    parser.add_argument(
        "--fallback-receipt",
        help="trusted fallback receipt issued from an abandoned primary host run",
    )
    if allow_policy_override:
        # None marks an absent --policy, which an installed CLI refuses; main fills
        # in the installed policy after that check.
        parser.add_argument("--policy", type=Path)


def _add_learning_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)


class _RestartingLedger:
    def __init__(
        self, ledger: LearningLedger, maintenance: CollectorMaintenance
    ) -> None:
        self._ledger = ledger
        self._maintenance = maintenance
        self._closed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._ledger, name)

    def close(self, *, operation_completed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ledger.close()
        except Exception:
            try:
                self._maintenance.resume()
            except CollectorError:
                pass
            raise
        try:
            self._maintenance.resume()
        except CollectorError as exc:
            if operation_completed:
                raise CollectorRestartError(
                    "ledger operation completed; collector restart failed; "
                    "run learning-status for a registered project"
                ) from exc


def _close_ledger(ledger: LearningLedger | _RestartingLedger) -> None:
    if isinstance(ledger, _RestartingLedger):
        ledger.close(operation_completed=sys.exc_info()[0] is None)
    else:
        ledger.close()


# Codex's sandbox blocks the collector's socket and state, so a sandboxed primary
# gets this alongside the unchanged error.
CODEX_SANDBOX_HINT = (
    "If this ran inside the Codex sandbox: run host-command --project-root "
    "<exact-root> and repeat this command with its argv_prefix; if codex_rule is not "
    "installed, ask the owner to run ./setup.sh (Codex commands step)."
)
# Set when this invocation failed to reach the learning state or collector.
_COLLECTOR_UNREACHED = False


def _note_collector_failure(exc: BaseException) -> None:
    """Note a failure to reach the learning state or collector.

    A collector that answered (a rejection code or a version mismatch) was reached.
    """

    global _COLLECTOR_UNREACHED
    if isinstance(exc, OSError) or (
        isinstance(exc, CollectorError)
        and exc.code is None
        and not isinstance(exc, CollectorVersionError)
    ):
        _COLLECTOR_UNREACHED = True


@contextlib.contextmanager
def _collector_contact() -> Iterator[None]:
    try:
        yield
    except (OSError, CollectorError) as exc:
        _note_collector_failure(exc)
        raise


def _with_codex_hint(command: str, report: dict) -> dict:
    """Add the sandbox hint when a Codex primary-host command missed the collector."""

    if (
        _COLLECTOR_UNREACHED
        and command in PRIMARY_HOST_COMMANDS
        and any(os.environ.get(name) for name in CODEX_THREAD_MARKERS)
    ):
        return {**report, "hint": CODEX_SANDBOX_HINT}
    return report


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(Path(path).expanduser()))


def _require_protected_runtime(args: argparse.Namespace) -> None:
    """Refuse what a protected runtime copy must not run, before anything else.

    The copy is what the owner's Codex rule runs outside the sandbox. It
    serves only the primary-host commands (and the collector it starts), only
    while the project's installed runtime is still byte-for-byte this copy (a
    package update, or a change to the project's installed files, stops it
    until the owner runs setup again), and only for a Codex session whose own
    record shows it cannot write the folders the rule depends on
    (`session_guard`).
    """

    if not PROTECTED_RUNTIME:
        return
    if _RUNTIME_BINDING_ERROR is not None:
        raise HostCommandError(_RUNTIME_BINDING_ERROR)
    if args.command not in PROTECTED_RUNTIME_COMMANDS:
        raise HostCommandError("a protected runtime copy runs only the primary-host commands")
    if args.command == "learning-serve":
        return
    if runtime_copy_problem(ORCHESTRATION_ROOT.parent.parent, BUNDLE_ROOT) is not None:
        raise HostCommandError(
            "the project's installed runtime differs from its protected copy; ask the "
            "owner to run ./setup.sh (Codex commands step)"
        )
    # The calling Codex session may not be able to write anything the rule
    # depends on: its home folder, ~/.codex, this copy, the clone, tool folders,
    # the learning state, the private scratch, or a folder that contains the
    # project (or the project's .codex, .agents or .git as a folder of its own).
    try:
        check_current_session(
            ORCHESTRATION_ROOT.parent.parent,
            Path(_BINDING["clone_root"]) if _BINDING is not None else None,
            BUNDLE_ROOT,
        )
    except SessionRefused as exc:
        raise HostCommandError(str(exc)) from None


def _require_own_roots(args: argparse.Namespace) -> None:
    """Pin an installed CLI's primary-host commands to this project and its state.

    The owner's Codex command rule may run these commands outside the sandbox
    with arguments the model chose, so nothing may move a trust root: the state
    root must be the default one, the project root must be the project holding
    this CLI, and no policy file may be named. It keys on the install manifest
    next to this file, which a sandboxed Codex session cannot create or remove
    (`.codex/` is read-only there); the source tree keeps these options for
    development and tests. Host markers play no part. It runs before any git,
    process or collector work, and the checked, resolved values replace the
    given ones so nothing is resolved twice.
    """

    if args.command not in PRIMARY_HOST_COMMANDS or not os.path.lexists(INSTALL_MANIFEST):
        return
    if getattr(args, "policy", None) is not None:
        raise OwnRootRefused("--policy is refused: an installed CLI uses its installed policy")
    state_root = getattr(args, "state_root", None)
    if state_root is not None:
        if _resolved(state_root) != _resolved(default_state_root()):
            raise OwnRootRefused(
                "--state-root is refused: an installed CLI uses only the default learning state"
            )
        args.state_root = None
    for option, name in (("--project-root", "project_root"), ("--root", "root")):
        value = getattr(args, name, None)
        if value is None:
            continue
        if _resolved(value) != BUNDLE_ROOT:
            raise OwnRootRefused(
                f"{option} is refused: an installed CLI serves only its own project"
            )
        setattr(args, name, BUNDLE_ROOT)


def _learning_ledger(args: argparse.Namespace) -> tuple[_RestartingLedger, str]:
    with _collector_contact():
        state = LearningState(args.state_root, create=False)
        maintenance = CollectorMaintenance.acquire(
            state,
            ORCHESTRATION_ROOT / "cli.py",
            args.project_root,
        )
    try:
        ledger = LearningLedger(state)
    except Exception:
        maintenance.resume()
        raise
    managed = _RestartingLedger(ledger, maintenance)
    try:
        return managed, ledger.project_for_root(args.project_root)
    except Exception:
        managed.close(operation_completed=False)
        raise


def _validated_assignment(args: argparse.Namespace) -> dict:
    task = _json_input(args.task)
    project_root = getattr(args, "project_root", None)
    policy = (
        load_effective_policy(
            project_root,
            args.policy,
            ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
        )
        if project_root is not None
        else load_policy(args.policy)
    )
    fallback_receipt = args.fallback_receipt
    if fallback_receipt is not None and project_root is None:
        raise ContractError("fallback receipt requires a registered project")
    if isinstance(task, dict) and task.get("verification_for_outcome") is not None:
        if project_root is None:
            raise LedgerError("verification_for_outcome requires --project-root")
        ledger, project_id = _learning_ledger(args)
        try:
            ledger.verification_worker_result(project_id, task, with_result=False)
        finally:
            _close_ledger(ledger)
    if fallback_receipt is not None:
        ledger, project_id = _learning_ledger(args)
        try:
            route, _ = _trusted_host_route(
                ledger,
                project_id,
                project_root,
                task,
                fallback_receipt=fallback_receipt,
            )
        finally:
            _close_ledger(ledger)
    elif (
        project_root is not None
        and isinstance(task, dict)
        and task.get("routing_evidence_refs")
    ):
        ledger, project_id = _learning_ledger(args)
        try:
            trusted = ledger.trusted_routing_evidence_refs(project_id, task)
            route = route_task(
                task,
                policy,
                trusted_routing_evidence_refs=trusted,
                primary_provider=_host_primary_provider(),
            )
        finally:
            _close_ledger(ledger)
    else:
        route = route_task(task, policy, primary_provider=_host_primary_provider())
    return validate_assignment(
        _json_input(args.assignment),
        task=task,
        route=route,
        parent_grant=_json_input(args.parent_grant),
    )


# Bridged children run under their own ledger client, which declares the
# client-observed usage the bridge reports.
BRIDGE_CLIENT = "bridge-v1"
# The primary provider follows the detected host client and never the task.
HOST_CLIENT_PRIMARY_PROVIDERS = {
    CODEX_HOST_CLIENT: "codex",
    CLAUDE_HOST_CLIENT: "claude",
}
# Set from --host-client; it must agree with the detected host.
_EXPLICIT_HOST_CLIENT: str | None = None


def _host_client() -> str:
    """The host driving this lifecycle step, from its environment markers.

    The markers only catch accidental misuse; see codex_agents.hosts for the
    trust model. Admission, the bridge flag and the sandbox are the gates.
    """

    return resolve_host_client(os.environ, _EXPLICIT_HOST_CLIENT)


def _host_primary_provider(client: str | None = None) -> str:
    provider = HOST_CLIENT_PRIMARY_PROVIDERS.get(client or _host_client())
    if provider is None:
        raise ContractError("host client has no trusted primary provider")
    return provider


def _require_host_owner(
    ledger: LearningLedger,
    project_id: str,
    *,
    orchestration_handle: str | None = None,
    run_handle: str | None = None,
) -> str:
    """Refuse a lifecycle mutation from a host other than the one that owns it.

    Ownership is the host client stored when the orchestration opened; the
    bridge's execution client is separate and never counts as an owner.
    """

    host = _host_client()
    owner = ledger.host_owner(
        project_id, orchestration_handle=orchestration_handle, run_handle=run_handle
    )
    if owner != host:
        raise HostClientMismatch("host_client_mismatch")
    return host


def _claude_host_open_checks(
    ledger: LearningLedger,
    project_id: str,
    host: str,
    route: dict,
    project_root: Path,
) -> tuple[dict, dict | None]:
    """Run the Claude-host open checks and return the run profile to record.

    Every Claude-host open checks for a metered backend, since its children
    inherit the session. A native Claude child also gets its spawn parameters
    from code, and an environment that would override its model is denied.
    """

    if host != CLAUDE_HOST_CLIENT:
        return {
            "host_client": host, "backend_check": "not_applicable", "model_pin": None,
            "subagent_type": None, "model_alias": None, "effort": None,
        }, None
    try:
        check_backend(os.environ, project_root)
        spawn = None
        if is_native_claude(route):
            check_model_overrides(os.environ, project_root)
            spawn = native_spawn(route)
    except ClaudeHostDenied as exc:
        ledger.record_host_denial(
            project_id, provider=route.get("provider", "codex"), reason_code=exc.reason_code
        )
        raise
    return {
        "host_client": host,
        "backend_check": "passed",
        "model_pin": "alias" if spawn is not None else None,
        "subagent_type": spawn["subagent_type"] if spawn else None,
        "model_alias": spawn["model_alias"] if spawn else None,
        "effort": spawn["effort"] if spawn else None,
    }, spawn


def _host_client_for(route: dict) -> str:
    return BRIDGE_CLIENT if route.get("dispatch") == "bridge" else _host_client()


def _trusted_host_route(
    ledger: LearningLedger,
    project_id: str,
    project_root: Path,
    task: dict,
    *,
    fallback_receipt: str | None = None,
) -> tuple[dict, dict]:
    """Resolve the exact installed route used by the trusted host lifecycle."""

    policy = load_effective_policy(
        project_root,
        ORCHESTRATION_ROOT / "config" / "routing-policy.json",
        ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
    )
    trusted = ledger.trusted_routing_evidence_refs(project_id, task)
    # A verifier bound to a worker outcome is routed only when that outcome is
    # this project's, from the named provider and family, in an active boundary.
    # Open and dispatch read the result itself; recording needs only the binding.
    ledger.verification_worker_result(project_id, task, with_result=False)
    primary_route = route_task(
        task,
        policy,
        trusted_routing_evidence_refs=trusted,
        primary_provider=_host_primary_provider(),
    )
    clients = load_client_capabilities(
        ORCHESTRATION_ROOT / "config" / "learning-clients.json"
    )
    capability = clients[_host_client_for(primary_route)]
    primary_route["execution_configuration"] = installed_execution_configuration(
        BUNDLE_ROOT, primary_route, client_capability=capability
    )
    selection_slice = model_selection_slice(task)
    released = load_released_candidate_digests(
        RELEASES_PATH,
        verify_signature=lambda payload, signature: OwnerGrantAuthority(
            ledger.state.owner_allowed_signers()
        ).verify_release_signature(payload, signature),
    )
    applied = []
    for candidate in ledger.routing_candidates(
        project_id, selection_slice, released_candidate_digests=released
    ):
        primary_route = apply_candidate_to_route(
            primary_route,
            candidate,
            primary_route["execution_configuration"],
            policy,
        )
        applied.append(candidate["candidate_digest"])
    primary_route["learning_candidate_digests"] = applied
    primary_route["ready"] = True
    if fallback_receipt is None:
        return primary_route, capability
    fallback_authorization = ledger.verify_legacy_fallback_receipt(
        project_id,
        receipt_id=fallback_receipt,
        task=task,
        primary_route=primary_route,
        client_version=_host_client(),
    )
    route = _route_task_legacy_fallback(
        task,
        policy,
        trusted_routing_evidence_refs=trusted,
        primary_provider=_host_primary_provider(),
    )
    route["fallback"]["receipt_id"] = fallback_receipt
    route["fallback"]["authorization_digest"] = fallback_authorization[
        "authorization_digest"
    ]
    route["execution_configuration"] = installed_execution_configuration(
        BUNDLE_ROOT, route, client_capability=capability
    )
    route["learning_candidate_digests"] = []
    route["ready"] = True
    return route, capability


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a native package")
    validate_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    validate_parser.add_argument(
        "--package-only",
        action="store_true",
        help="exit on package readiness while still reporting project readiness",
    )

    inventory_parser = subparsers.add_parser("inventory", help="discover definitions inertly")
    inventory_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    inventory_parser.add_argument("--policy", type=Path)

    route_parser = subparsers.add_parser("route", help="route one task contract")
    route_parser.add_argument("--task", required=True, help="JSON file or - for stdin")
    route_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    route_parser.add_argument("--policy", type=Path)

    learning_route = subparsers.add_parser(
        "learning-route", help="route one task with approved learning lifecycle state"
    )
    learning_route.add_argument("--task", required=True)
    learning_route.add_argument("--client-version")
    learning_route.add_argument(
        "--fallback-from-run-handle",
        help="abandoned primary host run proving model unavailability",
    )
    _add_learning_project(learning_route)

    learning_canary_route = subparsers.add_parser(
        "learning-route-canary",
        help="resolve one reviewed canary case through its trusted ledger binding",
    )
    learning_canary_route.add_argument("--case-id", required=True)
    learning_canary_route.add_argument("--task", required=True)
    _add_learning_project(learning_canary_route)

    result_parser = subparsers.add_parser("validate-result", help="validate one result")
    result_parser.add_argument("--result", required=True, help="JSON file or - for stdin")
    result_parser.add_argument("--state-root", type=Path)
    result_parser.add_argument("--project-root", type=Path)
    _add_assignment_context(result_parser)

    result_preflight_parser = subparsers.add_parser(
        "result-preflight",
        help="print the exact non-mutating result template and admissibility rules",
    )
    result_preflight_parser.add_argument("--state-root", type=Path)
    result_preflight_parser.add_argument("--project-root", type=Path)
    _add_assignment_context(result_preflight_parser)

    assignment_parser = subparsers.add_parser(
        "validate-assignment", help="validate one bounded subagent assignment"
    )
    _add_assignment_context(assignment_parser)
    assignment_parser.add_argument("--project-root", type=Path)
    assignment_parser.add_argument("--state-root", type=Path)

    evaluate_parser = subparsers.add_parser("evaluate", help="aggregate evaluation JSONL")
    evaluate_parser.add_argument("--records", required=True, type=Path)
    evaluate_parser.add_argument(
        "--policy",
        type=Path,
        default=ORCHESTRATION_ROOT / "config" / "routing-policy.json",
    )

    learning_validate = subparsers.add_parser(
        "learning-validate", help="validate one closed learning record"
    )
    learning_validate.add_argument("--record", required=True, help="JSON file or -")

    learning_register = subparsers.add_parser(
        "learning-register", help="register one trusted project root"
    )
    learning_register.add_argument("--state-root", type=Path)
    learning_register.add_argument("--project-root", required=True, type=Path)
    learning_register.add_argument(
        "--move-legacy-state",
        help="approve moving the pre-0.8.10 macOS state: the SHA-256 of its "
        "owner-allowed-signers file as reviewed, or 'none'",
    )

    learning_serve = subparsers.add_parser(
        "learning-serve", help="run the single-writer local collector"
    )
    learning_serve.add_argument("--state-root", type=Path)

    learning_status = subparsers.add_parser(
        "learning-status", help="report persistent collector and redacted client health"
    )
    learning_status.add_argument("--state-root", type=Path)
    learning_status.add_argument("--project-root", required=True, type=Path)

    learning_backup = subparsers.add_parser(
        "learning-backup-project",
        help="quiesce the writer and create a recoverable project-partition backup",
    )
    _add_learning_project(learning_backup)
    learning_backup.add_argument("--file-id", required=True)
    learning_backup.add_argument("--expires-in-seconds", type=int, default=604800)

    learning_verify_backup = subparsers.add_parser(
        "learning-verify-backup",
        help="verify and restore a registered project backup in isolated memory",
    )
    _add_learning_project(learning_verify_backup)
    learning_verify_backup.add_argument("--file-id", required=True)

    learning_delete = subparsers.add_parser(
        "learning-delete-project", help="delete one registered project partition"
    )
    learning_delete.add_argument("--state-root", type=Path)
    learning_delete.add_argument("--project-root", required=True, type=Path)
    learning_delete.add_argument("--confirm-project-id", required=True)

    learning_app = subparsers.add_parser(
        "learning-normalize-app", help="normalize one App Server notification"
    )
    learning_app.add_argument("--state-root", type=Path)
    learning_app.add_argument("--message", required=True, help="JSON file or -")
    learning_app.add_argument("--client-version", required=True)

    learning_ingest_app = subparsers.add_parser(
        "learning-ingest-app", help="normalize and enqueue one App Server notification"
    )
    learning_ingest_app.add_argument("--state-root", type=Path)
    learning_ingest_app.add_argument("--project-root", required=True, type=Path)
    learning_ingest_app.add_argument("--message", required=True, help="JSON file or -")
    learning_ingest_app.add_argument("--client-version", required=True)

    learning_compare = subparsers.add_parser(
        "learning-compare", help="compare one inert candidate with matched trials"
    )
    learning_compare.add_argument("--candidate", required=True)
    learning_compare.add_argument("--trials", required=True)
    learning_compare.add_argument("--cases", required=True)
    learning_compare.add_argument("--client-version", required=True)
    learning_compare.add_argument("--independent-review-digest", required=True)
    learning_compare.add_argument("--grader-calibration")
    learning_compare.add_argument("--subjective-grading-required", action="store_true")

    learning_host_result = subparsers.add_parser(
        "learning-record-host-result",
        help="derive and atomically persist a host result and QA outcome",
    )
    _add_learning_project(learning_host_result)
    _add_assignment_context(learning_host_result, allow_policy_override=False)
    learning_host_result.add_argument("--result", required=True)
    learning_host_result.add_argument("--observation", required=True)
    learning_host_result.add_argument("--evaluation-assignment-digest")
    learning_host_result.add_argument("--trial-id")

    learning_open_host = subparsers.add_parser(
        "learning-open-host-run",
        help="open one exact assigned host run and return a bounded opaque handle",
    )
    _add_learning_project(learning_open_host)
    _add_assignment_context(learning_open_host, allow_policy_override=False)
    learning_open_host.add_argument("--route", required=True)
    learning_open_host.add_argument("--orchestration-handle", required=True)

    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="run one admitted, opened host run as a read-only bridged child",
    )
    _add_learning_project(dispatch_parser)
    _add_assignment_context(dispatch_parser, allow_policy_override=False)
    dispatch_parser.add_argument("--route", required=True)
    dispatch_parser.add_argument("--run-handle", required=True)

    provider_observe = subparsers.add_parser(
        "provider-observe",
        help="record a non-spending provider identity, headroom and billing observation",
    )
    _add_learning_project(provider_observe)
    provider_observe.add_argument(
        "--provider", required=True, choices=("codex", "claude")
    )
    provider_observe.add_argument(
        "--billing-probe",
        action="store_true",
        help="for Claude, also run one minimal locked turn to record billing evidence",
    )

    learning_billing_attestation = subparsers.add_parser(
        "learning-record-billing-attestation",
        help="record an owner-signed billing attestation for one provider account",
    )
    _add_learning_project(learning_billing_attestation)
    learning_billing_attestation.add_argument(
        "--provider", required=True, choices=("codex", "claude")
    )
    learning_billing_attestation.add_argument("--grant", required=True)
    learning_billing_attestation.add_argument("--signature", type=Path)

    learning_open_orchestration = subparsers.add_parser(
        "learning-open-orchestration",
        help="open one goal-scoped orchestration statistics boundary",
    )
    _add_learning_project(learning_open_orchestration)

    learning_checkpoint_orchestration = subparsers.add_parser(
        "learning-checkpoint-orchestration",
        help="record exact native-goal usage while an orchestration remains active",
    )
    _add_learning_project(learning_checkpoint_orchestration)
    learning_checkpoint_orchestration.add_argument(
        "--orchestration-handle", required=True
    )
    learning_checkpoint_orchestration.add_argument(
        "--goal-tokens-used", required=True, type=int
    )
    learning_checkpoint_orchestration.add_argument(
        "--goal-time-used-seconds", required=True, type=int
    )

    learning_pause_orchestration = subparsers.add_parser(
        "learning-pause-orchestration",
        help="persist an explicit pause without closing the orchestration",
    )
    _add_learning_project(learning_pause_orchestration)
    learning_pause_orchestration.add_argument("--orchestration-handle", required=True)

    learning_resume_orchestration = subparsers.add_parser(
        "learning-resume-orchestration",
        help="resume an explicitly paused orchestration without resetting history",
    )
    _add_learning_project(learning_resume_orchestration)
    learning_resume_orchestration.add_argument("--orchestration-handle", required=True)

    learning_close_orchestration = subparsers.add_parser(
        "learning-close-orchestration",
        help="close one orchestration boundary and return its statistics",
    )
    _add_learning_project(learning_close_orchestration)
    learning_close_orchestration.add_argument(
        "--orchestration-handle", required=True
    )
    learning_close_orchestration.add_argument(
        "--status",
        required=True,
        choices=("completed", "failed", "interrupted", "cancelled"),
    )
    learning_close_orchestration.add_argument("--goal-tokens-used", type=int)
    learning_close_orchestration.add_argument("--goal-time-used-seconds", type=int)

    learning_orchestration_report = subparsers.add_parser(
        "learning-orchestration-report",
        help="report content-free statistics for one orchestration",
    )
    _add_learning_project(learning_orchestration_report)
    learning_orchestration_report.add_argument("--orchestration-id", required=True)

    learning_bind_agent = subparsers.add_parser(
        "learning-bind-host-agent",
        help="bind a host run to the native subagent identifier",
    )
    _add_learning_project(learning_bind_agent)
    learning_bind_agent.add_argument("--run-handle", required=True)
    learning_bind_agent.add_argument("--agent-id", required=True)

    learning_observe_host = subparsers.add_parser(
        "learning-observe-host-run",
        help="persist progress or an elapsed observation deadline for one host run",
    )
    _add_learning_project(learning_observe_host)
    learning_observe_host.add_argument("--run-handle", required=True)
    learning_observe_host.add_argument(
        "--event", required=True, choices=("progress", "deadline")
    )

    learning_abandon_host = subparsers.add_parser(
        "learning-classify-host-run-abandoned",
        help="classify one pending host binding after its orchestration is paused",
    )
    _add_learning_project(learning_abandon_host)
    learning_abandon_host.add_argument("--run-handle", required=True)
    learning_abandon_host.add_argument(
        "--reason",
        required=True,
        choices=("host_confirmed_gone", "owner_cancelled", "recovery_unavailable"),
    )

    learning_prepare_upgrade = subparsers.add_parser(
        "learning-prepare-upgrade-reconciliation",
        help="prepare one exact superseded host run for detached owner signing",
    )
    _add_learning_project(learning_prepare_upgrade)
    learning_prepare_upgrade.add_argument("--orchestration-id", required=True)
    learning_prepare_upgrade.add_argument("--run-handle", required=True)

    learning_commit_upgrade = subparsers.add_parser(
        "learning-commit-upgrade-reconciliation",
        help="consume one signed superseded-run request and interrupt its boundary",
    )
    _add_learning_project(learning_commit_upgrade)
    learning_commit_upgrade.add_argument("--request", required=True)
    learning_commit_upgrade.add_argument("--signature", required=True)

    learning_verified_outcome = subparsers.add_parser(
        "learning-record-verified-outcome",
        help="derive QA from a durable assigned host run, checks, and verifier result",
    )
    _add_learning_project(learning_verified_outcome)
    learning_verified_outcome.add_argument(
        "--evaluation-assignment-digest", required=True
    )
    learning_verified_outcome.add_argument("--host-outcome-digest", required=True)
    learning_verified_outcome.add_argument(
        "--verifier-host-outcome-digest", required=True
    )
    learning_verified_outcome.add_argument("--evaluator-assignment", required=True)
    learning_verified_outcome.add_argument("--evaluator-result", required=True)
    learning_verified_outcome.add_argument("--check-report", required=True)
    learning_verified_outcome.add_argument("--verifier-assignment", required=True)
    learning_verified_outcome.add_argument("--verifier-result", required=True)
    learning_verified_outcome.add_argument("--signature", type=Path)

    learning_operational_qa = subparsers.add_parser(
        "learning-record-operational-qa",
        help="record automatic QA for two durable host-bound runs",
    )
    _add_learning_project(learning_operational_qa)
    learning_operational_qa.add_argument("--host-outcome-digest", required=True)
    learning_operational_qa.add_argument(
        "--verifier-host-outcome-digest", required=True
    )
    learning_operational_qa.add_argument("--evaluator-assignment", required=True)
    learning_operational_qa.add_argument("--evaluator-result", required=True)
    learning_operational_qa.add_argument("--check-receipt", required=True)
    learning_operational_qa.add_argument("--verifier-assignment", required=True)
    learning_operational_qa.add_argument("--verifier-result", required=True)

    learning_run_checks = subparsers.add_parser(
        "learning-run-operational-checks",
        help="run project-approved checks and issue one bound QA receipt",
    )
    _add_learning_project(learning_run_checks)
    learning_run_checks.add_argument("--host-outcome-digest", required=True)
    learning_run_checks.add_argument(
        "--verifier-host-outcome-digest", required=True
    )

    learning_expire = subparsers.add_parser(
        "learning-expire", help="apply retention and lineage deletion for one project"
    )
    _add_learning_project(learning_expire)
    learning_expire.add_argument("--now-ms", type=int)

    learning_cycle = subparsers.add_parser(
        "learning-run-cycle",
        help="run deterministic aggregation, case mining, and inert proposal generation",
    )
    _add_learning_project(learning_cycle)
    learning_cycle.add_argument("--max-age-ms", type=int, default=90 * 24 * 60 * 60 * 1000)

    learning_assignment = subparsers.add_parser(
        "learning-create-evaluation-assignment",
        help="persist the complete matched evaluation population before trials run",
    )
    _add_learning_project(learning_assignment)
    learning_assignment.add_argument("--candidate-digest", required=True)
    learning_assignment.add_argument("--case-ids", required=True)
    learning_assignment.add_argument("--client-version", required=True)
    learning_assignment.add_argument("--observed-at-ms", required=True, type=int)

    learning_trial = subparsers.add_parser(
        "learning-record-trial", help="derive and persist one pre-assigned trial"
    )
    _add_learning_project(learning_trial)
    learning_trial.add_argument("--evaluation-assignment-digest", required=True)
    learning_trial.add_argument("--trial-id", required=True)
    learning_trial.add_argument("--outcome-digest", required=True)

    learning_review_evidence = subparsers.add_parser(
        "learning-record-evidence-review",
        help="record a post-population review of owner-attested verified subjects",
    )
    _add_learning_project(learning_review_evidence)
    learning_review_evidence.add_argument("--subject-digests", required=True)

    learning_record_comparison = subparsers.add_parser(
        "learning-record-comparison",
        help="derive and persist a comparison from the complete assigned population",
    )
    _add_learning_project(learning_record_comparison)
    learning_record_comparison.add_argument(
        "--evaluation-assignment-digest", required=True
    )
    learning_record_comparison.add_argument(
        "--independent-review-digest", required=True
    )
    learning_record_comparison.add_argument("--grader-calibration-digest")
    learning_record_comparison.add_argument(
        "--subjective-grading-required", action="store_true"
    )

    learning_mine = subparsers.add_parser(
        "learning-mine-case", help="mine one development case from an admitted outcome"
    )
    _add_learning_project(learning_mine)
    learning_mine.add_argument("--source-outcome-id", required=True)
    learning_mine.add_argument("--task", required=True)
    learning_mine.add_argument("--criteria-digest", required=True)
    learning_mine.add_argument("--lineage-digest", required=True)

    learning_review = subparsers.add_parser(
        "learning-review-case", help="review and freeze one development case"
    )
    _add_learning_project(learning_review)
    learning_review.add_argument("--case-id", required=True)
    learning_review.add_argument("--review-digest", required=True)
    learning_review.add_argument("--similarity-group-digest", required=True)
    learning_review.add_argument(
        "--destination", required=True, choices=("promotion_holdout", "canary")
    )
    learning_review.add_argument("--freeze", action="store_true")

    learning_analyze = subparsers.add_parser(
        "learning-analyze", help="analyze admitted project outcomes by configuration"
    )
    _add_learning_project(learning_analyze)
    learning_analyze.add_argument("--as-of-ms", required=True, type=int)
    learning_analyze.add_argument("--max-age-ms", required=True, type=int)

    learning_candidate = subparsers.add_parser(
        "learning-generate-candidate", help="generate and store one inert typed candidate"
    )
    _add_learning_project(learning_candidate)
    learning_candidate.add_argument("--spec", required=True)

    learning_calibration = subparsers.add_parser(
        "learning-calibrate-grader", help="record owner-labelled grader calibration"
    )
    _add_learning_project(learning_calibration)
    learning_calibration.add_argument("--labels", required=True)
    learning_calibration.add_argument("--configuration", required=True)
    learning_calibration.add_argument("--observed-at-ms", required=True, type=int)
    learning_calibration.add_argument("--signature", type=Path)

    learning_feedback = subparsers.add_parser(
        "learning-record-feedback", help="record controlled owner feedback for one admitted run"
    )
    _add_learning_project(learning_feedback)
    learning_feedback.add_argument("--run-id", required=True)
    learning_feedback.add_argument(
        "--label", required=True, choices=("accepted", "rejected", "corrected")
    )
    learning_feedback.add_argument(
        "--reason-code",
        required=True,
        choices=("quality", "incorrect", "incomplete", "unsafe", "usage", "scope", "other"),
    )
    learning_feedback.add_argument("--correction-file", type=Path)
    learning_feedback.add_argument("--observed-at-ms", required=True, type=int)
    learning_feedback.add_argument("--signature", type=Path)

    learning_evaluate_candidate = subparsers.add_parser(
        "learning-evaluate-candidate", help="record a durable candidate evaluation decision"
    )
    _add_learning_project(learning_evaluate_candidate)
    learning_evaluate_candidate.add_argument("--candidate-digest", required=True)
    learning_evaluate_candidate.add_argument("--comparison-digest", required=True)
    learning_evaluate_candidate.add_argument("--observed-at-ms", required=True, type=int)

    learning_start_canary = subparsers.add_parser(
        "learning-start-canary", help="consume one owner grant and start its bounded canary"
    )
    _add_learning_project(learning_start_canary)
    learning_start_canary.add_argument("--candidate-digest", required=True)
    learning_start_canary.add_argument("--comparison-digest", required=True)
    learning_start_canary.add_argument("--case-digests", required=True)
    learning_start_canary.add_argument("--grant", required=True)

    learning_complete_canary = subparsers.add_parser(
        "learning-complete-canary", help="derive and record a canary decision from durable outcomes"
    )
    _add_learning_project(learning_complete_canary)
    learning_complete_canary.add_argument("--candidate-digest", required=True)
    learning_complete_canary.add_argument("--comparison-digest", required=True)
    learning_complete_canary.add_argument("--independent-review-digest", required=True)
    learning_complete_canary.add_argument("--case-results", required=True)
    learning_complete_canary.add_argument("--observed-at-ms", required=True, type=int)

    learning_promote = subparsers.add_parser(
        "learning-promote", help="consume a separate owner grant after a passing canary"
    )
    _add_learning_project(learning_promote)
    learning_promote.add_argument("--candidate-digest", required=True)
    learning_promote.add_argument("--canary-report-digest", required=True)
    learning_promote.add_argument("--grant", required=True)

    learning_drift = subparsers.add_parser(
        "learning-record-drift", help="record rejecting matched evidence for an active candidate"
    )
    _add_learning_project(learning_drift)
    learning_drift.add_argument("--candidate-digest", required=True)
    learning_drift.add_argument("--comparison-digest", required=True)
    learning_drift.add_argument("--observed-at-ms", required=True, type=int)

    learning_rollback = subparsers.add_parser(
        "learning-rollback", help="record exact failback to the candidate champion"
    )
    _add_learning_project(learning_rollback)
    learning_rollback.add_argument("--candidate-digest", required=True)
    learning_rollback.add_argument("--evidence-digest", required=True)
    learning_rollback.add_argument("--observed-at-ms", required=True, type=int)

    learning_owner_key = subparsers.add_parser(
        "learning-configure-owner-key", help="install one owner public SSH key"
    )
    learning_owner_key.add_argument("--state-root", type=Path)
    learning_owner_key.add_argument("--public-key", required=True, type=Path)

    learning_grant_request = subparsers.add_parser(
        "learning-create-grant-request", help="create exact bytes for detached owner signing"
    )
    _add_learning_project(learning_grant_request)
    learning_grant_request.add_argument(
        "--kind", required=True, choices=("canary", "promotion")
    )
    learning_grant_request.add_argument("--candidate-digest", required=True)
    learning_grant_request.add_argument("--champion-digest", required=True)
    learning_grant_request.add_argument("--review-digest", required=True)
    learning_grant_request.add_argument("--bounds-digest", required=True)
    learning_grant_request.add_argument("--issued-at-ms", required=True, type=int)
    learning_grant_request.add_argument("--expires-at-ms", required=True, type=int)

    learning_attach_signature = subparsers.add_parser(
        "learning-attach-grant-signature", help="verify and attach a detached owner signature"
    )
    learning_attach_signature.add_argument("--state-root", type=Path)
    learning_attach_signature.add_argument("--request", required=True)
    learning_attach_signature.add_argument("--signature", required=True, type=Path)

    # Read-only and collector-free, so a sandboxed Codex primary can run it first.
    host_command = subparsers.add_parser(
        "host-command",
        help="print the exact command prefix a Codex primary uses and its rule status",
    )
    host_command.add_argument("--project-root", required=True, type=Path)

    install_codex_rule = subparsers.add_parser(
        "install-codex-rule",
        help="owner setup: write the Codex command rule for one project",
    )
    install_codex_rule.add_argument("--project-root", required=True, type=Path)
    install_codex_rule.add_argument(
        "--replace", action="store_true", help="replace a differing rule for this project"
    )
    install_codex_rule.add_argument(
        "--print", dest="print_only", action="store_true",
        help="print the rule that would be written, without writing it",
    )

    remove_codex_rule_parser = subparsers.add_parser(
        "remove-codex-rule",
        help="owner removal: delete the Codex command rule and runtime copy for one project",
    )
    remove_codex_rule_parser.add_argument("--project-root", required=True, type=Path)

    # Read-only: which recent Codex sessions could write this clone, the home folder,
    # the learning state, or a folder that contains a set-up project.
    session_scan = subparsers.add_parser(
        "codex-session-scan",
        help="setup: list recent Codex sessions that could write this clone, the home "
        "folder, the learning state or a folder containing a set-up project",
    )
    session_scan.add_argument("--clone-root", required=True, type=Path)

    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "--host-client",
            choices=HOST_CLIENTS,
            help="expected host client; refused unless it matches the detected host",
        )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    global _EXPLICIT_HOST_CLIENT, _COLLECTOR_UNREACHED
    _EXPLICIT_HOST_CLIENT = args.host_client
    _COLLECTOR_UNREACHED = False
    try:
        _require_protected_runtime(args)
        _require_own_roots(args)
        if getattr(args, "policy", False) is None:
            args.policy = ORCHESTRATION_ROOT / "config" / (
                "discovery-policy.json" if args.command == "inventory" else "routing-policy.json"
            )
        if args.command == "validate":
            report = validate_package(args.root)
        elif args.command == "inventory":
            report = discover(args.root, load_discovery_policy(args.policy))
        elif args.command == "route":
            report = route_task(
                _json_input(args.task),
                load_effective_policy(
                    args.root,
                    args.policy,
                    ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
                ),
                primary_provider=_host_primary_provider(),
            )
        elif args.command == "learning-route":
            task = _json_input(args.task)
            policy = load_effective_policy(
                args.project_root,
                ORCHESTRATION_ROOT / "config" / "routing-policy.json",
                ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
            )
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            explicit_client = args.client_version
            if explicit_client in HOST_CLIENTS:
                # An explicit host must be the detected one; it is then treated
                # exactly like the implicit host, bridge client included.
                if explicit_client != _host_client():
                    raise HostClientUnknown("host_client_unknown")
                explicit_client = None
            client_version = explicit_client or _host_client()
            if client_version not in clients:
                raise ContractError(
                    "client version is not in the validated capability matrix"
                )
            selection_slice = model_selection_slice(task)
            ledger, project_id = _learning_ledger(args)
            try:
                trusted = ledger.trusted_routing_evidence_refs(project_id, task)
                ledger.verification_worker_result(project_id, task)
                base_route = route_task(
                    task,
                    policy,
                    trusted_routing_evidence_refs=trusted,
                    primary_provider=_host_primary_provider(),
                )
                if explicit_client is None:
                    # A bridged route runs under the bridge client, as the host
                    # lifecycle will when it opens the run.
                    client_version = _host_client_for(base_route)
                configuration = installed_execution_configuration(
                    BUNDLE_ROOT, base_route, client_capability=clients[client_version]
                )
                released = load_released_candidate_digests(
                    RELEASES_PATH,
                    verify_signature=lambda payload, signature: OwnerGrantAuthority(
                        ledger.state.owner_allowed_signers()
                    ).verify_release_signature(payload, signature),
                )
                applied = []
                report = dict(base_route)
                report["execution_configuration"] = dict(configuration)
                for candidate in ledger.routing_candidates(
                    project_id,
                    selection_slice,
                    released_candidate_digests=released,
                ):
                    report = apply_candidate_to_route(
                        report, candidate, report["execution_configuration"], policy
                    )
                    applied.append(candidate["candidate_digest"])
                report["learning_candidate_digests"] = applied
                report["ready"] = True
                if args.fallback_from_run_handle is not None:
                    _require_host_owner(
                        ledger, project_id, run_handle=args.fallback_from_run_handle
                    )
                    if base_route.get("dispatch") == "bridge":
                        # The fallback answers a native host's model rejection; a
                        # bridged child (every Codex child) ends fenced instead.
                        raise ContractError(
                            "the legacy fallback applies only to native children"
                        )
                    receipt = ledger.issue_legacy_fallback_receipt(
                        project_id,
                        run_handle=args.fallback_from_run_handle,
                        task=task,
                        primary_route=report,
                        client_version=client_version,
                    )
                    report = _route_task_legacy_fallback(
                        task,
                        policy,
                        trusted_routing_evidence_refs=trusted,
                        primary_provider=_host_primary_provider(),
                    )
                    report["fallback"]["receipt_id"] = receipt["receipt_id"]
                    report["fallback"]["authorization_digest"] = receipt[
                        "authorization_digest"
                    ]
                    report["execution_configuration"] = (
                        installed_execution_configuration(
                            BUNDLE_ROOT,
                            report,
                            client_capability=clients[client_version],
                        )
                    )
                    report["learning_candidate_digests"] = []
                    report["ready"] = True
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-route-canary":
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            ledger, project_id = _learning_ledger(args)
            try:
                case, candidate = ledger.canary_routing_candidate(
                    project_id, args.case_id
                )
                task = _json_input(args.task)
                task_slice = routing_task_slice(task)
                if (
                    task["id"] != case["record_id"]
                    or task_slice != case["task_slice"]
                    or runtime_fixture_digest(ledger.state.digest_key(), task)
                    != case["fixture_digest"]
                ):
                    raise ContractError("canary task does not match its frozen fixture")
                champion = candidate["champion_configuration"]
                matching_clients = [
                    (version, capability)
                    for version, capability in clients.items()
                    if content_digest(capability) == champion["client_digest"]
                ]
                if len(matching_clients) != 1:
                    raise ContractError(
                        "canary case client does not resolve to exactly one accepted capability"
                    )
                client_version, client_capability = matching_clients[0]
                policy = load_policy(
                    ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                )
                trusted = ledger.trusted_routing_evidence_refs(project_id, task)
                report = route_task(
                    task,
                    policy,
                    trusted_routing_evidence_refs=trusted,
                    primary_provider=_host_primary_provider(),
                )
                current_configuration = installed_execution_configuration(
                    BUNDLE_ROOT,
                    report,
                    client_capability=client_capability,
                )
                if current_configuration != champion:
                    raise ContractError(
                        "canary champion differs from the installed task route"
                    )
                report["execution_configuration"] = current_configuration
                report = apply_candidate_to_route(
                    report, candidate, current_configuration, policy
                )
                report.update(
                    ready=True,
                    task_slice=case["task_slice"],
                    client_version=client_version,
                    canary_case_id=case["record_id"],
                    canary_case_digest=content_digest(case),
                    learning_candidate_digests=[candidate["candidate_digest"]],
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "validate-assignment":
            report = _validated_assignment(args)
        elif args.command == "result-preflight":
            report = {"ready": True, **result_preflight(_validated_assignment(args))}
        elif args.command == "validate-result":
            assignment = _validated_assignment(args)
            try:
                report = validate_result(_result_input(args.result), assignment=assignment)
            except ResultContractError as exc:
                repair_receipt = None
                learning_ready: bool | None = None
                if args.project_root is not None:
                    learning_ready = False
                    try:
                        state = LearningState(args.state_root, create=False)
                        ensure_collector(
                            state,
                            ORCHESTRATION_ROOT / "cli.py",
                            project_root=args.project_root,
                        )
                        assignment_digest = keyed_digest(
                            state.digest_key(),
                            canonical_json(validate_assignment_shape(assignment)),
                        )
                        repair_receipt = CollectorClient(
                            state.socket
                        ).issue_result_repair(
                            args.project_root,
                            assignment_digest,
                            exc.reason_code,
                        )
                        learning_ready = True
                    except (CollectorError, StateError, OSError) as failure:
                        _note_collector_failure(failure)
                _print(
                    _with_codex_hint(args.command, {
                        "ready": False,
                        "error": "result validation failed",
                        "reason_code": exc.reason_code,
                        "repair_receipt": repair_receipt,
                        "learning_ready": learning_ready,
                    })
                )
                return 2
        elif args.command == "evaluate":
            report = evaluate(load_jsonl(args.records), load_policy(args.policy))
        elif args.command == "learning-validate":
            report = validate_learning_record(_bounded_json_input(args.record))
        elif args.command == "learning-register":
            validate_collector_runtime(ORCHESTRATION_ROOT / "cli.py")
            # The owner's registration copies a pre-0.8.10 macOS state folder to the
            # default location first (once) and renames the old one aside; nothing
            # else does. `state_moved` names the old copy, which the owner may delete.
            moved_aside = (
                migrate_legacy_state(
                    ORCHESTRATION_ROOT / "cli.py",
                    args.project_root,
                    approved_signers=args.move_legacy_state,
                )
                if args.state_root is None
                else None
            )
            state = LearningState(args.state_root)
            maintenance = CollectorMaintenance.acquire(
                state,
                ORCHESTRATION_ROOT / "cli.py",
                args.project_root,
                owner_registration=True,
            )
            try:
                ledger = LearningLedger(state)
                try:
                    project_id = ledger.register_project(args.project_root)
                finally:
                    _close_ledger(ledger)
                authorize_collector_runtime(
                    state, ORCHESTRATION_ROOT / "cli.py"
                )
            except Exception:
                try:
                    maintenance.resume()
                except CollectorError:
                    pass
                raise
            try:
                maintenance.resume()
            except CollectorError as exc:
                raise CollectorRestartError(
                    "project registration completed; collector restart failed; "
                    "run learning-status for the registered project"
                ) from exc
            health = ensure_collector(
                state,
                ORCHESTRATION_ROOT / "cli.py",
                project_root=args.project_root,
            )
            report = {
                "ready": True,
                "active": True,
                "project_id": project_id,
                "collector_schema_version": health["schema_version"],
                "collector_runtime_version": health["runtime_version"],
                "collector_runtime_digest": health["runtime_digest"],
                "state_moved": str(moved_aside) if moved_aside is not None else None,
            }
        elif args.command == "learning-serve":
            state = LearningState(args.state_root, create=False)
            require_authorized_collector_runtime(state)
            ledger = LearningLedger(state)
            server = CollectorServer(ledger)
            try:
                server.start()
                while not server.wait(0.5):
                    pass
            except KeyboardInterrupt:
                pass
            finally:
                server.stop()
                _close_ledger(ledger)
            report = {"ready": True, "stopped": True}
        elif args.command == "learning-status":
            with _collector_contact():
                state = LearningState(args.state_root, create=False)
            try:
                with _collector_contact():
                    ensure_collector(
                        state,
                        ORCHESTRATION_ROOT / "cli.py",
                        project_root=args.project_root,
                    )
                    health = CollectorClient(state.socket).health(args.project_root)
                client_failures = client_health_snapshot(state, args.project_root)
                report = {
                    "ready": True,
                    "active": True,
                    "collector_schema_version": health["schema_version"],
                    "collector_runtime_version": health["runtime_version"],
                    "collector_runtime_digest": health["runtime_digest"],
                    "started_at_ms": health["started_at_ms"],
                    "collector_health": health.get("collector_health", {}),
                    "client_failures": client_failures,
                    "ingestion": health["ingestion"],
                }
            except CollectorError as exc:
                client_failures = client_health_snapshot(state, args.project_root)
                report = _with_codex_hint(args.command, {
                    "ready": False,
                    "active": False,
                    "error": "collector or project registration is unavailable",
                    "client_failures": client_failures,
                    "ingestion": empty_ingestion_snapshot(
                        registered=exc.code != "project_unregistered"
                    ),
                })
        elif args.command == "learning-backup-project":
            if args.expires_in_seconds < 60:
                raise ContractError("backup expiry must be at least 60 seconds")
            ledger, project_id = _learning_ledger(args)
            try:
                path = ledger.create_sanitized_backup(
                    project_id,
                    file_id=args.file_id,
                    expires_at_ms=(
                        time.time_ns() // 1_000_000
                        + args.expires_in_seconds * 1000
                    ),
                )
                verified = ledger.verify_sanitized_backup(
                    project_id, file_id=args.file_id
                )
                report = {
                    "ready": True,
                    "project_id": project_id,
                    "path": str(path),
                    **verified,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-verify-backup":
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    "project_id": project_id,
                    **ledger.verify_sanitized_backup(
                        project_id, file_id=args.file_id
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-delete-project":
            ledger, project_id = _learning_ledger(args)
            try:
                if args.confirm_project_id != project_id:
                    raise LedgerError("project deletion confirmation does not match")
                report = {
                    "ready": True,
                    "project_id": project_id,
                    "deletion_id": ledger.delete_project(project_id),
                }
                client_health_clear(ledger.state, args.project_root)
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-normalize-app":
            state = LearningState(args.state_root, create=False)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            normalized = normalize_app_server(
                _bounded_json_input(args.message),
                digest_key=state.digest_key(),
                client_version=args.client_version,
                coverage=clients[args.client_version]["app_server_coverage"],
            )
            report = {"ready": normalized is not None, "record": normalized}
        elif args.command == "learning-ingest-app":
            state = LearningState(args.state_root, create=False)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            message = _bounded_json_input(args.message)
            normalized = normalize_app_server(
                message,
                digest_key=state.digest_key(),
                client_version=args.client_version,
                coverage=clients[args.client_version]["app_server_coverage"],
            )
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            if normalized is None or not isinstance(thread_id, str) or not thread_id:
                raise ContractError("App Server notification is not ingestible")
            client = CollectorClient(state.socket)
            binding = client.bind(
                args.project_root, thread_id, "app_server", args.client_version
            )
            report = {"ready": True, **client.ingest(binding, normalized)}
        elif args.command == "learning-compare":
            trials = _json_input(args.trials)
            cases = _json_input(args.cases)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            if not isinstance(trials, list) or not isinstance(cases, list):
                raise ContractError("trials and cases must be arrays")
            report = compare_candidate(
                candidate=_json_input(args.candidate),
                trials=trials,
                cases=cases,
                client_version=args.client_version,
                client_capability=clients[args.client_version],
                independent_review_digest=args.independent_review_digest,
                grader_calibration=(
                    _json_input(args.grader_calibration)
                    if args.grader_calibration else None
                ),
                subjective_grading_required=args.subjective_grading_required,
            )
            report["ready"] = report["decision"] == "promote"
        elif args.command == "learning-open-host-run":
            task = _json_input(args.task)
            supplied_route = _json_input(args.route)
            ledger, project_id = _learning_ledger(args)
            try:
                host = _require_host_owner(
                    ledger, project_id, orchestration_handle=args.orchestration_handle
                )
                route, _ = _trusted_host_route(
                    ledger,
                    project_id,
                    args.project_root,
                    task,
                    fallback_receipt=args.fallback_receipt,
                )
                if supplied_route != route:
                    raise ContractError("supplied route does not match the installed route")
                assignment = validate_assignment(
                    _json_input(args.assignment),
                    task=task,
                    route=route,
                    parent_grant=_json_input(args.parent_grant),
                )
                verification_input = ledger.verification_worker_result(
                    project_id, task, orchestration_handle=args.orchestration_handle
                )
                host_profile, spawn = _claude_host_open_checks(
                    ledger, project_id, host, route, args.project_root.resolve()
                )
                # The ledger admits the run itself as it opens it; later steps read
                # the stored decision. A verifier is admitted when its run opens.
                report = {
                    "ready": True,
                    **ledger.open_host_run(
                        project_id,
                        assignment=assignment,
                        task=task,
                        route=route,
                        execution_configuration=route["execution_configuration"],
                        client_version=_host_client_for(route),
                        orchestration_handle=args.orchestration_handle,
                        fallback_receipt=args.fallback_receipt,
                        host_profile=host_profile,
                    ),
                    **({"native_spawn": spawn} if spawn is not None else {}),
                    # A native verifier cannot be handed a file by the bridge, so
                    # the primary puts this ledger-read result in its prompt. A
                    # bridged verifier gets it from `dispatch` instead.
                    **(
                        {"verification_input": {
                            "host_outcome_digest": verification_input["host_outcome_digest"],
                            "worker_result": verification_input["worker_result"],
                        }}
                        if verification_input is not None
                        and route.get("dispatch") != "bridge"
                        else {}
                    ),
                    # Native children of either provider run with the parent
                    # session's permissions, so only a bridged Claude child's
                    # reads are contained.
                    "read_containment": (
                        "enforced"
                        if route.get("dispatch") == "bridge"
                        and route.get("provider") == "claude"
                        else "advisory"
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "dispatch":
            if args.fallback_receipt is not None:
                raise ContractError("bridged dispatch never uses a fallback receipt")
            task = _json_input(args.task)
            route = _json_input(args.route)
            if not isinstance(route, dict):
                raise ContractError("route must be an object")
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                # A termination signal kills the child's process group before
                # this process exits, and the export is still removed.
                with bridge_signal_guard():
                    report = {
                        "ready": True,
                        **dispatch_bridge(
                            ledger,
                            project_id,
                            project_root=args.project_root.resolve(),
                            bundle_root=BUNDLE_ROOT,
                            task=task,
                            route=route,
                            assignment=_json_input(args.assignment),
                            parent_grant=_json_input(args.parent_grant),
                            run_handle=args.run_handle,
                            result_schema=(
                                ORCHESTRATION_ROOT / "schemas" / "result.schema.json"
                            ).read_bytes(),
                        ),
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "provider-observe":
            ledger, project_id = _learning_ledger(args)
            try:
                if args.billing_probe and args.provider != "claude":
                    raise ContractError("the billing probe is for Claude only")
                probe = None
                if args.billing_probe:
                    observed = ledger.observe_claude_billing(
                        project_id, project_root=args.project_root.resolve()
                    )
                    probe, records = observed["probe"], observed["records"]
                else:
                    records = ledger.observe_provider(
                        project_id,
                        args.provider,
                        project_root=args.project_root.resolve(),
                    )
                # The local client path stays in the ledger and is never printed.
                report = {
                    "ready": True,
                    "records": [
                        {
                            **{
                                key: value for key, value in record.items()
                                if key != "client_path"
                            },
                            # Display only: admission makes its own decision,
                            # by the same rule, from the ledger.
                            **(
                                {"paid_usage_waived_by_attestation": (
                                    ledger.billing_waived_by_attestation(
                                        project_id, record
                                    )
                                )}
                                if record.get("record_type") == "provider_billing"
                                else {}
                            ),
                        }
                        for record in records
                    ],
                    **({"probe": probe} if probe is not None else {}),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-billing-attestation":
            ledger, project_id = _learning_ledger(args)
            try:
                record = billing_attestation_record(_json_input(args.grant))
                if record["provider"] != args.provider:
                    raise ContractError("billing grant provider does not match")
                authority = OwnerGrantAuthority(ledger.state.owner_allowed_signers())
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-open-orchestration":
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.open_orchestration(
                        project_id, client_version=_host_client()
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-checkpoint-orchestration":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, orchestration_handle=args.orchestration_handle
                )
                report = {
                    "ready": True,
                    **ledger.checkpoint_orchestration(
                        project_id,
                        orchestration_handle=args.orchestration_handle,
                        goal_tokens_used=args.goal_tokens_used,
                        goal_time_used_seconds=args.goal_time_used_seconds,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command in {
            "learning-pause-orchestration", "learning-resume-orchestration"
        }:
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, orchestration_handle=args.orchestration_handle
                )
                report = {
                    "ready": True,
                    **ledger.set_orchestration_paused(
                        project_id,
                        orchestration_handle=args.orchestration_handle,
                        paused=args.command == "learning-pause-orchestration",
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-close-orchestration":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, orchestration_handle=args.orchestration_handle
                )
                report = {
                    "ready": True,
                    **ledger.close_orchestration(
                        project_id,
                        orchestration_handle=args.orchestration_handle,
                        status=args.status,
                        goal_tokens_used=args.goal_tokens_used,
                        goal_time_used_seconds=args.goal_time_used_seconds,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-orchestration-report":
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.orchestration_report(
                        project_id, args.orchestration_id
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-bind-host-agent":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.bind_host_agent(
                        project_id,
                        run_handle=args.run_handle,
                        agent_id=args.agent_id,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-observe-host-run":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.observe_host_run(
                        project_id,
                        run_handle=args.run_handle,
                        event=args.event,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-classify-host-run-abandoned":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.classify_host_run_abandoned(
                        project_id,
                        run_handle=args.run_handle,
                        reason=args.reason,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-prepare-upgrade-reconciliation":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                request = ledger.prepare_upgrade_reconciliation(
                    project_id,
                    orchestration_id=args.orchestration_id,
                    run_handle=args.run_handle,
                )
                report = {
                    "ready": True,
                    "request": request,
                    "signing_payload_b64": base64.b64encode(
                        canonical_json(request)
                    ).decode("ascii"),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-commit-upgrade-reconciliation":
            request = _json_input(args.request)
            signature = _bounded_input(args.signature)
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.commit_upgrade_reconciliation(
                        project_id, request=request, signature=signature
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-host-result":
            task = _json_input(args.task)
            result = _json_input(args.result)
            observation = _bounded_json_input(args.observation)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            trusted_host = "run_handle" in observation
            # The host is detected unconditionally. Legacy telemetry recording
            # (no run handle) is a Codex path: only a Codex host, for a Codex run.
            host = _host_client()
            client_version = host if trusted_host else observation.get("client_version")
            if not trusted_host and (
                host != CODEX_HOST_CLIENT
                or not isinstance(client_version, str)
                or not client_version.startswith("codex-")
            ):
                raise HostClientMismatch("host_client_mismatch")
            if client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            if (args.evaluation_assignment_digest is None) != (args.trial_id is None):
                raise ContractError(
                    "evaluation assignment digest and trial id must appear together"
                )
            ledger, project_id = _learning_ledger(args)
            try:
                if trusted_host:
                    _require_host_owner(
                        ledger, project_id, run_handle=observation["run_handle"]
                    )
                    if args.evaluation_assignment_digest is not None:
                        raise ContractError(
                            "trusted host handles do not accept evaluation overrides"
                        )
                    route, _ = _trusted_host_route(
                        ledger,
                        project_id,
                        args.project_root,
                        task,
                        fallback_receipt=args.fallback_receipt,
                    )
                    assignment = validate_assignment(
                        _json_input(args.assignment),
                        task=task,
                        route=route,
                        parent_grant=_json_input(args.parent_grant),
                    )
                    admitted = ledger.host_run_admission(
                        project_id, observation["run_handle"]
                    )
                    if admitted["provider"] != route.get("provider", "codex"):
                        raise ContractError("host run admission differs from its route")
                    trusted_now_ms = time.time_ns() // 1_000_000
                    committed = ledger.commit_host_result(
                        project_id,
                        assignment=assignment,
                        task=task,
                        route=route,
                        result=result,
                        observation=observation,
                        execution_configuration=route["execution_configuration"],
                        now_ms=trusted_now_ms,
                    )
                    report = {"ready": True, **committed}
                    report["learning_cycle"] = ledger.run_learning_cycle(
                        project_id, as_of_ms=trusted_now_ms
                    )
                    _print(report)
                    return 0
                policy = load_policy(
                    ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                )
                if args.fallback_receipt is not None:
                    raise ContractError(
                        "fallback receipts require a trusted host run handle"
                    )
                route = route_task(
                    task, policy, primary_provider=_host_primary_provider()
                )
                current_configuration = installed_execution_configuration(
                    BUNDLE_ROOT,
                    route,
                    client_capability=clients[client_version],
                )
                task_slice = routing_task_slice(task)
                route["execution_configuration"] = current_configuration
                if args.evaluation_assignment_digest is not None:
                    evaluation_assignment = ledger.record_by_digest(
                        project_id,
                        args.evaluation_assignment_digest,
                        "evaluation_assignment",
                    )
                    planned = [
                        item for item in evaluation_assignment["trial_plan"]
                        if item["trial_id"] == args.trial_id
                    ]
                    if len(planned) != 1:
                        raise ContractError("trial is not in the evaluation assignment")
                    case = ledger.evaluation_case(project_id, planned[0]["case_id"])
                    candidate, _ = ledger.lifecycle_records(
                        project_id, evaluation_assignment["candidate_digest"]
                    )
                    validate_runtime_candidate(candidate, policy)
                    if (
                        case["task_slice"] != task_slice
                        or task["id"] != case["record_id"]
                        or case["fixture_digest"]
                        != runtime_fixture_digest(ledger.state.digest_key(), task)
                        or evaluation_assignment["client_digest"]
                        != content_digest(clients[client_version])
                        or candidate["champion_configuration"] != current_configuration
                    ):
                        raise ContractError(
                            "evaluation trial does not match installed task and client"
                        )
                    selected = (
                        candidate["candidate_configuration"]
                        if planned[0]["cohort"] == "candidate"
                        else candidate["champion_configuration"]
                    )
                    selected_reference = (
                        candidate["candidate_digest"]
                        if planned[0]["cohort"] == "candidate"
                        else candidate["base_digest"]
                    )
                    if planned[0]["configuration_digest"] != selected_reference:
                        raise ContractError("evaluation trial configuration is invalid")
                    expected_role = policy["roles"].get(task["family"])
                    if selected["role"] != expected_role:
                        raise ContractError("evaluation trial role differs from policy")
                    verifier = (
                        {
                            "role": policy["verification"]["role"],
                            "model": policy["verification"]["model"],
                            "effort": policy["verification"]["effort"],
                            "sandbox_mode": "read-only",
                        }
                        if expected_role != "codex_agents_verification_worker" else None
                    )
                    if (
                        selected["verification_required"]
                        != (expected_role != "codex_agents_verification_worker")
                        or selected["verifier_digest"]
                        != (content_digest(verifier) if verifier is not None else None)
                        or selected["bounds_digest"] != content_digest(route["bounds"])
                    ):
                        raise ContractError(
                            "evaluation execution controls differ from installed policy"
                        )
                    route.update(
                        model=selected["model"],
                        effort=selected["effort"],
                        role=expected_role,
                        topology=selected["topology"],
                        max_parallel=selected["max_parallel"],
                        verification_required=(
                            expected_role != "codex_agents_verification_worker"
                        ),
                        verifier=verifier,
                        bounds=dict(policy["bounds"]),
                        execution_configuration=selected,
                    )
                else:
                    released = load_released_candidate_digests(
                        RELEASES_PATH,
                        verify_signature=lambda payload, signature: OwnerGrantAuthority(
                            ledger.state.owner_allowed_signers()
                        ).verify_release_signature(payload, signature),
                    )
                    for candidate in ledger.routing_candidates(
                        project_id,
                        model_selection_slice(task),
                        released_candidate_digests=released,
                    ):
                        route = apply_candidate_to_route(
                            route, candidate, route["execution_configuration"], policy
                        )
                assignment = validate_assignment(
                    _json_input(args.assignment),
                    task=task,
                    route=route,
                    parent_grant=_json_input(args.parent_grant),
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                committed = ledger.commit_host_result(
                    project_id,
                    assignment=assignment,
                    task=task,
                    route=route,
                    result=result,
                    observation=observation,
                    execution_configuration=route["execution_configuration"],
                    now_ms=trusted_now_ms,
                )
                report = {
                    "ready": True,
                    **committed,
                }
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=trusted_now_ms
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-run-operational-checks":
            ledger, project_id = _learning_ledger(args)
            try:
                ledger.validate_operational_qa_scope(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                )
                check_report = run_operational_check_suite(
                    args.project_root, scratch_root=ledger.state.root
                )
                receipt = ledger.issue_operational_check_receipt(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                    check_report=check_report,
                )
                report = {
                    "ready": True,
                    **receipt,
                    "check_report": check_report,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-operational-qa":
            ledger, project_id = _learning_ledger(args)
            try:
                committed = ledger.commit_operational_qa(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                    evaluator_assignment=_json_input(args.evaluator_assignment),
                    evaluator_result=_json_input(args.evaluator_result),
                    check_receipt=args.check_receipt,
                    verifier_assignment=_json_input(args.verifier_assignment),
                    verifier_result=_json_input(args.verifier_result),
                )
                report = {"ready": True, **committed}
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=time.time_ns() // 1_000_000
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-verified-outcome":
            ledger, project_id = _learning_ledger(args)
            try:
                inputs = {
                    "evaluation_assignment_digest": args.evaluation_assignment_digest,
                    "host_outcome_digest": args.host_outcome_digest,
                    "verifier_host_outcome_digest": args.verifier_host_outcome_digest,
                    "evaluator_assignment": _json_input(args.evaluator_assignment),
                    "evaluator_result": _json_input(args.evaluator_result),
                    "check_report": _bounded_json_input(args.check_report),
                    "verifier_assignment": _json_input(args.verifier_assignment),
                    "verifier_result": _json_input(args.verifier_result),
                }
                if args.signature is None:
                    qa, outcome = ledger.prepare_verified_evaluation_outcome(
                        project_id,
                        **inputs,
                    )
                    authority = OwnerGrantAuthority(
                        ledger.state.owner_allowed_signers()
                    )
                    report = {
                        "ready": True,
                        "qa_evidence": qa,
                        "outcome": outcome,
                        "signing_payload_b64": base64.b64encode(
                            authority.qa_attestation_payload(project_id, qa)
                        ).decode("ascii"),
                        "namespace": "codex-agents-owner-v1",
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_verified_evaluation_outcome(
                            project_id,
                            **inputs,
                            signature=args.signature.read_bytes(),
                        ),
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-expire":
            ledger, project_id = _learning_ledger(args)
            try:
                trusted_now_ms = time.time_ns() // 1_000_000
                if args.now_ms is not None and args.now_ms > trusted_now_ms:
                    raise ContractError("retention time cannot be in the future")
                report = {
                    "ready": True,
                    "expired_records": ledger.expire(
                        project_id,
                        now_ms=args.now_ms if args.now_ms is not None else trusted_now_ms,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-run-cycle":
            if args.max_age_ms < 1:
                raise ContractError("max age must be positive")
            ledger, project_id = _learning_ledger(args)
            try:
                trusted_now_ms = time.time_ns() // 1_000_000
                report = {
                    "ready": True,
                    **ledger.run_learning_cycle(
                        project_id,
                        as_of_ms=trusted_now_ms,
                        max_age_ms=args.max_age_ms,
                    ),
                    "expired_records": ledger.expire(
                        project_id, now_ms=trusted_now_ms
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-create-evaluation-assignment":
            case_ids = _json_input(args.case_ids)
            if not isinstance(case_ids, list) or not all(
                isinstance(item, str) for item in case_ids
            ):
                raise ContractError("evaluation case ids must be an array of identifiers")
            ledger, project_id = _learning_ledger(args)
            try:
                candidate, _ = ledger.lifecycle_records(
                    project_id, args.candidate_digest
                )
                validate_runtime_candidate(
                    candidate,
                    load_policy(
                        ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                    ),
                )
                cases = [ledger.evaluation_case(project_id, case_id) for case_id in case_ids]
                clients = load_client_capabilities(
                    ORCHESTRATION_ROOT / "config" / "learning-clients.json"
                )
                if args.client_version not in clients:
                    raise ContractError(
                        "client version is not in the validated capability matrix"
                    )
                record = create_evaluation_assignment(
                    candidate=candidate,
                    cases=cases,
                    client_version=args.client_version,
                    client_capability=clients[args.client_version],
                    observed_at_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, record, producer="evaluator",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-trial":
            ledger, project_id = _learning_ledger(args)
            try:
                assignment_record = ledger.record_by_digest(
                    project_id,
                    args.evaluation_assignment_digest,
                    "evaluation_assignment",
                )
                planned = [
                    item for item in assignment_record["trial_plan"]
                    if item["trial_id"] == args.trial_id
                ]
                if len(planned) != 1:
                    raise ContractError("trial is not in the evaluation assignment")
                case = ledger.evaluation_case(project_id, planned[0]["case_id"])
                outcome = ledger.record_by_digest(
                    project_id, args.outcome_digest, "outcome"
                )
                record = build_evaluation_trial(
                    case=case,
                    outcome=outcome,
                    assignment=assignment_record,
                    trial_id=args.trial_id,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-evidence-review":
            subjects = _json_input(args.subject_digests)
            if not isinstance(subjects, list) or not all(
                isinstance(item, str) for item in subjects
            ):
                raise ContractError("review subjects must be an array of digests")
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.commit_evidence_review(
                        project_id, subject_digests=subjects
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-comparison":
            ledger, project_id = _learning_ledger(args)
            try:
                assignment_record = ledger.record_by_digest(
                    project_id,
                    args.evaluation_assignment_digest,
                    "evaluation_assignment",
                )
                candidate, _ = ledger.lifecycle_records(
                    project_id, assignment_record["candidate_digest"]
                )
                cases = [
                    ledger.evaluation_case(project_id, case_id)
                    for case_id in assignment_record["case_ids"]
                ]
                with ledger.repository(project_id) as repository:
                    trials = [
                        {
                            key: value for key, value in record.items()
                            if not key.startswith("_")
                        }
                        for record in repository.records("evaluation_trial")
                        if record["evaluation_assignment_digest"]
                        == args.evaluation_assignment_digest
                    ]
                calibration = (
                    ledger.record_by_digest(
                        project_id,
                        args.grader_calibration_digest,
                        "grader_calibration",
                    )
                    if args.grader_calibration_digest else None
                )
                clients = load_client_capabilities(
                    ORCHESTRATION_ROOT / "config" / "learning-clients.json"
                )
                client_version = assignment_record["client_version"]
                record = compare_candidate(
                    candidate=candidate,
                    trials=trials,
                    cases=cases,
                    client_version=client_version,
                    client_capability=clients[client_version],
                    independent_review_digest=args.independent_review_digest,
                    evaluation_assignment_digest=args.evaluation_assignment_digest,
                    grader_calibration=calibration,
                    subjective_grading_required=args.subjective_grading_required,
                )
                report = {
                    "ready": True,
                    "decision_ready": record["decision"] == "promote",
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-mine-case":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcome = repository.record(args.source_outcome_id, "outcome")
                record = mine_evaluation_case(
                    digest_key=ledger.state.digest_key(),
                    outcome=outcome,
                    fixture_digest=runtime_fixture_digest(
                        ledger.state.digest_key(), _json_input(args.task)
                    ),
                    criteria_digest=args.criteria_digest,
                    lineage_digest=args.lineage_digest,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-review-case":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    source_case = repository.record(args.case_id, "evaluation_case")
                record = review_evaluation_case(
                    source_case,
                    digest_key=ledger.state.digest_key(),
                    review_digest=args.review_digest,
                    similarity_group_digest=args.similarity_group_digest,
                    destination=args.destination,
                    freeze=args.freeze,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=time.time_ns() // 1_000_000
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-analyze":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcomes = repository.records("outcome")
                    feedback = repository.records("feedback")
                clean = [
                    {key: value for key, value in record.items() if not key.startswith("_")}
                    for record in outcomes
                ]
                invalidated = {
                    record["run_id"] for record in feedback
                    if record["label"] in {"rejected", "corrected"}
                }
                report = {
                    "ready": True,
                    "analysis": analyze_outcomes(
                        clean,
                        as_of_ms=args.as_of_ms,
                        max_age_ms=args.max_age_ms,
                        owner_invalidated_run_ids=invalidated,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-generate-candidate":
            spec = _bounded_json_input(args.spec)
            required = {
                "scope", "task_slice", "target", "effect", "base_digest",
                "change_manifest", "champion_configuration",
            }
            if set(spec) != required:
                raise ContractError("candidate spec must use the closed field set")
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcomes = repository.records("outcome")
                    feedback = repository.records("feedback")
                invalidated = {
                    record["run_id"] for record in feedback
                    if record["label"] in {"rejected", "corrected"}
                }
                provisional = generate_candidate(
                    digest_key=ledger.state.digest_key(),
                    **spec,
                    evidence_digests=[content_digest(spec)],
                )
                configuration_fields = tuple(
                    provisional["champion_configuration"].keys()
                )

                def execution_configuration(record: dict[str, Any]) -> dict[str, Any]:
                    return {field: record[field] for field in configuration_fields}

                clean = [
                    {key: value for key, value in record.items() if not key.startswith("_")}
                    for record in outcomes
                    if record["task_slice"] == spec["task_slice"]
                    and record["run_id"] not in invalidated
                    and record["accepted"] is True
                    and record.get("execution_state") == "completed"
                    and record.get("admission_state") == "accepted"
                    and record.get("semantic_verdict")
                    in {"pass", "ready", "ready-with-corrections"}
                    and record.get("retry_attempt") is not None
                    and record["verification_required"] is True
                    and record["verifier_digest"] is not None
                    and record["verifier_disagreement"] is False
                    and record["deterministic_pass"] is True
                    and record["security_pass"] is True
                    and record["required_criteria_passed"]
                    == record["required_criteria_total"]
                    and record["coverage"] == "end_to_end_complete"
                ]
                if any(
                    any(
                        record["usage"][field] is None
                        or record.get("usage_sources", {}).get(field, {}).get("source")
                        == "unavailable"
                        or record.get("usage_sources", {}).get(field, {}).get("coverage")
                        != "end_to_end_complete"
                        for field in {
                            "input_tokens", "cached_tokens", "output_tokens",
                            "reasoning_tokens", "model_calls", "tool_calls",
                            "latency_ms",
                        }
                    )
                    for record in clean
                ):
                    raise ContractError(
                        "candidate requires complete authoritative workflow telemetry"
                    )
                champion_by_fixture: dict[str, list[dict[str, Any]]] = {}
                challenger_by_fixture: dict[str, list[dict[str, Any]]] = {}
                for outcome in clean:
                    configuration = execution_configuration(outcome)
                    cohort = None
                    if configuration == provisional["champion_configuration"]:
                        cohort = champion_by_fixture
                    elif configuration == provisional["candidate_configuration"]:
                        cohort = challenger_by_fixture
                    if cohort is not None:
                        cohort.setdefault(outcome["fixture_digest"], []).append(outcome)
                fixtures = sorted(
                    champion_by_fixture.keys() & challenger_by_fixture.keys()
                )[-32:]
                if len(fixtures) < 20:
                    raise ContractError(
                        "candidate requires at least 20 exact matched champion/candidate fixtures"
                    )
                selected: list[dict[str, Any]] = []
                for fixture in fixtures:
                    selected.extend(
                        (
                            champion_by_fixture[fixture][-1],
                            challenger_by_fixture[fixture][-1],
                        )
                    )
                if len({item["assignment_id"] for item in selected}) != len(selected):
                    raise ContractError(
                        "candidate requires 40 distinct retry-deduplicated outcomes"
                    )
                as_of_ms = max(record["observed_at_ms"] for record in selected)
                analysis = analyze_outcomes(
                    selected,
                    as_of_ms=as_of_ms,
                    max_age_ms=2**63 - 1,
                    owner_invalidated_run_ids=invalidated,
                )
                evidence_digests = sorted(content_digest(item) for item in selected)
                record = generate_candidate(
                    digest_key=ledger.state.digest_key(),
                    **spec,
                    evidence_digests=evidence_digests,
                    eligibility_receipt={
                        "task_slice": spec["task_slice"],
                        "evidence_set_digest": content_digest(evidence_digests),
                        "independent_case_count": len(fixtures),
                        "usage_complete": True,
                        "version_unconfounded": True,
                        "task_mix_matched": True,
                        "retry_deduplicated": True,
                        "verifier_evidence_complete": True,
                    },
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                    "analysis": analysis,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-calibrate-grader":
            labels = _json_input(args.labels)
            configuration = _json_input(args.configuration)
            if not isinstance(labels, list) or not isinstance(configuration, dict):
                raise ContractError("grader inputs must be an array and an object")
            ledger, project_id = _learning_ledger(args)
            try:
                record = calibrate_grader(
                    labels,
                    configuration=configuration,
                    observed_at_ms=args.observed_at_ms,
                )
                authority = OwnerGrantAuthority(ledger.state.owner_allowed_signers())
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": record["ready"],
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-feedback":
            correction = (
                args.correction_file.read_bytes() if args.correction_file is not None else None
            )
            ledger, project_id = _learning_ledger(args)
            try:
                record = build_feedback(
                    digest_key=ledger.state.digest_key(),
                    run_id=args.run_id,
                    label=args.label,
                    reason_code=args.reason_code,
                    correction=correction,
                    observed_at_ms=args.observed_at_ms,
                )
                authority = OwnerGrantAuthority(ledger.state.owner_allowed_signers())
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-evaluate-candidate":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                comparison = ledger.record_by_digest(
                    project_id, args.comparison_digest, "comparison"
                )
                transition = lifecycle.mark_evaluated(
                    comparison,
                    evidence_digest=args.comparison_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-start-canary":
            case_digests = _json_input(args.case_digests)
            if not isinstance(case_digests, list):
                raise ContractError("canary case digests must be an array")
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                transition = lifecycle.start_canary(
                    grant=_bounded_json_input(args.grant),
                    case_digests=case_digests,
                    evidence_digest=args.comparison_digest,
                    now_ms=trusted_now_ms,
                    commit=lambda grant, promotion: ledger.commit_granted_promotion(
                        project_id,
                        grant=grant,
                        promotion=promotion,
                        now_ms=trusted_now_ms,
                    ),
                )
                report = {"ready": True, "record": transition}
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-complete-canary":
            case_results = _json_input(args.case_results)
            if not isinstance(case_results, list):
                raise ContractError("canary case results must be an array")
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                record = build_canary_report(
                    candidate_digest=lifecycle.candidate["candidate_digest"],
                    champion_digest=lifecycle.champion_digest,
                    case_results=case_results,
                    comparison_digest=args.comparison_digest,
                    independent_review_digest=args.independent_review_digest,
                    observed_at_ms=args.observed_at_ms,
                )
                ledger.append(
                    project_id, record, producer="evaluator", now_ms=args.observed_at_ms
                )
                transition = lifecycle.complete_canary(
                    report=record, now_ms=args.observed_at_ms
                )
                ledger.append(
                    project_id,
                    transition,
                    producer="lifecycle",
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": transition["action"] == "canary_passed",
                    "canary_report": record,
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-promote":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                transition = lifecycle.promote(
                    grant=_bounded_json_input(args.grant),
                    evidence_digest=args.canary_report_digest,
                    now_ms=trusted_now_ms,
                    commit=lambda grant, promotion: ledger.commit_granted_promotion(
                        project_id,
                        grant=grant,
                        promotion=promotion,
                        now_ms=trusted_now_ms,
                    ),
                )
                report = {"ready": True, "record": transition}
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-drift":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                comparison = ledger.record_by_digest(
                    project_id, args.comparison_digest, "comparison"
                )
                transition = lifecycle.record_drift(
                    comparison=comparison,
                    evidence_digest=args.comparison_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-rollback":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                transition = lifecycle.rollback(
                    active_digest=args.candidate_digest,
                    evidence_digest=args.evidence_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-configure-owner-key":
            state = LearningState(args.state_root)
            path = state.install_owner_public_key(args.public_key.read_text())
            report = {"ready": True, "allowed_signers": path.name}
        elif args.command == "learning-create-grant-request":
            ledger, project_id = _learning_ledger(args)
            try:
                authority = OwnerGrantAuthority(ledger.state.owner_allowed_signers())
                request = authority.request(
                    digest_key=ledger.state.digest_key(),
                    kind=args.kind,
                    project_id=project_id,
                    candidate_digest_value=args.candidate_digest,
                    champion_digest=args.champion_digest,
                    review_digest=args.review_digest,
                    bounds_digest=args.bounds_digest,
                    issued_at_ms=args.issued_at_ms,
                    expires_at_ms=args.expires_at_ms,
                )
                report = {
                    "ready": True,
                    "request": request,
                    "signing_payload_b64": base64.b64encode(
                        authority.signing_payload(request)
                    ).decode("ascii"),
                    "namespace": "codex-agents-owner-v1",
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-attach-grant-signature":
            state = LearningState(args.state_root, create=False)
            authority = OwnerGrantAuthority(state.owner_allowed_signers())
            request = _bounded_json_input(args.request)
            record = authority.attach_signature(
                request, args.signature.read_bytes()
            )
            report = {"ready": True, "record": record}
        elif args.command == "host-command":
            try:
                status = codex_rule_status(args.project_root)
            except CodexHomeUnsupported as exc:
                # The rule and its copy live only in the real ~/.codex.
                report = {
                    "ready": False, "argv_prefix": None, "codex_rule": "unsupported",
                    "reason": str(exc), "rule_file": None, "runtime_copy": None,
                }
            else:
                try:
                    # The rule runs the protected copy, never the project's own CLI.
                    prefix = codex_argv_prefix(args.project_root)
                    runtime_copy = str(runtime_copy_root(args.project_root))
                except HostCommandError:
                    # No install manifest: nothing to copy, so the rule is stale.
                    prefix = runtime_copy = None
                clone = None
                if runtime_copy is not None:
                    try:
                        clone = Path(
                            runtime_binding(Path(runtime_copy) / ".codex" / "orchestration")
                            ["clone_root"]
                        )
                    except (HostCommandError, TypeError):
                        clone = None  # no copy yet: nothing recorded to protect
                report = {
                    "ready": status == "installed",
                    "argv_prefix": prefix,
                    "codex_rule": status,
                    "rule_file": str(codex_rule_path(args.project_root)),
                    "runtime_copy": runtime_copy,
                    # Whether this session would pass the guard the copy applies.
                    "session_guard": session_guard_status(
                        Path(runtime_copy) if runtime_copy is not None else None, clone,
                        Path(os.path.abspath(args.project_root)),
                    ),
                }
        elif args.command == "install-codex-rule":
            rule_file = str(codex_rule_path(args.project_root))
            if args.print_only:
                report = {
                    "ready": True,
                    "rule_file": rule_file,
                    "rule_text": render_codex_rule(args.project_root),
                    "runtime_copy": str(runtime_copy_root(args.project_root)),
                }
            else:
                installed = install_codex_rule(
                    args.project_root, replace=args.replace,
                    clone_root=installer_root(ORCHESTRATION_ROOT),
                )
                report = {
                    "ready": installed["codex_rule"] != "differs",
                    **installed,
                    "rule_file": rule_file,
                }
        elif args.command == "remove-codex-rule":
            report = {"ready": True, "removed": remove_codex_rule(args.project_root)}
        elif args.command == "codex-session-scan":
            report = {
                "ready": True,
                "warnings": scan_recent_sessions(
                    args.clone_root,
                    configured_codex_home(),
                    Path(os.environ.get("HOME") or Path.home()),
                    projects=registered_project_roots(),
                ),
            }
        else:  # pragma: no cover - argparse restricts commands.
            raise ContractError("unsupported command")
    except OSError:
        _print(_with_codex_hint(
            args.command, {"ready": False, "error": "filesystem operation failed"}
        ))
        return 2
    except HostClientMismatch:
        _print({
            "ready": False,
            "error": "host client does not own this lifecycle",
            "reason_code": "host_client_mismatch",
        })
        return 2
    except ClaudeHostDenied as exc:
        _print({
            "ready": False,
            "error": "provider admission denied",
            "reason_code": exc.reason_code,
        })
        return 2
    except HostClientUnknown:
        _print({
            "ready": False,
            "error": "host client is unknown",
            "reason_code": "host_client_unknown",
        })
        return 2
    except BridgeRefused as exc:
        # Refusal messages are fixed strings; they never carry prompts or paths.
        _print({"ready": False, "error": "dispatch refused", "reason": str(exc)})
        return 2
    except AdmissionDenied as exc:
        _print({
            "ready": False,
            "error": "provider admission denied",
            "reason_code": exc.reason_code,
        })
        return 2
    except ContractError:
        _print({"ready": False, "error": "input validation failed"})
        return 2
    except (
        CatalogError,
        CollectorError,
        HostCommandError,
        LedgerError,
        PolicyError,
        StateError,
        json.JSONDecodeError,
    ) as exc:
        _print(_with_codex_hint(args.command, {"ready": False, "error": str(exc)}))
        return 2
    _print(report)
    successful = report.get("ready", True)
    if args.command == "validate" and args.package_only:
        successful = report.get("package_ready", False)
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
