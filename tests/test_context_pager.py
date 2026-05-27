from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory import ContextPager
from gcos.memory.policy import EvictionPolicy


def mkpcb(*pages: ContextPage) -> AgentControlBlock:
    pcb = AgentControlBlock(pid=1, name="t", prompt="user-prompt")
    pcb.context_pages = list(pages)
    return pcb


class DropFirstPolicy(EvictionPolicy):
    name = "drop_first"
    def evict(self, pcb, *, client=None):
        non_pinned = [p for p in pcb.context_pages if not p.pinned]
        if not non_pinned:
            return 0
        pcb.context_pages.remove(non_pinned[0])
        return 1


def test_fits_returns_true_when_under_budget():
    pcb = mkpcb(ContextPage(role="user", content="abc", tokens=3))
    assert ContextPager(budget_tokens=10).fits(pcb)


def test_overflow_reports_positive_excess():
    pcb = mkpcb(
        ContextPage(role="user", content="x", tokens=80),
        ContextPage(role="assistant", content="y", tokens=80),
    )
    p = ContextPager(budget_tokens=100)
    assert p.fits(pcb) is False
    assert p.overflow(pcb) == 60


def test_assemble_applies_policy_until_fits():
    pcb = mkpcb(*[
        ContextPage(role="user", content=f"page-{i}", tokens=40)
        for i in range(5)
    ])
    p = ContextPager(budget_tokens=100, policies=[DropFirstPolicy()])
    msgs = p.assemble(pcb, extra_user_prompt="next user turn")
    # After eviction: <= 2-3 pages plus extra prompt
    assert sum(pg.tokens for pg in pcb.context_pages) <= 100
    assert msgs[-1] == {"role": "user", "content": "next user turn"}


def test_assemble_does_not_evict_pinned():
    pcb = mkpcb(
        ContextPage(role="system", content="SYS", tokens=80, pinned=True),
        ContextPage(role="user", content="A", tokens=30),
        ContextPage(role="user", content="B", tokens=30),
        ContextPage(role="user", content="C", tokens=30),
    )
    p = ContextPager(budget_tokens=100, policies=[DropFirstPolicy()])
    p.assemble(pcb)
    # The pinned system page must remain
    assert any(pg.pinned for pg in pcb.context_pages)


def test_assemble_gives_up_when_no_policy_helps():
    pcb = mkpcb(ContextPage(role="system", content="big", tokens=1000, pinned=True))
    p = ContextPager(budget_tokens=10)  # no policies
    msgs = p.assemble(pcb)  # should not loop forever
    assert len(msgs) == 1


def test_stats_reports_breakdown():
    pcb = mkpcb(
        ContextPage(role="system", content="s", tokens=10, pinned=True),
        ContextPage(role="system", content="summary", tokens=20, summarized=True),
        ContextPage(role="user", content="u", tokens=30),
    )
    s = ContextPager(budget_tokens=100).stats(pcb)
    assert s["pages"] == 3
    assert s["pinned"] == 1
    assert s["summarized"] == 1
    assert s["tokens"] == 60
    assert s["overflow"] == 0
