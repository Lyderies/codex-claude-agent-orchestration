from __future__ import annotations

import os
import pwd
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


def interpreter_trusted() -> bool:
    """Whether the Codex rule may name this Python (CI toolcaches can be world-writable)."""

    completed = subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from codex_agents.host_commands import interpreter_path; interpreter_path()",
         str(ORCHESTRATION)],
        capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    return completed.returncode == 0


TRUSTED_PYTHON = interpreter_trusted()
SCRATCH_CONTAINER = "agent-orchestration-owner"
FAKE_RULE = '"""A stand-in for the CLI\'s host-command and install-codex-rule replies (FAKE_RULE)."""\nimport hashlib, json, os, sys\nfrom pathlib import Path\nargs = sys.argv[1:]\nwith open(os.environ["FAKE_RULE_LOG"], "a") as log:\n    log.write(" ".join(args) + "\\n")\ncommand = next(item for item in args if item in ("host-command", "install-codex-rule"))\nroot = Path(os.path.realpath(args[args.index("--project-root") + 1]))\ndigest = hashlib.sha256(str(root).encode()).hexdigest()[:12]\nhome = Path(os.environ["HOME"]) / ".codex"\nrule = str(home / "rules" / f"agent-orchestration-{digest}.rules")\ncopy = str(home / "agent-orchestration" / digest / ("0" * 64))\nstatus = os.environ["FAKE_RULE"]\nif command == "host-command":\n    report = {"ready": status == "installed", "codex_rule": status, "rule_file": rule, "runtime_copy": copy}\nelif "--print" in args:\n    report = {"ready": True, "rule_file": rule, "runtime_copy": copy,\n              "rule_text": \'prefix_rule(pattern = ["python3"], decision = "allow")\\n\'}\nelse:\n    report = {"ready": True, "codex_rule": "replaced" if "--replace" in args else "created",\n              "rule_file": rule, "runtime_copy": copy, "runtime_copy_status": "created"}\nprint(json.dumps(report))\n'
ACCOUNT_HOME = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))


