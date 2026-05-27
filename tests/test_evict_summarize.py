import time

from gcos.backend.solar_client import ChatResult
from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.evict_summarize import SummarizeEvictionPolicy


class FakeClient:
    def __init__(self, summary="SUMMARY"):
        self.summary = summary
        self.calls = 0

    def chat(self, messages, *, temperature=0.2, max_tokens=200, timeout=20.0):
        self.calls += 1
        # Capture the body for assertions
        self.last_body = messages[-1]["content"]
        return ChatResult(
            content=self.summary,
            prompt_tokens=0, completion_tokens=10, total_tokens=10,
            model="fake",
        )


def mkpages(n: int) -> list[ContextPage]:
    now = time.time()
    return [
        ContextPage(role="user" if i % 2 == 0 else "assistant",
                    content=f"turn-{i}", tokens=30,
                    last_access=now - (n - i))  # older = smaller last_access
        for i in range(n)
    ]


def test_compresses_oldest_batch_into_one_summary_page():
    pcb = AgentControlBlock(pid=1, name="t", prompt="x")
    pcb.context_pages = mkpages(6)
    client = FakeClient(summary="they discussed X")
    policy = SummarizeEvictionPolicy(batch_size=4, client_factory=lambda: client)

    net_freed = policy.evict(pcb, client=client)
    assert net_freed == 3                              # 4 evicted, 1 summary added
    assert len(pcb.context_pages) == 3                 # 6 - 4 + 1 = 3
    summaries = [p for p in pcb.context_pages if p.summarized]
    assert len(summaries) == 1
    assert "they discussed X" in summaries[0].content
    assert client.calls == 1


def test_does_nothing_when_under_batch_size():
    pcb = AgentControlBlock(pid=1, name="t", prompt="x")
    pcb.context_pages = mkpages(3)
    client = FakeClient()
    n = SummarizeEvictionPolicy(batch_size=4, client_factory=lambda: client).evict(
        pcb, client=client,
    )
    assert n == 0
    assert client.calls == 0
    assert len(pcb.context_pages) == 3


def test_pinned_and_already_summarized_are_excluded():
    pcb = AgentControlBlock(pid=1, name="t", prompt="x")
    pcb.context_pages = [
        ContextPage(role="system", content="SYS", tokens=50,
                    pinned=True, last_access=time.time() - 100),
        ContextPage(role="system", content="prev-summary",
                    tokens=20, summarized=True, last_access=time.time() - 50),
        *mkpages(4),  # 4 fresh evictable
    ]
    client = FakeClient(summary="new summary")
    policy = SummarizeEvictionPolicy(batch_size=4, client_factory=lambda: client)
    policy.evict(pcb, client=client)
    # Pinned + prior summary should still be present
    assert any(p.pinned for p in pcb.context_pages)
    assert any(p.summarized and p.content == "prev-summary"
               for p in pcb.context_pages)


def test_summary_page_inserted_at_oldest_position():
    pcb = AgentControlBlock(pid=1, name="t", prompt="x")
    pcb.context_pages = mkpages(4)
    # Insert a recent pinned anchor at the end
    pcb.context_pages.append(ContextPage(
        role="system", content="LAST", tokens=10, pinned=True,
        last_access=time.time(),
    ))
    client = FakeClient(summary="S")
    SummarizeEvictionPolicy(batch_size=4, client_factory=lambda: client).evict(
        pcb, client=client,
    )
    # Order: [summary, LAST]
    assert pcb.context_pages[0].summarized
    assert pcb.context_pages[-1].content == "LAST"
