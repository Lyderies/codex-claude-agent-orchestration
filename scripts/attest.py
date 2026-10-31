#!/usr/bin/env python3
"""Guided owner billing attestation for an orchestrated project.

Usage: ``python3 -I -B -S scripts/attest.py --target <project> [--state-root <dir>]``, or
``python3 -I -B -S scripts/attest.py --change-passphrase`` to change the owner key's passphrase.

Admission requires, per provider account, an owner-signed statement that the account
cannot be charged beyond its subscription. This command walks the human owner through
it. What proves a person signed is the owner key's passphrase: the key is refused unless
it is encrypted, and signing runs without ssh-agent so a loaded key cannot sign silently.
The command also insists on an interactive terminal, but that alone does not keep a
program out; the passphrase does.

The isolation flags, which ./setup.sh always passes, keep anything in the working directory,
the environment or site-packages out of this process; it imports only the standard library. Its working files live in a host-private folder that no AI agent's sandbox can
write, it runs the project's orchestration CLI only after checking that CLI byte for byte
against this repository's copy, and it signs only the statement the owner approved.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

NAMESPACE = "codex-agents-owner-v1"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
DEFAULT_KEY = "~/.ssh/codex-agents-owner"
CLI_RELATIVE = Path(".codex/orchestration/cli.py")
ROUTING_RELATIVE = Path(".codex/orchestration/provider-routing.json")
QUESTIONS = {
    "claude": "Is extra usage (pay-as-you-go overage) turned off for this Claude account?",
    "codex_buy": "Is buying Codex credits turned off for this account?",
    "codex_spend": "Is spending any existing Codex credit balance turned off for this account?",
}
KEY_ADVICE = (
    "The owner key proves that a person approved the billing statement.\n"
    "The passphrase is what proves a person signed: without one, any program on this machine,\n"
    "including an AI agent, could sign. Keys without a passphrase are refused."
)

_TEMP_DIRS: set[Path] = set()
_CLEANUP_FAILURES: list[Path] = []

# This repository's own copy of the native package: the bytes an installed project's
# orchestration runtime must match before an owner tool runs it.
NATIVE_PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "codex-agents" / "native-package"
RUNTIME_RELATIVE = Path(".codex") / "orchestration"
MANIFEST_RELATIVE = RUNTIME_RELATIVE / "install-manifest.json"
# Every Python an owner tool starts gets these: isolated mode (no environment variables,
# user site-packages or script directory on the path), no bytecode written, no
# site-packages, and no cached bytecode read from the project's __pycache__ folders (the
# cache prefix points below /dev/null, where no folder can exist).
PYTHON_FLAGS = ("-I", "-B", "-S", "-X", "pycache_prefix=/dev/null")
GETCONF = "/usr/bin/getconf"
SCRATCH_CONTAINER = "agent-orchestration-owner"
SCRATCH_BASE: str | None = None  # tests only; the platform's private folder otherwise
MACOS_ADMIN_GROUP = 80
_IMPORTABLE = (".py", ".pyc", ".pyo", ".pyw", ".so", ".pyd")


class AttestError(RuntimeError):
    """A step failed in a way the owner should read about; never carries identifiers."""


# --- terminal ---------------------------------------------------------------------------


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# --- host-private scratch ---------------------------------------------------------------
# Working files (the public key being enrolled, the statement being signed, its signature)
# never go to /tmp or $TMPDIR: a sandboxed AI agent running as this same user can write
# there and could swap a file between two steps. They live in a fresh owner-only folder
# inside the per-user cache folder on macOS, or $XDG_RUNTIME_DIR elsewhere, which those
# sandboxes cannot write. This mirrors host_paths.private_directory in the runtime.


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _darwin_directory(name: str) -> str | None:
    try:
        info = os.lstat(GETCONF)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            return None
        completed = subprocess.run([GETCONF, name], env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and os.path.isabs(value) else None


def temp_roots() -> set[Path]:
    """Every temporary folder a sandboxed session may write, resolved."""

    candidates = ["/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or ""]
    if sys.platform == "darwin":
        candidates.append(_darwin_directory("DARWIN_USER_TEMP_DIR") or "")
    return {Path(os.path.realpath(item)) for item in candidates if os.path.isabs(item)}


NO_SCRATCH = (
    "no private working folder is available (macOS: your user cache folder; Linux: "
    "$XDG_RUNTIME_DIR, which a normal login session sets). Working files are never put in a "
    "temporary folder, where an AI agent could change them"
)


def scratch_base(project: Path | None = None) -> Path:
    """The existing host folder that holds private scratch, outside every sandbox's reach."""

    if SCRATCH_BASE is not None:
        value: str | None = SCRATCH_BASE
    elif sys.platform == "darwin":
        value = _darwin_directory("DARWIN_USER_CACHE_DIR")
    else:
        value = os.environ.get("XDG_RUNTIME_DIR")
    if not value or not os.path.isabs(value):
        raise AttestError(NO_SCRATCH)
    resolved = Path(os.path.realpath(value))
    untrusted = [*temp_roots(), *([Path(os.path.realpath(project))] if project else [])]
    if any(within(resolved, root) or within(root, resolved) for root in untrusted):
        raise AttestError(NO_SCRATCH)
    return Path(value)


