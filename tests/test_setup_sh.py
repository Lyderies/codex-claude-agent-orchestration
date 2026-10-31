from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
POLICY = ORCHESTRATION / "config" / "routing-policy.json"

# Constructs that bash 3.2 (macOS) does not support.
FORBIDDEN = {
    "associative array": r"(declare|local|typeset)\s+-[a-zA-Z]*A\b",
    "case-conversion expansion": r"\$\{[A-Za-z_][A-Za-z_0-9]*(,,|\^\^|,|\^)\}",
    "mapfile": r"\bmapfile\b",
    "readarray": r"\breadarray\b",
    "read -i": r"\bread\b[^\n]*\s-[a-zA-Z]*i\s",
    "fractional read timeout": r"\bread\b[^\n]*-t\s+0?\.\d",
    "&>> redirect": r"&>>",
    "negative substring offset": r"\$\{[A-Za-z_]+:\s+-\d",
    "coproc": r"\bcoproc\b",
    "|& pipe": r"\|&",
}

VALIDATE = (
    "import json, sys, tempfile\n"
    "from pathlib import Path\n"
    "from codex_agents.routing import load_policy, validate_provider_routing\n"
    "raw = sys.stdin.buffer.read()\n"
    "policy = load_policy(Path(sys.argv[1]))\n"
    "with tempfile.TemporaryDirectory() as directory:\n"
    "    config = validate_provider_routing(Path(directory), raw, policy)\n"
    "print(json.dumps(config, sort_keys=True))\n"
)


def routing(*choices: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP), "--print-provider-routing", *choices],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


class SetupScriptTests(unittest.TestCase):
    def test_syntax(self) -> None:
        for shell in ("bash", "/bin/bash"):
            if shutil.which(shell) or Path(shell).exists():
                result = subprocess.run([shell, "-n", str(SETUP)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_bash_4_constructs(self) -> None:
        lines = SETUP.read_text(encoding="utf-8").splitlines()
        for name, pattern in FORBIDDEN.items():
            for number, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                with self.subTest(construct=name, line=number):
                    self.assertIsNone(re.search(pattern, line), f"{name}: {line.strip()}")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self) -> None:
        result = subprocess.run(
            ["shellcheck", "-S", "warning", "-e", "SC2034,SC2155,SC2162,SC2086,SC2206,SC2207,SC2178",
             str(SETUP)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_refuses_without_a_terminal(self) -> None:
        result = subprocess.run(["bash", str(SETUP)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must run in a terminal", result.stderr)
        self.assertIn("human step", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_provider_routing_matches_the_router_validator(self) -> None:
        models = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5"
        cases = [
            ("on", "on", models, "off"),
            ("on", "on", "claude-haiku-5-5", "on"),
            ("on", "off", "-", "off"),
            ("off", "on", "claude-sonnet-5-5,claude-opus-5-5", "off"),
        ]
        env = dict(os.environ, PYTHONPATH=str(ORCHESTRATION))
        for case in cases:
            with self.subTest(case=case):
                made = routing(*case)
                self.assertEqual(made.returncode, 0, made.stderr)
                checked = subprocess.run(
                    [sys.executable, "-c", VALIDATE, str(POLICY)],
                    input=made.stdout, capture_output=True, text=True, env=env,
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)
                import json
                config = json.loads(made.stdout)
                self.assertEqual(config["providers"]["codex"]["enabled"], case[0] == "on")
                self.assertEqual(config["providers"]["claude"]["enabled"], case[1] == "on")
                self.assertEqual(config["codex_primary_may_bridge_to_claude"], case[3] == "on")
                if case[1] == "on":
                    self.assertEqual(config["providers"]["claude"]["models"], case[2].split(","))
                else:
                    self.assertNotIn("models", config["providers"]["claude"])

    def test_invalid_provider_choices_are_rejected(self) -> None:
        for case in (
            ("off", "off", "-", "off"),
            ("on", "on", "-", "off"),
            ("on", "off", "-", "on"),
            ("on", "on", "bad model", "off"),
            ("maybe", "on", "claude-haiku-5-5", "off"),
        ):
            with self.subTest(case=case):
                result = routing(*case)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_hook_requires_four_choices(self) -> None:
        result = routing("on", "on")
        self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(sys.platform != "win32", "needs a pseudo-terminal")
    def test_quit_on_the_first_screen_changes_nothing(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import termios

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fakes = base / "bin"
            fakes.mkdir()
            home = base / "home"
            home.mkdir()
            (fakes / "codex").write_text("#!/bin/sh\nexit 0\n")
            (fakes / "claude").write_text(
                '#!/bin/sh\necho \'{"loggedIn": true, "subscriptionType": "max", "authMethod": "x"}\'\n')
            for fake in fakes.iterdir():
                fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
            env.update(PATH=f"{fakes}:{os.environ['PATH']}", HOME=str(home), TERM="xterm", TMPDIR=str(base))
            env.pop("NO_COLOR", None)
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(base)
                os.execvpe("bash", ["bash", str(SETUP)], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

            def read_some(seconds: float) -> str:
                out, end = b"", time.time() + seconds
                while time.time() < end:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                    if ready:
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        out += chunk
                return out.decode(errors="replace")

            screen = read_some(3)
            self.assertIn("Step 1 of 8", screen)
            self.assertIn("signed in (plan: max, login: x)", screen)
            os.write(fd, b"q")
            read_some(0.5)
            os.write(fd, b"y")
            text = screen + read_some(1.5)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.WEXITSTATUS(status), 130, text)
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual([p.name for p in base.iterdir() if p.name.startswith("agent-orch")], [])


if __name__ == "__main__":
    unittest.main()
