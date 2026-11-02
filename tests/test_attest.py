from __future__ import annotations

import base64
import contextlib
import hashlib
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
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "attest.py"
_SPEC = importlib.util.spec_from_file_location("umbrella_attest", _SCRIPT)
attest = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = attest
_SPEC.loader.exec_module(attest)
_REAL_CHECK = attest.check_ssh_keygen
_REAL_RUNTIME_CHECK = attest.check_project_runtime
_REAL_RUN = subprocess.run
_ORCHESTRATION = attest.NATIVE_PACKAGE / ".codex" / "orchestration"


def _runtime_payload(project_id: str, grant: dict) -> tuple[dict, bytes]:
    """The record and signing payload exactly as the runtime builds them (its own code)."""

    sys.path.insert(0, str(_ORCHESTRATION))
    try:
        from codex_agents.learning_engine import OwnerGrantAuthority
        from codex_agents.providers import billing_attestation_record
    finally:
        sys.path.remove(str(_ORCHESTRATION))
    record = billing_attestation_record(grant)
    return record, OwnerGrantAuthority.owner_record_payload(project_id, record)


def outside_temporary_folders() -> Path | None:
    """An existing folder outside every temporary root, for private scratch in tests."""

    candidates = []
    if sys.platform == "darwin":
        candidates.append(subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                                         text=True, check=False).stdout.strip())
    candidates += [os.environ.get("XDG_RUNTIME_DIR") or "", str(Path(__file__).resolve().parents[1])]
    temporary = [Path(os.path.realpath(path)) for path in
                 ("/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp")]
    for candidate in candidates:
        real = Path(os.path.realpath(candidate)) if candidate else None
        if real and real.is_dir() and not any(real == root or root in real.parents for root in temporary):
            return real
    return None


def private_base(test: unittest.TestCase) -> Path:
    """A fresh owner-only folder outside the temporary roots, removed after the test, or skip."""

    outside = outside_temporary_folders()
    if outside is None:
        test.skipTest("no folder outside the temporary roots is available")
    base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
    test.addCleanup(shutil.rmtree, base, True)
    return base


PROJECT_ID = "prj_" + "0123456789abcdef" * 2
# A CI toolcache Python lies outside the runtime's trusted installation folders. The rule is
# tested on its own (IsolationTests); elsewhere this interpreter is treated as installed.
SELF_TRUSTED = attest.python_location_problem(Path(os.path.realpath(sys.executable))) is None
TRUST = "" if SELF_TRUSTED else "+trust"


def trust_this_interpreter(test: unittest.TestCase) -> None:
    if not SELF_TRUSTED:
        patcher = mock.patch.object(attest.host_paths, "install_location_problem", return_value=None)
        patcher.start()
        test.addCleanup(patcher.stop)

# Runs attest.py the way setup.sh does (isolated), pointed at a test bundle and scratch base:
#   python -I -B -S -c RUNNER attest.py BUNDLE SCRATCH MODE ARGS...
RUNNER = (
    "import importlib.util, sys\n"
    "from pathlib import Path\n"
    "spec = importlib.util.spec_from_file_location('attest_under_test', sys.argv[1])\n"
    "attest = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(attest)\n"
    "attest.NATIVE_PACKAGE = Path(sys.argv[2])\n"
    "attest.SCRATCH_BASE = sys.argv[3]\n"
    "import os\n"
    "attest.ACCOUNT_HOME = os.environ['HOME']  # the test's home stands in for the account's\n"
    "if sys.argv[4].endswith('+trust'):\n"
    "    attest.host_paths.install_location_problem = lambda path: None\n"
    "if sys.argv[4].startswith('break-echo'):\n"
    "    import termios\n"
    "    def broken(*a, **k):\n"
    "        raise termios.error('simulated')\n"
    "    termios.tcsetattr = broken\n"
    "sys.exit(attest.main(sys.argv[5:]))\n"
)


def stub_install(root: Path, project: Path, cli_text: str) -> Path:
    """Install a stub CLI in project and a matching one-file bundle; return the bundle."""

    bundle = root / "bundle"
    for base in (bundle, project):
        (base / ".codex/orchestration").mkdir(parents=True, exist_ok=True)
        (base / ".codex/orchestration/cli.py").write_text(cli_text)
    (bundle / "VERSION").write_text("9.9.9\n")
    (project / ".codex/orchestration/install-manifest.json").write_text(
        json.dumps({"format": 1, "version": "9.9.9", "files": {}}))
    return bundle

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


def fake_signature(data: bytes) -> bytes:
    return attest.SIGNATURE_HEAD + b"\n" + hashlib.sha256(data).hexdigest().encode() + b"\n"


class FakeWorld:
    """Stands in for ssh-keygen and the orchestration CLI; records what was asked.

    The CLI builds its record and signing payload with the runtime's own code, so the
    attestation helper's local reconstruction is checked against the real construction.
    """

    def __init__(self, root: Path, *, sign_ok: bool = True, logged_in: bool = True) -> None:
        self.root = root
        self.encrypted = True
        self.derived = f"ssh-ed25519 AAAAkey {COMMENT}\n"
        self.envs: list[dict] = []
        self.sign_ok = sign_ok
        self.logged_in = logged_in
        self.calls: list[list[str]] = []
        self.grants: dict[str, dict] = {}
        self.handed_over: list[str] = []  # the paths the CLI was given for key and signature
        self.recorded: list[str] = []
        self.signed_names: list[str] = []
        self.passphrase_changes: list[list[str]] = []
        self.change_rc = 0
        self.grant_inputs: list[str] = []
        self.payload_override = None  # callable(grant, record, payload) -> bytes
        self.enrol_as: str | None = None  # a different key the state ends up holding
        self.sign_other_bytes = False
        self.cli_commands: list[list[str]] = []
        self.signed_inputs: list[bytes] = []
        self.key_files: list[tuple[str, bytes | None]] = []  # each -f file and its bytes then

    def _cli(self, command: list[str], stdin: str | None) -> subprocess.CompletedProcess:
        self.cli_commands.append(command)
        assert command[1 + len(attest.PYTHON_FLAGS)] == str(attest.clone_cli()), "never the project's CLI"
        args = command[2 + len(attest.PYTHON_FLAGS):]
        name = args[0]
        option = lambda flag: args[args.index(flag) + 1]  # noqa: E731
        if name == "learning-configure-owner-key":
            public_path = Path(option("--public-key"))
            self.handed_over.append(str(public_path))
            pub = public_path.read_text().split()
            state = Path(option("--state-root"))
            enrolled = self.enrol_as or f"{pub[0]} {pub[1]}"
            (state / "owner-allowed-signers").write_text(f"codex-agents-owner {enrolled}\n")
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
            assert option("--grant") == "-", "the grant must travel on stdin"
            self.grant_inputs.append(stdin)
            grant = json.loads(stdin)
            self.grants[grant["provider"]] = grant
            record, payload = _runtime_payload(PROJECT_ID, grant)
            if "--signature" in args:
                signature = Path(option("--signature"))
                self.handed_over.append(str(signature))
                assert signature.read_bytes() == fake_signature(payload)
                self.recorded.append(grant["provider"])
                report = {"ready": True}
            else:
                if self.payload_override is not None:
                    payload = self.payload_override(grant, record, payload)
                report = {"ready": True, "requires_signature": True, "record": record,
                          "signing_payload_b64": base64.b64encode(payload).decode()}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    def run(self, command, **kwargs):
        if command[0] == attest.GETCONF:  # the private-folder lookup is real
            return _REAL_RUN(command, **kwargs)
        self.calls.append([str(part) for part in command])
        if [str(part) for part in command[1:1 + len(attest.PYTHON_FLAGS)]] == list(attest.PYTHON_FLAGS):
            assert command[0] == os.path.realpath(sys.executable), command[0]
            return self._cli([str(part) for part in command], kwargs.get("input"))
        assert command[0] == "/usr/bin/ssh-keygen", command[0]
        self.envs.append(kwargs["env"])
        if "-f" in command and command[command.index("-f") + 1] != "-":
            used = Path(command[command.index("-f") + 1])
            self.key_files.append((str(used), used.read_bytes() if used.is_file() else None))
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
            assert command[2:] == ["-f", "-"], "the public key goes on stdin, never a file"
            assert COMMENT not in kwargs["input"]  # only type and blob are ever shown
            return subprocess.CompletedProcess(command, 0, "256 SHA256:publicprint (ED25519)\n", "")
        if command[1:3] == ["-Y", "check-novalidate"]:
            source = command[command.index("-s") + 1]
            assert source.startswith("/dev/fd/"), "the signature goes through a pipe, never a file"
            good = Path(source).read_bytes() == fake_signature(kwargs["input"])
            return subprocess.CompletedProcess(command, 0 if good else 255, b"", b"")
        assert command[1:3] == ["-Y", "sign"]
        assert command[3:] == ["-f", command[4], "-n", attest.NAMESPACE], "no file to sign"
        self.signed_names.append(command[command.index("-n") + 1])
        self.signed_inputs.append(kwargs["input"])
        if self.sign_ok:
            signed = kwargs["input"] + (b"other" if self.sign_other_bytes else b"")
            return subprocess.CompletedProcess(command, 0, fake_signature(signed),
                                               b"Signing data on standard input\n")
        return subprocess.CompletedProcess(command, 1, b"", b"")


