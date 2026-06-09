"""Schedulers — FCFS / Priority / RoundRobin.

Two axes define a scheduler here:
  1. **Selection** — `pick_next(queue)`: which ready PCB runs next. FCFS and RR
     deliberately *share* FIFO selection (`queue.popleft()`); that is the correct
     round-robin ordering, not a stub. Priority selects the highest effective
     priority instead (atomic `pop_best`, with aging).
  2. **Preemption** — `quantum`: how many consecutive LLM calls a worker runs for
     the picked agent before yielding it back to the tail. This is where FCFS and
     RR actually differ:

       - FCFS:     `quantum = None` → **non-preemptive**, runs an agent to
                   completion before the next starts (classic FCFS; shows the
                   convoy effect with multi-step agents).
       - Priority: `quantum = 1` → **preemptive**, re-evaluates priority (incl.
                   aging) after every call so a newly-urgent agent can take over.
       - RR:       `quantum = k` (default 2) → **preemptive**, rotates every k
                   calls (fair time-slicing).

For single-shot agents (every agent terminates in one call) all three coincide
in observable order — which is correct: RR degenerates to FCFS exactly when the
quantum is ≥ every job's burst length. The schedulers diverge only once agents
are multi-step (see tests/test_scheduler_fairness.py and the `scheduler_
preemption` eval metric).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Optional

from gcos.kernel.pcb import AgentControlBlock
from gcos.kernel.ready_queue import ReadyQueue


class Scheduler(ABC):
    name: str = "base"
    # LLM calls a worker may run per dispatch before yielding. None = run to
    # completion (non-preemptive).
    quantum: Optional[int] = 1

    @abstractmethod
    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        ...


class FCFSScheduler(Scheduler):
    """First-Come First-Served: oldest entry wins, **non-preemptive**.

    quantum=None → a picked agent runs to completion before the next one starts.
    With multi-step agents this is the textbook FCFS convoy effect (a long job
    delays everyone behind it); RR exists to fix exactly that.
    """
    name = "fcfs"
    quantum = None  # non-preemptive

    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        return queue.popleft()


class PriorityScheduler(Scheduler):
    """Highest *effective* priority wins; ties broken by created_at (older first).

    Effective priority = base priority + an aging bonus that grows with how long
    the agent has been waiting, capped at `aging_max_bonus` (C9). Without it, a
    steady stream of high-priority arrivals would starve low-priority agents
    forever (there is no periodic boost otherwise). With the default gentle rate
    the bonus is negligible for agents picked promptly, so immediate-dispatch
    ordering is still strict priority-descending.

    Selection goes through `queue.pop_best()`, which chooses *and* removes the
    winner under one lock and returns exactly that PCB — so two workers can never
    both dispatch the same agent (A1).

    Complexity (C10): `pop_best` is a single atomic O(n) scan, so draining n
    agents is O(n^2). This is a deliberate trade-off: aging makes the sort key
    time-dependent (recomputed every pick), which a static-key binary heap can't
    track without re-heapifying, and GCOS targets tens of concurrent agents
    where an O(n) scan under one lock is cheaper and simpler than maintaining a
    heap whose keys keep drifting. The previous code was *also* O(n) per pick
    (snapshot copy + max + remove) but split across three passes and racy.

    Preemptive (quantum=1): priority is re-evaluated after every call, so a
    newly-arrived or freshly-aged higher-priority agent takes the next slice.
    """
    name = "priority"
    quantum = 1  # preemptive: re-pick highest effective priority each call

    def __init__(self, aging_rate_per_s: float = 0.1, aging_max_bonus: float = 8.0) -> None:
        # 0.1 priority/sec → +1 effective priority per ~10s waited, up to +8, so
        # even a priority-0 agent escalates toward the top if it waits long
        # enough. Set aging_rate_per_s=0 to disable aging (strict priority).
        self.aging_rate_per_s = aging_rate_per_s
        self.aging_max_bonus = aging_max_bonus

    def effective_priority(self, p: AgentControlBlock, now: Optional[float] = None) -> float:
        if self.aging_rate_per_s <= 0:
            return float(p.priority)
        now = time.time() if now is None else now
        waited = max(0.0, now - p.created_at)
        return p.priority + min(self.aging_max_bonus, waited * self.aging_rate_per_s)

    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        now = time.time()
        return queue.pop_best(lambda p: (self.effective_priority(p, now), -p.created_at))


class RoundRobinScheduler(Scheduler):
    """Round-robin: FIFO **selection**, preemptive with a quantum of LLM calls.

    Selection is `queue.popleft()` — the same FIFO primitive FCFS uses, because
    that *is* correct round-robin order (rotate through the ready queue). RR's
    distinctive behaviour is **preemption**: the worker runs up to `quantum`
    consecutive calls for the picked agent, then re-queues it at the tail, so
    several multi-step agents share the worker fairly instead of one running to
    completion (the FCFS convoy effect). Preemption inside a single non-streaming
    LLM call isn't possible, so the quantum is measured in calls.

    For single-shot agents (one call → terminal) RR's quantum never bites — every
    job is one call long, so RR and FCFS produce the same order. That is the
    standard "RR with quantum ≥ max burst == FCFS" degeneration, *not* a stub:
    flip in a multi-step workload and the interleaving appears (see
    tests/test_scheduler_fairness.py and the `scheduler_preemption` eval metric).
    """
    name = "rr"

    def __init__(self, quantum: int = 2) -> None:
        if quantum < 1:
            raise ValueError("quantum must be >= 1")
        self.quantum = quantum

    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        return queue.popleft()


SCHEDULERS: dict[str, type[Scheduler]] = {
    FCFSScheduler.name: FCFSScheduler,
    PriorityScheduler.name: PriorityScheduler,
    RoundRobinScheduler.name: RoundRobinScheduler,
}


def make(name: str) -> Scheduler:
    cls = SCHEDULERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown scheduler '{name}'. Choices: {list(SCHEDULERS)}")
    return cls()
