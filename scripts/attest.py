#!/usr/bin/env python3
"""Guided owner billing attestation for an orchestrated project.

Usage: ``python3 scripts/attest.py --target <project> [--state-root <dir>]``.

Admission requires, per provider account, an owner-signed statement that the account
cannot be charged beyond its subscription. This command walks the human owner through
it. It deliberately runs only in an interactive terminal and signs with ssh-keygen
attached to that terminal, so the key's passphrase prompt reaches the person, not a
program. A key without a passphrase cannot stop other local programs from signing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAMESPACE = "codex-agents-owner-v1"
DEFAULT_KEY = "~/.ssh/codex-agents-owner"
CLI_RELATIVE = Path(".codex/orchestration/cli.py")
ROUTING_RELATIVE = Path(".codex/orchestration/provider-routing.json")
QUESTIONS = {
    "claude": "Is extra usage (pay-as-you-go overage) turned off for this Claude account?",
    "codex": "Is buying or spending Codex credits turned off for this account?",
}
KEY_ADVICE = (
    "The owner key proves that a person, not a program, approved the billing statement.\n"
    "Only the private key can sign it, so whoever can use the key can make the attestation.\n"
    "Set a passphrase: without one, any program on this machine, including an AI agent, could sign."
)


class AttestError(RuntimeError):
    """A step failed in a way the owner should read about; never carries identifiers."""


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


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
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AttestError("provider-routing.json is unreadable") from exc


def configured_public_key(state_root: Path) -> str | None:
    path = state_root / "owner-allowed-signers"
    try:
        fields = path.read_text(encoding="ascii").split()
    except OSError:
        return None
    if len(fields) >= 3 and fields[0] == "codex-agents-owner":
        return f"{fields[1]} {fields[2]}"
    return None


def fingerprint_line(public_key_file: Path) -> str:
    result = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(public_key_file)], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AttestError("ssh-keygen could not read that public key")
    return result.stdout.strip().splitlines()[0]


def _create_key(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    print("ssh-keygen will now ask for a passphrase. Choose one and remember it.")
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", "codex-agents-owner"]
    )
    if result.returncode != 0:
        raise AttestError("key creation did not finish")


def choose_new_key() -> Path:
    print(KEY_ADVICE)
    if ask_yes_no(f"Create a dedicated owner key at {DEFAULT_KEY}?"):
        key = _expand(DEFAULT_KEY)
        if key.exists():
            raise AttestError("that key file already exists; choose it as an existing key instead")
        _create_key(key)
        return key
    entered = input("Path of an existing private key to use (blank to stop): ").strip()
    if not entered:
        raise AttestError("no owner key chosen")
    key = _expand(entered)
    if not key.is_file():
        raise AttestError("that private key file does not exist")
    return key


def ensure_owner_key(target: Path, state_root: Path, cli_state: Path | None) -> Path:
    """Return the private key path whose public half is the configured owner key."""

    configured = configured_public_key(state_root)
    if configured is None:
        key = choose_new_key()
        public = Path(str(key) + ".pub")
        if not public.is_file():
            raise AttestError("the matching .pub file was not found next to the private key")
        print(f"Owner key: {fingerprint_line(public)}")
        run_cli(target, ["learning-configure-owner-key", "--public-key", str(public)], cli_state)
        print("Owner public key configured.")
        return key
    scratch = Path(tempfile.mkdtemp(prefix="attest-key-"))
    try:
        public = scratch / "owner.pub"
        public.write_text(configured + "\n")
        print(f"Owner key already configured: {fingerprint_line(public)}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    entered = input(f"Private key path [{DEFAULT_KEY}]: ").strip()
    key = _expand(entered or DEFAULT_KEY)
    if not key.is_file():
        raise AttestError("that private key file does not exist")
    return key


def describe(records: list[dict]) -> tuple[str, str] | None:
    """Return (plan, login method) from the identity record, or None if not logged in."""

    for record in records:
        if record.get("record_type") == "provider_identity" and record.get("logged_in") is True:
            plan = record.get("subscription_type") or record.get("plan_type") or "unknown"
            return str(plan), str(record.get("auth_method") or "unknown")
    return None


def attest_provider(target: Path, key: Path, provider: str, cli_state: Path | None) -> bool:
    print(f"\n== {provider.capitalize()} ==")
    records = run_cli(
        target, ["provider-observe", "--provider", provider, "--project-root", str(target)], cli_state
    ).get("records", [])
    shown = describe(records)
    identity = next((r for r in records if r.get("record_type") == "provider_identity"), None)
    if shown is None or identity is None:
        print(f"{provider.capitalize()} is not logged in. Skipping; its children stay blocked.")
        return False
    print(f"Plan: {shown[0]}   Login method: {shown[1]}")
    if not ask_yes_no(QUESTIONS[provider]):
        print(f"Skipped. {provider.capitalize()} children stay blocked until you can answer Yes.")
        return False
    grant = {
        "version": 1,
        "provider": provider,
        "account_fingerprint": identity["account_fingerprint"],
        "no_paid_overage": True,
        "no_usage_credits": True,
        "issued_at": int(time.time() * 1000),
    }
    if provider == "codex":
        grant["codex_credits_spending_disabled"] = True
    work = Path(tempfile.mkdtemp(prefix="attest-"))
    try:
        work.chmod(0o700)
        grant_path = work / "grant.json"
        grant_path.write_text(json.dumps(grant, sort_keys=True))
        base = ["learning-record-billing-attestation", "--provider", provider,
                "--project-root", str(target), "--grant", str(grant_path)]
        payload = base64.b64decode(run_cli(target, base, cli_state)["signing_payload_b64"])
        payload_path = work / "payload"
        payload_path.write_bytes(payload)
        print("Now sign the statement. Enter your key's passphrase when asked.")
        signed = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(payload_path)]
        )
        signature = Path(str(payload_path) + ".sig")
        if signed.returncode != 0 or not signature.is_file():
            raise AttestError("signing did not finish")
        run_cli(target, [*base, "--signature", str(signature)], cli_state)
    finally:
        shutil.rmtree(work, ignore_errors=True)
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
    try:
        if not (target / CLI_RELATIVE).is_file():
            raise AttestError("the target has no installed orchestration CLI; run the installer first")
        providers = enabled_providers(target)
        key = ensure_owner_key(target, state_root, cli_state)
        done = [p for p in providers if attest_provider(target, key, p, cli_state)]
    except AttestError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    print("\nSummary")
    print("Attested: " + (", ".join(done) if done else "none"))
    skipped = [p for p in providers if p not in done]
    if skipped:
        print("Still blocked: " + ", ".join(skipped))
    print("Run this command again whenever billing settings change. A new Codex attestation "
          "without the credits field revokes the credits waiver.")
    return 0 if done or not providers else 1


if __name__ == "__main__":
    sys.exit(main())
