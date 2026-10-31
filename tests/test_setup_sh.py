from __future__ import annotations

import os
import re
import shutil
import signal
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


TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789SECRETTOKEN"


def write_routing(project: Path, codex: str = "on", claude: str = "on",
                  models: str = "claude-opus-5-5", bridge: str = "off") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP), "--write-provider-routing", str(project), codex, claude, models, bridge],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
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
            ["shellcheck", "-S", "warning", "-e", "SC2034,SC2155,SC2162,SC2206,SC2207,SC2178",
             str(SETUP)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_leading_home_markers_are_expanded(self) -> None:
        home = "/Users/<you>"
        cases = {
            "~": home,
            "~/Code/app": f"{home}/Code/app",
            "~/my app": f"{home}/my app",
            "$HOME": home,
            "$HOME/Code/app": f"{home}/Code/app",
            "${HOME}/Code/app": f"{home}/Code/app",
            "  ~/Code/app  ": f"{home}/Code/app",
            "'~/Code/app'": f"{home}/Code/app",
            '"$HOME/Code/app"': f"{home}/Code/app",
            # Left alone: absolute and relative paths, other users, a ~ that is not leading.
            "/Code/app": "/Code/app",
            "Code/app": "Code/app",
            "~other/Code": "~other/Code",
            "/a/~/b": "/a/~/b",
            "$HOMEWORK/x": "$HOMEWORK/x",
        }
        for shell in ("bash", "/bin/bash"):
            if not (shutil.which(shell) or Path(shell).exists()):
                continue
            for value, expected in cases.items():
                with self.subTest(shell=shell, value=value):
                    result = subprocess.run(
                        [shell, str(SETUP), "--print-expanded-path", value],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        env=dict(os.environ, HOME=home))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected + "\n")

    def codex_hint(self, wanted: str, ready: str, profile: str | None) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            if profile is not None:
                (Path(directory) / "orchestrator.config.toml").write_text(profile)
            result = subprocess.run(
                ["bash", str(SETUP), "--print-codex-hint", wanted, ready],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                env=dict(os.environ, CODEX_HOME=directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout)
        return result.stdout.splitlines()

    def test_codex_hint_follows_the_profile_network_setting(self) -> None:
        secret = f"# {TOKEN}\nmodel = \"x\"\n"
        enabled = (
            secret + "sandbox_mode = \"workspace-write\"\n\n"
            "[other]\nnetwork_access = false\n\n"
            "[sandbox_workspace_write]  # allow network\nnetwork_access   =   true # yes\n")
        # A differing but network-enabled profile (the owner's case) is recommended.
        for wanted, ready in (("1", "0"), ("0", "0"), ("1", "1")):
            with self.subTest(wanted=wanted, ready=ready):
                self.assertEqual(self.codex_hint(wanted, ready, enabled)[0], "codex -p orchestrator")
        for name, profile in {
            "network off": secret + "[sandbox_workspace_write]\nnetwork_access = false\n",
            "setting in another table": secret + "[other]\nnetwork_access = true\n",
            "setting before any table": "network_access = true\n",
            "last assignment wins": "[sandbox_workspace_write]\nnetwork_access = true\nnetwork_access = false\n",
            "commented out": "[sandbox_workspace_write]\n# network_access = true\n",
        }.items():
            with self.subTest(profile=name):
                lines = self.codex_hint("1", "0", profile)
                self.assertEqual(lines[0], "codex")
                self.assertIn("network_access = true", lines[1])
        missing = self.codex_hint("1", "0", None)
        self.assertEqual(missing[0], "codex")
        self.assertIn("missing", missing[1])
        not_wanted = self.codex_hint("0", "0", None)
        self.assertEqual(not_wanted[0], "codex")
        self.assertIn("not chosen", not_wanted[1])
        self.assertEqual(self.codex_hint("1", "1", None)[0], "codex -p orchestrator")

    def test_codex_hint_ignores_a_symlinked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real.toml"
            real.write_text("[sandbox_workspace_write]\nnetwork_access = true\n")
            home = base / "codex"
            home.mkdir()
            (home / "orchestrator.config.toml").symlink_to(real)
            result = subprocess.run(
                ["bash", str(SETUP), "--print-codex-hint", "1", "0"], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, env=dict(os.environ, CODEX_HOME=str(home)))
        self.assertEqual(result.stdout.splitlines()[0], "codex")

    def test_codex_hint_refuses_a_symlinked_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            real = base / "real"
            real.mkdir()
            (real / "orchestrator.config.toml").write_text(
                "[sandbox_workspace_write]\nnetwork_access = true\n")
            link = base / "link"
            link.symlink_to(real)
            for home, expected in ((real, "codex -p orchestrator"), (link, "codex")):
                with self.subTest(home=home.name):
                    result = subprocess.run(
                        ["bash", str(SETUP), "--print-codex-hint", "1", "0"], capture_output=True,
                        text=True, stdin=subprocess.DEVNULL, env=dict(os.environ, CODEX_HOME=str(home)))
                    self.assertEqual(result.stdout.splitlines()[0], expected)
            # A symlink in the middle of the path is refused as well.
            (base / "outer").mkdir()
            (base / "outer" / "inner").symlink_to(real)
            result = subprocess.run(
                ["bash", str(SETUP), "--print-codex-hint", "1", "0"], capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, CODEX_HOME=str(base / "outer" / "inner")))
            self.assertEqual(result.stdout.splitlines()[0], "codex")

    def test_codex_hint_does_not_hang_on_a_fifo_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            os.mkfifo(base / "orchestrator.config.toml")
            started = time.time()
            result = subprocess.run(
                ["bash", str(SETUP), "--print-codex-hint", "1", "0"], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, env=dict(os.environ, CODEX_HOME=str(base)), timeout=20)
            self.assertLess(time.time() - started, 15)
        self.assertEqual(result.stdout.splitlines()[0], "codex")

    def test_uncommitted_summary_keeps_rename_source_names_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
            (project / ".codex").mkdir()
            old = project / ".codex" / "old name .txt"
            old.write_text("same content for rename detection\n" * 5)
            git = ["git", "-C", str(project)]
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            subprocess.run(git + ["commit", "-qm", "i"], check=True, env=env)
            subprocess.run(git + ["mv", ".codex/old name .txt", ".codex/new name.txt"], check=True, env=env)
            (project / ".codex" / "extra").write_text("x")
            result = subprocess.run(["bash", str(SETUP), "--print-uncommitted", str(project)],
                                    capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn(".codex/new name.txt (from .codex/old name .txt)", lines)
        self.assertIn(".codex/extra", lines)
        self.assertEqual(len(lines), 2, lines)

    def helper(self, *args: str, stdin: bytes = b"", env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SETUP), "--helper", *args], input=stdin, capture_output=True,
                              env=dict(os.environ, **(env or {})))

    def planned_project(self, directory: str, obsolete: str) -> tuple[Path, Path]:
        import json
        project = Path(directory) / "project"
        (project / ".codex" / "orchestration").mkdir(parents=True)
        (project / ".codex" / "orchestration" / "install-manifest.json").write_text(
            json.dumps({"files": {obsolete: "0" * 64}}))
        preview = Path(directory) / "preview.json"
        preview.write_text(json.dumps({"codex": {"files": []}}))
        return project, preview

    def test_unsafe_file_names_are_refused_before_any_plan_text_exists(self) -> None:
        bundle = str(ORCHESTRATION.parents[1])
        names = {
            "newline injection": ".codex/old\nREMOVE private.txt",
            "carriage return": ".codex/old\rx",
            "tab": ".codex/old\tx",
            "escape sequence": ".codex/old\x1b[2Jx",
            "trailing space": ".codex/old ",
            "leading space": " .codex/old",
            "lone surrogate (invalid UTF-8)": ".codex/old\udcff",
            "parent traversal": ".codex/../../x",
            "absolute": "/etc/passwd",
        }
        for label, name in names.items():
            with self.subTest(name=label), tempfile.TemporaryDirectory() as directory:
                project, preview = self.planned_project(directory, name)
                plan = self.helper("plan", str(preview), str(project), bundle)
                self.assertEqual(plan.returncode, 0, plan.stderr)
                lines = plan.stdout.decode().splitlines()
                self.assertEqual(lines, ["UNSAFE"])
                paths = self.helper("planpaths", str(preview), str(project), bundle)
                self.assertEqual(paths.returncode, 3)
                self.assertEqual(paths.stdout, b"")

    def test_names_are_never_stripped_and_travel_nul_delimited(self) -> None:
        bundle = str(ORCHESTRATION.parents[1])
        with tempfile.TemporaryDirectory() as directory:
            project, preview = self.planned_project(directory, ".codex/my file (1).txt")
            result = self.helper("planpaths", str(preview), str(project), bundle)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b".codex/my file (1).txt\0")

    def git_project(self, directory: str, quote_path: bool = False) -> Path:
        project = Path(directory) / "project"
        project.mkdir()
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        subprocess.run(["git", "init", "-q", str(project)], check=True, env=env)
        subprocess.run(["git", "-C", str(project), "config", "core.quotePath", str(quote_path).lower()],
                       check=True, env=env)
        return project

    def test_commit_paths_are_literal_and_a_bad_list_fails_closed(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            codex = project / ".codex"
            codex.mkdir()
            (codex / "local-other").write_text("x")
            (codex / "ok").write_text("x")
            git = ["git", "-C", str(project)]
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            apply = Path(directory) / "apply.json"
            apply.write_text(json.dumps({"codex": {"project_configuration": {}}, "claude": {"changed_paths": []}}))
            plan = Path(directory) / "plan.nul"
            # `.codex/local*` is only a wildcard to Git, never a file here: it must not match local-other.
            plan.write_bytes(b".codex/local*\0.codex/ok\0")
            result = self.helper("paths", str(apply), str(project), str(plan))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b".codex/ok\0")
            # Invalid UTF-8 in the plan fails closed with no output (core.quotePath=false).
            try:  # macOS file systems refuse invalid UTF-8 names; Linux accepts them.
                (codex / os.fsdecode(b"bad\xff")).write_text("x")
            except OSError:
                pass
            plan.write_bytes(b".codex/ok\0.codex/bad\xff\0")
            result = self.helper("paths", str(apply), str(project), str(plan))
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b"")
            # A missing plan file fails closed too.
            result = self.helper("paths", str(apply), str(project), str(Path(directory) / "missing"))
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b"")
            # A tracked file with an invalid-UTF-8 name does not break a valid list.
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            plan.write_bytes(b".codex/ok\0")
            result = self.helper("paths", str(apply), str(project), str(plan))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b".codex/ok\0")

    def test_every_git_call_with_paths_is_literal(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        for needle in (" add -A -- ", " commit -m ", '"ls-files"'):
            lines = [line for line in text.splitlines()
                     if needle in line and not line.lstrip().startswith("#")]
            self.assertTrue(lines, needle)
            for line in lines:
                with self.subTest(line=line.strip()):
                    self.assertIn("--literal-pathspecs", line)

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

    def test_symlinked_routing_destination_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            (project / ".codex" / "orchestration").mkdir(parents=True)
            victim = base / "victim"
            victim.write_text("precious\n")
            link = project / ".codex" / "orchestration" / "provider-routing.json"
            link.symlink_to(victim)
            result = write_routing(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), "precious\n")
            self.assertTrue(link.is_symlink())
            # A symlinked parent directory is refused too.
            other = base / "other"
            (other / "orchestration").mkdir(parents=True)
            linked = base / "linked"
            (linked).mkdir()
            (linked / ".codex").symlink_to(other)
            result = write_routing(linked)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list((other / "orchestration").iterdir()), [])

    def test_routing_is_written_atomically_without_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / ".codex" / "orchestration"
            target.mkdir(parents=True)
            self.assertEqual(write_routing(project).returncode, 0)
            self.assertEqual(
                [p.name for p in target.iterdir()], ["provider-routing.json"])
            self.assertTrue((target / "provider-routing.json").read_text().startswith("{"))
            # Replacing a regular file works.
            self.assertEqual(write_routing(project, "off", "on", "claude-haiku-5-5").returncode, 0)
            self.assertIn("claude-haiku-5-5", (target / "provider-routing.json").read_text())


