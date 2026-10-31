from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import bridge, host_commands, learning_store
from codex_agents.host_commands import HostCommandError, interpreter_path
from codex_agents.host_paths import (
    PYTHON_ISOLATION_FLAGS,
    HostPathError,
    private_directory,
    remove_private_directory,
    temp_roots,
    trusted_location_problem,
    within,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_store import LearningLedger, LearningState


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


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class PrivateDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "base"
        self.base.mkdir(mode=0o700)

    def test_it_is_owner_only_and_fresh(self) -> None:
        first = private_directory(self.base, "scratch", "qa-")
        second = private_directory(self.base, "scratch", "qa-")
        self.assertNotEqual(first, second)
        self.assertEqual(self.base / "scratch", first.parent)
        self.assertTrue(first.name.startswith("qa-"))
        for path in (first, second, self.base / "scratch"):
            self.assertEqual(0o700, mode(path))
            self.assertEqual(os.geteuid(), os.lstat(path).st_uid)

    def test_a_planted_symlink_at_any_component_is_refused(self) -> None:
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (self.base / "scratch").symlink_to(elsewhere)
        with self.assertRaises(HostPathError):
            private_directory(self.base, "scratch", "qa-")
        self.assertEqual([], list(elsewhere.iterdir()))
        # A symlink above the base, owned by this user, is refused too.
        link = Path(self.temporary.name) / "link"
        link.symlink_to(elsewhere)
        with self.assertRaises(HostPathError):
            private_directory(link, "scratch", "qa-")
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_a_shared_container_or_base_is_refused(self) -> None:
        (self.base / "scratch").mkdir(mode=0o755)
        os.chmod(self.base / "scratch", 0o755)
        with self.assertRaisesRegex(HostPathError, "owner-only"):
            private_directory(self.base, "scratch", "qa-")
        open_base = Path(self.temporary.name) / "open"
        open_base.mkdir()
        os.chmod(open_base, 0o777)
        with self.assertRaisesRegex(HostPathError, "writable by other users"):
            private_directory(open_base, "scratch", "qa-")

    def test_removal_handles_read_only_directories(self) -> None:
        directory = private_directory(self.base, "scratch", "qa-")
        sealed = directory / "sealed"
        sealed.mkdir()
        (sealed / "file").write_text("x")
        os.chmod(sealed, 0o500)
        remove_private_directory(directory)
        self.assertFalse(directory.exists())


class TrustedLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.bin = self.base / "bin"
        self.bin.mkdir(mode=0o755)
        os.chmod(self.bin, 0o755)
        self.python = self.bin / "python3"
        self.python.write_text("#!/bin/sh\n")
        self.python.chmod(0o755)

    def test_inside_an_untrusted_root_is_refused(self) -> None:
        self.assertIsNone(trusted_location_problem(self.python, []))
        self.assertIn("project or a temporary", trusted_location_problem(
            self.python, [self.base]
        ))
        # The real temporary directories are untrusted roots.
        self.assertTrue(any(within(Path(TEMP_ROOT), root) for root in temp_roots()))

    def test_writable_or_symlinked_shared_components_are_refused(self) -> None:
        os.chmod(self.bin, 0o775)
        if sys.platform != "darwin" or os.lstat(self.bin).st_gid != 80:
            self.assertIn("writable", trusted_location_problem(self.python, []))
        os.chmod(self.bin, 0o757)
        self.assertIn("writable", trusted_location_problem(self.python, []))
        os.chmod(self.bin, 0o755)
        shared = self.base / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        target = shared / "python3"
        target.write_text("#!/bin/sh\n")
        link = self.bin / "python-link"
        link.symlink_to(target)
        # A path through a symlink is never trusted as given; its target is checked.
        self.assertEqual("the path is not canonical", trusted_location_problem(link, []))
        self.assertIn("writable", trusted_location_problem(
            Path(os.path.realpath(link)), []
        ))

    @NEEDS_TRUSTED_PYTHON
    def test_the_rule_pins_the_resolved_interpreter_past_any_link_chain(self) -> None:
        # alias -> hop (in a directory the model could write) -> trusted target.
        hop_directory = self.base / "hop"
        hop_directory.mkdir(mode=0o755)
        os.chmod(hop_directory, 0o755)
        hop = hop_directory / "python3"
        hop.symlink_to(self.python)
        alias = self.bin / "python-alias"
        alias.symlink_to(hop)
        untrusted = {Path(os.path.realpath(hop_directory))}
        with mock.patch.object(host_commands, "temp_roots", lambda: untrusted), \
                mock.patch.object(sys, "executable", str(alias)), \
                mock.patch.object(sys, "prefix", sys.base_prefix):
            pinned = interpreter_path()
        # The rule names the target itself, so retargeting the hop changes nothing.
        self.assertEqual(os.path.realpath(self.python), pinned)
        self.assertEqual(Path(pinned), Path(os.path.realpath(pinned)))
        # A chain whose resolved target is itself in a writable place is refused.
        planted = hop_directory / "planted-python3"
        planted.write_text("#!/bin/sh\n")
        hop.unlink()
        hop.symlink_to(planted)
        with mock.patch.object(host_commands, "temp_roots", lambda: untrusted), \
                mock.patch.object(sys, "executable", str(alias)), \
                mock.patch.object(sys, "prefix", sys.base_prefix):
            with self.assertRaisesRegex(HostCommandError, "cannot be trusted"):
                interpreter_path()

    def test_the_rule_names_a_trusted_base_interpreter(self) -> None:
        project = self.base / "project"
        project.mkdir()
        inside = project / ".venv" / "bin" / "python3"
        inside.parent.mkdir(parents=True)
        inside.write_text("#!/bin/sh\n")
        with mock.patch.object(sys, "executable", str(inside)), \
                mock.patch.object(sys, "prefix", sys.base_prefix):
            with self.assertRaisesRegex(HostCommandError, "cannot be trusted"):
                interpreter_path(project_root=project)
        # A virtual environment's interpreter is replaced by its base one.
        venv = self.base / "venv"
        with mock.patch.object(sys, "prefix", str(venv)), \
                mock.patch.object(sys, "_base_executable", str(venv / "bin" / "python3"),
                                  create=True):
            with self.assertRaisesRegex(HostCommandError, "virtual environment"):
                interpreter_path()
        real = sys.executable
        resolved = os.path.realpath(real)
        if trusted_location_problem(Path(resolved), list(temp_roots())) is None:
            with mock.patch.object(sys, "prefix", str(venv)), \
                    mock.patch.object(sys, "executable", str(venv / "bin" / "python3")), \
                    mock.patch.object(sys, "_base_executable", real, create=True):
                self.assertEqual(resolved, interpreter_path())
        # The temporary directory itself is never trusted.
        with mock.patch.object(sys, "executable", str(self.python)), \
                mock.patch.object(sys, "prefix", sys.base_prefix):
            with self.assertRaisesRegex(HostCommandError, "cannot be trusted"):
                interpreter_path()


class PythonLaunchTests(unittest.TestCase):
    """Every Python an allowlisted command starts outside the sandbox is isolated."""

    def test_the_collector_and_the_watchdog_start_without_site_packages(self) -> None:
        self.assertEqual(("-I", "-B", "-S"), PYTHON_ISOLATION_FLAGS)
        launched: list[list[str]] = []

        def record(argv, **options):
            launched.append(list(argv))
            raise OSError("not launched in this test")

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state = LearningState(Path(directory) / "state")
            learning_store.authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
            with mock.patch.object(learning_store.subprocess, "Popen", record), \
                    self.assertRaises(OSError):
                learning_store._start_collector_locked(
                    state, ORCHESTRATION_ROOT / "cli.py", project_root=Path(directory),
                    timeout=0.1,
                )
        # The watchdog launch is only recorded; nothing is started or killed.
        with mock.patch.object(bridge.subprocess, "Popen", lambda argv, **_: launched.append(
            list(argv)
        )):
            bridge._start_watchdog(-1, 0.0)
        self.assertEqual(2, len(launched))
        for argv in launched:
            self.assertEqual([sys.executable, "-I", "-B", "-S"], argv[:4])


class HostScratchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    @unittest.skipUnless(sys.platform == "darwin", "the per-user cache directory is macOS's")
    def test_production_scratch_is_outside_home_and_every_temporary_directory(self) -> None:
        with mock.patch.object(bridge, "WORKSPACE_PARENT", None):
            base = bridge.scratch_base()
        resolved = Path(os.path.realpath(base))
        self.assertFalse(within(resolved, Path(os.path.realpath(Path.home()))))
        self.assertFalse(any(within(resolved, root) for root in temp_roots()))
        bridge.check_context_boundary(resolved)

    def test_linux_without_a_runtime_directory_refuses(self) -> None:
        with mock.patch.object(bridge, "WORKSPACE_PARENT", None), \
                mock.patch.object(bridge.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            with self.assertRaisesRegex(bridge.BridgeRefused, "host_scratch_unavailable"):
                bridge.scratch_base()

    def test_a_dispatch_workspace_is_private_and_planted_links_refuse(self) -> None:
        parent = self.base / "dispatch"
        parent.mkdir(mode=0o700)
        project = self.base / "project"
        project.mkdir()
        with mock.patch.object(bridge, "WORKSPACE_PARENT", str(parent)), \
                mock.patch.object(bridge, "temp_roots", lambda: set()):
            workspace = bridge.private_workspace("dispatch-", claude=True, project_root=project)
            self.assertEqual(parent / bridge.SCRATCH_CONTAINER, workspace.parent)
            for name in ("export", "control", "tmp"):
                self.assertEqual(0o700, mode(workspace / name))
            remove_private_directory(workspace)
            container = parent / bridge.SCRATCH_CONTAINER
            os.rmdir(container)
            elsewhere = self.base / "elsewhere"
            elsewhere.mkdir(mode=0o700)
            container.symlink_to(elsewhere)
            with self.assertRaisesRegex(bridge.BridgeRefused, "host_scratch_unavailable"):
                bridge.private_workspace("dispatch-", claude=False, project_root=project)
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_a_scratch_base_overlapping_a_writable_root_is_refused(self) -> None:
        project = self.base / "project"
        (project / "cache").mkdir(parents=True)
        outer = self.base / "outer"
        (outer / "inner").mkdir(parents=True)
        # Against the real temporary directories: the test scratch is inside one.
        with mock.patch.object(bridge, "WORKSPACE_PARENT", str(self.base)):
            with self.assertRaisesRegex(bridge.BridgeRefused, "host_scratch_unavailable"):
                bridge.private_workspace("dispatch-", claude=False, project_root=None)
        cases = (
            # The base inside a temporary directory, or containing one.
            (outer / "inner", {outer}, None),
            (outer, {outer / "inner"}, None),
            # The base inside the project, or the project itself.
            (project / "cache", set(), project),
            (project, set(), project),
        )
        for base, temporary, root in cases:
            with self.subTest(base=base.name), \
                    mock.patch.object(bridge, "WORKSPACE_PARENT", str(base)), \
                    mock.patch.object(bridge, "temp_roots", lambda temporary=temporary: temporary):
                with self.assertRaisesRegex(bridge.BridgeRefused, "host_scratch_unavailable"):
                    bridge.private_workspace("dispatch-", claude=False, project_root=root)
                self.assertFalse((base / bridge.SCRATCH_CONTAINER).exists())

    def test_a_provider_observation_gets_a_private_temporary_directory(self) -> None:
        state = LearningState(self.base / "state")
        ledger = LearningLedger(
            state,
            client_capabilities=load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            ),
        )
        self.addCleanup(ledger.close)
        project = self.base / "project"
        project.mkdir()
        project_id = ledger.register_project(project, now_ms=1)
        seen: list[Path] = []

        def observe(**options):
            seen.append(options["tmpdir"])
            self.assertEqual(0o700, mode(options["tmpdir"]))
            raise host_commands.HostCommandError("stop here")

        with mock.patch("codex_agents.providers.observe_claude", observe), \
                self.assertRaises(host_commands.HostCommandError):
            ledger.observe_provider(project_id, "claude", project_root=project)
        self.assertEqual(state.root / "scratch", seen[0].parent)
        self.assertFalse(seen[0].exists())


if __name__ == "__main__":
    unittest.main()
