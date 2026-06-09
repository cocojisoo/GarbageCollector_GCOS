"""preempt.py — real preemption via POSIX job-control signals.

The userspace GCOS could only "preempt" an agent *between* LLM calls, because a
Python function call isn't interruptible. With real child processes the kernel
gives us true preemption: a worker that has used its quantum gets `SIGSTOP`
(frozen mid-instruction by the kernel scheduler) and is resumed later with
`SIGCONT`. This is exactly how a real RR scheduler time-slices — the quantum is
wall-clock, not "calls", and a runaway agent cannot monopolise the CPU.

Works on any POSIX host (macOS included), so this module is fully exercised by
the test suite and the `real_preemption` eval metric even off Linux. cgroup CFS
*weighting* of those processes is the Linux-only part (see cgroup.py); the
preemption itself is portable.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Optional


log = logging.getLogger(__name__)


class Preemptor:
    """Stop/continue/kill a real process by PID, the way a kernel scheduler does.

    Every operation is a real syscall (`kill(2)` with SIGSTOP/SIGCONT/SIGKILL),
    so `stopped`/`running` reflect the actual kernel task state, not a flag we
    keep. Idempotent and race-tolerant: a signal to an already-dead child raises
    ProcessLookupError, which we treat as "already terminal".
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._stopped = False
        self._alive = True

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def alive(self) -> bool:
        return self._alive

    def _signal(self, sig: int) -> bool:
        try:
            os.kill(self.pid, sig)
            return True
        except ProcessLookupError:
            self._alive = False
            return False
        except PermissionError:
            log.warning("preempt: not permitted to signal pid=%d", self.pid)
            return False

    def stop(self) -> bool:
        """Freeze the process (SIGSTOP) — it consumes no CPU until continued."""
        if not self._alive or self._stopped:
            return self._stopped
        if self._signal(signal.SIGSTOP):
            self._stopped = True
            log.debug("preempt: SIGSTOP pid=%d", self.pid)
        return self._stopped

    def cont(self) -> bool:
        """Resume the process (SIGCONT) — give it the CPU again."""
        if not self._alive or not self._stopped:
            return not self._stopped and self._alive
        if self._signal(signal.SIGCONT):
            self._stopped = False
            log.debug("preempt: SIGCONT pid=%d", self.pid)
        return not self._stopped

    def kill(self) -> bool:
        """Terminate the process (SIGKILL) — uninterceptable, like `kill -9`."""
        # A stopped process won't reap on SIGKILL until continued, so wake it.
        if self._stopped:
            self._signal(signal.SIGCONT)
            self._stopped = False
        ok = self._signal(signal.SIGKILL)
        self._alive = False
        return ok

    def is_running_in_kernel(self) -> Optional[bool]:
        """Best-effort: read the task state from /proc (Linux). Returns True if
        the kernel reports the task Running/Sleeping, False if Stopped (T),
        None if unknowable (non-Linux or gone). This is how we *prove* SIGSTOP
        actually parked the task, rather than trusting our own flag."""
        try:
            with open(f"/proc/{self.pid}/stat", "r") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            state = fields[0]  # R, S, D, T (stopped), Z, ...
            return state != "T"
        except (FileNotFoundError, ProcessLookupError, IndexError, PermissionError):
            return None


def run_rr_quantum(preemptors: list[Preemptor], quantum_s: float,
                   *, is_done) -> list[int]:
    """Round-robin the given processes with a wall-clock quantum, using real
    SIGSTOP/SIGCONT preemption. Returns the dispatch order (list of PIDs in the
    order they were given a slice). `is_done(pid) -> bool` lets the caller report
    completion. This is the real-OS analogue of RoundRobinScheduler: the quantum
    is time, and preemption is enforced by the kernel, not cooperation."""
    order: list[int] = []
    live = list(preemptors)
    # Everyone starts stopped; the scheduler hands out slices.
    for p in live:
        p.stop()
    while live:
        nxt = []
        for p in live:
            if is_done(p.pid) or not p.alive:
                p.cont()  # let a finished/dead one drain
                continue
            order.append(p.pid)
            p.cont()
            time.sleep(quantum_s)
            if not is_done(p.pid) and p.alive:
                p.stop()
                nxt.append(p)
        live = nxt
    return order
