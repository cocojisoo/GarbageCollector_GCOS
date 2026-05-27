from gcos.kernel.pcb import (
    AgentControlBlock,
    AgentState,
    CapabilitySet,
    ContextPage,
)


def test_default_state_is_new():
    pcb = AgentControlBlock(pid=1, name="t", prompt="hi")
    assert pcb.state == AgentState.NEW
    assert pcb.started_at is None
    assert pcb.is_terminal() is False


def test_transition_running_sets_started_at():
    pcb = AgentControlBlock(pid=1, name="t", prompt="hi")
    pcb.transition(AgentState.RUNNING)
    assert pcb.started_at is not None
    assert pcb.finished_at is None


def test_transition_done_is_terminal():
    pcb = AgentControlBlock(pid=1, name="t", prompt="hi")
    pcb.transition(AgentState.RUNNING)
    pcb.transition(AgentState.DONE)
    assert pcb.is_terminal()
    assert pcb.finished_at is not None
    assert pcb.wall_time() >= 0


def test_capability_default_is_safe():
    cap = CapabilitySet.default_user()
    assert cap.can_call_llm is True
    assert cap.can_exec_code is False
    assert cap.can_net is False
    assert cap.can_fs_write is False


def test_capability_coder_can_exec():
    cap = CapabilitySet.coder()
    assert cap.can_exec_code is True
    assert cap.can_spawn_child is True


def test_context_page_touch_updates_time():
    p = ContextPage(role="user", content="hi", tokens=1, last_access=0.0)
    p.touch()
    assert p.last_access > 0


def test_to_row_has_all_columns():
    pcb = AgentControlBlock(pid=42, name="researcher", prompt="x", priority=7)
    row = pcb.to_row()
    assert row["pid"] == 42
    assert row["state"] == "NEW"
    assert row["prio"] == 7
    for key in ("pid", "name", "state", "prio", "quota", "tokens", "calls", "wall"):
        assert key in row
