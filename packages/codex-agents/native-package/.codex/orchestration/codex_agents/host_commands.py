"""The Codex command rule that lets a sandboxed Codex primary run the orchestrator.

Codex runs a primary's shell commands inside its workspace-write sandbox, which
blocks the learning collector's local socket and its state directory. A Codex
command rule (``prefix_rule`` with ``decision = "allow"``) in the user's rules
layer runs a matching command outside that sandbox without prompting.

The rule matches an exact argument prefix, so it pins an absolute, trusted base
interpreter, its isolation flags and an absolute CLI: the sandboxed model can
write files elsewhere in the workspace and in temporary directories, so a
relative path, a virtual environment's interpreter or a startup that reads the
environment or site-packages would let it run its own code outside the sandbox.
It allows only the primary-host subcommands below. Owner commands (registration,
attestation, owner keys, grants, backups, canaries, promotion and the like) are
never listed, so they stay sandboxed.

The CLI the rule names is not the project's own ``.codex/orchestration/cli.py``.
Codex keeps ``.codex`` read-only only at the top of a session's writable root, so
a session opened on a parent folder could rewrite the project's copy and run it
through the rule. The owner's setup instead copies the installed runtime into
``${CODEX_HOME:-~/.codex}/agent-orchestration/<project>/<runtime>/``, next to
the rules directory itself: a session that could change that copy could already
write a rule that allows anything. The copy is verified byte for byte against
the project's installed files, carries a binding to its project, and is what the
rule runs.

The rule leaves every later argument to the CLI. The copy therefore pins these
commands to its bound project, the default learning state and its installed
policy, refusing ``--project-root``, ``--root``, ``--state-root`` or ``--policy``
values that would move them (``cli._require_own_roots``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .host_paths import (
    PYTHON_ISOLATION_FLAGS,
    HostPathError,
    open_private_directories,
    remove_tree_at,
    temp_roots,
    trusted_location_problem,
    within,
)

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
MANIFEST_RELATIVE = ".codex/orchestration/install-manifest.json"
# Present only in a protected runtime copy, never in a project or the source tree.
BINDING_RELATIVE = ".codex/orchestration/runtime-binding.json"
RUNTIME_CONTAINER = "agent-orchestration"
BINDING_FORMAT = 1
MAX_MANIFEST_BYTES = 1_048_576
MAX_MANIFEST_FILES = 4096
MAX_RUNTIME_FILE_BYTES = 8 * 1_048_576
MAX_BINDING_BYTES = 4096
# The one package path under .claude, as the installer owns it.
CLAUDE_SKILL_PREFIX = (".claude", "skills", "agent-orchestration")
# Signed release data; the manifest records it but does not pin its bytes.
UNPINNED_RUNTIME_FILE = "learning-releases.json"
_HEX = frozenset("0123456789abcdef")
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
    if sys.platform == "darwin" and os.path.dirname(value) == "/usr/bin":
        # Every developer tool there is an xcrun shim, which reads a lookup
        # cache under $TMPDIR; the real interpreter it would run is elsewhere.
        raise HostCommandError("the Python interpreter cannot be trusted: it is an xcrun shim")
    untrusted = [*temp_roots(), *([project_root] if project_root is not None else [])]
    for path in (Path(value), Path(os.path.realpath(sys.base_prefix))):
        problem = trusted_location_problem(path, untrusted)
        if problem is not None:
            raise HostCommandError(f"the Python interpreter cannot be trusted: {problem}")
    return value


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    if not home.is_absolute():
        raise HostCommandError("CODEX_HOME is not an absolute path")
    return home


def project_digest(root: Path) -> str:
    """The short digest of a resolved project root that names its rule and copy."""

    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def _read_nofollow(root: Path, relative: str, limit: int) -> bytes | None:
    """Read `<root>/<relative>` through no-follow descriptors; None when absent.

    A symlink at or below `root`, a non-regular file or more than `limit` bytes
    refuses. `root` itself is opened as given (callers pass canonical paths).
    """

    parts = PurePosixPath(relative).parts
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostCommandError("a runtime file is unavailable") from exc
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        os.close(descriptor)
        return None
    except OSError as exc:
        os.close(descriptor)
        raise HostCommandError("a runtime file is a symlink or unavailable") from exc
    os.close(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise HostCommandError("a runtime file is not a regular file")
        chunks = []
        received = 0
        while chunk := os.read(handle, 1_048_576):
            received += len(chunk)
            if received > limit:
                raise HostCommandError("a runtime file is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)


def _manifest_files(raw: bytes) -> dict[str, str]:
    """The installer manifest's file map, with every path checked as the installer does."""

    try:
        manifest = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HostCommandError("the install manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "version", "files"}
        or manifest["format"] != 1
        or not isinstance(manifest["version"], str)
        or not isinstance(manifest["files"], dict)
        or len(manifest["files"]) > MAX_MANIFEST_FILES
    ):
        raise HostCommandError("the install manifest is invalid")
    for relative, digest in manifest["files"].items():
        parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
        if (
            not parts
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or "\\" in relative
            or relative in {MANIFEST_RELATIVE, BINDING_RELATIVE}
            or not (
                parts[0] in {".agents", ".codex"}
                or (len(parts) > 3 and parts[:3] == CLAUDE_SKILL_PREFIX)
            )
            or not isinstance(digest, str)
            or len(digest) != 64
            or not set(digest) <= _HEX
        ):
            raise HostCommandError("the install manifest is invalid")
    if CLI_RELATIVE.as_posix() not in manifest["files"]:
        raise HostCommandError("the install manifest does not list the CLI")
    return manifest["files"]


