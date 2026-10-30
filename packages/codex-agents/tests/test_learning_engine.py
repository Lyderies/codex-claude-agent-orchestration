from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import (
    candidate_base_digest,
    canonical_json,
    content_digest,
    keyed_digest,
    load_released_candidate_digests,
    validate_candidate,
    validate_learning_record,
)
from codex_agents.learning_engine import (
    CandidateLifecycle,
    OwnerGrantAuthority,
    QA_WORK_DIRECTORY_TOKEN,
    _bounded_tree_size,
    _copy_and_digest_runtime_tree,
    _sandboxed_check_argv,
    _snapshot_file,
    _terminate_process_group,
    analyze_outcomes,
    apply_candidate_to_route,
    build_canary_report,
    build_feedback,
    build_operational_qa_records,
    build_outcome,
    build_verified_qa_records,
    calibrate_grader,
    compare_candidate,
    create_evaluation_assignment,
    deduplicate_cases,
    generate_candidate,
    installed_execution_configuration,
    load_operational_check_configuration,
    mine_evaluation_case,
    operational_sandbox_preflight,
    review_evaluation_case,
    run_operational_check_suite,
    validate_operational_check_configuration_input,
)
from codex_agents.routing import load_effective_policy, load_policy, route_task

from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    DIGEST_KEY,
    END_TO_END_CAPABILITY,
    FIXTURE_CLIENT_VERSION,
    GRADER_CONFIG,
    assignment,
    candidate,
    champion_configuration,
    evaluation_assignment,
    full_usage,
    generate_test_owner_key,
    heldout_cases,
    matched_trials,
    refresh_trial,
    result,
    routing_fields,
    sign_owner_grant,
    sign_owner_payload,
    trial_outcome,
    verification_outcome,
)


def verified_qa_inputs() -> dict:
    proposed = candidate()
    case = heldout_cases(1)[0]
    assigned = evaluation_assignment([case], proposed)
    plan = next(item for item in assigned["trial_plan"] if item["cohort"] == "candidate")
    selected = proposed["candidate_configuration"]
    evaluator_assignment = assignment()
    evaluator_assignment.update(
        assignment_id=plan["trial_id"],
        task_id=case["record_id"],
        objective="Run one frozen fixture",
        role=selected["role"],
        model=selected["model"],
        effort=selected["effort"],
        context=[],
        verification_required=True,
    )
    evaluator_result = result()
    evaluator_result["assignment_id"] = plan["trial_id"]
    host = trial_outcome(case, "candidate", proposed, trial_id=plan["trial_id"])
    host.update(
        observed_at_ms=100,
        status="unverified",
        admission_state="unverified",
        deterministic_pass=False,
        security_pass=False,
        required_criteria_passed=0,
        quality_micros=0,
        accepted=False,
    )
    host["evidence_digests"] = sorted(
        {
            keyed_digest(DIGEST_KEY, canonical_json(evaluator_assignment)),
            keyed_digest(DIGEST_KEY, canonical_json(evaluator_result)),
        }
    )
    verifier_assignment = assignment()
    verifier_assignment.update(
        assignment_id="verify-assignment",
        task_id="verify-task",
        objective="Independently verify the frozen fixture",
        role="codex_agents_verification_worker",
        model="gpt-6-sol",
        effort="high",
        context=[content_digest(host)],
        verification_required=False,
    )
    verifier_result = result()
    verifier_result["assignment_id"] = verifier_assignment["assignment_id"]
    verifier_host = verification_outcome("verifier-host")
    verifier_host.update(
        run_id="run_verifier_host",
        assignment_id=verifier_assignment["assignment_id"],
        task_slice="verification-medium-low",
        model=verifier_assignment["model"],
        effort=verifier_assignment["effort"],
        role=verifier_assignment["role"],
        observed_at_ms=101,
        status="unverified",
        admission_state="unverified",
        deterministic_pass=False,
        security_pass=False,
        required_criteria_passed=0,
        quality_micros=0,
        accepted=False,
        usage={
            **full_usage(input_tokens=10, output_tokens=5, model_calls=1),
            "reasoning_tokens": 0,
        },
    )
    verifier_configuration = {
        key: verifier_host[key]
        for key in (
            "model", "effort", "role", "topology", "max_parallel",
            "verification_required", "verifier_digest", "bounds_digest",
            "package_digest", "policy_digest", "agent_digest", "skill_digests",
            "prompt_digest", "client_digest",
        )
    }
    verifier_host["configuration_digest"] = content_digest(verifier_configuration)
    verifier_host["evidence_digests"] = sorted(
        {
            keyed_digest(DIGEST_KEY, canonical_json(verifier_assignment)),
            keyed_digest(DIGEST_KEY, canonical_json(verifier_result)),
        }
    )
    return {
        "digest_key": DIGEST_KEY,
        "host_outcome": host,
        "verifier_host_outcome": verifier_host,
        "evaluation_assignment": assigned,
        "evaluation_case": case,
        "evaluator_assignment": evaluator_assignment,
        "evaluator_result": evaluator_result,
        "check_report": {
            "deterministic_checks": [
                {"check_id": "fixture", "passed": True, "evidence_digest": DIGEST_A}
            ],
            "security_checks": [
                {"check_id": "authority", "passed": True, "evidence_digest": DIGEST_B}
            ],
            "observed_at_ms": 102,
        },
        "verifier_assignment": verifier_assignment,
        "verifier_result": verifier_result,
    }


def outcome(**overrides: object) -> dict:
    record = build_outcome(
        assignment=assignment(),
        result=result(),
        digest_key=DIGEST_KEY,
        run_id="run_observed",
        task_slice="coding-low-low",
        fixture_digest=DIGEST_A,
        topology="delegated",
        max_parallel=1,
        verification_required=True,
        verifier_digest=DIGEST_A,
        bounds_digest=DIGEST_B,
        package_digest=DIGEST_A,
        policy_digest=DIGEST_B,
        agent_digest=DIGEST_C,
        skill_digests=[DIGEST_A],
        prompt_digest=DIGEST_B,
        client_digest=DIGEST_C,
        deterministic_pass=True,
        security_pass=True,
        quality_score=1.0,
        repairs=0,
        repair_reason_codes=[],
        delegation_overhead_tokens=10,
        no_progress_events=0,
        terminal_failure=False,
        verifier_disagreement=False,
        accepted=True,
        coverage="end_to_end_complete",
        workflow_usage=full_usage(),
        observed_at_ms=1000,
    )
    record.update(overrides)
    return record


def canary_results(
    champion_digest: str, candidate_digest_value: str, *, candidate_pass: bool = True
) -> list[dict]:
    champion = verification_outcome("canary_champion")
    champion.update(
        task_slice="coding-low-low",
        evaluation_candidate_digest=champion_digest,
        evaluation_case_digest=DIGEST_B,
    )
    candidate_outcome = verification_outcome("canary_candidate")
    candidate_outcome.update(
        task_slice="coding-low-low",
        evaluation_candidate_digest=candidate_digest_value,
        evaluation_case_digest=DIGEST_B,
    )
    if not candidate_pass:
        candidate_outcome.update(
            deterministic_pass=False,
            accepted=False,
            status="rejected",
            admission_state="rejected",
        )
    return [
        {
            "case_digest": DIGEST_B,
            "champion_outcome": champion,
            "candidate_outcome": candidate_outcome,
        }
    ]


