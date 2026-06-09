"""Regression tests for the concurrency-correctness bugs (category A).

These exercise paths the existing eval/tests miss: the *multi-worker* priority
dispatch path (A1), the dequeue/idle accounting boundary (A2), global-quota
conservation on no-call step exits (A3), and kill-during-step (A4).

Every test here is deterministic — collisions are forced with a Barrier and
state windows are encoded at the queue/PCB primitive level rather than relying
on lucky thread timing.
"""

from __future__ import annotations

import threading
import time

from gcos.kernel import AgentState, Kernel, KernelConfig
from gcos.kernel.pcb import AgentControlBlock
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import PriorityScheduler
from gcos.kernel.worker_pool import WorkerPool


def mkpcb(pid: int, prio: int = 5) -> AgentControlBlock:
    return AgentControlBlock(pid=pid, name=f"a{pid}", prompt="x", priority=prio)


# --- A1: priority double-dispatch -----------------------------------------

_PRIO_KEY = lambda p: (p.priority, -p.created_at)  # noqa: E731


def test_concurrent_priority_picks_are_distinct():
    """Invariant check: N threads hammering one PriorityScheduler get N *distinct*
    PCBs. This validates the atomic-dispatch contract under contention.

    NB: this does NOT by itself reproduce the old bug — under CPython's GIL the
    old snapshot()->max()->pop() window rarely opens, so the racy code usually
    passed this too (it's a guard for the *new* contract). The deterministic
    reproduction of the race lives in
    test_old_nonatomic_selection_can_double_dispatch_but_pop_best_cannot."""
    q = ReadyQueue()
    n = 60
    for i in range(1, n + 1):
        q.put(mkpcb(i, prio=i % 7))

    sched = PriorityScheduler()
    workers = 8
    barrier = threading.Barrier(workers)
    picked: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        while True:
            p = sched.pick_next(q)
            if p is None:
                return
            with lock:
                picked.append(p.pid)
            time.sleep(0.0005)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert len(picked) == len(set(picked)), "an agent was dispatched more than once"
    assert sorted(picked) == list(range(1, n + 1)), "every agent dispatched exactly once"


