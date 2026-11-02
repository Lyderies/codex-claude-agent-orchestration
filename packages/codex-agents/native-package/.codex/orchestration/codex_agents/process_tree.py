"""Track and kill the descendants of one bridged child, and never anything else.

`ps` only lists candidate pids. Every decision rests on one kernel record per
process, read in a single call, that gives its parent, process group and birth
time together (kinfo_proc on macOS, /proc/<pid>/stat on Linux). A process is
adopted only when its own record names a tracked parent and that parent's
record, read afterwards, still has the tracked birth time; its own birth time
comes from the same record as its parent. A process group never adopts. Every
signal is preceded by a fresh record read, so a pid that now belongs to another
process is not signalled, and a group is signalled only through a verified
tracked leader, which includes the child itself while it is unreaped.

Tracking is by polling, so it is conservative rather than complete: a
descendant whose parent exits before any snapshot has seen it is lost, and it
is left running rather than risk signalling an unrelated process.

Standard library only: the deadline watchdog runs this file as a script.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import struct
import subprocess
import sys
import time

# Fixed paths, never a search path: macOS has /bin/ps, Linux distributions one or both.
PS_PATHS = ("/bin/ps", "/usr/bin/ps")
PS_COLUMNS = "pid=,ppid="
PS_ENVIRONMENT = {"PATH": os.defpath, "LC_ALL": "C"}
SNAPSHOT_TIMEOUT_SECONDS = 5
TRACK_INTERVAL_SECONDS = 0.25
WATCHDOG_INTERVAL_SECONDS = 0.5
# The watchdog retries identification for this long, while the bridge holds the
# child unreaped and waits for its acknowledgement a little longer.
WATCHDOG_IDENTIFY_SECONDS = 5
WATCHDOG_READY_SECONDS = 10
WATCHDOG_READY = b"ready\n"
# Kill rounds: each takes a fresh snapshot, adopts what the tree started since and
# kills every live tracked process, until a round finds none left alive.
MAX_KILL_ROUNDS = 5
KILL_ROUND_PAUSE_SECONDS = 0.05

# macOS: sysctl(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid) fills one 648-byte
# struct kinfo_proc. kp_proc starts with p_starttime (struct timeval); p_stat is
# at 36 and p_pid at 40; kp_eproc starts at 296 with e_ppid at 560 and e_pgid at
# 564. Every record must name the pid asked for, and this process's own record
# must give its own parent and group, so a layout change fails closed.
DARWIN_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
DARWIN_KINFO_PROC_SIZE = 648
DARWIN_STAT_OFFSET = 36
DARWIN_PID_OFFSET = 40
DARWIN_PPID_OFFSET = 560
DARWIN_PGID_OFFSET = 564
DARWIN_ZOMBIE = 5

# pid -> parent pid, as `ps` listed it: a hint for which records to read, never trusted.
Table = dict[int, int]
# One kernel record: (parent pid, process group, birth time, exited)
Record = tuple[int, int, str, bool]


def process_table() -> Table | None:
    """This user's pids and their listed parents from a fixed-path `ps`; None when unavailable."""

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
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            return None
        table[int(fields[0])] = int(fields[1])
    return table or None


_SYSCTL: list = []