class VerifiedQaTests(unittest.TestCase):
    def test_operational_check_requires_exact_interpreter_digest(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            script = root / "qa-check"
            script.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            script.chmod(0o700)
            base = {
                "check_id": "tests",
                "driver": "python-wrapper",
                "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": "0" * 64,
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [base],
                    "security_checks": [{**base, "check_id": "security"}],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "interpreter digest"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_rejects_script_interpreter_chain(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            interpreter = root / "interpreter"
            interpreter.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n")
            interpreter.chmod(0o700)
            script = root / "qa-check"
            script.write_text(f"#!{interpreter}\nraise SystemExit(0)\n")
            script.chmod(0o700)
            check = {
                "check_id": "tests",
                "driver": "python-wrapper",
                "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    interpreter.read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [check],
                    "security_checks": [{**check, "check_id": "security"}],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "interpreter is invalid"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_rejects_project_executable_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            target = root / "real-check"
            target.write_bytes(Path("/usr/bin/true").read_bytes())
            target.chmod(0o700)
            (root / "qa-check").symlink_to(target)
            check = {
                "check_id": "tests",
                "driver": "python-wrapper",
                "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "interpreter_sha256": None,
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [check],
                    "security_checks": [{**check, "check_id": "security"}],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "executable is invalid"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_snapshot_uses_opened_validated_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            source = root / "approved"
            replacement = root / "replacement"
            target = root / "snapshot"
            source.write_bytes(b"approved bytes")
            replacement.write_bytes(b"changed bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            from codex_agents import learning_engine

            original_read = learning_engine.os.read
            replaced = False

            def replace_after_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                result = original_read(descriptor, size)
                if result and not replaced:
                    os.replace(replacement, source)
                    replaced = True
                return result

            with patch(
                "codex_agents.learning_engine.os.read",
                side_effect=replace_after_read,
            ):
                _snapshot_file(source, digest, target)
            self.assertEqual(b"approved bytes", target.read_bytes())
            self.assertEqual(b"changed bytes", source.read_bytes())

    def test_operational_check_snapshot_rejects_in_place_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            source = root / "approved"
            target = root / "snapshot"
            source.write_bytes(b"a" * 131_072)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            from codex_agents import learning_engine

            original_read = learning_engine.os.read
            changed = False

            def change_after_first_chunk(descriptor: int, size: int) -> bytes:
                nonlocal changed
                result = original_read(descriptor, size)
                if result and not changed:
                    with source.open("r+b") as stream:
                        stream.seek(65_536)
                        stream.write(b"b" * 65_536)
                        stream.flush()
                        os.fsync(stream.fileno())
                    changed = True
                return result

            with patch(
                "codex_agents.learning_engine.os.read",
                side_effect=change_after_first_chunk,
            ), self.assertRaisesRegex(ContractError, "identity changed"):
                _snapshot_file(source, digest, target)
            self.assertFalse(target.exists())

    def test_operational_check_snapshots_pinned_python_runtime_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            config_root = root / ".codex/orchestration"
            packages = (
                root
                / "runtime"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            config_root.mkdir(parents=True)
            packages.mkdir(parents=True)
            (packages / "qa_dependency.py").write_text(
                "VALUE = 'approved'\n", encoding="utf-8"
            )
            script = root / "qa-runtime"
            script.write_text(
                f"#!{sys.executable}\n"
                "import qa_dependency\n"
                "raise SystemExit(0 if qa_dependency.VALUE == 'approved' else 1)\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            check = {
                "check_id": "runtime",
                "driver": "python-wrapper",
                "argv": ["./qa-runtime"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": {
                    "kind": "python-site-packages",
                    "path": str(packages),
                    "sha256": _copy_and_digest_runtime_tree(packages),
                },
                "timeout_seconds": 5,
            }
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [{**check, "check_id": "security"}],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm",
                    "Add runtime check",
                ],
                cwd=root,
                check=True,
            )
            report = run_operational_check_suite(root)
            self.assertTrue(report["deterministic_checks"][0]["passed"])
            self.assertTrue(report["security_checks"][0]["passed"])

    def test_operational_check_rejects_changed_python_runtime_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            packages = (
                root
                / "runtime"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            packages.mkdir(parents=True)
            dependency = packages / "qa_dependency.py"
            dependency.write_text("VALUE = 'approved'\n", encoding="utf-8")
            digest = _copy_and_digest_runtime_tree(packages)
            script = root / "qa-runtime"
            script.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            script.chmod(0o700)
            check = {
                "check_id": "runtime",
                "driver": "python-wrapper",
                "argv": ["./qa-runtime"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": {
                    "kind": "python-site-packages",
                    "path": str(packages),
                    "sha256": digest,
                },
                "timeout_seconds": 5,
            }
            dependency.write_text("VALUE = 'changed'\n", encoding="utf-8")
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [check],
                    "security_checks": [{**check, "check_id": "security"}],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "tree digest"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_runtime_digest_includes_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            packages = Path(directory) / "site-packages"
            packages.mkdir()
            before = _copy_and_digest_runtime_tree(packages)
            (packages / "empty-package").mkdir()
            after = _copy_and_digest_runtime_tree(packages)
            self.assertNotEqual(before, after)

    def test_operational_check_runtime_tree_bounds_depth_and_copied_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            packages = Path(directory) / "site-packages"
            nested = packages / "one" / "two"
            nested.mkdir(parents=True)
            (nested / "module.py").write_bytes(b"four")
            with patch(
                "codex_agents.learning_engine.MAX_RUNTIME_TREE_DEPTH", 1
            ), self.assertRaisesRegex(ContractError, "exceeds its bounds"):
                _copy_and_digest_runtime_tree(packages)
            with patch(
                "codex_agents.learning_engine.MAX_RUNTIME_TREE_DEPTH", 32
            ), patch(
                "codex_agents.learning_engine.MAX_RUNTIME_TREE_BYTES", 3
            ), self.assertRaisesRegex(ContractError, "exceeds its bounds"):
                _copy_and_digest_runtime_tree(packages)

    def test_operational_check_rejects_direct_python_launcher(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            script = root / "qa.py"
            script.write_text("raise SystemExit(0)\n", encoding="utf-8")
            executable = Path("/usr/bin/python3")
            check = {
                "check_id": "python",
                "driver": "python-wrapper",
                "argv": [str(executable), "./qa.py"],
                "executable_sha256": hashlib.sha256(
                    executable.read_bytes()
                ).hexdigest(),
                "interpreter_sha256": None,
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [check],
                    "security_checks": [{**check, "check_id": "security"}],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "driver is invalid"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_rejects_untyped_code_loaders(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            source = root / "mutable-source"
            source.write_text("exit 0\n", encoding="utf-8")
            for executable, arguments in (
                (Path("/usr/bin/osascript"), [str(source)]),
                (Path("/usr/bin/awk"), ["-f", str(source)]),
                (Path("/usr/bin/make"), ["-f", str(source)]),
            ):
                check = {
                    "check_id": "loader",
                    "driver": "python-wrapper",
                    "argv": [str(executable), *arguments],
                    "executable_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                    "interpreter_sha256": None,
                    "runtime_tree": None,
                    "timeout_seconds": 5,
                }
                raw = json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [
                            {**check, "check_id": "security-loader"}
                        ],
                    }
                ).encode()
                with self.assertRaisesRegex(ContractError, "driver is invalid"):
                    validate_operational_check_configuration_input(root, raw)

    def test_operational_check_rejects_ruff_result_control_arguments(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            executable = root / "ruff"
            executable.write_bytes(b"pinned native tool")
            executable.chmod(0o700)
            base = {
                "check_id": "ruff",
                "driver": "ruff",
                "executable_sha256": hashlib.sha256(
                    executable.read_bytes()
                ).hexdigest(),
                "interpreter_sha256": None,
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            invalid_arguments = (
                ["check", "--isolated", "--no-cache", "--exit-zero", "."],
                ["check", "--isolated", "--no-cache", "--help"],
                ["check", "--isolated", "--no-cache", "--select", "ALL", "."],
                ["check", "--isolated", "--no-cache", "-"],
                ["check", "--exit-zero", "--", "--isolated", "--no-cache", "."],
                ["check", "--isolated", "--no-cache", "/private/tmp"],
                ["check", "--isolated", "--no-cache", "../outside"],
            )
            for arguments in invalid_arguments:
                check = {**base, "argv": [str(executable), *arguments]}
                raw = json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [
                            {**check, "check_id": "security-ruff"}
                        ],
                    }
                ).encode()
                with self.assertRaisesRegex(ContractError, "driver is invalid"):
                    validate_operational_check_configuration_input(root, raw)

            valid = {
                **base,
                "argv": [str(executable), "check", "--isolated", "--no-cache", "."],
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [valid],
                    "security_checks": [{**valid, "check_id": "security-ruff"}],
                }
            ).encode()
            validate_operational_check_configuration_input(root, raw)
            selected = {
                **base,
                "argv": [
                    str(executable), "check", "--isolated", "--no-cache",
                    "--select", "E4,E7,E9,F", ".",
                ],
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [selected],
                    "security_checks": [{**selected, "check_id": "security-ruff"}],
                }
            ).encode()
            validate_operational_check_configuration_input(root, raw)

    def test_operational_check_work_directory_token_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            executable = root / "pytest"
            executable.write_text(
                f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8"
            )
            executable.chmod(0o700)
            base = {
                "check_id": "pytest",
                "driver": "python-wrapper",
                "executable_sha256": hashlib.sha256(
                    executable.read_bytes()
                ).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            valid = {
                **base,
                "argv": [
                    str(executable), "--basetemp", QA_WORK_DIRECTORY_TOKEN
                ],
            }
            raw = json.dumps({
                "version": 1,
                "deterministic_checks": [valid],
                "security_checks": [{**valid, "check_id": "security-pytest"}],
            }).encode()
            validate_operational_check_configuration_input(root, raw)

            invalid = {
                **base,
                "argv": [str(executable), QA_WORK_DIRECTORY_TOKEN],
            }
            raw = json.dumps({
                "version": 1,
                "deterministic_checks": [invalid],
                "security_checks": [{**invalid, "check_id": "security-pytest"}],
            }).encode()
            with self.assertRaisesRegex(ContractError, "driver is invalid"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_input_rejects_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [
                        {
                            "check_id": "tests",
                            "driver": "python-wrapper",
                            "argv": ["/bin/sh", "-c", "true"],
                            "executable_sha256": hashlib.sha256(
                                Path("/bin/sh").resolve().read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": None,
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                    "security_checks": [
                        {
                            "check_id": "security",
                            "driver": "python-wrapper",
                            "argv": ["/usr/bin/true"],
                            "executable_sha256": hashlib.sha256(
                                Path("/usr/bin/true").read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": None,
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "executable is invalid"):
                validate_operational_check_configuration_input(Path(directory), raw)

    def test_operational_check_input_rejects_env_launcher(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            executable = Path("/usr/bin/env")
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [
                        {
                            "check_id": "tests",
                            "driver": "python-wrapper",
                            "argv": [str(executable), "true"],
                            "executable_sha256": hashlib.sha256(
                                executable.read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": None,
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                    "security_checks": [
                        {
                            "check_id": "security",
                            "driver": "python-wrapper",
                            "argv": ["/usr/bin/true"],
                            "executable_sha256": hashlib.sha256(
                                Path("/usr/bin/true").read_bytes()
                            ).hexdigest(),
                            "interpreter_sha256": None,
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                }
            ).encode()
            with self.assertRaisesRegex(ContractError, "executable is invalid"):
                validate_operational_check_configuration_input(Path(directory), raw)

    def test_operational_check_rejects_changed_executable_digest(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            script = root / "qa-check"
            script.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            script.chmod(0o700)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [
                        {
                            "check_id": "tests",
                            "driver": "python-wrapper",
                            "argv": ["./qa-check"],
                            "executable_sha256": digest,
                            "interpreter_sha256": hashlib.sha256(
                                Path(sys.executable).resolve().read_bytes()
                            ).hexdigest(),
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                    "security_checks": [
                        {
                            "check_id": "security",
                            "driver": "python-wrapper",
                            "argv": ["./qa-check"],
                            "executable_sha256": digest,
                            "interpreter_sha256": hashlib.sha256(
                                Path(sys.executable).resolve().read_bytes()
                            ).hexdigest(),
                            "runtime_tree": None,
                            "timeout_seconds": 5,
                        }
                    ],
                }
            ).encode()
            validate_operational_check_configuration_input(root, raw)
            script.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
            with self.assertRaisesRegex(ContractError, "digest does not match"):
                validate_operational_check_configuration_input(root, raw)

    def test_operational_check_cleanup_targets_the_process_group(self) -> None:
        process = Mock(pid=8123)
        process.poll.side_effect = [None, None, 0]
        process.wait.return_value = 0
        with patch("codex_agents.learning_engine.os.killpg") as killpg, patch(
            "codex_agents.learning_engine.time.sleep"
        ):
            _terminate_process_group(process)
        self.assertEqual(
            [(8123, signal.SIGTERM), (8123, signal.SIGKILL)],
            [call.args for call in killpg.call_args_list],
        )
        self.assertEqual(
            [signal.SIGTERM, signal.SIGKILL],
            [call.args[0] for call in process.send_signal.call_args_list],
        )
        process.wait.assert_called_once_with(timeout=1)

    def test_operational_check_cleanup_fails_if_process_survives(self) -> None:
        process = Mock(pid=8123)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("qa", 1)
        with patch(
            "codex_agents.learning_engine.os.killpg", side_effect=PermissionError
        ), patch(
            "codex_agents.learning_engine.time.sleep"
        ), self.assertRaisesRegex(ContractError, "process cleanup failed"):
            _terminate_process_group(process)

    def test_operational_check_pins_runtime_libraries(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            cellar = root / "Cellar"
            library = cellar / "openssl@3" / "3.6.3" / "lib" / "libssl.3.dylib"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"approved-library")
            script = root / "qa-check"
            script.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            script.chmod(0o700)
            check = {
                "check_id": "runtime-library",
                "driver": "python-wrapper",
                "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "runtime_libraries": [
                    {
                        "path": str(library),
                        "sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
                        "load_name": "libssl.dylib",
                    }
                ],
                "timeout_seconds": 5,
            }
            raw = json.dumps(
                {
                    "version": 1,
                    "deterministic_checks": [check],
                    "security_checks": [{**check, "check_id": "security"}],
                }
            ).encode()
            with patch(
                "codex_agents.learning_engine.HOMEBREW_CELLAR_ROOT", cellar
            ):
                normalized = validate_operational_check_configuration_input(root, raw)
            self.assertEqual(
                normalized["deterministic_checks"][0]["runtime_libraries"][0]["name"],
                "libssl.dylib",
            )

    def test_operational_check_grants_sysctl_read_only_to_ruff(self) -> None:
        identity = {"resolved": "/usr/bin/true", "interpreter": None}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            temporary = Path(directory)
            work = temporary / "work"
            work.mkdir()
            ruff = _sandboxed_check_argv(
                Path(directory), temporary, work, ["/usr/bin/true"], identity, "ruff",
                None,
            )
            python = _sandboxed_check_argv(
                Path(directory), temporary, work, ["/usr/bin/true"], identity,
                "python-wrapper",
                None,
            )
        self.assertIn("(allow sysctl-read)", ruff[2])
        self.assertNotIn("(allow sysctl-read)", python[2])
        self.assertIn("(allow file-read-metadata", python[2])

    def test_operational_check_runner_uses_only_approved_commands(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            config_root = root / ".codex" / "orchestration"
            config_root.mkdir(parents=True)
            scripts = root / "scripts"
            scripts.mkdir()
            passing = scripts / "qa-tests"
            failing = scripts / "qa-security"
            passing.write_text(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "expected = Path(os.environ['TMPDIR']) / 'pytest'\n"
                "expected.mkdir()\n"
                "target = expected / 'test-case0'\n"
                "target.mkdir()\n"
                "(expected / 'test-casecurrent').symlink_to(target)\n"
                "time.sleep(0.1)\n"
                "raise SystemExit(0 if Path(sys.argv[-1]) == expected else 2)\n",
                encoding="utf-8",
            )
            failing.write_text(
                f"#!{sys.executable}\nraise SystemExit(1)\n", encoding="utf-8"
            )
            passing.chmod(0o700)
            failing.chmod(0o700)
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [
                            {
                                "check_id": "tests",
                                "driver": "python-wrapper",
                                "argv": [
                                    "./scripts/qa-tests", "--basetemp",
                                    QA_WORK_DIRECTORY_TOKEN,
                                ],
                                "executable_sha256": hashlib.sha256(
                                    passing.read_bytes()
                                ).hexdigest(),
                                "interpreter_sha256": hashlib.sha256(
                                    Path(sys.executable).resolve().read_bytes()
                                ).hexdigest(),
                                "runtime_tree": None,
                                "timeout_seconds": 5,
                            }
                        ],
                        "security_checks": [
                            {
                                "check_id": "security",
                                "driver": "python-wrapper",
                                "argv": ["./scripts/qa-security"],
                                "executable_sha256": hashlib.sha256(
                                    failing.read_bytes()
                                ).hexdigest(),
                                "interpreter_sha256": hashlib.sha256(
                                    Path(sys.executable).resolve().read_bytes()
                                ).hexdigest(),
                                "runtime_tree": None,
                                "timeout_seconds": 5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "add", ".codex/orchestration/operational-checks.json", "scripts"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "Add approved checks",
                ],
                cwd=root,
                check=True,
            )
            report = run_operational_check_suite(root)
        self.assertTrue(report["deterministic_checks"][0]["passed"])
        self.assertFalse(report["security_checks"][0]["passed"])
        self.assertEqual(
            {"check_id", "passed", "evidence_digest"},
            set(report["security_checks"][0]),
        )

    def test_operational_check_snapshot_guard_invalidates_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            config_root = root / ".codex" / "orchestration"
            config_root.mkdir(parents=True)
            script = root / "qa-check"
            script.write_text(
                f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8"
            )
            script.chmod(0o700)
            check = {
                "check_id": "tests",
                "driver": "python-wrapper",
                "argv": ["./qa-check"],
                "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [{**check, "check_id": "security"}],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm",
                    "Add approved check",
                ],
                cwd=root,
                check=True,
            )

            def tamper(
                _root: Path,
                _temporary: Path,
                _work: Path,
                argv: list[str],
                _identity: dict,
                _driver: str,
                _runtime_library_directory: Path | None,
            ) -> list[str]:
                snapshot = Path(argv[1])
                snapshot.chmod(0o700)
                snapshot.write_text(
                    f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8"
                )
                return ["/usr/bin/true"]

            with patch(
                "codex_agents.learning_engine._sandboxed_check_argv",
                side_effect=tamper,
            ):
                report = run_operational_check_suite(root)
            self.assertFalse(report["deterministic_checks"][0]["passed"])
            self.assertFalse(report["security_checks"][0]["passed"])

    def test_operational_check_configuration_bytes_match_committed_blob(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            config_root = root / ".codex" / "orchestration"
            config_root.mkdir(parents=True)
            approved = root / "approved-check"
            approved.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8"
            )
            approved.chmod(0o700)
            check = {
                "check_id": "tests",
                "driver": "python-wrapper",
                "argv": ["./approved-check"],
                "executable_sha256": hashlib.sha256(
                    approved.read_bytes()
                ).hexdigest(),
                "interpreter_sha256": hashlib.sha256(
                    Path("/usr/bin/python3").read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            config = config_root / "operational-checks.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [{**check, "check_id": "security"}],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm",
                    "Add approved check",
                ],
                cwd=root,
                check=True,
            )
            load_operational_check_configuration(root)
            changed = json.loads(config.read_text())
            changed["deterministic_checks"][0]["argv"] = ["/tmp/transient"]
            transient = json.dumps(changed).encode()

            from codex_agents import learning_engine

            original_read = learning_engine.os.read
            supplied = False

            def transient_read(descriptor: int, size: int) -> bytes:
                nonlocal supplied
                if not supplied and size == 65_537:
                    supplied = True
                    return transient
                return original_read(descriptor, size)

            with patch(
                "codex_agents.learning_engine.os.read", side_effect=transient_read
            ), self.assertRaisesRegex(ContractError, "committed and clean"):
                load_operational_check_configuration(root)

    def test_operational_check_sandbox_denies_unrelated_authority(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory) / "project"
            root.mkdir()
            outside = Path(directory) / "outside-sentinel"
            outside.write_text("private", encoding="utf-8")
            config_root = root / ".codex/orchestration"
            config_root.mkdir(parents=True)
            script = root / "qa-boundary"
            script.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "import socket\n"
                "from pathlib import Path\n"
                "def denied(operation):\n"
                "    try:\n"
                "        operation()\n"
                "    except OSError:\n"
                "        return True\n"
                "    return False\n"
                f"outside = Path({str(outside)!r})\n"
                "if not denied(outside.read_bytes):\n"
                "    raise SystemExit(1)\n"
                "if not denied(lambda: (Path.cwd() / 'forbidden').write_text('x')):\n"
                "    raise SystemExit(2)\n"
                "Path('/dev/null').write_text('discarded')\n"
                "def bind_tcp():\n"
                "    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                "    try:\n"
                "        sock.bind(('127.0.0.1', 0))\n"
                "    finally:\n"
                "        sock.close()\n"
                "if not denied(bind_tcp):\n"
                "    raise SystemExit(3)\n"
                "unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "try:\n"
                "    unix_socket.bind(str(Path(os.environ['TMPDIR']) / 'qa.sock'))\n"
                "finally:\n"
                "    unix_socket.close()\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            check = {
                "check_id": "boundary",
                "driver": "python-wrapper",
                "argv": ["./qa-boundary"],
                "executable_sha256": digest,
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [
                            {**check, "check_id": "security-boundary"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "Add boundary check",
                ],
                cwd=root,
                check=True,
            )

            report = run_operational_check_suite(root)
            self.assertTrue(report["deterministic_checks"][0]["passed"])
            self.assertTrue(report["security_checks"][0]["passed"])
            self.assertFalse((root / "forbidden").exists())

    def test_operational_check_sandbox_preflight_is_available(self) -> None:
        self.assertTrue(operational_sandbox_preflight())

    def test_operational_check_sandbox_denies_fork(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config_root = root / ".codex/orchestration"
            config_root.mkdir(parents=True)
            script = root / "qa-no-fork"
            script.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "import subprocess\n"
                "try:\n"
                "    child = os.fork()\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    if child == 0:\n"
                "        os._exit(0)\n"
                "    os.waitpid(child, 0)\n"
                "    raise SystemExit(1)\n"
                "try:\n"
                "    subprocess.run(['/usr/bin/true'], check=False)\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    raise SystemExit(2)\n"
                "try:\n"
                "    child = os.posix_spawn('/usr/bin/true', ['/usr/bin/true'], {})\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    os.waitpid(child, 0)\n"
                "    raise SystemExit(3)\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            check = {
                "check_id": "no-fork",
                "driver": "python-wrapper",
                "argv": ["./qa-no-fork"],
                "executable_sha256": digest,
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [{**check, "check_id": "security-no-fork"}],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm",
                    "Add no-fork check",
                ],
                cwd=root,
                check=True,
            )

            report = run_operational_check_suite(root)
            self.assertTrue(report["deterministic_checks"][0]["passed"])
            self.assertTrue(report["security_checks"][0]["passed"])

    def test_operational_check_enforces_temporary_storage_bound(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory) / "project"
            root.mkdir()
            config_root = root / ".codex/orchestration"
            config_root.mkdir(parents=True)
            script = root / "qa-storage"
            script.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['TMPDIR'], 'output').write_bytes(b'x' * 4096)\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            check = {
                "check_id": "storage",
                "driver": "python-wrapper",
                "argv": ["./qa-storage"],
                "executable_sha256": digest,
                "interpreter_sha256": hashlib.sha256(
                    Path(sys.executable).resolve().read_bytes()
                ).hexdigest(),
                "runtime_tree": None,
                "timeout_seconds": 5,
            }
            (config_root / "operational-checks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [check],
                        "security_checks": [{**check, "check_id": "security-storage"}],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm",
                    "Add bounded-storage check",
                ],
                cwd=root,
                check=True,
            )

            with patch("codex_agents.learning_engine.MAX_QA_TEMP_BYTES", 1024):
                report = run_operational_check_suite(root)
            self.assertFalse(report["deterministic_checks"][0]["passed"])
            self.assertFalse(report["security_checks"][0]["passed"])

    def test_operational_check_rejects_directory_symlink_in_temporary_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            parent = Path(directory)
            work = parent / "work"
            outside = parent / "outside"
            work.mkdir()
            outside.mkdir()
            (work / "linked-directory").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(ContractError, "storage is invalid"):
                _bounded_tree_size(work)

    def test_operational_check_allows_only_proven_pytest_internal_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            work = Path(directory) / "work"
            pytest_root = work / "pytest"
            target = pytest_root / "pytest-0"
            outside = work / "outside"
            target.mkdir(parents=True)
            outside.mkdir()
            current = pytest_root / "pytest-current"
            current.symlink_to(target, target_is_directory=True)
            self.assertGreater(
                _bounded_tree_size(
                    work, allowed_symlink_root=pytest_root
                ),
                0,
            )
            current.unlink()
            file_target = pytest_root / "target-file"
            file_target.write_bytes(b"fixture")
            current.symlink_to(file_target)
            self.assertGreater(
                _bounded_tree_size(
                    work, allowed_symlink_root=pytest_root
                ),
                0,
            )
            current.unlink()
            current.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ContractError, "storage is invalid"):
                _bounded_tree_size(
                    work, allowed_symlink_root=pytest_root
                )

    def test_operational_qa_is_automatic_and_does_not_double_count_verifier_usage(self) -> None:
        inputs = verified_qa_inputs()
        inputs.pop("evaluation_assignment")
        inputs.pop("evaluation_case")
        inputs["host_outcome"]["evaluation_candidate_digest"] = None
        inputs["host_outcome"]["evaluation_case_digest"] = None
        inputs["verifier_assignment"]["context"] = [
            content_digest(inputs["host_outcome"])
        ]
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )
        qa, record = build_operational_qa_records(**inputs)
        self.assertTrue(qa["accepted"])
        self.assertTrue(record["accepted"])
        self.assertEqual(inputs["host_outcome"]["usage"], record["usage"])
        self.assertNotIn("deterministic_checks", qa)
        self.assertEqual(
            [content_digest(inputs["check_report"]["deterministic_checks"][0])],
            qa["deterministic_check_digests"],
        )
        inputs["evaluator_result"]["remaining_risks"] = ["Unresolved correctness risk"]
        inputs["host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["evaluator_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_result"])),
            }
        )
        inputs["verifier_assignment"]["context"] = [
            content_digest(inputs["host_outcome"])
        ]
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )
        risk_qa, risk_record = build_operational_qa_records(**inputs)
        self.assertFalse(risk_qa["accepted"])
        self.assertEqual("rejected", risk_record["status"])
        inputs["evaluator_result"]["remaining_risks"] = []
        inputs["host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["evaluator_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_result"])),
            }
        )
        inputs["verifier_assignment"]["context"] = [
            content_digest(inputs["host_outcome"])
        ]
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )
        inputs["verifier_assignment"]["required_evidence"][0]["description"] = (
            "A weaker verifier criterion"
        )
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )
        with self.assertRaisesRegex(ContractError, "independently bound"):
            build_operational_qa_records(**inputs)

    def test_operational_qa_admits_an_independently_verified_negative_verdict(self) -> None:
        inputs = verified_qa_inputs()
        inputs.pop("evaluation_assignment")
        inputs.pop("evaluation_case")
        inputs["host_outcome"]["evaluation_candidate_digest"] = None
        inputs["host_outcome"]["evaluation_case_digest"] = None
        for key in ("evaluator_result", "verifier_result"):
            value = inputs[key]
            value.update(
                execution_state="completed",
                semantic_verdict="not-ready",
                admission_state="unverified",
                retry={"attempt": 1, "retry_of_assignment_id": None},
                remaining_risks=["The reviewed criterion remains unsatisfied"],
            )
            value["evidence"][0]["outcome"] = "fail"
        for key in ("host_outcome", "verifier_host_outcome"):
            inputs[key].update(
                execution_state="completed",
                semantic_verdict="not-ready",
                admission_state="unverified",
                retry_attempt=1,
                retry_of_assignment_id=None,
            )
        inputs["host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["evaluator_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_result"])),
            }
        )
        inputs["verifier_assignment"]["context"] = [
            content_digest(inputs["host_outcome"])
        ]
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )

        qa, record = build_operational_qa_records(**inputs)

        self.assertTrue(qa["accepted"])
        self.assertTrue(record["accepted"])
        self.assertEqual("accepted", record["admission_state"])
        self.assertEqual("not-ready", record["semantic_verdict"])
        self.assertEqual(0, record["required_criteria_passed"])

    def test_verified_qa_binds_both_runs_checks_and_full_workflow_usage(self) -> None:
        qa, record = build_verified_qa_records(**verified_qa_inputs())
        self.assertTrue(qa["accepted"])
        self.assertTrue(record["accepted"])
        self.assertEqual(90, record["usage"]["input_tokens"])
        self.assertEqual(45, record["usage"]["output_tokens"])
        self.assertEqual(3, record["usage"]["model_calls"])
        self.assertEqual("fixture", qa["deterministic_checks"][0]["check_id"])
        self.assertEqual(
            keyed_digest(
                DIGEST_KEY,
                canonical_json(verified_qa_inputs()["evaluator_assignment"]),
            ),
            qa["evaluator_assignment_digest"],
        )

    def test_verified_qa_admits_an_independently_verified_negative_verdict(self) -> None:
        inputs = verified_qa_inputs()
        for key in ("evaluator_result", "verifier_result"):
            value = inputs[key]
            value.update(
                execution_state="completed",
                semantic_verdict="not-ready",
                admission_state="unverified",
                retry={"attempt": 1, "retry_of_assignment_id": None},
                remaining_risks=["The reviewed criterion remains unsatisfied"],
            )
            value["evidence"][0]["outcome"] = "fail"
        for key in ("host_outcome", "verifier_host_outcome"):
            inputs[key].update(
                execution_state="completed",
                semantic_verdict="not-ready",
                admission_state="unverified",
                retry_attempt=1,
                retry_of_assignment_id=None,
            )
        inputs["host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["evaluator_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["evaluator_result"])),
            }
        )
        inputs["verifier_assignment"]["context"] = [
            content_digest(inputs["host_outcome"])
        ]
        inputs["verifier_host_outcome"]["evidence_digests"] = sorted(
            {
                keyed_digest(
                    DIGEST_KEY, canonical_json(inputs["verifier_assignment"])
                ),
                keyed_digest(DIGEST_KEY, canonical_json(inputs["verifier_result"])),
            }
        )

        qa, record = build_verified_qa_records(**inputs)

        self.assertTrue(qa["accepted"])
        self.assertEqual("completed", qa["execution_state"])
        self.assertEqual("not-ready", qa["semantic_verdict"])
        self.assertEqual(0, qa["required_criteria_passed"])
        self.assertTrue(record["accepted"])
        self.assertEqual("not-ready", record["semantic_verdict"])

    def test_legacy_qa_evidence_without_separated_axes_remains_valid(self) -> None:
        qa, _ = build_verified_qa_records(**verified_qa_inputs())
        legacy = {
            key: value
            for key, value in qa.items()
            if key not in {"record_id", "execution_state", "semantic_verdict"}
        }
        legacy["record_id"] = f"qa_evidence:{content_digest(legacy)}"

        self.assertEqual(legacy, validate_learning_record(legacy))

    def test_qa_rejects_fabricated_assignments_results_and_reused_run(self) -> None:
        altered_assignment = verified_qa_inputs()
        altered_assignment["evaluator_assignment"]["context"] = [DIGEST_C]
        with self.assertRaisesRegex(ContractError, "evaluator result"):
            build_verified_qa_records(**altered_assignment)

        altered_result = verified_qa_inputs()
        altered_result["evaluator_result"]["summary"] = "different result"
        with self.assertRaisesRegex(ContractError, "evaluator result"):
            build_verified_qa_records(**altered_result)

        reused = verified_qa_inputs()
        reused["verifier_host_outcome"]["run_id"] = reused["host_outcome"]["run_id"]
        with self.assertRaisesRegex(ContractError, "independent verifier"):
            build_verified_qa_records(**reused)

    def test_failed_check_rejects_outcome_and_qa_must_follow_verifier(self) -> None:
        failed = verified_qa_inputs()
        failed["check_report"]["deterministic_checks"][0]["passed"] = False
        qa, record = build_verified_qa_records(**failed)
        self.assertFalse(qa["accepted"])
        self.assertEqual("rejected", record["status"])
        self.assertFalse(record["deterministic_pass"])

        early = verified_qa_inputs()
        early["check_report"]["observed_at_ms"] = 100
        with self.assertRaisesRegex(ContractError, "check time"):
            build_verified_qa_records(**early)

    def test_owner_signature_covers_exact_qa_evidence(self) -> None:
        qa, _ = build_verified_qa_records(**verified_qa_inputs())
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            private_key, public_key = generate_test_owner_key(root)
            allowed_signers = root / "allowed-signers"
            key_type, key_data = public_key.split()[:2]
            allowed_signers.write_text(
                f"codex-agents-owner {key_type} {key_data}\n"
            )
            allowed_signers.chmod(0o600)
            authority = OwnerGrantAuthority(allowed_signers)
            signature_path = sign_owner_payload(
                private_key,
                authority.qa_attestation_payload("prj_test", qa),
                "qa-payload",
            )
            try:
                signature = signature_path.read_bytes()
            finally:
                signature_path.unlink(missing_ok=True)
            self.assertTrue(
                authority.verify_qa_attestation("prj_test", qa, signature)
            )
            changed = deepcopy(qa)
            changed["observed_at_ms"] += 1
            material = {
                key: value for key, value in changed.items() if key != "record_id"
            }
            changed["record_id"] = f"qa_evidence:{content_digest(material)}"
            self.assertFalse(
                authority.verify_qa_attestation("prj_test", changed, signature)
            )


class OutcomeAndCaseTests(unittest.TestCase):
    def test_outcome_keeps_evidence_digest_and_discards_result_text(self) -> None:
        record = outcome()
        encoded = json.dumps(record)
        self.assertNotIn("sensitive result", encoded)
        self.assertNotIn("secret command", encoded)
        self.assertNotIn("secret output", encoded)
        self.assertEqual(1, record["required_criteria_passed"])

    def test_accepted_outcome_cannot_hide_a_failed_gate(self) -> None:
        with self.assertRaisesRegex(ContractError, "failed hard gate"):
            build_outcome(
                assignment=assignment(),
                result=result(),
                digest_key=DIGEST_KEY,
                run_id="run_observed",
                task_slice="coding-low-low",
                fixture_digest=DIGEST_A,
                topology="delegated",
                max_parallel=1,
                verification_required=True,
                verifier_digest=DIGEST_A,
                bounds_digest=DIGEST_B,
                package_digest=DIGEST_A,
                policy_digest=DIGEST_B,
                agent_digest=DIGEST_C,
                skill_digests=[DIGEST_A],
                prompt_digest=DIGEST_B,
                client_digest=DIGEST_C,
                deterministic_pass=False,
                security_pass=True,
                quality_score=1,
                repairs=0,
                repair_reason_codes=[],
                delegation_overhead_tokens=10,
                no_progress_events=0,
                terminal_failure=False,
                verifier_disagreement=False,
                accepted=True,
                coverage="end_to_end_complete",
                workflow_usage=full_usage(),
                observed_at_ms=1000,
            )

    def test_owner_feedback_uses_controlled_reason_and_digest(self) -> None:
        feedback = build_feedback(
            digest_key=DIGEST_KEY,
            run_id="run_1",
            label="corrected",
            reason_code="incorrect",
            correction="private correction",
            observed_at_ms=10,
        )
        self.assertNotIn("private correction", json.dumps(feedback))
        with self.assertRaisesRegex(ContractError, "unsupported"):
            build_feedback(
                digest_key=DIGEST_KEY,
                run_id="run_1",
                label="corrected",
                reason_code="free text reason",
                correction="x",
                observed_at_ms=10,
            )

    def test_mined_case_requires_review_before_holdout_and_freezes(self) -> None:
        proposed = mine_evaluation_case(
            digest_key=DIGEST_KEY,
            outcome=outcome(),
            fixture_digest=DIGEST_A,
            criteria_digest=DIGEST_B,
            lineage_digest=DIGEST_C,
        )
        self.assertEqual("development", proposed["corpus_state"])
        reviewed = review_evaluation_case(
            proposed,
            digest_key=DIGEST_KEY,
            review_digest="d" * 64,
            similarity_group_digest="e" * 64,
            destination="promotion_holdout",
            freeze=True,
        )
        self.assertTrue(reviewed["frozen"])
        with self.assertRaisesRegex(ContractError, "frozen"):
            review_evaluation_case(
                reviewed,
                digest_key=DIGEST_KEY,
                review_digest="e" * 64,
                similarity_group_digest="f" * 64,
                destination="canary",
                freeze=True,
            )

    def test_case_deduplication_detects_exact_and_lineage_groups(self) -> None:
        first = mine_evaluation_case(
            digest_key=DIGEST_KEY,
            outcome=outcome(),
            fixture_digest=DIGEST_A,
            criteria_digest=DIGEST_B,
            lineage_digest=DIGEST_C,
        )
        second = deepcopy(first)
        second["record_id"] = "case_second"
        first_reviewed = review_evaluation_case(
            first,
            digest_key=DIGEST_KEY,
            review_digest="d" * 64,
            similarity_group_digest="f" * 64,
            destination="promotion_holdout",
            freeze=True,
        )
        second_reviewed = review_evaluation_case(
            second,
            digest_key=DIGEST_KEY,
            review_digest="e" * 64,
            similarity_group_digest="f" * 64,
            destination="promotion_holdout",
            freeze=True,
        )
        result_value = deduplicate_cases([first_reviewed, second_reviewed])
        self.assertEqual(
            [second_reviewed["record_id"]], result_value["exact_duplicates"]
        )
        self.assertEqual(1, len(result_value["lineage_groups"]))
        self.assertEqual(1, len(result_value["similarity_groups"]))

    def test_analysis_keeps_unknown_usage_unknown_and_flags_small_sample(self) -> None:
        complete = outcome()
        missing = outcome(record_id="out_2", coverage="partial")
        missing["usage"] = {key: None for key in missing["usage"]}
        missing["usage_sources"] = {
            key: {"source": "unavailable", "coverage": "unknown"}
            for key in missing["usage"]
        }
        report_value = analyze_outcomes([complete, missing], as_of_ms=2000, max_age_ms=500)
        configurations = report_value["slices"]["coding-low-low"]["configurations"]
        self.assertEqual(1, len(configurations))
        section = next(iter(configurations.values()))
        self.assertIsNone(section["observations"]["workflow_token_volume"])
        self.assertTrue(section["evidence_flags"]["stale"])
        self.assertTrue(section["evidence_flags"]["insufficient_sample"])
        self.assertIn("telemetry_incomplete", section["diagnoses"])

    def test_analysis_reports_latency_delegation_no_progress_and_version_confounding(self) -> None:
        first = outcome()
        second = outcome(record_id="out_2")
        second["effort"] = "high"
        configuration = {
            key: second[key]
            for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                "client_digest",
            )
        }
        second["configuration_digest"] = content_digest(configuration)
        report_value = analyze_outcomes([first, second], as_of_ms=1000, max_age_ms=500)
        sections = report_value["slices"]["coding-low-low"]["configurations"].values()
        for section in sections:
            self.assertEqual(100, section["observations"]["workflow_latency_ms"])
            self.assertEqual(10, section["observations"]["delegation_overhead_tokens"])
            self.assertEqual(0, section["observations"]["no_progress_events"])
            self.assertTrue(section["evidence_flags"]["version_confounding"])
            self.assertIn("version_confounding", section["diagnoses"])

    def test_candidate_is_inert_and_protected_changes_fail(self) -> None:
        source = {"routing": "champion"}
        before = deepcopy(source)
        old_route = "route_champion"
        new_route = "route_candidate"
        manifest = [
            {
                "path_code": "routing.selection",
                "before_digest": content_digest(old_route),
                "after_digest": content_digest(new_route),
                "before_value": old_route,
                "after_value": new_route,
            }
        ]
        record = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-low-low",
            target="routing",
            effect="optimize",
            base_digest=candidate_base_digest(champion_configuration()),
            champion_configuration=champion_configuration(),
            change_manifest=manifest,
            evidence_digests=[DIGEST_C],
        )
        self.assertEqual(before, source)
        self.assertEqual("proposed", validate_candidate(record)["status"])
        with self.assertRaisesRegex(ContractError, "unsupported"):
            generate_candidate(
                digest_key=DIGEST_KEY,
                scope="general",
                task_slice="coding-low-low",
                target="routing",
                effect="optimize",
                base_digest=DIGEST_A,
                champion_configuration=champion_configuration(),
                change_manifest=[
                    {
                        "path_code": "authority.tools",
                        "before_digest": DIGEST_A,
                        "after_digest": DIGEST_B,
                        "before_value": "route_champion",
                        "after_value": "route_candidate",
                    }
                ],
                evidence_digests=[DIGEST_C],
            )

    def test_candidate_exact_value_and_monotonic_bounds_are_enforced(self) -> None:
        before = {"max_attempts": 2, "max_tool_calls": 30}
        expanded = {"max_attempts": 3, "max_tool_calls": 30}
        with self.assertRaisesRegex(ContractError, "only retain or reduce"):
            generate_candidate(
                digest_key=DIGEST_KEY,
                scope="general",
                task_slice="coding-low-low",
                target="bounds",
                effect="narrow",
                base_digest=DIGEST_A,
                champion_configuration=champion_configuration(),
                change_manifest=[{
                    "path_code": "bounds.limits",
                    "before_digest": content_digest(before),
                    "after_digest": content_digest(expanded),
                    "before_value": before,
                    "after_value": expanded,
                }],
                evidence_digests=[DIGEST_C],
            )
        substituted = candidate()
        substituted["change_manifest"][0]["after_value"]["effort"] = "xhigh"
        with self.assertRaisesRegex(ContractError, "after_digest"):
            validate_candidate(substituted)
        wrong_base = candidate()
        wrong_base["base_digest"] = DIGEST_A
        with self.assertRaisesRegex(ContractError, "champion configuration"):
            validate_candidate(wrong_base)

        widened = {**champion_configuration(), "topology": "single"}
        with self.assertRaisesRegex(ContractError, "only narrow"):
            generate_candidate(
                digest_key=DIGEST_KEY,
                scope="general",
                task_slice="coding-medium-low",
                target="topology",
                effect="narrow",
                base_digest=candidate_base_digest(widened),
                champion_configuration=widened,
                change_manifest=[{
                    "path_code": "orchestration.topology",
                    "before_digest": content_digest("single"),
                    "after_digest": content_digest("parallel"),
                    "before_value": "single",
                    "after_value": "parallel",
                }],
                evidence_digests=[DIGEST_C],
            )

    def test_candidate_replay_binds_policy_and_rejects_unobserved_targets(self) -> None:
        base_route = {
            "model": "gpt-6-sol",
            "effort": "medium",
            "role": "codex_agents_implementation_worker",
            "topology": "single",
            "max_parallel": 1,
            "bounds": {},
            "reason": "fixture",
        }
        champion = champion_configuration()
        policy = load_policy(ORCHESTRATION_ROOT / "config/routing-policy.json")
        with self.assertRaisesRegex(ContractError, "complete execution configuration"):
            apply_candidate_to_route(
                base_route,
                candidate(),
                {**champion, "policy_digest": "d" * 64},
                policy,
            )

        model_route = apply_candidate_to_route(
            base_route, candidate(), champion, policy
        )
        self.assertEqual("gpt-6-luna", model_route["model"])

        astra_after = {"model": "gpt-6-astra", "effort": "medium"}
        astra_candidate = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-medium-low",
            target="model_effort",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(
                    {"model": champion["model"], "effort": champion["effort"]}
                ),
                "after_digest": content_digest(astra_after),
                "before_value": {
                    "model": champion["model"], "effort": champion["effort"]
                },
                "after_value": astra_after,
            }],
            evidence_digests=[DIGEST_C],
        )
        with self.assertRaisesRegex(ContractError, "hard end-to-end"):
            apply_candidate_to_route(
                base_route, astra_candidate, champion, policy
            )

        restricted_policy = deepcopy(policy)
        restricted_policy["models"].pop("gpt-6-luna")
        with self.assertRaisesRegex(ContractError, "installed policy"):
            apply_candidate_to_route(
                base_route, candidate(), champion, restricted_policy
            )

        unsupported_after = {"model": "gpt-6-luna", "effort": "ultra"}
        unsupported_candidate = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-medium-low",
            target="model_effort",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "routing.model_effort",
                "before_digest": content_digest(
                    {"model": champion["model"], "effort": champion["effort"]}
                ),
                "after_digest": content_digest(unsupported_after),
                "before_value": {
                    "model": champion["model"], "effort": champion["effort"]
                },
                "after_value": unsupported_after,
            }],
            evidence_digests=[DIGEST_C],
        )
        with self.assertRaisesRegex(ContractError, "installed policy"):
            apply_candidate_to_route(
                base_route, unsupported_candidate, champion, policy
            )

        skill_after = [DIGEST_B]
        skill_candidate = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-medium-low",
            target="skill",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "skills.selection",
                "before_digest": content_digest(champion["skill_digests"]),
                "after_digest": content_digest(skill_after),
                "before_value": champion["skill_digests"],
                "after_value": skill_after,
            }],
            evidence_digests=[DIGEST_C],
        )
        with self.assertRaisesRegex(ContractError, "host-observed"):
            apply_candidate_to_route(
                base_route, skill_candidate, champion, policy
            )
        with self.assertRaisesRegex(ContractError, "host-observed"):
            create_evaluation_assignment(
                candidate=skill_candidate,
                cases=heldout_cases(20),
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                observed_at_ms=1,
            )

        prompt_candidate = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-medium-low",
            target="prompt",
            effect="optimize",
            base_digest=candidate_base_digest(champion),
            champion_configuration=champion,
            change_manifest=[{
                "path_code": "prompts.template_digest",
                "before_digest": content_digest(champion["prompt_digest"]),
                "after_digest": content_digest("d" * 64),
                "before_value": champion["prompt_digest"],
                "after_value": "d" * 64,
            }],
            evidence_digests=[DIGEST_C],
        )
        with self.assertRaisesRegex(ContractError, "host-observed"):
            apply_candidate_to_route(
                base_route, prompt_candidate, champion, policy
            )

        parallel = {**champion, "topology": "parallel"}
        topology_candidate = generate_candidate(
            digest_key=DIGEST_KEY,
            scope="general",
            task_slice="coding-medium-low",
            target="topology",
            effect="narrow",
            base_digest=candidate_base_digest(parallel),
            champion_configuration=parallel,
            change_manifest=[{
                "path_code": "orchestration.topology",
                "before_digest": content_digest("parallel"),
                "after_digest": content_digest("single"),
                "before_value": "parallel",
                "after_value": "single",
            }],
            evidence_digests=[DIGEST_C],
        )
        parallel_route = {**base_route, "topology": "parallel", "max_parallel": 4}
        with self.assertRaisesRegex(ContractError, "host-observed"):
            apply_candidate_to_route(
                parallel_route, topology_candidate, parallel, policy
            )


