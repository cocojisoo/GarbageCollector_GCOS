"""ContextPager — assembles messages within a token budget.

This is the LLM analogue of OS virtual memory.  Each agent's
`context_pages: list[ContextPage]` is its address space; the pager assembles
the active "working set" (the messages list we actually send to Solar) and
applies eviction policies when it doesn't fit.

Policies (defined in `gcos.memory.evict_*`) are tried in order:
    1. LRU drop  — cheap, no API cost, drops the oldest non-pinned page.
    2. Summarize — Solar call, compresses N old pages into 1 summary page.
    3. Swap-out  — write pages to disk for later reload (optional, M5+).

Pinned pages (e.g. the coder system prompt) are never evicted.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.policy import EvictionPolicy
from gcos.memory.tokens import estimate_tokens


log = logging.getLogger(__name__)


def _page_tokens(p: ContextPage) -> int:
    """Use the recorded token count when available, fall back to an estimate."""
    return p.tokens if p.tokens > 0 else estimate_tokens(p.content)


def context_size(pages: Sequence[ContextPage]) -> int:
    return sum(_page_tokens(p) for p in pages)


class ContextPager:
    def __init__(
        self,
        budget_tokens: int = 4096,
        policies: Optional[Sequence[EvictionPolicy]] = None,
    ) -> None:
        self.budget = budget_tokens
        # Default: no policies — the pager just measures. Real policies are
        # plugged in by the kernel.
        self.policies: list[EvictionPolicy] = list(policies or [])

    # ------------------------------------------------------------------

    def fits(self, pcb: AgentControlBlock) -> bool:
        return context_size(pcb.context_pages) <= self.budget

    def overflow(self, pcb: AgentControlBlock) -> int:
        """Tokens over budget (>=0)."""
        return max(0, context_size(pcb.context_pages) - self.budget)

    def assemble(
        self,
        pcb: AgentControlBlock,
        *,
        client: Optional[object] = None,
        extra_user_prompt: Optional[str] = None,
    ) -> list[dict]:
        """Apply policies until under budget, then return a messages list.

        If `extra_user_prompt` is given, it's appended at the end without
        being persisted to context_pages — used by the executor to attach
        the *current* user turn that hasn't been recorded yet.
        """
        budget_for_extra = estimate_tokens(extra_user_prompt or "")
        effective_budget = self.budget - budget_for_extra

        guard = 16  # cap policy attempts so a buggy policy can't loop forever
        while context_size(pcb.context_pages) > effective_budget and guard > 0:
            freed = 0
            for policy in self.policies:
                freed = policy.evict(pcb, client=client)
                if freed > 0:
                    log.info(
                        "pager: PID %d policy=%s freed=%d new_size=%d budget=%d",
                        pcb.pid, policy.name, freed,
                        context_size(pcb.context_pages), effective_budget,
                    )
                    break
            if freed == 0:
                # No policy could free anything — give up. The LLM will see
                # whatever we have; Solar may itself complain about length.
                log.warning(
                    "pager: PID %d cannot fit context "
                    "(size=%d budget=%d, %d pages, %d pinned)",
                    pcb.pid,
                    context_size(pcb.context_pages),
                    effective_budget,
                    len(pcb.context_pages),
                    sum(1 for p in pcb.context_pages if p.pinned),
                )
                break
            guard -= 1

        messages: list[dict] = []
        for p in pcb.context_pages:
            p.touch()
            messages.append({"role": p.role, "content": p.content})
        if extra_user_prompt:
            messages.append({"role": "user", "content": extra_user_prompt})
        return messages

    # ------------------------------------------------------------------

    def add_policy(self, policy: EvictionPolicy) -> None:
        self.policies.append(policy)

    def stats(self, pcb: AgentControlBlock) -> dict:
        pages = pcb.context_pages
        return {
            "pages": len(pages),
            "pinned": sum(1 for p in pages if p.pinned),
            "summarized": sum(1 for p in pages if p.summarized),
            "tokens": context_size(pages),
            "budget": self.budget,
            "overflow": self.overflow(pcb),
        }