def _darwin_raw(pid: int) -> bytes | None:
    import ctypes

    if not _SYSCTL:
        library = ctypes.CDLL(DARWIN_LIBSYSTEM, use_errno=True)
        function = library.sysctl
        function.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        function.restype = ctypes.c_int
        _SYSCTL.append(function)
    name = (ctypes.c_int * 4)(1, 14, 1, pid)
    buffer = ctypes.create_string_buffer(DARWIN_KINFO_PROC_SIZE)
    size = ctypes.c_size_t(DARWIN_KINFO_PROC_SIZE)
    if _SYSCTL[0](name, 4, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.raw if size.value == DARWIN_KINFO_PROC_SIZE else None


def parse_kinfo_proc(raw: bytes, pid: int) -> Record | None:
    """A macOS kinfo_proc as a record; None unless it is the record for `pid`."""

    if len(raw) != DARWIN_KINFO_PROC_SIZE:
        return None
    if struct.unpack_from("=i", raw, DARWIN_PID_OFFSET)[0] != pid:
        return None
    seconds, microseconds = struct.unpack_from("=qi", raw, 0)
    parent = struct.unpack_from("=i", raw, DARWIN_PPID_OFFSET)[0]
    group = struct.unpack_from("=i", raw, DARWIN_PGID_OFFSET)[0]
    exited = raw[DARWIN_STAT_OFFSET] == DARWIN_ZOMBIE
    return parent, group, f"{seconds}.{microseconds:06d}", exited


_DARWIN_LAYOUT: list[bool] = []


def _darwin_record(pid: int) -> Record | None:
    if not _DARWIN_LAYOUT:
        own_pid = os.getpid()
        raw = _darwin_raw(own_pid)
        own = parse_kinfo_proc(raw, own_pid) if raw is not None else None
        _DARWIN_LAYOUT.append(
            own is not None and own[:2] == (os.getppid(), os.getpgrp()) and not own[3]
        )
    if not _DARWIN_LAYOUT[0]:
        return None
    raw = _darwin_raw(pid)
    return parse_kinfo_proc(raw, pid) if raw is not None else None


def parse_proc_stat(content: bytes) -> Record | None:
    """A Linux /proc/<pid>/stat line as a record: state, ppid, pgrp and starttime."""

    # The command name may hold spaces and parentheses; the fields follow the last ")".
    end = content.rfind(b")")
    if end < 0:
        return None
    fields = content[end + 2:].split()
    if len(fields) < 20 or not all(fields[index].isdigit() for index in (1, 2, 19)):
        return None
    return int(fields[1]), int(fields[2]), fields[19].decode("ascii"), fields[0] == b"Z"


def _linux_record(pid: int) -> Record | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            content = handle.read()
    except OSError:
        return None
    return parse_proc_stat(content)


def process_record(pid: int) -> Record | None:
    """One process's parent, group, birth time and exit state, from one kernel read.

    None when the process is gone (reaped), unreadable, or the platform is not
    macOS or Linux.
    """

    if pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_record(pid)
        if sys.platform.startswith("linux"):
            return _linux_record(pid)
    except (OSError, ValueError, AttributeError, IndexError, struct.error):
        return None
    return None


def tracking_available() -> bool:
    """Whether both the process table and kernel records can be read here."""

    return process_table() is not None and process_record(os.getpid()) is not None


def _excluded_pid(pid: int) -> bool:
    return pid <= 1 or pid == os.getpid()


def _excluded_group(group: int) -> bool:
    return group <= 1 or group == os.getpgrp()


class ProcessTree:
    """One child and every descendant seen so far, each known by pid and birth time."""

    def __init__(self, root: int) -> None:
        self.root = root
        self.root_birth: str | None = None
        self.tracked: dict[int, str] = {}
        # Until the root's birth time is known nothing is tracked or signalled.
        self.identified = False

    def identify(self, table: Table | None, birth: str | None = None) -> bool:
        """Record the root, which must be unreaped, and adopt what is already visible.

        With `birth` the root must still be that same process.
        """

        if table is None:
            return False
        record = process_record(self.root)
        if record is None or (birth is not None and record[2] != birth):
            return False
        self.root_birth = record[2]
        self.tracked = {self.root: record[2]}
        self.identified = True
        self.update(table)
        return True

    def _record(self, pid: int) -> Record | None:
        """The pid's current record, only while it is still the tracked process."""

        birth = self.tracked.get(pid)
        if birth is None or _excluded_pid(pid):
            return None
        record = process_record(pid)
        return record if record is not None and record[2] == birth else None

    def update(self, table: Table) -> set[int]:
        """Adopt the children of tracked processes; return the tracked pids still present.

        The listed parent only chooses which records to read. A candidate, even a
        pid tracked before as another process, is adopted only when its own record
        names a tracked parent whose record, read afterwards, is still that process.
        """

        live = {pid for pid in self.tracked if self._record(pid) is not None}
        grew = True
        while grew:
            grew = False
            for pid, listed_parent in table.items():
                if pid in live or listed_parent not in live or _excluded_pid(pid):
                    continue
                record = process_record(pid)
                if record is None or record[0] not in live:
                    continue
                if self._record(record[0]) is None:
                    continue
                self.tracked[pid] = record[2]
                live.add(pid)
                grew = True
        # Exited processes are forgotten; their pids may belong to anything later.
        self.tracked = {pid: self.tracked[pid] for pid in live}
        return live

    def root_exited(self) -> bool:
        """Whether the unreaped root has exited: a zombie, or no longer the tracked root."""

        record = self._record(self.root)
        return record is None or record[3]

    def terminate(self) -> None:
        """Kill every tracked process in rounds, each verified just before its signal.

        A group is killed only through a verified leader whose record shows it
        leads that group: the root, whose group is its own for life, or another
        tracked process. Nothing is stopped, so nothing is ever left stopped.
        """

        if not self.identified:
            return
        for round_number in range(MAX_KILL_ROUNDS):
            if round_number:
                time.sleep(KILL_ROUND_PAUSE_SECONDS)
            table = process_table()
            if table is not None:
                self.update(table)
            records = {pid: self._record(pid) for pid in sorted(self.tracked)}
            running = [pid for pid, record in records.items() if record and not record[3]]
            if round_number and not running:
                return
            for group, record in records.items():
                if record is None or record[1] != group or _excluded_group(group):
                    continue
                leader = self._record(group)
                if leader is not None and leader[1] == group:
                    with contextlib.suppress(OSError):
                        os.killpg(group, signal.SIGKILL)
            for pid in running:
                if self._record(pid) is not None:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
            # Without a snapshot nothing new can be adopted.
            if table is None:
                return


def await_ready(stream, seconds: float) -> bool:
    """Whether the watchdog acknowledged on `stream` within `seconds`."""

    received = b""
    deadline = time.monotonic() + seconds
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(received) < len(WATCHDOG_READY):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return False
            chunk = os.read(stream.fileno(), len(WATCHDOG_READY) - len(received))
            if not chunk:
                return False
            received += chunk
    return received == WATCHDOG_READY


def watch(root: int, birth: str, deadline: float, announce) -> None:
    """The detached deadline watchdog for one bridged child.

    It verifies the child's identity from the bridge, which keeps the child
    unreaped until `announce` acknowledges, and gives up silently if it cannot.
    It then tracks the tree on its own, so a crash of the bridge still ends it:
    the tree is killed at the deadline, or once the child itself has exited.
    """

    tree = ProcessTree(root)
    give_up = time.monotonic() + WATCHDOG_IDENTIFY_SECONDS
    while not tree.identify(process_table(), birth):
        if time.monotonic() >= give_up:
            return
        time.sleep(0.1)
    announce()
    while True:
        table = process_table()
        if table is not None:
            tree.update(table)
        if tree.root_exited():
            tree.terminate()
            return
        if time.time() >= deadline:
            tree.terminate()
            return
        time.sleep(WATCHDOG_INTERVAL_SECONDS)


def _announce() -> None:
    # A bridge that died before reading this leaves the watchdog to clean up alone.
    with contextlib.suppress(OSError):
        os.write(1, WATCHDOG_READY)
    # Replace the pipe with /dev/null so the bridge sees it close.
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.close(null)


if __name__ == "__main__":
    watch(int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), _announce)
