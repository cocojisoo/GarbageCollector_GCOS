"""Lifecycle / leak tests (category E): bounded WAITING + reliable pipe (E16),
mailbox + terminal-entry reaping (E14)."""

from __future__ import annotations

from gcos.ipc.message_bus import MessageBus
from gcos.kernel import AgentState, Kernel, KernelConfig


def fake_step(pcb, _c) -> bool:
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.result = f"{pcb.name}-done"
    pcb.transition(AgentState.DONE)
    return False


def _kernel(workers=2):
    return Kernel(
        KernelConfig(scheduler="fcfs", workers=workers, quota_total=50),
        client_factory=lambda: None,
        step_runner=fake_step,
    )


# --- E16: a consumer must not wait forever ----------------------------------

def test_consumer_fails_when_input_never_arrives():
    """A producer that never pipes must not strand the consumer in WAITING."""
    with _kernel() as k:
        prod = k.spawn("produce", name="prod")           # no pipe_to → never sends
        cons = k.spawn("use {INPUT}", name="cons", input_from=prod, timeout_s=0.3)
        assert k.wait_idle(timeout=5.0)                  # the queue actually drains
        c = k.get(cons)
        assert c.state is AgentState.ERROR
        assert "input never arrived" in (c.error or "")
        assert k.get(prod).state is AgentState.DONE


def test_send_block_returns_false_on_full_mailbox():
    bus = MessageBus(mailbox_capacity=1)
    assert bus.send(1, {"content": "a"}) is True
    assert bus.send(1, {"content": "b"}, block=True, timeout=0.1) is False


def test_send_block_delivers_when_room():
    bus = MessageBus(mailbox_capacity=1)
    assert bus.send(1, {"content": "a"}) is True
    assert bus.recv(1)["content"] == "a"
    assert bus.send(1, {"content": "b"}, block=True, timeout=0.1) is True


# --- E14: mailbox + terminal-entry reaping ----------------------------------

def test_kill_drops_mailboxes_of_victim_and_descendants():
    k = _kernel(workers=0)  # no workers — agents stay parked
    with k:
        root = k.spawn("root", name="root")
        child = k.spawn("child", name="child", parent_pid=root)
        k.bus.send(root, k.bus.make_result(99, "x"))
        k.bus.send(child, k.bus.make_result(99, "y"))
        assert root in k.bus.snapshot() and child in k.bus.snapshot()
        assert k.kill(root) is True
        snap = k.bus.snapshot()
        assert root not in snap and child not in snap


def test_reap_terminal_removes_finished_keeps_others():
    with _kernel(workers=0) as k:
        running = k.spawn("never runs")     # stays READY (no workers)
        # Manually finish one agent so we have a terminal entry to reap.
        done = k.spawn("x")
        k.get(done).transition(AgentState.RUNNING)
        k.get(done).transition(AgentState.DONE)
        k.bus.send(done, k.bus.make_result(9, "z"))

        reaped = k.reap_terminal()
        assert reaped == 1
        assert k.get(done) is None              # removed
        assert done not in k.bus.snapshot()     # mailbox freed
        assert k.get(running) is not None       # untouched


def test_shutdown_drops_all_mailboxes():
    k = _kernel(workers=0)
    k.start()
    pid = k.spawn("x")
    k.bus.send(pid, k.bus.make_result(9, "x"))
    assert k.bus.snapshot() != {}
    k.shutdown()
    assert k.bus.snapshot() == {}
