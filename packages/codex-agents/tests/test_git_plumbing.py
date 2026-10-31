from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import BUNDLE_ROOT, TEMP_ROOT
from tests.learning_support import assignment, routing_fields

from codex_agents import git_plumbing
from codex_agents.bridge import BridgeRefused, export_committed_paths
from codex_agents.contracts import ContractError
from codex_agents.host_commands import PYTHON_FLAGS, argv_prefix
from codex_agents.learning_engine import (
    _committed_and_clean,
    load_operational_check_configuration,
)
from codex_agents.learning_store import PACKAGE_VERSION

GIT_SETUP = {
    **os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], env=GIT_SETUP, capture_output=True,
        stdin=subprocess.DEVNULL, check=check, timeout=60,
    )


def write_checks(root: Path) -> None:
    script = root / "qa-check"
    script.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    script.chmod(0o700)
    check = {
        "check_id": "tests", "driver": "python-wrapper", "argv": ["./qa-check"],
        "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "interpreter_sha256": hashlib.sha256(
            Path(sys.executable).resolve().read_bytes()
        ).hexdigest(),
        "runtime_tree": None, "timeout_seconds": 5,
    }
    (root / ".codex" / "orchestration" / "operational-checks.json").write_text(json.dumps({
        "version": 1, "deterministic_checks": [check],
        "security_checks": [{**check, "check_id": "security"}],
    }))


