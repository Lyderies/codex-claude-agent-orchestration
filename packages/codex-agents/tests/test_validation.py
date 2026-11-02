from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT

from codex_agents.validation import validate_package


class PackageValidationTests(unittest.TestCase):
    def test_native_package_is_ready(self) -> None:
        report = validate_package(BUNDLE_ROOT)
        self.assertTrue(report["package_ready"], report)
        self.assertFalse(report["project_ready"], report)
        self.assertFalse(report["ready"], report)

    def test_package_only_validation_exits_on_package_readiness(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ORCHESTRATION_ROOT / "cli.py"),
                "validate",
                "--root",
                str(BUNDLE_ROOT),
                "--package-only",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        report = json.loads(completed.stdout)
        self.assertTrue(report["package_ready"])
        self.assertFalse(report["project_ready"])
        self.assertFalse(report["ready"])

    def test_package_ships_no_hooks_file_and_ignores_the_rendered_one(self) -> None:
        # The installer renders .codex/hooks.json for each checkout (hook_config); the
        # package ships only the .gitignore that keeps it and its record out of Git.
        self.assertFalse((BUNDLE_ROOT / ".codex" / "hooks.json").exists())
        ignored = (BUNDLE_ROOT / ".codex" / ".gitignore").read_text().splitlines()
        self.assertIn("/hooks.json", ignored)
        self.assertIn("/orchestration/local-install.json", ignored)
        report = validate_package(BUNDLE_ROOT)
        self.assertIn(
            "learning hooks are not installed in this checkout; run ./setup.sh and choose "
            "to update the install",
            report["project_errors"],
        )
        self.assertFalse(any("hook" in item for item in report["package_errors"]))

    def test_every_package_agent_requires_literal_result_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            agent = root / ".codex" / "agents" / "codex_agents_researcher.toml"
            agent.write_text(
                agent.read_text().replace(
                    '{"assignment_id":"<assignment_id>"', "{}"
                )
            )

            report = validate_package(root)

            self.assertFalse(report["ready"])
            self.assertIn(
                ".codex/agents/codex_agents_researcher.toml: "
                "packaged result template is required",
                report["errors"],
            )

    def test_contract_schemas_require_runtime_semantic_parity(self) -> None:
        cases = [
            (
                "task.schema.json",
                lambda data: data.update(allOf=[]),
                "failure history schema lacks runtime semantic parity",
            ),
            (
                "learning-host-observation.schema.json",
                lambda data: data.update(oneOf=[]),
                "result-attempt schema lacks runtime semantic parity",
            ),
            (
                "learning-outcome.schema.json",
                lambda data: data.update(oneOf=[]),
                "repair schema lacks runtime semantic parity",
            ),
        ]
        for filename, mutate, expected in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "target"
                shutil.copytree(BUNDLE_ROOT, root)
                path = root / ".codex" / "orchestration" / "schemas" / filename
                data = json.loads(path.read_text())
                mutate(data)
                path.write_text(json.dumps(data))

                report = validate_package(root)

                self.assertFalse(report["ready"])
                self.assertTrue(
                    any(expected in error for error in report["errors"]),
                    report,
                )

    def test_deleted_committed_provider_configuration_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            path = root / ".codex/orchestration/provider-routing.json"
            path.write_text(json.dumps({
                "version": 1,
                "providers": {"codex": {"enabled": False}, "claude": {"enabled": False}},
                "codex_primary_may_bridge_to_claude": False,
            }))
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Validation Tests"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "provider config"], check=True
            )
            path.unlink()
            report = validate_package(root)
            self.assertFalse(report["project_ready"], report)
            self.assertIn(
                "project provider routing is committed but missing from the worktree",
                report["project_errors"],
            )

    def test_outside_claude_symlink_does_not_block_codex_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            shared = Path(directory) / "shared-agent.md"
            shared.write_text(
                "---\nname: shared\ndescription: Shared\ntools: Read\n---\n\nRead.\n"
            )
            agents = root / ".claude/agents"
            agents.mkdir(parents=True)
            (agents / "shared.md").symlink_to(shared)
            report = validate_package(root)
            self.assertTrue(report["package_ready"], report)
            self.assertFalse(
                any("claude" in error.lower() for error in report["errors"]), report
            )
            self.assertIn("agent:shared: Claude agent is a symlink", report["warnings"])

    def test_project_agents_and_skills_can_coexist_with_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            agent = root / ".codex" / "agents" / "project-writer.toml"
            agent.write_text(
                'name = "project_writer"\n'
                'description = "Project-owned implementation role."\n'
                'sandbox_mode = "workspace-write"\n'
                'developer_instructions = "Use only project-approved authority."\n'
            )
            skill = root / ".agents" / "skills" / "project-skill" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\n"
                "name: project-skill\n"
                "description: Project-owned instructions.\n"
                "---\n\n"
                "# Project skill\n"
            )

            report = validate_package(root)

            self.assertTrue(report["package_ready"], report)
            self.assertFalse(report["project_ready"], report)
            self.assertIn(
                "agent:project_writer: file stem differs from agent name",
                report["warnings"],
            )

            policy_path = (
                root
                / ".codex"
                / "orchestration"
                / "config"
                / "routing-policy.json"
            )
            policy = json.loads(policy_path.read_text())
            policy["roles"]["coding"] = "project_writer"
            policy_path.write_text(json.dumps(policy))

            changed = validate_package(root)

            self.assertFalse(changed["ready"])
            self.assertIn(
                "routing policy role mapping must match package policy",
                changed["errors"],
            )

            policy["roles"]["coding"] = "codex_agents_security_reviewer"
            policy["roles"]["security"] = "codex_agents_implementation_worker"
            policy_path.write_text(json.dumps(policy))

            swapped = validate_package(root)

            self.assertFalse(swapped["ready"])
            self.assertIn(
                "routing policy role mapping must match package policy",
                swapped["errors"],
            )

            policy["roles"]["coding"] = "codex_agents_implementation_worker"
            policy["roles"]["security"] = "codex_agents_security_reviewer"
            policy["verification"]["role"] = "codex_agents_implementation_worker"
            policy_path.write_text(json.dumps(policy))

            wrong_verifier = validate_package(root)

            self.assertFalse(wrong_verifier["ready"])
            self.assertIn(
                "verification role must match package verifier",
                wrong_verifier["errors"],
            )

    def test_smoke_routes_ignore_a_workspace_write_project_mapping_and_warn(self) -> None:
        # Mapping a workspace-write Codex coding agent and verifier was allowed
        # before every Codex child was bridged read-only; install verification
        # must still pass, and say why those agents are not used.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            for name, description in (
                ("implementation_worker", "Project implementation role."),
                ("verification_worker", "Project verification role."),
            ):
                (root / ".codex" / "agents" / f"{name}.toml").write_text(
                    f'name = "{name}"\n'
                    f'description = "{description}"\n'
                    'sandbox_mode = "workspace-write"\n'
                    'developer_instructions = "Use only project-approved authority."\n'
                    "mcp_servers = []\n"
                    "skills = []\n"
                )
            (root / ".codex" / "orchestration" / "project-agent-routing.json").write_text(
                json.dumps({
                    "version": 1,
                    "roles": {
                        "coding": "implementation_worker",
                        "verification": "verification_worker",
                    },
                    "verifiers": {"coding": "verification_worker"},
                })
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for key, value in (("user.email", "tests@example.invalid"),
                               ("user.name", "Validation Tests")):
                subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "mapping"], check=True)

            report = validate_package(root)

            self.assertTrue(report["package_ready"], report)
            self.assertFalse(
                any("smoke" in error or "eligible" in error for error in report["errors"]),
                report,
            )
            for name in ("implementation_worker", "verification_worker"):
                self.assertIn(
                    f"{name} is workspace-write; Codex children are bridged read-only in "
                    "this release, so it is not used for Codex routes",
                    report["warnings"],
                )

    def test_operational_checks_must_be_committed_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            qa = root / "qa-check"
            qa.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8")
            qa.chmod(0o700)
            checks = root / ".codex" / "orchestration" / "operational-checks.json"
            checks.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "deterministic_checks": [
                            {"check_id": "tests", "driver": "python-wrapper",
                             "argv": ["./qa-check"],
                             "executable_sha256": hashlib.sha256(
                                 qa.read_bytes()
                             ).hexdigest(),
                             "interpreter_sha256": hashlib.sha256(
                                 Path("/usr/bin/python3").read_bytes()
                             ).hexdigest(),
                             "runtime_tree": None,
                             "timeout_seconds": 5}
                        ],
                        "security_checks": [
                            {"check_id": "security", "driver": "python-wrapper",
                             "argv": ["./qa-check"],
                             "executable_sha256": hashlib.sha256(
                                 qa.read_bytes()
                             ).hexdigest(),
                             "interpreter_sha256": hashlib.sha256(
                                 Path("/usr/bin/python3").read_bytes()
                             ).hexdigest(),
                             "runtime_tree": None,
                             "timeout_seconds": 5}
                        ],
                    }
                )
            )

            report = validate_package(root)

            self.assertFalse(report["ready"])
            self.assertIn(
                "operational check configuration is not committed and clean",
                report["errors"],
            )

    def test_project_readiness_fails_when_nested_sandbox_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            shutil.copytree(BUNDLE_ROOT, root)
            checks = root / ".codex/orchestration/operational-checks.json"
            checks.write_text("{}", encoding="utf-8")
            with patch(
                "codex_agents.validation.load_operational_check_configuration",
                return_value={},
            ), patch(
                "codex_agents.validation.operational_sandbox_preflight",
                return_value=False,
            ):
                report = validate_package(root)

            self.assertTrue(report["package_ready"], report)
            self.assertFalse(report["project_ready"], report)
            self.assertIn(
                "operational check sandbox preflight failed; validate from an "
                "owner shell or approved unsandboxed command boundary",
                report["project_errors"],
            )


if __name__ == "__main__":
    unittest.main()
