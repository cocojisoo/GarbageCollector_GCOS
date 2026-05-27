"""Ready queue — thread-safe FIFO of READY agents.

The scheduler is allowed to pull arbitrary entries (not just head), because
Priority and RoundRobin need that. We expose `snapshot()` so schedulers can
inspect without mutating.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable, Optional

from gcos.kernel.pcb import AgentControlBlock, AgentState


class ReadyQueue:
    def __init__(self) -> None:
        self._q: deque[AgentControlBlock] = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def put(self, pcb: AgentControlBlock) -> None:
        with self._not_empty:
            pcb.transition(AgentState.READY)
            self._q.append(pcb)
            self._not_empty.notify()

    def pop(self, pcb: AgentControlBlock) -> bool:
        """Remove a specific PCB (used by Priority / RR after selection)."""
        with self._lock:
            try:
                self._q.remove(pcb)
                return True
            except ValueError:
                return False

    def popleft(self) -> Optional[AgentControlBlock]:
        with self._lock:
            if not self._q:
                return None
            return self._q.popleft()

    def snapshot(self) -> list[AgentControlBlock]:
        with self._lock:
            return list(self._q)

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    def wait_nonempty(self, timeout: Optional[float] = None) -> bool:
        with self._not_empty:
            if self._q:
                return True
            return self._not_empty.wait(timeout=timeout)

    def __iter__(self) -> Iterable[AgentControlBlock]:
        return iter(self.snapshot())