def _manifest_version(raw: bytes) -> str:
    _manifest_files(raw)
    return json.loads(raw)["version"]


def installed_runtime(root: Path) -> tuple[bytes, dict[str, bytes]]:
    """The project's install manifest and every file it lists, integrity-checked.

    Refused without a manifest: only an installed package has a runtime to copy
    and a rule to name.
    """

    raw = _read_nofollow(root, MANIFEST_RELATIVE, MAX_MANIFEST_BYTES)
    if raw is None:
        raise HostCommandError("the project has no install manifest; install the package first")
    contents: dict[str, bytes] = {}
    for relative, digest in sorted(_manifest_files(raw).items()):
        content = _read_nofollow(root, relative, MAX_RUNTIME_FILE_BYTES)
        if content is None:
            raise HostCommandError("an installed runtime file is missing")
        if (
            PurePosixPath(relative).name != UNPINNED_RUNTIME_FILE
            and hashlib.sha256(content).hexdigest() != digest
        ):
            raise HostCommandError("an installed runtime file differs from its manifest")
        contents[relative] = content
    return raw, contents


def _copy_root(root: Path, manifest: bytes, environ: Mapping[str, str] | None) -> Path:
    home = Path(os.path.realpath(codex_home(environ)))
    _checked_text(str(home), "Codex home")
    copy = (
        home / RUNTIME_CONTAINER / project_digest(root)
        / hashlib.sha256(manifest).hexdigest()
    )
    if any(within(copy, item) for item in [root, *temp_roots()]):
        raise HostCommandError(
            "the runtime copy would lie in the project or a temporary directory, "
            "which Codex sessions can change"
        )
    return copy


