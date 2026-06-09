"""realproc.py — a scheduler over REAL processes, not a list.

This is where the simulation becomes an OS. GCOS's `RoundRobinScheduler` picks a
PCB out of a Python list and "preempts" only between LLM calls. Here we fork
real CPU-bound child processes and:

  - `rr_order()` time-slices them with a real wall-clock quantum using kernel
    SIGSTOP/SIGCONT preemption (vs FCFS run-to-completion). Portable; the
    `real_preemption` eval metric runs this even on macOS.
  - `cpu_share()` puts each child in a cgroup with a different `cpu.weight`,
    pins them to one CPU so they genuinely compete, lets the **Linux CFS**
    scheduler arbitrate, and reads back `cpu.stat` to show the measured CPU
    share tracks the weights. Linux-only (the kernel CFS + cgroup part);
    degrades to None elsewhere.

Forked children do only pure CPU work and `os._exit`, touching no inherited
locks, so the fork-with-threads caveat doesn't bite.
"""

from __future__ import annotations

import logging
import os
import signal
import time
import warnings
from typing import Optional

from gcos.osprims import cgroup as cg
from gcos.osprims.preempt import Preemptor


log = logging.getLogger(__name__)


def _fork() -> int:
    """os.fork() with the 3.12+ multi-threaded-fork DeprecationWarning silenced.

    The warning is sound in general, but every child we fork here goes straight
    into pure CPU work (no syscalls, no inherited locks) and `os._exit`, so the
    classic fork-with-threads deadlock cannot occur. Suppressing it keeps the
    eval/CI output clean while documenting that we considered it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return os.fork()


def _cpu_work(chunks: int) -> None:
    """A child: `chunks` units of pure CPU work, then exit. Each chunk is a tight
    integer loop (no syscalls), so SIGSTOP genuinely freezes it mid-computation."""
    x = 1
    for _ in range(chunks):
        for _ in range(120_000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    os._exit(0)


def _spin_forever() -> None:
    """A child that burns CPU until killed — for the cpu_share competition."""
    try:
        if hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, {0})  # pin to CPU 0 so siblings compete
    except OSError:
        pass
    # Defense in depth: ask the kernel to SIGKILL this spinner if the parent
    # dies (Linux PR_SET_PDEATHSIG). An infinite-spin child can then never be
    # orphaned and peg a CPU forever, even if the parent's cleanup is skipped.
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0)
    except Exception:  # noqa: BLE001 — non-Linux / no libc: best-effort only
        pass
    x = 1
    while True:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF


def _fork_cpu_child(chunks: int) -> int:
    pid = _fork()
    if pid == 0:
        _cpu_work(chunks)  # never returns
    return pid


class RealProcessScheduler:
    """Schedules real OS processes with real preemption / real CFS weighting."""

    def rr_order(self, n_procs: int, chunks: int, quantum_s: float = 0.02,
                 *, preempt: bool = True, max_wall_s: float = 30.0) -> list[int]:
        """Drive `n_procs` CPU children in quantum slices. Returns the per-slice
        dispatch order. preempt=True → RR (rotate every quantum, real SIGSTOP).
        preempt=False → FCFS (run each child to completion before the next).

        The loop runs until every child has finished its bounded work, bounded by
        a wall-clock `max_wall_s` safety deadline (NOT a chunk count): a per-chunk
        time that exceeds the quantum, or a small quantum on a slow/contended CPU,
        must not silently truncate the dispatch order (it used to). If the
        deadline is hit with children still pending, we log loudly and kill them
        in the finally block rather than returning a misleadingly partial order."""
        children: list[tuple[int, Preemptor]] = []
        for idx in range(n_procs):
            pid = _fork_cpu_child(chunks)
            children.append((idx, Preemptor(pid)))

        exited: set[int] = set()

        def reaped(idx: int, p: Preemptor) -> bool:
            if idx in exited:
                return True
            try:
                wpid, _status = os.waitpid(p.pid, os.WNOHANG)
            except ChildProcessError:
                exited.add(idx)
                return True
            if wpid == p.pid:
                exited.add(idx)
                return True
            return False

        order: list[int] = []
        for _idx, p in children:
            p.stop()  # everyone starts frozen; the scheduler hands out slices

        try:
            pending = list(children)
            i = 0
            deadline = time.monotonic() + max_wall_s
            while pending and time.monotonic() < deadline:
                i %= len(pending)
                idx, p = pending[i]
                if reaped(idx, p):
                    pending.pop(i)
                    continue
                order.append(idx)
                p.cont()
                time.sleep(quantum_s)
                if reaped(idx, p):
                    pending.pop(i)
                    continue
                p.stop()
                if preempt:
                    i += 1            # RR: next child gets the CPU
                # FCFS: leave i — same child keeps getting slices until it exits
            if pending:
                log.warning("rr_order: %d/%d children did not finish within %.1fs; "
                            "dispatch order is incomplete (raise max_wall_s)",
                            len(pending), n_procs, max_wall_s)
        finally:
            # Always kill+reap every survivor — including on an exception/interrupt
            # during the quantum sleep, when children are SIGSTOP'd and would
            # otherwise be orphaned frozen. Preemptor.kill() SIGCONTs before
            # SIGKILL so a stopped child actually dies.
            for idx, p in children:
                if idx not in exited:
                    p.kill()
                    try:
                        os.waitpid(p.pid, 0)
                    except ChildProcessError:
                        pass
        return order

    def cpu_share(self, weights: list[int], duration_s: float = 1.0) -> Optional[dict]:
        """Run one CPU-bound child per weight, each in its own cgroup with that
        `cpu.weight`, all pinned to CPU 0 so they compete, and let Linux CFS
        arbitrate. Returns measured per-child CPU time (usage_usec) so the caller
        can show share ≈ weight. None when cgroup v2 isn't enforceable (degraded).
        """
        if not cg.available():
            return None
        pids: list[int] = []
        groups: list[cg.Cgroup] = []
        usages: Optional[list] = None
        # try/finally so a fork failure (EAGAIN/ENOMEM) on a later iteration, or a
        # KeyboardInterrupt during the sleep, still kills+reaps every spinner
        # already forked — an infinite-spin child must never be orphaned (it also
        # has PR_SET_PDEATHSIG as a backstop on Linux).
        try:
            for k, w in enumerate(weights):
                pid = _fork()
                if pid == 0:
                    _spin_forever()  # never returns
                g = cg.Cgroup(f"share-{k}", weight=int(w))
                g.add_pid(pid)
                pids.append(pid)
                groups.append(g)
            time.sleep(duration_s)
            usages = [g.cpu_usage_us() for g in groups]
        finally:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for pid in pids:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            for g in groups:
                g.remove()

        if usages is None or any(u is None for u in usages):
            return None
        total = sum(usages) or 1
        return {
            "weights": list(weights),
            "usage_us": usages,
            "measured_share_pct": [round(100 * u / total, 1) for u in usages],
            "expected_share_pct": [round(100 * w / sum(weights), 1) for w in weights],
        }


def max_consecutive_run(order: list[int]) -> int:
    """Longest run of the same pid back-to-back. Large for FCFS (a child runs to
    completion); small for RR. NOT exactly 1 for RR: when only one child is left
    it correctly runs alone (no peer to rotate to), so the tail can show 2-3 —
    prefer `block_count` for a jitter-proof preempt-vs-convoy invariant."""
    best = run = 0
    prev = object()
    for x in order:
        run = run + 1 if x == prev else 1
        best = max(best, run)
        prev = x
    return best


def block_count(order: list[int]) -> int:
    """Number of maximal same-pid runs ("blocks"). This is the robust
    preemption signal, independent of CPU jitter:
      - FCFS (non-preemptive): each child runs to completion → exactly one block
        per child → block_count == number of distinct children.
      - RR (preemptive): children interleave → block_count > number of children.
    """
    if not order:
        return 0
    return 1 + sum(1 for a, b in zip(order, order[1:]) if a != b)
