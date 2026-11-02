#!/usr/bin/env python3
"""Verify a Codex app-server handshake and project thread initialization."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path


class AppServerError(RuntimeError):
    """Raised when app-server does not complete the expected handshake."""


def _stop_process_group(process: subprocess.Popen[bytes], timeout: float = 3.0) -> None:
    """Terminate the dedicated app-server process group and reap its leader."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)
    else:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cleanup_process(
    process: subprocess.Popen[bytes], selector: selectors.BaseSelector | None
) -> None:
    """Always stop the process group, then close each local resource independently."""

    try:
        _stop_process_group(process)
    finally:
        resources = (selector, process.stdin, process.stdout, process.stderr)
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except OSError:
                pass


def check(target: Path, timeout: float = 15.0) -> dict:
    resolved_target = target.resolve()
    process: subprocess.Popen[bytes] = subprocess.Popen(
        ["codex", "app-server", "--strict-config", "--listen", "stdio://"],
        cwd=resolved_target,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector: selectors.BaseSelector | None = None
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        messages = [
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex_agents_gate4",
                        "title": "codex-agents Gate 4 verifier",
                        "version": "0.1.0",
                    }
                },
            },
            {"method": "initialized", "params": {}},
            {
                "method": "thread/start",
                "id": 1,
                "params": {
                    "cwd": str(resolved_target),
                    "model": "gpt-6-luna",
                    "ephemeral": True,
                },
            },
        ]
        responses: dict[int, dict] = {}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout_buffer = b""
        stderr_tail = b""
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and set(responses) != {0, 1}:
            events = selector.select(0.25)
            if not events:
                if process.poll() is not None:
                    break
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_tail = (stderr_tail + chunk)[-8192:]
                    continue
                stdout_buffer += chunk
                while b"\n" in stdout_buffer:
                    raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    if not raw_line.strip():
                        continue
                    message = json.loads(raw_line)
                    if message.get("id") in {0, 1}:
                        responses[message["id"]] = message
        if set(responses) != {0, 1}:
            detail = stderr_tail.decode(errors="replace").strip()
            suffix = f"; stderr tail: {detail}" if detail else ""
            raise AppServerError(
                f"missing responses: {sorted({0, 1} - set(responses))}{suffix}"
            )
        for identifier, response in responses.items():
            if "error" in response:
                raise AppServerError(f"request {identifier} failed: {response['error']}")
        thread = responses[1]["result"]["thread"]
        return {
            "ready": True,
            "target": str(resolved_target),
            "user_agent": responses[0]["result"].get("userAgent"),
            "thread_id": thread.get("id"),
            "ephemeral": thread.get("ephemeral"),
            "model_provider": thread.get("modelProvider"),
            "instruction_sources": responses[1]["result"].get("instructionSources", []),
        }
    finally:
        _cleanup_process(process, selector)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    try:
        report = check(args.target, args.timeout)
    except (AppServerError, OSError, json.JSONDecodeError, KeyError) as exc:
        report = {"ready": False, "error": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
