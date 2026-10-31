from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_bridge_writes as writes
from tests.test_write_verification import WriteVerificationTests

from codex_agents import bridge, integration
from codex_agents.hosts import HostClientMismatch
from codex_agents.learning_contracts import (
    ContractError,
    content_digest,
    validate_learning_record,
)
from codex_agents.learning_engine import operational_sandbox_preflight
from codex_agents.learning_store import LedgerError

SANDBOX = unittest.skipUnless(
    sys.platform == "darwin" and operational_sandbox_preflight(),
    "the operational-check sandbox (sandbox-exec) is unavailable",
)
HOOKS = (
    "pre-commit", "commit-msg", "post-commit", "post-checkout", "post-merge",
    "reference-transaction", "post-index-change", "pre-merge-commit", "pre-auto-gc",
)


def git_out(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@unittest.skipUnless(writes.GIT_NO_LAZY_FETCH, "the contained workspace needs Git 2.44 or later")
class WriteIntegrationTests(WriteVerificationTests):
    """An accepted Claude write patch, integrated into the primary as one fast-forward."""

    def setUp(self) -> None:
        super().setUp()
        self.integration_parent = Path(self.temporary.name) / "integration"
        self.integration_parent.mkdir()
        patcher = mock.patch.object(integration, "INTEGRATION_PARENT", str(self.integration_parent))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hook_marker = Path(self.temporary.name) / "hook-ran"
        self.check_roots: list[Path] = []

    # --- helpers -------------------------------------------------------------

    def accepted(self, *, verified: bool = True) -> dict:
        work = self.written(
            {"write": "src/app.py", "content": "print('changed')\n"},
            {"write": "src/new.py", "content": "new\n"},
        )
        binding = self.binding(work)
        if verified:
            self.verify(work, self.verdict(binding["patch_digest"]))
            self.assertEqual("accepted", self.verification_state(work))
        return binding

    def install_hooks(self) -> None:
        """Hooks in the shared hooks directory, which both checkouts would run."""

        hooks = Path(git_out(self.project, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        hooks = hooks / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in HOOKS:
            path = hooks / name
            path.write_text(f"#!/bin/sh\necho {name} >> '{self.hook_marker}'\n")
            path.chmod(0o755)

    def report(self, *, passed: bool = True) -> dict:
        check = {"check_id": "unit", "passed": passed, "evidence_digest": "a" * 64}
        return {
            "deterministic_checks": [check], "security_checks": [],
            "observed_at_ms": time.time_ns() // 1_000_000,
        }

    def checks(self, *, passed: bool = True, also=None):
        def run(root: Path) -> dict:
            self.check_roots.append(root)
            # The checks see the patched source commit, outside the repository.
            self.assertEqual("print('changed')\n", (root / "src/app.py").read_text())
            self.assertEqual("new\n", (root / "src/new.py").read_text())
            self.assertTrue(root.is_relative_to(self.integration_parent.resolve()))
            if also is not None:
                also()
            self.last_report = self.report(passed=passed)
            return self.last_report
        return run

    def integrate(self, binding: dict, *, checks=None, host: str = "codex-host-v1") -> dict:
        self.install_hooks()
        return integration.integrate_write(
            self.ledger, self.project_id, project_root=self.project,
            bridge_run_id=binding["bridge_run_id"], host_client=host,
            run_checks=checks or self.checks(),
        )

    def integrations(self) -> list[dict]:
        with self.ledger.repository(self.project_id) as repository:
            return repository.records("write_integration")

    def head(self) -> str:
        return git_out(self.project, "rev-parse", "HEAD")

    def assert_clean_up(self) -> None:
        self.assertEqual([], list(self.integration_parent.iterdir()))
        listing = git_out(self.project, "worktree", "list", "--porcelain")
        self.assertEqual(1, listing.count("worktree "))
        self.assertFalse(self.hook_marker.exists(), "a Git hook ran")

    def assert_refused(self, outcome: dict, reason: str, source: str) -> dict:
        record = outcome["write_integration"]
        self.assertEqual(("refused", reason), (record["outcome"], record["reason"]))
        self.assertEqual(source, self.head())
        self.assertEqual("print('app')\n", (self.project / "src/app.py").read_text())
        self.assert_clean_up()
        self.assertEqual(record["record_id"], self.integrations()[-1]["record_id"])
        return record

    # --- the accepted path ---------------------------------------------------

    def test_accepted_patch_fast_forwards_the_primary(self) -> None:
        binding = self.accepted()
        source = self.head()
        branch = git_out(self.project, "symbolic-ref", "HEAD")
        outcome = self.integrate(binding)
        record = outcome["write_integration"]
        self.assertEqual(("integrated", None), (record["outcome"], record["reason"]))
        # The branch, index and working tree moved together to one new commit.
        new = self.head()
        self.assertEqual(branch, git_out(self.project, "symbolic-ref", "HEAD"))
        self.assertEqual(source, git_out(self.project, "rev-parse", "HEAD^"))
        self.assertEqual("print('changed')\n", (self.project / "src/app.py").read_text())
        self.assertEqual("new\n", (self.project / "src/new.py").read_text())
        self.assertEqual("", git_out(self.project, "status", "--porcelain"))
        # The record binds the patch, both commits, the tested tree and the checks.
        self.assertEqual(
            (binding["bridge_run_id"], binding["patch_digest"], source, new,
             git_out(self.project, "rev-parse", "HEAD^{tree}"),
             content_digest(self.last_report)),
            (record["bridge_run_id"], record["patch_digest"], record["source_commit"],
             record["new_commit"], record["tested_tree_digest"],
             record["checks_receipt_digest"]),
        )
        # A durable `promoting` intent with the same facts preceded the promotion.
        intent, final = self.integrations()
        self.assertEqual(record["record_id"], final["record_id"])
        self.assertEqual(
            ("promoting", None, record["new_commit"], record["tested_tree_digest"],
             record["checks_receipt_digest"], branch),
            (intent["outcome"], intent["reason"], intent["new_commit"],
             intent["tested_tree_digest"], intent["checks_receipt_digest"], intent["branch"]),
        )
        self.assertEqual(branch, record["branch"])
        self.assertEqual(self.last_report, outcome["check_report"])
        # Committed with the primary's identity, naming the task, providers, patch and run.
        self.assertEqual(
            "Bridge Tests <tests@example.invalid>",
            git_out(self.project, "log", "-1", "--format=%an <%ae>"),
        )
        message = git_out(self.project, "log", "-1", "--format=%B")
        for part in ("Worker provider: claude", "Verifier provider: codex",
                     f"Patch digest: {binding['patch_digest']}",
                     f"Bridge run: {binding['bridge_run_id']}", "Task: "):
            self.assertIn(part, message)
        self.assertEqual(1, len(self.check_roots))
        self.assert_clean_up()
        self.assertFalse(self.check_roots[0].exists())

    def test_already_integrated_is_refused(self) -> None:
        binding = self.accepted()
        self.integrate(binding)
        new = self.head()
        calls = []
        outcome = self.integrate(binding, checks=lambda root: calls.append(root))
        record = outcome["write_integration"]
        self.assertEqual(("refused", "write_already_integrated"), (record["outcome"], record["reason"]))
        self.assertEqual(([], new), (calls, self.head()))
        self.assert_clean_up()

    # --- preconditions --------------------------------------------------------

    def test_unverified_patch_is_refused(self) -> None:
        binding = self.accepted(verified=False)
        source = self.head()
        calls = []
        outcome = self.integrate(binding, checks=lambda root: calls.append(root))
        record = self.assert_refused(outcome, "write_not_verified", source)
        self.assertEqual([], calls)
        self.assertEqual(
            (binding["patch_digest"], source, None, None, None),
            (record["patch_digest"], record["source_commit"], record["new_commit"],
             record["tested_tree_digest"], record["checks_receipt_digest"]),
        )

    def test_another_host_or_an_unknown_run_records_nothing(self) -> None:
        binding = self.accepted()
        with self.assertRaises(HostClientMismatch):
            self.integrate(binding, host="claude-host-v1")
        with self.assertRaisesRegex(LedgerError, "recorded write result"):
            self.integrate({"bridge_run_id": "brg_" + "0" * 32})
        self.assertEqual([], self.integrations())
        self.assert_clean_up()

    # --- the primary's base ---------------------------------------------------

    def test_moved_base_is_refused(self) -> None:
        binding = self.accepted()
        (self.project / "docs/guide.md").write_text("moved\n")
        git_out(self.project, "commit", "-qam", "move the base")
        self.assert_refused(self.integrate(binding), "write_base_moved", self.head())

    def test_detached_head_is_refused(self) -> None:
        binding = self.accepted()
        source = self.head()
        git_out(self.project, "checkout", "-q", "--detach")
        self.assert_refused(self.integrate(binding), "write_base_moved", source)

    def test_staged_change_is_refused(self) -> None:
        binding = self.accepted()
        source = self.head()
        (self.project / "docs/extra.md").write_text("staged\n")
        git_out(self.project, "add", "docs/extra.md")
        self.assert_refused(self.integrate(binding), "write_primary_dirty", source)

    def test_modified_tracked_file_is_refused(self) -> None:
        binding = self.accepted()
        source = self.head()
        (self.project / "docs/guide.md").write_text("edited\n")
        self.assert_refused(self.integrate(binding), "write_primary_dirty", source)

    def test_untracked_file_at_an_added_path_is_a_collision(self) -> None:
        binding = self.accepted()
        source = self.head()
        (self.project / "src/new.py").write_text("untracked\n")
        self.assert_refused(self.integrate(binding), "write_primary_collision", source)
        self.assertEqual("untracked\n", (self.project / "src/new.py").read_text())

    def test_unrelated_untracked_file_is_allowed(self) -> None:
        binding = self.accepted()
        (self.project / "notes.txt").write_text("mine\n")
        record = self.integrate(binding)["write_integration"]
        self.assertEqual("integrated", record["outcome"])
        self.assertEqual("mine\n", (self.project / "notes.txt").read_text())

    # --- checks and promotion -------------------------------------------------

    def test_failed_checks_promote_nothing(self) -> None:
        binding = self.accepted()
        source = self.head()
        record = self.assert_refused(
            self.integrate(binding, checks=self.checks(passed=False)),
            "write_checks_failed", source,
        )
        self.assertEqual(content_digest(self.last_report), record["checks_receipt_digest"])
        self.assertIsNone(record["new_commit"])

    def test_unavailable_checks_promote_nothing(self) -> None:
        binding = self.accepted()
        source = self.head()

        def unavailable(root: Path) -> dict:
            raise ContractError("operational check configuration is unavailable")

        record = self.assert_refused(
            self.integrate(binding, checks=unavailable), "write_checks_failed", source)
        self.assertIsNone(record["checks_receipt_digest"])

    def test_commit_injected_before_promotion_is_a_race(self) -> None:
        binding = self.accepted()
        original = integration._commit_tree

        def then_move(*arguments, **keywords):
            made = original(*arguments, **keywords)
            (self.project / "docs/guide.md").write_text("raced\n")
            git_out(self.project, "commit", "-qam", "race")
            return made

        with mock.patch.object(integration, "_commit_tree", then_move):
            outcome = self.integrate(binding)
        raced = self.head()
        record = outcome["write_integration"]
        self.assertEqual(("refused", "write_promotion_race"), (record["outcome"], record["reason"]))
        # The tested commit exists but the primary is exactly as the racer left it.
        self.assertIsNotNone(record["new_commit"])
        self.assertNotEqual(raced, record["new_commit"])
        self.assertEqual("print('app')\n", (self.project / "src/app.py").read_text())
        self.assertFalse((self.project / "src/new.py").exists())
        self.assertEqual("", git_out(self.project, "status", "--porcelain"))
        self.assert_clean_up()

    def test_primary_dirtied_before_promotion_is_a_race(self) -> None:
        binding = self.accepted()
        source = self.head()
        original = integration._commit_tree

        def then_dirty(*arguments, **keywords):
            made = original(*arguments, **keywords)
            (self.project / "src/app.py").write_text("dirty\n")
            return made

        with mock.patch.object(integration, "_commit_tree", then_dirty):
            outcome = self.integrate(binding)
        record = outcome["write_integration"]
        self.assertEqual(("refused", "write_promotion_race"), (record["outcome"], record["reason"]))
        self.assertEqual(source, self.head())
        self.assertEqual("dirty\n", (self.project / "src/app.py").read_text())
        self.assert_clean_up()

    # --- untrusted repository configuration -----------------------------------

    def markers(self) -> list[str]:
        return sorted(path.name for path in Path(self.temporary.name).glob("marker-*"))

    def poison(self) -> None:
        """Repository-configured filters and a filesystem monitor that leave markers.

        Set after the write child and its verifier ran, so only integration
        could trigger them; `.gitattributes` assigns the filter to every path.
        """

        base = Path(self.temporary.name)
        for key, value in (
            ("filter.evil.smudge", f"sh -c 'touch {base}/marker-smudge; cat'"),
            ("filter.evil.clean", f"sh -c 'touch {base}/marker-clean; cat'"),
            ("filter.evil.process", f"sh -c 'touch {base}/marker-process'"),
            ("core.fsmonitor", f"sh -c 'touch {base}/marker-fsmonitor'"),
            ("diff.external", f"sh -c 'touch {base}/marker-diff'"),
        ):
            git_out(self.project, "config", key, value)
        # The repository's own attributes file, which `--attr-source` does not replace.
        (self.project / ".git/info").mkdir(exist_ok=True)
        (self.project / ".git/info/attributes").write_text("* filter=evil diff=evil\n")

    def force_reread(self) -> None:
        """Make every index entry racily clean, so writing an index re-reads its file.

        File times move into the future and the index is refreshed before the
        filters are configured; the content does not change. Writing any
        index from this one then compares the files' content, as Linux timing
        made Git do in CI.
        """

        future = time.time() + 3600
        for path in self.project.rglob("*"):
            if ".git" not in path.relative_to(self.project).parts and path.is_file():
                os.utime(path, (future, future))
        git_out(self.project, "update-index", "-q", "--refresh")

    def commit_attributes(self) -> None:
        (self.project / ".gitattributes").write_text("* filter=evil\n")
        git_out(self.project, "add", ".gitattributes")
        git_out(self.project, "commit", "-qm", "attributes")

    def checks_without_markers(self):
        checks = self.checks()

        def run(root: Path) -> dict:
            # Nothing ran while the disposable worktree was prepared.
            self.assertEqual([], self.markers())
            return checks(root)
        return run

    def test_repository_filters_and_monitor_never_run(self) -> None:
        self.commit_attributes()
        binding = self.accepted()
        self.poison()
        record = self.integrate(binding, checks=self.checks_without_markers())["write_integration"]
        self.assertEqual([], self.markers())
        self.assertEqual(("integrated", None), (record["outcome"], record["reason"]))
        self.assertEqual("new\n", (self.project / "src/new.py").read_text())
        self.assert_clean_up()

    def test_racily_clean_entries_never_run_repository_filters(self) -> None:
        # The CI failure on Linux: writing the promoted index re-read racily
        # clean files through the repository's process filter.
        self.commit_attributes()
        binding = self.accepted()
        self.force_reread()
        self.poison()
        record = self.integrate(binding, checks=self.checks_without_markers())["write_integration"]
        self.assertEqual([], self.markers())
        self.assertEqual(("integrated", None), (record["outcome"], record["reason"]))
        self.assertEqual("print('changed')\n", (self.project / "src/app.py").read_text())
        self.assert_clean_up()

    def test_merge_options_cannot_turn_promotion_into_a_squash(self) -> None:
        binding = self.accepted()
        source = self.head()
        branch = git_out(self.project, "symbolic-ref", "--short", "HEAD")
        git_out(self.project, "config", f"branch.{branch}.mergeOptions", "--squash")
        git_out(self.project, "config", "merge.ff", "false")
        record = self.integrate(binding)["write_integration"]
        self.assertEqual("integrated", record["outcome"])
        self.assertEqual((record["new_commit"], source),
                         (self.head(), git_out(self.project, "rev-parse", "HEAD^")))
        self.assertEqual("", git_out(self.project, "status", "--porcelain"))

    def approve_checks(self) -> None:
        """A committed, approved check that passes only on the patched tree."""

        script = self.project / "scripts/qa-patched"
        script.parent.mkdir()
        script.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            "raise SystemExit(0 if Path('src/new.py').read_text() == 'new\\n' else 1)\n"
        )
        script.chmod(0o755)
        config = self.project / ".codex/orchestration/operational-checks.json"
        config.parent.mkdir(parents=True)
        check = {
            "check_id": "patched", "driver": "python-wrapper",
            "argv": ["./scripts/qa-patched"],
            "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "interpreter_sha256": hashlib.sha256(
                Path(sys.executable).resolve().read_bytes()).hexdigest(),
            "runtime_tree": None, "timeout_seconds": 30,
        }
        config.write_text(json.dumps({
            "version": 1, "deterministic_checks": [check],
            "security_checks": [{**check, "check_id": "patched-security"}],
        }))
        git_out(self.project, "add", "scripts", ".codex")
        git_out(self.project, "commit", "-qm", "approved checks")

    def integrate_with_default_checks(self, binding: dict) -> dict:
        self.install_hooks()
        return integration.integrate_write(
            self.ledger, self.project_id, project_root=self.project,
            bridge_run_id=binding["bridge_run_id"], host_client="codex-host-v1",
        )

    @SANDBOX
    def test_default_runner_checks_the_patched_worktree(self) -> None:
        self.approve_checks()
        binding = self.accepted()
        outcome = self.integrate_with_default_checks(binding)
        record = outcome["write_integration"]
        self.assertEqual(("integrated", None), (record["outcome"], record["reason"]))
        self.assertEqual(["patched"], [item["check_id"] for item in
                                       outcome["check_report"]["deterministic_checks"]])
        self.assertEqual(content_digest(outcome["check_report"]), record["checks_receipt_digest"])
        self.assertEqual("new\n", (self.project / "src/new.py").read_text())
        self.assert_clean_up()

    @SANDBOX
    def test_default_runner_never_runs_repository_filters_or_monitor(self) -> None:
        self.approve_checks()
        self.commit_attributes()
        binding = self.accepted()
        self.force_reread()
        self.poison()
        record = self.integrate_with_default_checks(binding)["write_integration"]
        self.assertEqual([], self.markers())
        self.assertEqual(("integrated", None), (record["outcome"], record["reason"]))
        self.assert_clean_up()

    # --- promotion under Git's locks ---------------------------------------------

    def branch_ref(self) -> str:
        return git_out(self.project, "symbolic-ref", "HEAD")

    def assert_unchanged(self, source: str) -> None:
        self.assertEqual(source, self.head())
        self.assertEqual("print('app')\n", (self.project / "src/app.py").read_text())
        self.assertFalse((self.project / "src/new.py").exists())
        self.assertEqual("", git_out(self.project, "status", "--porcelain"))
        self.assert_no_leftover_locks()

    def assert_no_leftover_locks(self) -> None:
        git_dir = self.project / ".git"
        self.assertEqual([], sorted(path.name for path in git_dir.rglob("*.lock")))
        self.assertEqual([], sorted(path.name for path in git_dir.glob("index.cca-*")))

    def reflog(self) -> bytes:
        return (self.project / ".git/logs" / self.branch_ref()).read_bytes()

    def test_an_existing_branch_lock_is_busy(self) -> None:
        binding = self.accepted()
        source = self.head()
        lock = self.project / ".git" / f"{self.branch_ref()}.lock"
        lock.write_text("")
        outcome = self.integrate(binding)
        lock.unlink()
        self.assert_refused(outcome, "write_promotion_busy", source)
        self.assert_unchanged(source)

    def test_failure_after_the_files_are_written_restores_everything(self) -> None:
        binding = self.accepted()
        source = self.head()
        written = []

        def fail(locks, commit):
            written.append((self.project / "src/new.py").read_text())
            raise OSError("injected")

        with mock.patch.object(integration, "_write_ref_lock", fail):
            outcome = self.integrate(binding)
        self.assertEqual(["new\n"], written)
        record = self.assert_refused(outcome, "write_promotion_failed", source)
        self.assertIsNotNone(record["new_commit"])
        self.assert_unchanged(source)
        self.assertEqual(["promoting", "refused"],
                         [item["outcome"] for item in self.integrations()])

    def test_failure_after_the_index_rename_restores_the_index(self) -> None:
        binding = self.accepted()
        source = self.head()

        def fail(locks):
            raise OSError("injected")

        before = self.reflog()
        with mock.patch.object(integration, "_commit_ref", fail):
            outcome = self.integrate(binding)
        self.assertEqual(before, self.reflog())
        self.assert_refused(outcome, "write_promotion_failed", source)
        self.assert_unchanged(source)
        git_out(self.project, "diff-index", "--cached", "--quiet", "HEAD", "--")

    def test_the_index_stays_reserved_between_install_and_publication(self) -> None:
        binding = self.accepted()
        source = self.head()
        attempts = []

        def compete():
            for arguments in (("reset", "-q", "--hard", source), ("add", "docs/guide.md")):
                attempts.append(subprocess.run(
                    ["git", "-C", str(self.project), "-c", "core.hooksPath=/dev/null",
                     *arguments], capture_output=True, check=False,
                ).returncode)
            self.assertTrue((self.project / ".git/index.lock").exists())

        with mock.patch.object(integration, "_after_index_installed", compete):
            record = self.integrate(binding)["write_integration"]
        self.assertEqual(2, len(attempts))
        self.assertTrue(all(code != 0 for code in attempts), attempts)
        self.assertEqual("integrated", record["outcome"])
        self.assertEqual(record["new_commit"], self.head())
        self.assertEqual("", git_out(self.project, "status", "--porcelain"))
        self.assert_no_leftover_locks()

    def ref_write(self, behaviour):
        """Route lock-file writes through `behaviour` when they carry a commit ID."""

        original = integration._os_write
        routed: set[int] = set()

        def write(descriptor, data):
            if re.fullmatch(rb"[0-9a-f]{40}\n|[0-9a-f]{64}\n", bytes(data)):
                routed.add(descriptor)
            if descriptor in routed:
                return behaviour(descriptor, data, original)
            return original(descriptor, data)
        return mock.patch.object(integration, "_os_write", write)

    def test_enospc_on_the_branch_lock_leaves_no_lock_and_no_change(self) -> None:
        binding = self.accepted()
        source = self.head()
        before = self.reflog()

        def full(descriptor, data, original):
            raise OSError(errno.ENOSPC, "no space")

        with self.ref_write(full):
            outcome = self.integrate(binding)
        self.assert_refused(outcome, "write_promotion_failed", source)
        self.assert_unchanged(source)
        self.assertEqual(before, self.reflog())

    def test_a_short_write_that_stalls_leaves_no_lock_and_no_change(self) -> None:
        binding = self.accepted()
        source = self.head()
        calls = []

        def stall(descriptor, data, original):
            calls.append(len(data))
            return original(descriptor, bytes(data)[:5]) if len(calls) == 1 else 0

        with self.ref_write(stall):
            outcome = self.integrate(binding)
        self.assertEqual(2, len(calls))
        self.assert_refused(outcome, "write_promotion_failed", source)
        self.assert_unchanged(source)

    def test_short_writes_are_completed(self) -> None:
        binding = self.accepted()

        def trickle(descriptor, data, original):
            return original(descriptor, bytes(data)[:3])

        with self.ref_write(trickle):
            record = self.integrate(binding)["write_integration"]
        self.assertEqual("integrated", record["outcome"])
        branch_file = self.project / ".git" / self.branch_ref()
        self.assertEqual(f"{record['new_commit']}\n", branch_file.read_text())
        self.assert_no_leftover_locks()
        # The reflog entry was appended under the locks, before publication.
        self.assertIn(f" {record['new_commit']} ".encode(), self.reflog().splitlines()[-1])

    def test_a_file_raced_in_before_application_survives_rollback(self) -> None:
        binding = self.accepted()
        source = self.head()

        def ignored_file_appears():
            (self.project / ".git/info").mkdir(exist_ok=True)
            (self.project / ".git/info/exclude").write_text("src/new.py\n")
            (self.project / "src/new.py").write_text("ignored\n")

        with mock.patch.object(integration, "_before_apply", ignored_file_appears):
            outcome = self.integrate(binding)
        record = outcome["write_integration"]
        self.assertEqual(("refused", "write_promotion_race"), (record["outcome"], record["reason"]))
        self.assertEqual("ignored\n", (self.project / "src/new.py").read_text())
        self.assertEqual("print('app')\n", (self.project / "src/app.py").read_text())
        self.assertEqual(source, self.head())
        self.assert_no_leftover_locks()
        self.assert_clean_up()

    def test_other_git_processes_are_refused_while_promotion_holds_the_locks(self) -> None:
        binding = self.accepted()
        original = integration._Locks.acquire
        attempts = []

        def acquire_then_compete(locks):
            original(locks)
            for arguments in (
                ("commit", "-q", "--allow-empty", "-m", "competing"),
                ("switch", "-q", "-c", "competing"),
                ("update-ref", self.branch_ref(), "HEAD"),
                ("add", "docs/guide.md"),
            ):
                attempts.append(subprocess.run(
                    ["git", "-C", str(self.project), "-c", "core.hooksPath=/dev/null",
                     *arguments], capture_output=True, check=False,
                ).returncode)

        with mock.patch.object(integration._Locks, "acquire", acquire_then_compete):
            record = self.integrate(binding)["write_integration"]
        self.assertTrue(all(code != 0 for code in attempts), attempts)
        self.assertEqual("integrated", record["outcome"])
        self.assertEqual(record["new_commit"], self.head())

    def test_raced_ignored_file_at_an_added_path_is_kept(self) -> None:
        binding = self.accepted()
        source = self.head()
        original = integration._commit_tree

        def then_ignored(*arguments, **keywords):
            made = original(*arguments, **keywords)
            (self.project / ".git/info").mkdir(exist_ok=True)
            (self.project / ".git/info/exclude").write_text("src/new.py\n")
            (self.project / "src/new.py").write_text("ignored\n")
            return made

        with mock.patch.object(integration, "_commit_tree", then_ignored):
            outcome = self.integrate(binding)
        record = outcome["write_integration"]
        self.assertEqual(("refused", "write_promotion_race"), (record["outcome"], record["reason"]))
        self.assertEqual("ignored\n", (self.project / "src/new.py").read_text())
        self.assertEqual(source, self.head())
        self.assert_clean_up()

    # --- the durable intent and reconciliation -------------------------------------

    def test_a_lost_integrated_record_is_reconciled_without_double_integration(self) -> None:
        binding = self.accepted()
        source = self.head()
        original = type(self.ledger).record_write_integration

        def lose_integrated(ledger, project_id, **keywords):
            if keywords["outcome"] == "integrated":
                raise LedgerError("injected append failure")
            return original(ledger, project_id, **keywords)

        with mock.patch.object(type(self.ledger), "record_write_integration", lose_integrated), \
                self.assertRaisesRegex(LedgerError, "injected"):
            self.integrate(binding)
        promoted = self.head()
        self.assertNotEqual(source, promoted)
        self.assertEqual(["promoting"], [item["outcome"] for item in self.integrations()])
        self.assertEqual(1, len(self.ledger.unfinished_write_integrations(self.project_id)))
        # The next attempt reconciles the intent first, then refuses a second integration.
        outcome = self.integrate(binding)
        self.assertEqual(("refused", "write_already_integrated"),
                         (outcome["write_integration"]["outcome"],
                          outcome["write_integration"]["reason"]))
        self.assertEqual(["promoting", "integrated", "refused"],
                         [item["outcome"] for item in self.integrations()])
        self.assertEqual(promoted, self.integrations()[1]["new_commit"])
        self.assertEqual(promoted, self.head())
        self.assertEqual([], self.ledger.unfinished_write_integrations(self.project_id))

    def intent(self, binding: dict, new_commit: str) -> dict:
        [result] = [item for item in self.write_records()
                    if item["bridge_run_id"] == binding["bridge_run_id"]]
        return self.ledger.record_write_integration(
            self.project_id, base=result, outcome="promoting", reason=None,
            new_commit=new_commit, tested_tree_digest="d" * 40,
            checks_receipt_digest="e" * 64, branch=self.branch_ref(),
        )

    def test_an_intent_with_the_branch_back_at_the_source_needs_the_owner(self) -> None:
        # A crash after publication followed by an owner's reset looks the same
        # as a promotion that never happened, so it is never classified as failed.
        binding = self.accepted()
        source = self.head()
        self.intent(binding, "c" * 40)
        record = self.integrate(binding)["write_integration"]
        self.assertEqual(("refused", "write_integration_needs_owner"),
                         (record["outcome"], record["reason"]))
        self.assertEqual(
            [("promoting", None), ("refused", "write_integration_needs_owner"),
             ("refused", "write_integration_needs_owner")],
            [(item["outcome"], item["reason"]) for item in self.integrations()],
        )
        self.assertEqual(source, self.head())

    def test_another_host_with_a_pending_intent_appends_nothing(self) -> None:
        binding = self.accepted()
        self.intent(binding, "c" * 40)
        with self.assertRaises(HostClientMismatch):
            self.integrate(binding, host="claude-host-v1")
        self.assertEqual(["promoting"], [item["outcome"] for item in self.integrations()])

    def test_an_intent_with_a_changed_primary_needs_the_owner(self) -> None:
        binding = self.accepted()
        source = self.head()
        self.intent(binding, "c" * 40)
        (self.project / "src/app.py").write_text("half written\n")
        record = self.integrate(binding)["write_integration"]
        self.assertEqual(("refused", "write_integration_needs_owner"),
                         (record["outcome"], record["reason"]))
        self.assertEqual(
            ["promoting", "refused", "refused"],
            [item["outcome"] for item in self.integrations()],
        )
        self.assertEqual(source, self.head())
        (self.project / "src/app.py").write_text("print('app')\n")
        again = self.integrate(binding)["write_integration"]
        self.assertEqual("write_integration_needs_owner", again["reason"])

    # --- signals ---------------------------------------------------------------------

    def test_sigterm_during_cleanup_still_removes_and_records(self) -> None:
        binding = self.accepted()
        source = self.head()
        original = integration._git

        def terminate_during_removal(root, *arguments, **keywords):
            if arguments[:2] == ("worktree", "remove"):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)
            return original(root, *arguments, **keywords)

        with mock.patch.object(integration, "_git", terminate_during_removal), \
                bridge.signal_guard(), self.assertRaises(bridge.BridgeInterrupted):
            self.integrate(binding, checks=self.checks(passed=False))
        [record] = self.integrations()
        self.assertEqual(("refused", "write_integration_interrupted"),
                         (record["outcome"], record["reason"]))
        self.assertEqual(source, self.head())
        self.assert_clean_up()

    def test_cancellation_removes_the_worktree_and_records_it(self) -> None:
        binding = self.accepted()
        source = self.head()

        def cancelled(root: Path) -> dict:
            self.check_roots.append(root)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.integrate(binding, checks=cancelled)
        [record] = self.integrations()
        self.assertEqual(("refused", "write_integration_interrupted"),
                         (record["outcome"], record["reason"]))
        self.assertEqual(source, self.head())
        self.assertFalse(self.check_roots[0].exists())
        self.assert_clean_up()


