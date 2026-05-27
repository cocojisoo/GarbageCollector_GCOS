"""End-to-end IPC test: producer pipes output to consumer via the bus.

Uses a fake step_runner so no Solar calls are made.
"""

from __future__ import annotations

from gcos.kernel import AgentState, Kernel, KernelConfig


def fake_step(pcb, _client) -> bool:
    """One step → DONE. Result = '<NAME>:<prompt-after-substitution>'."""
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.tokens_used += 1
    pcb.result = f"{pcb.name}:{pcb.prompt}"
    pcb.transition(AgentState.DONE)
    return False


def _kernel(workers=2):
    return Kernel(
        KernelConfig(scheduler="fcfs", workers=workers, quota_total=50),
        client_factory=lambda: None,
        step_runner=fake_step,
    )


def test_producer_pipes_to_consumer_with_input_placeholder():
    with _kernel() as k:
        consumer_pid_holder = []

        # Spawn consumer first so it has a known PID, then producer with pipe_to.
        consumer_pid = k.spawn(
            "use upstream: {INPUT}",
            name="consumer",
            input_from=None,  # will be set after producer
        )
        consumer_pid_holder.append(consumer_pid)
        # Producer pipes its result to consumer
        producer_pid = k.spawn(
            "PRODUCED",
            name="producer",
            pipe_to=consumer_pid,
        )
        # Tell the consumer to expect input from producer
        k.get(consumer_pid).input_from = producer_pid

        assert k.wait_idle(timeout=3.0)

        producer = k.get(producer_pid)
        consumer = k.get(consumer_pid)
        assert producer.state is AgentState.DONE
        assert consumer.state is AgentState.DONE
        # The fake_step echoes the *resolved* prompt. The placeholder must
        # have been substituted with the producer's result.
        assert "producer:PRODUCED" in consumer.result


def test_kill_cascades_to_children():
    with _kernel(workers=0) as k:  # no workers — agents stay READY
        root = k.spawn("root prompt", name="root")
        c1 = k.spawn("c1", name="c1", parent_pid=root)
        c2 = k.spawn("c2", name="c2", parent_pid=root)
        gc = k.spawn("gc", name="gc", parent_pid=c1)
        assert k.kill(root) is True
        # all descendants reaped to ZOMBIE
        for pid in (c1, c2, gc):
            assert k.get(pid).state is AgentState.ZOMBIE
            assert "reaped" in k.get(pid).error


def test_pipe_to_with_no_input_placeholder_is_still_delivered():
    """An agent without {INPUT} can still receive (and ignore) piped data."""
    with _kernel() as k:
        downstream = k.spawn("static prompt", name="downstream")
        upstream = k.spawn("UP", name="up", pipe_to=downstream)
        assert k.wait_idle(timeout=3.0)
        # Both complete; downstream just ran with its original prompt.
        assert k.get(downstream).state is AgentState.DONE
        assert k.get(upstream).state is AgentState.DONE
