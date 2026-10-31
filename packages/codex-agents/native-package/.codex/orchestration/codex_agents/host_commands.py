"""The Codex command rule that lets a sandboxed Codex primary run the orchestrator.

Codex runs a primary's shell commands inside its workspace-write sandbox, which
blocks the learning collector's local socket and its state directory. A Codex
command rule (``prefix_rule`` with ``decision = "allow"``) in the user's rules
layer runs a matching command outside that sandbox without prompting.

The rule matches an exact argument prefix, so it pins the absolute interpreter
and the absolute project CLI: the sandboxed model can write files elsewhere in
the workspace and in temporary directories, and a relative path would let it run
a look-alike outside the sandbox. It allows only the primary-host subcommands
below. Owner commands (registration, attestation, owner keys, grants, backups,
canaries, promotion and the like) are never listed, so they stay sandboxed.

What the rule cannot check: it trusts the interpreter at that path, and it
leaves every later argument, such as ``--project-root`` or ``--state-root``, to
the CLI's own checks.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
import unicodedata
from collections.abc import Mapping
from pathlib import Path

# The subcommands a primary host runs during an orchestration. Defined once; the
# rule's allowlist and its tests read it from here.
PRIMARY_HOST_COMMANDS = (
    "validate",
    "inventory",
    "route",
    "learning-route",
    "validate-assignment",
    "result-preflight",
    "validate-result",
    "learning-status",
    "learning-open-orchestration",
    "learning-checkpoint-orchestration",
    "learning-pause-orchestration",
    "learning-resume-orchestration",
    "learning-close-orchestration",
    "learning-orchestration-report",
    "learning-open-host-run",
    "learning-bind-host-agent",
    "learning-observe-host-run",
    "learning-classify-host-run-abandoned",
    "learning-record-host-result",
    "dispatch",
    "provider-observe",
    "learning-run-operational-checks",
    "learning-record-operational-qa",
)
CLI_RELATIVE = Path(".codex") / "orchestration" / "cli.py"
RULE_FILE_PREFIX = "agent-orchestration-"
RULE_JUSTIFICATION = (
    "Agent orchestration: this project's everyday orchestrator commands need the "
    "local learning collector, which the Codex sandbox blocks."
)
MAX_RULE_BYTES = 65_536
# Line breaks, other control characters, invisible format characters and
# unpaired surrogates never reach a rule file, even quoted.
_REFUSED_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


class HostCommandError(RuntimeError):
    """A rule cannot be built or written safely; messages never carry paths."""


def _checked_text(value: str, what: str) -> str:
    if not value or any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in value):
        raise HostCommandError(f"{what} contains a character a Codex rule cannot hold")
    return value


def _starlark_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _starlark_list(values: list[str]) -> str:
    return "[" + ", ".join(_starlark_string(value) for value in values) + "]"


def project_cli(project_root: Path) -> tuple[Path, Path]:
    """Return the resolved project root and its installed CLI, both absolute."""

    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise HostCommandError("project root is unavailable") from exc
    if not root.is_dir():
        raise HostCommandError("project root is not a directory")
    _checked_text(str(root), "project root")
    cli = root / CLI_RELATIVE
    try:
        info = cli.lstat()
        resolved = cli.resolve(strict=True)
    except OSError as exc:
        raise HostCommandError("project CLI is not installed") from exc
    if not stat.S_ISREG(info.st_mode) or resolved != cli:
        raise HostCommandError("project CLI is a symlink or not a regular file")
    return root, cli


def interpreter_path(interpreter: str | None = None) -> str:
    """The interpreter running this code: ``sys.executable`` as Python reports it.

    It is never resolved further; a realpath names a versioned build directory
    that the next patch upgrade replaces. The wizard and a Codex primary both
    start ``python3`` from PATH, so they report the same path, and any other
    interpreter makes the rule stale.
    """

    value = sys.executable if interpreter is None else interpreter
    if not value or not os.path.isabs(value):
        raise HostCommandError("interpreter path is not absolute")
    return _checked_text(value, "interpreter path")


def argv_prefix(project_root: Path, interpreter: str | None = None) -> list[str]:
    """The exact command prefix a Codex primary must use for every CLI call."""

    _root, cli = project_cli(project_root)
    return [interpreter_path(interpreter), str(cli)]


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    if not home.is_absolute():
        raise HostCommandError("CODEX_HOME is not an absolute path")
    return home


def rule_file_name(project_root: Path) -> str:
    root, _cli = project_cli(project_root)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return f"{RULE_FILE_PREFIX}{digest}.rules"


def rule_path(project_root: Path, environ: Mapping[str, str] | None = None) -> Path:
    return codex_home(environ) / "rules" / rule_file_name(project_root)


def render_rule(project_root: Path, interpreter: str | None = None) -> str:
    """The complete rule file for one project, with Codex's own load-time self-tests."""

    root, cli = project_cli(project_root)
    python = interpreter_path(interpreter)
    prefix = [python, str(cli)]
    relative_cli = CLI_RELATIVE.as_posix()
    commands = "".join(
        f"            {_starlark_string(command)},\n" for command in PRIMARY_HOST_COMMANDS
    )
    match = [[*prefix, "learning-status", "--project-root", str(root)]]
    not_match = [
        # An owner command stays in the sandbox.
        [*prefix, "learning-register", "--project-root", str(root)],
        # Relative forms could name a look-alike the model wrote.
        ["python3", relative_cli, "learning-status", "--project-root", str(root)],
        [python, relative_cli, "learning-status", "--project-root", str(root)],
    ]
    return (
        "# Agent orchestration: Codex command rule for one project.\n"
        "# Written by ./setup.sh (Codex commands step) with the owner's consent.\n"
        "# It lets Codex run only this project's everyday orchestrator commands\n"
        "# outside the Codex sandbox, so they can reach the local learning collector.\n"
        "# Owner commands are not listed and stay sandboxed.\n"
        "# To remove it, delete this file; ./setup.sh can create it again.\n"
        "\n"
        "prefix_rule(\n"
        "    pattern = [\n"
        f"        {_starlark_string(python)},\n"
        f"        {_starlark_string(str(cli))},\n"
        "        [\n"
        f"{commands}"
        "        ],\n"
        "    ],\n"
        '    decision = "allow",\n'
        f"    justification = {_starlark_string(RULE_JUSTIFICATION)},\n"
        "    match = [\n"
        + "".join(f"        {_starlark_list(example)},\n" for example in match)
        + "    ],\n"
        "    not_match = [\n"
        + "".join(f"        {_starlark_list(example)},\n" for example in not_match)
        + "    ],\n"
        ")\n"
    )


