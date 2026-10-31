#!/usr/bin/env python3
"""Guided owner billing attestation for an orchestrated project.

Usage: ``python3 scripts/attest.py --target <project> [--state-root <dir>]``, or
``python3 scripts/attest.py --change-passphrase`` to change the owner key's passphrase.

Admission requires, per provider account, an owner-signed statement that the account
cannot be charged beyond its subscription. This command walks the human owner through
it. What proves a person signed is the owner key's passphrase: the key is refused unless
it is encrypted, and signing runs without ssh-agent so a loaded key cannot sign silently.
The command also insists on an interactive terminal, but that alone does not keep a
program out; the passphrase does.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
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


class AttestError(RuntimeError):
    """A step failed in a way the owner should read about; never carries identifiers."""


# --- terminal, temporary directories, signals -------------------------------------------


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def make_temp(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))  # created 0700
    path.chmod(0o700)
    _TEMP_DIRS.add(path)
    return path


def remove_temp(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        if path not in _CLEANUP_FAILURES:
            _CLEANUP_FAILURES.append(path)
        print("Warning: a temporary directory could not be removed; delete it yourself:", path,
              file=sys.stderr)
        return False
    _TEMP_DIRS.discard(path)
    return True


def _on_signal(signum: int, _frame: object) -> None:
    for path in list(_TEMP_DIRS):
        remove_temp(path)
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


def run_cli(target: Path, args: list[str], state_root: Path | None) -> dict:
    command = [sys.executable, str(target / CLI_RELATIVE), *args]
    if state_root is not None:
        command += ["--state-root", str(state_root)]
    result = subprocess.run(command, capture_output=True, text=True)
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
    scratch = make_temp("attest-pub-")
    try:
        public = scratch / "owner.pub"
        public.write_text(f"{key_type} {blob}\n")
        result = ssh(["-l", "-f", str(public)], capture_output=True, text=True)
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
            raise AttestError("ssh-keygen could not read that public key")
        return fields[1]
    finally:
        remove_temp(scratch)


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


def wrong_passphrase(result: subprocess.CompletedProcess) -> bool:
    """Whether ssh-keygen failed only because the passphrase did not unlock the key.

    ssh-keygen asks on the terminal (/dev/tty) and reports on stderr, so capturing stderr does
    not hide the prompt and the passphrase itself is never read here. Only the fixed OpenSSH
    wording is matched; nothing from stderr is shown or kept.
    """

    return result.returncode != 0 and "incorrect passphrase" in (result.stderr or "").lower()


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
        if wrong_passphrase(result) and offer_retry():
            continue
        if wrong_passphrase(result):
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
        scratch = make_temp("attest-enrol-")
        try:
            public = scratch / "owner.pub"
            public.write_text(f"{derived[0]} {derived[1]}\n")
            run_cli(target, ["learning-configure-owner-key", "--public-key", str(public)], cli_state)
        finally:
            remove_temp(scratch)
        print("Owner public key configured.")
        return key
    print(f"Owner key already configured: {describe_key(*configured)}")
    key = locate_key_file(configured)
    if derive_public(key) != configured:
        raise AttestError("that private key does not match the configured owner key")
    return key


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
        if wrong_passphrase(result) and offer_retry():
            continue
        raise AttestError("the passphrase was not changed")
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
    work = make_temp("attest-")
    try:
        grant_path = work / "grant.json"
        grant_path.write_text(json.dumps(grant, sort_keys=True))
        base = ["learning-record-billing-attestation", "--provider", provider,
                "--project-root", str(target), "--grant", str(grant_path)]
        payload = base64.b64decode(run_cli(target, base, cli_state)["signing_payload_b64"])
        payload_path = work / "payload"
        payload_path.write_bytes(payload)
        print("Now sign the statement. Enter your key's passphrase when asked.")
        signature = Path(str(payload_path) + ".sig")
        while True:
            signed = ssh(["-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(payload_path)],
                         stderr=subprocess.PIPE, text=True)
            if signed.returncode == 0 and signature.is_file():
                break
            if wrong_passphrase(signed):
                if offer_retry():
                    continue
                raise AttestError("stopped: the passphrase did not unlock the key")
            raise AttestError("signing did not finish")
        run_cli(target, [*base, "--signature", str(signature)], cli_state)
    finally:
        remove_temp(work)
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
