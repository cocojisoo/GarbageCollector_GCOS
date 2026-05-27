"""GCOS memory manager — context paging + eviction policies + swap.

  ContextPager(budget, [LRU, Summarize, Swap]).assemble(pcb)
  → fits within budget by applying policies in order.

LRU is free; Summarize costs one Solar call; Swap writes to disk.
"""

from __future__ import annotations

from gcos.memory.context_pager import ContextPager, context_size
from gcos.memory.evict_lru import LRUEvictionPolicy
from gcos.memory.evict_summarize import SummarizeEvictionPolicy
from gcos.memory.policy import EvictionPolicy
from gcos.memory.swap import SwapEvictionPolicy, swap_in
from gcos.memory.tokens import estimate_tokens


def default_policies(*, summarize_client_factory=None) -> list[EvictionPolicy]:
    """The standard 3-tier policy stack used by Kernel."""
    return [
        LRUEvictionPolicy(min_keep=2),
        SummarizeEvictionPolicy(batch_size=4, client_factory=summarize_client_factory),
        SwapEvictionPolicy(batch_size=2, min_keep=2),
    ]


__all__ = [
    "ContextPager",
    "EvictionPolicy",
    "LRUEvictionPolicy",
    "SummarizeEvictionPolicy",
    "SwapEvictionPolicy",
    "context_size",
    "default_policies",
    "estimate_tokens",
    "swap_in",
]
