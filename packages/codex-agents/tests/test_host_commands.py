from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT, TEMP_ROOT

import cli
from codex_agents import host_commands
from codex_agents.host_commands import (
    BINDING_RELATIVE,
    CLI_RELATIVE,
    PRIMARY_HOST_COMMANDS,
    PYTHON_FLAGS,
    CodexHomeUnsupported,
    HostCommandError,
    argv_prefix,
    bound_project_root,
    install_rule,
    interpreter_path,
    project_digest,
    remove_rule,
    render_rule,
    rule_file_name,
    rule_path,
    rule_status,
    runtime_copy_problem,
    runtime_copy_root,
    write_rule,
)
from codex_agents import session_guard
from codex_agents.runtime_files import ORCHESTRATION_PREFIX, RUNTIME_FILES, runtime_digest
from tests.test_session_guard import THREAD, SessionFiles
from codex_agents.learning_store import (
    COLLECTOR_RUNTIME_DIGEST,
    PACKAGE_VERSION,
    CollectorError,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    default_state_root,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.host_paths import darwin_directory, temp_roots, within

# Commands only the owner runs. None may run outside a sandboxed Codex primary.
OWNER_COMMANDS = {
    "learning-register",
    "learning-serve",
    "learning-record-billing-attestation",
    "learning-configure-owner-key",
    "learning-create-grant-request",
    "learning-attach-grant-signature",
    "learning-delete-project",
    "learning-backup-project",
    "learning-verify-backup",
    "learning-promote",
    "learning-rollback",
    "learning-start-canary",
    "learning-complete-canary",
    "learning-prepare-upgrade-reconciliation",
    "learning-commit-upgrade-reconciliation",
    "learning-record-verified-outcome",
    "learning-calibrate-grader",
    "learning-record-feedback",
    "learning-expire",
    "install-codex-rule",
    "remove-codex-rule",
    "codex-session-scan",
}
PYTHON = "/opt/example/bin/python3"
# The interpreter the rule names when tests run: the base one, never a venv's,
# fully resolved.
BASE_PYTHON = os.path.realpath(
    getattr(sys, "_base_executable", sys.executable)
    if sys.prefix != sys.base_prefix else sys.executable
)


def non_temporary_directory(test: unittest.TestCase) -> Path:
    """A fresh directory outside every temporary root (a rule refuses those), or skip."""

    candidates = [os.environ.get("XDG_RUNTIME_DIR") or ""]
    if sys.platform == "darwin":
        candidates.insert(0, darwin_directory("DARWIN_USER_CACHE_DIR") or "")
    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and not any(
            within(Path(os.path.realpath(candidate)), root) for root in temp_roots()
        ):
            directory = Path(os.path.realpath(
                tempfile.mkdtemp(prefix="codex-agents-test-", dir=candidate)
            ))
            test.addCleanup(shutil.rmtree, directory, True)
            return directory
    test.skipTest("no directory outside the temporary roots is available")


def stand_in_project(project: Path) -> Path:
    """A project with stand-ins for every runtime file and the manifest that names them."""

    orchestration = project / ".codex" / "orchestration"
    files = {}
    for relative in RUNTIME_FILES:
        path = orchestration / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# stand-in {relative}\n")
        files[ORCHESTRATION_PREFIX + relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (orchestration / "install-manifest.json").write_text(
        json.dumps({"format": 1, "version": "0.0.1", "files": files})
    )
    return project


def without_codex_home(environ: dict) -> dict:
    return {key: value for key, value in environ.items() if key != "CODEX_HOME"}


def install_package(project: Path) -> Path:
    """Install this package's files into `project` as the installer does, with its manifest."""

    files = {}
    for path in sorted(BUNDLE_ROOT.rglob("*")):
        relative = path.relative_to(BUNDLE_ROOT).as_posix()
        if (
            not path.is_file() or "__pycache__" in path.parts or path.name == ".DS_Store"
            or relative in {"VERSION", ".codex/orchestration/install-manifest.json"}
        ):
            continue
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (project / ".codex" / "orchestration" / "install-manifest.json").write_text(
        json.dumps({"format": 1, "version": PACKAGE_VERSION, "files": files}, indent=2)
    )
    return project


def expected_copy(codex_home: Path, project: Path) -> Path:
    orchestration = project / ".codex" / "orchestration"
    digest = runtime_digest(
        {relative: (orchestration / relative).read_bytes() for relative in RUNTIME_FILES}
    )
    return codex_home / "agent-orchestration" / project_digest(project.resolve()) / digest


def _python_trusted() -> bool:
    try:
        interpreter_path()
    except HostCommandError:
        return False
    return True


# CI toolcaches can be world-writable, and such a Python is rightly refused.
NEEDS_TRUSTED_PYTHON = unittest.skipUnless(
    _python_trusted(), "this machine's Python cannot be named by the Codex rule"
)


def cli_subcommands() -> set[str]:
    parser = cli.build_parser()
    actions = [
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    return set(actions[0].choices)


class AllowlistTests(unittest.TestCase):
    def test_allowlist_is_a_subset_of_real_subcommands(self) -> None:
        self.assertEqual(len(PRIMARY_HOST_COMMANDS), len(set(PRIMARY_HOST_COMMANDS)))
        self.assertLessEqual(set(PRIMARY_HOST_COMMANDS), cli_subcommands())

    def test_allowlist_excludes_every_owner_command(self) -> None:
        self.assertLessEqual(OWNER_COMMANDS, cli_subcommands())
        self.assertEqual(set(), set(PRIMARY_HOST_COMMANDS) & OWNER_COMMANDS)

    def test_host_command_itself_stays_sandboxed(self) -> None:
        self.assertIn("host-command", cli_subcommands())
        self.assertNotIn("host-command", PRIMARY_HOST_COMMANDS)


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        # These projects live in the system temp directory, which a real rule
        # refuses (test_temporary_projects_never_get_a_rule); here none counts.
        self.no_temp_roots = mock.patch.object(host_commands, "temp_roots", lambda: set())
        self.no_temp_roots.start()
        self.addCleanup(self.no_temp_roots.stop)
        self.project = self.make_project("project")
        # The rule and its copy live only in the account's real ~/.codex; here the
        # account database names this test's home.
        self.home = Path(os.path.realpath(self.base)) / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.environ = {"HOME": str(self.home)}
        account = mock.patch.object(host_commands, "account_home", lambda: self.home)
        account.start()
        self.addCleanup(account.stop)

    def make_project(self, name: str) -> Path:
        return stand_in_project(self.base / name)

    @staticmethod
    def pattern(text: str) -> list[str]:
        """The literal tokens of the rule's pattern, decoded like Starlark strings."""

        body = text.split("pattern = [", 1)[1].split("    ],\n    decision", 1)[0]
        return [
            ast.literal_eval(line.strip().rstrip(","))
            for line in body.splitlines()
            if line.strip().startswith('"')
        ]

    def test_rule_pins_absolute_paths_and_only_the_allowlist(self) -> None:
        text = render_rule(self.project, PYTHON, self.environ)
        project_cli_path = str(self.project / ".codex" / "orchestration" / "cli.py")
        # The rule runs the protected copy in Codex home, never the project's CLI.
        cli_path = str(expected_copy(self.codex_home, self.project) / CLI_RELATIVE)
        self.assertEqual(("-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"), PYTHON_FLAGS)
        self.assertEqual(
            [PYTHON, *PYTHON_FLAGS, cli_path, *PRIMARY_HOST_COMMANDS], self.pattern(text)
        )
        self.assertEqual(
            [PYTHON, *PYTHON_FLAGS, cli_path], argv_prefix(self.project, PYTHON, self.environ)
        )
        self.assertNotIn(f'"{project_cli_path}", "learning-status"', text.split("not_match")[0])
        self.assertIn(
            f'["{PYTHON}", "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", '
            f'"{project_cli_path}", "learning-status"',
            text.split("not_match")[1],
        )
        # A rule without the bytecode prefix (as written before) is not this one.
        self.assertIn(f'["{PYTHON}", "-I", "-B", "-S", "{cli_path}", "learning-status"',
                      text.split("not_match")[1])
        self.assertEqual(1, text.count("prefix_rule("))
        self.assertIn('    decision = "allow",\n', text)
        self.assertIn("justification = ", text)
        # Codex checks these examples when it loads the file.
        match = text.split("    match = [\n", 1)[1].split("    ],\n", 1)[0]
        not_match = text.split("    not_match = [\n", 1)[1].split("    ],\n", 1)[0]
        self.assertIn('"learning-status"', match)
        self.assertIn('"learning-register"', not_match)
        self.assertIn('["python3", ".codex/orchestration/cli.py", "learning-status"', not_match)
        self.assertIn(
            f'["{PYTHON}", "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", '
            '".codex/orchestration/cli.py"', not_match,
        )
        # The isolation flags are part of the match; the bare form stays sandboxed.
        self.assertIn(f'["{PYTHON}", "{cli_path}", "learning-status"', not_match)
        for owner in OWNER_COMMANDS - {"learning-register"}:
            self.assertNotIn(f'"{owner}"', text)
        header = text.split("prefix_rule(", 1)[0]
        self.assertTrue(all(line.startswith("#") for line in header.strip().splitlines()))
        self.assertIn("with the owner's consent", header)
        self.assertIn("To remove it, delete this file and the runtime copy", header)

    def test_rule_quotes_backslashes_and_double_quotes(self) -> None:
        project = self.make_project('odd "name" \\ dir')
        text = render_rule(project, PYTHON, self.environ)
        cli_path = str(expected_copy(self.codex_home, project) / CLI_RELATIVE)
        self.assertEqual(cli_path, self.pattern(text)[1 + len(PYTHON_FLAGS)])
        self.assertIn('odd \\"name\\" \\\\ dir', text)

    def test_line_breaks_and_control_characters_are_refused(self) -> None:
        for name in ("line\nbreak", "tab\there", "bell\x07", "rtl‮override", "sep x"):
            with self.subTest(name=repr(name)):
                project = self.make_project(name)
                with self.assertRaisesRegex(HostCommandError, "cannot hold"):
                    render_rule(project, PYTHON)
        for interpreter in ("/opt/py\nthon", "python3", "", "relative/python3"):
            with self.subTest(interpreter=repr(interpreter)):
                with self.assertRaises(HostCommandError):
                    render_rule(self.project, interpreter)

    @NEEDS_TRUSTED_PYTHON
    def test_the_interpreter_is_the_running_one_unresolved(self) -> None:
        self.assertEqual(BASE_PYTHON, argv_prefix(self.project, environ=self.environ)[0])
        self.assertIn(_quoted(BASE_PYTHON), render_rule(self.project, environ=self.environ))

    def test_temporary_projects_never_get_a_rule(self) -> None:
        self.no_temp_roots.stop()
        project = self.make_project("temporary")
        self.assertTrue(any(within(project, root) for root in temp_roots()))
        for attempt in (
            lambda: render_rule(project, PYTHON),
            lambda: argv_prefix(project, PYTHON),
            lambda: write_rule(project, self.environ, PYTHON),
            lambda: rule_status(project, self.environ, PYTHON),
            lambda: rule_path(project, self.environ),
        ):
            with self.assertRaisesRegex(HostCommandError, "temporary directory"):
                attempt()
        self.assertFalse(self.codex_home.exists())

    def test_project_cli_must_be_a_regular_installed_file(self) -> None:
        with self.assertRaisesRegex(HostCommandError, "not installed"):
            render_rule(self.base, PYTHON)
        linked = self.base / "linked"
        (linked / ".codex" / "orchestration").mkdir(parents=True)
        (linked / ".codex" / "orchestration" / "cli.py").symlink_to(
            self.project / ".codex" / "orchestration" / "cli.py"
        )
        with self.assertRaisesRegex(HostCommandError, "symlink"):
            render_rule(linked, PYTHON)
        borrowed = self.base / "borrowed"
        borrowed.mkdir()
        (borrowed / ".codex").symlink_to(self.project / ".codex")
        with self.assertRaisesRegex(HostCommandError, "symlink"):
            render_rule(borrowed, PYTHON)

    def test_file_name_is_a_digest_of_the_resolved_root(self) -> None:
        digest = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:12]
        self.assertEqual(f"agent-orchestration-{digest}.rules", rule_file_name(self.project))
        self.assertEqual(
            self.codex_home / "rules" / f"agent-orchestration-{digest}.rules",
            rule_path(self.project, self.environ),
        )
        with self.assertRaisesRegex(HostCommandError, "absolute"):
            rule_path(self.project, {**self.environ, "CODEX_HOME": "relative/codex"})
        # CODEX_HOME may name ~/.codex itself, never another folder.
        self.assertEqual(
            rule_path(self.project, self.environ),
            rule_path(self.project, {**self.environ, "CODEX_HOME": str(self.codex_home)}),
        )

    def test_write_is_private_atomic_and_idempotent(self) -> None:
        self.assertEqual("missing", rule_status(self.project, self.environ, PYTHON))
        self.assertEqual("created", write_rule(self.project, self.environ, PYTHON))
        path = rule_path(self.project, self.environ)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        self.assertEqual(render_rule(self.project, PYTHON, self.environ), path.read_text())
        # The rule names a runtime copy that only install_rule creates.
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
        before = path.stat()
        self.assertEqual("unchanged", write_rule(self.project, self.environ, PYTHON))
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_a_differing_rule_is_replaced_only_when_asked(self) -> None:
        write_rule(self.project, self.environ, PYTHON)
        path = rule_path(self.project, self.environ)
        other = "/opt/other/bin/python3"
        self.assertEqual("stale", rule_status(self.project, self.environ, other))
        self.assertEqual("differs", write_rule(self.project, self.environ, other))
        self.assertEqual(render_rule(self.project, PYTHON, self.environ), path.read_text())
        self.assertEqual(
            "replaced", write_rule(self.project, self.environ, other, replace=True)
        )
        self.assertEqual(render_rule(self.project, other, self.environ), path.read_text())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_a_rule_that_appears_after_the_read_is_never_overwritten(self) -> None:
        path = rule_path(self.project, self.environ)
        real_read = host_commands._read_rule

        def late_writer(directory: int, name: str):
            current = real_read(directory, name)
            if current is None:
                # Another setup publishes its rule between this read and the write.
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(render_rule(self.project, "/opt/other/bin/python3", self.environ))
            return current

        with mock.patch.object(host_commands, "_read_rule", side_effect=late_writer):
            self.assertEqual("differs", write_rule(self.project, self.environ, PYTHON))
        self.assertEqual(render_rule(self.project, "/opt/other/bin/python3", self.environ), path.read_text())
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_concurrent_setups_create_exactly_one_rule(self) -> None:
        interpreters = [f"/opt/python-{index}/bin/python3" for index in range(6)]
        barrier = threading.Barrier(len(interpreters))
        real_read = host_commands._read_rule
        outcomes: dict[str, str] = {}
        waited: set[int] = set()

        def synchronized(directory: int, name: str):
            current = real_read(directory, name)
            if threading.get_ident() not in waited:
                # Every setup reads "missing" before any of them writes.
                waited.add(threading.get_ident())
                barrier.wait(timeout=10)
            return current

        def setup(interpreter: str) -> None:
            try:
                outcomes[interpreter] = write_rule(self.project, self.environ, interpreter)
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                outcomes[interpreter] = repr(exc)

        with mock.patch.object(host_commands, "_read_rule", side_effect=synchronized):
            threads = [threading.Thread(target=setup, args=(item,)) for item in interpreters]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        created = [item for item, outcome in outcomes.items() if outcome == "created"]
        self.assertEqual(1, len(created), outcomes)
        self.assertEqual({"created", "differs"}, set(outcomes.values()))
        path = rule_path(self.project, self.environ)
        self.assertEqual(render_rule(self.project, created[0], self.environ), path.read_text())
        self.assertEqual([path.name], os.listdir(path.parent))

    def test_a_symlinked_rules_directory_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.codex_home.mkdir()
        (self.codex_home / "rules").symlink_to(elsewhere)
        with self.assertRaisesRegex(HostCommandError, "rules directory is a symlink"):
            write_rule(self.project, self.environ, PYTHON)
        self.assertEqual([], os.listdir(elsewhere))
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_a_symlinked_rule_file_is_refused_and_never_followed(self) -> None:
        target = self.base / "target.rules"
        target.write_text(render_rule(self.project, PYTHON, self.environ))
        rules = self.codex_home / "rules"
        rules.mkdir(parents=True)
        (rules / rule_file_name(self.project)).symlink_to(target)
        for replace in (False, True):
            with self.assertRaisesRegex(HostCommandError, "rule file is a symlink"):
                write_rule(self.project, self.environ, PYTHON, replace=replace)
        self.assertEqual(render_rule(self.project, PYTHON, self.environ), target.read_text())
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_a_non_regular_rule_file_is_refused(self) -> None:
        rules = self.codex_home / "rules"
        (rules / rule_file_name(self.project)).mkdir(parents=True)
        with self.assertRaisesRegex(HostCommandError, "not a regular file"):
            write_rule(self.project, self.environ, PYTHON, replace=True)
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    @unittest.skipUnless(shutil.which("codex"), "the Codex CLI is not installed")
    def test_codex_loads_the_rule_and_its_self_tests(self) -> None:
        project = self.make_project('proj "q" \\ s;x')
        rules = self.base / "check.rules"
        rules.write_text(render_rule(project, PYTHON, self.environ))
        prefix = argv_prefix(project, PYTHON, self.environ)

        def check(*argv: str) -> dict:
            completed = subprocess.run(
                ["codex", "execpolicy", "check", "--rules", str(rules), *argv],
                capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                env={**without_codex_home(os.environ), "HOME": str(self.home)},
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            return json.loads(completed.stdout)

        self.codex_home.mkdir()
        for command in PRIMARY_HOST_COMMANDS:
            self.assertEqual("allow", check(*prefix, command, "--x").get("decision"), command)
        for argv in (
            [*prefix, "learning-register"],
            [*prefix, "host-command"],
            ["python3", ".codex/orchestration/cli.py", "dispatch"],
            [PYTHON, *PYTHON_FLAGS, ".codex/orchestration/cli.py", "dispatch"],
            [PYTHON, prefix[-1], "dispatch"],
            [PYTHON, "-I", prefix[-1], "dispatch"],
            [PYTHON, "-I", "-B", "-S", prefix[-1], "dispatch"],
        ):
            self.assertEqual([], check(*argv)["matchedRules"], argv)


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ProtectedCopyTests(unittest.TestCase):
    """The rule runs an owner-only copy of the installed runtime, bound to its project."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        # These projects live in the system temp directory; here none counts.
        patcher = mock.patch.object(host_commands, "temp_roots", lambda: set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = install_package(self.base / "project")
        # The rule and its copy live only in the account's real ~/.codex; here the
        # account database names this test's home.
        self.home = Path(os.path.realpath(self.base)) / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.environ = {"HOME": str(self.home)}
        account = mock.patch.object(host_commands, "account_home", lambda: self.home)
        account.start()
        self.addCleanup(account.stop)

    def install(self, **kwargs) -> dict:
        return install_rule(self.project, self.environ, PYTHON, **kwargs)

    def test_the_copy_is_private_complete_and_bound(self) -> None:
        installed = self.install()
        copy = expected_copy(self.codex_home, self.project)
        self.assertEqual(
            {"codex_rule": "created", "runtime_copy": str(copy),
             "runtime_copy_status": "created"},
            installed,
        )
        # Exactly this package's runtime files, byte for byte: no skills, hook,
        # release data or anything else the project's manifest lists.
        copied = sorted(
            path.relative_to(copy).as_posix() for path in copy.rglob("*") if path.is_file()
        )
        self.assertEqual(
            sorted([
                *(ORCHESTRATION_PREFIX + relative for relative in RUNTIME_FILES),
                ".codex/orchestration/install-manifest.json", BINDING_RELATIVE,
            ]),
            copied,
        )
        for relative in RUNTIME_FILES:
            self.assertEqual(
                (self.project / ORCHESTRATION_PREFIX / relative).read_bytes(),
                (copy / ORCHESTRATION_PREFIX / relative).read_bytes(), relative,
            )
        copy_manifest = json.loads(
            (copy / ".codex" / "orchestration" / "install-manifest.json").read_text()
        )
        self.assertEqual(
            {ORCHESTRATION_PREFIX + relative for relative in RUNTIME_FILES},
            set(copy_manifest["files"]),
        )
        # The copy's name is the digest of every file it runs.
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, copy.name)
        for current, directories, files in os.walk(self.codex_home / "agent-orchestration"):
            for name in directories:
                self.assertEqual(0o700, stat.S_IMODE(os.lstat(Path(current, name)).st_mode))
            for name in files:
                self.assertEqual(0o600, stat.S_IMODE(os.lstat(Path(current, name)).st_mode))
        binding = json.loads((copy / BINDING_RELATIVE).read_text())
        self.assertEqual(
            {"format": 2, "project_root": str(self.project.resolve()),
             "runtime_digest": copy.name, "version": PACKAGE_VERSION,
             # Installed through the API without a clone, the project stands in.
             "clone_root": str(self.project.resolve())},
            binding,
        )
        # The copy serves its bound project, wherever it sits.
        self.assertEqual(self.project.resolve(), bound_project_root(copy / ".codex" / "orchestration"))
        self.assertIsNone(bound_project_root(self.project / ".codex" / "orchestration"))
        self.assertIsNone(runtime_copy_problem(copy, self.project.resolve()))
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        self.assertEqual(
            str(copy / CLI_RELATIVE), argv_prefix(self.project, PYTHON, self.environ)[-1]
        )
        # Running setup again changes nothing.
        before = (copy / CLI_RELATIVE).stat()
        self.assertEqual(
            ("unchanged", "unchanged"),
            tuple(self.install()[key] for key in ("codex_rule", "runtime_copy_status")),
        )
        self.assertEqual(before.st_ino, (copy / CLI_RELATIVE).stat().st_ino)

    def test_status_is_stale_unless_rule_copy_and_project_all_match(self) -> None:
        self.install()
        copy = expected_copy(self.codex_home, self.project)
        installed_cli = self.project / CLI_RELATIVE
        original = installed_cli.read_bytes()
        # The project's installed runtime changes (a parent-folder session could).
        installed_cli.write_bytes(original + b"# changed\n")
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
        self.assertIsNotNone(runtime_copy_problem(copy, self.project.resolve()))
        installed_cli.write_bytes(original)
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        # The copy changes.
        (copy / ".codex" / "orchestration" / "config" / "routing-policy.json").write_text("{}")
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
        # Setup rebuilds a copy that fails verification.
        self.assertEqual("replaced", self.install(replace=True)["runtime_copy_status"])
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        # Bytecode, or any other entry the manifest does not list, appears in the copy.
        for extra in (
            copy / ".codex" / "orchestration" / "codex_agents" / "__pycache__",
            copy / ".codex" / "orchestration" / "codex_agents" / "extra.py",
        ):
            with self.subTest(extra=extra.name):
                if extra.suffix:
                    extra.write_text("")
                else:
                    extra.mkdir(mode=0o700)
                self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
                self.assertIn(
                    "holds files it should not",
                    runtime_copy_problem(copy, self.project.resolve()),
                )
                host_commands.remove_tree_at(os.open(extra.parent, os.O_RDONLY), extra.name)
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        # The copy disappears.
        host_commands.remove_tree_at(os.open(copy.parent, os.O_RDONLY), copy.name)
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
        self.install()
        # Another installed version leaves the rule stale until setup runs again.
        manifest = self.project / ".codex" / "orchestration" / "install-manifest.json"
        data = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**data, "version": "0.0.1"}))
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))
        manifest.write_text(json.dumps(data))
        self.assertEqual("installed", rule_status(self.project, self.environ, PYTHON))
        # Setup keeps only the copy the rule names.
        (copy.parent / ("f" * 64)).mkdir(mode=0o700)
        self.assertEqual("unchanged", self.install()["codex_rule"])
        self.assertEqual([copy.name], os.listdir(copy.parent))

    def test_a_rule_naming_the_project_cli_is_stale(self) -> None:
        self.install()
        path = rule_path(self.project, self.environ)
        current = path.read_text()
        copy_cli = str(expected_copy(self.codex_home, self.project) / CLI_RELATIVE)
        # What earlier setups wrote: the project's own CLI as the rule target.
        path.write_text(current.replace(copy_cli, str(self.project.resolve() / CLI_RELATIVE)))
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_no_manifest_means_no_copy_and_no_rule(self) -> None:
        (self.project / ".codex" / "orchestration" / "install-manifest.json").unlink()
        for attempt in (
            lambda: install_rule(self.project, self.environ, PYTHON),
            lambda: render_rule(self.project, PYTHON, self.environ),
            lambda: argv_prefix(self.project, PYTHON, self.environ),
            lambda: runtime_copy_root(self.project, self.environ),
        ):
            with self.assertRaisesRegex(HostCommandError, "no install manifest"):
                attempt()
        self.assertFalse(self.codex_home.exists())
        self.assertEqual("missing", rule_status(self.project, self.environ, PYTHON))
        rules = self.codex_home / "rules"
        rules.mkdir(parents=True)
        (rules / rule_file_name(self.project)).write_text("# an older rule\n")
        self.assertEqual("stale", rule_status(self.project, self.environ, PYTHON))

    def test_a_runtime_that_is_not_this_package_is_refused(self) -> None:
        manifest = self.project / ".codex" / "orchestration" / "install-manifest.json"
        data = json.loads(manifest.read_text())
        module = ".codex/orchestration/codex_agents/routing.py"
        changed = (self.project / module).read_bytes() + b"# drift\n"
        (self.project / module).write_bytes(changed)
        # A consistent manifest is not enough: the runtime digest must be this package's.
        data["files"][module] = hashlib.sha256(changed).hexdigest()
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(HostCommandError, "recorded runtime"):
            self.install()
        self.assertFalse((self.codex_home / "rules").exists())
        # A manifest whose file digests do not hold is refused too.
        data["files"][module] = "0" * 64
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(HostCommandError, "recorded runtime"):
            self.install()

    def test_extra_files_in_the_project_runtime_refuse_the_copy(self) -> None:
        orchestration = self.project / ".codex" / "orchestration"
        manifest = orchestration / "install-manifest.json"
        original = manifest.read_text()
        # A parent-folder session plants a top-level module and lists it, with its
        # digest, in the project's manifest: the copy never takes the manifest's word.
        planted = orchestration / "socket.py"
        planted.write_text("raise SystemExit('planted module ran')\n")
        data = json.loads(original)
        data["files"][".codex/orchestration/socket.py"] = hashlib.sha256(
            planted.read_bytes()
        ).hexdigest()
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(HostCommandError, "files the package does not install"):
            self.install()
        self.assertFalse(self.codex_home.exists())
        planted.unlink()
        manifest.write_text(original)
        for extra in (
            orchestration / "codex_agents" / "__pycache__" / "routing.cpython-312.pyc",
            orchestration / "codex_agents" / "native.so",
            orchestration / "extra_package" / "__init__.py",
        ):
            with self.subTest(extra=extra.name):
                extra.parent.mkdir(exist_ok=True)
                extra.write_bytes(b"")
                with self.assertRaisesRegex(HostCommandError, "does not install"):
                    self.install()
                extra.unlink()
                if extra.parent.name in {"__pycache__", "extra_package"}:
                    extra.parent.rmdir()
        # Finder's metadata is not importable and is ignored; project files are allowed.
        (orchestration / ".DS_Store").write_bytes(b"")
        (orchestration / "provider-routing.json").write_text("{}")
        self.assertEqual("created", self.install()["codex_rule"])

    def test_only_the_real_home_codex_folder_is_supported(self) -> None:
        elsewhere = self.base / "codex-config"
        for environ in (
            {**self.environ, "CODEX_HOME": str(elsewhere)},
            {**self.environ, "CODEX_HOME": str(self.home / "codex")},
        ):
            with self.subTest(codex_home=environ["CODEX_HOME"]):
                with self.assertRaisesRegex(CodexHomeUnsupported, "only in ~/.codex"):
                    install_rule(self.project, environ, PYTHON)
                with self.assertRaisesRegex(CodexHomeUnsupported, "only in ~/.codex"):
                    rule_status(self.project, environ, PYTHON)
        # A symlinked ~/.codex may resolve into a folder a home session can write.
        elsewhere.mkdir(mode=0o700)
        self.codex_home.symlink_to(elsewhere)
        with self.assertRaisesRegex(CodexHomeUnsupported, "symbolic link"):
            install_rule(self.project, self.environ, PYTHON)
        self.assertEqual([], os.listdir(elsewhere))
        self.codex_home.unlink()
        # A home folder reached through a symlink is refused too.
        linked_home = self.base / "linked-home"
        linked_home.symlink_to(self.home)
        with self.assertRaisesRegex(CodexHomeUnsupported, "symbolic link"):
            install_rule(self.project, {"HOME": str(linked_home)}, PYTHON)
        # ~/.codex that others can change is refused.
        self.codex_home.mkdir(mode=0o700)
        self.codex_home.chmod(0o777)
        with self.assertRaisesRegex(CodexHomeUnsupported, "others can change"):
            install_rule(self.project, self.environ, PYTHON)
        self.codex_home.chmod(0o700)
        self.assertEqual("created", self.install()["codex_rule"])

    def test_a_home_other_than_the_accounts_is_refused(self) -> None:
        # HOME is the caller's; the account database decides where ~/.codex is.
        other = self.base / "other-home"
        other.mkdir()
        environ = {"HOME": str(os.path.realpath(other))}
        for attempt in (
            lambda: install_rule(self.project, environ, PYTHON),
            lambda: rule_status(self.project, environ, PYTHON),
        ):
            with self.assertRaisesRegex(CodexHomeUnsupported, "not this account's home folder"):
                attempt()
        self.assertEqual([], os.listdir(other))
        self.assertFalse(self.codex_home.exists())
        # Without HOME, the account's home is used.
        self.assertEqual("created", install_rule(self.project, {}, PYTHON)["codex_rule"])

    def test_copies_in_the_project_or_temporary_directories_are_refused(self) -> None:
        # A home folder inside the project would put ~/.codex there too.
        inside = Path(os.path.realpath(self.project)) / "home"
        inside.mkdir()
        with mock.patch.object(host_commands, "account_home", lambda: inside), \
                self.assertRaisesRegex(HostCommandError, "project or a temporary directory"):
            install_rule(self.project, {"HOME": str(inside)}, PYTHON)
        with mock.patch.object(host_commands, "temp_roots", lambda: {self.codex_home}):
            with self.assertRaisesRegex(HostCommandError, "project or a temporary directory"):
                install_rule(self.project, self.environ, PYTHON)
        self.assertFalse(self.codex_home.exists())

    def test_planted_symlinks_in_the_copy_path_refuse(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.codex_home.mkdir()
        (self.codex_home / "agent-orchestration").symlink_to(elsewhere)
        with self.assertRaisesRegex(HostCommandError, "unavailable"):
            self.install()
        self.assertEqual([], os.listdir(elsewhere))

    def test_a_misplaced_or_tampered_binding_refuses(self) -> None:
        self.install()
        copy = expected_copy(self.codex_home, self.project)
        moved = self.base / "moved" / copy.name
        shutil.copytree(copy, moved)
        with self.assertRaisesRegex(HostCommandError, "not where its binding says"):
            bound_project_root(moved / ".codex" / "orchestration")
        binding = copy / BINDING_RELATIVE
        data = json.loads(binding.read_text())
        binding.write_text(json.dumps({**data, "extra": 1}))
        with self.assertRaisesRegex(HostCommandError, "binding is invalid"):
            bound_project_root(copy / ".codex" / "orchestration")
        binding.write_text(json.dumps({**data, "runtime_digest": "0" * 64}))
        with self.assertRaisesRegex(HostCommandError, "not where its binding says"):
            bound_project_root(copy / ".codex" / "orchestration")

    def test_remove_deletes_the_rule_and_every_copy(self) -> None:
        self.install()
        copy = expected_copy(self.codex_home, self.project)
        stale = copy.parent / ("f" * 64)
        stale.mkdir(mode=0o700)
        removed = remove_rule(self.project, self.environ)
        self.assertEqual(
            [str(rule_path(self.project, self.environ)), str(copy.parent)], removed
        )
        self.assertFalse(rule_path(self.project, self.environ).exists())
        self.assertFalse(copy.parent.exists())
        self.assertEqual([], remove_rule(self.project, self.environ))
        # Removal works after the package files are gone too.
        self.install()
        shutil.rmtree(self.project / ".codex")
        self.assertEqual(2, len(remove_rule(self.project, self.environ)))

    def test_remove_cleans_up_for_a_temporary_project_too(self) -> None:
        # Uninstall removes whatever exists, even where no rule may be installed.
        with mock.patch.object(host_commands, "temp_roots", lambda: {self.base}):
            self.assertEqual([], remove_rule(self.project, self.environ))
            self.assertEqual(
                [], remove_rule(self.base / "never-installed", self.environ)
            )
            rules = self.codex_home / "rules"
            rules.mkdir(parents=True)
            name = f"agent-orchestration-{project_digest(self.project.resolve())}.rules"
            (rules / name).write_text("# an older rule\n")
            self.assertEqual(
                [str(rules / name)], remove_rule(self.project, self.environ)
            )
            self.assertEqual([], remove_rule(self.project, self.environ))


@NEEDS_TRUSTED_PYTHON
class HostCommandCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        outside = non_temporary_directory(self)
        self.project = install_package(outside / "project")
        # The copy may not sit in a temporary directory either.
        # The copy may not sit in a temporary directory either.
        self.home = outside / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.environ = {"HOME": str(self.home)}
        # Setup commands run in this process, where the account database can be
        # pointed at this test's home; the copy runs as its own process.
        account = mock.patch.object(host_commands, "account_home", lambda: self.home)
        account.start()
        self.addCleanup(account.stop)

    def run_cli(
        self, *arguments: str, expected: int = 0, cli_path: Path | None = None,
        own_project: bool = True, env: dict | None = None,
    ) -> dict:
        project = ["--project-root", str(self.project)] if own_project else []
        environment = {
            **without_codex_home(os.environ), **self.environ, **(env or {}),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
        }
        if cli_path is None:
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", *arguments, *project]), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    contextlib.redirect_stdout(stdout):
                code = cli.main()
            self.assertEqual(expected, code, stdout.getvalue())
            return json.loads(stdout.getvalue())
        completed = subprocess.run(
            [sys.executable, *PYTHON_FLAGS, str(cli_path), *arguments, *project],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env=environment,
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def test_host_command_reports_the_prefix_and_rule_without_writing(self) -> None:
        report = self.run_cli("host-command", expected=1)
        copy = expected_copy(self.codex_home, self.project)
        self.assertEqual(
            {"ready": False, "argv_prefix": [BASE_PYTHON, *PYTHON_FLAGS, str(copy / CLI_RELATIVE)],
             "codex_rule": "missing",
             "rule_file": str(rule_path(self.project, self.environ)),
             "runtime_copy": str(copy),
             # These tests' CODEX_THREAD_ID is no Codex session ID, so the guard says so.
             "session_guard": "CODEX_THREAD_ID is not a Codex session ID"},
            report,
        )
        self.assertFalse(self.codex_home.exists())

    def test_install_then_host_command_reports_installed(self) -> None:
        printed = self.run_cli("install-codex-rule", "--print")
        self.assertFalse(self.codex_home.exists())
        self.assertEqual(render_rule(self.project, environ=self.environ), printed["rule_text"])
        written = self.run_cli("install-codex-rule")
        self.assertEqual(
            ("created", "created", printed["runtime_copy"]),
            (written["codex_rule"], written["runtime_copy_status"], written["runtime_copy"]),
        )
        self.assertEqual(printed["rule_text"], Path(printed["rule_file"]).read_text())
        self.assertEqual("unchanged", self.run_cli("install-codex-rule")["codex_rule"])
        report = self.run_cli("host-command")
        self.assertEqual(("installed", True), (report["codex_rule"], report["ready"]))
        self.assertEqual(str(Path(written["runtime_copy"]) / CLI_RELATIVE), report["argv_prefix"][-1])
        # The binding records the installing clone (this source tree's repository).
        binding = json.loads((Path(written["runtime_copy"]) / BINDING_RELATIVE).read_text())
        self.assertEqual(str(Path(os.path.realpath(BUNDLE_ROOT)).parents[2]), binding["clone_root"])

    def run_copy(self, copy_cli: Path, *arguments: str, expected: int = 0,
                 workspace: Path | None = None) -> dict:
        """Run the copy's own cli.py in this process, as the rule would, for a session
        recorded (synthetically) under this test's account home."""

        SessionFiles(self.home).write(workspace or self.project)
        (self.base / "tmp").mkdir(exist_ok=True)
        environment = {
            **without_codex_home(os.environ), **self.environ, "CODEX_THREAD_ID": THREAD,
            "TMPDIR": str(self.base / "tmp"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
        }
        stdout = io.StringIO()
        saved_path = list(sys.path)
        with mock.patch.object(sys, "argv", [str(copy_cli), *arguments]), \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(session_guard, "account_home", lambda: self.home), \
                mock.patch.object(session_guard, "SLASH_TMP", str(self.base / "tmp")), \
                contextlib.redirect_stdout(stdout):
            try:
                runpy.run_path(str(copy_cli), run_name="__main__")
                code = 0
            except SystemExit as exit_:
                code = exit_.code
            finally:
                sys.path[:] = saved_path
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_the_copy_serves_only_its_project_and_only_while_it_matches(self) -> None:
        copy = Path(self.run_cli("install-codex-rule")["runtime_copy"])
        copy_cli = copy / CLI_RELATIVE
        # Every allowlisted command parses under the exact prefix.
        for command in PRIMARY_HOST_COMMANDS:
            with self.subTest(command=command):
                completed = subprocess.run(
                    [BASE_PYTHON, *PYTHON_FLAGS, str(copy_cli), command, "--help"],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
        # The own-root invariant uses the bound project, not the copy's location.
        report = self.run_copy(copy_cli, "validate", "--root", str(self.project), expected=1)
        self.assertIn("package_ready", report)
        other = self.base / "other"
        other.mkdir()
        refused = self.run_copy(
            copy_cli, "learning-status", "--project-root", str(other), expected=2
        )
        self.assertTrue(refused["error"].startswith("--project-root is refused"), refused)
        refused = self.run_copy(copy_cli, "validate", "--root", str(copy), expected=2)
        self.assertTrue(refused["error"].startswith("--root is refused"), refused)
        # A session that can write the home folder is refused before anything runs.
        refused = self.run_copy(
            copy_cli, "validate", "--root", str(self.project), expected=2, workspace=self.home
        )
        self.assertIn("can write to your home folder", refused["error"])
        # So is a command that did not come from a Codex session at all.
        completed = subprocess.run(
            [BASE_PYTHON, *PYTHON_FLAGS, str(copy_cli), "validate", "--root", str(self.project)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
            env={key: value for key, value in os.environ.items() if key != "CODEX_THREAD_ID"},
        )
        self.assertEqual(2, completed.returncode, completed.stdout)
        self.assertIn("CODEX_THREAD_ID is not set", json.loads(completed.stdout)["error"])
        # Owner and setup commands never run from the copy.
        for command in ("host-command", "install-codex-rule", "learning-register"):
            refused = self.run_cli(command, cli_path=copy_cli, expected=2)
            self.assertEqual(
                "a protected runtime copy runs only the primary-host commands", refused["error"]
            )
        # A change to the project's installed runtime stops the copy.
        policy = self.project / ".codex" / "orchestration" / "config" / "routing-policy.json"
        original = policy.read_bytes()
        policy.write_bytes(original + b"\n")
        refused = self.run_copy(copy_cli, "validate", "--root", str(self.project), expected=2)
        self.assertIn("differs from its protected copy", refused["error"])
        self.assertEqual("stale", self.run_cli("host-command", expected=1)["codex_rule"])
        policy.write_bytes(original)
        self.assertEqual("installed", self.run_cli("host-command")["codex_rule"])
        self.assertEqual([], list(copy.rglob("__pycache__")))

    def test_a_differing_rule_needs_replace(self) -> None:
        write_rule(self.project, self.environ, "/opt/other/bin/python3")
        self.assertEqual("stale", self.run_cli("host-command", expected=1)["codex_rule"])
        refused = self.run_cli("install-codex-rule", expected=1)
        self.assertEqual(("differs", False), (refused["codex_rule"], refused["ready"]))
        self.assertEqual(
            "replaced", self.run_cli("install-codex-rule", "--replace")["codex_rule"]
        )
        self.assertEqual("installed", self.run_cli("host-command")["codex_rule"])

    def test_remove_codex_rule_reports_what_it_removed(self) -> None:
        installed = self.run_cli("install-codex-rule")
        removed = self.run_cli("remove-codex-rule")
        self.assertEqual(
            {"ready": True,
             "removed": [installed["rule_file"], str(Path(installed["runtime_copy"]).parent)]},
            removed,
        )
        self.assertEqual({"ready": True, "removed": []}, self.run_cli("remove-codex-rule"))
        self.assertEqual("missing", self.run_cli("host-command", expected=1)["codex_rule"])
        # A project in a temporary folder never gets a rule, but removal still answers.
        temporary_project = self.base / "temporary-project"
        temporary_project.mkdir()
        self.assertEqual(
            {"ready": True, "removed": []},
            self.run_cli(
                "remove-codex-rule", "--project-root", str(temporary_project),
                own_project=False,
            ),
        )

    def test_a_module_planted_in_the_copy_never_imports(self) -> None:
        copy = Path(self.run_cli("install-codex-rule")["runtime_copy"])
        marker = self.base / "planted-ran"
        planted = copy / ".codex" / "orchestration" / "socket.py"
        planted.write_text(f"open({str(marker)!r}, 'w').close()\n")
        refused = self.run_cli(
            "validate", "--root", str(self.project), cli_path=copy / CLI_RELATIVE,
            expected=2, own_project=False,
        )
        self.assertIn("holds files it should not", refused["error"])
        self.assertFalse(marker.exists())
        self.assertEqual("stale", self.run_cli("host-command", expected=1)["codex_rule"])

    def test_host_command_reports_whether_the_session_passes_the_guard(self) -> None:
        self.run_cli("install-codex-rule")
        sessions = SessionFiles(self.home)
        (self.base / "tmp").mkdir()
        env = {"CODEX_THREAD_ID": THREAD, "TMPDIR": str(self.base / "tmp")}
        with mock.patch.object(session_guard, "account_home", lambda: self.home), \
                mock.patch.object(session_guard, "SLASH_TMP", str(self.base / "tmp")):
            sessions.write(self.project)
            self.assertEqual("ok", self.run_cli("host-command", env=env)["session_guard"])
            sessions.write(self.home)
            report = self.run_cli("host-command", env=env)
            self.assertIn("can write to your home folder", report["session_guard"])
            # The clone recorded at install is protected too.
            sessions.write(Path(os.path.realpath(BUNDLE_ROOT)).parents[2])
            report = self.run_cli("host-command", env=env)
            self.assertIn("orchestrator's setup folder", report["session_guard"])

    def test_host_command_reports_an_overridden_home(self) -> None:
        other = self.base / "other-home"
        other.mkdir()
        report = self.run_cli(
            "host-command", expected=1, env={"HOME": os.path.realpath(other)}
        )
        self.assertEqual("unsupported", report["codex_rule"])
        self.assertIn("not this account's home folder", report["reason"])

    def test_host_command_reports_an_unsupported_codex_home(self) -> None:
        elsewhere = self.base / "codex-config"
        report = self.run_cli(
            "host-command", expected=1, env={"CODEX_HOME": str(elsewhere)}
        )
        self.assertEqual(("unsupported", False, None), (
            report["codex_rule"], report["ready"], report["argv_prefix"]
        ))
        self.assertIn("only in ~/.codex", report["reason"])
        refused = self.run_cli(
            "install-codex-rule", expected=2, env={"CODEX_HOME": str(elsewhere)}
        )
        self.assertIn("only in ~/.codex", refused["error"])
        self.assertFalse(elsewhere.exists())
        elsewhere.mkdir(mode=0o700)
        self.codex_home.symlink_to(elsewhere)
        report = self.run_cli("host-command", expected=1)
        self.assertEqual("unsupported", report["codex_rule"])
        self.assertIn("symbolic link", report["reason"])

    def test_refusals_are_fixed_messages_without_paths(self) -> None:
        self.codex_home.mkdir()
        (self.codex_home / "rules").symlink_to(self.base)
        report = self.run_cli("install-codex-rule", expected=2)
        self.assertEqual(
            {"ready": False, "error": "Codex rules directory is a symlink"}, report
        )
        (self.project / ".codex" / "orchestration" / "install-manifest.json").unlink()
        report = self.run_cli("install-codex-rule", expected=2)
        self.assertEqual(
            {"ready": False,
             "error": "the project has no install manifest; install the package first"},
            report,
        )


class SandboxHintTests(unittest.TestCase):
    """A primary that cannot reach the collector keeps its error and gets a hint."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state_root = base / "state"
        LearningState(self.state_root)
        self.project = base / "project"
        self.project.mkdir()

    def run_main(self, command: str, *arguments: str, env: dict | None = None) -> dict:
        stdout = io.StringIO()
        argv = ["cli.py", command, "--state-root", str(self.state_root),
                "--project-root", str(self.project), *arguments]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(stdout):
            cli.main()
        return json.loads(stdout.getvalue())

    def blocked(self, *args, **kwargs):
        raise PermissionError("blocked by the sandbox")

    def test_a_blocked_lifecycle_command_gets_the_hint(self) -> None:
        with mock.patch.object(cli.CollectorMaintenance, "acquire", self.blocked):
            report = self.run_main("learning-open-orchestration")
        self.assertEqual("filesystem operation failed", report["error"])
        self.assertFalse(report["ready"])
        self.assertIn("host-command", report["hint"])
        self.assertIn("argv_prefix", report["hint"])
        self.assertIn("./setup.sh", report["hint"])

    def test_no_hint_without_codex_markers_or_for_owner_commands(self) -> None:
        unmarked = {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}
        with mock.patch.object(cli.CollectorMaintenance, "acquire", self.blocked):
            self.assertEqual(
                {"ready": False, "error": "filesystem operation failed"},
                self.run_main("learning-open-orchestration", env=unmarked),
            )
            self.assertEqual(
                {"ready": False, "error": "filesystem operation failed"},
                self.run_main("learning-expire"),
            )

    def test_learning_status_hint_follows_reachability(self) -> None:
        with mock.patch.object(cli, "ensure_collector", self.blocked):
            report = self.run_main("learning-status")
        self.assertEqual("filesystem operation failed", report["error"])
        self.assertIn("hint", report)

        def unhealthy(*args, **kwargs):
            raise CollectorError("collector did not become healthy")

        with mock.patch.object(cli, "ensure_collector", unhealthy):
            report = self.run_main("learning-status")
        self.assertEqual("collector or project registration is unavailable", report["error"])
        self.assertIn("hint", report)

        def rejected(*args, **kwargs):
            raise CollectorError("collector rejected request: x", code="project_unregistered")

        with mock.patch.object(cli, "ensure_collector", rejected):
            report = self.run_main("learning-status")
        self.assertNotIn("hint", report)


class OwnRootTests(unittest.TestCase):
    """An installed CLI pins its primary-host commands to its project and state.

    The owner's Codex rule may run them outside the sandbox with model-chosen
    arguments, so another project, state root or policy is refused first.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        shutil.copytree(
            BUNDLE_ROOT, self.project,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        (self.project / ".codex" / "orchestration" / "install-manifest.json").write_text(
            json.dumps({"format": 1, "version": PACKAGE_VERSION, "files": {}})
        )
        self.cli = self.project / ".codex" / "orchestration" / "cli.py"
        # The default learning state follows HOME (XDG_STATE_HOME on Linux), which
        # a model cannot set for a command Codex runs outside the sandbox.
        self.home = self.base / "home"
        self.env = {
            **os.environ, "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with mock.patch.dict(os.environ, self.env):
            self.default_state = default_state_root()
        self.other_state = self.base / "other-state"
        # A model-built repository whose own configuration runs a command on any
        # index refresh; it must never see a git call from an installed CLI.
        self.other = self.base / "other"
        self.marker = self.base / "hostile-git-ran"
        hook = self.base / "fsmonitor.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{self.marker}"\nexit 1\n')
        hook.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(self.other)], check=True)
        (self.other / "file").write_text("x\n")
        subprocess.run(
            ["git", "-C", str(self.other), "config", "core.fsmonitor", str(hook)], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.other), "status", "--porcelain"],
            check=True, capture_output=True,
        )
        self.assertTrue(self.marker.exists(), "the hostile repository is not armed")
        self.marker.unlink()

    def run_cli(self, *arguments: str, cwd: Path | None = None) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(self.cli), *arguments],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env=self.env, cwd=cwd,
        )
        return completed.returncode, json.loads(completed.stdout)

    def assert_refused(self, option: str, *arguments: str) -> None:
        code, report = self.run_cli(*arguments)
        self.assertEqual(2, code, arguments)
        self.assertFalse(report["ready"])
        self.assertTrue(report["error"].startswith(f"{option} is refused"), (arguments, report))
        self.assertEqual({"ready", "error"}, set(report))

    def test_other_roots_state_and_policy_are_refused_before_any_work(self) -> None:
        own, other, state = str(self.project), str(self.other), str(self.other_state)
        context = ("--assignment", "a.json", "--task", "t.json", "--parent-grant", "g.json")
        lifecycle = {
            "dispatch": (*context, "--route", "r.json", "--run-handle", "h"),
            "provider-observe": ("--provider", "claude", "--billing-probe"),
            "learning-open-host-run": (
                *context, "--route", "r.json", "--orchestration-handle", "o",
            ),
            "learning-run-operational-checks": (
                "--host-outcome-digest", "0" * 64, "--verifier-host-outcome-digest", "1" * 64,
            ),
            "learning-status": (),
            "learning-open-orchestration": (),
            "learning-route": ("--task", "t.json"),
        }
        for command, extra in lifecycle.items():
            with self.subTest(command=command):
                self.assert_refused("--project-root", command, *extra, "--project-root", other)
                self.assert_refused(
                    "--state-root", command, *extra, "--project-root", own, "--state-root", state,
                )
                # argparse keeps the last value; that is the one checked.
                self.assert_refused(
                    "--project-root", command, *extra,
                    "--project-root", own, "--project-root", other,
                )
                # Abbreviations reach the same option.
                self.assert_refused("--project-root", command, *extra, "--proj", other)
        self.assert_refused("--root", "validate", "--root", other)
        self.assert_refused("--root", "inventory", "--root", other)
        self.assert_refused("--root", "route", "--task", "t.json", "--root", other)
        self.assert_refused("--policy", "route", "--task", "t.json", "--policy", "p.json")
        self.assert_refused("--policy", "inventory", "--policy", "p.json")
        for command in ("validate-assignment", "result-preflight", "validate-result"):
            with self.subTest(command=command):
                result = ("--result", "r.json") if command == "validate-result" else ()
                self.assert_refused("--policy", command, *context, *result, "--pol", "p.json")
                self.assert_refused(
                    "--project-root", command, *context, *result, "--project-root", other,
                )
                self.assert_refused(
                    "--state-root", command, *context, *result, "--state-root", state,
                )
        self.assertFalse(self.marker.exists(), "an installed CLI ran git in another project")
        self.assertFalse(self.other_state.exists())

    def test_own_project_and_default_state_still_work(self) -> None:
        state = LearningState(self.default_state)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        ledger.register_project(self.project, now_ms=1)
        ledger.close()
        link = self.base / "link"
        link.symlink_to(self.project)
        for arguments, cwd in (
            (("--project-root", str(self.project)), None),
            (("--project-root", str(link), "--state-root", str(self.default_state)), None),
            (("--project-root", "."), self.project),
        ):
            with self.subTest(arguments=arguments):
                code, report = self.run_cli("learning-open-orchestration", *arguments, cwd=cwd)
                self.assertEqual(0, code, report)
                self.assertTrue(report["ready"])
        for arguments in ((), ("--root", str(self.project))):
            code, report = self.run_cli("validate", *arguments)
            self.assertIn("package_ready", report)
        # host-command is not pinned (it needs no state), and reads only.
        code, report = self.run_cli("host-command", "--project-root", str(self.project))
        # This project is in the system temp directory, so it never gets a rule.
        self.assertIn("temporary directory", report["error"])

    @NEEDS_TRUSTED_PYTHON
    def test_every_allowlisted_command_runs_under_the_exact_prefix(self) -> None:
        # The project is in the system temp directory, which a rule refuses, so the
        # prefix is assembled here exactly as argv_prefix would.
        prefix = [interpreter_path(), *PYTHON_FLAGS, str(self.cli)]
        self.assertEqual(BASE_PYTHON, prefix[0])
        for command in PRIMARY_HOST_COMMANDS:
            with self.subTest(command=command):
                completed = subprocess.run(
                    [*prefix, command, "--help"], capture_output=True, text=True,
                    env=self.env, stdin=subprocess.DEVNULL, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
        state = LearningState(self.default_state)
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        ledger.register_project(self.project, now_ms=1)
        ledger.close()
        for arguments in (
            ("learning-open-orchestration", "--project-root", str(self.project)),
            ("validate",),
            ("inventory",),
        ):
            completed = subprocess.run(
                [*prefix, *arguments], capture_output=True, text=True, env=self.env,
                stdin=subprocess.DEVNULL, check=False,
            )
            self.assertIn('"ready"', completed.stdout, completed.stderr)
        self.assertEqual([], list(self.project.rglob("__pycache__")))

    def test_source_tree_cli_keeps_its_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), "learning-status",
             "--state-root", str(self.other_state), "--project-root", str(self.project)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env=self.env,
        )
        self.assertNotIn("is refused", completed.stdout)


if __name__ == "__main__":
    unittest.main()
