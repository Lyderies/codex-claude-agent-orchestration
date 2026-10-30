#!/usr/bin/env python3
"""Block personal and machine identifiers from entering this public repository.

Findings report only a category, path and line, never the matched text, so CI logs
cannot leak a denylisted value. Owner-specific terms live in a private denylist file
outside the repository (one term per line, ``#`` comments allowed):

- ``$AO_IDENTIFIER_DENYLIST`` when set, otherwise
- ``~/.config/codex-claude-agent-orchestration/identifier-denylist.txt``.

Usage:
    check_identifiers.py                 scan every tracked file
    check_identifiers.py --staged        scan staged content (pre-commit)
    check_identifiers.py --identity      check the configured commit identity
    check_identifiers.py --commits REV   check author/committer emails reachable from REV
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
DEFAULT_DENYLIST = Path.home() / ".config" / "codex-claude-agent-orchestration" / "identifier-denylist.txt"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home-path", re.compile(r"(?<![\w/])/Users/(?!Shared\b)[A-Za-z0-9._-]+")),
    ("home-path", re.compile(r"(?<![\w/])/home/[A-Za-z0-9._-]+")),
    ("home-path", re.compile(r"\b[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+", re.IGNORECASE)),
    ("hostname", re.compile(r"\b[A-Za-z0-9-]+\.local\b(?!\.)")),
    ("hostname", re.compile(r"\b[A-Za-z0-9]+-MacBook(?:-[A-Za-z0-9]+)*\b")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)
ALLOWED_EMAILS = (
    re.compile(r"^\d+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$"),
    re.compile(r"^noreply@github\.com$"),
    re.compile(r"^noreply@anthropic\.com$"),
    re.compile(r"^[A-Za-z0-9._%+-]+@example\.(?:com|org|net)$"),
    re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]*\.(?:invalid|test|example|localhost)$"),
    re.compile(r"^[A-Za-z0-9._-]+@openssh\.com$"),  # SSH key-type names, not mailboxes
)
# Project namespaces that look like mDNS hostnames but identify no machine.
ALLOWED_HOSTNAMES = frozenset({"codex-agents.local", "claude-agents.local"})


@dataclass(frozen=True)
class Finding:
    category: str
    location: str

    def __str__(self) -> str:
        return f"{self.location}: {self.category}"


def load_denylist(path: Path | None = None) -> tuple[str, ...]:
    """Return lower-cased private terms; a missing file yields no terms."""

    if path is None:
        configured = os.environ.get("AO_IDENTIFIER_DENYLIST")
        path = Path(configured) if configured else DEFAULT_DENYLIST
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    terms = (line.strip() for line in text.splitlines())
    return tuple(term.casefold() for term in terms if term and not term.startswith("#"))


def email_allowed(address: str) -> bool:
    return any(pattern.fullmatch(address) for pattern in ALLOWED_EMAILS)


def scan_text(text: str, location: str, denylist: tuple[str, ...]) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{location}:{number}"
        for category, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if category == "email" and email_allowed(match.group(0)):
                    continue
                if category == "hostname" and match.group(0).casefold() in ALLOWED_HOSTNAMES:
                    continue
                findings.append(Finding(category, where))
        folded = line.casefold()
        if any(term in folded for term in denylist):
            findings.append(Finding("denylisted-term", where))
    return findings


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _decode(data: bytes) -> str | None:
    if len(data) > MAX_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def scan_paths(staged: bool, denylist: tuple[str, ...]) -> list[Finding]:
    if staged:
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        names = _git("ls-files", "-z")
    findings = []
    for raw in filter(None, names.split(b"\0")):
        path = raw.decode("utf-8", errors="replace")
        findings.extend(scan_text(path, f"{path} (path)", denylist))
        data = _git("show", f":{path}") if staged else Path(path).read_bytes()
        text = _decode(data)
        if text is not None:
            findings.extend(scan_text(text, path, denylist))
    return findings


def check_person(name: str, email: str, role: str, denylist: tuple[str, ...]) -> list[Finding]:
    findings = []
    if not email_allowed(email):
        findings.append(Finding("non-noreply-email", role))
    if any(term in f"{name} {email}".casefold() for term in denylist):
        findings.append(Finding("denylisted-term", role))
    return findings


def check_identity(denylist: tuple[str, ...]) -> list[Finding]:
    def config(key: str) -> str:
        result = subprocess.run(["git", "config", key], capture_output=True, text=True)
        return result.stdout.strip()

    return check_person(config("user.name"), config("user.email"), "git config user", denylist)


def check_commits(revision: str, denylist: tuple[str, ...]) -> list[Finding]:
    log = _git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", revision).decode("utf-8", "replace")
    findings = []
    for line in filter(None, log.splitlines()):
        commit, author, author_email, committer, committer_email = line.split("\0")
        findings += check_person(author, author_email, f"commit {commit[:12]} author", denylist)
        findings += check_person(committer, committer_email, f"commit {commit[:12]} committer", denylist)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="scan staged content")
    parser.add_argument("--identity", action="store_true", help="check the configured commit identity")
    parser.add_argument("--commits", metavar="REV", help="check commit identities reachable from REV")
    args = parser.parse_args(argv)

    denylist = load_denylist()
    findings = scan_paths(args.staged, denylist)
    if args.identity:
        findings += check_identity(denylist)
    if args.commits:
        findings += check_commits(args.commits, denylist)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"FAIL: {len(findings)} identifier finding(s); values are not printed.", file=sys.stderr)
        return 1
    print(f"PASS: no identifiers found ({len(denylist)} private denylist terms loaded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