def _component_problem(info: os.stat_result) -> bool:
    if info.st_uid not in (0, os.geteuid()):
        return True
    others = info.st_mode & stat.S_IWOTH
    group = info.st_mode & stat.S_IWGRP and not (
        sys.platform == "darwin" and info.st_gid == MACOS_ADMIN_GROUP)
    return bool(others or group) and not (
        stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX and info.st_uid == 0)


def _open_verified_directory(path: Path) -> tuple[int, Path]:
    """Open an existing folder by descriptors, never following a symlink.

    Root-owned system links (such as /var on macOS) are resolved first; any other symlink
    refuses. Every component must belong to you or root and be writable by no one else.
    """

    full = Path(os.path.abspath(path))
    for _ in range(40):
        current = Path(full.anchor)
        for index, part in enumerate(full.parts[1:]):
            current = current / part
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise AttestError(NO_SCRATCH) from exc
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != 0:
                    raise AttestError("the private working folder's path passes through a symbolic link")
                full = Path(os.path.realpath(current), *full.parts[index + 2:])
                break
        else:
            break
    else:
        raise AttestError("the private working folder's path has too many symbolic links")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in full.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if _component_problem(os.fstat(descriptor)):
                raise AttestError("the private working folder's path can be changed by other users")
    except OSError as exc:
        os.close(descriptor)
        raise AttestError("the private working folder's path passes through a symbolic link") from exc
    except AttestError:
        os.close(descriptor)
        raise
    return descriptor, full


