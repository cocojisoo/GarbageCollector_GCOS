from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.process_tree import ProcessTree


def add(table, pid, parent=None):
    pcb = AgentControlBlock(pid=pid, name=f"a{pid}", prompt="x", parent_pid=parent)
    table.add(pcb)
    if parent is not None:
        table.get(parent).children.append(pid)
    return pcb


def test_children_of_returns_direct_children():
    t = ProcessTable()
    add(t, 1)
    add(t, 2, parent=1)
    add(t, 3, parent=1)
    add(t, 4, parent=2)
    tree = ProcessTree(t)
    assert {c.pid for c in tree.children_of(1)} == {2, 3}
    assert {c.pid for c in tree.children_of(2)} == {4}
    assert tree.children_of(99) == []


def test_descendants_does_full_subtree():
    t = ProcessTable()
    add(t, 1)
    add(t, 2, parent=1)
    add(t, 3, parent=2)
    add(t, 4, parent=3)
    tree = ProcessTree(t)
    assert {d.pid for d in tree.descendants_of(1)} == {2, 3, 4}


def test_ancestors_climbs_parent_chain():
    t = ProcessTable()
    add(t, 1)
    add(t, 2, parent=1)
    add(t, 3, parent=2)
    tree = ProcessTree(t)
    assert [a.pid for a in tree.ancestors_of(3)] == [2, 1]


def test_reap_marks_descendants_zombie():
    t = ProcessTable()
    add(t, 1)
    add(t, 2, parent=1)
    add(t, 3, parent=2)
    # Mark some as already terminal to verify they're skipped
    t.get(3).transition(AgentState.DONE)

    tree = ProcessTree(t)
    n = tree.reap_descendants(1, reason="parent killed")
    assert n == 1                       # only 2 was alive; 3 was already terminal
    assert t.get(2).state is AgentState.ZOMBIE
    assert "reaped" in t.get(2).error
    assert t.get(3).state is AgentState.DONE  # unchanged


def test_tree_view_returns_depth_prefixed_rows():
    t = ProcessTable()
    add(t, 1)
    add(t, 2, parent=1)
    add(t, 3, parent=2)
    add(t, 4, parent=1)
    tree = ProcessTree(t)
    rows = tree.tree_view(1)
    by_pid = {r["pid"]: r["depth"] for r in rows}
    assert by_pid[1] == 0
    assert by_pid[2] == 1
    assert by_pid[3] == 2
    assert by_pid[4] == 1
