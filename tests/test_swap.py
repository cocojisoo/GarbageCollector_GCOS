import time
from pathlib import Path

from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.swap import SwapEvictionPolicy, swap_in


def mkpages(n: int):
    now = time.time()
    return [
        ContextPage(role="user", content=f"page-{i}", tokens=30,
                    last_access=now - (n - i))
        for i in range(n)
    ]


def test_swap_out_writes_json(tmp_path: Path):
    pcb = AgentControlBlock(pid=42, name="t", prompt="x")
    pcb.context_pages = mkpages(5)
    n = SwapEvictionPolicy(swap_dir=tmp_path, batch_size=2, min_keep=2).evict(pcb)
    assert n == 2
    assert len(pcb.context_pages) == 3
    files = list((tmp_path / "42").iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".json"


def test_swap_in_restores_pages(tmp_path: Path):
    pcb = AgentControlBlock(pid=7, name="t", prompt="x")
    pcb.context_pages = mkpages(4)
    original_contents = [p.content for p in pcb.context_pages]
    SwapEvictionPolicy(swap_dir=tmp_path, batch_size=2, min_keep=2).evict(pcb)

    loaded = swap_in(pcb, swap_dir=tmp_path)
    assert loaded == 2
    contents = {p.content for p in pcb.context_pages}
    # All originals should now be present again
    assert set(original_contents) <= contents


def test_swap_respects_min_keep(tmp_path: Path):
    pcb = AgentControlBlock(pid=1, name="t", prompt="x")
    pcb.context_pages = mkpages(2)
    n = SwapEvictionPolicy(swap_dir=tmp_path, batch_size=2, min_keep=2).evict(pcb)
    assert n == 0
    assert len(pcb.context_pages) == 2