class HostileOwnRepositoryTests(unittest.TestCase):
    """The installed project's own git configuration names programs; none may run.

    Trust checks reached from the allowlisted commands use plumbing only, so an
    fsmonitor, clean and smudge filters, textconv, an external diff, a pager and
    hooks configured in the repository never execute, while a modified file is
    still detected.
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
        orchestration = self.project / ".codex" / "orchestration"
        (orchestration / "install-manifest.json").write_text(
            json.dumps({"format": 1, "version": PACKAGE_VERSION, "files": {}})
        )
        (orchestration / "project-agent-routing.json").write_text(
            json.dumps({"version": 1, "roles": {}, "verifiers": {}})
        )
        write_checks(self.project)
        (self.project / "docs").mkdir()
        (self.project / "docs" / "readme.txt").write_text("hello\n")
        (self.project / ".gitattributes").write_text("* filter=hostile diff=hostile\n")
        git(self.project, "init", "-q")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "install")
        # Arm the repository's own configuration after the commit.
        self.marker = self.base / "hostile-ran"
        hostile = self.base / "hostile.sh"
        hostile.write_text(f'#!/bin/sh\necho "$0 $*" >> "{self.marker}"\ncat\nexit 0\n')
        hostile.chmod(0o755)
        for key, value in (
            ("core.fsmonitor", str(hostile)),
            ("filter.hostile.clean", str(hostile)),
            ("filter.hostile.smudge", str(hostile)),
            ("diff.external", str(hostile)),
            ("diff.hostile.textconv", str(hostile)),
            ("core.pager", str(hostile)),
        ):
            git(self.project, "config", key, value)
        hooks = self.project / ".git" / "hooks"
        for name in ("post-index-change", "reference-transaction", "post-checkout"):
            (hooks / name).write_text(f'#!/bin/sh\necho hook >> "{self.marker}"\n')
            (hooks / name).chmod(0o755)
        # The traps are live for ordinary git use.
        (self.project / "docs" / "readme.txt").write_text("changed\n")
        git(self.project, "status", "--porcelain", check=False)
        git(self.project, "diff", check=False)
        (self.project / "docs" / "readme.txt").write_text("hello\n")
        self.assertTrue(self.marker.exists(), "the hostile repository is not armed")
        self.marker.unlink()
        self.executable = git_plumbing.git_executable()
        self.home = self.base / "home"
        self.env = {
            **os.environ, "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
        }

    def assert_never_ran(self) -> None:
        self.assertFalse(
            self.marker.exists(),
            self.marker.read_text() if self.marker.exists() else "",
        )

    def run_prefix(self, *arguments: str) -> subprocess.CompletedProcess:
        """Run the installed CLI the way the rule does (this Python, its isolation flags)."""

        cli = argv_prefix(self.project, sys.executable)[-1]
        return subprocess.run(
            [sys.executable, *PYTHON_FLAGS, cli, *arguments], capture_output=True, text=True,
            env=self.env, stdin=subprocess.DEVNULL, check=False,
        )

    def test_trust_paths_never_run_repository_helpers(self) -> None:
        task = self.base / "task.json"
        task.write_text(json.dumps({
            "id": "task-1", "objective": "Read one fixture", "family": "research",
            "complexity": "low", "risk": "low", **routing_fields(),
            "meaningful": True, "deterministic_validation": True,
            "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0,
            "failure_events": [],
        }))
        assigned = self.base / "assignment.json"
        assigned.write_text(json.dumps(assignment()))
        grant = self.base / "grant.json"
        grant.write_text(json.dumps({"version": 1}))
        result = self.base / "result.json"
        result.write_text("{}")
        context = ("--task", str(task), "--assignment", str(assigned),
                   "--parent-grant", str(grant), "--project-root", str(self.project))
        for arguments in (
            ("validate",),
            ("inventory",),
            ("route", "--task", str(task)),
            ("learning-route", "--task", str(task), "--project-root", str(self.project)),
            ("validate-assignment", *context),
            ("result-preflight", *context),
            ("validate-result", *context, "--result", str(result)),
        ):
            with self.subTest(command=arguments[0]):
                completed = self.run_prefix(*arguments)
                self.assertTrue(completed.stdout.startswith("{"), completed.stderr)
                self.assert_never_ran()
        subcommands: list[str] = []
        real_run = subprocess.run

        def recording(argv, **options):
            if argv[0] == self.executable:
                subcommands.append(argv[argv.index("-C") + 2])
            return real_run(argv, **options)

        with mock.patch.object(git_plumbing.subprocess, "run", recording):
            load_operational_check_configuration(self.project)
            export = self.base / "export"
            export.mkdir()
            self.assertEqual(1, export_committed_paths(self.project, ["docs"], export))
        self.assertEqual("hello\n", (export / "docs" / "readme.txt").read_text())
        self.assertLessEqual(set(subcommands), {"rev-parse", "ls-tree", "cat-file"})
        self.assert_never_ran()

    def test_modified_files_are_still_detected(self) -> None:
        config = self.project / ".codex" / "orchestration" / "operational-checks.json"
        script = self.project / "qa-check"
        original_config, original_script = config.read_bytes(), script.read_bytes()
        config.write_bytes(original_config + b" ")
        with self.assertRaisesRegex(ContractError, "not committed and clean"):
            load_operational_check_configuration(self.project)
        config.write_bytes(original_config)
        load_operational_check_configuration(self.project)
        script.write_bytes(original_script + b"# changed\n")
        with self.assertRaises(ContractError):
            load_operational_check_configuration(self.project)
        script.write_bytes(original_script)
        load_operational_check_configuration(self.project)
        # The executable comparison itself: bytes, mode and presence in the commit.
        commit = git(self.project, "rev-parse", "HEAD").stdout.decode().strip()

        def clean(relative: str = "qa-check") -> bool:
            return _committed_and_clean(self.executable, self.project, commit, relative)

        self.assertTrue(clean())
        script.write_bytes(original_script + b"# changed\n")
        self.assertFalse(clean())
        script.write_bytes(original_script)
        script.chmod(0o600)
        self.assertFalse(clean())
        script.chmod(0o700)
        self.assertTrue(clean())
        (self.project / "untracked").write_text("x\n")
        self.assertFalse(clean("untracked"))
        self.assertFalse(clean("docs"))
        link = self.project / "linked-check"
        link.symlink_to(script)
        self.assertFalse(clean("linked-check"))
        self.assert_never_ran()


class RepositoryLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.executable = git_plumbing.git_executable()

    def commit(self, root: Path) -> None:
        (root / "file").write_text("x\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")

    def test_a_plain_repository_is_trusted(self) -> None:
        root = self.base / "plain"
        git(self.base, "init", "-q", str(root))
        self.assertIsNone(git_plumbing.repository_layout_problem(self.executable, root))

    def test_a_git_directory_inside_the_working_tree_is_refused(self) -> None:
        root = self.base / "separated"
        root.mkdir()
        git(root, "init", "-q", "--separate-git-dir", str(root / "inner-git"))
        self.assertEqual(
            "the project repository lies inside its working tree",
            git_plumbing.repository_layout_problem(self.executable, root),
        )
        write_dir = root / ".codex" / "orchestration"
        write_dir.mkdir(parents=True)
        write_checks(root)
        self.commit(root)
        with self.assertRaisesRegex(ContractError, "not committed and clean"):
            load_operational_check_configuration(root)
        with self.assertRaisesRegex(BridgeRefused, "cannot be trusted"):
            export_committed_paths(root, ["file"], self.base / "out")

    def test_a_worktree_whose_shared_repository_is_temporary_is_refused(self) -> None:
        main = self.base / "writable" / "main"
        main.mkdir(parents=True)
        git(main, "init", "-q")
        (main / ".codex" / "orchestration").mkdir(parents=True)
        write_checks(main)
        self.commit(main)
        worktree = self.base / "owner" / "worktree"
        worktree.parent.mkdir()
        git(main, "worktree", "add", "-q", str(worktree))
        marker = self.base / "fsmonitor-ran"
        hook = self.base / "fsmonitor.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o755)
        git(main, "config", "core.fsmonitor", str(hook))
        # Only the shared repository's directory counts as temporary here.
        temporary = {Path(os.path.realpath(self.base / "writable"))}
        with mock.patch.object(git_plumbing, "temp_roots", return_value=temporary):
            self.assertEqual(
                "the project repository lies in a temporary directory",
                git_plumbing.repository_layout_problem(self.executable, worktree),
            )
            with self.assertRaisesRegex(ContractError, "not committed and clean"):
                load_operational_check_configuration(worktree)
            # A project inside the same temporary directory is wholly writable
            # anyway, so its own layout is not refused for that reason.
            self.assertIsNone(git_plumbing.repository_layout_problem(self.executable, main))
        self.assertFalse(marker.exists())

    def test_every_call_disables_repository_helpers(self) -> None:
        options = " ".join(git_plumbing.GIT_OPTIONS)
        for setting in ("core.fsmonitor=false", "core.hooksPath=/dev/null"):
            self.assertIn(setting, options)
        for flag in ("--no-pager", "--no-optional-locks"):
            self.assertIn(flag, git_plumbing.GIT_OPTIONS)
        self.assertEqual("1", git_plumbing.GIT_ENVIRONMENT["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("/dev/null", git_plumbing.GIT_ENVIRONMENT["GIT_CONFIG_GLOBAL"])
        self.assertEqual("0", git_plumbing.GIT_ENVIRONMENT["GIT_TERMINAL_PROMPT"])
        source = "".join(
            path.read_text() for path in sorted(
                (BUNDLE_ROOT / ".codex" / "orchestration" / "codex_agents").glob("*.py")
            )
        )
        # Every git call in the runtime is plumbing that reads refs and objects.
        calls = re.findall(r"(?:run_git\(|_git\(|\brun\(\[)[^)]*?\"([a-z-]+)\"", source)
        self.assertTrue(calls)
        self.assertLessEqual(set(calls), {"rev-parse", "ls-tree", "cat-file"})
        self.assertNotIn('"git", "-C"', source)


if __name__ == "__main__":
    unittest.main()
