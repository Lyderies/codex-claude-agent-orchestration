#!/usr/bin/env python3
"""Observational Codex hook: normalize metadata, enqueue it, and never steer.

Codex runs it outside the sandbox through the checkout's rendered `.codex/hooks.json`
(`codex_agents.hook_config`): the installer's trusted interpreter, isolated, and this
file by its absolute path. Nothing here looks up a program on PATH or runs git; the
only child is the collector, started with this same interpreter (`sys.executable`),
a fixed environment and `xcrun_nocache=1`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from codex_agents.contracts import ContractError  # noqa: E402
from codex_agents.host_paths import HostPathError  # noqa: E402
from codex_agents.learning_adapters import normalize_hook  # noqa: E402
from codex_agents.learning_contracts import parse_bounded_json  # noqa: E402
from codex_agents.learning_store import (  # noqa: E402
    CollectorClient,
    CollectorError,
    LearningState,
    StateError,
    client_health_clear,
    client_health_increment,
    ensure_collector,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--client-version", required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(81_921)
    quiet_json = False
    admission_failure = False
    try:
        if len(raw) > 81_920:
            raise ContractError("hook input is oversized")
        payload = parse_bounded_json(raw)
        quiet_json = isinstance(payload, dict) and payload.get("hook_event_name") in {
            "Stop",
            "SubagentStop",
        }
        if not isinstance(payload, dict):
            raise ContractError("hook payload must be an object")
        state = LearningState(args.state_root, create=False)
        session = payload.get("session_id")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str):
            raise ContractError("hook binding fields are invalid")
        project_root = Path(cwd)
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
    if quiet_json:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