class AttestHarness(unittest.TestCase):
    """A project, a state folder, a home and a fake world; private scratch outside temp."""

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
        self.scratch_base = private_base(self)
        trust_this_interpreter(self)
        for patcher in (mock.patch.object(attest, "check_ssh_keygen"),
                        mock.patch.object(attest, "check_project_runtime"),
                        mock.patch.object(attest, "SCRATCH_BASE", str(self.scratch_base)),
                        mock.patch.object(attest, "ACCOUNT_HOME", str(self.home))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_providers(self, **enabled: bool) -> None:
        config = {"version": 1, "providers": {n: {"enabled": v} for n, v in enabled.items()}}
        (self.target / ".codex/orchestration/provider-routing.json").write_text(json.dumps(config))

    def ssh_dir(self) -> Path:
        """The test account's ~/.ssh (ACCOUNT_HOME stands in for the account database's home)."""

        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        return ssh_dir

    def assert_used_a_removed_copy_of(self, key: Path, call: list[str]) -> Path:
        """ssh-keygen ran `call` on a private copy of `key` (its bytes when the operation began)
        in the owner's scratch container, and the copy is gone now."""

        used = Path(call[call.index("-f") + 1])
        self.assertEqual(used.name, "owner-key")
        self.assertTrue(used.parent.name.startswith("key-"), used)
        self.assertEqual(Path(os.path.realpath(used.parent.parent)),
                         Path(os.path.realpath(self.scratch_base)) / attest.SCRATCH_CONTAINER)
        self.assertFalse(os.path.lexists(used.parent), "the key copy was not removed")
        self.assertIn((str(used), key.read_bytes()), self.world.key_files)
        return used

    def configure_key(self) -> Path:
        key = self.ssh_dir() / "codex-agents-owner"
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



class AttestTests(AttestHarness):
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
        other = self.ssh_dir() / "mykey"
        write_key(other)
        Path(str(other) + ".pub").write_text(f"ssh-ed25519 AAAAkey {COMMENT}\n")
        self.assertEqual(self.run_main(["n", str(other), "y", "y", "y"]), 0)
        self.assertFalse(any(c[:2] == ["/usr/bin/ssh-keygen", "-t"] for c in self.world.calls))
        self.assertTrue(any("-y" in c and "-P" not in c for c in self.world.calls))
        self.assert_used_a_removed_copy_of(other, next(c for c in self.world.calls if "-Y" in c))

    def test_pub_mismatch_is_refused(self) -> None:
        self.set_providers(codex=True)
        other = self.ssh_dir() / "mykey"
        write_key(other)
        Path(str(other) + ".pub").write_text("ssh-ed25519 AAAAdifferent c\n")
        self.assertEqual(self.run_main(["n", str(other)]), 1)
        self.assertIn("does not match", self.output)
        self.assertFalse(any("learning-configure-owner-key" in c for c in self.world.calls))

    def test_public_key_file_cannot_be_the_signing_key(self) -> None:
        self.set_providers(codex=True)
        public = self.ssh_dir() / "mykey.pub"
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
        self.assert_used_a_removed_copy_of(key, next(c for c in self.world.calls if "-Y" in c))

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
        other = self.ssh_dir() / "elsewhere-key"
        write_key(other)
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["~/.ssh/elsewhere-key", "y", "y", "y"]), 0)
        self.assertIn("This asks for a file location, not your passphrase.", self.output)
        self.assertIn("Press Enter to use ~/.ssh/codex-agents-owner", self.prompts[0])
        self.assertNotIn("passphrase:", self.prompts[0].lower())
        self.assert_used_a_removed_copy_of(other, next(c for c in self.world.calls if "-Y" in c))

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
        before = key.stat().st_ino
        self.assertEqual(self.run_main([], change=True), 0)
        self.assertEqual(len(self.world.passphrase_changes), 1)
        self.assertEqual(self.world.passphrase_changes[0][:3], ["/usr/bin/ssh-keygen", "-p", "-f"])
        # -p ran on a private copy, which is gone; the key file is a new inode with its bytes.
        self.assert_used_a_removed_copy_of(key, self.world.passphrase_changes[0])
        self.assertNotEqual(key.stat().st_ino, before)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in key.parent.iterdir()), [key.name])
        self.assertNotIn("SSH_AUTH_SOCK", self.world.envs[-1])
        calls = [c for c in self.world.calls if c[0] == "/usr/bin/ssh-keygen"]
        self.assertEqual(calls[-1][:2], ["/usr/bin/ssh-keygen", "-y"])  # verified afterwards
        self.assertFalse(any("-Y" in c for c in self.world.calls))
        self.assertIn("Passphrase changed", self.output)

    def key_copies_left(self) -> list[str]:
        container = self.scratch_base / attest.SCRATCH_CONTAINER
        return sorted(p.name for p in container.iterdir() if p.name.startswith("key-")) if container.exists() else []

    def test_a_retained_descriptor_cannot_change_what_is_checked_and_derived(self) -> None:
        # A program kept a writable descriptor on the key's inode (opened through an alias in a
        # folder it could write, since unlinked, so the link count is 1). Right after the
        # passphrase check it rewrites the key; the derivation still sees the checked bytes.
        key = self.configure_key()
        original = key.read_bytes()
        retained = os.open(key, os.O_WRONLY)
        self.addCleanup(os.close, retained)
        real = self.world.run
        checks = {"n": 0}

        def rewrite_after_the_operations_check(command, **kwargs):
            result = real(command, **kwargs)
            if command[0] == attest.SSH_KEYGEN and command[1] == "-y" and "-P" in command:
                checks["n"] += 1
                if checks["n"] == 2:  # the check on the operation's own copy
                    os.ftruncate(retained, 0)
                    os.pwrite(retained, b"-----BEGIN OPENSSH PRIVATE KEY-----\nplanted\n", 0)
            return result

        self.world.run = rewrite_after_the_operations_check
        with mock.patch.object(attest.subprocess, "run", self.world.run), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(attest.ensure_owner_key(self.target, self.state, None), key)
        derive = [(path, data) for path, data in self.world.key_files
                  if path.endswith("/owner-key")]
        self.assertGreaterEqual(len(derive), 3)
        check_and_derive = [entry for entry in derive if entry[1] is not None][-2:]
        self.assertEqual({data for _path, data in check_and_derive}, {original})
        self.assertEqual(len({path for path, _data in check_and_derive}), 1)  # one copy for both
        self.assertNotEqual(key.read_bytes(), original)  # the rewrite did land on the key file
        self.assertEqual(self.key_copies_left(), [])

    def test_the_key_copy_is_removed_when_the_operation_fails(self) -> None:
        self.configure_key()
        self.world.derived = "ssh-ed25519 AAAAother\n"  # not the configured key
        with mock.patch.object(attest.subprocess, "run", self.world.run), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(attest.AttestError, "does not match"):
                attest.ensure_owner_key(self.target, self.state, None)
        self.assertTrue(any(path.endswith("/owner-key") for path, _ in self.world.key_files))
        self.assertEqual(self.key_copies_left(), [])
        self.world.derived = f"ssh-ed25519 AAAAkey {COMMENT}\n"
        self.world.change_rc = 1
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertEqual(self.key_copies_left(), [])

    def test_after_a_passphrase_change_a_retained_descriptor_writes_to_an_unused_file(self) -> None:
        key = self.configure_key()
        retained = os.open(key, os.O_WRONLY)
        self.addCleanup(os.close, retained)
        self.assertEqual(self.run_main([], change=True), 0)
        changed = key.read_bytes()
        os.pwrite(retained, b"planted", 0)
        self.assertEqual(key.read_bytes(), changed)
        self.assertEqual(self.key_copies_left(), [])

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
        self.assertIn("your key file is as it was", self.output)  # -p only touched the copy
        self.world.change_rc = 0
        self.world.passphrase_changes.clear()
        self.world.derived = "ssh-ed25519 AAAAother\n"
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("does not match", self.output)
        self.assertEqual(self.world.passphrase_changes, [])  # -p never ran on the wrong key

    def test_a_failed_check_after_the_change_keeps_the_key_file(self) -> None:
        key = self.configure_key()
        before = key.stat().st_ino
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
        self.assertIn("The new passphrase was not saved", self.output)
        self.assertIn("non-empty", self.output)
        self.assertEqual(key.stat().st_ino, before)  # never replaced

    def test_an_empty_new_passphrase_is_never_saved(self) -> None:
        self.configure_key()
        real = self.world.run

        def empties_the_passphrase(command, **kwargs):
            if command[0] == "/usr/bin/ssh-keygen" and command[1] == "-p":
                self.world.encrypted = False
            return real(command, **kwargs)

        self.world.run = empties_the_passphrase
        self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("The new passphrase was not saved", self.output)

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
        self.assertIn("your key file is as it was", self.output)

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
        self.assertIn("The new passphrase was not saved", self.output)

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
        attest.check_key_location(self.configure_key())

    # owner private keys only from the account's own ~/.ssh

    def no_keygen_ran(self, key: Path | None = None) -> None:
        """No ssh-keygen call at all, or (with `key`) none that names that key file."""

        calls = [c for c in self.world.calls if "ssh-keygen" in c[0]]
        self.assertFalse([c for c in calls if key is None or str(key) in c])

    def test_a_private_key_outside_the_account_ssh_folder_is_refused_at_every_use(self) -> None:
        # A key in a project or temporary folder could be swapped between the checks and the
        # signing by a model that can write there; none of the key operations accepts one.
        elsewhere = self.root / "project-keys"
        elsewhere.mkdir(mode=0o700)
        key = elsewhere / "owner"
        write_key(key)
        self.ssh_dir()
        sneaky = self.home / ".ssh" / ".." / "project-keys" / "owner"
        for path in (key, sneaky, self.home / "owner", self.home / ".ssh"):
            for name, call in (("validate", lambda: attest.validate_private_key(path)),
                               ("derive", lambda: attest.derive_public(path)),
                               ("sign", lambda: attest.sign_in_memory(path, b"payload")),
                               ("create", lambda: attest._create_key(path.parent / "new-key"))):
                with self.subTest(path=str(path), call=name), \
                        mock.patch.object(attest.subprocess, "run", self.world.run), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(attest.AttestError, r"own ~/\.ssh folder"):
                        call()
        self.no_keygen_ran()

    def test_a_key_with_another_hard_link_is_refused_at_every_use(self) -> None:
        # A key moved (not copied) into ~/.ssh from a folder an agent can write keeps that
        # folder's link: the agent could rewrite this same file after the checks.
        key = self.configure_key()
        writable = self.root / "project-keys"
        writable.mkdir(mode=0o700)
        os.link(key, writable / "owner")
        for name, call in (("validate", lambda: attest.validate_private_key(key)),
                           ("derive", lambda: attest.derive_public(key)),
                           ("sign", lambda: attest.sign_in_memory(key, b"payload")),
                           ("location", lambda: attest.check_key_location(key))):
            with self.subTest(call=name), \
                    mock.patch.object(attest.subprocess, "run", self.world.run), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(attest.AttestError) as caught:
                    call()
                self.assertEqual(str(caught.exception), attest.KEY_HAS_LINK)
        # The passphrase change refuses it as well, before ssh-keygen ever opens the key.
        with mock.patch.object(attest, "configured_public_key", return_value=("ssh-ed25519", "AAAAkey")), \
                mock.patch.object(attest, "locate_key_file", return_value=key), \
                mock.patch.object(attest, "describe_key", return_value="ssh-ed25519 SHA256:x"):
            self.assertEqual(self.run_main([], change=True), 1)
        self.assertIn("another link elsewhere", self.output)
        self.no_keygen_ran(key)
        # Also on the descriptor validation opens: a link made after the path check is caught.
        (writable / "owner").unlink()
        real = os.lstat

        def one_link(path):
            info = real(path)
            if Path(path) == key:
                fields = list(info)
                fields[3] = 1
                return os.stat_result(fields)
            return info

        os.link(key, writable / "owner")
        with mock.patch.object(attest, "_lstat", one_link), mock.patch.object(attest.Path, "lstat",
                                                                               lambda path: one_link(path)):
            with self.assertRaisesRegex(attest.AttestError, "another link elsewhere"):
                attest.validate_private_key(key)
        # A fresh copy (one link) is accepted again.
        (writable / "owner").unlink()
        with mock.patch.object(attest.subprocess, "run", self.world.run):
            attest.check_key_location(key)

    def test_enrolling_a_typed_key_location_outside_ssh_is_refused_without_echo(self) -> None:
        self.set_providers(codex=True)
        other = self.root / "mykey"
        write_key(other)
        Path(str(other) + ".pub").write_text(f"ssh-ed25519 AAAAkey {COMMENT}\n")
        for entered in (str(other), "~/Work/owner-key", "~/.ssh/../mykey"):
            with self.subTest(entered=entered):
                self.assertEqual(self.run_main(["n", entered]), 1)
                self.assertIn("own ~/.ssh folder", self.output)
                self.assertNotIn(entered, self.output)
        self.no_keygen_ran()
        self.assertFalse(any("learning-configure-owner-key" in c for c in self.world.calls))

    def test_the_key_folder_comes_from_the_account_database_not_home(self) -> None:
        with mock.patch.object(attest, "ACCOUNT_HOME", None), \
                mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            import pwd
            expected = Path(os.path.normpath(pwd.getpwuid(os.getuid()).pw_dir))
            self.assertEqual(attest.account_home(), expected)
            self.assertEqual(attest.default_key(), expected / ".ssh" / "codex-agents-owner")
            self.assertEqual(attest._expand("~/.ssh/k"), expected / ".ssh" / "k")
            # A key under HOME's .ssh is refused when HOME is not the account's home folder.
            if expected != self.home:
                with self.assertRaisesRegex(attest.AttestError, r"own ~/\.ssh folder"):
                    attest.check_key_location(self.configure_key())

    def test_ssh_folder_owner_writability_and_symlinks_are_checked(self) -> None:
        key = self.configure_key()
        attest.check_key_location(key)
        with self._fake_lstat({self.home / ".ssh": (0, stat.S_IFDIR | 0o700)}):
            with self.assertRaisesRegex(attest.AttestError, "owned by you"):
                attest.check_key_location(key)
        for mode in (0o720, 0o702):
            (self.home / ".ssh").chmod(mode)
            with self.assertRaisesRegex(attest.AttestError, "writable by other users"):
                attest.check_key_location(key)
        (self.home / ".ssh").chmod(0o700)
        nested = self.home / ".ssh" / "keys"
        nested.mkdir(mode=0o700)
        write_key(nested / "owner")
        attest.check_key_location(nested / "owner")  # a folder inside ~/.ssh is fine
        real = self.root / "real-keys"
        real.mkdir(mode=0o700)
        write_key(real / "owner")
        linked = self.home / ".ssh" / "linked"
        linked.symlink_to(real)
        with self.assertRaisesRegex(attest.AttestError, "symbolic links"):
            attest.check_key_location(linked / "owner")
        (self.home / ".ssh" / "key-link").symlink_to(key)
        with self.assertRaisesRegex(attest.AttestError, "symbolic links"):
            attest.check_key_location(self.home / ".ssh" / "key-link")
        # A root-owned link above the home folder is refused too: no symlink anywhere.
        with self._fake_lstat({self.home: (0, stat.S_IFLNK | 0o777)}):
            with self.assertRaisesRegex(attest.AttestError, "symbolic links"):
                attest.check_key_location(key)
        # A folder above ~/.ssh that others can write.
        with self._fake_lstat({self.home: (os.getuid(), stat.S_IFDIR | 0o777)}):
            with self.assertRaisesRegex(attest.AttestError, "above your ~/.ssh is writable"):
                attest.check_key_location(key)

    def test_the_key_location_is_checked_again_before_signing(self) -> None:
        # The key passed every check while it was derived; then ~/.ssh is swapped for a link.
        self.configure_key()
        self.set_providers(codex=True)
        real_run = self.world.run

        def swapping(command, **kwargs):
            result = real_run(command, **kwargs)
            if command[0] == attest.SSH_KEYGEN and command[1] == "-y" and "-P" not in command:
                swapped = self.root / "swapped"
                shutil.copytree(self.home / ".ssh", swapped)
                shutil.rmtree(self.home / ".ssh")
                (self.home / ".ssh").symlink_to(swapped)
            return result

        with mock.patch.object(self.world, "run", swapping):
            self.assertEqual(self.run_main(["y", "y", "y"]), 1)
        self.assertIn("symbolic links", self.output)
        self.assertFalse(any(c[1:3] == ["-Y", "sign"] for c in self.world.calls))
        self.assertEqual(self.world.recorded, [])

    def test_change_passphrase_refuses_a_key_outside_ssh(self) -> None:
        (self.state / "owner-allowed-signers").write_text("codex-agents-owner ssh-ed25519 AAAAkey\n")
        other = self.root / "mykey"
        write_key(other)
        self.assertEqual(self.run_main([str(other)], change=True), 1)
        self.assertIn("own ~/.ssh folder", self.output)
        self.assertEqual(self.world.passphrase_changes, [])
        self.no_keygen_ran(other)

    def test_a_new_key_creates_only_ssh_itself_owner_only(self) -> None:
        self.set_providers(codex=True)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertEqual(self.run_main(["y", "y", "y", "y"]), 0)
        self.assertEqual((self.home / ".ssh").stat().st_mode & 0o777, 0o700)
        created = [c for c in self.world.calls if c[:2] == [attest.SSH_KEYGEN, "-t"]]
        self.assertEqual([c[c.index("-f") + 1] for c in created],
                         [str(self.home / ".ssh" / "codex-agents-owner")])
        nested = self.home / ".ssh" / "missing" / "key"
        with self.assertRaisesRegex(attest.AttestError, r"own ~/\.ssh folder"):
            attest._create_key(nested)
        self.assertFalse(nested.parent.exists())

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

    def test_nothing_the_owner_approves_touches_disk(self) -> None:
        self.set_providers(codex=True)
        real_scratch = attest.make_scratch

        def key_copies_only(prefix, *args, **kwargs):
            # The only files: each operation's private copy of the key, never the statement.
            assert prefix == "key-", prefix
            return real_scratch(prefix, *args, **kwargs)

        with mock.patch.object(attest, "make_scratch", side_effect=key_copies_only):
            self.assertEqual(self.run_main(["y", "y", "y", "y"]), 0)  # enrol a new key, then sign
        # The enrolled key and the signature reached the CLI through pipes.
        self.assertEqual(len(self.world.handed_over), 2)
        self.assertTrue(all(path.startswith("/dev/fd/") for path in self.world.handed_over))
        # ssh-keygen signed exactly the approved bytes, given on stdin.
        grant = self.world.grants["codex"]
        self.assertEqual(self.world.signed_inputs, [_runtime_payload(PROJECT_ID, grant)[1]])
        self.assertEqual(self.world.recorded, ["codex"])

    def test_a_failed_signature_records_nothing(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.sign_ok = False
        self.assertEqual(self.run_main(["y", "y", "y"]), 1)
        self.assertIn("signing did not finish", self.output)
        self.assertEqual(self.world.recorded, [])

    def test_signal_handler_removes_temp_dirs(self) -> None:
        scratch = attest.make_scratch("attest-")
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


class SignOnlyWhatWasApprovedTests(AttestHarness):
    """attest.py signs only the statement the owner approved, and enrols only the key shown."""

    def test_grant_goes_on_stdin_and_never_through_a_file(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.assertEqual(self.run_main(["y", "y", "y"]), 0)
        self.assertEqual(self.world.recorded, ["codex"])
        self.assertEqual(len(self.world.grant_inputs), 2)  # the request and the signed record
        approved = self.world.grants["codex"]
        for sent in self.world.grant_inputs:
            self.assertEqual(json.loads(sent), approved)
        billing = [c for c in self.world.cli_commands if "learning-record-billing-attestation" in c]
        for command in billing:
            self.assertEqual(command[command.index("--grant") + 1], "-")

    def test_a_payload_for_another_statement_is_refused_before_signing(self) -> None:
        def flipped(grant, record, payload):  # credit spending flipped behind the owner's back
            other = dict(grant, codex_credits_spending_disabled=True)
            return _runtime_payload(PROJECT_ID, other)[1]

        def purpose(grant, record, payload):
            data = json.loads(payload)
            data["purpose"] = "authenticated_verified_qa"
            return attest.canonical_json(data)

        def extra(grant, record, payload):
            return attest.canonical_json({**json.loads(payload), "extra": True})

        def project(grant, record, payload):
            return attest.canonical_json({**json.loads(payload), "project_id": "../other"})

        def spaced(grant, record, payload):
            return json.dumps(json.loads(payload), sort_keys=True).encode()

        cases = {"another statement": flipped, "another purpose": purpose, "an extra field": extra,
                 "a malformed project": project, "non-canonical bytes": spaced,
                 "not JSON": lambda grant, record, payload: b"\xffpayload"}
        for label, override in cases.items():
            with self.subTest(case=label):
                self.setUp()
                self.configure_key()
                self.set_providers(codex=True)
                self.world.payload_override = override
                self.assertEqual(self.run_main(["y", "n", "y"]), 1)
                self.assertFalse(any(c[1:3] == ["-Y", "sign"] for c in self.world.calls), label)
                self.assertEqual(self.world.recorded, [])
                self.assertTrue("not the one you approved" in self.output
                                or "no usable statement" in self.output, self.output)

    def test_the_local_payload_matches_the_runtime_construction(self) -> None:
        grant = {"version": 1, "provider": "codex", "account_fingerprint": FINGERPRINT,
                 "no_paid_overage": True, "no_usage_credits": True, "issued_at": 1_700_000_000_000,
                 "codex_credits_spending_disabled": True}
        record, payload = _runtime_payload(PROJECT_ID, grant)
        self.assertEqual(attest.billing_record(grant), record)
        prepared = {"signing_payload_b64": base64.b64encode(payload).decode(), "record": record}
        self.assertEqual(attest.approved_payload(grant, prepared), payload)
        with self.assertRaises(attest.AttestError):  # a different returned record is refused
            attest.approved_payload(grant, {**prepared, "record": {**record, "provider": "claude"}})

    def test_a_signature_over_other_bytes_is_never_recorded(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.world.sign_other_bytes = True
        self.assertEqual(self.run_main(["y", "y", "y"]), 1)
        self.assertIn("does not cover the approved statement", self.output)
        self.assertEqual(self.world.recorded, [])

    def test_enrolment_is_refused_when_state_shows_another_key(self) -> None:
        self.set_providers(codex=True)
        self.world.enrol_as = "ssh-ed25519 AAAAattacker"
        self.assertEqual(self.run_main(["y", "y", "y", "y"]), 1)
        self.assertIn("not the key shown above, so nothing was signed", self.output)
        self.assertNotIn("Owner public key configured.", self.output)
        self.assertEqual(self.world.grants, {})
        self.assertFalse(any(c[1:3] == ["-Y", "sign"] for c in self.world.calls))

    def test_the_runtime_check_runs_before_any_cli_call(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        with mock.patch.object(attest, "check_project_runtime",
                               side_effect=attest.AttestError("the orchestrator ... not an exact copy")):
            self.assertEqual(self.run_main([]), 1)
        self.assertIn("not an exact copy", self.output)
        self.assertEqual(self.world.cli_commands, [])
        self.assertFalse(any("ssh-keygen" in c[0] for c in self.world.calls))

    def test_this_repositorys_cli_runs_isolated_never_the_projects(self) -> None:
        self.configure_key()
        self.set_providers(codex=True)
        self.run_main(["y", "y", "y"])
        self.assertTrue(self.world.cli_commands)
        clone = attest.NATIVE_PACKAGE / ".codex" / "orchestration" / "cli.py"
        for command in self.world.cli_commands:
            self.assertEqual(command[0], os.path.realpath(sys.executable))
            self.assertEqual(command[1:6], ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"])
            self.assertEqual(command[6], str(clone))
            self.assertNotIn(str(self.target / ".codex"), " ".join(command[6:7]))
            if command[7] != "learning-configure-owner-key":
                self.assertEqual(command[command.index("--project-root") + 1], str(self.target))

    def test_a_second_signer_in_the_owner_key_record_is_refused(self) -> None:
        key = self.configure_key()
        self.set_providers(codex=True)
        signers = self.state / "owner-allowed-signers"
        line = signers.read_text()
        for content in (line + "codex-agents-owner ssh-ed25519 AAAAattacker\n",
                        line.rstrip("\n") + " comment\n", 'namespaces="x" ' + line, line + "\n"):
            with self.subTest(content=content[:30]):
                signers.write_text(content)
                self.assertEqual(self.run_main([]), 1)
                self.assertIn("does not hold exactly one owner key", self.output)
                self.assertFalse(any(c[1:3] == ["-Y", "sign"] for c in self.world.calls))
        signers.write_text("")
        self.assertIsNone(attest.configured_public_key(self.state))
        self.assertTrue(key.exists())

    def test_enrolment_is_refused_when_state_gains_a_second_signer(self) -> None:
        self.set_providers(codex=True)
        real = self.world._cli

        def append_attacker(command, stdin):
            result = real(command, stdin)
            if "learning-configure-owner-key" in command:
                with (self.state / "owner-allowed-signers").open("a") as handle:
                    handle.write("codex-agents-owner ssh-ed25519 AAAAattacker\n")
            return result

        self.world._cli = append_attacker
        self.assertEqual(self.run_main(["y", "y", "y", "y"]), 1)
        self.assertIn("does not hold exactly one owner key", self.output)
        self.assertEqual(self.world.grants, {})


class StateRootTests(unittest.TestCase):
    """The owner tools follow the runtime's learning state folder, never a path of their own."""

    def setUp(self) -> None:
        # Canonical: the runtime refuses a non-canonical XDG_STATE_HOME (/var is /private/var).
        self.home = Path(os.path.realpath(tempfile.mkdtemp(prefix="attest-state-")))
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_the_state_root_is_the_runtimes_owner_state_root(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "XDG_STATE_HOME": str(self.home / ".local" / "state")}):
            self.assertEqual(attest.default_state_root(), attest.host_paths.owner_state_root())
            self.assertEqual(attest.default_state_root(), self.home / ".local" / "state" / "codex-agents")
        # Without XDG_STATE_HOME it is the account database's home, whatever HOME says: every
        # test that resolves the default state must set XDG_STATE_HOME (test_install does).
        import pwd
        environ = {key: value for key, value in os.environ.items() if key != "XDG_STATE_HOME"}
        with mock.patch.dict(os.environ, {**environ, "HOME": str(self.home)}, clear=True):
            account = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
            self.assertEqual(attest.host_paths.default_state_root(), account / ".local" / "state" / "codex-agents")
        self.assertNotIn("Application Support", _SCRIPT.read_text())

    @unittest.skipUnless(sys.platform == "darwin", "the old folder was macOS's")
    def test_the_old_macos_folder_is_used_until_the_collector_step_moves_it(self) -> None:
        old = self.home / "Library" / "Application Support" / "codex-agents"
        old.mkdir(parents=True, mode=0o700)
        new = self.home / ".local" / "state" / "codex-agents"
        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "XDG_STATE_HOME": str(self.home / ".local" / "state")}):
            self.assertEqual(attest.default_state_root(), old)
            new.mkdir(parents=True, mode=0o700)
            self.assertEqual(attest.default_state_root(), new)

    def test_an_unusable_state_location_is_refused_with_its_plain_message(self) -> None:
        cases = {
            "outside HOME": (str(self.home.parent / "elsewhere"), "XDG_STATE_HOME must be inside a hidden folder"),
            "not hidden": (str(self.home / "state"), "XDG_STATE_HOME must be inside a hidden folder"),
            "through a link": (str(self.home / ".linked" / "state"), "XDG_STATE_HOME must be inside a hidden folder"),
        }
        (self.home / ".real").mkdir()
        (self.home / ".linked").symlink_to(self.home / ".real")
        for label, (value, message) in cases.items():
            with self.subTest(label), mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": value}):
                with self.assertRaises(attest.AttestError) as caught:
                    attest.default_state_root()
                self.assertIn(message, str(caught.exception))
                self.assertNotIn(str(self.home), str(caught.exception))
        # ~/.local a symbolic link: the folders above the state must be real and the owner's.
        (self.home / "real-local").mkdir()
        (self.home / ".local").symlink_to(self.home / "real-local")
        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "XDG_STATE_HOME": str(self.home / ".config" / "state")}):
            self.assertEqual(attest.default_state_root(), self.home / ".config" / "state" / "codex-agents")
        environ = {key: value for key, value in os.environ.items() if key != "XDG_STATE_HOME"}
        with mock.patch.dict(os.environ, {**environ, "HOME": str(self.home)}, clear=True), \
                mock.patch.object(attest.host_paths, "account_home", lambda: self.home):
            with self.assertRaisesRegex(attest.AttestError, "a folder above the learning state"):
                attest.default_state_root()

    @unittest.skipUnless(os.path.exists("/usr/bin/ssh-keygen"), "needs ssh-keygen")
    def test_the_legacy_report_shows_the_signer_key_and_whether_it_is_yours(self) -> None:
        legacy = self.home / "Library" / "Application Support" / "codex-agents"
        environment = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local" / "state")}
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(attest.host_paths, "legacy_state_root", lambda: legacy), \
                mock.patch.object(attest, "ACCOUNT_HOME", str(self.home)):
            self.assertEqual(attest.legacy_state_report(), {"present": False})
            legacy.mkdir(parents=True, mode=0o700)
            keys = {}
            for name in ("owner", "other"):
                subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                                str(self.home / name)], check=True, capture_output=True)
                keys[name] = (self.home / f"{name}.pub").read_text().split()
            signers = f"codex-agents-owner {keys['owner'][0]} {keys['owner'][1]}\n".encode()
            (legacy / "owner-allowed-signers").write_bytes(signers)
            report = attest.legacy_state_report()
            self.assertEqual(report["signers_sha256"], hashlib.sha256(signers).hexdigest())
            self.assertRegex(report["signer_key"], r"^ssh-ed25519 SHA256:[A-Za-z0-9+/]+$")
            self.assertRegex(report["created"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            self.assertIsNone(report["current_key"])
            self.assertIsNone(report["match"])
            (self.home / ".ssh").mkdir(mode=0o700)
            pub = self.home / ".ssh" / "codex-agents-owner.pub"
            pub.write_text(" ".join(keys["owner"]) + "\n")
            self.assertTrue(attest.legacy_state_report()["match"])
            pub.write_text(" ".join(keys["other"]) + "\n")
            self.assertFalse(attest.legacy_state_report()["match"])
            # A planted type field is never echoed; no signer file approves "none".
            (legacy / "owner-allowed-signers").write_bytes(b"codex-agents-owner \x1b[31mevil AAAA\n")
            self.assertIsNone(attest.legacy_state_report()["signer_key"])
            (legacy / "owner-allowed-signers").unlink()
            self.assertEqual(attest.legacy_state_report()["signers_sha256"], "none")
            # Once the new folder exists there is nothing to move.
            (self.home / ".local" / "state" / "codex-agents").mkdir(parents=True)
            self.assertEqual(attest.legacy_state_report(), {"present": False})

    def test_the_scratch_container_is_the_one_the_guard_protects(self) -> None:
        self.assertEqual(attest.SCRATCH_CONTAINER, attest.host_paths.OWNER_SCRATCH_CONTAINER)


class ClientStatusTests(unittest.TestCase):
    """setup.sh's sign-in checks find and run a client only as the runtime would."""

    def setUp(self) -> None:
        import pwd

        # A hidden top-level folder of the account's home is a trusted installation folder.
        home = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
        parent = home / ".cache" / "agent-orch-tests"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.trusted = Path(tempfile.mkdtemp(prefix="clients-", dir=parent))
        self.addCleanup(shutil.rmtree, self.trusted, True)
        self.temp = Path(tempfile.mkdtemp(prefix="client-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.record = self.temp / "record"
        self.marker = self.temp / "planted-ran"
        # A Codex session opened on ~/Work planted these; the owner's PATH lists them first.
        planted = self.temp / "Work" / "tools" / "bin"
        planted.mkdir(parents=True)
        for name in ("codex", "claude"):
            self.write(planted / name, f'#!/bin/sh\necho {name} >> "{self.marker}"\nexit 0\n')
        base = private_base(self)
        self.scratch = base / attest.SCRATCH_CONTAINER
        self.search = [str(self.trusted)]
        providers = attest.clone_module("providers")
        for patcher in (mock.patch.object(attest, "SCRATCH_BASE", str(base)),
                        mock.patch.object(providers, "TRUSTED_SEARCH_DIRECTORIES", self.search),
                        mock.patch.dict(os.environ, {"PATH": f"{planted}:{os.environ['PATH']}",
                                                     "CODEX_HOME": "/planted/codex-home",
                                                     "OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "sk-test"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.home = home

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    def client(self, name: str, folder: Path | None = None, rc: int = 0, extra: str = "") -> Path:
        path = (folder or self.trusted) / name
        self.write(path, f'#!/bin/sh\n{{ echo "ARGS $*"; echo "CWD $(pwd)"; /usr/bin/env; }} > "{self.record}"\n'
                         f"{extra}echo '{{\"loggedIn\": true, \"subscriptionType\": \"max\"}}'\nexit {rc}\n")
        return path

    def recorded(self) -> tuple[str, str, dict[str, str]]:
        lines = self.record.read_text().splitlines()
        env = dict(line.split("=", 1) for line in lines[2:] if "=" in line)
        return lines[0][5:], lines[1][4:], env

    def test_the_trusted_client_runs_with_the_runtimes_environment_never_the_path_one(self) -> None:
        self.client("codex")
        self.assertEqual(attest.client_status("codex"), (0, self.recorded_output()))
        args, cwd, env = self.recorded()
        self.assertEqual(args, "login status")
        self.assertFalse(self.marker.exists())
        for name in ("CODEX_HOME", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], str(self.trusted))
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(env["xcrun_nocache"], "1")
        # A private working folder and TMPDIR, inside the owner's scratch, removed afterwards.
        self.assertEqual(os.path.realpath(env["TMPDIR"]), os.path.realpath(cwd))
        self.assertTrue(Path(os.path.realpath(cwd)).is_relative_to(os.path.realpath(self.scratch)))
        self.assertFalse(Path(cwd).exists())
        self.client("claude")
        code, output = attest.client_status("claude")
        self.assertEqual((code, json.loads(output)["loggedIn"]), (0, True))
        self.assertEqual(self.recorded()[0], "auth status --json")
        self.assertFalse(self.marker.exists())

    def recorded_output(self) -> bytes:
        return b'{"loggedIn": true, "subscriptionType": "max"}\n'

    def test_missing_untrusted_and_signed_out_clients(self) -> None:
        self.assertEqual(attest.client_status("codex"), (attest.CLIENT_MISSING, b""))
        # In a temporary folder: refused before it ever runs.
        untrusted = self.temp / "untrusted-bin"
        untrusted.mkdir()
        self.client("codex", untrusted)
        self.search[:] = [str(untrusted)]
        self.assertEqual(attest.client_status("codex"), (attest.CLIENT_UNTRUSTED, b""))
        self.assertFalse(self.record.exists())
        # Inside the project: refused too.
        self.search[:] = [str(self.trusted)]
        self.client("codex")
        self.assertEqual(attest.client_status("codex", self.trusted), (attest.CLIENT_UNTRUSTED, b""))
        self.assertFalse(self.record.exists())
        # A symlink on the search path into a writable folder is refused, never run.
        (self.trusted / "codex").unlink()
        (self.trusted / "codex").symlink_to(untrusted / "codex")
        self.assertEqual(attest.client_status("codex"), (attest.CLIENT_UNTRUSTED, b""))
        self.assertFalse(self.record.exists())
        (self.trusted / "codex").unlink()
        self.client("codex", rc=1)
        self.assertEqual(attest.client_status("codex")[0], attest.CLIENT_SIGNED_OUT)
        self.assertFalse(self.marker.exists())

    def test_a_client_that_hangs_is_stopped(self) -> None:
        self.client("codex", extra="exec /bin/sleep 30\n")
        with mock.patch.object(attest, "CLIENT_TIMEOUT_SECONDS", 1):
            self.assertEqual(attest.client_status("codex"), (attest.CLIENT_FAILED, b""))

    def test_a_client_that_floods_its_output_is_stopped_at_the_cap(self) -> None:
        # Writes without end and leaves a child in its process group; reading stops at the cap,
        # long before the deadline, and the whole group is killed.
        pidfile = self.temp / "child.pid"
        self.client("codex", extra=f'/bin/sleep 60 & echo $! > "{pidfile}"\n'
                                   "exec /usr/bin/yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        started = time.monotonic()
        with mock.patch.object(attest, "CLIENT_TIMEOUT_SECONDS", 20):
            self.assertEqual(attest.client_status("codex"), (attest.CLIENT_FAILED, b""))
        self.assertLess(time.monotonic() - started, 10)
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the client's process group was not stopped")

    def test_owner_tool_prints_only_claudes_json(self) -> None:
        for name, printed in (("codex", b""), ("claude", self.recorded_output())):
            self.client(name)
            stream = io.TextIOWrapper(io.BytesIO())
            with self.subTest(client=name), contextlib.redirect_stdout(stream):
                self.assertEqual(attest.owner_tool(["client-status", name]), 0)
            stream.flush()
            self.assertEqual(stream.buffer.getvalue(), printed)
        for argv in (["client-status", "bash"], ["client-status"], ["client-status", "codex", "a", "b"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(attest.owner_tool(argv), 2)


class OwnerScratchTests(unittest.TestCase):
    """Working files live in a host-private folder no sandbox can write, never in temp."""

    def setUp(self) -> None:
        attest._TEMP_DIRS.clear()
        self.base = private_base(self)
        # The test base may sit under the real home folder (a CI checkout); the home rule is
        # tested on its own below.
        patcher = mock.patch.object(attest.Path, "home", return_value=Path("/nonexistent-home-for-tests"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_base_under_the_home_folder_is_refused(self) -> None:
        with mock.patch.object(attest.Path, "home", return_value=self.base.parent), \
                mock.patch.object(attest, "SCRATCH_BASE", str(self.base)):
            with self.assertRaisesRegex(attest.AttestError, "no private working folder"):
                attest.make_scratch("test-")

    def scratch(self, base: str | None) -> Path:
        with mock.patch.object(attest, "SCRATCH_BASE", base):
            path = attest.make_scratch("test-")
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_scratch_is_owner_only_in_its_own_container(self) -> None:
        path = self.scratch(str(self.base))
        self.assertEqual(path.parent, Path(os.path.realpath(self.base)) / attest.SCRATCH_CONTAINER)
        for folder in (path, path.parent):
            info = folder.lstat()
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(oct(info.st_mode & 0o777), "0o700")
        self.assertFalse(any(attest.within(path, root) for root in attest.temp_roots()))
        self.assertTrue(attest.remove_scratch(path))
        self.assertFalse(path.exists())

    def test_a_temporary_base_is_refused(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temporary, True)
        for base in (str(temporary), "/tmp", os.path.realpath(tempfile.gettempdir())):
            with self.subTest(base=base), mock.patch.object(attest, "SCRATCH_BASE", base):
                with self.assertRaisesRegex(attest.AttestError, "no private working folder"):
                    attest.make_scratch("test-")
        self.assertEqual(list(temporary.iterdir()), [])

    def test_a_base_containing_the_project_is_refused(self) -> None:
        project = self.base / "project"
        project.mkdir()
        with mock.patch.object(attest, "SCRATCH_BASE", str(self.base)):
            with self.assertRaisesRegex(attest.AttestError, "no private working folder"):
                attest.make_scratch("test-", project)

    def test_a_symlinked_base_is_refused(self) -> None:
        real = self.base / "real"
        real.mkdir(mode=0o700)
        link = self.base / "link"
        link.symlink_to(real)
        with mock.patch.object(attest, "SCRATCH_BASE", str(link)):
            with self.assertRaisesRegex(attest.AttestError, "symbolic link"):
                attest.make_scratch("test-")
        self.assertEqual(list(real.iterdir()), [])

    def test_a_symlinked_or_shared_container_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        container = self.base / attest.SCRATCH_CONTAINER
        container.symlink_to(elsewhere)
        with mock.patch.object(attest, "SCRATCH_BASE", str(self.base)):
            with self.assertRaises((attest.AttestError, OSError)):
                attest.make_scratch("test-")
            self.assertEqual(list(elsewhere.iterdir()), [])
            container.unlink()
            container.mkdir(mode=0o755)
            container.chmod(0o755)
            with self.assertRaisesRegex(attest.AttestError, "not yours alone"):
                attest.make_scratch("test-")

    def test_a_base_writable_by_others_or_owned_by_someone_else_is_refused(self) -> None:
        shared = self.base / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with mock.patch.object(attest, "SCRATCH_BASE", str(shared)):
            with self.assertRaisesRegex(attest.AttestError, "changed by other users"):
                attest.make_scratch("test-")
        with mock.patch.object(attest, "SCRATCH_BASE", str(self.base)), \
                mock.patch.object(attest.os, "geteuid", return_value=os.geteuid() + 4242):
            with self.assertRaises(attest.AttestError):
                attest.make_scratch("test-")

    def test_linux_needs_xdg_runtime_dir_outside_temp(self) -> None:
        environment = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        with mock.patch.object(attest.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(attest.AttestError, "XDG_RUNTIME_DIR"):
                attest.make_scratch("test-")
            os.environ["XDG_RUNTIME_DIR"] = os.path.realpath(tempfile.gettempdir())
            with self.assertRaisesRegex(attest.AttestError, "no private working folder"):
                attest.make_scratch("test-")
            os.environ["XDG_RUNTIME_DIR"] = str(self.base)
            path = attest.make_scratch("test-")
            self.addCleanup(shutil.rmtree, path, True)
            self.assertEqual(path.parent.parent, Path(os.path.realpath(self.base)))

    @unittest.skipUnless(sys.platform == "darwin", "macOS keeps it in the per-user cache folder")
    def test_macos_uses_the_per_user_cache_folder(self) -> None:
        cache = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                               text=True, check=True).stdout.strip()
        path = self.scratch(None)
        self.assertEqual(path.parent, Path(os.path.realpath(cache)) / attest.SCRATCH_CONTAINER)

    def test_owner_tool_prints_a_new_folder_and_refuses_unknown_tools(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(attest, "SCRATCH_BASE", str(self.base)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(attest.owner_tool(["scratch", "setup-"]), 0)
            self.assertEqual(attest.owner_tool(["scratch", "../x"]), 1)
            self.assertEqual(attest.owner_tool(["shell"]), 2)
        created = Path(out.getvalue().strip())
        self.addCleanup(shutil.rmtree, created, True)
        self.assertTrue(created.name.startswith("setup-") and created.is_dir())


class ProjectRuntimeTests(unittest.TestCase):
    """Steps that depend on the install need it to be this release, byte for byte, and nothing else."""

    FILES = {
        ".codex/orchestration/cli.py": "print('cli')\n",
        ".codex/orchestration/codex_agents/__init__.py": "",
        ".codex/orchestration/codex_agents/host_paths.py": "X = 1\n",
        ".codex/orchestration/config/policy.json": "{}\n",
    }

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="attest-runtime-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bundle, self.project = self.root / "bundle", self.root / "project"
        for base in (self.bundle, self.project):
            for relative, text in self.FILES.items():
                (base / relative).parent.mkdir(parents=True, exist_ok=True)
                (base / relative).write_text(text)
        (self.bundle / "VERSION").write_text("1.2.3\n")
        self.manifest({"format": 1, "version": "1.2.3", "files": {}})
        patcher = mock.patch.object(attest, "NATIVE_PACKAGE", self.bundle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def manifest(self, data) -> None:
        (self.project / ".codex/orchestration/install-manifest.json").write_text(json.dumps(data))

    def refused(self, text: str) -> None:
        with self.assertRaises(attest.AttestError) as raised:
            _REAL_RUNTIME_CHECK(self.project)
        self.assertIn("not an exact copy", str(raised.exception))
        self.assertIn(text, str(raised.exception))
        self.assertIn("so setup stops here", str(raised.exception))

    def test_an_exact_copy_passes_with_the_projects_own_files(self) -> None:
        orchestration = self.project / ".codex/orchestration"
        for name in attest.PROJECT_FILES:
            if not (orchestration / name).exists():
                (orchestration / name).write_text("{}")
        (orchestration / ".DS_Store").write_bytes(b"finder")
        _REAL_RUNTIME_CHECK(self.project)

    def test_the_project_file_list_matches_the_runtime(self) -> None:
        sys.path.insert(0, str(_ORCHESTRATION))
        try:
            from codex_agents.runtime_files import PACKAGE_DATA_FILES, PROJECT_FILES, RUNTIME_FILES
        finally:
            sys.path.remove(str(_ORCHESTRATION))
        self.assertEqual(set(attest.PROJECT_FILES), set(PROJECT_FILES))
        with mock.patch.object(attest, "NATIVE_PACKAGE", _ORCHESTRATION.parents[1]):
            bundle = {name.removeprefix(".codex/orchestration/") for name in attest._runtime_bundle()}
        self.assertEqual(bundle, {*RUNTIME_FILES, *PACKAGE_DATA_FILES})

    def test_a_modified_file_is_refused(self) -> None:
        (self.project / ".codex/orchestration/codex_agents/host_paths.py").write_text("X = 2\n")
        self.refused(".codex/orchestration/codex_agents/host_paths.py differs")

    def test_a_missing_file_is_refused(self) -> None:
        (self.project / ".codex/orchestration/config/policy.json").unlink()
        self.refused("config/policy.json differs")

    def test_a_symlinked_file_with_identical_bytes_is_refused(self) -> None:
        cli = self.project / ".codex/orchestration/cli.py"
        copy = self.root / "copy.py"
        copy.write_text(cli.read_text())
        cli.unlink()
        cli.symlink_to(copy)
        self.refused("cli.py differs")

    def test_another_version_or_no_install_record_is_refused(self) -> None:
        self.manifest({"format": 1, "version": "1.2.2\x1b[2J", "files": {}})
        self.refused("version 1.2.22J is installed and this setup is version 1.2.3")
        (self.project / ".codex/orchestration/install-manifest.json").unlink()
        self.refused("install record is missing")

    def test_any_unexpected_entry_is_refused(self) -> None:
        package = self.root / "attacker-package"
        package.mkdir()
        (package / "__init__.py").write_text("raise SystemExit('planted')\n")
        orchestration = self.project / ".codex/orchestration"
        cases = {
            "an extensionless symlink to a package": ("hashlib", lambda path: path.symlink_to(package)),
            "a symlink to a module": ("json.py", lambda path: path.symlink_to(package / "__init__.py")),
            "a dangling symlink": ("socket", lambda path: path.symlink_to(self.root / "nowhere")),
            "a package": ("sqlite3", lambda path: (path.mkdir(), (path / "__init__.py").write_text(""))),
            "an empty folder": ("empty", lambda path: path.mkdir()),
            "a bytecode cache": ("__pycache__", lambda path: (path.mkdir(),
                                 (path / "cli.cpython-312.pyc").write_bytes(b"x"))),
            "an extension module": ("codex_agents/routing.cpython-312-darwin.so",
                                    lambda path: path.write_bytes(b"")),
            "an extra file": ("notes", lambda path: path.write_text("")),
            "a symlinked config folder": ("schemas", lambda path: path.symlink_to(self.root)),
        }
        for label, (relative, make) in cases.items():
            with self.subTest(case=label):
                path = orchestration / relative
                make(path)
                self.refused("holds a file, folder or link the package does not install")
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        _REAL_RUNTIME_CHECK(self.project)


class IsolationTests(unittest.TestCase):
    def test_attest_imports_only_the_standard_library(self) -> None:
        import ast

        tree = ast.parse(_SCRIPT.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)  # nothing relative to its own folder
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names - set(sys.stdlib_module_names) - {"__future__"}, set())

    def test_a_virtual_environment_or_temporary_python_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="attest-python-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        temporary = root / "python3"
        temporary.write_text("")
        with mock.patch.object(attest.sys, "executable", str(temporary)):
            with self.assertRaisesRegex(attest.AttestError, "temporary folder"):
                attest.trusted_python()
        outside = private_base(self)
        venv = outside / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python3").write_text("")
        (venv / "pyvenv.cfg").write_text("home = /elsewhere\n")
        with mock.patch.object(attest.sys, "executable", str(venv / "bin" / "python3")):
            with self.assertRaisesRegex(attest.AttestError, "virtual environment"):
                attest.trusted_python()
        # A hidden folder of the account's home is a trusted installation folder; a project
        # inside it still is not.
        with mock.patch.object(attest.host_paths, "account_home", return_value=outside):
            project = outside / ".tools" / "project"
            (project / "bin").mkdir(parents=True)
            (project / "bin" / "python3").write_text("")
            with mock.patch.object(attest.sys, "executable", str(project / "bin" / "python3")):
                self.assertEqual(attest.trusted_python(), str(project / "bin" / "python3"))
                with self.assertRaisesRegex(attest.AttestError, "inside the project"):
                    attest.trusted_python(project)

    def test_only_the_runtimes_trusted_installation_folders_are_accepted(self) -> None:
        home = private_base(self) / "home"
        with mock.patch.object(attest.host_paths, "account_home", return_value=home):
            for relative, trusted in ((".pyenv/versions/3.12.4/bin/python3.12", True),
                                      (".local/share/uv/python/cpython-3.12/bin/python3.12", True),
                                      ("miniforge3/bin/python3", False),
                                      ("Code/tools/bin/python3", False),
                                      ("Work/tools/python3", False)):
                with self.subTest(path=relative):
                    executable = home / relative
                    executable.parent.mkdir(parents=True, exist_ok=True)
                    executable.write_text("")
                    problem = attest.python_location_problem(executable)
                    if trusted:
                        self.assertIsNone(problem)
                    else:
                        self.assertIn("not in a trusted installation folder", problem)
            # A sibling install outside the home folder, as /srv/work/tools next to /srv/work/app.
            sibling = home.parent / "srv-work" / "tools" / "python3"
            sibling.parent.mkdir(parents=True)
            sibling.write_text("")
            self.assertIn("not in a trusted installation folder", attest.python_location_problem(sibling))
        # The list is the runtime's own.
        self.assertIs(attest.host_paths.TRUSTED_INSTALL_ROOTS, attest.host_paths.TRUSTED_INSTALL_ROOTS)
        self.assertIn("/opt/homebrew", attest.host_paths.TRUSTED_INSTALL_ROOTS)
        self.assertEqual(attest.host_paths.__file__, str(_ORCHESTRATION / "codex_agents" / "host_paths.py"))

    @unittest.skipUnless(sys.platform == "darwin", "the xcrun shim is macOS's")
    def test_the_macos_xcrun_shim_is_refused(self) -> None:
        self.assertIn("shim", attest.python_location_problem(Path("/usr/bin/python3")))

    def test_an_owner_chosen_input_is_snapshot_safely(self) -> None:
        base = private_base(self)
        source = base / "checks.json"
        source.write_text('{"version": 1}')
        copy = base / "copy.json"
        attest.copy_input(source, copy)
        self.assertEqual(copy.read_text(), '{"version": 1}')
        self.assertEqual(oct(copy.stat().st_mode & 0o777), "0o600")
        with self.assertRaises(attest.AttestError):  # never overwrites
            attest.copy_input(source, copy)
        fifo = base / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(attest.AttestError, "not a regular file"):
            attest.copy_input(fifo, base / "from-fifo.json")  # returns at once, never blocks
        big = base / "big.json"
        big.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(attest.AttestError, "too large"):
            attest.copy_input(big, base / "from-big.json")

    def test_a_symlinked_interpreter_is_launched_as_its_real_file(self) -> None:
        trust_this_interpreter(self)
        outside = private_base(self)
        link = outside / "python3"
        link.symlink_to(os.path.realpath(sys.executable))
        with mock.patch.object(attest.sys, "executable", str(link)):
            command = attest.cli_command(outside, ["validate"])
        self.assertEqual(command[0], os.path.realpath(sys.executable))
        self.assertEqual(command[1:6], list(attest.PYTHON_FLAGS))


class RealTerminalTests(unittest.TestCase):
    """A mocked input() cannot show what the terminal echoes, so use a real pseudo-terminal."""

    def run_in_pty(self, secret: str, *, break_echo: bool = False) -> str:
        import pty
        import select
        import time

        root = Path(tempfile.mkdtemp(prefix="attest-pty-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        home, state, project = root / "home", root / "state", root / "project"
        for directory in (home, state):
            directory.mkdir(mode=0o755)
        bundle = stub_install(root, project, "")
        # A throwaway key pair for this test only; it is never used to sign anything.
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "throwaway")],
                       check=True, capture_output=True)
        fields = (root / "throwaway.pub").read_text().split()
        (state / "owner-allowed-signers").write_text(f"codex-agents-owner {fields[0]} {fields[1]}\n")
        real_home = os.path.realpath(home)
        env = dict(os.environ, HOME=real_home, XDG_STATE_HOME=os.path.join(real_home, ".local", "state"))
        env.pop("SSH_AUTH_SOCK", None)
        args = ["--target", str(project), "--state-root", str(state)]
        command = [sys.executable, "-I", "-B", "-S", "-c", RUNNER, str(_SCRIPT), str(bundle),
                   str(private_base(self)), ("break-echo" if break_echo else "normal") + TRUST, *args]
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
        # Every CLI call reports a logged-out provider, so a success ends with "not logged in".
        self.bundle = stub_install(self.root, self.project,
                                   "import json\nprint(json.dumps({'ready': True, 'records': []}))\n")
        self.scratch = private_base(self)
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

        real_home = os.path.realpath(self.home)
        env = dict(os.environ, HOME=real_home, XDG_STATE_HOME=os.path.join(real_home, ".local", "state"))
        env.pop("SSH_AUTH_SOCK", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-I", "-B", "-S", "-c", RUNNER, str(_SCRIPT),
                                       str(self.bundle), str(self.scratch), "normal" + TRUST,
                                       "--target", str(self.project), "--state-root", str(self.state)], env)
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

    SIGNING_CLI = (
        "import json, subprocess, sys, tempfile\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, {orchestration!r})\n"
        "from codex_agents.learning_engine import OwnerGrantAuthority\n"
        "from codex_agents.providers import billing_attestation_record\n"
        "args = sys.argv[1:]\n"
        "option = lambda flag: args[args.index(flag) + 1]\n"
        "if args[0] == 'provider-observe':\n"
        "    print(json.dumps({{'ready': True, 'records': [{{'record_type': 'provider_identity',\n"
        "        'logged_in': True, 'account_fingerprint': 'ab' * 32, 'auth_method': 'chatgpt',\n"
        "        'plan_type': 'pro'}}]}}))\n"
        "    raise SystemExit(0)\n"
        "assert option('--grant') == '-'\n"
        "record = billing_attestation_record(json.loads(sys.stdin.read()))\n"
        "payload = OwnerGrantAuthority.owner_record_payload('prj_' + '0' * 32, record)\n"
        "if '--signature' not in args:\n"
        "    import base64\n"
        "    print(json.dumps({{'ready': True, 'record': record,\n"
        "        'signing_payload_b64': base64.b64encode(payload).decode()}}))\n"
        "    raise SystemExit(0)\n"
        "source = option('--signature')\n"
        "assert source.startswith('/dev/fd/'), source\n"
        "with tempfile.TemporaryDirectory() as directory:\n"
        "    signature = Path(directory) / 'signature'\n"
        "    signature.write_bytes(Path(source).read_bytes())\n"
        "    checked = subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'verify', '-f',\n"
        "        str(Path(option('--state-root')) / 'owner-allowed-signers'), '-I', 'codex-agents-owner',\n"
        "        '-n', 'codex-agents-owner-v1', '-s', str(signature)], input=payload, capture_output=True)\n"
        "print(json.dumps({{'ready': checked.returncode == 0, 'error': 'bad signature'}}))\n"
    )

    def test_the_real_signer_signs_the_approved_bytes_from_memory(self) -> None:
        (self.project / ".codex/orchestration/cli.py").write_text(
            self.SIGNING_CLI.format(orchestration=str(_ORCHESTRATION)))
        (self.bundle / ".codex/orchestration/cli.py").write_text(
            self.SIGNING_CLI.format(orchestration=str(_ORCHESTRATION)))
        transcript, code = self.drive([
            ("Enter passphrase", self.RIGHT), ("credits turned off", "y"),
            ("existing Codex credit", "y"), ("Sign this statement?", "y"),
            ("Enter passphrase", self.RIGHT)])
        self.assertIn("Codex attested.", transcript)
        self.assertEqual(code, 0)
        self.no_passphrase_in(transcript)
        self.assertEqual([path.name for path in self.root.rglob("*.sig")], [])

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
