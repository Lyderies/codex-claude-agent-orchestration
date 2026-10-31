from __future__ import annotations

from pathlib import Path
from unittest import mock
import contextlib
import os
import subprocess
import sys

from codex_agents.learning_adapters import empty_usage
from codex_agents.learning_contracts import candidate_base_digest, content_digest
from codex_agents import host_paths, providers
from codex_agents.providers import billing_attestation_record
from codex_agents.learning_engine import (
    OWNER_SIGNATURE_NAMESPACE,
    OwnerGrantAuthority,
    build_evaluation_trial,
    create_evaluation_assignment,
    generate_candidate,
)


DIGEST_KEY = b"d" * 32
OWNER_KEY = b"o" * 32
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def routing_fields(
    *,
    operation: str = "exploration",
    ambiguity: str = "known",
    context_breadth: str = "repository",
    acceptance_strength: str = "executable",
) -> dict:
    return {
        "operation": operation,
        "ambiguity": ambiguity,
        "context_breadth": context_breadth,
        "acceptance_strength": acceptance_strength,
        "routing_evidence_refs": [],
    }
END_TO_END_CAPABILITY = {
    "producers": ["app_server"],
    "hook_coverage": "unknown",
    "app_server_coverage": "end_to_end_complete",
    "root_lifecycle": True,
    "child_lifecycle": True,
    "tool_lifecycle": True,
    "root_usage": True,
    "child_usage": True,
    "ordering_proven": True,
}
FIXTURE_CLIENT_VERSION = "codex-eval-fixture-1"
GRADER_CONFIG = {
    "model": "gpt-6-astra",
    "effort": "high",
    "prompt_digest": DIGEST_A,
    "rubric_digest": DIGEST_B,
    "masking": True,
    "drift_digest": DIGEST_C,
    "owner_override_digest": "d" * 64,
}


