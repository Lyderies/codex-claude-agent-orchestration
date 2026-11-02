"""Pure offline checks. Success validates fixtures and never permits execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    READ_TOOLS, ROLES, Assignment, BillingObservation, ParentGrant, Task,
    VersionObservation, _record, parse_assignment, parse_billing_observation,
    parse_parent_grant, parse_task, parse_version_observation, reject, relative_path,
)


@dataclass(frozen=True, slots=True)
class FixtureReadiness:
    task: Task
    parent: ParentGrant
    assignment: Assignment
    version: VersionObservation
    billing: BillingObservation
    runtime_enabled: bool = field(default=False, init=False)


def assess_fixture_readiness(value: Any) -> FixtureReadiness:
    """Synthetic structural readiness only: no real attestation or admission API."""
    d = _record(value, "schema_version task parent_grant assignment version billing")
    task = parse_task(d["task"])
    parent = parse_parent_grant(d["parent_grant"])
    assignment = parse_assignment(d["assignment"], task=task, parent=parent)
    version = parse_version_observation(d["version"])
    billing = parse_billing_observation(d["billing"])
    for observation in (version, billing):
        if (observation.assignment_id != assignment.assignment_id
                or observation.attempt_id != assignment.attempt_id):
            reject("observation_mismatch")
    if assignment.lifecycle_state != "pre-send":
        reject("attempt_not_pre_send")
    if (version.source != "synthetic" or version.channel != "latest"
            or version.client_version is None
            or version.client_version != version.latest_version
            or not version.fresh or not version.capabilities_verified):
        reject("version_unverified")
    if (billing.source != "synthetic" or billing.provider != "first_party"
            or billing.auth_method != "claude.ai"
            or billing.billing_mode != "subscription"
            or billing.usage_credits != "disabled"
            or billing.entitlement != "available" or not billing.fresh):
        reject("billing_unverified")
    return FixtureReadiness(task, parent, assignment, version, billing)


@dataclass(frozen=True, slots=True)
class NativeFixture:
    name: str
    description: str
    tools: tuple[str, ...]
    body: str
    runtime_enabled: bool = field(default=False, init=False)


_HEADER = re.compile(r"([a-z]+): ([A-Za-z0-9][A-Za-z0-9 ,._/-]{0,511})\Z")
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def parse_native_fixture(text: Any) -> NativeFixture:
    """Parse our three-key scalar YAML subset, not general YAML or native discovery.

    Model omission in this inert fixture does not establish native model isolation.
    Body content is untrusted text and cannot grant tools, permissions or admission.
    """
    if type(text) is not str or len(text) > 16384 or "\r" in text:
        reject("invalid_native_fixture")
    lines = text.splitlines()
    if (len(lines) < 6 or lines[0] != "---"
            or any(ord(c) < 32 and c != "\n" for c in text)):
        reject("invalid_native_fixture")
    try:
        boundary = lines.index("---", 1)
    except ValueError:
        reject("invalid_native_fixture")
    header = {}
    for line in lines[1:boundary]:
        match = _HEADER.fullmatch(line)
        if not match or match[1] in header:
            reject("invalid_native_header")
        header[match[1]] = match[2]
    if set(header) != {"name", "description", "tools"} or header["name"] not in ROLES:
        reject("invalid_native_header")
    tools = tuple(p.strip() for p in header["tools"].split(","))
    if (not tools or len(tools) != len(set(tools)) or not set(tools) <= READ_TOOLS):
        reject("native_tools_denied")
    body = "\n".join(lines[boundary + 1:]).strip()
    if not body:
        reject("invalid_native_fixture")
    for link in _LINK.findall(body):
        relative_path(link)
    return NativeFixture(header["name"], header["description"], tools, body)


def validate_native_inventory(texts: Any) -> tuple[NativeFixture, ...]:
    """Validate exactly one inert definition for each package role."""
    if type(texts) is not list or len(texts) != len(ROLES):
        reject("invalid_native_inventory")
    fixtures = tuple(parse_native_fixture(text) for text in texts)
    names = tuple(f.name for f in fixtures)
    if len(names) != len(set(names)) or set(names) != ROLES:
        reject("invalid_native_inventory")
    return fixtures