class InstalledEvidenceTests(unittest.TestCase):
    def test_project_agent_snapshot_uses_discovered_path_not_internal_name(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            bundle = Path(directory) / "bundle"
            shutil.copytree(ORCHESTRATION_ROOT.parent.parent, bundle)
            agent = bundle / ".codex/agents/technology-researcher.toml"
            agent.write_text(
                'name = "technology_researcher"\n'
                'description = "Project researcher"\n'
                'sandbox_mode = "read-only"\n'
                'developer_instructions = "Research the bounded question."\n'
                'mcp_servers = []\n'
                'skills = []\n',
                encoding="utf-8",
            )
            mapping = bundle / ".codex/orchestration/project-agent-routing.json"
            mapping.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roles": {"research": "technology_researcher"},
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(bundle)], check=True)
            subprocess.run(
                ["git", "-C", str(bundle), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(bundle), "config", "user.name", "Learning Tests"],
                check=True,
            )
            subprocess.run(["git", "-C", str(bundle), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(bundle), "commit", "-qm", "project route"],
                check=True,
            )
            policy = load_effective_policy(
                bundle,
                bundle / ".codex/orchestration/config/routing-policy.json",
                bundle / ".codex/orchestration/config/discovery-policy.json",
            )
            route = route_task(
                {
                    "id": "hyphenated-project-agent",
                    "objective": "Research one bounded question",
                    "family": "research",
                    "complexity": "medium",
                    "risk": "low",
                    **routing_fields(),
                    "meaningful": True,
                    "deterministic_validation": True,
                    "independent_parts": 1,
                    "parallel_safe": False,
                    "failed_attempts": 0,
                    "failure_events": [],
                },
                policy,
            )

            configuration = installed_execution_configuration(
                bundle, route, client_capability=END_TO_END_CAPABILITY
            )

            self.assertEqual(
                ".codex/agents/technology-researcher.toml", route["agent_path"]
            )
            self.assertEqual(
                hashlib.sha256(agent.read_bytes()).hexdigest(),
                configuration["agent_digest"],
            )

    def test_package_digest_ignores_unmanaged_files_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            bundle = Path(directory) / "bundle"
            shutil.copytree(ORCHESTRATION_ROOT.parent.parent, bundle)
            task = {
                "id": "package-fixture",
                "objective": "Check installed package evidence",
                "family": "coding",
                "complexity": "medium",
                "risk": "low",
                **routing_fields(operation="implementation"),
                "meaningful": True,
                "deterministic_validation": True,
                "independent_parts": 1,
                "parallel_safe": False,
                "failed_attempts": 0,
                "failure_events": [],
            }
            policy_path = bundle / ".codex/orchestration/config/routing-policy.json"
            route = route_task(task, load_policy(policy_path))
            first = installed_execution_configuration(
                bundle, route, client_capability=END_TO_END_CAPABILITY
            )
            (bundle / "unmanaged.txt").write_text("unrelated")
            second = installed_execution_configuration(
                bundle, route, client_capability=END_TO_END_CAPABILITY
            )
            self.assertEqual(first["package_digest"], second["package_digest"])

            agent = bundle / ".codex/agents/codex_agents_implementation_worker.toml"
            target = Path(directory) / "outside-agent.toml"
            target.write_bytes(agent.read_bytes())
            agent.unlink()
            agent.symlink_to(target)
            with self.assertRaisesRegex(ContractError, "symlink"):
                installed_execution_configuration(
                    bundle, route, client_capability=END_TO_END_CAPABILITY
                )

    def test_release_registry_is_unsigned_only_when_empty(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            registry = Path(directory) / "learning-releases.json"
            registry.write_text(json.dumps({
                "version": 1,
                "candidate_digests": [],
                "signature_b64": None,
            }))
            self.assertEqual(frozenset(), load_released_candidate_digests(registry))
            registry.write_text(json.dumps({
                "version": 1,
                "candidate_digests": [DIGEST_A],
                "signature_b64": None,
            }))
            with self.assertRaisesRegex(ContractError, "owner authenticated"):
                load_released_candidate_digests(registry)


class ComparisonTests(unittest.TestCase):
    def test_matched_quality_preserving_efficiency_promotes(self) -> None:
        case_ids, trials = matched_trials()
        report = compare_candidate(
            candidate=candidate(),
            trials=trials,
            cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
            bootstrap_samples=1000,
        )
        self.assertEqual("promote", report["decision"])
        self.assertEqual(["tokens"], report["metrics"]["proven_improvements"])

    def test_case_level_quality_regression_rejects(self) -> None:
        case_ids, trials = matched_trials()
        trials[1]["quality_micros"] = 999_999
        refresh_trial(trials[1])
        report = compare_candidate(
            candidate=candidate(), trials=trials, cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        self.assertEqual("reject", report["decision"])
        self.assertIn("case_quality_regression", report["reasons"])

    def test_absolute_quality_and_required_criteria_are_hard_gates(self) -> None:
        case_ids, trials = matched_trials()
        for item in trials:
            item["required_criteria_pass"] = False
            item["quality_micros"] = 800_000
            refresh_trial(item)
        report = compare_candidate(
            candidate=candidate(), trials=trials, cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        self.assertEqual("reject", report["decision"])
        self.assertIn("required_criteria", report["reasons"])
        self.assertIn("absolute_quality", report["reasons"])

    def test_missing_case_usage_and_partial_coverage_are_insufficient(self) -> None:
        case_ids, trials = matched_trials()
        self.assertEqual(
            "insufficient_evidence",
            compare_candidate(
                candidate=candidate(), trials=trials[:-1], cases=case_ids,
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                independent_review_digest=DIGEST_C,
            )["decision"],
        )
        trials[-1]["coverage"] = "partial"
        refresh_trial(trials[-1])
        self.assertEqual(
            "insufficient_evidence",
            compare_candidate(
                candidate=candidate(), trials=trials, cases=case_ids,
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                independent_review_digest=DIGEST_C,
            )["decision"],
        )

    def test_post_assignment_exclusion_is_not_allowed(self) -> None:
        case_ids, trials = matched_trials()
        trials[0]["assigned"] = False
        refresh_trial(trials[0])
        report = compare_candidate(
            candidate=candidate(), trials=trials, cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        self.assertEqual("insufficient_evidence", report["decision"])
        self.assertIn("population_incomplete", report["reasons"])

    def test_usage_regression_and_unproven_saving_do_not_promote(self) -> None:
        case_ids, trials = matched_trials()
        for item in trials:
            if item["cohort"] == "candidate":
                item["input_tokens"] = 101
                refresh_trial(item)
        self.assertEqual(
            "reject",
            compare_candidate(
                candidate=candidate(), trials=trials, cases=case_ids,
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                independent_review_digest=DIGEST_C,
            )["decision"],
        )
        case_ids, trials = matched_trials()
        for index, item in enumerate(trials):
            if item["cohort"] == "candidate":
                item["input_tokens"] = 0 if index < 20 else 180
                item["output_tokens"] = 0 if index < 20 else 90
                refresh_trial(item)
        report = compare_candidate(
            candidate=candidate(),
            trials=trials,
            cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
            bootstrap_samples=1000,
        )
        self.assertNotEqual("promote", report["decision"])

    def test_subjective_grader_needs_owner_calibration(self) -> None:
        labels = [{"owner": "a", "grader": "a", "swapped": "b"} for _ in range(30)]
        calibration = calibrate_grader(
            labels, configuration=GRADER_CONFIG, observed_at_ms=100
        )
        self.assertTrue(calibration["ready"])
        biased = [
            {"owner": "a", "grader": "b" if index < 4 else "a", "swapped": "b"}
            for index in range(30)
        ]
        failed_calibration = calibrate_grader(
            biased, configuration=GRADER_CONFIG, observed_at_ms=100
        )
        self.assertFalse(failed_calibration["ready"])
        case_ids, trials = matched_trials()
        self.assertEqual(
            "insufficient_evidence",
            compare_candidate(
                candidate=candidate(),
                trials=trials,
                cases=case_ids,
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                independent_review_digest=DIGEST_C,
                subjective_grading_required=True,
                grader_calibration=failed_calibration,
            )["decision"],
        )
        with self.assertRaisesRegex(ContractError, "missing required fields"):
            compare_candidate(
                candidate=candidate(), trials=trials, cases=case_ids,
                client_version=FIXTURE_CLIENT_VERSION,
                client_capability=END_TO_END_CAPABILITY,
                independent_review_digest=DIGEST_C,
                subjective_grading_required=True,
                grader_calibration={"ready": True},
            )

    def test_configuration_change_requires_a_new_matched_cohort(self) -> None:
        case_ids, trials = matched_trials()
        trials[1]["configuration_digest"] = "f" * 64
        refresh_trial(trials[1])
        report = compare_candidate(
            candidate=candidate(), trials=trials, cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        self.assertEqual("insufficient_evidence", report["decision"])
        self.assertIn("configuration_mismatch", report["reasons"])

        client_changed = matched_trials()[1]
        client_changed[0]["client_version"] = "codex-cli-0.999.0"
        refresh_trial(client_changed[0])
        client_report = compare_candidate(
            candidate=candidate(), trials=client_changed, cases=case_ids,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        self.assertEqual("insufficient_evidence", client_report["decision"])
        self.assertIn("client_coverage", client_report["reasons"])


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        owner_root = Path(self.temporary.name)
        self.private_key, public_key = generate_test_owner_key(owner_root)
        allowed_signers = owner_root / "allowed-signers"
        key_type, key_data = public_key.split()[:2]
        allowed_signers.write_text(
            f"codex-agents-owner {key_type} {key_data}\n"
        )
        allowed_signers.chmod(0o600)
        self.authority = OwnerGrantAuthority(allowed_signers)
        proposed = candidate()
        self.lifecycle = CandidateLifecycle(
            candidate=proposed,
            champion_digest=proposed["base_digest"],
            project_id="prj_test",
            authority=self.authority,
            digest_key=DIGEST_KEY,
        )
        cases, trials = matched_trials()
        comparison = compare_candidate(
            candidate=self.lifecycle.candidate,
            trials=trials,
            cases=cases,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
            bootstrap_samples=1000,
        )
        self.review_digest = content_digest(comparison)
        self.lifecycle.mark_evaluated(
            comparison, evidence_digest=self.review_digest, now_ms=99
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def grant(self, kind: str, *, candidate_digest_value: str | None = None, nonce: bytes) -> dict:
        return sign_owner_grant(
            self.authority,
            self.private_key,
            digest_key=DIGEST_KEY,
            kind=kind,
            project_id="prj_test",
            candidate_digest_value=candidate_digest_value
            or self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            review_digest=self.review_digest,
            bounds_digest=content_digest([DIGEST_B]),
            issued_at_ms=100,
            expires_at_ms=1000,
            nonce=nonce,
        )

    def test_path_cannot_replace_owner_signature_verifier(self) -> None:
        fake_bin = Path(self.temporary.name) / "fake-bin"
        fake_bin.mkdir()
        fake = fake_bin / "ssh-keygen"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        request = self.authority.request(
            digest_key=DIGEST_KEY,
            kind="canary",
            project_id="prj_test",
            candidate_digest_value=self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            review_digest=self.review_digest,
            bounds_digest=content_digest([DIGEST_B]),
            issued_at_ms=100,
            expires_at_ms=1000,
            nonce=b"x" * 32,
        )
        previous = os.environ.get("PATH")
        os.environ["PATH"] = str(fake_bin)
        try:
            with self.assertRaisesRegex(ContractError, "signature is invalid"):
                self.authority.attach_signature(request, b"not-a-signature")
        finally:
            if previous is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous

    def test_two_distinct_grants_canary_promotion_and_exact_rollback(self) -> None:
        canary = self.grant("canary", nonce=b"c" * 32)
        self.lifecycle.start_canary(
            grant=canary,
            case_digests=[DIGEST_B],
            evidence_digest=self.review_digest,
            now_ms=200,
            commit=lambda _grant, _transition: None,
        )
        self.assertEqual(
            self.lifecycle.candidate["candidate_digest"],
            self.lifecycle.resolve_digest(DIGEST_B),
        )
        self.assertEqual(
            self.lifecycle.champion_digest, self.lifecycle.resolve_digest("e" * 64)
        )
        canary_report = build_canary_report(
            candidate_digest=self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            case_results=canary_results(
                self.lifecycle.champion_digest,
                self.lifecycle.candidate["candidate_digest"],
            ),
            comparison_digest=DIGEST_C,
            independent_review_digest=DIGEST_C,
            observed_at_ms=300,
        )
        self.lifecycle.complete_canary(report=canary_report, now_ms=300)
        self.review_digest = content_digest(canary_report)
        promotion = self.grant("promotion", nonce=b"p" * 32)
        self.lifecycle.promote(
            grant=promotion,
            evidence_digest=self.review_digest,
            now_ms=400,
            commit=lambda _grant, _transition: None,
        )
        self.assertEqual("promoted", self.lifecycle.status)
        self.lifecycle.rollback(
            active_digest=self.lifecycle.candidate["candidate_digest"],
            evidence_digest=DIGEST_C,
            now_ms=500,
        )
        self.assertEqual("reverted", self.lifecycle.status)
        self.assertEqual(
            self.lifecycle.champion_digest, self.lifecycle.resolve_digest(DIGEST_B)
        )
        self.assertEqual(
            ["evaluated", "canary_started", "canary_passed", "promoted", "reverted"],
            [record["action"] for record in self.lifecycle.audit],
        )

    def test_digest_substitution_grant_reuse_and_wrong_kind_fail(self) -> None:
        wrong = self.grant(
            "canary", candidate_digest_value="f" * 64, nonce=b"w" * 32
        )
        with self.assertRaisesRegex(ContractError, "does not match"):
            self.lifecycle.start_canary(
                grant=wrong,
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=200,
                commit=lambda _grant, _transition: None,
            )
        promotion = self.grant("promotion", nonce=b"p" * 32)
        with self.assertRaisesRegex(ContractError, "kind"):
            self.lifecycle.start_canary(
                grant=promotion,
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=200,
                commit=lambda _grant, _transition: None,
            )
        wrong_bounds = sign_owner_grant(
            self.authority,
            self.private_key,
            digest_key=DIGEST_KEY,
            kind="canary",
            project_id="prj_test",
            candidate_digest_value=self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            review_digest=self.review_digest,
            bounds_digest="f" * 64,
            issued_at_ms=100,
            expires_at_ms=1000,
            nonce=b"b" * 32,
        )
        with self.assertRaisesRegex(ContractError, "bounds"):
            self.lifecycle.start_canary(
                grant=wrong_bounds,
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=200,
                commit=lambda _grant, _transition: None,
            )
        invalid_signature = self.grant("canary", nonce=b"i" * 32)
        invalid_signature["signature_b64"] = base64.b64encode(
            b"not-an-ssh-signature"
        ).decode("ascii")
        invalid_material = {
            key: value
            for key, value in invalid_signature.items()
            if key != "record_id"
        }
        invalid_signature["record_id"] = "grant:" + content_digest(invalid_material)
        with self.assertRaisesRegex(ContractError, "signature"):
            self.lifecycle.start_canary(
                grant=invalid_signature,
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=200,
                commit=lambda _grant, _transition: None,
            )
        canary = self.grant("canary", nonce=b"c" * 32)
        self.lifecycle.start_canary(
            grant=canary,
            case_digests=[DIGEST_B],
            evidence_digest=self.review_digest,
            now_ms=200,
            commit=lambda _grant, _transition: None,
        )
        self.lifecycle._status = "evaluated"
        with self.assertRaisesRegex(ContractError, "already used"):
            self.lifecycle.start_canary(
                grant=canary,
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=201,
                commit=lambda _grant, _transition: None,
            )

    def test_failed_canary_cannot_promote_and_rollback_cannot_overwrite_other_state(self) -> None:
        self.lifecycle.start_canary(
            grant=self.grant("canary", nonce=b"c" * 32),
            case_digests=[DIGEST_B],
            evidence_digest=self.review_digest,
            now_ms=200,
            commit=lambda _grant, _transition: None,
        )
        failed_report = build_canary_report(
            candidate_digest=self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            case_results=canary_results(
                self.lifecycle.champion_digest,
                self.lifecycle.candidate["candidate_digest"],
                candidate_pass=False,
            ),
            comparison_digest=DIGEST_C,
            independent_review_digest=DIGEST_C,
            observed_at_ms=300,
        )
        self.lifecycle.complete_canary(report=failed_report, now_ms=300)
        with self.assertRaisesRegex(ContractError, "successful canary"):
            self.lifecycle.promote(
                grant=self.grant("promotion", nonce=b"p" * 32),
                evidence_digest=DIGEST_C,
                now_ms=400,
                commit=lambda _grant, _transition: None,
            )

    def test_failed_durable_commit_cannot_activate_canary(self) -> None:
        def fail_commit(_grant: dict, _transition: dict) -> None:
            raise RuntimeError("durable write failed")

        with self.assertRaisesRegex(RuntimeError, "durable write failed"):
            self.lifecycle.start_canary(
                grant=self.grant("canary", nonce=b"z" * 32),
                case_digests=[DIGEST_B],
                evidence_digest=self.review_digest,
                now_ms=200,
                commit=fail_commit,
            )
        self.assertEqual("evaluated", self.lifecycle.status)
        self.assertEqual(
            self.lifecycle.champion_digest, self.lifecycle.resolve_digest(DIGEST_B)
        )
        self.assertEqual(set(), self.lifecycle.used_grants)

    def test_later_rejecting_comparison_fails_back_and_records_rollback(self) -> None:
        self.lifecycle.start_canary(
            grant=self.grant("canary", nonce=b"d" * 32),
            case_digests=[DIGEST_B],
            evidence_digest=self.review_digest,
            now_ms=200,
            commit=lambda _grant, _transition: None,
        )
        report = build_canary_report(
            candidate_digest=self.lifecycle.candidate["candidate_digest"],
            champion_digest=self.lifecycle.champion_digest,
            case_results=canary_results(
                self.lifecycle.champion_digest,
                self.lifecycle.candidate["candidate_digest"],
            ),
            comparison_digest=DIGEST_C,
            independent_review_digest=DIGEST_C,
            observed_at_ms=300,
        )
        self.lifecycle.complete_canary(report=report, now_ms=300)
        self.review_digest = content_digest(report)
        self.lifecycle.promote(
            grant=self.grant("promotion", nonce=b"e" * 32),
            evidence_digest=self.review_digest,
            now_ms=400,
            commit=lambda _grant, _transition: None,
        )
        cases, trials = matched_trials()
        trials[1]["quality_micros"] = 999_999
        refresh_trial(trials[1])
        drift = compare_candidate(
            candidate=self.lifecycle.candidate,
            trials=trials,
            cases=cases,
            client_version=FIXTURE_CLIENT_VERSION,
            client_capability=END_TO_END_CAPABILITY,
            independent_review_digest=DIGEST_C,
        )
        drift_digest = content_digest(drift)
        detected = self.lifecycle.record_drift(
            comparison=drift, evidence_digest=drift_digest, now_ms=500
        )
        self.assertEqual("drift_detected", detected["action"])
        self.assertEqual(
            self.lifecycle.champion_digest, self.lifecycle.resolve_digest(DIGEST_B)
        )
        rolled_back = self.lifecycle.rollback(
            active_digest=self.lifecycle.candidate["candidate_digest"],
            evidence_digest=drift_digest,
            now_ms=501,
        )
        self.assertEqual("reverted", rolled_back["action"])


if __name__ == "__main__":
    unittest.main()
