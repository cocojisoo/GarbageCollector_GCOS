import time

from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.evict_lru import LRUEvictionPolicy


def mkpcb(*pages):
    p = AgentControlBlock(pid=1, name="t", prompt="x")
    p.context_pages = list(pages)
    return p


def page(content, *, pinned=False, age=0.0, tokens=10):
    return ContextPage(
        role="user", content=content, tokens=tokens,
        pinned=pinned, last_access=time.time() - age,
    )


def test_drops_oldest_nonpinned():
    pcb = mkpcb(
        page("oldest", age=10.0),
        page("middle", age=5.0),
        page("newest", age=1.0),
        page("newer",  age=0.5),
    )
    LRUEvictionPolicy(min_keep=2).evict(pcb)
    contents = [p.content for p in pcb.context_pages]
    assert "oldest" not in contents
    assert "newest" in contents
    assert "newer" in contents


def test_respects_min_keep_floor():
    pcb = mkpcb(page("only", age=10.0), page("recent", age=1.0))
    n = LRUEvictionPolicy(min_keep=2).evict(pcb)
    assert n == 0
    assert len(pcb.context_pages) == 2  # no eviction; floor protected


def test_never_drops_pinned():
    pcb = mkpcb(
        page("PIN", pinned=True, age=100.0),
        page("evictable-1", age=5.0),
        page("evictable-2", age=2.0),
        page("evictable-3", age=1.0),
    )
    LRUEvictionPolicy(min_keep=2).evict(pcb)
    assert any(p.pinned for p in pcb.context_pages)
    assert "PIN" in [p.content for p in pcb.context_pages]


def test_returns_zero_when_nothing_to_evict():
    pcb = mkpcb(page("pinned-1", pinned=True), page("pinned-2", pinned=True))
    assert LRUEvictionPolicy().evict(pcb) == 0