@unittest.skipUnless(sys.platform != "win32", "needs a pseudo-terminal")
class WizardTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        self.fakes = self.base / "bin"
        self.fakes.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.tmpdir = self.base / "tmp"
        self.tmpdir.mkdir()
        (self.fakes / "codex").write_text("#!/bin/sh\nexit 0\n")
        (self.fakes / "claude").write_text(
            '#!/bin/sh\necho \'{"loggedIn": true, "subscriptionType": "max", "authMethod": "x"}\'\n')
        self.py_log = self.base / "python.log"
        self.slow_pid = self.base / "slow.pid"
        (self.fakes / "python3").write_text(
            "#!/bin/sh\n"
            f'echo "$PYTHONDONTWRITEBYTECODE" >> "{self.py_log}"\n'
            'if [ -n "$FAKE_FAIL" ]; then case "$*" in *install.py*--apply*)'
            ' echo \'{"error": "boom ' + TOKEN + ' user@example.invalid"}\'; echo ' + TOKEN + ' >&2; exit 2;; esac; fi\n'
            f'if [ -n "$FAKE_SLOW" ]; then case "$*" in *install.py*install*)'
            f' echo $$ > "{self.slow_pid}"; exec sleep 60;; esac; fi\n'
            'for a in "$@"; do if [ "$a" = paths ] && [ -n "$FAKE_PJ_FAIL" ]; then exit 1; fi; done\n'
            f'exec "{sys.executable}" "$@"\n')
        for fake in self.fakes.iterdir():
            fake.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NO_COLOR", "PYTHONDONTWRITEBYTECODE")}
        self.env.update(
            PATH=f"{self.fakes}:{os.environ['PATH']}", HOME=str(self.home), TERM="xterm",
            TMPDIR=str(self.tmpdir), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.pid = 0
        self.fd = -1
        self.screen = ""
        self.transcript = ""

    def start(self, cwd: Path, extra_env: dict | None = None) -> None:
        import fcntl
        import pty
        import struct
        import termios

        env = dict(self.env, **(extra_env or {}))
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execvpe("bash", ["bash", str(SETUP)], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        if self.pid:
            self._kill_and_wait()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _kill_and_wait(self) -> None:
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass
        end = time.time() + 5
        while time.time() < end:
            try:
                done, _ = os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                return
            if done:
                return
            time.sleep(0.05)

    def expect(self, text: str, timeout: float = 25.0) -> None:
        import select

        end = time.time() + timeout
        while time.time() < end:
            if text in self.screen:
                return
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.screen += chunk.decode(errors="replace")
                self.transcript += chunk.decode(errors="replace")
        self.fail(f"never saw {text!r}; last screen: {re.sub(chr(27) + r'.*?[A-Za-z]', '', self.screen[-1500:])!r}")

    def settle(self, seconds: float = 0.6) -> None:
        import select

        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                chunk = os.read(self.fd, 65536).decode(errors="replace")
                self.screen += chunk
                self.transcript += chunk

    def send(self, data: str) -> None:
        self.screen = ""
        os.write(self.fd, data.encode())

    def finish(self, timeout: float = 10.0) -> int:
        end = time.time() + timeout
        import select

        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pid = 0
                return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
            # Drain the terminal so the wizard never blocks on a full pty buffer.
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536).decode(errors="replace")
                    self.screen += chunk
                    self.transcript += chunk
                except OSError:
                    time.sleep(0.1)
        self.fail("the wizard did not exit")

    def make_project(self, name: str = "project") -> tuple[Path, Path]:
        project = self.base / name
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True, env=self.env)
        (project / "qa-check").write_text("#!/usr/bin/python3\nprint(1)\n")
        (project / "qa-check").chmod(0o700)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "init"], check=True, env=self.env)
        import hashlib
        import json
        interpreter = hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest()
        check = {
            "driver": "python-wrapper", "argv": ["./qa-check"],
            "executable_sha256": hashlib.sha256((project / "qa-check").read_bytes()).hexdigest(),
            "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 5,
        }
        checks = self.base / "operational-checks.json"
        checks.write_text(json.dumps({
            "version": 1,
            "deterministic_checks": [{"check_id": "tests", **check}],
            "security_checks": [{"check_id": "security", **check}],
        }))
        return project, checks

    def to_project_prompt(self, project: Path) -> None:
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")

    def test_quit_on_the_first_screen_changes_nothing(self) -> None:
        self.start(self.base)
        self.expect("Step 1 of 8")
        self.expect("signed in (plan: max, login: x)")
        self.send("q")
        self.expect("Quit setup?")
        self.send("y")
        self.assertEqual(self.finish(), 130)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_sigterm_during_a_slow_command_leaves_no_child(self) -> None:
        project, checks = self.make_project()
        self.start(project, {"FAKE_SLOW": "1"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        deadline = time.time() + 20
        while not self.slow_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        child = int(self.slow_pid.read_text())
        os.kill(child, 0)  # it is running
        os.kill(self.pid, signal.SIGTERM)
        self.assertEqual(self.finish(), 130)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the slow child is still running")
        self.assertEqual(list(self.tmpdir.iterdir()), [], "the private log directory was kept")

    @unittest.skipUnless(sys.platform == "darwin",
                         "operational checks use the macOS sandbox, so verify is not ready elsewhere")
    def test_full_run_leaves_no_bytecode_quotes_commands_and_cleans_up(self) -> None:
        project, checks = self.make_project("my proj;x")
        self.to_project_prompt(project)
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        self.expect("fresh install is possible")
        self.send("\r")
        self.expect("Codex children")
        self.send("\r")
        self.expect("Looks right")
        self.send("\r")
        self.expect("Files to add")
        self.expect("Install now")
        self.send("\r")
        self.expect("Commit now")
        self.send("\r")
        self.expect("Verification passed")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\x1b[B\r")
        self.expect("Not chosen")
        self.send("\r")
        self.expect("Press Enter to exit")
        self.assertIn("my\\ proj\\;x", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen))
        self.assertNotIn("codex -p orchestrator", self.screen.split("Codex, start")[-1][:60])
        final = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen.split("\x1b[2J")[-1]).replace("\r", "")
        self.assertIn("4. Install: installed and verified; committed", final)
        self.assertLess(final.index("7. Profile"), final.index("How to use it"))
        self.assertLess(final.index("How to use it"), final.index("To remove everything, run:"))
        self.assertRegex(final, r"To remove everything, run:\n\s+python3 .*install\.py.* remove --target ")
        self.assertEqual(final.count("To remove everything"), 1)
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        runs = self.py_log.read_text().split()
        self.assertTrue(runs and set(runs) == {"1"}, runs)
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(status, "")

    def test_failure_summary_is_an_allowlist_and_never_echoes_output(self) -> None:
        project, checks = self.make_project()
        self.start(project, {"FAKE_FAIL": "1"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        for wait in ("fresh install is possible", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("The install failed")
        self.expect("run it manually to see why")
        self.expect("Continue to the summary")
        self.send("\r")
        self.expect("Save a summary of what failed")
        self.send("\x1b[B\r")
        self.expect("Save the summary to")
        destination = self.base / "summary.txt"
        self.send(f"{destination}\r")
        self.expect("Saved a summary")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        saved = destination.read_text()
        self.assertIn("exit code 2", saved)
        self.assertIn("install failed".lower(), saved.lower())
        for text in (saved, self.transcript):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("user@example.invalid", text)
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o600")

    def test_linux_warns_that_operational_checks_need_macos(self) -> None:
        (self.fakes / "uname").write_text('#!/bin/sh\necho Linux\n')
        (self.fakes / "uname").chmod(0o755)
        self.start(self.base)
        self.expect("Operational checks need macOS today")
        self.expect("known limitation")
        self.assertNotIn("FAIL", self.screen)
        self.send("q")
        self.expect("Quit setup?")
        self.send("y")
        self.assertEqual(self.finish(), 130)

    def project_under_home(self) -> tuple[Path, Path]:
        project, checks = self.make_project("staging")
        (self.home / "Code").mkdir()
        moved = self.home / "Code" / "proj"
        project.rename(moved)
        return moved, checks

    def test_home_markers_in_the_project_path_are_expanded(self) -> None:
        for typed in ("~/Code/proj", "$HOME/Code/proj", "${HOME}/Code/proj", "'~/Code/proj'"):
            with self.subTest(typed=typed):
                self.setUp()
                project, _ = self.project_under_home()
                self.to_project_prompt(project)
                self.send(f"{typed}\r")
                self.expect("Path to operational-checks.json")
                self.assertNotIn("Cannot open", self.screen)
                self._reap()
                self.pid = 0

    def test_a_lost_tilde_is_reported_exactly_and_offered_back(self) -> None:
        project, checks = self.project_under_home()
        self.to_project_prompt(project)
        self.send("/Code/proj\r")
        self.expect("Cannot open")
        self.expect("Choose another path")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn("Cannot open: [/Code/proj]", plain)
        self.assertIn("Type the full path, for example /Users/<you>/projects/app. Tab completes paths.", plain)
        self.assertIn(f"that path does not exist, but this one does: {project}".lower(), plain.lower())
        self.assertIn("Use the path shown above", plain)
        self.send("\r")
        self.expect("Path to operational-checks.json")

    def test_cannot_open_without_a_home_match_offers_no_guess(self) -> None:
        project, _ = self.project_under_home()
        self.to_project_prompt(project)
        self.send("~/Code/nowhere\r")
        self.expect("Cannot open")
        self.expect("Choose another path")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn(f"Cannot open: [{self.home}/Code/nowhere]", plain)
        self.assertIn("You typed: [~/Code/nowhere]", plain)
        self.assertIn("Tab completes paths", plain)
        self.assertNotIn("but this one does", plain)
        self.assertNotIn("Use the path shown above", plain)

    def drive_to_commit_menu(self, project: Path, checks: Path, extra_env: dict | None = None) -> None:
        self.start(project, extra_env)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        for wait in ("fresh install is possible", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("Commit now")

    def finish_after_install(self) -> str:
        """Skip the collector, billing and profile steps, and return the final screen."""
        self.expect("Continue")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\x1b[B\r")
        self.expect("Not chosen")
        self.send("\r")
        self.expect("remove --target")
        self.settle()
        final = self.screen.split("\x1b[2J")[-1]
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", final).replace("\r", "")

    def test_summary_says_not_committed_and_lists_what_is_left(self) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks)
        self.send("\x1b[B\r")
        final = self.finish_after_install()
        self.assertIn("not committed (you chose to commit yourself)", final)
        self.assertIn("Left uncommitted in the project:", final)
        self.assertIn(".agents/skills/", final)
        self.assertRegex(final, r"\.\.\. and \d+ more")
        self.assertNotIn("commit failed", final)
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertNotEqual(status, "")

    def test_a_failed_path_helper_stages_and_commits_nothing(self) -> None:
        project, checks = self.make_project()
        self.start(project, {"FAKE_PJ_FAIL": "1"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        for wait in ("fresh install is possible", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("commit failed: no paths to commit")
        self.expect("Continue")
        self.assertNotIn("Commit now", self.screen)
        final = self.finish_after_install()
        self.assertIn("commit failed", final)
        count = subprocess.run(["git", "-C", str(project), "rev-list", "--count", "HEAD"],
                               capture_output=True, text=True, env=self.env).stdout.strip()
        self.assertEqual(count, "1")
        staged = subprocess.run(["git", "-C", str(project), "diff", "--cached", "--name-only"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(staged, "")

    def test_wildcard_named_obsolete_file_is_removed_without_touching_its_neighbour(self) -> None:
        import hashlib
        import json
        project, checks = self.make_project()
        routing_in = self.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
             "--project-agent-routing", str(routing_in), "--operational-checks", str(checks), "--apply"],
            check=True, capture_output=True, env=self.env)
        (project / ".codex" / "local*").write_text("local\n")
        (project / ".codex" / "local-other").write_text("other\n")
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        data = json.loads(manifest.read_text())
        data["files"][".codex/local*"] = hashlib.sha256(b"local\n").hexdigest()
        manifest.write_text(json.dumps(data, indent=2))
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "adopt"], check=True, env=self.env)
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\r")
        for wait in ("fresh install is possible", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("Commit now")
        self.assertIn(".codex/local*", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen))
        self.send("\r")
        final = self.finish_after_install()
        self.assertIn("; committed", final)
        tracked = subprocess.run(["git", "-C", str(project), "ls-files"],
                                 capture_output=True, text=True, env=self.env).stdout.splitlines()
        self.assertNotIn(".codex/local*", tracked)
        self.assertIn(".codex/local-other", tracked)
        self.assertEqual((project / ".codex" / "local-other").read_text(), "other\n")
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(status, "")

    def test_summary_says_commit_failed_when_a_hook_refuses(self) -> None:
        project, checks = self.make_project()
        hook = project / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.drive_to_commit_menu(project, checks)
        self.send("\r")
        final = self.finish_after_install()
        self.assertIn("commit failed", final)
        self.assertNotIn("not committed", final)
        self.assertIn("Left uncommitted in the project:", final)

    def test_commit_stages_updated_files_and_provider_routing(self) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks)
        self.send("\r")
        final = self.finish_after_install()
        self.assertIn("; committed", final)
        self.assertNotIn("not committed", final)
        self.assertNotIn("Left uncommitted", final)
        tracked = subprocess.run(["git", "-C", str(project), "ls-files"],
                                 capture_output=True, text=True, env=self.env).stdout.split()
        self.assertIn(".codex/orchestration/provider-routing.json", tracked)
        self.assertIn(".codex/orchestration/install-manifest.json", tracked)
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(status, "")

    def test_path_starting_with_a_dash_is_resolved(self) -> None:
        (self.base / "-odd").mkdir()
        self.start(self.base)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("-odd\r")
        self.expect("not inside a Git working tree")
        self.assertNotIn("Cannot open", self.screen)


if __name__ == "__main__":
    unittest.main()
