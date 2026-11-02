"""Plumbing-only git for trust checks, hardened against the repository's own settings.

Trust checks may run outside the Codex sandbox through the owner's command rule,
and a repository's configuration can name programs that porcelain commands run:
an fsmonitor, clean and smudge filters, textconv, an external diff, hooks or a
pager. These checks therefore use only plumbing that reads refs and objects
(`rev-parse`, `ls-tree`, `cat-file`); callers read working-tree bytes themselves
with no-follow opens and compare them with committed blobs, never through
`git diff` or `git status`. Every call also switches the helpers off on the
command line, which overrides any configuration file, and ignores system and
global configuration.

Plumbing can still fetch: a partial clone fetches a missing object from its
promisor remote, whose transport (`ext::` runs a command) the repository's
configuration, or a file it includes, can name. So no transport is allowed at
all (an empty GIT_ALLOW_PROTOCOL overrides every `protocol.*.allow`), lazy
fetching is off where git supports GIT_NO_LAZY_FETCH, and a repository with a
promisor remote or partial-clone settings is refused before any object is read.

The git that runs is a real binary at a fixed location, never `/usr/bin/git` on
macOS: that is an xcrun shim, which consults a lookup cache under `$TMPDIR`, a
directory the sandboxed model can write.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from .host_paths import executable_location_problem, temp_roots, within

# Real git binaries, in order of preference. On macOS every developer tool in
# /usr/bin (git, python3, make, ...) is an xcrun shim, so /usr/bin is never used
# there; the active developer directory comes from xcode-select's root-owned link.
DARWIN_GIT_CANDIDATES = (
    "/Library/Developer/CommandLineTools/usr/bin/git",
    "{developer_dir}/usr/bin/git",
    "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
)
LINUX_GIT_CANDIDATES = ("/usr/bin/git",)
XCODE_SELECT_LINK = "/var/db/xcode_select_link"
# xcrun's lookup cache lives under $TMPDIR; any shim a child or git reaches skips it.
XCRUN_NO_CACHE = {"xcrun_nocache": "1"}

GIT_ENVIRONMENT = {
    "PATH": os.defpath,
    **XCRUN_NO_CACHE,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    # An empty list allows no transport, whatever protocol.*.allow says.
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_NO_LAZY_FETCH": "1",
}
GIT_OPTIONS = (
    "--no-pager",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.untrackedCache=false",
    "-c", "protocol.allow=never",
    "-c", "credential.helper=",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
)


def _developer_directory() -> str | None:
    """xcode-select's active developer directory, from its root-owned link only."""

    try:
        info = os.lstat(XCODE_SELECT_LINK)
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            return None
        value = os.readlink(XCODE_SELECT_LINK)
    except OSError:
        return None
    return value if os.path.isabs(value) else None


def git_candidates() -> tuple[str, ...]:
    if sys.platform != "darwin":
        return LINUX_GIT_CANDIDATES
    developer = _developer_directory()
    return tuple(
        candidate.format(developer_dir=developer) if developer else candidate
        for candidate in DARWIN_GIT_CANDIDATES
        if developer or "{developer_dir}" not in candidate
    )


def git_executable(untrusted: Iterable[Path] = ()) -> str | None:
    """The first real git binary whose location a sandboxed session cannot change.

    Each candidate is pinned as its realpath and must be a regular executable
    file outside every temporary directory and every `untrusted` root (callers
    pass the repository they inspect) and every ordinary folder under the home
    folder, with no symlink on its path and no component another user could
    change. On macOS the xcrun shim in /usr/bin is
    never accepted. None when no candidate passes; callers then fail closed.
    """

    untrusted = [*temp_roots(), *untrusted]
    for candidate in git_candidates():
        real = os.path.realpath(candidate)
        if sys.platform == "darwin" and os.path.dirname(real) == "/usr/bin":
            continue
        try:
            info = os.stat(real)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
            continue
        if executable_location_problem(Path(real), untrusted) is None:
            return real
    return None


def run_git(
    executable: str,
    root: Path,
    *arguments: str,
    text: bool = False,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [executable, *GIT_OPTIONS, "-C", str(root), *arguments],
        env=GIT_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=text,
        timeout=timeout,
        check=False,
    )


def repository_layout_problem(executable: str, root: Path) -> str | None:
    """Why the project's repository cannot be trusted, or None.

    The project must be the top of its working tree, and its git directory and
    common directory must be `<root>/.git` or lie outside the working tree, and
    outside every temporary directory unless the project itself is in one. A
    linked worktree whose shared repository sits in a sandbox-writable place
    would otherwise let the model rewrite the configuration git reads.
    """

    try:
        completed = run_git(
            executable, root, "rev-parse", "--path-format=absolute",
            "--show-toplevel", "--git-dir", "--git-common-dir", text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "the project repository is unavailable"
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 3:
        return "the project repository is unavailable"
    project = Path(os.path.realpath(root))
    top, git_directory, common_directory = (Path(os.path.realpath(line)) for line in lines)
    if top != project:
        return "the project is not the top of its repository"
    temporary = temp_roots()
    project_is_temporary = any(within(project, item) for item in temporary)
    for directory in (git_directory, common_directory):
        if within(directory, project) and not within(directory, project / ".git"):
            return "the project repository lies inside its working tree"
        if not project_is_temporary and any(within(directory, item) for item in temporary):
            return "the project repository lies in a temporary directory"
    # `git config` only reads configuration files, included ones too; it runs nothing.
    try:
        promisor = run_git(
            executable, root, "config", "--get-regexp",
            r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$",
            text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "the project repository is unavailable"
    if promisor.returncode != 1:
        return "the project repository is a partial clone or names a promisor remote"
    return None