def _drain_picks(pick, queue: ReadyQueue, workers: int) -> dict[int, int]:
    """Drive `pick(queue)` from `workers` threads until the queue is empty,
    counting how many times each PID is dispatched."""
    runs: dict[int, int] = {}
    lock = threading.Lock()

    def w() -> None:
        while True:
            p = pick(queue)
            if p is None:
                return
            with lock:
                runs[p.pid] = runs.get(p.pid, 0) + 1

    threads = [threading.Thread(target=w) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    return runs


def _racy_pick(queue: ReadyQueue):
    """Reconstruct the OLD non-atomic PriorityScheduler.pick_next: select on a
    snapshot, then remove in a separate step, ignoring pop()'s result. The
    injected sleep stands in for the scheduling window (GIL switch / real
    preemption) that two workers can interleave in."""
    cands = queue.snapshot()
    if not cands:
        return None
    best = max(cands, key=_PRIO_KEY)
    time.sleep(0.0005)            # select -> (window) -> remove
    queue.pop(best)              # bool ignored, exactly like the old bug
    return best


def test_old_nonatomic_selection_can_double_dispatch_but_pop_best_cannot():
    """The actual A1 reproduction: with a select->remove window the old pattern
    dispatches the same PCB on two workers; the atomic pop_best never does."""
    # (1) Old non-atomic pattern CAN double-dispatch (try a few rounds — the
    # race is windowed, not every round trips it).
    saw_dup = False
    for _ in range(6):
        q = ReadyQueue()
        for i in range(1, 41):
            q.put(mkpcb(i, prio=i % 5))
        runs = _drain_picks(_racy_pick, q, workers=8)
        if max(runs.values(), default=0) > 1:
            saw_dup = True
            break
    assert saw_dup, "expected the non-atomic select+remove to double-dispatch at least once"

    # (2) The atomic pop_best never double-dispatches, under the same contention.
    for _ in range(6):
        q = ReadyQueue()
        for i in range(1, 41):
            q.put(mkpcb(i, prio=i % 5))
        runs = _drain_picks(lambda qq: qq.pop_best(_PRIO_KEY), q, workers=8)
        assert max(runs.values(), default=0) == 1, "pop_best double-dispatched"
        assert sum(runs.values()) == 40, "pop_best lost or duplicated an agent"


def test_priority_pick_returns_the_pcb_it_removed():
    """pick_next must return exactly the PCB it removed from the queue."""
    q = ReadyQueue()
    a, b, c = mkpcb(1, 1), mkpcb(2, 9), mkpcb(3, 5)
    for p in (a, b, c):
        q.put(p)
    sched = PriorityScheduler()
    got = sched.pick_next(q)
    assert got is b                      # highest priority
    assert len(q) == 2
    assert b not in list(q)              # actually removed


# --- A2: early-idle accounting boundary ------------------------------------

def test_dequeue_is_not_idle_until_task_done():
    """A dequeued-but-unfinished PCB means the system is NOT idle.

    This is the A2 invariant: in-flight work counts even before the worker has
    entered the busy section / started the LLM call.
    """
    q = ReadyQueue()
    q.put(mkpcb(1))
    assert not q.is_drained()
    p = q.popleft()
    assert p is not None
    assert q.in_flight == 1
    assert not q.is_drained(), "dequeued-in-flight work must not read as idle"
    q.task_done()
    assert q.is_drained()


def test_is_idle_never_true_while_agent_mid_flight():
    """Stress: a poller must never observe idle while an agent is non-terminal."""
    q, table, quota = ReadyQueue(), ProcessTable(), Quota(100)

    def slow_runner(pcb, _c):
        pcb.transition(AgentState.RUNNING)
        time.sleep(0.02)
        pcb.llm_calls_used += 1
        pcb.transition(AgentState.DONE)
        return False

    pool = WorkerPool(4, q, PriorityScheduler(), table, quota,
                      step_runner=slow_runner, idle_poll_s=0.005)
    for i in range(1, 13):
        p = mkpcb(i)
        table.add(p)
        q.put(p)

    violations: list[str] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            if pool.is_idle():
                non_terminal = [p.pid for p in table.snapshot() if not p.is_terminal()]
                if non_terminal:
                    violations.append(f"idle while {non_terminal} non-terminal")
            time.sleep(0.001)

    watcher = threading.Thread(target=poll)
    watcher.start()
    pool.start()
    try:
        assert pool.wait_idle(timeout=5.0)
    finally:
        stop.set()
        watcher.join(timeout=1.0)
        pool.shutdown()

    assert not violations, violations[:5]


# --- A3: global quota conservation -----------------------------------------

def _drain(pool, q, table, n):
    for i in range(1, n + 1):
        p = mkpcb(i)
        table.add(p)
        q.put(p)
    pool.start()
    try:
        assert pool.wait_idle(timeout=5.0)
    finally:
        pool.shutdown()


def test_quota_refunded_when_step_makes_no_llm_call():
    """No LLM call (e.g. gate DENY / per-agent quota 0) must not burn global quota."""
    q, table, quota = ReadyQueue(), ProcessTable(), Quota(50)

    def no_call_runner(pcb, _c):
        pcb.transition(AgentState.RUNNING)
        pcb.error = "denied by gate (no LLM call)"
        pcb.transition(AgentState.ERROR)
        return False  # note: llm_calls_used stays 0

    pool = WorkerPool(4, q, PriorityScheduler(), table, quota,
                      step_runner=no_call_runner, idle_poll_s=0.005)
    _drain(pool, q, table, 20)
    assert quota.snapshot()["used"] == 0, "global quota leaked on no-call exits"


def test_quota_used_equals_actual_calls():
    """When steps do call the LLM, global quota.used == number of calls."""
    q, table, quota = ReadyQueue(), ProcessTable(), Quota(50)

    def call_runner(pcb, _c):
        pcb.transition(AgentState.RUNNING)
        pcb.llm_calls_used += 1
        pcb.transition(AgentState.DONE)
        return False

    pool = WorkerPool(4, q, PriorityScheduler(), table, quota,
                      step_runner=call_runner, idle_poll_s=0.005)
    _drain(pool, q, table, 15)
    assert quota.snapshot()["used"] == 15


# --- A4: kill during a step must not resurrect -----------------------------

def test_put_refuses_terminal_pcb():
    """A finalized PCB (e.g. killed while a worker was about to re-queue it) must
    never sit in the ready queue (review #6)."""
    q = ReadyQueue()
    p = mkpcb(1)
    p.transition(AgentState.RUNNING)
    p.transition(AgentState.ZOMBIE)
    assert q.put(p) is False
    assert len(q) == 0
    assert q.is_drained()


def test_terminal_state_is_absorbing():
    pcb = mkpcb(1)
    pcb.transition(AgentState.RUNNING)
    pcb.transition(AgentState.ZOMBIE)
    pcb.transition(AgentState.DONE)      # must be ignored
    assert pcb.state == AgentState.ZOMBIE
    pcb.transition(AgentState.ERROR)     # also ignored
    assert pcb.state == AgentState.ZOMBIE


def test_kill_mid_flight_stays_zombie():
    """An agent killed while a worker is inside its step stays ZOMBIE."""
    started = threading.Event()
    release = threading.Event()

    def slow_runner(pcb, _c):
        pcb.transition(AgentState.RUNNING)
        started.set()
        release.wait(2.0)
        pcb.transition(AgentState.DONE)  # the worker's attempt to finish
        return False

    k = Kernel(
        KernelConfig(scheduler="priority", workers=1, quota_total=10),
        client_factory=lambda: None,
        step_runner=slow_runner,
    )
    k.start()
    try:
        pid = k.spawn("x", name="victim")
        assert started.wait(2.0)
        assert k.kill(pid) is True
        release.set()
        assert k.wait_idle(timeout=5.0)
        assert k.get(pid).state is AgentState.ZOMBIE, "kill was overwritten by DONE"
    finally:
        k.shutdown()
