"""SummarizeEvictionPolicy — replace N old turns with 1 Solar-written summary.

This is the policy that makes the project interesting: the OS itself uses
the LLM to manage its memory. We take the oldest `batch_size` non-pinned,
non-summarized pages and ask Solar to compress them into 2-3 sentences,
then insert that summary in their place.

Cost: one extra Solar call per eviction. The pager only invokes us when
LRU already failed or wasn't enough, so this is a deliberate spend.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.policy import EvictionPolicy
from gcos.memory.tokens import estimate_tokens


log = logging.getLogger(__name__)


SUMMARIZE_SYSTEM = (
    "You compress conversation history. Given a sequence of prior turns, "
    "produce 2-3 sentences that preserve key facts, names, numbers, and any "
    "decisions made. Do not add commentary. Do not invent details."
)


class SummarizeEvictionPolicy(EvictionPolicy):
    name = "summarize"

    def __init__(
        self,
        batch_size: int = 4,
        *,
        client_factory: Optional[Callable[[], object]] = None,
        max_summary_tokens: int = 200,
        quota: Optional[object] = None,
    ) -> None:
        self.batch_size = batch_size
        self.client_factory = client_factory
        self.max_summary_tokens = max_summary_tokens
        # Optional shared OS quota. The summarize call is a *real* Solar request
        # the OS issues on the agent's behalf, so it must be accounted for just
        # like any other call (B5). When the budget can't cover it we skip
        # (return 0) and let the next policy — swap — do the eviction instead.
        self.quota = quota

    def evict(self, pcb: AgentControlBlock, *, client: Optional[object] = None) -> int:
        candidates = [
            p for p in pcb.context_pages
            if not p.pinned and not p.summarized and p.role != "system"
        ]
        if len(candidates) < self.batch_size:
            return 0

        candidates.sort(key=lambda p: p.last_access)
        batch = candidates[: self.batch_size]

        # Account for the summarize call against the OS budget before spending.
        if self.quota is not None and not self.quota.acquire(1):
            log.info("summarize: PID %d skipped — global quota exhausted", pcb.pid)
            return 0

        # Prefer the client the worker is already holding (the rate-limited /
        # concurrency-capped batcher) so summarize calls go through the OS's
        # Solar throttle and show up in its stats — not a raw bypass client (B5).
        c = client or (self.client_factory() if self.client_factory else None)
        if c is None:
            from gcos.backend.solar_client import SolarClient  # lazy
            c = SolarClient()

        body = "\n\n".join(f"[{p.role}] {p.content}" for p in batch)
        log.info("summarize: PID %d compressing %d pages (~%d tokens)",
                 pcb.pid, len(batch), sum(p.tokens for p in batch))

        try:
            result = c.chat(
                [
                    {"role": "system", "content": SUMMARIZE_SYSTEM},
                    {"role": "user", "content": body},
                ],
                max_tokens=self.max_summary_tokens,
                timeout=20.0,
            )
        except Exception:
            # Call never landed — hand the reserved unit back so a failed
            # summarize doesn't permanently shrink the OS budget.
            if self.quota is not None:
                self.quota.refund(1)
            raise
        summary_text = result.content.strip()
        summary_tokens = (
            result.completion_tokens
            if getattr(result, "completion_tokens", 0) > 0
            else estimate_tokens(summary_text)
        )

        # Insert the summary in place of the first batch page (keeps order)
        insert_at = pcb.context_pages.index(batch[0])
        for p in batch:
            pcb.context_pages.remove(p)
        pcb.context_pages.insert(insert_at, ContextPage(
            role="system",
            content=f"[Summary of {len(batch)} prior turns]\n{summary_text}",
            tokens=summary_tokens,
            summarized=True,
            pinned=False,
        ))

        # Net pages freed = batch_size - 1 (we added one summary page back)
        return self.batch_size - 1
