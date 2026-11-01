#!/usr/bin/env python3
"""Observational Codex hook: normalize metadata, enqueue it, and never steer.

Codex runs it outside the sandbox through the checkout's rendered `.codex/hooks.json`
(`codex_agents.hook_config`): the installer's trusted interpreter, isolated, this file
by its absolute path, and `--checkout` naming the checkout it was rendered for. Nothing
here looks up a program on PATH or runs git; the only child is the collector, started
with this same interpreter (`sys.executable`), a fixed environment and `xcrun_nocache=1`.

Codex 0.164 loads a linked worktree's hooks from its main checkout, so this file also
runs for events from worktrees (and from repositories nested inside the checkout). Its
first action, with the standard library only and before any of the package is
imported, is to check that the event's working folder belongs to `--checkout`; any
other event is a silent no-op that imports nothing and starts nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
MAX_INPUT_BYTES = 81_920
QUIET_JSON_EVENTS = {"Stop", "SubagentStop"}


def belongs_to_checkout(cwd: str, checkout: str) -> bool:
    """Whether the working folder `cwd` is the checkout `checkout` or one of its folders.

    Both must exist and are resolved strictly; `cwd` must be the checkout or lie inside
    it, and the nearest `.git` entry (a folder or a file) walking up from `cwd` must be the
    checkout's own: a linked worktree, a clone or a repository nested inside the checkout
    has its own `.git` first. Only a missing `.git` counts as absent; any other error (a
    folder whose search permission was removed, say) refuses, so an inaccessible boundary
    is never walked past.
    """

    if not os.path.isabs(cwd) or not os.path.isabs(checkout):
        return False
    try:
        root = os.path.realpath(checkout, strict=True)
        current = os.path.realpath(cwd, strict=True)
    except (OSError, ValueError):
        return False
    if current != root and not current.startswith(root.rstrip("/") + "/"):
        return False
    while True:
        try:
            os.lstat(os.path.join(current, ".git"))
        except FileNotFoundError:
            found = False
        except (OSError, ValueError):
            return False
        else:
            found = True
        if found:
            return current == root
        if current == root:
            return False
        current = os.path.dirname(current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--checkout")
    parser.add_argument("--client-version", required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        payload = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        payload = None
    quiet_json = isinstance(payload, dict) and payload.get("hook_event_name") in QUIET_JSON_EVENTS
    project_root = None
    if args.checkout is not None:
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if not isinstance(cwd, str) or not belongs_to_checkout(cwd, args.checkout):
            if quiet_json:
                print("{}")
            return 0
        # A session in a subfolder records for the checkout it belongs to.
        project_root = Path(os.path.realpath(args.checkout))
    _record(args, raw, project_root)
    if quiet_json:
        print("{}")
    return 0


def _record(args: argparse.Namespace, raw: bytes, checkout_root: Path | None) -> None:
    """Normalize the event and hand it to the collector; every failure stays quiet."""

    sys.path.insert(0, str(ORCHESTRATION_ROOT))
    from codex_agents.contracts import ContractError
    from codex_agents.host_paths import HostPathError
    from codex_agents.learning_adapters import normalize_hook
    from codex_agents.learning_contracts import parse_bounded_json
    from codex_agents.learning_store import (
        CollectorClient,
        CollectorError,
        LearningState,
        StateError,
        client_health_clear,
        client_health_increment,
        ensure_collector,
    )

    admission_failure = False
    try:
        if len(raw) > MAX_INPUT_BYTES:
            raise ContractError("hook input is oversized")
        payload = parse_bounded_json(raw)
        if not isinstance(payload, dict):
            raise ContractError("hook payload must be an object")
        state = LearningState(args.state_root, create=False)
        session = payload.get("session_id")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str):
            raise ContractError("hook binding fields are invalid")
        project_root = checkout_root if checkout_root is not None else Path(cwd)
        try:
            record = normalize_hook(
                payload,
                digest_key=state.digest_key(),
                client_version=args.client_version,
            )
            if not isinstance(session, str):
                raise ContractError("hook binding fields are invalid")
        except (ContractError, StateError, OSError, json.JSONDecodeError):
            client_health_increment(
                state, project_root, "hook_normalization_rejected"
            )
            raise
        ensure_collector(
            state,
            ORCHESTRATION_ROOT / "cli.py",
            project_root=project_root,
        )
        client = CollectorClient(state.socket)
        try:
            binding = client.bind(project_root, session, "hook", args.client_version)
        except CollectorError as exc:
            admission_failure = True
            if exc.code == "project_unregistered":
                client_health_clear(state, project_root)
            else:
                client_health_increment(state, project_root, "hook_binding_rejected")
            raise
        try:
            client.ingest(binding, record)
        except CollectorError:
            admission_failure = True
            client_health_increment(state, project_root, "hook_ingest_rejected")
            raise
    except CollectorError as exc:
        # The stage-specific counters above distinguish admission failure. Collector
        # startup and transport failures remain visible under collector_unavailable.
        if (
            not admission_failure
            and "state" in locals()
            and "project_root" in locals()
            and exc.code != "project_unregistered"
        ):
            try:
                client_health_increment(state, project_root, "collector_unavailable")
            except (StateError, OSError):
                pass
    except (ContractError, StateError, HostPathError, OSError, json.JSONDecodeError):
        # Hook input stays out of storage unless it passes the closed normalizer. An
        # unusable state location (HostPathError) is as quiet as an unregistered project.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