def _host_paths():
    import importlib.util
    spec = importlib.util.spec_from_file_location("setup_tests_host_paths",
                                                  ORCHESTRATION / "codex_agents" / "host_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOST_PATHS = _host_paths()


def trusted_interpreter() -> str | None:
    """A Python 3.11+ the wizard will run: in one of the runtime's trusted installation folders
    and not macOS's /usr/bin shim. CI's toolcache Python is not, so another is looked for."""

    candidates = [sys.executable, "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/usr/bin/python3"]
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if not os.path.isfile(real) or (sys.platform == "darwin" and os.path.dirname(real) == "/usr/bin"):
            continue
        if HOST_PATHS.install_location_problem(Path(real)) is not None:
            continue
        version = subprocess.run([real, "-I", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                                 capture_output=True, check=False)
        if version.returncode == 0:
            return real
    return None


TRUSTED_PY = trusted_interpreter()
if TRUSTED_PY and TRUSTED_PY != os.path.realpath(sys.executable):
    # Every setup.sh this process starts finds that interpreter first on PATH.
    _SHIM = Path(tempfile.mkdtemp(prefix="setup-tests-python-"))
    (_SHIM / "python3").symlink_to(TRUSTED_PY)
    os.environ["PATH"] = f"{_SHIM}{os.pathsep}{os.environ['PATH']}"


def account_home_folder(test: unittest.TestCase) -> Path:
    """A fresh folder inside a hidden top-level folder of the account's home, which the
    runtime's rule trusts for installed programs; removed after the test."""

    parent = ACCOUNT_HOME / ".cache" / "agent-orch-tests"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    test.addCleanup(shutil.rmtree, folder, True)
    return folder


def trusted_git() -> str:
    """The git binary setup.sh trusts (never the PATH one)."""

    return subprocess.run(["bash", str(SETUP), "--print-git"], capture_output=True, text=True,
                          check=True).stdout.strip()


def outside_temporary_folders() -> Path | None:
    """An existing folder outside every temporary root: the wizard's private working folder
    and its trusted Python may not live in one."""

    candidates = []
    if sys.platform == "darwin":
        candidates.append(subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                                         text=True, check=False).stdout.strip())
    candidates += [os.environ.get("XDG_RUNTIME_DIR") or "", str(ROOT)]
    temporary = [Path(os.path.realpath(path)) for path in
                 ("/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp")]
    for candidate in candidates:
        real = Path(os.path.realpath(candidate)) if candidate else None
        if real and real.is_dir() and not any(real == root or root in real.parents for root in temporary):
            return real
    return None
NEEDS_TRUSTED_PYTHON = unittest.skipUnless(
    TRUSTED_PYTHON, "this machine's Python cannot be named by the Codex rule"
)


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

    def test_codex_primary_follows_the_provider_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def primary(path: Path) -> str:
                result = subprocess.run(
                    ["bash", str(SETUP), "--print-codex-primary", str(path)],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            # Without the file only Codex is enabled, so a Codex session may orchestrate.
            self.assertEqual(primary(base / "absent.json"), "yes")
            for choices, expected in (
                (("on", "off", "-", "off"), "yes"),
                (("off", "on", "claude-opus-5-5", "on"), "yes"),
                (("on", "on", "claude-opus-5-5", "off"), "yes"),
                (("off", "on", "claude-opus-5-5", "off"), "no"),
            ):
                with self.subTest(choices=choices):
                    path = base / ("-".join(choices) + ".json")
                    path.write_text(routing(*choices).stdout)
                    self.assertEqual(primary(path), expected)
            (base / "broken.json").write_text("{")
            self.assertEqual(primary(base / "broken.json"), "no")
            (base / "linked.json").symlink_to(base / "on-off---off.json")
            self.assertEqual(primary(base / "linked.json"), "no")
            os.mkfifo(base / "fifo.json")
            self.assertEqual(primary(base / "fifo.json"), "no")

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
        # Found with plumbing: a rename is its new and its old name, each kept whole.
        self.assertEqual(lines, [".codex/extra", ".codex/new name.txt", ".codex/old name .txt"])

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
            result = self.helper("paths", str(apply), str(project), str(plan), trusted_git())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b".codex/ok\0")
            # Invalid UTF-8 in the plan fails closed with no output (core.quotePath=false).
            try:  # macOS file systems refuse invalid UTF-8 names; Linux accepts them.
                (codex / os.fsdecode(b"bad\xff")).write_text("x")
            except OSError:
                pass
            plan.write_bytes(b".codex/ok\0.codex/bad\xff\0")
            result = self.helper("paths", str(apply), str(project), str(plan), trusted_git())
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b"")
            # A missing plan file fails closed too.
            result = self.helper("paths", str(apply), str(project), str(Path(directory) / "missing"), trusted_git())
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b"")
            # A tracked file with an invalid-UTF-8 name does not break a valid list.
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            plan.write_bytes(b".codex/ok\0")
            result = self.helper("paths", str(apply), str(project), str(plan), trusted_git())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b".codex/ok\0")

    def test_every_git_call_uses_the_trusted_binary_with_helpers_off(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        # No git is ever run by name from PATH: only "$GIT", through g() for the project.
        for line in code:
            stripped = line.strip()
            if re.search(r'(^|[;&|]\s*|run_cmd "[^"]*" |\$\()git\s', stripped):
                self.fail(f"git run from PATH: {stripped}")
        self.assertIn("xcrun_nocache=1 GIT_ALLOW_PROTOCOL='' GIT_NO_LAZY_FETCH=1 "
                      '"$GIT" -C "$project" "${GIT_SAFE[@]}" "$@"', text)
        uses = [line for line in code if re.search(r'\bg "\$', line)]
        self.assertGreaterEqual(len(uses), 4)
        # No porcelain: status, add and commit go through the installer's plumbing.
        for line in uses:
            self.assertIsNone(re.search(r'\bg "\$[^"]*"( --?[a-z-]+)* (status|add|commit|diff)\b', line), line)
        self.assertIn('owner_git commit "$PROJECT"', text)
        self.assertIn('owner_git changes "$1" .codex .agents .claude', text)
        safe = re.search(r"^GIT_SAFE=\((.*)\)$", text, re.M).group(1).split()
        self.assertEqual(safe, ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
                                "-c", "gc.auto=0", "-c", "maintenance.auto=false"])
        # The helper's own git call is the trusted binary too.
        self.assertIn('[git, "-C", project, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",', text)

    def test_the_helper_pattern_matches_the_installer(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("install_for_pattern", ROOT / "scripts" / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pattern = re.search(r"^GIT_HELPER_KEYS='(.*)'$", SETUP.read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(pattern, module.GIT_HELPER_KEYS)

    def test_the_trusted_git_is_never_the_path_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stubs = Path(directory)
            (stubs / "git").write_text("#!/bin/sh\necho planted\n")
            (stubs / "git").chmod(0o755)
            found = subprocess.run(["bash", str(SETUP), "--print-git"], capture_output=True, text=True,
                                   env=dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}")).stdout.strip()
        self.assertNotEqual(found, str(stubs / "git"))
        if found != "none":
            self.assertTrue(os.path.isabs(found))
            if sys.platform == "darwin":
                self.assertNotEqual(os.path.dirname(found), "/usr/bin")  # the xcrun shim

    def test_helper_programs_in_the_repository_stop_porcelain_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            marker = Path(directory) / "filter-ran"
            script = Path(directory) / "filter.sh"
            script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
            script.chmod(0o755)
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
            run = lambda *args: subprocess.run(["bash", str(SETUP), *args], capture_output=True, env=env)  # noqa: E731
            self.assertEqual(run("--print-git-helpers", str(project)).stdout.strip(), b"none")
            subprocess.run(["git", "-C", str(project), "config", "filter.planted.clean", str(script)],
                           check=True, env=env)
            (project / ".git" / "info").mkdir(exist_ok=True)
            (project / ".git" / "info" / "attributes").write_text("* filter=planted\n")
            (project / ".codex").mkdir()
            (project / ".codex" / "a").write_text("x")
            self.assertIn(b"filter.planted.clean", run("--print-git-helpers", str(project)).stdout)
            # Status is never run: every candidate counts as changed, the ignored query fails.
            self.assertEqual(run("--print-changed-paths", str(project), ".codex/a").stdout, b".codex/a\0")
            self.assertEqual(run("--print-ignored-paths", str(project), ".codex/a").stdout, b"!\0")
            self.assertIn(b"not checked", run("--print-uncommitted", str(project)).stdout)
            self.assertFalse(marker.exists())
            subprocess.run(["git", "-C", str(project), "config", "--unset", "filter.planted.clean"],
                           check=True, env=env)
            subprocess.run(["git", "-C", str(project), "config", "include.path", "../elsewhere"],
                           check=True, env=env)
            self.assertIn(b"include.path", run("--print-git-helpers", str(project)).stdout)

    def test_status_helpers_never_run_a_planted_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            marker = Path(directory) / "monitor-ran"
            monitor = Path(directory) / "monitor.sh"
            monitor.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
            monitor.chmod(0o755)
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
            subprocess.run(["git", "-C", str(project), "config", "core.fsmonitor", str(monitor)],
                           check=True, env=env)
            (project / ".codex").mkdir()
            (project / ".codex" / "a").write_text("x")
            for hook in (["--print-changed-paths", str(project), ".codex/a"],
                         ["--print-ignored-paths", str(project), ".codex/a"],
                         ["--print-uncommitted", str(project)]):
                result = subprocess.run(["bash", str(SETUP), *hook], capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

    def test_every_python_the_wizard_starts_is_isolated(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        # Every use of "$PY" either launches it isolated or only inspects the path.
        inspecting = ("inside_temp ", "inside_dir ", "dirname -- ", "[ -n ", "printf '%s\\n' ",
                      "python_location_problem ")
        launches = 0
        for line in code:
            for match in re.finditer(r'"\$PY"', line):
                before, after = line[:match.start()], line[match.end():]
                if before.endswith(inspecting):
                    continue
                launches += 1
                with self.subTest(line=line.strip()):
                    self.assertTrue(after.startswith(' "${PYF[@]}" '), line.strip())
        self.assertGreaterEqual(launches, 12)
        self.assertIn("PYF=(-I -B -S -X pycache_prefix=/dev/null)", text)
        # The commands shown to the owner carry the same flags.
        for line in code:
            if "python3 -I" in line:
                self.assertIn("python3 -I -B -S -X pycache_prefix=/dev/null", line)
        # No bare python3 is ever run: only the resolved, checked "$PY".
        for line in code:
            stripped = line.strip()
            if re.match(r'^(run_cmd "[^"]*" )?python3\b', stripped) or re.search(r'\$\(python3\b', stripped):
                self.fail(f"bare python3 launch: {stripped}")
        # Owner commands run this repository's CLI, never the project's copy.
        self.assertIn('run_cmd "$label" "$PY" "${PYF[@]}" "$CLONE_CLI" "$@"', text)
        self.assertIn('CLONE_CLI="$ORCH_DIR/cli.py"', text)
        self.assertNotIn('"$PROJECT/.codex/orchestration/cli.py" "$@"', text)

    def python_for(self, path_entry: Path, home: str = "/nonexistent-home-for-tests",
                   cwd: Path | None = None) -> str:
        result = subprocess.run(["bash", str(SETUP), "--print-python"], capture_output=True, text=True,
                                env=dict(os.environ, PATH=f"{path_entry}:/usr/bin:/bin", HOME=home),
                                cwd=cwd)
        return result.stdout.strip()

    def test_python_must_be_in_a_trusted_installation_folder(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        sibling = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside)) / "tools"
        self.addCleanup(shutil.rmtree, sibling.parent, True)
        hidden = account_home_folder(self) / "bin"
        for folder, expected in ((sibling, "refused: install"), (hidden, None)):
            with self.subTest(folder=str(folder)):
                folder.mkdir(parents=True)
                (folder / "python3").write_text("#!/bin/sh\nexit 99\n")  # never run
                (folder / "python3").chmod(0o755)
                self.assertEqual(self.python_for(folder), expected or str(folder.resolve() / "python3"))

    def test_the_trusted_installation_folders_match_the_runtime(self) -> None:
        printed = subprocess.run(["bash", str(SETUP), "--print-trusted-roots"], capture_output=True,
                                 text=True, check=True).stdout.split()
        self.assertEqual(tuple(printed), HOST_PATHS.TRUSTED_INSTALL_ROOTS)

    def test_python_inside_the_starting_project_is_refused_before_it_runs(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, base, True)
        project = base / "project"
        (project / "tools" / "bin").mkdir(parents=True)
        (project / ".git").mkdir()
        ran = base / "ran"
        (project / "tools" / "bin" / "python3").write_text(f"#!/bin/sh\ntouch '{ran}'\n")
        (project / "tools" / "bin" / "python3").chmod(0o755)
        (project / "src").mkdir()
        self.assertEqual(self.python_for(project / "tools" / "bin", cwd=project / "src"), "refused: project")
        self.assertFalse(ran.exists())

    @unittest.skipUnless(sys.platform == "darwin", "the xcrun shim is macOS's")
    def test_the_macos_python_shim_is_refused(self) -> None:
        if not Path("/usr/bin/python3").exists():
            self.skipTest("no /usr/bin/python3")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.python_for(Path(directory)), "refused: shim")

    def test_python_is_the_real_file_and_never_temporary_or_a_virtual_environment(self) -> None:
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        base = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, base, True)
        if TRUSTED_PY is None:
            self.skipTest("no Python in a trusted installation folder")
        real = TRUSTED_PY
        linked = base / "linked"
        linked.mkdir()
        (linked / "python3").symlink_to(real)
        self.assertEqual(self.python_for(linked), real)  # a symlink runs as its real file
        venv = base / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python3").write_text("#!/bin/sh\nexit 99\n")
        (venv / "bin" / "python3").chmod(0o755)
        (venv / "pyvenv.cfg").write_text("home = /elsewhere\n")
        self.assertEqual(self.python_for(venv / "bin"), "refused: venv")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            shutil.copy(real, temporary / "python3")
            self.assertEqual(self.python_for(temporary), "refused: temporary")
            # A link from a safe folder into a temporary one is judged by its real file.
            (linked / "python3").unlink()
            (linked / "python3").symlink_to(temporary / "python3")
            self.assertEqual(self.python_for(linked), "refused: temporary")

    def test_only_the_macos_only_check_failure_is_quiet_on_linux(self) -> None:
        import json
        sandbox = ("operational check sandbox preflight failed; validate from an owner shell or "
                   "approved unsandboxed command boundary")
        limited = {"ready": False, "codex": {"package_ready": True, "project_errors": [sandbox]},
                   "claude": {"result": "verified"}, "claude_settings": {"ready": True}}
        cases = {
            "ready": {**limited, "ready": True},
            "linux-limited": limited,
            "failed": None,
        }
        failures = [
            {**limited, "claude_settings": {"ready": False, "missing": ["Edit(/.git/**)"]}},
            {**limited, "codex": {"package_ready": True, "project_errors": [sandbox, "not committed and clean"]}},
            {**limited, "codex": {"package_ready": False, "project_errors": [sandbox]}},
            {**limited, "claude": {"error": "managed_file_modified"}},
            {"refused": "the project's Git settings name programs Git would run"},
            {},
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "verify.json"
            for expected, data in cases.items():
                for sample in ([data] if data is not None else failures):
                    with self.subTest(expected=expected, sample=str(sample)[:60]):
                        report.write_text(json.dumps(sample))
                        result = self.helper("verifyclass", str(report))
                        self.assertEqual(result.stdout.decode().strip(), expected)

    def test_every_git_call_with_paths_is_literal(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        for needle in (" add -A -- ", " commit -m ", '"ls-files"'):
            lines = [line for line in text.splitlines()
                     if needle in line and not line.lstrip().startswith("#")]
            self.assertTrue(lines, needle)
            for line in lines:
                with self.subTest(line=line.strip()):
                    self.assertIn("--literal-pathspecs", line)

    def test_changed_paths_lists_only_paths_with_real_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
            (project / ".codex").mkdir()
            for name in ("a.json", "b c.json", "gone.json", "star*.json"):
                (project / ".codex" / name).write_text("x\n")
            git = ["git", "-C", str(project)]
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            subprocess.run(git + ["commit", "-qm", "i"], check=True, env=env)
            candidates = [".codex/a.json", ".codex/b c.json", ".codex/gone.json", ".codex/star*.json",
                          ".codex/new.json"]

            def changed() -> list[bytes]:
                result = subprocess.run(
                    ["bash", str(SETUP), "--print-changed-paths", str(project), *candidates],
                    capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.split(b"\0")[:-1]

            self.assertEqual(changed(), [])  # clean project: nothing to commit
            (project / ".codex" / "b c.json").write_text("changed\n")
            (project / ".codex" / "gone.json").unlink()
            (project / ".codex" / "new.json").write_text("new\n")
            (project / ".codex" / "unrelated.json").write_text("not a candidate\n")
            self.assertEqual(sorted(changed()),
                             [b".codex/b c.json", b".codex/gone.json", b".codex/new.json"])

    def test_changed_paths_prints_every_candidate_when_git_cannot_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", str(SETUP), "--print-changed-paths", directory, ".codex/a", ".codex/b"],
                capture_output=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                                              GIT_CEILING_DIRECTORIES=str(Path(directory).parent)))
        self.assertEqual(result.stdout, b".codex/a\0.codex/b\0")

    def profile_compare(self, text: str | None) -> str:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "orchestrator.config.toml"
            if text is not None:
                profile.write_text(text)
            result = self.helper("profilecmp", str(profile))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().strip()

    def test_profile_compares_effective_settings_not_bytes(self) -> None:
        header = f"# written by hand {TOKEN}\n\n"
        same = (header + 'sandbox_mode = "workspace-write"  # keep\n\n'
                "[sandbox_workspace_write]\nnetwork_access = true\n")
        self.assertEqual(self.profile_compare(same), "same")
        extra = same + 'writable_roots = ["x"]\n'
        self.assertEqual(self.profile_compare(extra), "same")
        self.assertEqual(self.profile_compare(same.replace("= true", "= false")), "network_access")
        self.assertEqual(self.profile_compare(same.replace("workspace-write", "read-only")), "sandbox_mode")
        self.assertEqual(self.profile_compare('sandbox_mode = "workspace-write"\n'), "network_access")
        self.assertEqual(self.profile_compare("this is [not toml"), "unreadable")
        self.assertEqual(self.profile_compare(None), "unreadable")
        self.assertNotIn(TOKEN, self.profile_compare(same))

    def test_changed_paths_keeps_a_rename_source_that_is_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
            (project / ".codex").mkdir()
            (project / ".codex" / "old.txt").write_text("same content for rename detection\n" * 5)
            (project / ".codex" / "kept.txt").write_text("copy source\n" * 5)
            git = ["git", "-C", str(project)]
            subprocess.run(git + ["add", "-A"], check=True, env=env)
            subprocess.run(git + ["commit", "-qm", "i"], check=True, env=env)
            subprocess.run(git + ["mv", ".codex/old.txt", ".codex/new.txt"], check=True, env=env)
            status = subprocess.run(git + ["status", "--porcelain", "-z"], capture_output=True, env=env)
            self.assertEqual(status.stdout, b"R  .codex/new.txt\0.codex/old.txt\0")
            result = subprocess.run(
                ["bash", str(SETUP), "--print-changed-paths", str(project),
                 ".codex/new.txt", ".codex/old.txt", ".codex/kept.txt"],
                capture_output=True, env=env)
        self.assertEqual(sorted(result.stdout.split(b"\0")[:-1]), [b".codex/new.txt", b".codex/old.txt"])

    def test_ignored_untracked_candidates_are_reported_not_called_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.git_project(directory)
            (project / ".gitignore").write_text(".codex/\n")
            (project / ".codex").mkdir()
            (project / ".codex" / "ours.json").write_text("x\n")
            (project / ".codex" / "sp ace.json").write_text("x\n")
            candidates = [".codex/ours.json", ".codex/sp ace.json", ".codex/absent.json"]
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
            changed = subprocess.run(["bash", str(SETUP), "--print-changed-paths", str(project), *candidates],
                                     capture_output=True, env=env)
            ignored = subprocess.run(["bash", str(SETUP), "--print-ignored-paths", str(project), *candidates],
                                     capture_output=True, env=env)
        self.assertEqual(changed.stdout, b"")  # status never lists them: the reason for the check
        self.assertEqual(sorted(ignored.stdout.split(b"\0")[:-1]), [b".codex/ours.json", b".codex/sp ace.json"])

    def test_a_failed_ignored_file_query_is_reported_not_read_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Not a repository: the trusted git cannot answer.
            result = subprocess.run(
                ["bash", str(SETUP), "--print-ignored-paths", directory, ".codex/a"],
                capture_output=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                                              GIT_CEILING_DIRECTORIES=str(Path(directory).parent)))
        self.assertEqual(result.stdout, b"!\0")

    def test_profile_larger_than_the_read_limit_is_unreadable(self) -> None:
        good = 'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n'
        padded = good + "# pad\n" * 12000 + "network_access = false\n"
        self.assertGreater(len(padded), 65536)
        self.assertEqual(self.profile_compare(padded), "unreadable")
        self.assertEqual(self.profile_compare(good + "# pad\n" * 100), "same")

    def test_commands_and_commit_screens_say_what_the_rule_and_commit_do(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("Every Codex session on this machine loads this rule from $RULE_FILE (mode 0600). "
                      "It runs a protected copy of the orchestrator in ~/.codex that sessions opened on a "
                      "project cannot change. Never open Codex on your home folder, its parent or / while "
                      "the rule exists.", text)
        self.assertNotIn("It applies to Codex sessions started in this project's root folder", text)
        self.assertIn("Setup commits with Git's plumbing commands only (hash-object, update-index, write-tree, "
                      "commit-tree, update-ref), which start no hook, filter, signing program, pager or "
                      "maintenance whatever the project's Git settings say", text)
        self.assertNotIn("no script in the project runs as you", text)
        self.assertNotIn("or a commit hook stopped it", text)
        self.assertIn("Keep this setup folder outside every folder you open in Codex", text)
        self.assertIn("including the Codex commands rule", text)

    def test_wizard_messages_for_the_polished_screens(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("Nothing to commit: the project already has these files committed", text)
        self.assertIn("The install can be updated.", text)
        self.assertIn("A fresh install is possible.", text)
        self.assertIn("Profile: exists with the same settings", text)
        self.assertIn("network_access is off", text)
        self.assertIn('"Change the owner key passphrase"', text)
        self.assertIn("attest.py\" --change-passphrase", text)
        self.assertIn('"$HOME/agent-orchestration-setup-summary.txt"', text)
        self.assertNotIn('"$PWD/agent-orchestration-setup-summary.txt"', text)

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
        if TRUSTED_PY is None:
            self.skipTest("no Python in a trusted installation folder")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        # The fake python3 and the private working folder may not sit in a temporary folder.
        outside = outside_temporary_folders()
        if outside is None:
            self.skipTest("no folder outside the temporary roots is available")
        self.outside = Path(tempfile.mkdtemp(prefix="agent-orch-test-", dir=outside))
        self.addCleanup(shutil.rmtree, self.outside, True)
        # The fake python3 must sit in a trusted installation folder: a hidden folder of the
        # account's home, by the runtime's rule.
        self.fakes = account_home_folder(self) / "bin"
        self.fakes.mkdir()
        (self.fakes / "fake_rule.py").write_text(FAKE_RULE)
        self.rule_log = self.base / "rule.log"
        self.home = self.base / "home"
        self.home.mkdir()
        self.tmpdir = self.base / "tmp"
        self.tmpdir.mkdir()
        (self.fakes / "codex").write_text("#!/bin/sh\nexit 0\n")
        (self.fakes / "claude").write_text(
            '#!/bin/sh\necho \'{"loggedIn": true, "subscriptionType": "max", "authMethod": "x"}\'\n')
        self.py_log = self.base / "python.log"
        self.py_args = self.base / "python-args.log"
        self.py_full = self.base / "python-full.log"
        self.slow_pid = self.base / "slow.pid"
        (self.fakes / "python3").write_text(
            "#!/bin/sh\n"
            f'echo "$PYTHONDONTWRITEBYTECODE" >> "{self.py_log}"\n'
            f'echo "$1 $2 $3 $4 $5" >> "{self.py_args}"\n'
            f'printf "%s\\n" "$*" >> "{self.py_full}"\n'
            'if [ -n "$FAKE_PLANT" ]; then case "$*" in *install.py*--apply*)'
            ' "$0.real" "$@"; rc=$?; git -C "$FAKE_PLANT" config filter.planted.clean "$FAKE_PLANT_SCRIPT";'
            ' exit $rc;; esac; fi\n'
            'if [ -n "$FAKE_FAIL" ]; then case "$*" in *install.py*--apply*)'
            ' echo \'{"error": "boom ' + TOKEN + ' user@example.invalid"}\'; echo ' + TOKEN + ' >&2; exit 2;; esac; fi\n'
            f'if [ -n "$FAKE_SLOW" ]; then case "$*" in *install.py*install*)'
            f' echo $$ > "{self.slow_pid}"; exec sleep 60;; esac; fi\n'
            'for a in "$@"; do if [ "$a" = paths ] && [ -n "$FAKE_PJ_FAIL" ]; then exit 1; fi; done\n'
            'if [ -n "$FAKE_ATTEST" ]; then case "$*" in *"attest.py --"*)'
            ' n=$(cat "$FAKE_ATTEST" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$FAKE_ATTEST";'
            ' echo "attest stub run $n"; [ "$n" -le "${FAKE_ATTEST_FAILS:-0}" ] && exit 1; exit 0;; esac; fi\n'
            'if [ -n "$FAKE_UNTRUSTED" ]; then case "$*" in *host-command*)'
            ' echo \'{"ready": false, "error": "the Python interpreter cannot be trusted: x"}\';'
            ' exit 2;; esac; fi\n'
            'if [ -n "$FAKE_RULE" ]; then case "$*" in *"cli.py host-command"*|*"cli.py install-codex-rule"*)'
            f' exec "{TRUSTED_PY}" "{self.fakes / "fake_rule.py"}" "$@";; esac; fi\n'
            f'exec "{TRUSTED_PY}" "$@"\n')
        (self.fakes / "python3.real").symlink_to(TRUSTED_PY)
        for fake in self.fakes.iterdir():
            if not fake.is_symlink():
                fake.chmod(0o755)
        # CODEX_HOME is dropped so the commands step can only write under the test's HOME.
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NO_COLOR",
                                 "PYTHONDONTWRITEBYTECODE", "CODEX_HOME")}
        self.env.update(
            PATH=f"{self.fakes}:{os.environ['PATH']}", HOME=str(self.home), TERM="xterm",
            FAKE_RULE_LOG=str(self.rule_log),
            TMPDIR=str(self.tmpdir), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        if sys.platform == "darwin":  # the per-user cache folder, from getconf
            cache = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                                   text=True, check=True).stdout.strip()
            self.scratch = Path(os.path.realpath(cache)) / SCRATCH_CONTAINER
        else:  # a private XDG_RUNTIME_DIR for this test
            runtime = self.outside / "run"
            runtime.mkdir(mode=0o700)
            self.env["XDG_RUNTIME_DIR"] = str(runtime)
            self.scratch = runtime / SCRATCH_CONTAINER
        self.scratch_before = self.scratch_entries()
        self.pid = 0
        self.fd = -1
        self.screen = ""
        self.transcript = ""

    def scratch_entries(self) -> set[str]:
        try:
            return {name for name in os.listdir(self.scratch) if name.startswith("setup-")}
        except FileNotFoundError:
            return set()

    def assert_no_working_files_left(self) -> None:
        self.assertEqual(list(self.tmpdir.iterdir()), [], "something was written to TMPDIR")
        self.assertEqual(self.scratch_entries() - self.scratch_before, set(),
                         "the private working folder was kept")

    def assert_python_isolated(self) -> None:
        lines = self.py_args.read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(line, "-I -B -S -X pycache_prefix=/dev/null", line)

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
        # A wizard killed with SIGKILL cannot remove its private working folder.
        for name in self.scratch_entries() - self.scratch_before:
            shutil.rmtree(self.scratch / name, ignore_errors=True)

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

    def non_temporary_base(self) -> Path:
        """A fresh folder outside every temporary root (a rule refuses those), or skip."""

        candidates = [os.environ.get("XDG_RUNTIME_DIR") or ""]
        if sys.platform == "darwin":
            cache = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"],
                                   capture_output=True, text=True, check=False).stdout.strip()
            candidates.insert(0, cache)
        temporary = {os.path.realpath(path) for path in
                     ("/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp")}
        for candidate in candidates:
            real = os.path.realpath(candidate) if candidate else ""
            if real and os.path.isdir(real) and not any(
                real == root or real.startswith(root + os.sep) for root in temporary
            ):
                base = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-orch-test-", dir=real)))
                self.addCleanup(shutil.rmtree, base, True)
                return base
        self.skipTest("no folder outside the temporary roots is available")

    def make_project(self, name: str = "project", base: Path | None = None) -> tuple[Path, Path]:
        project = (base or self.base) / name
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
        self.expect("Step 1 of 9")
        self.expect("signed in (plan: max, login: x)")
        self.send("q")
        self.expect("Quit setup?")
        self.send("y")
        self.assertEqual(self.finish(), 130)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assert_no_working_files_left()

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
        self.assert_no_working_files_left()

    @unittest.skipUnless(sys.platform == "darwin",
                         "operational checks use the macOS sandbox, so verify is not ready elsewhere")
    def test_full_run_leaves_no_bytecode_quotes_commands_and_cleans_up(self) -> None:
        # The Codex rule's replies come from the fake CLI (the runtime's account-home check
        # refuses a test HOME); test_install drives the real rule lifecycle in process.
        project, checks = self.make_project("my proj;x", self.non_temporary_base())
        self.env["FAKE_RULE"] = "missing"
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
        self.expect("Yes, allow these commands")
        self.assertIn("owner commands", self.screen)
        commands = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B|\r|\n|  +", " ", self.screen)
        self.assertIn("Every Codex session on this machine loads this rule", re.sub(" +", " ", commands))
        self.send("\x1b[B\x1b[B\r")
        self.expect("Codex commands rule: lines")
        self.send("q")
        self.expect("Yes, allow these commands")
        self.send("\x1b[B\r")
        self.expect("Rule created")
        self.send("\r")
        self.expect("Back to the main menu")
        self.assertIn("my\\ proj\\;x", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen))
        self.assertNotIn("codex -p orchestrator", self.screen.split("Codex, start")[-1][:60])
        final = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen.split("\x1b[2J")[-1]).replace("\r", "")
        self.assertIn("4. Install: installed and verified; committed", final)
        self.assertLess(final.index("7. Profile"), final.index("8. Commands: created"))
        self.assertLess(final.index("8. Commands"), final.index("How to use it"))
        self.assertLess(final.index("How to use it"), final.index("To remove everything"))
        self.assertRegex(final, r"To remove everything, including the Codex commands rule, run:\n"
                                r"\s+python3 -I -B -S .*install\.py.* remove --target ")
        self.assertEqual(final.count("To remove everything"), 1)
        self.assertRegex(final, r"to undo only the rule, delete:\n\s+\S*/\.codex/rules/agent-orchestration-[0-9a-f]{12}\.rules")
        self.assertRegex(final, r"and its copy:\n\s+\S*/\.codex/agent-orchestration/")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        cli = project / ".codex" / "orchestration" / "cli.py"
        # The rule was written by this checkout's own CLI, for this project.
        clone = str(ORCHESTRATION / "cli.py")
        rule_calls = self.rule_log.read_text().splitlines()
        self.assertEqual([call.split(clone + " ", 1)[1] for call in rule_calls if "install-codex-rule" in call],
                         [f"install-codex-rule --project-root {project} --print",
                          f"install-codex-rule --project-root {project}"])
        runs = self.py_log.read_text().split()
        self.assertTrue(runs and set(runs) == {"1"}, runs)
        self.assert_python_isolated()
        self.assert_no_working_files_left()
        # Owner commands ran this checkout's CLI with --project-root, never the project's copy.
        launched = self.py_full.read_text().splitlines()
        clone = str(ORCHESTRATION / "cli.py")
        owner = [line for line in launched if clone in line]
        self.assertTrue(any("host-command" in line for line in owner), launched)
        self.assertTrue(any("install-codex-rule" in line for line in owner), launched)
        self.assertFalse(any(str(cli) in line for line in launched), launched)
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
        self.send("\x1b[B\r")
        self.expect("Save a summary of what failed")
        self.send("\x1b[B\r")
        self.expect("Save the summary to")
        destination = self.base / "summary.txt"
        self.send(f"{destination}\r")
        self.expect("Saved a summary")
        self.send("\r")  # Continue: saving a summary does not end the wizard
        self.expect("Back to the main menu")
        self.send("\r")  # Exit
        self.assertEqual(self.finish(), 0)
        saved = destination.read_text()
        self.assertIn("exit code 2", saved)
        self.assertIn("install failed".lower(), saved.lower())
        for text in (saved, self.transcript):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("user@example.invalid", text)
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o600")
        self.assert_python_isolated()
        self.assert_no_working_files_left()
        # Preview and install read one private snapshot of the chosen checks file, never the file.
        installs = [line for line in self.py_full.read_text().splitlines() if "install.py install" in line]
        self.assertEqual(len(installs), 2)
        for line in installs:
            self.assertNotIn(str(checks), line)
            self.assertRegex(line, SCRATCH_CONTAINER + r"/setup-[0-9a-f]+/inputs/operational-checks\.json")

    def commit_count(self, project: Path) -> int:
        return int(subprocess.run(["git", "-C", str(project), "rev-list", "--count", "HEAD"],
                                  capture_output=True, text=True, env=self.env).stdout.strip())

    def test_an_update_that_changes_nothing_offers_no_commit(self) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks)
        self.send("\r")
        self.finish_after_install()
        before = self.commit_count(project)
        self.assertEqual(before, 2)
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\r")
        for wait in ("install can be updated", "Keep the existing configuration", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("Nothing to commit: the project already has these files committed")
        self.assertNotIn("Commit now", self.screen)
        self.assertNotIn("Next: commit the installed files", self.screen)
        final = self.finish_after_install()
        self.assertIn("nothing to commit", final)
        self.assertNotIn("commit failed", final)
        self.assertNotIn("The commit", final)
        self.assertEqual(self.commit_count(project), before)

    def test_a_helper_planted_during_the_run_stops_git_and_the_commit(self) -> None:
        project, checks = self.make_project()
        marker = self.base / "filter-ran"
        script = self.base / "filter.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        script.chmod(0o755)
        (project / ".git" / "info").mkdir(exist_ok=True)
        (project / ".git" / "info" / "attributes").write_text("* filter=planted\n")
        before = self.commit_count(project)
        # The filter appears in .git/config right after the install, as a session opened on a
        # parent folder could do it.
        self.drive_to_commit_menu(project, checks, {"FAKE_PLANT": str(project), "FAKE_PLANT_SCRIPT": str(script)})
        self.assertIn("could not check whether installed files are ignored", self.screen)
        self.assertNotIn("Nothing to commit", self.screen)
        self.send("\r")  # Commit now
        self.expect("Not committed")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn("filter.planted.clean", plain)
        final = self.finish_after_install()
        self.assertNotIn("nothing to commit", final)
        self.assertEqual(self.commit_count(project), before)
        self.assertFalse(marker.exists(), "the planted filter ran")

    def test_helpers_in_the_project_settings_stop_setup_before_any_git_porcelain(self) -> None:
        project, checks = self.make_project()
        subprocess.run(["git", "-C", str(project), "config", "core.pager", "./pager.sh"],
                       check=True, env=self.env)
        self.to_project_prompt(project)
        self.send("\r")
        self.expect("Choose another path")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B|\r|\n", " ", self.screen)
        self.assertIn("core.pager", plain)
        self.assertIn("will not run Git commands that could start them", re.sub(r"\s+", " ", plain))
        self.send("\x1b[B\r")
        self.assertEqual(self.finish(), 1)

    def test_only_paths_with_changes_are_committed(self) -> None:
        import json
        project, checks = self.make_project()
        routing_in = self.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run(
            [TRUSTED_PY, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
             "--project-agent-routing", str(routing_in), "--operational-checks", str(checks), "--apply"],
            check=True, capture_output=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "adopt"], check=True, env=self.env)
        before = self.commit_count(project)
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\r")
        for wait in ("install can be updated", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("Commit now")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn("Only these 1 paths", plain)
        self.assertIn(".codex/orchestration/provider-routing.json", plain)
        self.assertNotIn("install-manifest.json", plain)
        self.send("\r")
        final = self.finish_after_install()
        self.assertIn("; committed", final)
        self.assertEqual(self.commit_count(project), before + 1)
        changed = subprocess.run(
            ["git", "-C", str(project), "show", "--name-only", "--format=", "HEAD"],
            capture_output=True, text=True, env=self.env).stdout.split()
        self.assertEqual(changed, [".codex/orchestration/provider-routing.json"])

    def failed_install_to_summary_menu(self, project: Path, checks: Path) -> None:
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
        self.expect("Continue to the summary")
        self.send("\x1b[B\r")
        self.expect("Save a summary of what failed")
        self.send("\x1b[B\r")
        self.expect("Save the summary to")

    def test_summary_defaults_to_the_home_folder(self) -> None:
        project, checks = self.make_project()
        self.failed_install_to_summary_menu(project, checks)
        self.assertIn(str(self.home / "agent-orchestration-setup-summary.txt"),
                      re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen))
        self.send("\r")
        self.expect("Saved a summary")
        self.send("\r")  # Continue: saving a summary does not end the wizard
        self.expect("Back to the main menu")
        self.send("\r")  # Exit
        self.assertEqual(self.finish(), 0)
        self.assertTrue((self.home / "agent-orchestration-setup-summary.txt").is_file())
        self.assertFalse((project / "agent-orchestration-setup-summary.txt").exists())
        self.assertFalse((ROOT / "agent-orchestration-setup-summary.txt").exists())

    def test_summary_inside_a_git_working_tree_asks_first(self) -> None:
        project, checks = self.make_project()
        self.failed_install_to_summary_menu(project, checks)
        destination = project / "summary.txt"
        self.send(f"{destination}\r")
        self.expect("inside a Git working tree")
        self.expect("Save it here anyway")
        self.assertFalse(destination.exists())
        self.send("\x1b[B\r")  # save it here anyway
        self.expect("Saved a summary")
        self.send("\r")  # Continue: saving a summary does not end the wizard
        self.expect("Back to the main menu")
        self.send("\r")  # Exit
        self.assertEqual(self.finish(), 0)
        self.assertTrue(destination.is_file())

    def test_summary_path_with_a_control_character_is_refused(self) -> None:
        project, checks = self.make_project()
        self.failed_install_to_summary_menu(project, checks)
        destination = self.base / "summary-bad.txt"
        self.send(f"{self.base}/sum\x16\x01mary.txt\r")  # Ctrl-V Ctrl-A types a control character
        self.expect("control character")
        self.assertFalse(any(self.base.glob("sum*mary.txt")))
        self.assertFalse(destination.exists())
        self.send("\r")  # Continue: saving a summary does not end the wizard
        self.expect("Back to the main menu")
        self.send("\r")  # Exit
        self.assertEqual(self.finish(), 0)

    def to_failed_billing(self, fails: int) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks, {
            "FAKE_ATTEST": str(self.base / "attest.count"), "FAKE_ATTEST_FAILS": str(fails)})
        self.send("\r")
        self.expect("Continue")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("attest stub run 1")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("Try again")

    def test_a_failed_attestation_offers_try_again_and_succeeds_on_retry(self) -> None:
        self.to_failed_billing(1)
        self.settle()
        self.assertIn("Skip for now", self.screen)
        self.assertIn("Back to the main menu", self.screen)
        self.send("\r")  # Try again is the default
        self.expect("attest stub run 2")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("attest.py ran")

    def test_back_to_the_main_menu_after_a_failure_then_another_option_works(self) -> None:
        self.to_failed_billing(1)
        self.send("\x1b[B\x1b[B\r")  # Back to the main menu
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")  # the existing-install menu, not the summary
        self.assertNotIn("Exit", self.screen)
        self.send("\x1b[B\x1b[B\r")  # Re-run the billing attestation only
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("attest stub run 2")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("attest.py ran")
        self.send("\r")
        self.expect("Back to the main menu")
        self.send("\r")
        self.assertEqual(self.finish(), 0)

    def test_the_wizard_only_ends_on_quit_after_failures(self) -> None:
        self.to_failed_billing(2)
        self.send("\x1b[B\x1b[B\r")  # Back to the main menu
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\x1b[B\x1b[B\r")  # Re-run the billing attestation only
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("attest stub run 2")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("Try again")
        self.settle()
        self.assertNotIn("Skip for now", self.screen)  # attestation-only has nothing to skip to
        self.send("\x1b[B\r")  # Back to the main menu
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.settle()
        self.assertGreater(self.pid, 0)  # still running: a failure never ended the wizard
        self.send("\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\r")  # Quit
        self.expect("Stopped.")
        self.assertEqual(self.finish(), 0)

    def test_failure_menu_defaults_to_the_main_menu_and_the_summary_can_go_back(self) -> None:
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
        self.expect("Back to the main menu")
        self.send("\r")  # the default: back, not the summary
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        for wait in ("fresh install is possible", "Codex children", "Looks right", "Install now"):
            self.expect(wait)
            self.send("\r")
        self.expect("Continue to the summary")
        self.send("\x1b[B\r")
        self.expect("Save a summary of what failed")
        self.settle()
        self.assertIn("Back to the main menu", self.screen)
        self.send("\x1b[B\x1b[B\r")  # Back to the main menu from the summary
        self.expect("Project path")
        self.send("q\r")
        self.expect("Quit setup?")  # only an explicit quit ends the script
        self.send("y")
        self.assertEqual(self.finish(), 130)

    @unittest.skipUnless(sys.platform == "darwin",
                         "operational checks use the macOS sandbox, so verify is not ready elsewhere")
    def test_verify_retry_back_and_the_summary_return_to_the_menu(self) -> None:
        import json
        project, checks = self.make_project(base=self.non_temporary_base())
        routing_in = self.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run(
            [TRUSTED_PY, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
             "--project-agent-routing", str(routing_in), "--operational-checks", str(checks), "--apply"],
            check=True, capture_output=True, env=self.env)  # left uncommitted: verification fails
        self.start(project, {"FAKE_RULE": "missing"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Verify the install only")
        self.send("\x1b[B\r")
        self.expect("Try again")
        self.send("\r")  # Try again: still failing, so the menu comes back
        self.expect("Try again")
        self.send("\x1b[B\r")  # Back to the main menu
        self.expect("Project path")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "install"], check=True, env=self.env)
        self.send("\r")
        self.expect("Verify the install only")
        self.send("\x1b[B\r")
        self.expect("Codex commands rule: missing")
        self.expect("Continue")
        self.send("\r")
        self.expect("Back to the main menu")  # the final summary
        self.settle()
        self.assertNotIn("Save a summary of what failed", self.screen)  # the old failures are gone
        self.send("\x1b[B\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\r")  # Quit
        self.expect("Stopped.")
        self.assertEqual(self.finish(), 0)

    def test_going_back_does_not_leak_the_last_attempts_choices_into_the_next(self) -> None:
        project, checks = self.make_project()
        self.start(project, {"FAKE_ATTEST": str(self.base / "attest.count"), "FAKE_ATTEST_FAILS": "1"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Path to operational-checks.json")
        self.send(f"{checks}\r")
        self.expect("fresh install is possible")
        self.send("\r")
        self.expect("Codex children")
        self.send("\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B \r")  # also turn on bridging to Claude
        self.expect("Looks right")
        self.send("\r")
        self.expect("Install now")
        self.send("\r")
        self.expect("Commit now")
        self.send("\r")
        self.expect("Continue")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("attest stub run 1")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("Try again")
        self.send("\x1b[B\x1b[B\r")  # Back to the main menu
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\x1b[B\x1b[B\r")  # a different mode: attestation only
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("Press Enter to continue")
        self.send("\r")
        self.expect("attest.py ran")
        self.send("\r")
        self.expect("Back to the main menu")
        self.settle()
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        # This attempt never chose bridging, so the earlier attempt's profile choice must be gone.
        self.assertIn("bridging was not chosen", plain.replace("\r\n", " "))
        self.assertNotIn("orchestrator profile is missing", plain.replace("\r\n", " "))
        self.send("\r")
        self.assertEqual(self.finish(), 0)

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

    def finish_after_install(self, trusted: bool = TRUSTED_PYTHON, temporary: bool = True) -> str:
        """Skip the collector, billing, profile and commands steps; return the final screen."""
        self.expect("Continue")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\x1b[B\r")
        self.expect("Not chosen")
        self.send("\r")
        if temporary:
            # Test projects live in the system temp folder, which never gets a rule.
            self.expect("This project is in a temporary folder")
        elif trusted:
            self.expect("No, not now")
        else:
            self.expect("This Python cannot be trusted")
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
            [TRUSTED_PY, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
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
        for wait in ("install can be updated", "Codex children", "Looks right", "Install now"):
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

    def test_the_commit_never_runs_project_hooks(self) -> None:
        project, checks = self.make_project()
        ran = self.base / "hook-ran"
        for hooks in (project / ".git" / "hooks", project / "githooks"):
            hooks.mkdir(exist_ok=True)
            for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
                (hooks / name).write_text(f"#!/bin/sh\necho {name} >> '{ran}'\nexit 1\n")
                (hooks / name).chmod(0o755)
        subprocess.run(["git", "-C", str(project), "config", "core.hooksPath", "githooks"],
                       check=True, env=self.env)
        self.drive_to_commit_menu(project, checks)
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn("Setup commits with Git's plumbing commands only", re.sub(r"\s+", " ", plain))
        self.send("\r")
        final = self.finish_after_install()
        self.assertIn("; committed", final)
        self.assertFalse(ran.exists(), "a project hook ran")

    def test_summary_says_commit_failed_when_git_refuses(self) -> None:
        project, checks = self.make_project()
        # Signing is switched off for the commit, so a signing setting never stops it.
        subprocess.run(["git", "-C", str(project), "config", "commit.gpgSign", "true"], check=True, env=self.env)
        self.drive_to_commit_menu(project, checks, {"GIT_AUTHOR_NAME": "", "GIT_COMMITTER_NAME": ""})
        self.send("\r")
        self.expect("Git identity")
        final = self.finish_after_install()
        self.assertIn("commit failed", final)
        self.assertNotIn("not committed", final)
        self.assertIn("Left uncommitted in the project:", final)

    def test_a_modified_orchestrator_is_never_run(self) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks, {"FAKE_ATTEST": str(self.base / "attest.count")})
        self.send("\r")
        self.finish_after_install()
        cli = project / ".codex" / "orchestration" / "cli.py"
        cli.write_text(cli.read_text() + "\nprint('changed')\n")
        subprocess.run(["git", "-C", str(project), "commit", "-qam", "change"], check=True, env=self.env)
        self.py_args.write_text("")
        self.start(project, {"FAKE_ATTEST": str(self.base / "attest.count")})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Update the install")
        self.send("\x1b[B\x1b[B\r")  # Re-run the billing attestation only
        self.expect("Start the attestation")
        self.send("\r")
        self.expect("does not match this setup")
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen)
        self.assertIn(".codex/orchestration/cli.py differs", plain.replace("\r\n", " "))
        self.assertFalse((self.base / "attest.count").exists(), "attest.py ran")
        self.send("\r")
        self.expect("Back to the main menu")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        self.assertFalse(any("cli.py" in line for line in self.py_args.read_text().splitlines()))
        self.assert_python_isolated()

    def test_commit_stages_updated_files_and_provider_routing(self) -> None:
        project, checks = self.make_project()
        self.drive_to_commit_menu(project, checks)
        self.send("\r")
        final = self.finish_after_install()
        # A project in a temporary folder is told why and gets no rule.
        self.assertIn("8. Commands: project in a temporary folder", final)
        self.assertIn("This project is in a temporary folder, so Codex sessions cannot", final)
        self.assertNotIn("to undo only the rule, delete:", final)
        self.assertFalse((self.home / ".codex").exists())
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

    def test_a_stale_rule_left_unchanged_keeps_its_undo_instruction(self) -> None:
        import hashlib
        project, checks = self.make_project(base=self.non_temporary_base())
        digest = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:12]
        self.drive_to_commit_menu(project, checks, {"FAKE_RULE": "stale"})
        self.send("\r")
        self.expect("Continue")
        self.send("\r")
        self.expect("Register now")
        self.send("\x1b[B\r")
        self.expect("Start the attestation")
        self.send("\x1b[B\r")
        self.expect("Not chosen")
        self.send("\r")
        self.expect("Leave it unchanged")
        self.assertIn("differs", self.screen)
        self.send("\r")
        self.expect("remove --target")
        self.settle()
        final = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "",
                       self.screen.split("\x1b[2J")[-1]).replace("\r", "")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        self.assertIn("8. Commands: exists, differs, left alone", final)
        self.assertIn("rule for this project is stale", final)
        self.assertNotIn("until the Codex commands rule exists", final)
        self.assertRegex(final, r"to undo only the rule, delete:\n\s+\S*/\.codex/rules/agent-orchestration-"
                         + digest + r"\.rules")
        self.assertNotIn("install-codex-rule", self.rule_log.read_text())  # left unchanged

    def test_verify_reports_the_codex_commands_rule_without_writing_it(self) -> None:
        import json
        project, checks = self.make_project(base=self.non_temporary_base())
        routing_in = self.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run(
            [TRUSTED_PY, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
             "--project-agent-routing", str(routing_in), "--operational-checks", str(checks), "--apply"],
            check=True, capture_output=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "install"], check=True, env=self.env)
        self.start(project, {"FAKE_RULE": "missing"})
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Verify the install only")
        self.send("\x1b[B\r")
        self.expect("Codex commands rule: missing")
        self.expect("Continue")
        self.send("\r")
        self.expect("remove --target")
        self.settle()
        final = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "",
                       self.screen.split("\x1b[2J")[-1]).replace("\r", "")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        self.assertIn("8. Commands: missing", final)
        self.assertIn("Codex sessions cannot reach the orchestrator", final)
        self.assertNotIn("to undo only the rule, delete:", final)
        self.assertFalse((self.home / ".codex").exists())

    def test_an_unsupported_codex_home_is_explained_with_its_fix(self) -> None:
        import json
        project, checks = self.make_project(base=self.non_temporary_base())
        routing_in = self.base / "par.json"
        routing_in.write_text(json.dumps({"version": 1, "roles": {}, "verifiers": {}}))
        subprocess.run(
            [TRUSTED_PY, str(ROOT / "scripts" / "install.py"), "install", "--target", str(project),
             "--project-agent-routing", str(routing_in), "--operational-checks", str(checks), "--apply"],
            check=True, capture_output=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "install"], check=True, env=self.env)
        # The real CLI, with HOME pointed at a test folder: not the account's home folder.
        self.start(project)
        self.expect("Everything looks good")
        self.send("\r")
        self.expect("Project path")
        self.send("\r")
        self.expect("Verify the install only")
        self.send("\x1b[B\r")
        self.expect("cannot be used with this Codex setup")
        self.expect("Continue")
        plain = re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", "", self.screen))
        self.assertIn("HOME is not this account's home folder", plain)
        self.assertIn("run setup with HOME set to your own home folder", plain)
        self.assertNotIn("may be older", plain)
        self.send("\r")
        self.expect("Back to the main menu")
        self.send("\r")
        self.assertEqual(self.finish(), 0)
        self.assertFalse((self.home / ".codex").exists())

    def test_an_untrusted_python_is_explained_and_nothing_is_written(self) -> None:
        project, checks = self.make_project(base=self.non_temporary_base())
        self.drive_to_commit_menu(project, checks, {"FAKE_UNTRUSTED": "1"})
        self.send("\r")
        final = self.finish_after_install(trusted=False, temporary=False)
        self.assertIn("8. Commands: not checked", final)
        self.assertIn("until the Codex commands rule exists", final)
        self.assertNotIn("to undo only the rule, delete:", final)
        self.assertFalse((self.home / ".codex").exists())

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
