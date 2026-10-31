"""Track and kill every descendant of one bridged child, and nothing else.

A child may start helpers in their own process group or session, and an orphan
is reparented once its parent exits, so the tree is tracked while the child
runs; one walk at the end would miss whatever already lost its parent. A
process is tracked only once it is seen as a descendant: a child of a tracked
process, or a member of a tracked process's group. A group never spans
sessions, and every session it can belong to began inside the child's tree.
Each process is known by its pid and start time, and is signalled only after a
fresh snapshot shows that same process, so a reused pid is never killed.

Standard library only: the deadline watchdog runs this file as a script.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

# Fixed paths, never a search path: macOS has /bin/ps, Linux distributions one or both.
PS_PATHS = ("/bin/ps", "/usr/bin/ps")
# pid, parent pid, process group and start time; a fixed locale and time zone keep
# the start time comparable between snapshots.
PS_COLUMNS = "pid=,ppid=,pgid=,lstart="
PS_ENVIRONMENT = {"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"}
SNAPSHOT_TIMEOUT_SECONDS = 5
TRACK_INTERVAL_SECONDS = 0.25
WATCHDOG_INTERVAL_SECONDS = 0.5
# Stop-and-rescan rounds before the final kill; each round stops what it found, so
# only a tree that keeps starting processes faster than a snapshot reaches the cap.
MAX_FREEZE_ROUNDS = 8

# pid -> (parent pid, process group, start time)
Table = dict[int, tuple[int, int, str]]


def process_table() -> Table | None:
    """This user's processes from a fixed-path `ps`; None when it is unavailable."""

    path = next((candidate for candidate in PS_PATHS if os.path.isfile(candidate)), None)
    if path is None:
        return None
    arguments = [path, "-U", str(os.getuid()), "-o", PS_COLUMNS]
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=PS_ENVIRONMENT,
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    table: Table = {}
    for line in completed.stdout.decode("ascii", "replace").splitlines():
        fields = line.split()
        if not fields:
            continue
        # A line that does not parse makes the whole snapshot unusable.
        if len(fields) < 4 or not all(field.isdigit() for field in fields[:3]):
            return None
        table[int(fields[0])] = (int(fields[1]), int(fields[2]), " ".join(fields[3:]))
    return table or None


class ProcessTree:
    """One child and every descendant seen so far, each known by pid and start time."""

    def __init__(self, root: int) -> None:
        self.root = root
        self.tracked: dict[int, str] = {}
        # Until the root's start time is known nothing can be verified or signalled.
        self.identified = False

    def identify(self, table: Table | None) -> bool:
        """Record the root's start time from a snapshot taken while it is unreaped."""

        if table is not None and self.root in table:
            self.tracked = {self.root: table[self.root][2]}
            self.identified = True
        return self.identified

    def update(self, table: Table) -> set[int]:
        """Adopt the new descendants in a snapshot; return the tracked pids it still holds."""

        live = {
            pid for pid, start in self.tracked.items()
            if pid in table and table[pid][2] == start
        }
        excluded_pids, excluded_groups = _own_identifiers()
        groups = {table[pid][1] for pid in live} - excluded_groups
        grew = True
        while grew:
            grew = False
            for pid, (parent, group, start) in table.items():
                if pid in live or pid in excluded_pids:
                    continue
                if parent in live or group in groups:
                    live.add(pid)
                    self.tracked[pid] = start
                    if group not in excluded_groups:
                        groups.add(group)
                    grew = True
        # Exited processes are forgotten; their pids may belong to anything later.
        self.tracked = {pid: self.tracked[pid] for pid in live}
        return live

    def refresh(self) -> None:
        """Adopt from a new snapshot; a failed snapshot keeps what is already tracked."""

        if self.identified:
            table = process_table()
            if table is not None:
                self.update(table)

    def terminate(self) -> bool:
        """Stop every tracked process, adopt what they started, then kill them all.

        A stopped process starts nothing new, so once a snapshot finds no new
        descendant the kill reaches the whole tree. Without a snapshot nothing is
        signalled, because a pid that cannot be verified again is never killed;
        the return value says whether the kill went ahead.
        """

        if not self.identified:
            return False
        stopped: set[int] = set()
        last: tuple[set[int], Table] | None = None
        for _ in range(MAX_FREEZE_ROUNDS):
            table = process_table()
            if table is None:
                break
            live = self.update(table)
            last = (live, table)
            fresh = live - stopped
            if not fresh:
                break
            _signal(fresh, table, signal.SIGSTOP)
            stopped |= fresh
        if last is None:
            return False
        _signal(*last, signal.SIGKILL)
        return True


def _own_identifiers() -> tuple[set[int], set[int]]:
    """The pids and groups that are never adopted or signalled: init and this process."""

    return {0, 1, os.getpid()}, {0, 1, os.getpgrp()}


def _signal(pids: set[int], table: Table, signum: int) -> None:
    excluded_pids, excluded_groups = _own_identifiers()
    for group in sorted({table[pid][1] for pid in pids} - excluded_groups):
        with contextlib.suppress(OSError):
            os.killpg(group, signum)
    for pid in sorted(pids - excluded_pids):
        with contextlib.suppress(OSError):
            os.kill(pid, signum)


def _group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except OSError:
        return False
    return True


def watch(root: int, deadline: float) -> None:
    """The detached deadline watchdog for one bridged child.

    It tracks the tree on its own, so a crash of the bridge still ends it: the
    whole tree is killed at the deadline, and what is left is killed once the
    child itself is gone. Without snapshots it falls back to the child's group.
    """

    tree = ProcessTree(root)
    tree.identify(process_table())
    while True:
        table = process_table() if tree.identified else None
        if table is not None:
            if root not in tree.update(table):
                tree.terminate()
                return
        elif not _group_exists(root):
            return
        if time.time() >= deadline:
            if not tree.terminate():
                with contextlib.suppress(OSError):
                    os.killpg(root, signal.SIGKILL)
            return
        time.sleep(WATCHDOG_INTERVAL_SECONDS)


if __name__ == "__main__":
    watch(int(sys.argv[1]), float(sys.argv[2]))
