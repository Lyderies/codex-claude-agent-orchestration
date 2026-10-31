from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import signal
import shutil
import stat
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
_REAL_CHECK = attest.check_ssh_keygen

FINGERPRINT = "ab" * 32
SECRET_BITS = ("owner@example.invalid", "acct-12345", FINGERPRINT)
PRIVATE_HEAD = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETBODY\n"
COMMENT = "someone@example.invalid"


def write_key(path: Path) -> None:
    path.write_text(PRIVATE_HEAD)
    path.chmod(0o600)


class FakeWorld:
    """Stands in for ssh-keygen and the orchestration CLI; records what was asked."""

    def __init__(self, root: Path, *, sign_ok: bool = True, logged_in: bool = True) -> None:
        self.root = root
        self.encrypted = True
        self.derived = f"ssh-ed25519 AAAAkey {COMMENT}\n"
        self.envs: list[dict] = []
        self.sign_ok = sign_ok
        self.logged_in = logged_in
        self.calls: list[list[str]] = []
        self.grants: dict[str, dict] = {}
        self.work_dirs: list[Path] = []
        self.recorded: list[str] = []
        self.signed_names: list[str] = []
        self.passphrase_changes: list[list[str]] = []
        self.change_rc = 0

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
        assert command[0] == "/usr/bin/ssh-keygen", command[0]
        self.envs.append(kwargs["env"])
        if command[1] == "-t":
            key = Path(command[command.index("-f") + 1])
            write_key(key)
            Path(str(key) + ".pub").write_text(f"ssh-ed25519 AAAAkey {COMMENT}\n")
            if not self.encrypted:  # user left the passphrase empty
                self.unencrypted_new = True
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "-p":
            self.passphrase_changes.append([str(part) for part in command])
            return subprocess.CompletedProcess(command, self.change_rc)
        if command[1] == "-y":
            if "-P" in command:
                return subprocess.CompletedProcess(command, 1 if self.encrypted else 0, "", "")
            return subprocess.CompletedProcess(command, 0, self.derived, "")
        if command[1] == "-l":
            content = Path(command[-1]).read_text()
            assert COMMENT not in content  # only type and blob are ever written for display
            return subprocess.CompletedProcess(command, 0, "256 SHA256:publicprint (ED25519)\n", "")
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
        self.state = self.root / "state"
        self.target = self.root / "project"
        for directory in (self.home, self.state):
            directory.mkdir()
            directory.chmod(0o755)
        (self.target / ".codex/orchestration").mkdir(parents=True)
        (self.target / ".codex/orchestration/cli.py").write_text("")
        self.world = FakeWorld(self.root)
        attest._CLEANUP_FAILURES.clear()
        attest._TEMP_DIRS.clear()
        patcher = mock.patch.object(attest, "check_ssh_keygen")
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_providers(self, **enabled: bool) -> None:
        config = {"version": 1, "providers": {n: {"enabled": v} for n, v in enabled.items()}}
        (self.target / ".codex/orchestration/provider-routing.json").write_text(json.dumps(config))

    def configure_key(self) -> Path:
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        key = ssh_dir / "codex-agents-owner"
        write_key(key)
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        return key

    def run_main(self, answers: list[str], *, terminal: bool = True, change: bool = False):
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
            argv = ["--state-root", str(self.state)]
            argv += ["--change-passphrase"] if change else ["--target", str(self.target)]
            code = attest.main(argv)
        self.output = out.getvalue() + err.getvalue() + "".join(prompts)
        self.prompts = prompts
        return code

    # terminal

    def test_refuses_without_terminal(self) -> None:
        self.assertEqual(self.run_main([], terminal=False), 2)
        self.assertIn("human step", self.output)
        self.assertEqual(self.world.calls, [])

    def test_terminal_check_needs_stdin_and_stdout(self) -> None:
        for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
            with mock.patch.object(sys, "stdin") as sin, mock.patch.object(sys, "stdout") as sout:
                sin.isatty.return_value, sout.isatty.return_value = stdin, stdout
                self.assertIs(attest._is_terminal(), expected)

    def test_wording_does_not_claim_terminal_keeps_agents_out(self) -> None:
        source = _SCRIPT.read_text().lower()
        self.assertNotIn("keeps agents out", source)
        self.assertIn("passphrase is what proves", attest.KEY_ADVICE)

    # signer binary

    def test_every_ssh_keygen_call_uses_fixed_path_despite_path_plant(self) -> None:
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        (fake_bin / "ssh-keygen").write_text("#!/bin/sh\nexit 0\n")
        self.set_providers(codex=True)
        self.configure_key()
        with mock.patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            self.run_main(["y", "y", "y", "y"])
        keygen = [c for c in self.world.calls if "ssh-keygen" in c[0]]
        self.assertTrue(keygen)
        self.assertTrue(all(c[0] == "/usr/bin/ssh-keygen" for c in keygen))

    # key creation, reuse, enrolment

    def test_creates_key_when_none_configured(self) -> None:
        self.set_providers(codex=True)
        code = self.run_main(["y", "y", "n", "y"])  # create key; buy off; spend balance not off; sign
        self.assertEqual(code, 0)
        keygen = [c for c in self.world.calls if c[:2] == ["/usr/bin/ssh-keygen", "-t"]]
        self.assertEqual(len(keygen), 1)
        self.assertEqual(keygen[0][2:], ["ed25519", "-f", str(self.home / ".ssh/codex-agents-owner"),
                                         "-C", "codex-agents-owner"])
        self.assertTrue(any("learning-configure-owner-key" in c for c in self.world.calls))
        self.assertIn("passphrase", self.output)
        self.assertEqual(self.world.recorded, ["codex"])
        self.assertEqual(self.world.signed_names, ["codex-agents-owner-v1"])
        self.assertEqual((self.home / ".ssh").stat().st_mode & 0o777, 0o700)

    def test_new_key_with_empty_passphrase_is_refused(self) -> None:
        self.set_providers(codex=True)
        self.world.encrypted = False
        self.assertEqual(self.run_main(["y"]), 1)
        self.assertIn("ssh-keygen -p -f", self.output)
        self.assertEqual(self.world.recorded, [])

    def test_existing_private_key_path_is_enrolled_from_derived_key(self) -> None:
        self.set_providers(codex=True)
        other = self.root / "mykey"
        write_key(other)
        Path(str(other) + ".pub").write_text(f"ssh-ed25519 AAAAkey {COMMENT}\n")
        self.assertEqual(self.run_main(["n", str(other), "y", "y", "y"]), 0)
        self.assertFalse(any(c[:2] == ["/usr/bin/ssh-keygen", "-t"] for c in self.world.calls))
        self.assertTrue(any("-y" in c and "-P" not in c for c in self.world.calls))
        self.assertIn(str(other), next(c for c in self.world.calls if "-Y" in c))

    def test_pub_mismatch_is_refused(self) -> None:
        self.set_providers(codex=True)
        other = self.root / "mykey"
        write_key(other)
        Path(str(other) + ".pub").write_text("ssh-ed25519 AAAAdifferent c\n")
        self.assertEqual(self.run_main(["n", str(other)]), 1)
        self.assertIn("does not match", self.output)
        self.assertFalse(any("learning-configure-owner-key" in c for c in self.world.calls))

    def test_public_key_file_cannot_be_the_signing_key(self) -> None:
        self.set_providers(codex=True)
        public = self.root / "mykey.pub"
        public.write_text("ssh-ed25519 AAAAkey c\n")
        public.chmod(0o600)
        self.assertEqual(self.run_main(["n", str(public)]), 1)
        self.assertIn("not a private key", self.output)

    def test_unencrypted_existing_key_is_refused(self) -> None:
        self.set_providers(codex=True)
        self.configure_key()
        self.world.encrypted = False
        self.assertEqual(self.run_main([""]), 1)
        self.assertIn("ssh-keygen -p -f", self.output)
        self.assertFalse(any("-Y" in c for c in self.world.calls))

    def test_reuses_configured_key(self) -> None:
        key = self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["y", "y", "y"]), 0)  # key found at the default, no path prompt
        self.assertFalse(any(c[:2] == ["/usr/bin/ssh-keygen", "-t"] for c in self.world.calls))
        self.assertFalse(any("learning-configure-owner-key" in c for c in self.world.calls))
        self.assertIn(str(key), next(c for c in self.world.calls if "-Y" in c))

    def test_default_key_is_found_without_prompting_for_a_path(self) -> None:
        key = self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["y", "y", "y"]), 0)
        self.assertIn(f"Using the owner key file at {key}", self.output)
        self.assertFalse(any("private key file" in prompt for prompt in self.prompts))
        self.assertEqual(len(self.prompts), 3)  # only the billing questions

    def test_default_key_with_a_different_pub_file_falls_back_to_asking(self) -> None:
        key = self.configure_key()
        Path(str(key) + ".pub").write_text("ssh-ed25519 AAAAdifferent c\n")
        self.set_providers(codex=True)
        self.run_main(["", "n"])
        self.assertIn("This asks for a file location, not your passphrase.", self.output)

    def test_missing_default_key_asks_in_words_that_are_not_a_passphrase_prompt(self) -> None:
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        other = self.home / "elsewhere-key"
        write_key(other)
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["~/elsewhere-key", "y", "y", "y"]), 0)
        self.assertIn("This asks for a file location, not your passphrase.", self.output)
        self.assertIn("Press Enter to use ~/.ssh/codex-agents-owner", self.prompts[0])
        self.assertNotIn("passphrase:", self.prompts[0].lower())
        self.assertIn(str(other), next(c for c in self.world.calls if "-Y" in c))

    def test_a_non_path_entry_is_refused_and_never_echoed(self) -> None:
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        self.set_providers(codex=True)
        secret = "correct horse battery staple"
        self.assertEqual(self.run_main([secret]), 1)
        self.assertNotIn(secret, self.output)
        self.assertNotIn("horse", self.output)
        self.assertIn("That is not a file location.", self.output)
        self.assertIn("Change the owner key passphrase", self.output)
        self.assertFalse(any("-Y" in c for c in self.world.calls))

    def test_change_passphrase_runs_fixed_keygen_with_p_and_verifies(self) -> None:
        key = self.configure_key()
        self.assertEqual(self.run_main([], change=True), 0)
        self.assertEqual(self.world.passphrase_changes, [["/usr/bin/ssh-keygen", "-p", "-f", str(key)]])
        self.assertNotIn("SSH_AUTH_SOCK", self.world.envs[-1])
        calls = [c for c in self.world.calls if c[0] == "/usr/bin/ssh-keygen"]
        self.assertEqual(calls[-1][:2], ["/usr/bin/ssh-keygen", "-y"])  # verified afterwards
        self.assertFalse(any("-Y" in c for c in self.world.calls))
        self.assertIn("Passphrase changed", self.output)

    def test_change_passphrase_needs_a_configured_key_and_a_terminal(self) -> None:
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("no owner key is configured", self.output)
        self.assertEqual(self.world.passphrase_changes, [])
        self.configure_key()
        self.assertEqual(self.run_main([], change=True, terminal=False), 2)
        self.assertEqual(self.world.passphrase_changes, [])

    def test_change_passphrase_failure_and_mismatch_are_reported(self) -> None:
        self.configure_key()
        self.world.change_rc = 1
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("was not changed", self.output)
        self.world.change_rc = 0
        self.world.derived = "ssh-ed25519 AAAAother\n"
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("does not match", self.output)

    def test_change_passphrase_refuses_an_unencrypted_key(self) -> None:
        self.configure_key()
        self.world.encrypted = False
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertEqual(self.world.passphrase_changes, [])

    def test_configured_key_mismatch_is_refused(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.derived = "ssh-ed25519 AAAAother\n"
        self.assertEqual(self.run_main([]), 1)
        self.assertIn("does not match", self.output)

    # agent, paths

    def test_signing_runs_without_agent_socket(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/agent.sock"}):
            self.run_main(["y", "y", "y"])
        self.assertTrue(self.world.envs)
        self.assertTrue(all("SSH_AUTH_SOCK" not in env for env in self.world.envs))

    def test_symlinked_ssh_dir_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        write_key(elsewhere / "codex-agents-owner")
        (self.home / ".ssh").symlink_to(elsewhere)
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        self.set_providers(codex=True)
        self.assertEqual(self.run_main([""]), 1)
        self.assertIn("symbolic links", self.output)

    def test_group_writable_directory_and_loose_key_are_refused(self) -> None:
        key = self.configure_key()
        key.parent.chmod(0o775)
        with self.assertRaises(attest.AttestError):
            attest.validate_private_key(key)
        key.parent.chmod(0o700)
        key.chmod(0o640)
        with self.assertRaises(attest.AttestError):
            attest.validate_private_key(key)

    def _fake_lstat(self, overrides: dict[Path, tuple[int, int]]):
        real = os.lstat

        def fake(path):
            info = real(path)
            if Path(path) in overrides:
                uid, mode = overrides[Path(path)]
                fields = list(info)
                fields[0], fields[4] = mode, uid
                return os.stat_result(fields)
            return info

        return mock.patch.object(attest, "_lstat", fake)

    def test_root_owned_world_writable_parent_without_sticky_is_refused(self) -> None:
        key = self.configure_key()
        with self._fake_lstat({self.home: (0, stat.S_IFDIR | 0o777)}):
            with self.assertRaises(attest.AttestError):
                attest.check_path_safe(key)

    def test_root_owned_sticky_parent_is_accepted_when_rest_is_safe(self) -> None:
        key = self.configure_key()
        with self._fake_lstat({self.home: (0, stat.S_IFDIR | 0o1777)}):
            attest.check_path_safe(key)
            key.parent.chmod(0o770)  # an unsafe remaining component still fails
            with self.assertRaises(attest.AttestError):
                attest.check_path_safe(key)

    def test_root_owned_symlink_to_unsafe_target_is_refused(self) -> None:
        unsafe = self.root / "unsafe"
        (unsafe / "keys").mkdir(parents=True)
        unsafe.chmod(0o777)
        link = self.root / "link"
        link.symlink_to(unsafe)
        safe = self.root / "safe"
        (safe / "keys").mkdir(parents=True)
        safe.chmod(0o755)
        safe_link = self.root / "safelink"
        safe_link.symlink_to(safe)
        root_owned = {link: (0, stat.S_IFLNK | 0o777), safe_link: (0, stat.S_IFLNK | 0o777)}
        with self._fake_lstat(root_owned):
            with self.assertRaises(attest.AttestError):
                attest.check_path_safe(link / "keys" / "k")
            attest.check_path_safe(safe_link / "keys" / "k")
        with self.assertRaises(attest.AttestError):  # user-owned symlink: refused
            attest.check_path_safe(safe_link / "keys" / "k")

    def test_normal_home_path_passes(self) -> None:
        attest.check_path_safe(self.configure_key())

    def test_existing_or_dangling_output_files_block_creation(self) -> None:
        self.set_providers(codex=True)
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        (ssh_dir / "codex-agents-owner.pub").symlink_to(self.root / "nowhere")  # dangling
        self.assertEqual(self.run_main(["y"]), 1)
        self.assertIn("already exists", self.output)
        self.assertFalse(any(c[:2] == ["/usr/bin/ssh-keygen", "-t"] for c in self.world.calls))

    def test_ssh_keygen_binary_checks(self) -> None:
        planted = self.root / "ssh-keygen"
        planted.write_text("#!/bin/sh\n")  # owned by this user, not root
        real = _REAL_CHECK
        with mock.patch.object(attest, "SSH_KEYGEN", str(planted)):
            with self.assertRaises(attest.AttestError):
                real()
        with mock.patch.object(attest, "SSH_KEYGEN", str(self.root / "missing")):
            with self.assertRaises(attest.AttestError):
                real()

    def test_main_stops_when_ssh_keygen_untrusted(self) -> None:
        with mock.patch.object(attest, "check_ssh_keygen", side_effect=attest.AttestError("bad")):
            self.assertEqual(self.run_main([]), 1)
        self.assertEqual(self.world.calls, [])

    # providers

    def test_claude_yes_and_no(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        # codex: buy off yes, spend yes, sign yes; claude: No
        self.assertEqual(self.run_main(["y", "y", "y", "n"]), 0)
        self.assertEqual(self.world.recorded, ["codex"])
        self.assertIn(attest.QUESTIONS["claude"], self.prompts[-1])
        self.assertIn("Attested: codex", self.output)
        self.assertIn("Not attested: claude", self.output)
        self.assertIn("Any earlier attestation for this provider stays in effect", self.output)
        self.assertIn("provider-routing.json", self.output)
        self.assertNotIn("revoke", self.output.lower())
        self.assertNotIn("stay blocked", self.output)

    def test_default_answer_is_no(self) -> None:
        self.configure_key()
        self.set_providers(claude=True, codex=False)
        self.assertEqual(self.run_main([""]), 1)
        self.assertEqual(self.world.recorded, [])

    def test_codex_buy_not_off_refuses_attestation(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["n"]), 1)
        self.assertEqual(self.world.grants, {})
        self.assertIn("not attested while credits can still be bought", self.output)

    def test_codex_two_questions_and_field_only_when_spend_disabled(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        self.run_main(["y", "y", "y", "y", "y"])
        self.assertIn(attest.QUESTIONS["codex_buy"], self.prompts[0])
        self.assertIn(attest.QUESTIONS["codex_spend"], self.prompts[1])
        self.assertIn("credit spending disabled", self.output)
        self.assertIs(self.world.grants["codex"]["codex_credits_spending_disabled"], True)
        self.assertNotIn("codex_credits_spending_disabled", self.world.grants["claude"])
        for grant in self.world.grants.values():
            self.assertEqual(grant["version"], 1)
            self.assertIs(grant["no_paid_overage"], True)
            self.assertIs(grant["no_usage_credits"], True)
            self.assertIsInstance(grant["issued_at"], int)

    def test_codex_spend_not_off_omits_field(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["y", "n", "y"]), 0)
        self.assertNotIn("codex_credits_spending_disabled", self.world.grants["codex"])
        self.assertNotIn("credit spending disabled", self.output)
        self.assertIn("no paid overage, no usage credits.", self.output)

    def test_final_confirmation_declined_does_not_sign(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["y", "y", "n"]), 1)
        self.assertEqual(self.world.grants, {})
        self.assertFalse(any("-Y" in c for c in self.world.calls))

    def test_codex_only_without_routing_file(self) -> None:
        self.configure_key()
        self.run_main(["y", "y", "y"])
        self.assertEqual(list(self.world.grants), ["codex"])

    def test_not_logged_in_is_skipped(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.logged_in = False
        self.assertEqual(self.run_main([""]), 1)
        self.assertEqual(self.world.grants, {})

    # temporary directories

    def test_temp_dir_removed_on_success_and_private(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        modes: list[int] = []
        original = attest.make_temp

        def spy(prefix: str) -> Path:
            path = original(prefix)
            modes.append(path.stat().st_mode & 0o777)
            return path

        with mock.patch.object(attest, "make_temp", spy):
            self.run_main(["y", "y", "y"])
        self.assertEqual(len(self.world.work_dirs), 2)
        for directory in self.world.work_dirs:
            self.assertFalse(directory.exists())
        self.assertTrue(modes and all(mode == 0o700 for mode in modes))
        self.assertEqual(attest._TEMP_DIRS, set())

    def test_temp_dir_removed_on_failure(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.sign_ok = False
        self.assertEqual(self.run_main(["y", "y", "y"]), 1)
        self.assertIn("signing did not finish", self.output)
        self.assertEqual(self.world.recorded, [])
        for directory in self.world.work_dirs:
            self.assertFalse(directory.exists())

    def test_cleanup_failure_is_reported_and_fails_exit(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        real_rmtree = shutil.rmtree

        def stubborn(path, ignore_errors=False):
            if Path(path).name.startswith("attest-") and "attest-pub-" not in str(path):
                return None
            return real_rmtree(path, ignore_errors=ignore_errors)

        with mock.patch.object(attest.shutil, "rmtree", stubborn):
            code = self.run_main(["y", "y", "y"])
        self.assertEqual(code, 1)
        self.assertIn("could not be removed", self.output)
        for leftover in attest._CLEANUP_FAILURES:
            real_rmtree(leftover, ignore_errors=True)

    def test_signal_handler_removes_temp_dirs(self) -> None:
        scratch = attest.make_temp("attest-")
        with self.assertRaises(SystemExit) as raised:
            attest._on_signal(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(scratch.exists())

    def test_handlers_installed_and_restored(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        previous = attest._install_handlers()
        self.assertIs(signal.getsignal(signal.SIGTERM), attest._on_signal)
        if hasattr(signal, "SIGHUP"):
            self.assertIs(signal.getsignal(signal.SIGHUP), attest._on_signal)
        attest._restore_handlers(previous)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    # privacy

    def test_nothing_identifying_in_output(self) -> None:
        self.configure_key()
        self.set_providers(codex=True, claude=True)
        self.run_main(["y", "y", "y", "y", "y"])
        for fragment in SECRET_BITS + ("AAAAkey", "SECRETBODY", COMMENT):
            self.assertNotIn(fragment, self.output)
        self.assertIn("Plan: max", self.output)
        self.assertIn("Plan: pro", self.output)
        self.assertIn("ssh-ed25519 SHA256:publicprint", self.output)

    def test_key_comment_never_printed_when_enrolling(self) -> None:
        self.set_providers(codex=True)
        self.run_main(["y", "y", "y", "y"])
        self.assertNotIn(COMMENT, self.output)
        self.assertNotIn("codex-agents-owner (", self.output)


if __name__ == "__main__":
    unittest.main()
