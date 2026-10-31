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


def _finish_child(pid: int, fd: int) -> int:
    """Reap a pty child, killing it if it is still alive after a few seconds; close the pty.

    Safe to call twice. A child that is stuck (for example a prompt that never comes) is
    terminated, so a regression fails the test instead of hanging it.
    """

    import time

    status = 0
    end = time.time() + 5
    try:
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.time() > end:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
                break
            time.sleep(0.05)
    except (ChildProcessError, ProcessLookupError, OSError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return status


def _child_finisher(pid: int, fd: int):
    """Return a once-only finisher, so the explicit call and the registered cleanup never
    close the pty descriptor twice (a reused descriptor number could belong to something else)."""

    state: dict[str, int] = {}

    def finish() -> int:
        if "status" not in state:
            state["status"] = _finish_child(pid, fd)
        return state["status"]

    return finish


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

    def run_main(self, answers: list[str], *, terminal: bool = True, change: bool = False,
                 trust_temp: bool = True):
        out, err = io.StringIO(), io.StringIO()
        prompts: list[str] = []

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            return answers.pop(0)

        with mock.patch.object(attest, "_is_terminal", return_value=terminal), \
                mock.patch.object(attest.subprocess, "run", self.world.run), \
                mock.patch("builtins.input", fake_input), \
                mock.patch.object(attest.getpass, "getpass", fake_input), \
                mock.patch.object(attest, "_in_temp_root", return_value=not trust_temp), \
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
        self.assertIn("may or may not have changed", self.output)  # not a known wrong passphrase
        self.world.change_rc = 0
        self.world.passphrase_changes.clear()
        self.world.derived = "ssh-ed25519 AAAAother\n"
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("does not match", self.output)
        self.assertEqual(self.world.passphrase_changes, [])  # -p never ran on the wrong key

    def test_a_failed_check_after_the_change_says_the_key_may_have_changed(self) -> None:
        self.configure_key()
        real = self.world.run
        state = {"derives": 0}

        def cancelled_second_derive(command, **kwargs):
            if command[0] == "/usr/bin/ssh-keygen" and command[1] == "-y" and "-P" not in command:
                state["derives"] += 1
                if state["derives"] == 2:  # the check after -p is cancelled
                    self.world.calls.append([str(c) for c in command])
                    return subprocess.CompletedProcess(command, 1, "", "")
            return real(command, **kwargs)

        self.world.run = cancelled_second_derive
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertEqual(len(self.world.passphrase_changes), 1)
        self.assertIn("may already have a new passphrase", self.output)
        self.assertIn("non-empty", self.output)
        self.assertNotIn("was not changed", self.output)

    def test_an_empty_new_passphrase_is_reported_as_possibly_changed(self) -> None:
        self.configure_key()
        real = self.world.run

        def empties_the_passphrase(command, **kwargs):
            if command[0] == "/usr/bin/ssh-keygen" and command[1] == "-p":
                self.world.encrypted = False
            return real(command, **kwargs)

        self.world.run = empties_the_passphrase
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("may already have a new passphrase", self.output)

    def test_every_ssh_keygen_call_drops_askpass_and_agent_variables(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        planted = {"SSH_ASKPASS": "/evil/helper", "SSH_ASKPASS_REQUIRE": "force",
                   "SSH_AUTH_SOCK": "/agent.sock"}
        with mock.patch.dict(os.environ, planted):
            self.run_main(["y", "y", "y"])
            self.run_main([], change=True)
        self.assertTrue(self.world.envs)
        for env in self.world.envs:
            self.assertNotIn("SSH_ASKPASS", env)
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")

    def test_working_tree_check_covers_the_folder_itself_and_every_ancestor(self) -> None:
        plain = self.root / "plain" / "state"
        plain.mkdir(parents=True)
        self.assertFalse(attest._in_working_tree(plain))
        direct = self.root / "direct"
        (direct / ".git").mkdir(parents=True)
        self.assertTrue(attest._in_working_tree(direct))  # .git directly inside the state folder
        home_repo = self.root / "homerepo"
        (home_repo / ".git").mkdir(parents=True)
        (home_repo / ".local" / "state" / "codex-agents").mkdir(parents=True)
        self.assertTrue(attest._in_working_tree(home_repo / ".local" / "state" / "codex-agents"))
        # And the full check refuses a state folder that is itself a repository.
        (direct / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        direct.chmod(0o755)
        with mock.patch.object(attest, "_in_temp_root", return_value=False):
            with self.assertRaises(attest.AttestError):
                attest.check_trusted_state(direct)

    def scripted_failures(self, matcher, count: int):
        """Make the first `count` ssh-keygen calls that match fail with a wrong-passphrase error."""

        real = self.world.run
        state = {"left": count}

        def run(command, **kwargs):
            if command[0] == "/usr/bin/ssh-keygen" and state["left"] > 0 and matcher(command):
                state["left"] -= 1
                self.world.calls.append([str(c) for c in command])
                key = command[command.index("-f") + 1]
                text = (f"Failed to load key {key}: {attest.WRONG_TEXT}" if command[1] == "-p"
                        else f'Load key "{key}": {attest.WRONG_TEXT}')
                return subprocess.CompletedProcess(command, 255, "", text + "\n")
            return real(command, **kwargs)

        self.world.run = run

    def test_wrong_current_passphrase_for_p_can_be_retried_or_abandoned(self) -> None:
        self.configure_key()
        self.scripted_failures(lambda c: c[1] == "-p", 1)
        self.assertEqual(self.run_main(["t"], change=True), 0)
        self.assertIn("That passphrase did not unlock the key.", self.output)
        self.world.calls.clear()
        self.scripted_failures(lambda c: c[1] == "-p", 1)
        self.assertEqual(self.run_main(["e"], change=True), 1)
        self.assertIn("was not changed", self.output)  # -p failed before writing anything

    def test_only_the_exact_diagnostic_for_this_key_counts_as_a_wrong_passphrase(self) -> None:
        key = Path("/keys/key")
        odd = Path("/keys/incorrect passphrase")

        def result(text: str, code: int = 255):
            return subprocess.CompletedProcess(["x"], code, "", text)

        load = f'Load key "{key}": {attest.WRONG_TEXT}'
        change = f"Failed to load key {key}: {attest.WRONG_TEXT}"
        self.assertTrue(attest.wrong_passphrase(result(load + "\n"), key, "load"))
        self.assertTrue(attest.wrong_passphrase(result(change + "\n"), key, "change"))
        self.assertFalse(attest.wrong_passphrase(result(load, 0), key, "load"))  # success
        self.assertFalse(attest.wrong_passphrase(result(load), key, "change"))  # other operation
        self.assertFalse(attest.wrong_passphrase(result(change), key, "load"))
        self.assertFalse(attest.wrong_passphrase(result(load), Path("/other/key"), "load"))  # other path
        self.assertFalse(attest.wrong_passphrase(result(""), key, "load"))
        # A path that contains the phrase, with an unrelated failure: never a wrong passphrase.
        for text in (f'Saving key "{odd}" failed: No space left on device',
                     f'Failed to save key {odd}: incorrect passphrase supplied to decrypt private key',
                     f'Load key "{odd}": No such file or directory'):
            with self.subTest(text=text):
                self.assertFalse(attest.wrong_passphrase(result(text), odd, "load"))
                self.assertFalse(attest.wrong_passphrase(result(text), odd, "change"))
        # Extra lines (for example a save failure after a load) make it a different failure.
        self.assertFalse(attest.wrong_passphrase(
            result(change + f'\nSaving key "{key}" failed: No space left on device'), key, "change"))

    def test_a_save_failure_in_p_is_never_retried(self) -> None:
        key = self.configure_key()
        real = self.world.run

        def save_failure(command, **kwargs):
            if command[0] == "/usr/bin/ssh-keygen" and command[1] == "-p":
                self.world.passphrase_changes.append([str(c) for c in command])
                return subprocess.CompletedProcess(
                    command, 255, "", f'Saving key "{key}" failed: No space left on device\n')
            return real(command, **kwargs)

        self.world.run = save_failure
        self.assertEqual(self.run_main(["t", "t"], change=True), 1)
        self.assertEqual(len(self.world.passphrase_changes), 1)  # one attempt, no retry prompt
        self.assertNotIn("That passphrase did not unlock the key.", self.output)
        self.assertIn("may or may not have changed", self.output)

    def test_wrong_passphrase_in_the_post_check_retries_that_check_only(self) -> None:
        self.configure_key()
        seen = {"derives": 0}

        def second_derive(command):
            if command[1] == "-y" and "-P" not in command:
                seen["derives"] += 1
                return seen["derives"] == 2
            return False

        self.scripted_failures(second_derive, 1)
        self.assertEqual(self.run_main(["t"], change=True), 0)
        self.assertEqual(len(self.world.passphrase_changes), 1)  # -p ran once, not repeated
        self.world.calls.clear()
        self.world.passphrase_changes.clear()
        seen["derives"] = 0
        self.scripted_failures(second_derive, 1)
        self.assertEqual(self.run_main(["e"], change=True), 1)
        self.assertIn("may already have a new passphrase", self.output)

    def test_wrong_passphrase_while_signing_can_be_retried(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.scripted_failures(lambda c: c[1:3] == ["-Y", "sign"], 1)
        self.assertEqual(self.run_main(["y", "y", "y", "t"]), 0)
        self.assertEqual(self.world.recorded, ["codex"])
        self.world.recorded.clear()
        self.scripted_failures(lambda c: c[1:3] == ["-Y", "sign"], 1)
        self.assertEqual(self.run_main(["y", "y", "y", "e"]), 1)
        self.assertEqual(self.world.recorded, [])
        self.assertIn("stopped: the passphrase did not unlock the key", self.output)

    def test_change_passphrase_refuses_untrusted_state(self) -> None:
        key = self.configure_key()
        signers = self.state / "owner-allowed-signers"
        # A temporary-folder state directory (the real check, not patched).
        self.assertEqual(self.run_main([], change=True, trust_temp=False), 1)
        self.assertIn("temporary folder or a project", self.output)
        # Writable by others.
        signers.chmod(0o666)
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("not writable by others", self.output)
        signers.chmod(0o644)
        # A symbolic link.
        real = self.root / "real-signers"
        real.write_text(signers.read_text())
        signers.unlink()
        signers.symlink_to(real)
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("symbolic link", self.output)
        signers.unlink()
        # Inside a Git working tree.
        project_state = self.home / "work" / "state"
        project_state.mkdir(parents=True)
        (self.home / "work" / ".git").mkdir()
        (project_state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        with mock.patch.object(attest, "default_state_root", return_value=project_state):
            out = io.StringIO()
            with mock.patch.object(attest, "_is_terminal", return_value=True), \
                    mock.patch.object(attest, "_in_temp_root", return_value=False), \
                    mock.patch.object(attest.Path, "home", return_value=self.home / "other"), \
                    contextlib.redirect_stderr(out):
                self.assertEqual(attest.main(["--change-passphrase"]), 1)
        self.assertIn("temporary folder or a project", out.getvalue())
        self.assertEqual(self.world.passphrase_changes, [])
        self.assertTrue(key.exists())

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


class RealTerminalTests(unittest.TestCase):
    """A mocked input() cannot show what the terminal echoes, so use a real pseudo-terminal."""

    NO_ECHO = ("import sys, termios\n"
               "def broken(*a, **k):\n"
               "    raise termios.error('simulated')\n"
               "termios.tcsetattr = broken\n"
               "import runpy\n"
               "sys.argv = sys.argv[1:]\n"
               "runpy.run_path(sys.argv[0], run_name='__main__')\n")

    def run_in_pty(self, secret: str, *, break_echo: bool = False) -> str:
        import pty
        import select
        import time

        root = Path(tempfile.mkdtemp(prefix="attest-pty-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        home, state, project = root / "home", root / "state", root / "project"
        for directory in (home, state):
            directory.mkdir(mode=0o755)
        (project / ".codex/orchestration").mkdir(parents=True)
        (project / ".codex/orchestration/cli.py").write_text("")
        # A throwaway key pair for this test only; it is never used to sign anything.
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "throwaway")],
                       check=True, capture_output=True)
        fields = (root / "throwaway.pub").read_text().split()
        (state / "owner-allowed-signers").write_text(f"codex-agents-owner {fields[0]} {fields[1]}\n")
        env = dict(os.environ, HOME=str(home))
        env.pop("SSH_AUTH_SOCK", None)
        args = [str(_SCRIPT), "--target", str(project), "--state-root", str(state)]
        command = [sys.executable, "-c", self.NO_ECHO, *args] if break_echo else [sys.executable, *args]
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, command, env)
        finish = _child_finisher(pid, fd)
        self.addCleanup(finish)
        transcript = ""
        sent = False
        start = time.time()
        end = start + 30
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 65536).decode(errors="replace")
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
            if break_echo and not sent and time.time() - start > 2:
                try:  # if the program were still reading, this would be echoed back
                    os.write(fd, (secret + "\r").encode())
                except OSError:
                    pass
                sent = True
            if not sent and "Press Enter to use" in transcript:
                time.sleep(0.3)
                os.write(fd, (secret + "\r").encode())
                sent = True
        finish()
        self.assertTrue(sent or break_echo, transcript)
        return transcript

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").exists(), "needs the system ssh-keygen")
    def test_a_rejected_key_location_entry_is_never_echoed(self) -> None:
        secret = "tangerine-Violin-8842-sample"
        transcript = self.run_in_pty(secret)
        self.assertIn("That is not a file location.", transcript)
        self.assertNotIn(secret, transcript)
        self.assertNotIn("Violin", transcript)

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").exists(), "needs the system ssh-keygen")
    def test_a_slash_prefixed_entry_is_never_printed_back(self) -> None:
        for secret in ("/tangerine-violin-8842", "~/tangerine-violin-8842"):
            with self.subTest(secret=secret):
                transcript = self.run_in_pty(secret)
                self.assertNotIn("tangerine", transcript)
                self.assertNotIn("violin-8842", transcript)
                self.assertIn("Stopped:", transcript)

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").exists(), "needs the system ssh-keygen")
    def test_if_echo_cannot_be_turned_off_nothing_is_read(self) -> None:
        secret = "tangerine-violin-8842"
        transcript = self.run_in_pty(secret, break_echo=True)
        self.assertNotIn(secret, transcript)
        self.assertIn("cannot hide what you type", transcript)


class PassphraseRetryTerminalTests(unittest.TestCase):
    """A wrong passphrase offers Try again / Exit, checked with the real ssh-keygen and a pty."""

    WRONG = "Wrong-Horse-1"
    RIGHT = "Right-Horse-2"

    def setUp(self) -> None:
        if not Path("/usr/bin/ssh-keygen").exists():
            self.skipTest("needs the system ssh-keygen")
        self.root = Path(tempfile.mkdtemp(prefix="attest-retry-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home, self.state, self.project = (self.root / n for n in ("home", "state", "project"))
        (self.home / ".ssh").mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o755)
        (self.project / ".codex/orchestration").mkdir(parents=True)
        # Every CLI call reports a logged-out provider, so a success ends with "not logged in".
        (self.project / ".codex/orchestration/cli.py").write_text(
            "import json\nprint(json.dumps({'ready': True, 'records': []}))\n")
        # Throwaway keys for these tests only; they never sign anything real.
        self.key = self.home / ".ssh" / "codex-agents-owner"
        self.make_key(self.key, self.RIGHT)
        self.write_signers(self.key)

    def make_key(self, path: Path, passphrase: str) -> None:
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)],
                       check=True, capture_output=True)
        Path(str(path) + ".pub").unlink()  # the wizard must not rely on a .pub next to the key

    def write_signers(self, key: Path) -> None:
        pub = subprocess.run(["/usr/bin/ssh-keygen", "-y", "-P", self.RIGHT, "-f", str(key)],
                             check=True, capture_output=True, text=True).stdout.split()
        (self.state / "owner-allowed-signers").write_text(f"codex-agents-owner {pub[0]} {pub[1]}\n")

    def drive(self, script: list[tuple[str, str]]) -> tuple[str, int]:
        import pty
        import select
        import time

        env = dict(os.environ, HOME=str(self.home))
        env.pop("SSH_AUTH_SOCK", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(_SCRIPT), "--target", str(self.project),
                                       "--state-root", str(self.state)], env)
        finish = _child_finisher(pid, fd)
        self.addCleanup(finish)
        transcript = buffer = ""
        steps = list(script)
        end = time.time() + 40
        status = 0
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 65536).decode(errors="replace")
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
                buffer += chunk
            if steps and steps[0][0] in buffer:
                expected, answer = steps.pop(0)
                buffer = buffer[buffer.index(expected) + len(expected):]
                time.sleep(0.3)
                os.write(fd, (answer + "\r").encode())
        status = finish()
        self.assertEqual(steps, [], f"never saw the prompts for {steps}: {transcript}")
        return transcript, os.WEXITSTATUS(status)

    def no_passphrase_in(self, transcript: str) -> None:
        for text in (self.WRONG, self.RIGHT):
            self.assertNotIn(text, transcript)

    def test_wrong_passphrase_then_try_again_succeeds(self) -> None:
        transcript, code = self.drive([
            ("Enter passphrase", self.WRONG), ("Try again or Exit?", ""),
            ("Enter passphrase", self.RIGHT)])
        self.assertIn("That passphrase did not unlock the key.", transcript)
        self.assertIn("is not logged in", transcript)  # past the unlock
        self.assertEqual(code, 1)  # nothing was attested
        self.no_passphrase_in(transcript)

    def test_wrong_passphrase_twice_then_right(self) -> None:
        transcript, _ = self.drive([
            ("Enter passphrase", self.WRONG), ("Try again or Exit?", "t"),
            ("Enter passphrase", self.WRONG), ("Try again or Exit?", ""),
            ("Enter passphrase", self.RIGHT)])
        self.assertEqual(transcript.count("That passphrase did not unlock the key."), 2)
        self.assertIn("is not logged in", transcript)
        self.no_passphrase_in(transcript)

    def test_wrong_passphrase_then_exit_stops_cleanly(self) -> None:
        transcript, code = self.drive([("Enter passphrase", self.WRONG), ("Try again or Exit?", "e")])
        self.assertIn("stopped: the passphrase did not unlock the key", transcript)
        self.assertNotIn("is not logged in", transcript)
        self.assertEqual(code, 1)
        self.no_passphrase_in(transcript)

    def test_a_key_that_is_not_the_owner_key_stops_without_the_retry_menu(self) -> None:
        other = self.root / "other-key"
        self.make_key(other, self.RIGHT)
        self.write_signers(other)  # the configured owner key is a different key
        transcript, code = self.drive([("Enter passphrase", self.RIGHT)])
        self.assertIn("does not match the configured owner key", transcript)
        self.assertNotIn("Try again or Exit", transcript)
        self.assertNotIn("did not unlock", transcript)
        self.assertEqual(code, 1)
        self.no_passphrase_in(transcript)


if __name__ == "__main__":
    unittest.main()
