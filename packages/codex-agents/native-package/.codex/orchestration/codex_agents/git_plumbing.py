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
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .host_paths import temp_roots, within

GIT_ENVIRONMENT = {
    "PATH": os.defpath,
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


def git_executable() -> str | None:
    return shutil.which("git", path=os.defpath)


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
