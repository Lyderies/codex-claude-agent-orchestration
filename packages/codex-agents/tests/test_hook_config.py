from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import BUNDLE_ROOT, TEMP_ROOT

from codex_agents import hook_config
from codex_agents.hook_config import (
    ASYNC_HOOK_EVENTS,
    HOOK_EVENTS,
    HookConfigError,
    hook_command,
    parse_local_manifest,
    render_hooks,
    render_local_manifest,
)

FIXTURE = BUNDLE_ROOT.parent / "tests" / "fixtures" / "learning" / "hook-pre-tool.json"


class RenderTests(unittest.TestCase):
    """The hooks file is exact bytes, POSIX-shell text with nothing looked up."""

    def test_paths_with_spaces_and_double_quotes_render_exactly(self) -> None:
        interpreter = Path("/opt/homebrew/Cellar/python 3/bin/python3")
        root = Path('/srv/work/my "project" $(touch x) `y`')
        command = hook_command(interpreter, root)
        self.assertEqual(
            command,
            "/usr/bin/env xcrun_nocache=1 '/opt/homebrew/Cellar/python 3/bin/python3' "
            "-I -B -S -X pycache_prefix=/dev/null "
            "'/srv/work/my \"project\" $(touch x) `y`/.codex/orchestration/learning_hook.py' "
            "--checkout '/srv/work/my \"project\" $(touch x) `y`' "
            "--client-version codex-hooks-v1",
        )
        # The shell reads back exactly these words: no expansion, no lookup but env's.
        self.assertEqual(
            shlex.split(command),
            [
                "/usr/bin/env", "xcrun_nocache=1", str(interpreter),
                "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null",
                str(root / ".codex/orchestration/learning_hook.py"),
                "--checkout", str(root),
                "--client-version", "codex-hooks-v1",
            ],
        )
        self.assertNotIn("git", command.replace("codex-hooks", ""))
        rendered = render_hooks(interpreter, root)
        self.assertEqual(rendered, render_hooks(interpreter, root))
        self.assertTrue(rendered.endswith(b"}\n"))
        data = json.loads(rendered)
        self.assertEqual(set(data), {"description", "hooks"})
        self.assertEqual(list(data["hooks"]), list(HOOK_EVENTS))
        for event, groups in data["hooks"].items():
            expected = {"type": "command", "command": command, "timeout": 3}
            if event in ASYNC_HOOK_EVENTS:
                expected["async"] = True
            self.assertEqual(groups, [{"hooks": [expected]}])

    def test_unquotable_paths_are_refused(self) -> None:
        good = Path("/usr/local/bin/python3")
        for bad in ("/srv/it's", "/srv/back\\slash", "/srv/new\nline", "/srv/esc\x1bape",
                    "/srv/tab\tbed", "relative/path", "/srv/bidi‮"):
            with self.subTest(path=bad):
                with self.assertRaises(HookConfigError):
                    render_hooks(good, Path(bad))
                with self.assertRaises(HookConfigError):
                    render_hooks(Path(bad), Path("/srv/project"))

    def test_local_record_round_trips_and_rejects_other_shapes(self) -> None:
        hooks = render_hooks(Path("/usr/local/bin/python3"), Path("/srv/project"))
        raw = render_local_manifest(Path("/usr/local/bin/python3"), hooks)
        interpreter, files = parse_local_manifest(raw)
        self.assertEqual(interpreter, Path("/usr/local/bin/python3"))
        self.assertEqual(set(files), {hook_config.HOOKS_RELATIVE})
        record = json.loads(raw)
        for mutate in (
            lambda item: item.update(extra=1),
            lambda item: item.update(format=2),
            lambda item: item.update(interpreter="python3"),
            lambda item: item["files"].update({".codex/other": "0" * 64}),
            lambda item: item["files"].update({hook_config.HOOKS_RELATIVE: "x"}),
        ):
            changed = json.loads(json.dumps(record))
            mutate(changed)
            with self.assertRaises(HookConfigError):
                parse_local_manifest(json.dumps(changed).encode())


class PlantedPathTests(unittest.TestCase):
    """The rendered command runs no PATH program: a planted python3 or git never runs."""

    def test_planted_python_and_git_never_run(self) -> None:
        interpreter = Path(os.path.realpath(sys.executable))
        payload = json.loads(FIXTURE.read_text())
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            base = Path(os.path.realpath(directory))
            # A checkout of its own (the hook's checkout guard passes for its subfolders).
            checkout = base / "checkout"
            (checkout / ".git").mkdir(parents=True)
            shutil.copytree(BUNDLE_ROOT / ".codex" / "orchestration", checkout / ".codex" / "orchestration",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(BUNDLE_ROOT / "VERSION", checkout / "VERSION")  # the version an install manifest would give
            command = hook_command(interpreter, checkout)
            marker = base / "planted-ran"
            planted = base / "planted-bin"
            planted.mkdir()
            for name in ("python3", "python", "git", "env", "xcrun"):
                tool = planted / name
                tool.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\nexit 0\n')
                tool.chmod(0o755)
            home = base / "home"
            (home / ".local" / "state").mkdir(parents=True)
            project = checkout / "src" / "deep"
            project.mkdir(parents=True)
            # A Stop event makes the hook print "{}", so a clean run is visible; the
            # learning state does not exist, so nothing is recorded or started.
            payload.update(cwd=str(project), hook_event_name="Stop")
            environment = {
                "PATH": f"{planted}:/usr/bin:/bin",
                "HOME": str(home),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "TMPDIR": str(base),
            }
            shells = [shell for shell in ("/bin/sh", "/bin/bash", "/bin/zsh") if os.path.exists(shell)]
            for shell in shells:
                with self.subTest(shell=shell):
                    completed = subprocess.run(
                        [shell, "-c", command],
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        cwd=project,
                        env=environment,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual("{}\n", completed.stdout)
                    self.assertEqual("", completed.stderr)
                    self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")


if __name__ == "__main__":
    unittest.main()
