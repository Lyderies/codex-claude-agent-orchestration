"""Detect which primary agent is driving the lifecycle.

Trust model: these environment markers are a consistency check against
accidental misuse, such as running the Codex lifecycle from a Claude session.
They are not proof of identity; any local process can set them. The authority
gates are elsewhere: provider admission, the committed bridge flag and the
read-only sandbox. A wrong host can only pick the other primary's routes, all
of which still pass those gates.
"""

from __future__ import annotations

from collections.abc import Mapping

from .contracts import ContractError

CODEX_HOST_CLIENT = "codex-host-v1"
CLAUDE_HOST_CLIENT = "claude-host-v1"
HOST_CLIENTS = (CODEX_HOST_CLIENT, CLAUDE_HOST_CLIENT)
# Set by Codex shells (verified live); CODEX_SANDBOX alone also appears in
# sandboxed tool runs, so the thread markers decide.
CODEX_THREAD_MARKERS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
# Set by Claude Code for its tools and children (verified live).
CLAUDE_MARKER = "CLAUDECODE"
CLAUDE_SESSION_MARKER = "CLAUDE_CODE_SESSION_ID"


class HostClientUnknown(ContractError):
    """Neither or both hosts are indicated, or an explicit host disagrees."""

    reason_code = "host_client_unknown"


def detect_host_client(environ: Mapping[str, str]) -> str:
    codex = any(environ.get(name) for name in CODEX_THREAD_MARKERS)
    claude_marker = bool(environ.get(CLAUDE_MARKER))
    claude = environ.get(CLAUDE_MARKER) == "1" and bool(environ.get(CLAUDE_SESSION_MARKER))
    if codex and not claude_marker:
        return CODEX_HOST_CLIENT
    if claude and not codex:
        return CLAUDE_HOST_CLIENT
    raise HostClientUnknown("host_client_unknown")


def resolve_host_client(environ: Mapping[str, str], explicit: str | None = None) -> str:
    """Return the detected host; an explicit value must agree with it."""

    detected = detect_host_client(environ)
    if explicit is not None and explicit != detected:
        raise HostClientUnknown("host_client_unknown")
    return detected