def runtime_copy_root(project_root: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Where the protected copy of the project's current installed runtime lives.

    `<CODEX_HOME>/agent-orchestration/<project digest>/<manifest digest>`: the
    manifest pins every runtime file's digest and the version, so a package
    update names a new copy and leaves the rule stale until setup runs again.
    """

    root, _cli = project_cli(project_root)
    raw = _read_nofollow(root, MANIFEST_RELATIVE, MAX_MANIFEST_BYTES)
    if raw is None:
        raise HostCommandError("the project has no install manifest; install the package first")
    return _copy_root(root, raw, environ)


def _binding_bytes(root: Path, manifest: bytes) -> bytes:
    version = _manifest_version(manifest)
    binding = {
        "format": BINDING_FORMAT,
        "project_root": str(root),
        "runtime_digest": hashlib.sha256(manifest).hexdigest(),
        "version": version,
    }
    return (json.dumps(binding, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _parse_binding(raw: bytes) -> dict[str, str]:
    try:
        binding = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HostCommandError("the protected runtime binding is invalid") from exc
    if (
        not isinstance(binding, dict)
        or set(binding) != {"format", "project_root", "runtime_digest", "version"}
        or binding["format"] != BINDING_FORMAT
        or not isinstance(binding["project_root"], str)
        or not os.path.isabs(binding["project_root"])
        or not isinstance(binding["runtime_digest"], str)
        or len(binding["runtime_digest"]) != 64
        or not set(binding["runtime_digest"]) <= _HEX
        or not isinstance(binding["version"], str)
    ):
        raise HostCommandError("the protected runtime binding is invalid")
    _checked_text(binding["project_root"], "bound project root")
    return binding


def bound_project_root(orchestration_root: Path) -> Path | None:
    """The project a protected runtime copy serves, from its binding; None elsewhere.

    The binding, the manifest and the code all sit in the owner-only copy, so
    the bound root is as trustworthy as the CLI reading it. The copy must sit
    at exactly the path its binding implies, its manifest must have the bound
    digest, and the project must still be a canonical directory. A project or
    source tree has no binding and keeps using its own location.
    """

    copy = orchestration_root.parent.parent
    if not os.path.lexists(copy / BINDING_RELATIVE):
        return None
    raw = _read_nofollow(copy, BINDING_RELATIVE, MAX_BINDING_BYTES)
    if raw is None:
        raise HostCommandError("the protected runtime binding is unavailable")
    binding = _parse_binding(raw)
    root = Path(binding["project_root"])
    if (
        copy.name != binding["runtime_digest"]
        or copy.parent.name != project_digest(root)
        or copy.parent.parent.name != RUNTIME_CONTAINER
    ):
        raise HostCommandError("the protected runtime copy is not where its binding says")
    manifest = _read_nofollow(copy, MANIFEST_RELATIVE, MAX_MANIFEST_BYTES)
    if (
        manifest is None
        or hashlib.sha256(manifest).hexdigest() != binding["runtime_digest"]
        or _manifest_version(manifest) != binding["version"]
    ):
        raise HostCommandError("the protected runtime copy differs from its binding")
    if Path(os.path.realpath(root)) != root or not root.is_dir():
        raise HostCommandError("the bound project is unavailable")
    return root


def runtime_copy_problem(copy: Path, root: Path) -> str | None:
    """Why `copy` is not the protected copy of `root`'s current runtime, or None.

    The copy's binding must name `root` and the copy's own manifest digest,
    the copy's manifest must be the project's, every file the manifest lists
    must be byte-for-byte the project's, and the copy must sit where no
    workspace or temporary directory reaches. Messages carry no paths.
    """

    try:
        problem = trusted_location_problem(copy / CLI_RELATIVE, [root, *temp_roots()])
        if problem is not None:
            return f"the runtime copy cannot be trusted: {problem}"
        raw = _read_nofollow(copy, BINDING_RELATIVE, MAX_BINDING_BYTES)
        if raw is None:
            return "the runtime copy has no binding"
        binding = _parse_binding(raw)
        if binding["project_root"] != str(root) or binding["runtime_digest"] != copy.name:
            return "the runtime copy is bound to another project or runtime"
        project_manifest = _read_nofollow(root, MANIFEST_RELATIVE, MAX_MANIFEST_BYTES)
        copy_manifest = _read_nofollow(copy, MANIFEST_RELATIVE, MAX_MANIFEST_BYTES)
        if project_manifest is None:
            return "the project has no install manifest"
        if copy_manifest != project_manifest:
            return "the runtime copy's manifest differs from the project's"
        if hashlib.sha256(copy_manifest).hexdigest() != copy.name:
            return "the runtime copy's manifest differs from its name"
        if _manifest_version(copy_manifest) != binding["version"]:
            return "the runtime copy's version differs from its binding"
        for relative in sorted(_manifest_files(copy_manifest)):
            ours = _read_nofollow(copy, relative, MAX_RUNTIME_FILE_BYTES)
            theirs = _read_nofollow(root, relative, MAX_RUNTIME_FILE_BYTES)
            if ours is None or theirs is None or ours != theirs:
                return "the runtime copy differs from the project's installed files"
    except HostCommandError as exc:
        return str(exc)
    return None


def argv_prefix(
    project_root: Path,
    interpreter: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """The exact command prefix a Codex primary must use: it names the protected copy."""

    root, _cli = project_cli(project_root)
    python = interpreter_path(interpreter, project_root=root)
    copy = runtime_copy_root(root, environ)
    return [python, *PYTHON_FLAGS, str(copy / CLI_RELATIVE)]


def rule_file_name(project_root: Path) -> str:
    root, _cli = project_cli(project_root)
    return f"{RULE_FILE_PREFIX}{project_digest(root)}.rules"


def rule_path(project_root: Path, environ: Mapping[str, str] | None = None) -> Path:
    return codex_home(environ) / "rules" / rule_file_name(project_root)


def render_rule(
    project_root: Path,
    interpreter: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The complete rule file for one project, with Codex's own load-time self-tests."""

    root, cli = project_cli(project_root)
    prefix = argv_prefix(root, interpreter, environ)
    python = prefix[0]
    relative_cli = CLI_RELATIVE.as_posix()
    commands = "".join(
        f"            {_starlark_string(command)},\n" for command in PRIMARY_HOST_COMMANDS
    )
    match = [[*prefix, "learning-status", "--project-root", str(root)]]
    not_match = [
        # An owner command stays in the sandbox.
        [*prefix, "learning-register", "--project-root", str(root)],
        # The project's own CLI stays sandboxed: a session can change it.
        [python, *PYTHON_FLAGS, str(cli), "learning-status", "--project-root", str(root)],
        # Relative forms could name a look-alike the model wrote.
        ["python3", relative_cli, "learning-status", "--project-root", str(root)],
        [python, *PYTHON_FLAGS, relative_cli, "learning-status", "--project-root", str(root)],
        # Without the isolation flags, startup could load code the model wrote.
        [python, prefix[-1], "learning-status", "--project-root", str(root)],
    ]
    return (
        "# Agent orchestration: Codex command rule for one project.\n"
        "# Written by ./setup.sh (Codex commands step) with the owner's consent.\n"
        "# It lets Codex run only this project's everyday orchestrator commands\n"
        "# outside the Codex sandbox, so they can reach the local learning collector.\n"
        "# They run from an owner-only copy of the installed runtime, kept next to\n"
        "# this rules directory, which no project workspace can change.\n"
        "# Owner commands are not listed and stay sandboxed.\n"
        "# To remove it, delete this file and the runtime copy it names;\n"
        "# ./setup.sh can create both again.\n"
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
    """``installed``, ``missing`` or ``stale``.

    ``installed`` only when the rule file is exactly a fresh render, which
    names the protected copy of the project's current install manifest, and
    that copy is bound to this project and byte-for-byte its installed files.
    A rule naming the project's own CLI (written before the copy existed), a
    missing or differing copy, an updated package and a project without an
    install manifest are all ``stale``; ``missing`` means no rule file.
    """

    root, _cli = project_cli(project_root)
    python = interpreter_path(interpreter, project_root=root)
    name = rule_file_name(root)
    try:
        directory = _open_rules_directory(codex_home(environ), create=False)
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
    try:
        copy = runtime_copy_root(root, environ)
        expected = render_rule(root, python, environ).encode("utf-8")
    except HostCommandError:
        return "stale"
    if current != expected or runtime_copy_problem(copy, root) is not None:
        return "stale"
    return "installed"


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

    content = render_rule(project_root, interpreter, environ).encode("utf-8")
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


def _write_new_file(directory: int, relative: str, content: bytes) -> None:
    """Create `relative` under `directory` (0700 directories, a 0600 file), never following."""

    parts = PurePosixPath(relative).parts
    descriptor = os.dup(directory)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=descriptor,
        )
        try:
            os.fchmod(handle, 0o600)
            view = memoryview(content)
            while view:
                view = view[os.write(handle, view):]
            os.fsync(handle)
        finally:
            os.close(handle)
    finally:
        os.close(descriptor)


def _validate_runtime(cli: Path) -> None:
    """Require the installed runtime at `cli` to be exactly this package's recorded runtime.

    The manifest's file digests must hold, and the version and collector
    runtime digest must equal the ones this package records for itself (and
    `runtime-releases.json` records for its version).
    """

    from .learning_store import CollectorAuthorizationError, validate_collector_runtime

    try:
        validate_collector_runtime(cli)
    except CollectorAuthorizationError as exc:
        raise HostCommandError(
            "the installed runtime differs from this package's recorded runtime; "
            "update the install first"
        ) from exc


def install_runtime_copy(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> tuple[Path, str]:
    """Copy the project's installed runtime to its protected location; return it and what happened.

    ``created``, ``unchanged`` (a verified copy was already there) or
    ``replaced`` (a copy at that name failed verification and was rebuilt).
    The copy is staged in a private sibling directory, verified, then renamed
    into place, so the rule never names a partial copy. Every directory is
    created owner-only through no-follow descriptors.
    """

    root, cli = project_cli(project_root)
    manifest, files = installed_runtime(root)
    _validate_runtime(cli)
    copy = _copy_root(root, manifest, environ)
    home = copy.parent.parent.parent
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        container = open_private_directories(
            home, (RUNTIME_CONTAINER, copy.parent.name), create=True
        )
    except (OSError, HostPathError) as exc:
        raise HostCommandError(f"the runtime copy directory is unavailable: {exc}") from exc
    if container is None:  # pragma: no cover - create=True always opens or raises.
        raise HostCommandError("the runtime copy directory is unavailable")
    outcome = "created"
    token = secrets.token_hex(8)
    staging = f".{copy.name}.{token}.tmp"
    aside = f".{copy.name}.{token}.old"
    try:
        if os.path.lexists(copy):
            if runtime_copy_problem(copy, root) is None:
                return copy, "unchanged"
            os.rename(copy.name, aside, src_dir_fd=container, dst_dir_fd=container)
            outcome = "replaced"
        os.mkdir(staging, 0o700, dir_fd=container)
        staged = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=container)
        try:
            for relative, content in files.items():
                _write_new_file(staged, relative, content)
            _write_new_file(staged, MANIFEST_RELATIVE, manifest)
            _write_new_file(staged, BINDING_RELATIVE, _binding_bytes(root, manifest))
        finally:
            os.close(staged)
        try:
            os.rename(staging, copy.name, src_dir_fd=container, dst_dir_fd=container)
        except OSError:
            # Another setup published this copy first; it must verify like any other.
            if runtime_copy_problem(copy, root) is not None:
                raise HostCommandError("a differing runtime copy appeared while setup ran") from None
            outcome = "unchanged"
    except OSError as exc:
        raise HostCommandError("the runtime copy could not be written") from exc
    finally:
        for leftover in (staging, aside):
            try:
                remove_tree_at(container, leftover)
            except OSError:
                pass
        os.close(container)
    problem = runtime_copy_problem(copy, root)
    if problem is not None:
        raise HostCommandError(f"the runtime copy could not be verified: {problem}")
    _validate_runtime(copy / CLI_RELATIVE)
    return copy, outcome


def prune_runtime_copies(
    project_root: Path, keep: Path, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Remove this project's other runtime copies once the rule names `keep`."""

    root, _cli = project_cli(project_root)
    home = Path(os.path.realpath(codex_home(environ)))
    container = open_private_directories(
        home, (RUNTIME_CONTAINER, project_digest(root)), create=False
    )
    if container is None:
        return []
    removed = []
    try:
        with os.scandir(container) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if name != keep.name and remove_tree_at(container, name):
                removed.append(str(keep.parent / name))
    finally:
        os.close(container)
    return removed


def install_rule(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
    interpreter: str | None = None,
    *,
    replace: bool = False,
) -> dict[str, str]:
    """Owner setup: write the protected runtime copy, then the rule that names it.

    The copy comes first, so the rule never names a missing runtime. Once the
    rule names it (created, replaced or unchanged), the project's older copies
    are removed; when another rule is left in place (``differs``), they stay.
    """

    copy, copy_outcome = install_runtime_copy(project_root, environ)
    outcome = write_rule(project_root, environ, interpreter, replace=replace)
    if outcome != "differs":
        try:
            prune_runtime_copies(project_root, copy, environ)
        except (OSError, HostPathError):
            pass  # an older copy no rule names is inert; the next setup retries
    return {
        "codex_rule": outcome,
        "runtime_copy": str(copy),
        "runtime_copy_status": copy_outcome,
    }


def remove_rule(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Owner removal: delete this project's rule file and every runtime copy for it.

    The project need not still hold an installed package (removal may come
    after the package files are gone), so only its resolved path is used.
    Symlinks are never followed: a symlinked rule or copy directory refuses.
    Returns the paths removed, or an empty list when there was nothing.
    """

    root = Path(os.path.realpath(Path(project_root).expanduser()))
    _checked_text(str(root), "project root")
    digest = project_digest(root)
    home = codex_home(environ)
    removed: list[str] = []
    directory = _open_rules_directory(home, create=False)
    if directory is not None:
        name = f"{RULE_FILE_PREFIX}{digest}.rules"
        try:
            if _read_rule(directory, name) is not None:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                removed.append(str(home / "rules" / name))
        finally:
            os.close(directory)
    canonical = Path(os.path.realpath(home))
    if not os.path.lexists(canonical / RUNTIME_CONTAINER):
        return removed
    try:
        container = open_private_directories(canonical, (RUNTIME_CONTAINER,), create=False)
    except HostPathError as exc:
        raise HostCommandError(f"the runtime copy directory cannot be removed: {exc}") from exc
    if container is None:
        return removed
    try:
        if remove_tree_at(container, digest):
            removed.append(str(canonical / RUNTIME_CONTAINER / digest))
    finally:
        os.close(container)
    return removed
