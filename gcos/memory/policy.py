"""EvictionPolicy interface.

Each policy answers one question: given a PCB whose context is over budget,
can you free one or more pages, and if so do it.

Policies are tried in order by the pager. Cheap ones go first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from gcos.kernel.pcb import AgentControlBlock


class EvictionPolicy(ABC):
    name: str = "base"

    @abstractmethod
    def evict(self, pcb: AgentControlBlock, *, client: Optional[object] = None) -> int:
        """Free pages from `pcb.context_pages`. Returns number of pages removed
        (a summarize policy may "remove" N and add 1, returning net = N-1)."""
        ...
