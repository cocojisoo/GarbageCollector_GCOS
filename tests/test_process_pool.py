"""Tests for the process-backed executor — agents as real OS processes.

The fork+pipe lifecycle and real-kill work on any POSIX host (incl. macOS); the
per-agent cgroup CFS (higher priority finishes sooner) is Linux-only and lives in
`measure_live_per_agent_cfs` / the CI privileged job.
"""

from __future__ import annotations

import time

from gcos.kernel.kernel import Kernel, KernelConfig
from gcos.kernel.pcb import AgentState


def _done_runner(result_prefix="ok"):
    def runner(pcb, _client):
        x = 1
        for _ in range(20_000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        pcb.result = f"{result_prefix}-{pcb.pid}"
        pcb.llm_calls_used += 1
        pcb.transition(AgentState.DONE)
        return False
    return runner


def test_process_executor_runs_agents_as_real_processes_to_completion():
    cfg = KernelConfig(scheduler="fcfs", workers=3, quota_total=100,
                       executor_backend="process")
    k = Kernel(cfg, client_factory=lambda: None, step_runner=_done_runner())
    k.start()
    pids = [k.spawn(f"task {i}", name=f"a{i}") for i in range(6)]
    assert k.wait_idle(timeout=20)
    # Results computed in the child processes came back over the pipe.
    for pid in pids:
        pcb = k.get(pid)
        assert pcb.state is AgentState.DONE
        assert pcb.result == f"ok-{pid}"
        assert pcb.llm_calls_used == 1
    # Global quota was charged post-hoc by the calls the children reported.
    assert k.quota.snapshot()["used"] == 6
    k.shutdown()


def test_process_executor_uses_process_pool_backend():
    from gcos.kernel.process_pool import ProcessWorkerPool
    k = Kernel(KernelConfig(workers=1, executor_backend="process"),
               client_factory=lambda: None, step_runner=_done_runner())
    assert isinstance(k.pool, ProcessWorkerPool)


def test_thread_backend_is_the_default():
    from gcos.kernel.worker_pool import WorkerPool
    k = Kernel(KernelConfig(workers=1), client_factory=lambda: None,
               step_runner=_done_runner())
    assert isinstance(k.pool, WorkerPool)


def test_process_executor_is_fork_safe_when_children_log_under_contention():
    """Regression for the fork-deadlock: a forked agent child that LOGS must not
    deadlock on RingTraceLog's private lock when a sibling thread is hammering the
    log at the fork instant. Without the os.register_at_fork lock reset this hangs."""
    import logging
    import threading
    from gcos.kernel.ring_log import RingTraceLog

    ring = RingTraceLog(256)
    glog = logging.getLogger("gcos")
    glog.addHandler(ring)
    forklog = logging.getLogger("gcos.forktest")
    stop = threading.Event()

    def spammer():
        while not stop.is_set():
            forklog.info("hammer the ring lock")

    sp = threading.Thread(target=spammer, daemon=True)
    sp.start()

    def logging_runner(pcb, _client):
        forklog.info("agent %d running in child", pcb.pid)  # child logs → would deadlock if buggy
        pcb.result = f"ok-{pcb.pid}"
        pcb.transition(AgentState.DONE)
        return False

    cfg = KernelConfig(scheduler="fcfs", workers=4, quota_total=500,
                       executor_backend="process")
    k = Kernel(cfg, client_factory=lambda: None, step_runner=logging_runner)
    try:
        k.start()
        pids = [k.spawn(f"t{i}", name=f"a{i}") for i in range(16)]
        ok = k.wait_idle(timeout=25)
        assert ok, "process pool hung — fork-deadlock regression"
        assert all(k.get(p).state is AgentState.DONE for p in pids)
    finally:
        stop.set()
        k.shutdown()
        glog.removeHandler(ring)


def test_process_executor_kill_terminates_a_real_child():
    """kill() SIGKILLs the agent's real child process — a genuine kernel signal,
    not just a state flip. The agent here would otherwise spin past the kill."""
    def slow_runner(pcb, _client):
        deadline = time.time() + 10.0
        x = 1
        while time.time() < deadline:
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        pcb.result = "should-have-been-killed"
        pcb.transition(AgentState.DONE)
        return False

    cfg = KernelConfig(scheduler="fcfs", workers=1, quota_total=100,
                       executor_backend="process")
    k = Kernel(cfg, client_factory=lambda: None, step_runner=slow_runner)
    k.start()
    pid = k.spawn("spin", name="victim")
    # Wait until the child is actually running (registered), then kill it.
    pool = k.pool
    t0 = time.time()
    while time.time() - t0 < 5.0:
        with pool._children_lock:
            running = pid in pool._children
        if running:
            break
        time.sleep(0.02)
    assert k.kill(pid) is True
    assert k.wait_idle(timeout=10)   # the pool unblocks quickly once the child dies
    pcb = k.get(pid)
    assert pcb.state is AgentState.ZOMBIE
    assert pcb.result != "should-have-been-killed"  # never completed its work
    k.shutdown()
