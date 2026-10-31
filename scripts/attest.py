#!/usr/bin/env python3
"""Guided owner billing attestation for an orchestrated project.

Usage: ``python3 scripts/attest.py --target <project> [--state-root <dir>]``.

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
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
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
    """Environment without ssh-agent, so a key loaded in an agent cannot sign silently."""

    return {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}


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


def derive_public(key: Path) -> tuple[str, str]:
    """Ask ssh-keygen for the key's public half; it prompts once for the passphrase."""

    result = ssh(["-y", "-f", str(key)], stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise AttestError("could not unlock the private key")
    return _split_public(result.stdout)


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
    entered = input("Path of an existing private key to use (blank to stop): ").strip()
    if not entered:
        raise AttestError("no owner key chosen")
    return _expand(entered)


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
    entered = input(f"Private key path [{DEFAULT_KEY}]: ").strip()
    key = _expand(entered or DEFAULT_KEY)
    validate_private_key(key)
    if derive_public(key) != configured:
        raise AttestError("that private key does not match the configured owner key")
    return key


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
        signed = ssh(["-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(payload_path)])
        signature = Path(str(payload_path) + ".sig")
        if signed.returncode != 0 or not signature.is_file():
            raise AttestError("signing did not finish")
        run_cli(target, [*base, "--signature", str(signature)], cli_state)
    finally:
        remove_temp(work)
    print(f"{provider.capitalize()} attested.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, type=Path, help="project with the packages installed")
    parser.add_argument("--state-root", type=Path, help="learning state directory (default: platform default)")
    args = parser.parse_args(argv)
    if not _is_terminal():
        print("This is a human step: run it yourself in an interactive terminal. "
              "It will not run from a script, pipe or agent.", file=sys.stderr)
        return 2
    target = args.target.resolve()
    state_root = (args.state_root or default_state_root()).expanduser()
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
