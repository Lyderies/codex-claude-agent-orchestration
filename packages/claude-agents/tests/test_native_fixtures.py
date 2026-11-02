from __future__ import annotations

import unittest
from pathlib import Path

from claude_agents.contracts import ContractError
from claude_agents.validation import parse_native_fixture, validate_native_inventory

FIXTURES = Path(__file__).parent / "fixtures" / "native"


class NativeFixtureTests(unittest.TestCase):
    def test_exact_five_inert_read_only_roles(self) -> None:
        texts = [p.read_text() for p in sorted(FIXTURES.glob("*.md"))]
        fixtures = validate_native_inventory(texts)
        self.assertEqual(len(fixtures), 5)
        for fixture in fixtures:
            self.assertFalse(fixture.runtime_enabled)
            self.assertEqual(fixture.tools, ("Read", "Glob", "Grep"))
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / ".claude").exists())
        self.assertFalse((root / "native-package").exists())

    def test_header_cannot_override_or_expand_native_authority(self) -> None:
        text = (FIXTURES / "claude_agents_researcher.md").read_text()
        for change in (
            text.replace("tools: Read, Glob, Grep", "tools: Read, Bash"),
            text.replace("tools: Read, Glob, Grep", "tools: Read, Read"),
            text.replace("description:", "name: duplicate\ndescription:", 1),
            text.replace("description:", "model: sonnet\ndescription:", 1),
            text.replace("description:", "hooks: command\ndescription:", 1),
            text.replace("description:", "permissionMode: bypassPermissions\ndescription:", 1),
            text.replace("description:", "mcpServers: server\ndescription:", 1),
            text.replace("name: claude_agents_researcher", "name: &alias researcher"),
            text.replace("name: claude_agents_researcher", "name: ../researcher"),
        ):
            with self.assertRaises(ContractError):
                parse_native_fixture(change)

    def test_malformed_oversized_and_escaping_references_are_rejected(self) -> None:
        text = (FIXTURES / "claude_agents_researcher.md").read_text()
        for change in ("---\nname: missing", text.replace("---", "bad", 1),
                       text + "\x00", text + "x" * 16385,
                       text + "\n[secret](../../secret)",
                       text + "\n[secret](/etc/passwd)",
                       text + "\n[dynamic](!echo-command)"):
            with self.assertRaises(ContractError):
                parse_native_fixture(change)

    def test_duplicate_or_missing_roles_are_rejected(self) -> None:
        text = (FIXTURES / "claude_agents_researcher.md").read_text()
        for texts in ([text], [text] * 5):
            with self.assertRaises(ContractError):
                validate_native_inventory(texts)


if __name__ == "__main__":
    unittest.main()
