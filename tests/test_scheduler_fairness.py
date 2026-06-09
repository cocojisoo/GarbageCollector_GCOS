"""Scheduler completeness/fairness tests (category C): RoundRobin quantum (C8)
and priority aging / anti-starvation (C9)."""

from __future__ import annotations

import threading
import time

from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import (
    FCFSScheduler,
    PriorityScheduler,
    RoundRobinScheduler,
)
from gcos.kernel.worker_pool import WorkerPool


def mkpcb(pid: int, prio: int = 5) -> AgentControlBlock:
    return AgentControlBlock(pid=pid, name=f"a{pid}", prompt="x", priority=prio)


def _n_step_recorder(steps_needed: int, order: list, lock: threading.Lock):
    counts: dict[int, int] = {}

    def runner(pcb: AgentControlBlock, _c) -> bool:
        with lock:
            order.append(pcb.pid)
        if pcb.state != AgentState.RUNNING:
            pcb.transition(AgentState.RUNNING)
        counts[pcb.pid] = counts.get(pcb.pid, 0) + 1
        pcb.llm_calls_used += 1
        if counts[pcb.pid] >= steps_needed:
            pcb.transition(AgentState.DONE)
            return False
        return True

    return runner


def _run(scheduler, runner) -> None:
    q, table, quota = ReadyQueue(), ProcessTable(), Quota(1000)
    pool = WorkerPool(1, q, scheduler, table, quota, step_runner=runner, idle_poll_s=0.005)
    for pid in (1, 2):
        p = mkpcb(pid)
        table.add(p)
        q.put(p)
    pool.start()
    try:
        assert pool.wait_idle(timeout=5.0)
    finally:
        pool.shutdown()


# --- C8: FCFS is non-preemptive, RR preempts on a quantum -------------------

def test_fcfs_is_non_preemptive_runs_to_completion():
    """FCFS (quantum=None): the first agent runs ALL its calls before the second
    starts — the textbook convoy effect, and the genuine contrast with RR."""
    order: list[int] = []
    _run(FCFSScheduler(), _n_step_recorder(4, order, threading.Lock()))
    assert order == [1, 1, 1, 1, 2, 2, 2, 2]


def test_rr_runs_quantum_calls_before_yielding():
    """RR(quantum=2): each agent runs 2 consecutive calls, then yields — the two
    multi-step agents interleave, unlike non-preemptive FCFS above."""
    order: list[int] = []
    _run(RoundRobinScheduler(quantum=2), _n_step_recorder(4, order, threading.Lock()))
    assert order == [1, 1, 2, 2, 1, 1, 2, 2]


def test_rr_quantum_one_rotates_every_call_unlike_fcfs():
    """RR(quantum=1) rotates after every call — distinct from non-preemptive
    FCFS, proving the difference is preemption (quantum), not selection."""
    order: list[int] = []
    _run(RoundRobinScheduler(quantum=1), _n_step_recorder(4, order, threading.Lock()))
    assert order == [1, 2, 1, 2, 1, 2, 1, 2]


def test_fcfs_and_rr_coincide_for_single_shot_agents():
    """The honest degeneration: when every agent is one call long, FCFS and RR
    produce the same order (RR with quantum >= burst == FCFS)."""
    order_fcfs: list[int] = []
    _run(FCFSScheduler(), _n_step_recorder(1, order_fcfs, threading.Lock()))
    order_rr: list[int] = []
    _run(RoundRobinScheduler(quantum=2), _n_step_recorder(1, order_rr, threading.Lock()))
    assert order_fcfs == order_rr == [1, 2]


# --- C9: priority aging / anti-starvation -----------------------------------

def test_aging_lets_a_starved_agent_overtake_higher_priority():
    sched = PriorityScheduler(aging_rate_per_s=0.1, aging_max_bonus=8.0)
    q = ReadyQueue()
    starved = mkpcb(1, prio=1)
    starved.created_at = time.time() - 1000      # waited long → +8 bonus → eff 9
    fresh_high = mkpcb(2, prio=8)                 # just arrived → eff 8
    q.put(starved)
    q.put(fresh_high)
    assert sched.pick_next(q).pid == 1            # starved low-prio wins


def test_without_aging_higher_priority_always_wins():
    sched = PriorityScheduler(aging_rate_per_s=0.0)
    q = ReadyQueue()
    starved = mkpcb(1, prio=1)
    starved.created_at = time.time() - 1000
    fresh_high = mkpcb(2, prio=8)
    q.put(starved)
    q.put(fresh_high)
    assert sched.pick_next(q).pid == 2            # strict priority


def test_aging_preserves_priority_order_for_prompt_dispatch():
    """Agents enqueued and picked immediately still come out priority-descending
    (the aging bonus is negligible over sub-second waits)."""
    sched = PriorityScheduler()  # default gentle aging
    q = ReadyQueue()
    for pid, prio in [(1, 5), (2, 1), (3, 9), (4, 7)]:
        q.put(mkpcb(pid, prio))
    picked = [sched.pick_next(q).priority for _ in range(4)]
    assert picked == [9, 7, 5, 1]