def _private_child(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise AttestError("the private working folder is not yours alone")
    return descriptor


def make_scratch(prefix: str, project: Path | None = None) -> Path:
    """Create a fresh owner-only folder at <private base>/agent-orchestration-owner/<prefix><hex>."""

    if not re.fullmatch(r"[a-z]+-", prefix):
        raise AttestError("invalid working folder name")
    base_fd, canonical = _open_verified_directory(scratch_base(project))
    try:
        try:
            container_fd = _private_child(base_fd, SCRATCH_CONTAINER, create=True)
        except OSError as exc:
            raise AttestError(NO_SCRATCH) from exc
        try:
            for _ in range(64):
                name = f"{prefix}{secrets.token_hex(6)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=container_fd)
                except FileExistsError:
                    continue
                os.close(_private_child(container_fd, name, create=False))
                path = canonical / SCRATCH_CONTAINER / name
                _TEMP_DIRS.add(path)
                return path
            raise AttestError("a private working folder could not be created")
        finally:
            os.close(container_fd)
    finally:
        os.close(base_fd)


def remove_scratch(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        if path not in _CLEANUP_FAILURES:
            _CLEANUP_FAILURES.append(path)
        print("Warning: a private working folder could not be removed; delete it yourself:", path,
              file=sys.stderr)
        return False
    _TEMP_DIRS.discard(path)
    return True


# --- the trusted interpreter and the project's runtime ----------------------------------


def trusted_python(project: Path | None = None) -> str:
    """The interpreter every child Python runs on: this one, as its real file.

    The real file, never a symlink that could be retargeted, and never a virtual
    environment's copy (whose pyvenv.cfg could point it at other code). It may not lie in a
    temporary folder or the project, which a sandboxed AI agent can write.
    """

    executable = Path(os.path.realpath(sys.executable)) if sys.executable else None
    if executable is None or not executable.is_absolute() or not executable.is_file():
        raise AttestError("this Python's own program file cannot be found")
    untrusted = [*temp_roots(), *([Path(os.path.realpath(project))] if project else [])]
    if any(within(executable, root) for root in untrusted):
        raise AttestError("this Python sits in a temporary folder or in the project, where an AI "
                          "agent could change it. Run this with a system Python, such as Homebrew's "
                          "or python.org's")
    if any((folder / "pyvenv.cfg").exists() for folder in (executable.parent, executable.parent.parent)):
        raise AttestError("this Python belongs to a virtual environment. Run this with a system "
                          "Python, such as Homebrew's or python.org's")
    return str(executable)


def cli_command(project: Path, args: list[str]) -> list[str]:
    """The isolated command line that runs the project's installed CLI."""

    return [trusted_python(project), *PYTHON_FLAGS, str(project / CLI_RELATIVE), *args]


def _runtime_bundle() -> dict[str, bytes]:
    """This repository's copy of every runtime file, keyed by project-relative path."""

    source = NATIVE_PACKAGE / RUNTIME_RELATIVE
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise AttestError("this setup's own package copy contains a symbolic link")
        if path.is_file():
            relative = path.relative_to(NATIVE_PACKAGE).as_posix()
            if relative != MANIFEST_RELATIVE.as_posix():
                files[relative] = path.read_bytes()
    if not files:
        raise AttestError("this setup's own package copy is missing")
    return files


def _read_below(root_fd: int, relative: str, limit: int = 8 * 1024 * 1024) -> bytes | None:
    """Read a regular file under root_fd without following any symlink; None if absent."""

    parts = relative.split("/")
    descriptor = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return b""  # never equal to a real file's bytes; a symlink or other type refuses
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            chunks, size = [], 0
            while size <= limit:
                chunk = os.read(handle, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return b"".join(chunks)
        finally:
            os.close(handle)
    except FileNotFoundError:
        return None
    finally:
        os.close(descriptor)


RUNTIME_ADVICE = (
    "so it was not run. Update the install from this setup (./setup.sh, then Update the "
    "install), or restore the project's files with git"
)


def check_project_runtime(project: Path) -> None:
    """Refuse unless the project's orchestration runtime is exactly this repository's copy.

    The installed version must equal this setup's, every runtime file must have identical
    bytes, and the folders Python imports from may hold no other module or package, so the
    CLI an owner tool runs with the owner's authority is code this repository ships.
    """

    expected_version = (NATIVE_PACKAGE / "VERSION").read_text(encoding="ascii").strip()
    bundle = _runtime_bundle()
    lead = "the orchestrator installed in this project is not an exact copy of this setup's version"
    try:
        root_fd = os.open(os.path.realpath(project), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise AttestError(f"{lead}: the project folder cannot be opened, {RUNTIME_ADVICE}") from exc
    try:
        raw = _read_below(root_fd, MANIFEST_RELATIVE.as_posix(), limit=1024 * 1024)
        try:
            manifest = json.loads(raw) if raw else None
        except (UnicodeDecodeError, ValueError):
            manifest = None
        installed = manifest.get("version") if isinstance(manifest, dict) else None
        if not isinstance(installed, str):
            raise AttestError(f"{lead}: its install record is missing or unreadable, {RUNTIME_ADVICE}")
        if installed != expected_version:
            shown = re.sub(r"[^0-9A-Za-z.+-]", "", installed)[:20] or "unknown"
            raise AttestError(f"{lead}: version {shown} is installed and this setup is version "
                              f"{expected_version}, {RUNTIME_ADVICE}")
        for relative, expected in bundle.items():
            current = _read_below(root_fd, relative)
            if current is None or hashlib.sha256(current).digest() != hashlib.sha256(expected).digest():
                raise AttestError(f"{lead}: {relative} differs from this setup's copy, {RUNTIME_ADVICE}")
        known = set(bundle)
        folders = {os.path.dirname(relative) for relative in bundle}
        # The folders Python imports from: the CLI's own (first on sys.path) and its package.
        importable = (RUNTIME_RELATIVE.as_posix(), (RUNTIME_RELATIVE / "codex_agents").as_posix())
        for folder in (item for item in importable if item in folders):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                for name in os.listdir(fd):
                    relative = f"{folder}/{name}"
                    if name == "__pycache__" or relative in known or relative in folders:
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) or name.endswith(_IMPORTABLE):
                        raise AttestError(f"{lead}: an extra Python module or folder sits in {folder}, "
                                          f"{RUNTIME_ADVICE}")
            finally:
                os.close(fd)
    except OSError as exc:
        raise AttestError(f"{lead}: its files cannot be read safely, {RUNTIME_ADVICE}") from exc
    finally:
        os.close(root_fd)


def owner_tool(argv: list[str]) -> int:
    """Non-interactive owner checks that ./setup.sh runs; prints only fixed text or a path.

    ``scratch PREFIX`` creates a private working folder and prints it; ``check-runtime
    PROJECT`` exits 0 only when the project's runtime is this repository's copy.
    """

    try:
        if len(argv) == 2 and argv[0] == "scratch":
            print(make_scratch(argv[1]))
            return 0
        if len(argv) == 2 and argv[0] == "check-runtime":
            check_project_runtime(Path(argv[1]))
            return 0
    except AttestError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("unknown owner tool", file=sys.stderr)
    return 2


# --- signals ----------------------------------------------------------------------------


def _on_signal(signum: int, _frame: object) -> None:
    for path in list(_TEMP_DIRS):
        remove_scratch(path)
    sys.exit(128 + signum)


def _install_handlers() -> dict[int, object]:
    previous: dict[int, object] = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.signal(number, _on_signal)
    return previous


def _restore_handlers(previous: dict[int, object]) -> None:
    for number, handler in previous.items():
        signal.signal(number, handler)


# --- trusted ssh-keygen -----------------------------------------------------------------


def check_ssh_keygen() -> None:
    """Apply the CLI verifier's checks: root-owned, not group/world-writable, safe parents."""

    path = Path(SSH_KEYGEN)
    try:
        info = path.lstat()
        ok = (
            not path.is_symlink()
            and stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
        for parent in (path.parent, path.parent.parent):
            parent_info = parent.lstat()
            ok = ok and (
                not parent.is_symlink()
                and stat.S_ISDIR(parent_info.st_mode)
                and parent_info.st_uid == 0
                and not parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
    except OSError:
        ok = False
    if not ok:
        raise AttestError("the system ssh-keygen is missing or not root-owned and protected")


def _env() -> dict[str, str]:
    """Environment without ssh-agent or a passphrase helper program.

    A key loaded in an agent cannot sign silently, and SSH_ASKPASS cannot hand the passphrase
    prompt to another program: ssh-keygen asks on the terminal only.
    """

    env = {k: v for k, v in os.environ.items() if k not in ("SSH_AUTH_SOCK", "SSH_ASKPASS")}
    env["SSH_ASKPASS_REQUIRE"] = "never"
    return env


def ssh(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([SSH_KEYGEN, *args], env=_env(), **kwargs)


# --- helpers ----------------------------------------------------------------------------


def default_state_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-agents"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "codex-agents"
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) / "codex-agents" if base else Path.home() / ".local/state/codex-agents"


def _expand(text: str) -> Path:
    return Path.home() / text[2:] if text.startswith("~/") else Path(text)


def ask_yes_no(prompt: str) -> bool:
    """Plain yes/no question; anything but an explicit yes is No."""

    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def run_cli(target: Path, args: list[str], state_root: Path | None, *, stdin: str | None = None) -> dict:
    """Run the project's CLI isolated; it must already have passed check_project_runtime."""

    command = cli_command(target, args)
    if state_root is not None:
        command += ["--state-root", str(state_root)]
    result = subprocess.run(command, capture_output=True, text=True,
                            input=stdin if stdin is not None else "")
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        report = None
    if result.returncode != 0 or not isinstance(report, dict) or report.get("ready") is not True:
        reason = report.get("error") if isinstance(report, dict) else None
        raise AttestError(f"the orchestration CLI refused {args[0]}: {reason or 'no usable output'}")
    return report


def enabled_providers(target: Path) -> list[str]:
    path = target / ROUTING_RELATIVE
    if not path.is_file():
        return ["codex"]
    try:
        providers = json.loads(path.read_text())["providers"]
        return [name for name in ("codex", "claude") if providers.get(name, {}).get("enabled") is True]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AttestError("provider-routing.json is unreadable") from exc


# --- key path safety --------------------------------------------------------------------


def _lstat(path: Path) -> os.stat_result:
    return os.lstat(path)


def check_path_safe(path: Path) -> None:
    """Refuse unsafe components, whoever owns them.

    Every existing component must be owned by you or root. A directory writable by group or
    others is refused unless it has the sticky bit, since another user could rename and
    replace it. A symlink must be root-owned (a system link such as ``/var``) and is followed:
    its resolved target must pass the same rules before the walk continues.
    """

    if not path.is_absolute():
        raise AttestError("key paths must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = _lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AttestError("a key path component could not be checked") from exc
        if info.st_uid not in (0, os.getuid()):
            raise AttestError("a key path component is owned by another user")
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != 0:
                raise AttestError("key paths may not pass through symbolic links")
            resolved = Path(os.path.realpath(current))
            if resolved != current:
                check_path_safe(resolved)
            current = resolved
        elif (
            stat.S_ISDIR(info.st_mode)
            and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not info.st_mode & stat.S_ISVTX
        ):
            raise AttestError("a key path directory is writable by other users")


def _split_public(text: str) -> tuple[str, str]:
    fields = text.split()
    if len(fields) < 2:
        raise AttestError("ssh-keygen returned no public key")
    return fields[0], fields[1]  # the comment is dropped here


def fingerprint(key_type: str, blob: str) -> str:
    scratch = make_scratch("pub-")
    try:
        public = scratch / "owner.pub"
        public.write_text(f"{key_type} {blob}\n")
        result = ssh(["-l", "-f", str(public)], capture_output=True, text=True)
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
            raise AttestError("ssh-keygen could not read that public key")
        return fields[1]
    finally:
        remove_scratch(scratch)


def describe_key(key_type: str, blob: str) -> str:
    return f"{key_type} {fingerprint(key_type, blob)}"


def validate_private_key(key: Path) -> None:
    check_path_safe(key)
    try:
        info = key.lstat()
    except OSError as exc:
        raise AttestError("that private key file does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AttestError("that path is not a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AttestError("the private key must be owned by you with mode 0600 or stricter")
    try:
        with key.open("rb") as handle:
            head = handle.read(64)
    except OSError as exc:
        raise AttestError("that private key file could not be read") from exc
    if b"PRIVATE KEY-----" not in head:
        raise AttestError("that path is not a private key (a public key file cannot sign)")
    unlocked = ssh(["-y", "-P", "", "-f", str(key)], capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    if unlocked.returncode == 0:
        raise AttestError(
            "that key has no passphrase, so any program could sign with it. Add one with: "
            f"ssh-keygen -p -f {key}"
        )


WRONG_TEXT = "incorrect passphrase supplied to decrypt private key"


def wrong_passphrase(result: subprocess.CompletedProcess, key: Path, kind: str) -> bool:
    """Whether ssh-keygen failed only because the passphrase did not unlock this key.

    ssh-keygen asks on the terminal (/dev/tty) and reports on stderr, so capturing stderr does
    not hide the prompt and the passphrase itself is never read here. Every line of stderr must
    be OpenSSH's complete diagnostic for this operation and this key path (``kind`` is "load"
    for -y and -Y sign, "change" for -p). Any other line, such as a save failure, or a path
    that merely contains the phrase, is not a wrong passphrase and is never retried. Nothing
    from stderr is shown or kept.
    """

    expected = (f'Load key "{key}": {WRONG_TEXT}' if kind == "load"
                else f"Failed to load key {key}: {WRONG_TEXT}")
    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    return result.returncode != 0 and bool(lines) and all(line == expected for line in lines)


def offer_retry() -> bool:
    """After a wrong passphrase: True to try again (the default), False to give up."""

    print("That passphrase did not unlock the key.")
    while True:
        answer = input("Try again or Exit? [T/e] ").strip().lower()
        if answer in ("", "t", "try", "try again"):
            return True
        if answer in ("e", "exit"):
            return False


def derive_public(key: Path) -> tuple[str, str]:
    """Ask ssh-keygen for the key's public half; it prompts for the passphrase, with retries."""

    while True:
        result = ssh(["-y", "-f", str(key)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            return _split_public(result.stdout)
        if wrong_passphrase(result, key, "load") and offer_retry():
            continue
        if wrong_passphrase(result, key, "load"):
            raise AttestError("stopped: the passphrase did not unlock the key")
        raise AttestError("could not unlock the private key")


def _create_key(path: Path) -> None:
    check_path_safe(path.parent)
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
        check_path_safe(path.parent)
    for candidate in (path, Path(str(path) + ".pub")):
        if os.path.lexists(candidate):  # also catches dangling symlinks
            raise AttestError("a file already exists where the key would go; choose another key")
    print("ssh-keygen will now ask for a passphrase. Choose one and remember it.")
    result = ssh(["-t", "ed25519", "-f", str(path), "-C", "codex-agents-owner"])
    if result.returncode != 0:
        raise AttestError("key creation did not finish")
    validate_private_key(path)  # mode, owner, and refuses an empty passphrase


def choose_new_key() -> Path:
    print(KEY_ADVICE)
    if ask_yes_no(f"Create a dedicated owner key at {DEFAULT_KEY}?"):
        key = _expand(DEFAULT_KEY)
        _create_key(key)
        return key
    print("This asks for a file location, not your passphrase. What you type is not shown.")
    entered = read_location("File location of an existing private key (blank to stop): ")
    if not entered:
        raise AttestError("no owner key chosen")
    return _accept_location(entered)


def read_location(prompt: str) -> str:
    """Read a file location with terminal echo off, so a mistyped passphrase is not shown."""

    with warnings.catch_warnings():
        # If echo cannot be turned off, getpass would warn and read with echo on: stop instead.
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning:
            raise AttestError(
                "this terminal cannot hide what you type, so nothing was read. "
                "Run this in a normal terminal window."
            ) from None


def _accept_location(entered: str) -> Path:
    """Expand an entry that looks like a file location; refuse anything else without echo."""

    if not entered.startswith("~/") and not os.path.isabs(entered):
        raise AttestError(NOT_A_PATH)
    return _expand(entered)  # never printed here: a mistyped passphrase can look like a path


def configured_public_key(state_root: Path) -> tuple[str, str] | None:
    try:
        fields = (state_root / "owner-allowed-signers").read_text(encoding="ascii").split()
    except OSError:
        return None
    if len(fields) >= 3 and fields[0] == "codex-agents-owner":
        return fields[1], fields[2]
    return None


def ensure_owner_key(target: Path, state_root: Path, cli_state: Path | None) -> Path:
    """Return the private key whose derived public half is the configured owner key."""

    configured = configured_public_key(state_root)
    if configured is None:
        key = choose_new_key()
        validate_private_key(key)
        derived = derive_public(key)
        adjacent = Path(str(key) + ".pub")
        if os.path.lexists(adjacent):
            try:
                existing = _split_public(adjacent.read_text())
            except (OSError, UnicodeDecodeError, AttestError):
                existing = None
            if existing != derived:
                raise AttestError("the .pub file next to the key does not match the private key")
        print(f"Owner key: {describe_key(*derived)}")
        scratch = make_scratch("enrol-", target)
        try:
            public = scratch / "owner.pub"
            public.write_text(f"{derived[0]} {derived[1]}\n")
            run_cli(target, ["learning-configure-owner-key", "--public-key", str(public)], cli_state)
        finally:
            remove_scratch(scratch)
        # Trust only what the state now holds: it must be exactly the key shown above.
        if configured_public_key(state_root) != derived:
            raise AttestError(ENROLLED_MISMATCH)
        print("Owner public key configured.")
        return key
    print(f"Owner key already configured: {describe_key(*configured)}")
    key = locate_key_file(configured)
    if derive_public(key) != configured:
        raise AttestError("that private key does not match the configured owner key")
    return key


ENROLLED_MISMATCH = (
    "the owner key now configured is not the key shown above, so nothing was signed. Do not "
    "attest until you know why: delete owner-allowed-signers in the learning state folder and "
    "run the attestation again"
)

NOT_A_PATH = (
    "That is not a file location. If you typed your passphrase here, it was not shown, but "
    "you can change it from ./setup.sh (Change the owner key passphrase) if you are unsure."
)


def _default_key_file(configured: tuple[str, str]) -> Path | None:
    """The key at the default location, when it is a plain file that can be the owner key.

    A ``.pub`` file next to it, if present, must agree with the configured key; this check
    needs no passphrase. The full check (deriving the public key) happens afterwards.
    """

    key = _expand(DEFAULT_KEY)
    try:
        check_path_safe(key)
        if not stat.S_ISREG(key.lstat().st_mode):
            return None
    except (AttestError, OSError):
        return None
    adjacent = Path(str(key) + ".pub")
    if os.path.lexists(adjacent):
        try:
            if _split_public(adjacent.read_text()) != configured:
                return None
        except (OSError, UnicodeDecodeError, AttestError):
            return None
    return key


def locate_key_file(configured: tuple[str, str]) -> Path:
    """Find and validate the private key file for the configured owner key (no passphrase)."""

    key = _default_key_file(configured)
    found = key is not None
    if found:
        print(f"Using the owner key file at {key}")
    else:
        print("Where is the private key file for this owner key?")
        print("This asks for a file location, not your passphrase.")
        print("What you type is not shown.")
        entered = read_location(f"Press Enter to use {DEFAULT_KEY}, or type another file location: ")
        key = _accept_location(entered) if entered else _expand(DEFAULT_KEY)
    validate_private_key(key)
    if not found:
        print(f"Using the private key file at {key}")  # only after it validated as a key file
    return key


_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp", "/private/var/folders")


def _in_temp_root(path: Path) -> bool:
    roots = {os.path.realpath(r) for r in (*_TEMP_ROOTS, tempfile.gettempdir())}
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def _in_working_tree(path: Path) -> bool:
    """Whether a Git working tree contains path: the folder itself and every ancestor, to /."""

    real = Path(os.path.realpath(path))
    return any(os.path.lexists(folder / ".git") for folder in (real, *real.parents))


def check_trusted_state(state_root: Path) -> None:
    """Trust the owner-key record only if an agent could not have written it.

    The standalone passphrase change has no project to anchor on, so the state directory and
    the signer file must be real, yours, not writable by others, and outside every temporary
    folder and Git working tree (places an agent or a project could plant a forged file).
    """

    signers = state_root / "owner-allowed-signers"
    if not os.path.lexists(signers):
        return  # nothing to trust; the caller reports that no key is configured
    check_path_safe(state_root)
    for path in (state_root, signers):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise AttestError("the owner key record may not be a symbolic link")
        if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AttestError("the owner key record must be owned by you and not writable by others")
    if not stat.S_ISREG(signers.lstat().st_mode):
        raise AttestError("the owner key record is not a regular file")
    if _in_temp_root(state_root) or _in_working_tree(state_root):
        raise AttestError("the owner key record is in a temporary folder or a project; refusing to trust it")


CHANGE_UNSURE = (
    "ssh-keygen had already finished, so the key may already have a new passphrase. If you "
    "cancelled or left the new passphrase empty, run the change again and choose a non-empty one."
)


def change_passphrase(state_root: Path) -> None:
    """Hand the terminal to ssh-keygen -p for the configured owner key, then re-verify it."""

    check_trusted_state(state_root)
    configured = configured_public_key(state_root)
    if configured is None:
        raise AttestError("no owner key is configured yet; run the billing attestation first")
    print(f"Owner key: {describe_key(*configured)}")
    key = locate_key_file(configured)
    print("First, ssh-keygen checks that this file is the owner key. Enter the current passphrase.")
    if derive_public(key) != configured:
        raise AttestError("that private key does not match the configured owner key")
    print("Now ssh-keygen will ask for the current passphrase, then the new one twice.")
    while True:
        result = ssh(["-p", "-f", str(key)], stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            break
        # A wrong current passphrase fails before anything is written, so retrying is safe.
        if wrong_passphrase(result, key, "change"):
            if offer_retry():
                continue
            raise AttestError("the passphrase was not changed")
        # Any other failure may have come after ssh-keygen began writing: never retry it.
        raise AttestError(
            "ssh-keygen stopped before it finished, so the passphrase may or may not have "
            "changed. Run the change again; the passphrase that unlocks the key is the current one"
        )
    try:
        validate_private_key(key)  # still encrypted, still safe
        print("Checking the key with the new passphrase. Enter it when asked.")
        if derive_public(key) != configured:
            raise AttestError("that private key no longer matches the configured owner key")
    except AttestError as exc:
        raise AttestError(f"{exc}. {CHANGE_UNSURE}") from exc
    print("Passphrase changed. The owner key is unchanged.")


# --- attestation ------------------------------------------------------------------------


def describe(records: list[dict]) -> tuple[str, str] | None:
    """Return (plan, login method) from the identity record, or None if not logged in."""

    for record in records:
        if record.get("record_type") == "provider_identity" and record.get("logged_in") is True:
            plan = record.get("subscription_type") or record.get("plan_type") or "unknown"
            return str(plan), str(record.get("auth_method") or "unknown")
    return None


def _skip(provider: str) -> bool:
    print(
        "Skipped. Any earlier attestation for this provider stays in effect. To block this "
        "provider's children now, set it to disabled in "
        ".codex/orchestration/provider-routing.json and commit."
    )
    return False


def canonical_json(value: object) -> bytes:
    """The runtime's canonical JSON (learning_contracts.canonical_json)."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def content_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def billing_record(grant: dict) -> dict:
    """The record the runtime builds from a grant (providers.billing_attestation_record)."""

    material = {field: grant[field] for field in sorted(grant) if field != "version"}
    body = {"schema_version": 1, "record_type": "billing_attestation", **material,
            "observed_at_ms": grant["issued_at"]}
    return {**body, "record_id": f"billing_attestation:{content_digest(body)}"}


PROJECT_ID = re.compile(r"prj_[0-9a-f]{32}")


def approved_payload(grant: dict, prepared: dict) -> bytes:
    """The bytes to sign, rebuilt here from the approved grant; refuse anything else.

    The runtime signs ``{"purpose": "authenticated_owner_record", "project_id": ...,
    "record_type": "billing_attestation", "record_digest": content_digest(record)}`` as
    canonical JSON (OwnerGrantAuthority.owner_record_payload). The record digest is computed
    here from the statement the owner approved, so a payload for any other statement, record
    type or purpose is refused. Only the registered project's identifier comes from the CLI.
    """

    try:
        returned = base64.b64decode(prepared["signing_payload_b64"], validate=True)
        claimed = json.loads(returned)
    except (KeyError, TypeError, ValueError):
        raise AttestError("the orchestration CLI returned no usable statement to sign") from None
    project_id = claimed.get("project_id") if isinstance(claimed, dict) else None
    record = billing_record(grant)
    expected = canonical_json({
        "purpose": "authenticated_owner_record",
        "project_id": project_id,
        "record_type": "billing_attestation",
        "record_digest": content_digest(record),
    })
    if (not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id)
            or returned != expected or prepared.get("record", record) != record):
        raise AttestError("the statement the orchestration CLI asked to sign is not the one you "
                          "approved, so nothing was signed")
    return expected


def attest_provider(target: Path, key: Path, provider: str, cli_state: Path | None) -> bool:
    print(f"\n== {provider.capitalize()} ==")
    records = run_cli(
        target, ["provider-observe", "--provider", provider, "--project-root", str(target)], cli_state
    ).get("records", [])
    shown = describe(records)
    identity = next((r for r in records if r.get("record_type") == "provider_identity"), None)
    if shown is None or identity is None:
        print(f"{provider.capitalize()} is not logged in, so it cannot be attested.")
        return False
    print(f"Plan: {shown[0]}   Login method: {shown[1]}")
    credits_disabled = False
    if provider == "claude":
        if not ask_yes_no(QUESTIONS["claude"]):
            return _skip(provider)
    else:
        if not ask_yes_no(QUESTIONS["codex_buy"]):
            print("Codex is not attested while credits can still be bought.")
            return _skip(provider)
        credits_disabled = ask_yes_no(QUESTIONS["codex_spend"])
    statement = ["no paid overage", "no usage credits"]
    if credits_disabled:
        statement.append("credit spending disabled")
    print(f"You are about to sign, for this {provider.capitalize()} account: " + ", ".join(statement) + ".")
    if not ask_yes_no("Sign this statement?"):
        return _skip(provider)
    grant = {
        "version": 1,
        "provider": provider,
        "account_fingerprint": identity["account_fingerprint"],
        "no_paid_overage": True,
        "no_usage_credits": True,
        "issued_at": int(time.time() * 1000),
    }
    if credits_disabled:
        grant["codex_credits_spending_disabled"] = True
    # The grant travels on stdin, never through a file another program could swap.
    grant_text = json.dumps(grant, sort_keys=True)
    base = ["learning-record-billing-attestation", "--provider", provider,
            "--project-root", str(target), "--grant", "-"]
    prepared = run_cli(target, base, cli_state, stdin=grant_text)
    payload = approved_payload(grant, prepared)
    work = make_scratch("sign-", target)
    try:
        payload_path = work / "payload"
        payload_path.write_bytes(payload)
        print("Now sign the statement. Enter your key's passphrase when asked.")
        signature = Path(str(payload_path) + ".sig")
        while True:
            signed = ssh(["-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(payload_path)],
                         stderr=subprocess.PIPE, text=True)
            if signed.returncode == 0 and signature.is_file():
                break
            if wrong_passphrase(signed, key, "load"):
                if offer_retry():
                    continue
                raise AttestError("stopped: the passphrase did not unlock the key")
            raise AttestError("signing did not finish")
        # The signature must cover exactly the approved bytes, whatever the file now holds.
        checked = ssh(["-Y", "check-novalidate", "-n", NAMESPACE, "-s", str(signature)],
                      input=payload, capture_output=True)
        if checked.returncode != 0:
            raise AttestError("the signature does not cover the approved statement; nothing was recorded")
        run_cli(target, [*base, "--signature", str(signature)], cli_state, stdin=grant_text)
    finally:
        remove_scratch(work)
    print(f"{provider.capitalize()} attested.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, help="project with the packages installed")
    parser.add_argument("--change-passphrase", action="store_true",
                        help="change the owner key's passphrase instead of attesting")
    parser.add_argument(
        "--state-root", type=Path,
        help="learning state directory (default: platform default; an installed project's "
             "CLI accepts only the default)",
    )
    args = parser.parse_args(argv)
    if not _is_terminal():
        print("This is a human step: run it yourself in an interactive terminal. "
              "It will not run from a script, pipe or agent.", file=sys.stderr)
        return 2
    state_root = (args.state_root or default_state_root()).expanduser()
    if args.change_passphrase:
        previous = _install_handlers()
        try:
            check_ssh_keygen()
            change_passphrase(state_root)
        except AttestError as exc:
            print(f"Stopped: {exc}", file=sys.stderr)
            return 1
        finally:
            _restore_handlers(previous)
        return 1 if _CLEANUP_FAILURES else 0
    if args.target is None:
        parser.error("--target is required")
    target = args.target.resolve()
    cli_state = args.state_root.expanduser() if args.state_root else None
    previous = _install_handlers()
    try:
        check_ssh_keygen()
        if not (target / CLI_RELATIVE).is_file():
            raise AttestError("the target has no installed orchestration CLI; run the installer first")
        trusted_python(target)
        check_project_runtime(target)
        providers = enabled_providers(target)
        key = ensure_owner_key(target, state_root, cli_state)
        done = [p for p in providers if attest_provider(target, key, p, cli_state)]
    except AttestError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_handlers(previous)
    print("\nSummary")
    print("Attested: " + (", ".join(done) if done else "none"))
    skipped = [p for p in providers if p not in done]
    if skipped:
        print("Not attested: " + ", ".join(skipped))
    print("Run this command again whenever billing settings change.")
    if _CLEANUP_FAILURES:
        return 1
    return 0 if done or not providers else 1


if __name__ == "__main__":
    sys.exit(main())
