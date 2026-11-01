"""The Codex learning hooks file (`.codex/hooks.json`), rendered for one checkout.

Codex 0.164 runs every hook command on the host, outside the sandbox, as
``<session shell> -c "<command>"`` (zsh, bash or sh; ``$SHELL -lc`` or
``/bin/sh -lc`` when no session shell is known), in the session's working
directory, with the session's environment (`codex-rs/hooks/src/engine/
command_runner.rs` `build_command`, `core/src/session/mod.rs`
`build_hooks_config`). There is no project-root variable for project hooks, and
the working directory may be any folder below the project.

So the command names everything absolutely and looks nothing up: the trusted
interpreter the installer ran under (never a PATH `python3`, never macOS's
`/usr/bin` xcrun shim) and this checkout's own `learning_hook.py`, no `$(...)`,
no git. ``--checkout`` pins the checkout the file was rendered for: Codex 0.164 loads a
linked worktree's hooks from its main checkout (`config/src/loader/mod.rs`
`root_checkout_hooks_folder_for_dir`), so `learning_hook.py` first checks, with the
standard library only, that the event's working folder belongs to that checkout, and
does nothing otherwise. ``/usr/bin/env xcrun_nocache=1`` sets the variable without relying on a
shell's ``VAR=value cmd`` syntax. Each path is single-quoted and may not hold a
single quote, a backslash or any control character, so sh, bash, zsh and fish
all read the same words.

The rendered file holds this checkout's absolute paths, so it is never
committed: the package's `.codex/.gitignore` keeps it (and the local install
record that names the interpreter) out of Git, and each checkout gets its own
from the installer. Codex trusts each hook by its hash, so after a new render the
owner re-trusts the hooks in Codex (`/hooks`); the installer never writes trust.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from pathlib import Path

from .host_paths import PYTHON_ISOLATION_FLAGS, executable_location_problem, temp_roots

HOOKS_RELATIVE = ".codex/hooks.json"
# Written beside the runtime for this checkout only: the interpreter the hooks name and
# the digest of the rendered hooks file, so verify, update and remove rebuild the bytes.
LOCAL_MANIFEST_RELATIVE = ".codex/orchestration/local-install.json"
LOCAL_FILES = (HOOKS_RELATIVE, LOCAL_MANIFEST_RELATIVE)
HOOK_SCRIPT_RELATIVE = ".codex/orchestration/learning_hook.py"
CLIENT_VERSION = "codex-hooks-v1"
HOOK_TIMEOUT = 3
HOOK_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "Interrupt",
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
)
ASYNC_HOOK_EVENTS = frozenset(
    {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse", "PostToolUse"}
)
DESCRIPTION = (
    "Observational, redacted codex-agents learning telemetry. Written by the installer "
    "for this checkout; never commit it."
)
# What 0.8.x shipped as a static, committed file: python3 from PATH and the hook found
# through `git rev-parse`, both run outside the sandbox.
LEGACY_HOOK_COMMAND = (
    "python3 -I -B -S -X pycache_prefix=/dev/null "
    '"$(git rev-parse --show-toplevel)/.codex/orchestration/learning_hook.py" '
    "--client-version codex-hooks-v1"
)
LOCAL_MANIFEST_FORMAT = 1
MAX_LOCAL_BYTES = 65_536
_REFUSED_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


class HookConfigError(ValueError):
    """The hooks cannot be rendered or the local install record is unusable."""


def path_problem(value: str) -> str | None:
    """Why `value` cannot be single-quoted into the hook command, or None."""

    if not value.startswith("/"):
        return "it is not an absolute path"
    if "'" in value or "\\" in value or any(
        unicodedata.category(character) in _REFUSED_CATEGORIES for character in value
    ):
        return "it contains a quote, a backslash or a control character"
    return None


def interpreter_problem(interpreter: Path, project_root: Path) -> str | None:
    """Why the hooks may not name `interpreter`, or None; decided without running it.

    The rule's own checks (`host_paths.executable_location_problem`): canonical, owned
    by this user or root and writable by no one else, outside the project and the
    temporary folders, and under `TRUSTED_INSTALL_ROOTS` or a hidden top-level home
    folder; never macOS's `/usr/bin` xcrun shim; and quotable.
    """

    problem = path_problem(str(interpreter))
    if problem is not None:
        return problem
    if sys.platform == "darwin" and interpreter.parent == Path("/usr/bin"):
        return "it is macOS's /usr/bin/python3, an xcrun shim that reads a cache under $TMPDIR"
    return executable_location_problem(interpreter, [*temp_roots(), project_root])


def _quoted(value: str, what: str) -> str:
    problem = path_problem(value)
    if problem is not None:
        raise HookConfigError(f"the {what} cannot be named in a hook command: {problem}")
    return f"'{value}'"


def hook_command(interpreter: Path, project_root: Path) -> str:
    """The one command every hook runs, as POSIX shell text."""

    return " ".join(
        [
            "/usr/bin/env",
            "xcrun_nocache=1",
            _quoted(str(interpreter), "interpreter path"),
            *PYTHON_ISOLATION_FLAGS,
            _quoted(str(project_root / HOOK_SCRIPT_RELATIVE), "project folder"),
            "--checkout",
            _quoted(str(project_root), "project folder"),
            "--client-version",
            CLIENT_VERSION,
        ]
    )


def render_hooks(interpreter: Path, project_root: Path) -> bytes:
    """The exact bytes of `.codex/hooks.json` for this interpreter and checkout."""

    command = hook_command(interpreter, project_root)
    lines = [
        "{",
        f"  \"description\": {json.dumps(DESCRIPTION)},",
        "  \"hooks\": {",
    ]
    for index, event in enumerate(HOOK_EVENTS):
        handler: dict[str, object] = {"type": "command", "command": command}
        if event in ASYNC_HOOK_EVENTS:
            handler["async"] = True
        handler["timeout"] = HOOK_TIMEOUT
        separator = "," if index < len(HOOK_EVENTS) - 1 else ""
        lines.append(f"    {json.dumps(event)}: {json.dumps([{'hooks': [handler]}])}{separator}")
    lines += ["  }", "}"]
    return ("\n".join(lines) + "\n").encode("ascii")


def render_local_manifest(interpreter: Path, hooks: bytes) -> bytes:
    record = {
        "format": LOCAL_MANIFEST_FORMAT,
        "interpreter": str(interpreter),
        "files": {HOOKS_RELATIVE: hashlib.sha256(hooks).hexdigest()},
    }
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_local_manifest(raw: bytes) -> tuple[Path, dict[str, str]]:
    """The recorded interpreter and file digests, or HookConfigError."""

    if len(raw) > MAX_LOCAL_BYTES:
        raise HookConfigError("the local install record is too large")
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookConfigError("the local install record is not valid JSON") from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"format", "interpreter", "files"}
        or record["format"] != LOCAL_MANIFEST_FORMAT
        or not isinstance(record["interpreter"], str)
        or not isinstance(record["files"], dict)
        or set(record["files"]) != {HOOKS_RELATIVE}
        or not isinstance(record["files"][HOOKS_RELATIVE], str)
        or len(record["files"][HOOKS_RELATIVE]) != 64
        or any(ch not in "0123456789abcdef" for ch in record["files"][HOOKS_RELATIVE])
    ):
        raise HookConfigError("the local install record has an unknown shape")
    if path_problem(record["interpreter"]) is not None:
        raise HookConfigError("the local install record names an unusable interpreter")
    return Path(record["interpreter"]), dict(record["files"])
