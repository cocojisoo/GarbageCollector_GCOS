"""Process table — the kernel's master registry of all PCBs.

Equivalent to the kernel's process table: every agent that ever existed
in this kernel boot lives here, keyed by PID, until explicitly cleared.

The ready queue is a *subset* of this table (just the READY ones).
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

from gcos.kernel.pcb import AgentControlBlock, AgentState


class ProcessTable:
    def __init__(self) -> None:
        self._table: dict[int, AgentControlBlock] = {}
        self._lock = threading.RLock()

    def add(self, pcb: AgentControlBlock) -> None:
        with self._lock:
            if pcb.pid in self._table:
                raise ValueError(f"PID {pcb.pid} already registered")
            self._table[pcb.pid] = pcb

    def get(self, pid: int) -> Optional[AgentControlBlock]:
        with self._lock:
            return self._table.get(pid)

    def remove(self, pid: int) -> Optional[AgentControlBlock]:
        with self._lock:
            return self._table.pop(pid, None)

    def snapshot(self) -> list[AgentControlBlock]:
        """Return a list copy of all PCBs. Order: ascending PID."""
        with self._lock:
            return [self._table[k] for k in sorted(self._table.keys())]

    def by_state(self, state: AgentState) -> list[AgentControlBlock]:
        with self._lock:
            return [p for p in self._table.values() if p.state == state]

    def clear_terminal(self) -> int:
        """Drop DONE/ERROR/TIMEOUT/ZOMBIE entries. Returns count removed."""
        with self._lock:
            dead = [pid for pid, p in self._table.items() if p.is_terminal()]
            for pid in dead:
                del self._table[pid]
            return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._table)

    def __iter__(self) -> Iterable[AgentControlBlock]:
        return iter(self.snapshot())

    def __contains__(self, pid: int) -> bool:
        with self._lock:
            return pid in self._table
