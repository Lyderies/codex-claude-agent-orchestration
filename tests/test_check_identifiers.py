from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_identifiers.py"
_SPEC = importlib.util.spec_from_file_location("check_identifiers", _SCRIPT)
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)

# Samples are assembled at runtime so this file never contains a matching literal.
HOME_MAC = "/" + "Users" + "/alice/project"
HOME_LINUX = "/" + "home" + "/alice/project"
HOME_WINDOWS = "C:" + "\\Users" + "\\alice\\project"
HOSTNAME = "alices-laptop" + ".local"
LAPTOP = "Alices-" + "MacBook" + "-Pro"
EMAIL = "alice" + "@" + "mailhost.io"
NOREPLY = "123+someone" + "@" + "users.noreply.github.com"


def categories(text: str, denylist: tuple[str, ...] = ()) -> list[str]:
    return [finding.category for finding in check.scan_text(text, "sample", denylist)]


class ScanTextTests(unittest.TestCase):
    def test_home_paths_are_flagged_on_each_platform(self) -> None:
        for sample in (HOME_MAC, HOME_LINUX, HOME_WINDOWS):
            with self.subTest(sample=sample):
                self.assertEqual(categories(f"path {sample} here"), ["home-path"])

    def test_placeholders_and_shared_paths_are_allowed(self) -> None:
        self.assertEqual(categories("use /" + "Users/<name> or /" + "Users/Shared/x"), [])

    def test_hostnames_are_flagged(self) -> None:
        self.assertEqual(categories(f"host {HOSTNAME}"), ["hostname"])
        self.assertEqual(categories(f"machine {LAPTOP}"), ["hostname"])

    def test_personal_email_is_flagged_but_noreply_is_allowed(self) -> None:
        self.assertEqual(categories(f"mail {EMAIL}"), ["email"])
        self.assertEqual(categories(f"mail {NOREPLY}"), [])

    def test_reserved_test_domains_and_protocol_names_are_allowed(self) -> None:
        for sample in ("tests" + "@" + "example.invalid", "t" + "@" + "example.test",
                       "sk-ssh-ed25519" + "@" + "openssh.com"):
            with self.subTest(sample=sample):
                self.assertEqual(categories(f"value {sample}"), [])

    def test_local_file_names_and_package_namespaces_are_allowed(self) -> None:
        self.assertEqual(categories("edit settings" + ".local" + ".json"), [])
        self.assertEqual(categories("https://codex-agents" + ".local" + "/schemas/x.json"), [])
        self.assertEqual(categories("ping build-box" + ".local" + " now"), ["hostname"])

    def test_denylisted_terms_match_case_insensitively(self) -> None:
        self.assertEqual(categories("Written by Jane Example", ("jane example",)), ["denylisted-term"])

    def test_findings_never_include_the_matched_value(self) -> None:
        findings = check.scan_text(f"{EMAIL} {HOME_MAC}", "doc.md", ("secret term",))
        rendered = "\n".join(str(finding) for finding in findings)
        self.assertNotIn(EMAIL, rendered)
        self.assertNotIn("alice", rendered)
        self.assertIn("doc.md:1", rendered)


class IdentityTests(unittest.TestCase):
    def test_noreply_identity_passes(self) -> None:
        self.assertEqual(check.check_person("someone", NOREPLY, "author", ()), [])

    def test_personal_email_and_denylisted_name_fail(self) -> None:
        found = check.check_person("Jane Example", EMAIL, "author", ("jane example",))
        self.assertEqual([item.category for item in found], ["non-noreply-email", "denylisted-term"])


class DenylistTests(unittest.TestCase):
    def test_missing_file_yields_no_terms(self) -> None:
        self.assertEqual(check.load_denylist(Path(tempfile.gettempdir()) / "absent-denylist.txt"), ())

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "denylist.txt"
            path.write_text("# private\n\nJane Example\n", encoding="utf-8")
            self.assertEqual(check.load_denylist(path), ("jane example",))


if __name__ == "__main__":
    unittest.main()
