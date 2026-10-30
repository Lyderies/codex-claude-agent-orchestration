from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "attest.py"
_SPEC = importlib.util.spec_from_file_location("umbrella_attest", _SCRIPT)
attest = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = attest
_SPEC.loader.exec_module(attest)

FINGERPRINT = "ab" * 32
SECRET_BITS = ("owner@example.invalid", "acct-12345", FINGERPRINT)


class FakeWorld:
    """Stands in for ssh-keygen and the orchestration CLI; records what was asked."""

    def __init__(self, root: Path, *, sign_ok: bool = True, logged_in: bool = True) -> None:
        self.root = root
        self.sign_ok = sign_ok
        self.logged_in = logged_in
        self.calls: list[list[str]] = []
        self.grants: dict[str, dict] = {}
        self.work_dirs: list[Path] = []
        self.recorded: list[str] = []
        self.signed_names: list[str] = []

    def _cli(self, command: list[str]) -> subprocess.CompletedProcess:
        name = command[2]
        option = lambda flag: command[command.index(flag) + 1]  # noqa: E731
        if name == "learning-configure-owner-key":
            pub = Path(option("--public-key")).read_text().split()
            state = Path(option("--state-root"))
            (state / "owner-allowed-signers").write_text(f"codex-agents-owner {pub[0]} {pub[1]}\n")
            report = {"ready": True}
        elif name == "provider-observe":
            provider = option("--provider")
            report = {"ready": True, "records": [{
                "record_type": "provider_identity", "provider": provider,
                "logged_in": self.logged_in, "account_fingerprint": FINGERPRINT,
                "auth_method": "claude.ai" if provider == "claude" else "chatgpt",
                "subscription_type": "max" if provider == "claude" else None,
                "plan_type": "pro" if provider == "codex" else None,
                "email": "owner@example.invalid", "account_id": "acct-12345",
            }]}
        else:
            grant_path = Path(option("--grant"))
            self.work_dirs.append(grant_path.parent)
            grant = json.loads(grant_path.read_text())
            self.grants[grant["provider"]] = grant
            if "--signature" in command:
                self.recorded.append(grant["provider"])
                report = {"ready": True}
            else:
                report = {"ready": True, "signing_payload_b64": base64.b64encode(b"payload").decode()}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    def run(self, command, **kwargs):
        self.calls.append([str(part) for part in command])
        if command[0] == sys.executable:
            return self._cli([str(part) for part in command])
        assert command[0] == "ssh-keygen"
        if command[1] == "-t":
            key = Path(command[command.index("-f") + 1])
            key.write_text("PRIVATE")
            Path(str(key) + ".pub").write_text("ssh-ed25519 AAAAkey codex-agents-owner\n")
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "-l":
            return subprocess.CompletedProcess(
                command, 0, "256 SHA256:publicprint codex-agents-owner (ED25519)\n", "")
        assert command[1:3] == ["-Y", "sign"]
        self.signed_names.append(command[command.index("-n") + 1])
        if self.sign_ok:
            Path(command[-1] + ".sig").write_text("SIG")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 1)


class AttestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="attest-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.target = self.root / "project"
        (self.target / ".codex/orchestration").mkdir(parents=True)
        (self.target / ".codex/orchestration/cli.py").write_text("")
        self.world = FakeWorld(self.root)

    def set_providers(self, **enabled: bool) -> None:
        config = {"version": 1, "providers": {n: {"enabled": v} for n, v in enabled.items()}}
        (self.target / ".codex/orchestration/provider-routing.json").write_text(json.dumps(config))

    def configure_key(self) -> Path:
        key = self.home / ".ssh" / "codex-agents-owner"
        key.parent.mkdir()
        key.write_text("PRIVATE")
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        return key

    def run_main(self, answers: list[str], *, terminal: bool = True):
        out, err = io.StringIO(), io.StringIO()
        prompts: list[str] = []

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            return answers.pop(0)

        with mock.patch.object(attest, "_is_terminal", return_value=terminal), \
                mock.patch.object(attest.subprocess, "run", self.world.run), \
                mock.patch("builtins.input", fake_input), \
                mock.patch.object(attest.Path, "home", return_value=self.home), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = attest.main(["--target", str(self.target), "--state-root", str(self.state)])
        self.output = out.getvalue() + err.getvalue() + "".join(prompts)
        self.prompts = prompts
        return code

    def test_refuses_without_terminal(self) -> None:
        self.assertEqual(self.run_main([], terminal=False), 2)
        self.assertIn("human step", self.output)
        self.assertEqual(self.world.calls, [])

    def test_terminal_check_needs_stdin_and_stdout(self) -> None:
        for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
            with mock.patch.object(sys, "stdin") as sin, mock.patch.object(sys, "stdout") as sout:
                sin.isatty.return_value, sout.isatty.return_value = stdin, stdout
                self.assertIs(attest._is_terminal(), expected)

    def test_creates_key_when_none_configured(self) -> None:
        self.set_providers(codex=True)
        code = self.run_main(["y", "y"])  # create key, credits off
        self.assertEqual(code, 0)
        keygen = [c for c in self.world.calls if c[:2] == ["ssh-keygen", "-t"]]
        self.assertEqual(len(keygen), 1)
        self.assertEqual(keygen[0][2:], ["ed25519", "-f", str(self.home / ".ssh/codex-agents-owner"),
                                         "-C", "codex-agents-owner"])
        self.assertTrue(any("learning-configure-owner-key" in c for c in self.world.calls))
        self.assertIn("passphrase", self.output)
        self.assertEqual(self.world.recorded, ["codex"])
        self.assertEqual(self.world.signed_names, ["codex-agents-owner-v1"])

    def test_existing_private_key_path_is_configured(self) -> None:
        self.set_providers(codex=True)
        other = self.root / "mykey"
        other.write_text("PRIVATE")
        Path(str(other) + ".pub").write_text("ssh-ed25519 AAAAother c\n")
        self.assertEqual(self.run_main(["n", str(other), "y"]), 0)
        self.assertFalse(any(c[:2] == ["ssh-keygen", "-t"] for c in self.world.calls))
        self.assertIn(str(other), next(c for c in self.world.calls if "-Y" in c))

    def test_reuses_configured_key(self) -> None:
        key = self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["", "y"]), 0)  # blank path = default
        self.assertFalse(any(c[:2] == ["ssh-keygen", "-t"] for c in self.world.calls))
        self.assertFalse(any("learning-configure-owner-key" in c for c in self.world.calls))
        self.assertIn(str(key), next(c for c in self.world.calls if "-Y" in c))

    def test_yes_and_no_per_provider(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        self.assertEqual(self.run_main(["", "y", "n"]), 0)
        self.assertEqual(self.world.recorded, ["codex"])
        self.assertIn(attest.QUESTIONS["codex"], self.prompts[-2])
        self.assertIn(attest.QUESTIONS["claude"], self.prompts[-1])
        self.assertIn("Attested: codex", self.output)
        self.assertIn("Still blocked: claude", self.output)
        self.assertIn("run this command again", self.output.lower())

    def test_default_answer_is_no(self) -> None:
        self.configure_key()
        self.set_providers(claude=True, codex=False)
        self.assertEqual(self.run_main(["", ""]), 1)
        self.assertEqual(self.world.recorded, [])
        self.assertIn("children stay blocked", self.output)

    def test_codex_field_only_for_codex(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        self.run_main(["", "y", "y"])
        self.assertIs(self.world.grants["codex"]["codex_credits_spending_disabled"], True)
        self.assertNotIn("codex_credits_spending_disabled", self.world.grants["claude"])
        for grant in self.world.grants.values():
            self.assertEqual(grant["version"], 1)
            self.assertIs(grant["no_paid_overage"], True)
            self.assertIs(grant["no_usage_credits"], True)
            self.assertIsInstance(grant["issued_at"], int)

    def test_codex_only_without_routing_file(self) -> None:
        self.configure_key()
        self.run_main(["", "y"])
        self.assertEqual(list(self.world.grants), ["codex"])

    def test_temp_dir_removed_on_success(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.run_main(["", "y"])
        self.assertEqual(len(self.world.work_dirs), 2)  # payload request and recording
        for directory in self.world.work_dirs:
            self.assertFalse(directory.exists())

    def test_temp_dir_removed_on_failure(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.sign_ok = False
        self.assertEqual(self.run_main(["", "y"]), 1)
        self.assertIn("signing did not finish", self.output)
        self.assertEqual(self.world.recorded, [])
        for directory in self.world.work_dirs:
            self.assertFalse(directory.exists())

    def test_not_logged_in_is_skipped(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.logged_in = False
        self.assertEqual(self.run_main([""]), 1)
        self.assertEqual(self.world.grants, {})

    def test_nothing_identifying_in_output(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        self.run_main(["", "y", "y"])
        for fragment in SECRET_BITS + ("AAAAkey", "PRIVATE"):
            self.assertNotIn(fragment, self.output)
        self.assertIn("Plan: max", self.output)
        self.assertIn("Plan: pro", self.output)
        self.assertIn("SHA256:publicprint", self.output)


if __name__ == "__main__":
    unittest.main()
