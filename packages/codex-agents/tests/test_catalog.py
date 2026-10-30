from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path

from tests.support import FIXTURES_ROOT, ORCHESTRATION_ROOT

from codex_agents.catalog import CatalogError, discover, load_discovery_policy


class CatalogTests(unittest.TestCase):
    def policy(self, root: str = ".") -> dict:
        return {
            "version": 1,
            "sources": [{"id": "project", "kind": "project", "root": root}],
            "duplicate_policy": "error",
            "resolutions": {},
            "allowed_mcp_servers": {},
            "allowed_legacy_tools": ["read", "search", "shell", "write", "web"],
        }

    def test_package_policy_loads(self) -> None:
        policy = load_discovery_policy(
            ORCHESTRATION_ROOT / "config" / "discovery-policy.json"
        )
        self.assertEqual("project", policy["sources"][0]["id"])

    def test_policy_rejects_name_only_mcp_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            policy = self.policy()
            policy["allowed_mcp_servers"] = ["mutable-name"]
            path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(CatalogError, "pinned MCP identity"):
                load_discovery_policy(path)

    def test_valid_native_definitions_are_selected(self) -> None:
        report = discover(FIXTURES_ROOT / "discovery" / "valid", self.policy())
        self.assertTrue(report["ready"])
        self.assertEqual({"agent:reader", "skill:summarize"}, {
            f"{item['kind']}:{item['name']}" for item in report["selected"]
        })

    def test_broken_reference_blocks_selection_readiness(self) -> None:
        report = discover(FIXTURES_ROOT / "discovery" / "broken-link", self.policy())
        self.assertFalse(report["ready"])
        self.assertIn("broken local references", report["errors"][0]["messages"][0])

    def test_unsafe_legacy_tool_blocks_readiness(self) -> None:
        report = discover(FIXTURES_ROOT / "discovery" / "unsafe-tool", self.policy())
        self.assertFalse(report["ready"])
        self.assertIn("dangerous-publish", report["errors"][0]["messages"][0])

    def test_legacy_agent_is_inert_and_requires_translation(self) -> None:
        report = discover(FIXTURES_ROOT / "discovery" / "legacy", self.policy())
        self.assertTrue(report["ready"])
        self.assertIn("requires explicit translation", report["selected"][0]["warnings"][0])

    def test_claude_agents_are_discovered_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / ".claude/agents"
            agents.mkdir(parents=True)
            (agents / "claude_agents_researcher.md").write_bytes(
                (FIXTURES_ROOT / "claude/claude_agents_researcher.md").read_bytes()
            )
            (agents / "writer.md").write_text(
                "---\nname: writer\ndescription: Edits files\n"
                "tools: Read, Edit, Bash\ncolor: blue\n---\n\nEdit things.\n"
            )
            codex_agents = root / ".codex/agents"
            codex_agents.mkdir(parents=True)
            (codex_agents / "writer.toml").write_text(
                'name = "writer"\ndescription = "Codex writer"\n'
                'developer_instructions = "Write."\n'
            )
            report = discover(root, self.policy())
            # Claude definitions never block readiness or conflict with Codex agents.
            self.assertTrue(report["ready"])
            claude = {
                item["name"]: item
                for item in report["selected"]
                if item["format"] == "claude-md"
            }
            self.assertEqual({"claude_agents_researcher", "writer"}, set(claude))
            reader = claude["claude_agents_researcher"]
            self.assertEqual(["Glob", "Grep", "Read"], reader["requested_tools"])
            self.assertEqual("read-only", reader["sandbox_mode"])
            self.assertEqual([], reader["warnings"])
            self.assertIsNone(claude["writer"]["sandbox_mode"])
            self.assertIn(
                "unreviewed Claude agent keys: color", claude["writer"]["warnings"]
            )

    def test_claude_front_matter_matches_the_installed_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / ".claude/agents"
            agents.mkdir(parents=True)
            (agents / "quoted.md").write_text(
                '---\nname: "quoted"\ndescription: Reads\ntools: Read\n---\n\nRead.\n'
            )
            (agents / "array.md").write_text(
                "---\nname: array\ndescription: Reads\ntools: [Read, Glob]\n---\n\nRead.\n"
            )
            (agents / "installed.md").write_text(
                "---\nname: installed\ndescription: Reads\ntools: Read, Glob\n"
                "maxTurns: 8\n---\n\nRead.\n"
            )
            outside = Path(directory).parent / "outside-claude-agent.md"
            (agents / "linked.md").symlink_to(outside)
            report = discover(root, self.policy())
            self.assertTrue(report["ready"])
            warnings = {
                item["name"]: item["warnings"] for item in report["candidates"]
            }
            self.assertIn("name must be unquoted plain text", warnings["quoted"][0])
            self.assertIn("tools must be unquoted plain text", warnings["array"][0])
            self.assertEqual(["Claude agent is a symlink"], warnings["linked"])
            self.assertEqual([], warnings["installed"])

    def test_duplicate_name_fails_without_resolution(self) -> None:
        policy = self.policy()
        policy["sources"] = [
            {"id": "a", "kind": "project", "root": "source-a"},
            {"id": "b", "kind": "catalog", "root": "source-b"},
        ]
        root = FIXTURES_ROOT / "discovery" / "conflict"
        report = discover(root, policy)
        self.assertFalse(report["ready"])
        self.assertEqual("agent:reviewer", report["conflicts"][0]["key"])
        policy["resolutions"] = {"agent:reviewer": "a"}
        resolved = discover(root, policy)
        self.assertTrue(resolved["ready"])
        self.assertEqual("a", resolved["selected"][0]["source"])

    def test_resolution_for_unknown_definition_fails(self) -> None:
        policy = self.policy()
        policy["resolutions"] = {"agent:missing": "project"}
        report = discover(FIXTURES_ROOT / "discovery" / "valid", policy)
        self.assertFalse(report["ready"])
        self.assertIn("undiscovered", report["errors"][-1]["messages"][0])

    def test_source_cannot_escape_trusted_root(self) -> None:
        with self.assertRaisesRegex(CatalogError, "escapes trusted root"):
            discover(FIXTURES_ROOT / "discovery" / "valid", self.policy("../"))

    def test_agent_mcp_dependency_is_inert_until_identity_is_pinned(self) -> None:
        root = FIXTURES_ROOT / "discovery" / "mcp"
        rejected = discover(root, self.policy())
        self.assertFalse(rejected["ready"])
        messages = " ".join(rejected["errors"][0]["messages"])
        self.assertIn("inline MCP definitions are rejected", messages)
        self.assertIn("pinned identity enforcement", messages)

    def test_symlinked_definition_cannot_escape_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            agent_root = root / ".codex" / "agents"
            agent_root.mkdir(parents=True)
            outside = Path(directory) / "outside.toml"
            outside.write_text(
                'name = "outside"\ndescription = "outside"\n'
                'developer_instructions = "outside"\n'
            )
            (agent_root / "outside.toml").symlink_to(outside)
            with self.assertRaisesRegex(CatalogError, "escapes source root"):
                discover(root, self.policy())

    def test_skill_reference_cannot_escape_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            skill_root = root / ".agents" / "skills" / "escape"
            skill_root.mkdir(parents=True)
            outside = Path(directory) / "outside.md"
            outside.write_text("outside")
            skill_root.joinpath("SKILL.md").write_text(
                "---\nname: escape\ndescription: escape\n---\n"
                "Read [outside](../../../../outside.md).\n"
            )
            report = discover(root, self.policy())
            self.assertFalse(report["ready"])
            self.assertIn("escape source root", report["errors"][0]["messages"][0])


if __name__ == "__main__":
    unittest.main()
