"""Process-backed worker pool — agents as REAL OS processes.

The thread `WorkerPool` orchestrates agents in one process; under CPython's GIL,
per-thread `cpu.weight` is meaningless (it would be theater — we measured a flat
0/0). This backend runs each agent in its OWN forked child process, placed in a
per-agent cgroup whose `cpu.weight` comes from the agent's priority — so the
**Linux CFS scheduler** gives a higher-priority agent a larger CPU share, for
real, in the LIVE execution path (not just the eval). The agent is a real
process, so kill/stop are real kernel signals.

This is the live-path analogue of what the xv6 teams do (agents as kernel
processes), on the host kernel. Opt-in: `Kernel(executor_backend="process")` /
`GCOS_EXEC=process`; the default stays the thread pool. On Linux it gives
per-agent CFS; on any POSIX host the process isolation + real signals still hold
(the cgroup weighting degrades to none, loudly).

Honest trade-offs (vs. the thread pool), stated rather than hidden:
- Each agent process gets its OWN Solar client, so the shared in-process
  batcher's global concurrency cap becomes per-process.
- Agents **run to completion** in their child process — this backend does NOT
  honor the scheduler quantum (RR/Priority call-level preemption is a no-op
  here); CPU fairness comes from the per-agent cgroup weight instead.
- The global quota is RESERVED up front (admission control) and the unspent
  remainder refunded after; a SIGKILLed child keeps its reservation charged
  (we can't know how many calls it made), so we never under-count the budget.
- Forking from a multi-threaded parent: app-level locks are reset in the child
  (Python resets the logging locks via os.register_at_fork; RingTraceLog resets
  its private lock), and the child avoids touching inherited locks until the
  step runner. Full process isolation (forkserver/posix_spawn) is future work.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import warnings
from typing import Optional

from gcos.ipc.message_bus import MessageBus, resolve_input_placeholder
from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import Scheduler


log = logging.getLogger(__name__)

# Fields the child reports back to the parent (over a pipe) after running.
# quota_remaining is NOT reported — the parent computes it from the reservation.
_RESULT_FIELDS = ("result", "error", "tokens_used", "llm_calls_used")
_CHILD_STEP_CAP = 64  # safety bound on steps per agent in-child


def _fork() -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # see realproc._fork
        return os.fork()


class ProcessWorkerPool:
    """Drop-in alternative to WorkerPool that runs each agent as a real process."""

    def __init__(self, num_workers: int, ready_queue: ReadyQueue, scheduler: Scheduler,
                 process_table: ProcessTable, quota: Quota, *,
                 client_factory=None, step_runner=None, bus: Optional[MessageBus] = None,
                 idle_poll_s: float = 0.2, input_wait_s: float = 0.1,
                 input_deadline_s: Optional[float] = None, **_ignored) -> None:
        self.num_workers = num_workers
        self.queue = ready_queue
        self.scheduler = scheduler
        self.table = process_table
        self.quota = quota
        self.bus = bus
        self.client_factory = client_factory
        if step_runner is None:
            from gcos.executor import run_step as step_runner  # noqa: PLW2901
        self.step_runner = step_runner
        self.idle_poll_s = idle_poll_s
        self.input_wait_s = input_wait_s
        # Bound how long an agent may stay parked WAITING on upstream input, so a
        # never-arriving {INPUT} can't infinitely re-park (E16). None → the PCB's
        # own timeout_s.
        self.input_deadline_s = input_deadline_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._children: dict[int, int] = {}   # pcb.pid -> child OS pid (for real kill)
        self._children_lock = threading.Lock()
        self.client = None  # parity with WorkerPool introspection

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("ProcessWorkerPool already started")
        from gcos.osprims import warn_if_degraded
        warn_if_degraded()
        for i in range(self.num_workers):
            t = threading.Thread(target=self._loop, args=(i,),
                                 name=f"gcos-proc-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("ProcessWorkerPool started: %d workers, agents run as real processes",
                 self.num_workers)

    def shutdown(self, wait: bool = True, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._children_lock:
            for cpid in list(self._children.values()):
                try:
                    os.kill(cpid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if wait:
            for t in self._threads:
                t.join(timeout=timeout)
        self._threads.clear()
        log.info("ProcessWorkerPool stopped")

    # --- introspection ------------------------------------------------------

    @property
    def busy(self) -> int:
        with self._busy_lock:
            return self._busy

    def is_idle(self) -> bool:
        return self.queue.is_drained()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        return self.queue.wait_drained(timeout=timeout)

    def kill_running(self, pcb_pid: int) -> bool:
        """SIGKILL the real child process of a running agent, if any. This makes
        `kill` a genuine kernel signal, not just a state flip."""
        with self._children_lock:
            cpid = self._children.get(pcb_pid)
        if cpid is None:
            return False
        try:
            os.kill(cpid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return False

    # --- internal -----------------------------------------------------------

    def _enter_busy(self) -> None:
        with self._busy_lock:
            self._busy += 1

    def _leave_busy(self) -> None:
        with self._busy_lock:
            self._busy -= 1

    def _loop(self, idx: int) -> None:
        while not self._stop.is_set():
            if not self.queue.wait_nonempty(timeout=self.idle_poll_s):
                continue
            pcb = self.scheduler.pick_next(self.queue)
            if pcb is None:
                continue
            try:
                self._dispatch(idx, pcb)
            finally:
                self.queue.task_done()

    def _dispatch(self, idx: int, pcb: AgentControlBlock) -> None:
        if pcb.is_terminal():
            return
        # IPC: resolve upstream {INPUT} before forking (the parent owns the bus).
        if pcb.input_from is not None and "{INPUT}" in pcb.prompt:
            if self.bus is None:
                pcb.transition(AgentState.ERROR)
                pcb.error = "input_from set but no message bus"
                return
            msg = self.bus.recv(pcb.pid, timeout=self.input_wait_s)
            if msg is None:
                self._repark_waiting(pcb)
                return
            pcb.prompt = resolve_input_placeholder(pcb.prompt, msg)
            pcb.waiting_since = None

        if pcb.started_at is None:
            pcb.transition(AgentState.RUNNING)  # record start in the parent's PCB

        self._enter_busy()
        try:
            self._run_in_process(pcb)
        finally:
            self._leave_busy()

        # IPC: forward a successful result downstream (parent owns the bus).
        if pcb.state == AgentState.DONE and pcb.pipe_to is not None and self.bus is not None:
            payload = self.bus.make_result(from_pid=pcb.pid, content=pcb.result or "")
            if self.bus.send(pcb.pipe_to, payload):
                log.info("pipe: PID %d -> PID %d (%d chars)",
                         pcb.pid, pcb.pipe_to, len(payload["content"]))

    def _repark_waiting(self, pcb: AgentControlBlock) -> None:
        """No upstream input yet → re-park WAITING, but bounded: past the input
        deadline, fail to ERROR so the queue drains (a lost/never-sent pipe
        message must not strand a consumer forever — E16)."""
        now = time.time()
        if pcb.waiting_since is None:
            pcb.waiting_since = now
        deadline = self.input_deadline_s if self.input_deadline_s is not None else pcb.timeout_s
        if now - pcb.waiting_since >= deadline:
            pcb.transition(AgentState.ERROR)
            pcb.error = (f"input never arrived from PID {pcb.input_from} "
                         f"after {now - pcb.waiting_since:.1f}s")
            return
        pcb.transition(AgentState.WAITING)
        self.queue.put(pcb)

    def _run_in_process(self, pcb: AgentControlBlock) -> None:
        from gcos.osprims import cgroup as cg
        from gcos.osprims.cgroup import priority_to_weight

        # --- admission control: RESERVE the agent's global budget BEFORE forking,
        # and refund the unspent remainder after (so agents can't collectively
        # exceed the OS budget, and low-budget calls are never silently dropped).
        orig_quota = pcb.quota_remaining
        reserve = self.quota.acquire_up_to(orig_quota)
        if reserve <= 0 and orig_quota > 0:
            pcb.transition(AgentState.BLOCKED)
            self.queue.put(pcb)
            time.sleep(self.idle_poll_s)   # don't hot-spin while the budget is empty
            return
        pcb.quota_remaining = reserve       # the child may make at most `reserve` calls
        calls_before = pcb.llm_calls_used

        cgrp = None
        if cg.available():
            cgrp = cg.Cgroup(f"agent-{pcb.pid}", weight=priority_to_weight(pcb.priority))
            if not cgrp.enforced:
                cgrp = None

        r, w = os.pipe()
        pid = _fork()
        if pid == 0:
            # -------- CHILD: become the agent process --------
            # NOTE (fork-safety): we fork from a multi-threaded parent. The robust
            # mitigation against a child deadlocking on ANY logging/handler/stderr
            # lock a sibling thread held at the fork instant is to NEUTRALIZE
            # logging in the child first thing — it only needs to run the agent and
            # report the result over the pipe, never to log to the parent's
            # handlers. (We also reset RingTraceLog's private lock via
            # os.register_at_fork as belt-and-suspenders.) We likewise do NOT call
            # cgroup.add_self() here — the parent places us via add_pid (below).
            os.close(r)
            import logging as _logging
            _logging.disable(_logging.CRITICAL)
            code = 0
            try:
                client = self.client_factory() if self.client_factory else None
                safety = _CHILD_STEP_CAP
                while not pcb.is_terminal() and safety > 0:
                    keep_going = self.step_runner(pcb, client)
                    safety -= 1
                    if not keep_going:
                        break
                payload = {f: getattr(pcb, f) for f in _RESULT_FIELDS}
                payload["state"] = pcb.state.value
            except BaseException as e:  # noqa: BLE001
                payload = {"state": "ERROR", "error": f"child crash: {e}",
                           "result": getattr(pcb, "result", None),
                           "tokens_used": pcb.tokens_used,
                           "llm_calls_used": pcb.llm_calls_used}
                code = 1
            try:
                # Loop the write: a single os.write can do a PARTIAL write once the
                # pipe buffer (~64 KiB) fills, which would silently truncate a large
                # agent result into corrupt JSON. Drain the whole payload.
                view = memoryview(json.dumps(payload).encode("utf-8"))
                while view:
                    written = os.write(w, view)
                    view = view[written:]
            finally:
                os.close(w)
            os._exit(code)

        # -------- PARENT: supervise the agent process --------
        os.close(w)
        with self._children_lock:
            self._children[pcb.pid] = pid
        if cgrp is not None:
            cgrp.add_pid(pid)  # place the child under its per-agent CFS weight
        completed = False
        try:
            chunks = []
            while True:
                buf = os.read(r, 65536)
                if not buf:
                    break
                chunks.append(buf)
            completed = self._apply_result(pcb, b"".join(chunks))
        finally:
            try:
                os.close(r)
            except OSError:
                pass
            # Reap + remove the registry entry under one lock, so kill_running /
            # shutdown can never SIGKILL a PID that's been reaped (and possibly
            # reused) in the window between waitpid and pop.
            with self._children_lock:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                self._children.pop(pcb.pid, None)
            if cgrp is not None:
                cgrp.remove()

        # --- reconcile the reservation. Only refund if the child reported a
        # result; a SIGKILLed child sends nothing, so we conservatively keep the
        # whole reservation charged (never under-charge the global budget).
        made = max(0, pcb.llm_calls_used - calls_before)
        if completed:
            if reserve > made:
                self.quota.refund(reserve - made)
            pcb.quota_remaining = max(0, orig_quota - made)
        else:
            pcb.quota_remaining = max(0, orig_quota - reserve)

    def _apply_result(self, pcb: AgentControlBlock, raw: bytes) -> bool:
        """Apply the child's reported result to the PCB. Returns True if the child
        reported a valid result (vs. was killed / sent nothing)."""
        if not raw:
            if not pcb.is_terminal():
                pcb.transition(AgentState.ERROR)
                pcb.error = "agent process produced no result (killed?)"
            if pcb.finished_at is None:
                pcb.finished_at = time.time()
            return False
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            if not pcb.is_terminal():
                pcb.transition(AgentState.ERROR)
                pcb.error = "corrupt result from agent process"
            return False
        for f in _RESULT_FIELDS:
            if payload.get(f) is not None:
                setattr(pcb, f, payload[f])
        if not pcb.is_terminal():        # the child is the source of truth
            try:
                pcb.state = AgentState(payload.get("state", "ERROR"))
            except ValueError:
                pcb.state = AgentState.ERROR
        if pcb.finished_at is None:
            pcb.finished_at = time.time()
        return True
