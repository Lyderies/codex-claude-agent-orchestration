"""The Codex command rule that lets a sandboxed Codex primary run the orchestrator.

Codex runs a primary's shell commands inside its workspace-write sandbox, which
blocks the learning collector's local socket and its state directory. A Codex
command rule (``prefix_rule`` with ``decision = "allow"``) in the user's rules
layer runs a matching command outside that sandbox without prompting.

The rule matches an exact argument prefix, so it pins an absolute, trusted base
interpreter, its isolation flags and the absolute project CLI: the sandboxed
model can write files elsewhere in the workspace and in temporary directories,
so a relative path, a virtual environment's interpreter or a startup that reads
the environment or site-packages would let it run its own code outside the
sandbox. It allows only the primary-host subcommands below. Owner commands
(registration, attestation, owner keys, grants, backups, canaries, promotion and
the like) are never listed, so they stay sandboxed.

The rule leaves every later argument to the CLI. An installed CLI therefore pins
these commands to its own project, the default learning state and its installed
policy, refusing ``--project-root``, ``--root``, ``--state-root`` or ``--policy``
values that would move them (``cli._require_own_roots``).
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

from .host_paths import PYTHON_ISOLATION_FLAGS, temp_roots, trusted_location_problem, within

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
# Isolated mode, no bytecode written into `.codex`, and no site-packages (see
# host_paths): the CLI needs only the standard library and its own package.
PYTHON_FLAGS = PYTHON_ISOLATION_FLAGS
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


class OwnRootRefused(HostCommandError):
    """An installed CLI's primary-host command named another project, state or policy.

    Messages name the refused option, never its value.
    """


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
    # The rule runs this file outside the sandbox, so a project a Codex session can
    # rewrite wholesale (one in a temporary directory) never gets one, and neither
    # does a CLI another user could replace.
    temporary = list(temp_roots())
    if any(within(root, item) for item in temporary):
        raise HostCommandError(
            "the project lies in a temporary directory, which Codex sessions can change"
        )
    problem = trusted_location_problem(cli, temporary)
    if problem is not None:
        raise HostCommandError(f"the project CLI cannot be trusted: {problem}")
    return root, cli


def interpreter_path(
    interpreter: str | None = None, *, project_root: Path | None = None
) -> str:
    """The trusted base interpreter the rule names, as Python reports it.

    It is ``sys.executable``, or the base interpreter of a virtual environment,
    whose files the project or the model could change, pinned as its realpath:
    no symlink on the way remains that could later be retargeted, and a Python
    upgrade that moves the build makes the rule stale until setup runs again.
    It is refused when it or its standard library prefix is inside the project
    or a temporary directory, or when any component belongs to another user or
    is writable by anyone else. The wizard and a Codex primary both start
    ``python3`` from PATH, so they resolve to the same path.

    An explicit `interpreter` is a test seam: it is checked as text only.
    """

    if interpreter is not None:
        if not os.path.isabs(interpreter):
            raise HostCommandError("interpreter path is not absolute")
        return _checked_text(interpreter, "interpreter path")
    value = sys.executable
    if sys.prefix != sys.base_prefix:
        value = getattr(sys, "_base_executable", "") or ""
        if not value or within(
            Path(os.path.realpath(value)), Path(os.path.realpath(sys.prefix))
        ):
            raise HostCommandError(
                "Python runs in a virtual environment; run setup with the base Python"
            )
    if not value or not os.path.isabs(value):
        raise HostCommandError("interpreter path is not absolute")
    value = os.path.realpath(value)
    _checked_text(value, "interpreter path")
    untrusted = [*temp_roots(), *([project_root] if project_root is not None else [])]
    for path in (Path(value), Path(os.path.realpath(sys.base_prefix))):
        problem = trusted_location_problem(path, untrusted)
        if problem is not None:
            raise HostCommandError(f"the Python interpreter cannot be trusted: {problem}")
    return value


def argv_prefix(project_root: Path, interpreter: str | None = None) -> list[str]:
    """The exact command prefix a Codex primary must use for every CLI call."""

    root, cli = project_cli(project_root)
    return [interpreter_path(interpreter, project_root=root), *PYTHON_FLAGS, str(cli)]


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
    prefix = argv_prefix(root, interpreter)
    python = prefix[0]
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
        [python, *PYTHON_FLAGS, relative_cli, "learning-status", "--project-root", str(root)],
        # Without the isolation flags, startup could load code the model wrote.
        [python, str(cli), "learning-status", "--project-root", str(root)],
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
        + "".join(f"        {_starlark_string(token)},\n" for token in prefix)
        + "        [\n"
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
        try:
            os.mkdir(rules, 0o700)
        except FileExistsError:
            pass  # another setup created it first; it is checked below like any other
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
    ``replace`` was not given, including one that appears while this call runs:
    a new rule is published with a hard link, which never overwrites. Symlinks
    are refused, never followed.
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
            if current is None:
                # Publish without replacing: another setup may have written a rule
                # since it was read, and only --replace may overwrite one.
                try:
                    os.link(
                        temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    outcome = (
                        "unchanged" if _read_rule(directory, name) == content else "differs"
                    )
                else:
                    outcome = "created"
            else:
                os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                outcome = "replaced"
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.fsync(directory)
        return outcome
    finally:
        os.close(directory)
