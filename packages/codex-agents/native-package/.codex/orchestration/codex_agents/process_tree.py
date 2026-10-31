"""Track and kill the descendants of one bridged child, and never anything else.

A process is tracked only once a snapshot shows its parent is a tracked process:
ancestry, never a shared process group. Each process is known by its pid and
the kernel's record of when it started (microseconds on macOS, clock ticks on
Linux), and that birth time is read again immediately before every signal, so
a pid that now belongs to another process is not signalled. Groups are
signalled only when their leader is a verified tracked process, which includes
the child itself while it is unreaped.

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
PS_COLUMNS = "pid=,ppid=,pgid=,stat="
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
# struct kinfo_proc; its first field is p_starttime (struct timeval) and p_pid
# sits at offset 40, which is checked so a layout change fails closed.
DARWIN_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
DARWIN_KINFO_PROC_SIZE = 648
DARWIN_PID_OFFSET = 40

# pid -> (parent pid, process group, ps state letters)
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
        if len(fields) != 4 or not all(field.isdigit() for field in fields[:3]):
            return None
        table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3])
    return table or None


_SYSCTL: list = []


def _darwin_birth(pid: int) -> str | None:
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
    if size.value != DARWIN_KINFO_PROC_SIZE:
        return None
    raw = buffer.raw
    seconds, microseconds = struct.unpack_from("=qi", raw, 0)
    if struct.unpack_from("=i", raw, DARWIN_PID_OFFSET)[0] != pid:
        return None
    return f"{seconds}.{microseconds:06d}"


def _linux_birth(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            content = handle.read()
    except OSError:
        return None
    # The command name may hold spaces and parentheses; the fields follow the last ")".
    end = content.rfind(b")")
    if end < 0:
        return None
    fields = content[end + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return fields[19].decode("ascii")


def birth_time(pid: int) -> str | None:
    """When the kernel says a process started; None when it is gone or unreadable."""

    if pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_birth(pid)
        if sys.platform.startswith("linux"):
            return _linux_birth(pid)
    except (OSError, ValueError, AttributeError, struct.error):
        return None
    return None


def tracking_available() -> bool:
    """Whether both the process table and birth times can be read here."""

    return process_table() is not None and birth_time(os.getpid()) is not None


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

        if table is None or self.root not in table:
            return False
        observed = birth_time(self.root)
        if observed is None or (birth is not None and observed != birth):
            return False
        self.root_birth = observed
        self.tracked = {self.root: observed}
        self.identified = True
        self.update(table)
        return True

    def _verified(self, pid: int) -> bool:
        birth = self.tracked.get(pid)
        return birth is not None and not _excluded_pid(pid) and birth_time(pid) == birth

    def update(self, table: Table) -> set[int]:
        """Adopt the children of tracked processes; return the tracked pids still present."""

        live = {pid for pid in self.tracked if pid in table and self._verified(pid)}
        children: dict[int, list[int]] = {}
        for pid, (parent, _, _) in table.items():
            children.setdefault(parent, []).append(pid)
        pending = sorted(live)
        while pending:
            for child in children.get(pending.pop(), ()):
                if child in live or _excluded_pid(child):
                    continue
                birth = birth_time(child)
                if birth is None:
                    continue
                self.tracked[child] = birth
                live.add(child)
                pending.append(child)
        # Exited processes are forgotten; their pids may belong to anything later.
        self.tracked = {pid: self.tracked[pid] for pid in live}
        return live

    def root_exited(self, table: Table) -> bool:
        """Whether the unreaped root has exited: a zombie, or absent from the snapshot."""

        entry = table.get(self.root)
        return entry is None or entry[2].startswith("Z")

    def terminate(self) -> None:
        """Kill every tracked process in rounds, each verified just before its signal.

        A group is killed only through a verified leader: the root, whose group is
        its own for life, or a tracked process that leads its group in the
        snapshot. Nothing is stopped, so nothing is ever left stopped.
        """

        if not self.identified:
            return
        for round_number in range(MAX_KILL_ROUNDS):
            if round_number:
                time.sleep(KILL_ROUND_PAUSE_SECONDS)
            table = process_table()
            if table is not None:
                self.update(table)
            running = [
                pid for pid in sorted(self.tracked)
                if table is None or (pid in table and not table[pid][2].startswith("Z"))
            ]
            if round_number and not running:
                return
            leaders = {self.root} | {
                pid for pid in self.tracked
                if table is not None and pid in table and table[pid][1] == pid
            }
            for group in sorted(leaders):
                if not _excluded_group(group) and self._verified(group):
                    with contextlib.suppress(OSError):
                        os.killpg(group, signal.SIGKILL)
            for pid in running:
                if self._verified(pid):
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
            # Without a snapshot nothing new can be adopted or seen to have died.
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
            live = tree.update(table)
            if root not in live or tree.root_exited(table):
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