def generate_test_owner_key(directory: Path) -> tuple[Path, str]:
    private_key = directory / "test-owner-key"
    completed = subprocess.run(
        [
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test-only",
            "-f", str(private_key),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("test SSH owner key generation failed")
    return private_key, private_key.with_suffix(".pub").read_text()


def sign_owner_grant(
    authority: OwnerGrantAuthority,
    private_key: Path,
    **request_arguments: object,
) -> dict:
    request = authority.request(**request_arguments)
    payload = private_key.parent / f"grant-payload-{request['nonce_digest']}"
    payload.write_bytes(authority.signing_payload(request))
    completed = subprocess.run(
        [
            "ssh-keygen", "-Y", "sign", "-f", str(private_key),
            "-n", OWNER_SIGNATURE_NAMESPACE, str(payload),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    signature_path = Path(str(payload) + ".sig")
    try:
        if completed.returncode != 0:
            raise RuntimeError("test SSH owner signing failed")
        return authority.attach_signature(request, signature_path.read_bytes())
    finally:
        payload.unlink(missing_ok=True)
        signature_path.unlink(missing_ok=True)


def sign_owner_payload(private_key: Path, payload_bytes: bytes, name: str) -> Path:
    payload = private_key.parent / name
    payload.write_bytes(payload_bytes)
    completed = subprocess.run(
        [
            "ssh-keygen", "-Y", "sign", "-f", str(private_key),
            "-n", OWNER_SIGNATURE_NAMESPACE, str(payload),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError("test SSH owner signing failed")
    return Path(str(payload) + ".sig")


def event_record(identifier: str = "evt_1", *, observed_at_ms: int = 1000) -> dict:
    return {
        "schema_version": 1,
        "record_type": "event",
        "record_id": identifier,
        "run_id": "run_1",
        "turn_id": "turn_1",
        "assignment_id": None,
        "parent_run_id": None,
        "observed_at_ms": observed_at_ms,
        "event_code": "turn_started",
        "model": "gpt-5.6-luna",
        "effort": None,
        "role": None,
        "task_family": None,
        "task_slice": None,
        "topology": None,
        "configuration_digest": None,
        "status": "started",
        "stop_reason": None,
        "tool_digest": None,
        "item_digest": None,
        "coverage": "lifecycle_partial",
        "usage": empty_usage(),
    }


def assignment() -> dict:
    return {
        "assignment_id": "assignment-1",
        "task_id": "task-1",
        "objective": "Read one fixture",
        "role": "codex_agents_researcher",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "attempt": 1,
        "meaningful": True,
        "owned_paths": [],
        "context": ["source.txt"],
        "permissions": {
            "tools": ["shell"],
            "filesystem": ["source.txt:read"],
            "network": False,
            "mcps": [],
            "external_effects": False,
        },
        "expected_output": "One marker",
        "required_evidence": [{"id": "marker", "description": "marker exists"}],
        "verification_required": True,
        "bounds": {
            "max_attempts": 2,
            "max_steers_per_attempt": 1,
            "max_no_progress_events": 2,
            "wait_seconds": 60,
            "timeout_seconds": 600,
            "max_tool_calls": 30,
        },
        "stopping_conditions": ["marker found"],
        "prohibited_actions": ["network"],
    }


def result() -> dict:
    return {
        "assignment_id": "assignment-1",
        "status": "completed",
        "summary": "sensitive result text",
        "artifacts": ["private/path"],
        "evidence": [
            {
                "criterion_id": "marker",
                "type": "command",
                "reference": "secret command",
                "outcome": "pass",
                "details": "secret output",
            }
        ],
        "unverified_claims": [],
        "remaining_risks": [],
    }


def full_usage(
    *, input_tokens: int = 100, output_tokens: int = 50, model_calls: int = 2
) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_tokens": 10,
        "output_tokens": output_tokens,
        "reasoning_tokens": 20,
        "model_calls": model_calls,
        "tool_calls": 1,
        "latency_ms": 100,
        "cost_microunits": None,
    }


def verification_outcome(record_id: str = "verification_outcome") -> dict:
    configuration = {
        "model": "gpt-5.6-sol",
        "effort": "high",
        "role": "codex_agents_verification_worker",
        "topology": "single",
        "max_parallel": 1,
        "verification_required": False,
        "verifier_digest": None,
        "bounds_digest": DIGEST_B,
        "package_digest": DIGEST_A,
        "policy_digest": DIGEST_B,
        "agent_digest": DIGEST_C,
        "skill_digests": [DIGEST_A],
        "prompt_digest": DIGEST_B,
        "client_digest": DIGEST_C,
    }
    usage = full_usage()
    usage_sources = {
        field: {
            "source": "client_observed" if value is not None else "unavailable",
            "coverage": "end_to_end_complete" if value is not None else "unknown",
        }
        for field, value in usage.items()
    }
    return {
        "schema_version": 1,
        "record_type": "outcome",
        "record_id": record_id,
        "run_id": f"run_{record_id}",
        "assignment_id": f"assignment_{record_id}",
        "task_slice": "verification-learning",
        "fixture_digest": DIGEST_A,
        **configuration,
        "configuration_digest": content_digest(configuration),
        "observed_at_ms": 1,
        "status": "accepted",
        "execution_state": "completed",
        "semantic_verdict": "pass",
        "admission_state": "accepted",
        "retry_attempt": 1,
        "retry_of_assignment_id": None,
        "deterministic_pass": True,
        "security_pass": True,
        "required_criteria_total": 1,
        "required_criteria_passed": 1,
        "quality_micros": 1_000_000,
        "repairs": 0,
        "repair_reason_codes": [],
        "delegation_overhead_tokens": 0,
        "no_progress_events": 0,
        "terminal_failure": False,
        "verifier_disagreement": False,
        "accepted": True,
        "coverage": "end_to_end_complete",
        "evaluation_candidate_digest": None,
        "evaluation_case_digest": None,
        "evidence_digests": [DIGEST_A],
        "usage": usage,
        "usage_sources": usage_sources,
    }


def candidate(evidence_digests: list[str] | None = None) -> dict:
    before = {"model": "gpt-6-sol", "effort": "medium"}
    after = {"model": "gpt-6-luna", "effort": "high"}
    manifest = [
        {
            "path_code": "routing.model_effort",
            "before_digest": content_digest(before),
            "after_digest": content_digest(after),
            "before_value": before,
            "after_value": after,
        }
    ]
    champion = champion_configuration()
    evidence = evidence_digests or [DIGEST_C]
    return generate_candidate(
        digest_key=DIGEST_KEY,
        scope="general",
        task_slice="coding-medium-low",
        target="model_effort",
        effect="optimize",
        base_digest=candidate_base_digest(champion),
        champion_configuration=champion,
        change_manifest=manifest,
        evidence_digests=evidence,
        eligibility_receipt=eligibility_receipt("coding-medium-low", evidence),
    )


def eligibility_receipt(task_slice: str, evidence_digests: list[str]) -> dict:
    return {
        "task_slice": task_slice,
        "evidence_set_digest": content_digest(sorted(evidence_digests)),
        "independent_case_count": 20,
        "usage_complete": True,
        "version_unconfounded": True,
        "task_mix_matched": True,
        "retry_deduplicated": True,
        "verifier_evidence_complete": True,
    }


def champion_configuration() -> dict:
    return {
        "model": "gpt-6-sol",
        "effort": "medium",
        "role": "codex_agents_implementation_worker",
        "topology": "single",
        "max_parallel": 1,
        "verification_required": True,
        "verifier_digest": DIGEST_A,
        "bounds_digest": DIGEST_B,
        "package_digest": DIGEST_A,
        "policy_digest": DIGEST_B,
        "agent_digest": DIGEST_C,
        "skill_digests": [DIGEST_A],
        "prompt_digest": DIGEST_B,
        "client_digest": content_digest(END_TO_END_CAPABILITY),
    }


def trial_outcome(
    case: dict, cohort: str, candidate_record: dict | None = None,
    *, trial_id: str | None = None,
) -> dict:
    proposed = candidate_record or candidate()
    record = verification_outcome(f"outcome-{case['record_id']}-{cohort}")
    selected = proposed[
        "champion_configuration" if cohort == "champion" else "candidate_configuration"
    ]
    record.update(
        **selected,
        assignment_id=trial_id or record["assignment_id"],
        task_slice=case["task_slice"],
        fixture_digest=case["fixture_digest"],
        evaluation_candidate_digest=(
            proposed["base_digest"]
            if cohort == "champion"
            else proposed["candidate_digest"]
        ),
        evaluation_case_digest=content_digest(case),
        usage=full_usage(
            input_tokens=100 if cohort == "champion" else 80,
            output_tokens=50 if cohort == "champion" else 40,
            model_calls=2,
        ),
    )
    configuration = {
        key: record[key]
        for key in (
            "model", "effort", "role", "topology", "max_parallel",
            "verification_required", "verifier_digest", "bounds_digest", "package_digest",
            "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
            "client_digest",
        )
    }
    record["configuration_digest"] = content_digest(configuration)
    return record


def trial(
    case: dict,
    cohort: str,
    candidate_record: dict | None = None,
    assignment_record: dict | None = None,
    **overrides: object,
) -> dict:
    proposed = candidate_record or candidate()
    assigned = assignment_record or evaluation_assignment([case], proposed)
    plan = next(
        item for item in assigned["trial_plan"]
        if item["case_id"] == case["record_id"] and item["cohort"] == cohort
    )
    record = trial_outcome(case, cohort, proposed, trial_id=plan["trial_id"])
    value = build_evaluation_trial(
        case=case,
        outcome=record,
        assignment=assigned,
        trial_id=plan["trial_id"],
    )
    value.update(overrides)
    material = {key: item for key, item in value.items() if key != "record_id"}
    value["record_id"] = f"trial:{content_digest(material)}"
    return value


def refresh_trial(value: dict) -> None:
    material = {key: item for key, item in value.items() if key != "record_id"}
    value["record_id"] = f"trial:{content_digest(material)}"


def heldout_cases(count: int = 20) -> list[dict]:
    return [
        {
            "schema_version": 1,
            "record_type": "evaluation_case",
            "record_id": f"case-{index:02d}",
            "source_outcome_id": f"outcome-{index:02d}",
            "supersedes_case_id": f"development-{index:02d}",
            "task_slice": "coding-medium-low",
            "corpus_state": "promotion_holdout",
            "fixture_digest": f"{index + 1:064x}",
            "criteria_digest": f"{index + 101:064x}",
            "lineage_digest": f"{index + 201:064x}",
            "similarity_group_digest": f"{index + 401:064x}",
            "reviewed": True,
            "review_digest": f"{index + 301:064x}",
            "frozen": True,
        }
        for index in range(count)
    ]


def evaluation_assignment(
    cases: list[dict], candidate_record: dict | None = None
) -> dict:
    return create_evaluation_assignment(
        candidate=candidate_record or candidate(),
        cases=cases,
        client_version=FIXTURE_CLIENT_VERSION,
        client_capability=END_TO_END_CAPABILITY,
        observed_at_ms=0,
    )


def matched_trials(
    count: int = 20, candidate_record: dict | None = None
) -> tuple[list[dict], list[dict]]:
    cases = heldout_cases(count)
    proposed = candidate_record or candidate()
    assigned = evaluation_assignment(cases, proposed)
    records = [
        trial(case, cohort, proposed, assigned)
        for case in cases
        for cohort in ("champion", "candidate")
    ]
    return cases, records


PROVIDER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers"


def provider_fixture(name: str) -> bytes:
    return (PROVIDER_FIXTURES / name).read_bytes()


# Stand-ins for the provider clients: each answers from a JSON file in its own
# directory, so a test changes an observation by rewriting that file. They never
# contact a provider.
# Child mode replays frames from `<name>-frames.jsonl`: a {"sleep": s} line
# pauses, a {"raw": text} line prints text as is, and every invocation (arguments, working directory, environment
# names and the files it can see) is logged for the test to inspect.
FAKE_CHILD = """\
import json, os, sys, time


def strict(schema):
    # The structured-output rule real codex exec enforces: every object lists
    # all of its properties as required and allows nothing else.
    if isinstance(schema, list):
        return all(strict(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    if "properties" in schema and (
        sorted(schema.get("required", [])) != sorted(schema["properties"])
        or schema.get("additionalProperties") is not False
    ):
        return False
    return "$schema" not in schema and all(strict(value) for value in schema.values())


def child(directory, name):
    if "--output-schema" in sys.argv:
        with open(sys.argv[sys.argv.index("--output-schema") + 1]) as handle:
            if not strict(json.load(handle)):
                print(json.dumps({"type": "error", "message": "invalid schema"}), flush=True)
                sys.exit(1)
    with open(os.path.join(directory, name + "-invocations.jsonl"), "a") as log:
        log.write(json.dumps({
            "argv": sys.argv[2:],
            "cwd": os.getcwd(),
            "env": sorted(os.environ),
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "files": sorted(
                os.path.relpath(os.path.join(base, item), os.getcwd())
                for base, _, items in os.walk(os.getcwd()) for item in items
            ),
            # What the bridge placed in the reserved verifier input directory.
            "reserved_inputs": {
                item: {
                    "content": open(os.path.join(base, item)).read(),
                    "mode": oct(os.stat(os.path.join(base, item)).st_mode & 0o777),
                }
                for base, _, items in os.walk(os.path.join(os.getcwd(), ".orchestration-input"))
                for item in items
            },
        }) + "\\n")
    frames = os.path.join(directory, name + "-frames.jsonl")
    for line in open(frames) if os.path.exists(frames) else []:
        frame = json.loads(line)
        if "sleep" in frame:
            time.sleep(frame["sleep"])
            continue
        print(frame["raw"] if "raw" in frame else json.dumps(frame), flush=True)
    last = os.path.join(directory, name + "-last-message.json")
    if "-o" in sys.argv and os.path.exists(last):
        with open(sys.argv[sys.argv.index("-o") + 1], "w") as handle:
            handle.write(open(last).read())
    code = os.path.join(directory, name + "-exit-code")
    sys.exit(int(open(code).read()) if os.path.exists(code) else 0)


directory, name = sys.argv[1], sys.argv[2]
arguments = sys.argv[3:]
if arguments == ["--version"]:
    # Answers from <name>-version.txt, or a version inside the reviewed range.
    version = os.path.join(directory, name + "-version.txt")
    default = {"claude": "2.1.200 (Claude Code)", "codex": "codex-cli 0.164.0"}[name]
    sys.stdout.write(open(version).read() if os.path.exists(version) else default + "\\n")
elif name == "claude" and arguments[:2] == ["auth", "status"]:
    sys.stdout.write(open(os.path.join(directory, "claude-status.json")).read())
elif name == "codex" and arguments == ["exec", "--help"]:
    # Answers from codex-exec-help.txt, or the options the reviewed client lists.
    help_text = os.path.join(directory, "codex-exec-help.txt")
    default = (
        "Options:\\n  -m, --model <MODEL>\\n      --ephemeral\\n"
        "      --ignore-user-config\\n      --ignore-rules\\n      --json\\n"
    )
    sys.stdout.write(open(help_text).read() if os.path.exists(help_text) else default)
elif name == "codex" and arguments[:1] == ["app-server"]:
    log = open(os.path.join(directory, "methods.log"), "a")
    for line in sys.stdin:
        message = json.loads(line)
        log.write(message["method"] + "\\n")
        log.flush()
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {"userAgent": "fake"}
        elif message["method"] == "account/rateLimits/read":
            result = json.load(open(os.path.join(directory, "codex-result.json")))
        else:
            print(json.dumps({"id": message["id"], "error": {"code": -1}}), flush=True)
            continue
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
else:
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    child(directory, name)
"""


def fake_provider_tools(directory: Path) -> dict[str, str]:
    """Install fake `codex` and `claude` clients; return an environment using them.

    They answer observations from rewritable JSON files and replay recorded
    child frames. They never contact a provider.
    """

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codex-result.json").write_bytes(provider_fixture("codex-rate-limits.json"))
    (directory / "claude-status.json").write_bytes(provider_fixture("claude-auth-status.json"))
    (directory / "fake_child.py").write_text(FAKE_CHILD)
    for name in ("codex", "claude"):
        script = directory / name
        script.write_text(
            f"#!/bin/sh\nexec {sys.executable} {directory / 'fake_child.py'} "
            f"{directory} {name} \"$@\"\n"
        )
        script.chmod(0o755)
    return {"PATH": str(directory)}


@contextlib.contextmanager
def trusted_tools(tools: Path):
    """Make the fake clients the trusted search path, as installed clients would be.

    The fakes live in the system temp directory, which a real client may never
    resolve into, so no temporary root counts here and their folder counts as a
    trusted installation folder; the project root still does not.
    """

    with mock.patch.object(providers, "TRUSTED_SEARCH_DIRECTORIES", (str(tools),)), \
            mock.patch.object(providers, "temp_roots", lambda: set()), \
            mock.patch.object(
                host_paths, "TRUSTED_INSTALL_ROOTS",
                (*host_paths.TRUSTED_INSTALL_ROOTS, os.path.realpath(tools)),
            ):
        yield


def observe_provider_fixture(
    ledger, project_id: str, provider: str, *, tools: Path, observed_at_ms: int
) -> list[dict]:
    """Run the ledger's own observation against the fake clients at a fixed time."""

    with trusted_tools(tools), mock.patch.object(
        ledger, "_clock_ms", return_value=observed_at_ms
    ):
        return ledger.observe_provider(project_id, provider, project_root=tools)


def attest_provider(
    ledger, project_id: str, private_key: Path, provider: str, fingerprint: str,
    *, issued_at: int = 1, credits_spending_disabled: bool = False,
) -> dict:
    """Sign and store an owner billing attestation for one account fingerprint."""

    record = billing_attestation_record({
        "version": 1,
        "provider": provider,
        "account_fingerprint": fingerprint,
        "no_paid_overage": True,
        "no_usage_credits": True,
        "issued_at": issued_at,
        **({"codex_credits_spending_disabled": True} if credits_spending_disabled else {}),
    })
    authority = OwnerGrantAuthority(ledger.state.owner_allowed_signers())
    signature = sign_owner_payload(
        private_key,
        authority.owner_record_payload(project_id, record),
        f"attestation-{record['record_id'].split(':')[1]}",
    )
    try:
        ledger.commit_owner_record(
            project_id, record=record, signature=signature.read_bytes()
        )
    finally:
        signature.unlink(missing_ok=True)
    return record


def admit_provider(
    ledger, project_id: str, private_key: Path, provider: str = "codex",
    *, tools: Path, observed_at_ms: int,
) -> str:
    """Give a project a fresh identity and a matching attestation; return the fingerprint."""

    identity = observe_provider_fixture(
        ledger, project_id, provider, tools=tools, observed_at_ms=observed_at_ms
    )[0]
    attest_provider(
        ledger, project_id, private_key, provider, identity["account_fingerprint"]
    )
    return identity["account_fingerprint"]