class WriteIntegrationContractTests(unittest.TestCase):
    def material(self, **changes) -> dict:
        value = {
            "schema_version": 1, "record_type": "write_integration",
            "record_id": "write_integration:x", "run_id": "run_1",
            "bridge_run_id": "brg_" + "1" * 32, "patch_digest": "a" * 64,
            "source_commit": "b" * 40, "new_commit": "c" * 40,
            "tested_tree_digest": "d" * 40, "checks_receipt_digest": "e" * 64,
            "branch": "refs/heads/main",
            "outcome": "integrated", "reason": None, "observed_at_ms": 1,
        }
        value.update(changes)
        return value

    def test_integrated_binds_every_digest_and_refused_carries_a_reason(self) -> None:
        validate_learning_record(self.material())
        validate_learning_record(self.material(outcome="promoting"))
        validate_learning_record(self.material(
            outcome="refused", reason="write_base_moved", new_commit=None,
            tested_tree_digest=None, checks_receipt_digest=None, branch=None,
        ))
        for changes in (
            {"tested_tree_digest": None},
            {"branch": None},
            {"outcome": "promoting", "branch": None},
            {"branch": "refs/heads/a b"},
            {"branch": "refs/tags/v1"},
            {"reason": "write_base_moved"},
            {"new_commit": "b" * 40},
            {"outcome": "refused", "reason": None},
            {"outcome": "refused", "reason": "Not A Code"},
            {"extra": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_learning_record(self.material(**changes))


class DeferredSignalTests(unittest.TestCase):
    def test_a_signal_during_promotion_is_delivered_after_it(self) -> None:
        seen: list[str] = []
        previous = signal.signal(signal.SIGTERM, lambda *_: seen.append("handled"))
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with integration.deferred_signals():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            seen.append("promoted")
        self.assertEqual(["promoted", "handled"], seen)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None):
    # Only this module's own tests: the write child and verification tests it
    # builds on run in their own modules.
    suite = unittest.TestSuite()
    for case in (WriteIntegrationTests, WriteIntegrationContractTests, DeferredSignalTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite
