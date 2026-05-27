import pytest

from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable


def mkpcb(pid: int, state: AgentState = AgentState.NEW) -> AgentControlBlock:
    p = AgentControlBlock(pid=pid, name=f"a{pid}", prompt="x")
    p.state = state
    return p


def test_add_and_get():
    t = ProcessTable()
    a = mkpcb(1)
    t.add(a)
    assert t.get(1) is a
    assert t.get(999) is None
    assert 1 in t
    assert len(t) == 1


def test_add_duplicate_pid_raises():
    t = ProcessTable()
    t.add(mkpcb(1))
    with pytest.raises(ValueError):
        t.add(mkpcb(1))


def test_snapshot_is_sorted_and_isolated():
    t = ProcessTable()
    for pid in [3, 1, 2]:
        t.add(mkpcb(pid))
    snap = t.snapshot()
    assert [p.pid for p in snap] == [1, 2, 3]
    # Mutating snapshot must not affect the table
    snap.clear()
    assert len(t) == 3


def test_by_state_filters():
    t = ProcessTable()
    t.add(mkpcb(1, AgentState.READY))
    t.add(mkpcb(2, AgentState.DONE))
    t.add(mkpcb(3, AgentState.READY))
    assert {p.pid for p in t.by_state(AgentState.READY)} == {1, 3}
    assert {p.pid for p in t.by_state(AgentState.DONE)} == {2}


def test_clear_terminal_removes_done_error_timeout_zombie():
    t = ProcessTable()
    t.add(mkpcb(1, AgentState.RUNNING))
    t.add(mkpcb(2, AgentState.DONE))
    t.add(mkpcb(3, AgentState.ERROR))
    t.add(mkpcb(4, AgentState.READY))
    t.add(mkpcb(5, AgentState.ZOMBIE))
    removed = t.clear_terminal()
    assert removed == 3
    assert {p.pid for p in t.snapshot()} == {1, 4}
