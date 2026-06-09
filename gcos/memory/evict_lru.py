"""LRUEvictionPolicy — drop the least-recently-touched non-pinned page."""

from __future__ import annotations

import logging
import time
from typing import Optional

from gcos.kernel.pcb import AgentControlBlock
from gcos.memory.policy import EvictionPolicy


log = logging.getLogger(__name__)


class LRUEvictionPolicy(EvictionPolicy):
    name = "lru"

    def __init__(self, min_keep: int = 2) -> None:
        # Never drop below `min_keep` non-pinned pages — keeps a tail of
        # recent turns so the agent doesn't lose all context.
        self.min_keep = min_keep

    def evict(self, pcb: AgentControlBlock, *, client: Optional[object] = None) -> int:
        non_pinned = [p for p in pcb.context_pages if not p.pinned]
        if len(non_pinned) <= self.min_keep:
            return 0
        # Oldest by last_access wins eviction
        victim = min(non_pinned, key=lambda p: p.last_access)
        age = max(0.0, time.time() - victim.last_access)
        log.debug("LRU evict: PID %d dropping role=%s tokens=%d age=%.1fs",
                  pcb.pid, victim.role, victim.tokens, age)
        pcb.context_pages.remove(victim)
        return 1