def _open_rules_directory(home: Path, *, create: bool) -> int | None:
    """Open the rules directory without following a symlink; None when absent.

    Codex home itself may be a symlink (dotfile setups often link it); the rules
    directory and the rule file may not.
    """

    rules = home / "rules"
    try:
        info = os.lstat(rules)
    except FileNotFoundError:
        if not create:
            return None
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.mkdir(rules, 0o700)
        info = os.lstat(rules)
    if stat.S_ISLNK(info.st_mode):
        raise HostCommandError("Codex rules directory is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise HostCommandError("Codex rules directory is not a directory")
    descriptor = os.open(rules, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(descriptor).st_uid != os.geteuid():
        os.close(descriptor)
        raise HostCommandError("Codex rules directory belongs to another user")
    return descriptor


def _read_rule(directory: int, name: str) -> bytes | None:
    """Read the existing rule without following a symlink; None when absent."""

    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise HostCommandError("Codex rule file is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise HostCommandError("Codex rule file is not a regular file")
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HostCommandError("Codex rule file is not a regular file")
        return os.read(descriptor, MAX_RULE_BYTES + 1)
    finally:
        os.close(descriptor)


def rule_status(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
    interpreter: str | None = None,
) -> str:
    """``installed``, ``missing`` or ``stale``, comparing the file with a fresh render."""

    expected = render_rule(project_root, interpreter).encode("utf-8")
    name = rule_file_name(project_root)
    home = codex_home(environ)
    try:
        directory = _open_rules_directory(home, create=False)
        if directory is None:
            return "missing"
        try:
            current = _read_rule(directory, name)
        finally:
            os.close(directory)
    except HostCommandError:
        # A symlinked or foreign rule is never reported as ours.
        return "stale"
    if current is None:
        return "missing"
    return "installed" if current == expected else "stale"


def write_rule(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
    interpreter: str | None = None,
    *,
    replace: bool = False,
) -> str:
    """Write the rule atomically (mode 0600) and return what happened.

    ``unchanged`` when the file already holds this rule, ``created`` or
    ``replaced`` after a write, and ``differs`` when another rule is there and
    ``replace`` was not given. Symlinks are refused, never followed.
    """

    content = render_rule(project_root, interpreter).encode("utf-8")
    name = rule_file_name(project_root)
    directory = _open_rules_directory(codex_home(environ), create=True)
    if directory is None:  # pragma: no cover - create=True always opens or raises.
        raise HostCommandError("Codex rules directory is unavailable")
    try:
        current = _read_rule(directory, name)
        if current == content:
            return "unchanged"
        if current is not None and not replace:
            return "differs"
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(content)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            raise
        os.fsync(directory)
        return "created" if current is None else "replaced"
    finally:
        os.close(directory)
