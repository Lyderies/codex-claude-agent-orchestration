#!/usr/bin/env python3
"""Run one permission-free Codex turn and report only normalized telemetry facts."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_ROOT = Path(__file__).resolve().parent
ORCHESTRATION_ROOT = REPOSITORY_ROOT / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))
sys.path.insert(0, str(SCRIPT_ROOT))

from check_app_server import _cleanup_process  # noqa: E402
from codex_agents.contracts import ContractError  # noqa: E402
from codex_agents.learning_adapters import normalize_app_server, reconcile_run  # noqa: E402


class LiveLearningError(RuntimeError):
    """Controlled live-check failure that never includes model content."""


REQUIRED_CODES = {
    "turn_started",
    "item_completed",
    "usage_snapshot",
    "turn_completed",
}


def normalize_messages(messages: list[dict[str, Any]], digest_key: bytes) -> dict[str, Any]:
    records = []
    for message in messages:
        record = normalize_app_server(
            message,
            digest_key=digest_key,
            client_version="codex-cli-0.153.0",
            coverage="root_turn_complete",
            observed_at_ms=1,
        )
        if record is not None:
            records.append(record)
    if not records:
        raise LiveLearningError("no_supported_events")
    run_ids = {record["run_id"] for record in records}
    if len(run_ids) != 1:
        raise LiveLearningError("multiple_runs")
    reconciled = reconcile_run(
        [{"source": "app_server", "record": record} for record in records],
        capability={"app_server_coverage": "root_turn_complete"},
    )
    codes = sorted({record["event_code"] for record in records})
    usage = reconciled["usage"] or {}
    return {
        "ready": REQUIRED_CODES.issubset(codes)
        and reconciled["coverage"] == "root_turn_complete",
        "client": "codex-cli-0.153.0",
        "event_codes": codes,
        "fixture_event_classes_matched": REQUIRED_CODES.issubset(codes),
        "coverage": reconciled["coverage"],
        "eligible_for_efficiency": reconciled["eligible_for_efficiency"],
        "terminal_status": reconciled["terminal_status"],
        "usage_fields_present": sorted(
            key for key, value in usage.items() if value is not None
        ),
        "child_usage_observed": False,
        "raw_content_persisted": False,
    }


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    process.stdin.flush()


def check(target: Path, *, timeout: float = 90.0) -> dict[str, Any]:
    target = target.resolve()
    process: subprocess.Popen[bytes] = subprocess.Popen(
        ["codex", "app-server", "--strict-config", "--listen", "stdio://"],
        cwd=target,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector: selectors.BaseSelector | None = None
    messages: list[dict[str, Any]] = []
    responses: dict[int, dict[str, Any]] = {}
    stdout_buffer = b""
    turn_sent = False
    completed_at: float | None = None
    try:
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        _send(
            process,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex_agents_learning_check",
                        "title": "codex-agents learning verifier",
                        "version": "0.2.1",
                    }
                },
            },
        )
        _send(process, {"method": "initialized", "params": {}})
        _send(
            process,
            {
                "method": "thread/start",
                "id": 1,
                "params": {
                    "cwd": str(target),
                    "model": "gpt-6-luna",
                    "ephemeral": True,
                },
            },
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if completed_at is not None and time.monotonic() - completed_at >= 1.0:
                break
            events = selector.select(0.25)
            if not events:
                if process.poll() is not None:
                    raise LiveLearningError("app_server_stopped")
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    continue
                stdout_buffer += chunk
                if len(stdout_buffer) > 2_000_000:
                    raise LiveLearningError("app_server_message_oversized")
                while b"\n" in stdout_buffer:
                    raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    if not raw_line.strip():
                        continue
                    message = json.loads(raw_line)
                    if not isinstance(message, dict):
                        raise LiveLearningError("app_server_message_invalid")
                    identifier = message.get("id")
                    if isinstance(identifier, int) and ("result" in message or "error" in message):
                        responses[identifier] = message
                    if isinstance(identifier, int) and "method" in message:
                        raise LiveLearningError("unexpected_server_request")
                    if message.get("method") in {
                        "turn/started",
                        "turn/completed",
                        "item/completed",
                        "model/rerouted",
                        "thread/tokenUsage/updated",
                    }:
                        messages.append(message)
                        if message.get("method") == "turn/completed":
                            completed_at = time.monotonic()
            if not turn_sent and 0 in responses and 1 in responses:
                if "error" in responses[0] or "error" in responses[1]:
                    raise LiveLearningError("initialization_failed")
                thread_id = responses[1].get("result", {}).get("thread", {}).get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise LiveLearningError("thread_id_missing")
                _send(
                    process,
                    {
                        "method": "turn/start",
                        "id": 2,
                        "params": {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": "Reply with exactly READY. Do not call tools.",
                                }
                            ],
                            "model": "gpt-6-luna",
                            "effort": "medium",
                            "approvalPolicy": "never",
                            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                        },
                    },
                )
                turn_sent = True
        if completed_at is None:
            raise LiveLearningError("turn_did_not_complete")
        if 2 in responses and "error" in responses[2]:
            raise LiveLearningError("turn_start_failed")
        return normalize_messages(messages, b"live-check-digest-key".ljust(32, b"0"))
    except (json.JSONDecodeError, ContractError) as exc:
        raise LiveLearningError("normalization_failed") from exc
    finally:
        _cleanup_process(process, selector)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    try:
        report = check(args.target, timeout=args.timeout)
    except (LiveLearningError, OSError) as exc:
        report = {"ready": False, "error": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
