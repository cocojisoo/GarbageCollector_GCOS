"""Schedulers — FCFS / Priority / RoundRobin.

Each scheduler exposes one method: `pick_next(queue) -> AgentControlBlock | None`.
The worker pool calls this in a loop. Removing the chosen PCB from the queue is
the scheduler's responsibility (so RR can re-insert at the tail after one
quantum).

M1 ships FCFS only — Priority and RR land in M2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from gcos.kernel.pcb import AgentControlBlock
from gcos.kernel.ready_queue import ReadyQueue


class Scheduler(ABC):
    name: str = "base"

    @abstractmethod
    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        ...


class FCFSScheduler(Scheduler):
    """First-Come First-Served: oldest entry wins."""
    name = "fcfs"

    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        return queue.popleft()


class PriorityScheduler(Scheduler):
    """Higher `priority` field wins; ties broken by created_at (older first)."""
    name = "priority"

    def pick_next(self, queue: ReadyQueue) -> Optional[AgentControlBlock]:
        candidates = queue.snapshot()
        if not candidates:
            return None
        best = max(candidates, key=lambda p: (p.priority, -p.created_at))
        queue.pop(best)
        return best


class RoundRobinScheduler(Scheduler):
    """FCFS with a quantum measured in LLM call count.

    M1 stub: behaves like FCFS until worker pool understands quanta (M2).
    """
    name = "rr"

    def __init__(self, quantum: int = 1) -> None:
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
