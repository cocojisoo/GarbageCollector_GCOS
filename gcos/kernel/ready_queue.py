"""Ready queue — thread-safe queue of READY agents with in-flight accounting.

The scheduler is allowed to pull arbitrary entries (not just the head), because
Priority needs that. Two design points matter for concurrency correctness:

1. **Atomic selection (A1).** `pop_best(key)` selects *and* removes the winning
   PCB under a single lock and returns exactly what it removed. The old
   `snapshot()` + external `max()` + `pop()` pattern was non-atomic: two workers
   could select the same PCB and both run it. `snapshot()` remains for read-only
   dashboards, but schedulers must dispatch via `popleft()` / `pop_best()`.

2. **In-flight accounting (A2).** A PCB that has been dequeued but not yet
   finished is *in flight*. `is_drained()` (queue empty **and** in_flight == 0)
   is the real "system idle" signal. The counter is incremented atomically as
   part of the pop, so there is no window where a dispatched-but-unfinished
   agent reads as idle. Each successful pop must be paired with exactly one
   `task_done()` (the worker does this in a `finally`).
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Iterable, Optional

from gcos.kernel.pcb import AgentControlBlock, AgentState


class ReadyQueue:
    def __init__(self) -> None:
        self._q: deque[AgentControlBlock] = deque()
        self._lock = threading.Lock()
        # Both conditions share the one lock: one wakes idle workers when work
        # arrives, the other wakes waiters when the system fully drains.
        self._not_empty = threading.Condition(self._lock)
        self._drained = threading.Condition(self._lock)
        self._in_flight = 0

    # --- enqueue ------------------------------------------------------------

    def put(self, pcb: AgentControlBlock) -> bool:
        """Enqueue a PCB as READY. Returns False without enqueuing if the PCB is
        already terminal — a finalized agent (e.g. killed while a worker was
        about to re-queue it) must never sit in the ready queue. transition()
        is absorbing, so it would refuse READY anyway; this stops the stale
        append that ignored that refusal."""
        with self._not_empty:
            if pcb.is_terminal():
                return False
            pcb.transition(AgentState.READY)
            self._q.append(pcb)
            self._not_empty.notify()
            return True

    # --- dequeue ------------------------------------------------------------

    def popleft(self) -> Optional[AgentControlBlock]:
        """FIFO dequeue (FCFS / RR). Increments in-flight atomically."""
        with self._lock:
            if not self._q:
                return None
            pcb = self._q.popleft()
            self._in_flight += 1
            return pcb

    def pop_best(
        self, key: Callable[[AgentControlBlock], object]
    ) -> Optional[AgentControlBlock]:
        """Atomically select max-by-`key` and remove it (Priority dispatch).

        Selection and removal happen under one lock and the *removed* PCB is
        returned, so two concurrent callers can never receive the same agent.
        """
        with self._lock:
            if not self._q:
                return None
            best = max(self._q, key=key)
            self._q.remove(best)
            self._in_flight += 1
            return best

    def pop(self, pcb: AgentControlBlock) -> bool:
        """Remove a specific *queued* PCB (e.g. kill before dispatch).

        Does not touch in-flight: the PCB was never dispatched. May make the
        system drained, so we notify waiters.
        """
        with self._drained:
            try:
                self._q.remove(pcb)
            except ValueError:
                return False
            if not self._q and self._in_flight == 0:
                self._drained.notify_all()
            return True

    def task_done(self) -> None:
        """Mark one dispatched PCB as fully handled. Pairs 1:1 with a pop."""
        with self._drained:
            if self._in_flight > 0:
                self._in_flight -= 1
            if not self._q and self._in_flight == 0:
                self._drained.notify_all()

    # --- introspection ------------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def is_drained(self) -> bool:
        """True iff nothing is queued *and* nothing is in flight."""
        with self._lock:
            return not self._q and self._in_flight == 0

    def snapshot(self) -> list[AgentControlBlock]:
        with self._lock:
            return list(self._q)

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    # --- waiting ------------------------------------------------------------

    def wait_nonempty(self, timeout: Optional[float] = None) -> bool:
        with self._not_empty:
            if self._q:
                return True
            return self._not_empty.wait(timeout=timeout)

    def wait_drained(self, timeout: Optional[float] = None) -> bool:
        """Block until the queue is empty and no work is in flight."""
        with self._drained:
            return self._drained.wait_for(
                lambda: not self._q and self._in_flight == 0, timeout=timeout
            )

    def __iter__(self) -> Iterable[AgentControlBlock]:
        return iter(self.snapshot())
