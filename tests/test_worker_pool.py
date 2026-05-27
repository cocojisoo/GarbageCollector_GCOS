"""Worker pool tests with a fake step runner (no Solar)."""

from __future__ import annotations

import threading
import time

from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import FCFSScheduler, PriorityScheduler
from gcos.kernel.worker_pool import WorkerPool


def fake_runner_done(pcb: AgentControlBlock, _client) -> bool:
    """One step → DONE."""
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.result = f"ok-{pcb.pid}"
    pcb.transition(AgentState.DONE)
    return False


def fake_runner_n_steps(n: int):
    """Returns a runner that takes `n` steps before completing."""
    state: dict[int, int] = {}

    def runner(pcb: AgentControlBlock, _client) -> bool:
        if pcb.state != AgentState.RUNNING:
            pcb.transition(AgentState.RUNNING)
        state[pcb.pid] = state.get(pcb.pid, 0) + 1
        pcb.llm_calls_used += 1
        if state[pcb.pid] >= n:
            pcb.result = f"done-after-{n}"
            pcb.transition(AgentState.DONE)
            return False
        return True

    return runner


def mkpcb(pid: int, prio: int = 5) -> AgentControlBlock:
    return AgentControlBlock(pid=pid, name=f"a{pid}", prompt="x", priority=prio)


def _new_pool(scheduler, runner, *, workers=2, quota_total=100):
    q = ReadyQueue()
    t = ProcessTable()
    qta = Quota(quota_total)
    pool = WorkerPool(workers, q, scheduler, t, qta, step_runner=runner, idle_poll_s=0.05)
    return pool, q, t, qta


def test_fcfs_processes_all_agents():
    pool, q, t, _ = _new_pool(FCFSScheduler(), fake_runner_done, workers=3)
    pcbs = [mkpcb(i) for i in range(1, 6)]
    for p in pcbs:
        t.add(p)
        q.put(p)

    pool.start()
    try:
        assert pool.wait_idle(timeout=3.0)
    finally:
        pool.shutdown()

    for p in pcbs:
        assert p.state == AgentState.DONE
        assert p.result == f"ok-{p.pid}"


def test_quota_exhaustion_blocks_then_resumes():
    pool, q, t, qta = _new_pool(FCFSScheduler(), fake_runner_done,
                                workers=1, quota_total=2)
    for i in range(1, 5):
        p = mkpcb(i)
        t.add(p)
        q.put(p)

    pool.start()
    try:
        # First 2 should complete; remaining 2 are stuck on quota
        time.sleep(0.5)
        done_before = sum(1 for p in t.snapshot() if p.state == AgentState.DONE)
        assert done_before == 2
        # Top up: should let the rest finish
        qta.topup(10)
        assert pool.wait_idle(timeout=3.0)
        done_after = sum(1 for p in t.snapshot() if p.state == AgentState.DONE)
        assert done_after == 4
    finally:
        pool.shutdown()


def test_multi_step_agent_requeues():
    pool, q, t, _ = _new_pool(FCFSScheduler(), fake_runner_n_steps(3), workers=1)
    pcb = mkpcb(1)
    t.add(pcb)
    q.put(pcb)

    pool.start()
    try:
        assert pool.wait_idle(timeout=3.0)
    finally:
        pool.shutdown()

    assert pcb.state == AgentState.DONE
    assert pcb.llm_calls_used == 3


def test_priority_scheduler_runs_highest_first():
    """With a single worker we observe ordering deterministically."""
    pool, q, t, _ = _new_pool(PriorityScheduler(), fake_runner_done, workers=1)
    order: list[int] = []
    lock = threading.Lock()

    def recording(pcb, c):
        with lock:
            order.append(pcb.pid)
        return fake_runner_done(pcb, c)

    pool.step_runner = recording

    # Insert in random order; priorities mean: pid 3 > pid 1 > pid 2
    t.add(mkpcb(1, prio=5)); q.put(t.get(1))
    t.add(mkpcb(2, prio=1)); q.put(t.get(2))
    t.add(mkpcb(3, prio=9)); q.put(t.get(3))

    pool.start()
    try:
        assert pool.wait_idle(timeout=3.0)
    finally:
        pool.shutdown()

    assert order == [3, 1, 2]


def test_shutdown_stops_workers():
    pool, q, t, _ = _new_pool(FCFSScheduler(), fake_runner_done, workers=2)
    pool.start()
    assert all(th.is_alive() for th in pool._threads)
    pool.shutdown(wait=True, timeout=2.0)
    assert all(not th.is_alive() for th in pool._threads)
