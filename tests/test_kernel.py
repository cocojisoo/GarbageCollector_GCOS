"""Kernel façade tests — wires everything together with a fake step runner."""

from __future__ import annotations

from gcos.kernel import AgentState, Kernel, KernelConfig


def fake_runner(pcb, _client) -> bool:
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.tokens_used += 5
    pcb.result = f"ok-{pcb.pid}"
    pcb.transition(AgentState.DONE)
    return False


def _build(scheduler="fcfs", workers=2):
    return Kernel(
        KernelConfig(scheduler=scheduler, workers=workers, quota_total=100),
        client_factory=lambda: None,
        step_runner=fake_runner,
    )


def test_spawn_and_complete():
    with _build() as k:
        pid = k.spawn("hi", name="t1")
        assert k.wait_idle(timeout=2.0)
        pcb = k.get(pid)
        assert pcb.state == AgentState.DONE
        assert pcb.result == f"ok-{pid}"


def test_status_reports_counts():
    with _build() as k:
        for i in range(3):
            k.spawn(f"prompt {i}")
        assert k.wait_idle(timeout=2.0)
        s = k.status()
        assert s["scheduler"] == "fcfs"
        assert s["total_agents"] == 3
        assert s["by_state"].get("DONE") == 3
        assert s["quota"]["used"] == 3
        assert s["quota"]["remaining"] == 97


def test_kill_marks_zombie_if_still_ready():
    """An agent in the queue can be killed before a worker grabs it."""
    k = Kernel(
        KernelConfig(scheduler="fcfs", workers=0, quota_total=10),
        client_factory=lambda: None,
        step_runner=fake_runner,
    )
    # Don't start workers — agent stays READY forever.
    pid = k.spawn("never runs")
    assert k.kill(pid) is True
    assert k.get(pid).state == AgentState.ZOMBIE
    # Killing again is a no-op
    assert k.kill(pid) is False


def test_parent_child_linkage():
    with _build() as k:
        parent_pid = k.spawn("parent")
        child_pid = k.spawn("child", parent_pid=parent_pid)
        assert k.wait_idle(timeout=2.0)
        parent = k.get(parent_pid)
        child = k.get(child_pid)
        assert child.parent_pid == parent_pid
        assert child_pid in parent.children


def test_context_manager_protocol():
    k = _build()
    assert k._started is False
    with k:
        assert k._started is True
    assert k._started is False
