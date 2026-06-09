"""Worker pool — N OS threads draining the ready queue.

Each worker loops:
    1. wait for the queue to be non-empty
    2. scheduler.pick_next() chooses a PCB
    3. quota.acquire(1) reserves an LLM call
    4. executor.run_step(pcb) makes the Solar call
    5. if run_step returned True (more steps needed) → re-queue
       else → terminal state already set by executor

Quota exhaustion: the worker re-queues the PCB in BLOCKED state and parks
on the quota's condition variable (acquire_blocking with timeout).

Shutdown: a stop Event makes every worker exit at the next loop iteration.
Threads are daemon so an unclean Python exit doesn't hang.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from gcos.ipc.message_bus import MessageBus, resolve_input_placeholder
from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import Scheduler


log = logging.getLogger(__name__)


# Safety bound on a non-preemptive (FCFS) run-to-completion dispatch, so a
# buggy step runner that never reports terminal can't pin a worker forever.
# Real agents are bounded long before this by their per-agent / global quota.
_NONPREEMPTIVE_STEP_CAP = 10_000


# A "step runner" is anything that takes a PCB + client and returns
# True if the PCB should be re-queued. The default is the real executor,
# resolved lazily (see __init__) to avoid a circular import:
# executor → kernel.pcb → kernel/__init__ → kernel.kernel → worker_pool → executor.
StepRunner = Callable[[AgentControlBlock, object], bool]


class WorkerPool:
    def __init__(
        self,
        num_workers: int,
        ready_queue: ReadyQueue,
        scheduler: Scheduler,
        process_table: ProcessTable,
        quota: Quota,
        *,
        client_factory: Callable[[], object] = None,
        step_runner: Optional[StepRunner] = None,
        bus: Optional[MessageBus] = None,
        idle_poll_s: float = 0.2,
        blocked_wait_s: float = 0.5,
        input_wait_s: float = 0.1,
        input_deadline_s: Optional[float] = None,
    ) -> None:
        self.num_workers = num_workers
        self.queue = ready_queue
        self.scheduler = scheduler
        self.table = process_table
        self.quota = quota
        self.bus = bus  # may be None for tests that don't need IPC
        self.client_factory = client_factory
        if step_runner is None:
            # Lazy import to break the kernel ↔ executor cycle (see top of file)
            from gcos.executor import run_step as _default_runner
            step_runner = _default_runner
        self.step_runner = step_runner
        self.input_wait_s = input_wait_s
        self.idle_poll_s = idle_poll_s
        self.blocked_wait_s = blocked_wait_s
        # How long an agent may stay parked WAITING on upstream input before it
        # is failed so the queue can drain (E16). None → fall back to the PCB's
        # own timeout_s.
        self.input_deadline_s = input_deadline_s

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._busy = 0
        self._busy_lock = threading.Lock()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("WorkerPool already started")
        # One Solar client shared by all workers (the OpenAI SDK is thread-safe).
        client = self.client_factory() if self.client_factory else None
        self.client = client  # expose for introspection (kernel.pool.client)
        for i in range(self.num_workers):
            t = threading.Thread(
                target=self._loop,
                args=(i, client),
                name=f"gcos-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        log.info("WorkerPool started with %d workers", self.num_workers)

    def shutdown(self, wait: bool = True, timeout: float = 3.0) -> None:
        self._stop.set()
        if wait:
            for t in self._threads:
                t.join(timeout=timeout)
        self._threads.clear()
        log.info("WorkerPool stopped")

    # --- introspection ------------------------------------------------------

    @property
    def busy(self) -> int:
        with self._busy_lock:
            return self._busy

    def is_idle(self) -> bool:
        # The real idle signal is "nothing queued AND nothing in flight". A PCB
        # that has been dequeued but not yet finished still counts as work, so a
        # dispatched-but-not-started agent never reads as idle (bug A2).
        return self.queue.is_drained()

    # --- internal -----------------------------------------------------------

    def _loop(self, idx: int, client: object) -> None:
        while not self._stop.is_set():
            if not self.queue.wait_nonempty(timeout=self.idle_poll_s):
                continue
            pcb = self.scheduler.pick_next(self.queue)
            if pcb is None:
                continue
            # From here on the PCB is *in flight* (the pop already incremented
            # the queue's in-flight counter). Exactly one task_done() must run,
            # so the whole dispatch body is wrapped in try/finally.
            try:
                self._dispatch(idx, pcb, client)
            finally:
                self.queue.task_done()

    def _dispatch(self, idx: int, pcb: AgentControlBlock, client: object) -> None:
        # A PCB can be finalized (killed/reaped) between selection and here, or
        # slip into the queue via a kill-vs-requeue race. Drop it immediately —
        # the finally: task_done() in _loop still drains it. This is the robust
        # backstop that keeps a dead agent from being run or bounced (review #6).
        if pcb.is_terminal():
            return

        # ----- IPC: resolve upstream input if any (once per dispatch) -----
        if pcb.input_from is not None and "{INPUT}" in pcb.prompt:
            if self.bus is None:
                pcb.transition(AgentState.ERROR)
                pcb.error = "input_from set but kernel has no message bus"
                return
            msg = self.bus.recv(pcb.pid, timeout=self.input_wait_s)
            if msg is None:
                self._repark_waiting(pcb)
                return
            pcb.prompt = resolve_input_placeholder(pcb.prompt, msg)
            pcb.waiting_since = None
            log.debug("PID %d resolved {INPUT} from PID %d (%d chars)",
                      pcb.pid, msg.get("from_pid"), len(msg.get("content", "")))

        # ----- Run up to the scheduler's quantum of LLM calls, then yield -----
        # Preemption granularity comes from the scheduler:
        #   quantum is None → non-preemptive (FCFS): run to completion.
        #   quantum is k    → preemptive (RR=k, Priority=1): yield after k calls.
        # The preemption boundary is between LLM calls (a single non-streaming
        # call isn't preemptible). _NONPREEMPTIVE_STEP_CAP bounds the
        # run-to-completion loop so a never-terminating agent can't pin a worker.
        quantum = getattr(self.scheduler, "quantum", 1)
        limit = _NONPREEMPTIVE_STEP_CAP if quantum is None else max(1, quantum)
        keep_going = False
        for _ in range(limit):
            if self._stop.is_set():
                break
            status, keep_going = self._run_one_step(idx, pcb, client)
            if status != "ran":
                # Blocked on quota (or shutting down): already re-queued. Don't
                # pipe or yield again.
                return
            if pcb.is_terminal() or not keep_going:
                break

        # ----- IPC: forward result downstream ----------------------------
        # Only a *successful* agent (DONE) pipes its result. A killed (ZOMBIE),
        # errored, or timed-out agent must not push garbage/empty input to a
        # waiting consumer.
        if pcb.state == AgentState.DONE and pcb.pipe_to is not None and self.bus is not None:
            payload = self.bus.make_result(from_pid=pcb.pid, content=pcb.result or "")
            # Best-effort, NON-blocking delivery. We deliberately do not block the
            # worker on a full mailbox: with few workers a fan-in (many producers
            # -> one consumer) would self-stall, since the worker holding the put
            # is exactly the one that would otherwise dispatch the consumer to
            # drain it. Liveness for a lost message is already guaranteed by the
            # consumer's WAITING deadline (_repark_waiting), which fails it to
            # ERROR rather than letting it hang (E16) — so a drop is visible, not
            # a silent forever-wait.
            if self.bus.send(pcb.pipe_to, payload):
                log.info("pipe: PID %d -> PID %d (%d chars)",
                         pcb.pid, pcb.pipe_to, len(payload["content"]))
            else:
                log.warning("pipe: PID %d -> PID %d dropped (mailbox full); "
                            "consumer will fail on its input deadline",
                            pcb.pid, pcb.pipe_to)

        # Quantum used up but the agent still has work → yield to the tail.
        if keep_going and not pcb.is_terminal():
            self.queue.put(pcb)

    def _repark_waiting(self, pcb: AgentControlBlock) -> None:
        """No upstream input yet → re-park WAITING, but not forever.

        If the agent has waited past its input deadline, fail it to ERROR so the
        queue can drain — a lost or never-sent pipe message must not strand a
        consumer in WAITING indefinitely (E16). We key the decision purely on
        elapsed wait time, never on the upstream's state: the producer sets DONE
        a hair before it sends, so an upstream-is-terminal shortcut would race
        the delivery and spuriously fail a consumer whose message is in flight."""
        now = time.time()
        if pcb.waiting_since is None:
            pcb.waiting_since = now
        deadline = self.input_deadline_s if self.input_deadline_s is not None else pcb.timeout_s
        waited = now - pcb.waiting_since
        if waited >= deadline:
            pcb.transition(AgentState.ERROR)
            pcb.error = f"input never arrived from PID {pcb.input_from} after {waited:.1f}s"
            log.warning("PID %d failed waiting on input from PID %s (%.1fs)",
                        pcb.pid, pcb.input_from, waited)
            return
        pcb.transition(AgentState.WAITING)
        self.queue.put(pcb)

    def _run_one_step(self, idx: int, pcb: AgentControlBlock, client: object) -> tuple[str, bool]:
        """One LLM-call step: acquire quota, run, refund-if-no-call (A3).

        Returns (status, keep_going). status is "ran" normally, or "blocked"/
        "stopped" if quota was unavailable and the PCB was re-queued (the caller
        must then stop dispatching it)."""
        if not self.quota.acquire(1):
            pcb.transition(AgentState.BLOCKED)
            log.debug("PID %d BLOCKED on quota", pcb.pid)
            got = self.quota.acquire_blocking(1, timeout=self.blocked_wait_s)
            if self._stop.is_set():
                # If acquire_blocking succeeded it already took a unit; hand it
                # back before bailing out, or shutdown racing a refund leaks it
                # (review #4).
                if got:
                    self.quota.refund(1)
                self.queue.put(pcb)
                return ("stopped", False)
            if not got:
                self.queue.put(pcb)
                return ("blocked", False)

        # We now hold exactly one global-quota unit. A step may exit *without*
        # making an LLM call (gate DENY, per-agent quota 0, timeout, exception);
        # in that case we must hand the unit back or the OS budget leaks (A3).
        calls_before = pcb.llm_calls_used
        self._enter_busy()
        try:
            keep_going = self.step_runner(pcb, client)
        except Exception as e:  # noqa: BLE001
            log.exception("worker %d: unhandled error in step_runner", idx)
            pcb.transition(AgentState.ERROR)
            pcb.error = f"worker crash: {e}"
            keep_going = False
        finally:
            self._leave_busy()
        if pcb.llm_calls_used == calls_before:
            self.quota.refund(1)
        return ("ran", keep_going)

    def _enter_busy(self) -> None:
        with self._busy_lock:
            self._busy += 1

    def _leave_busy(self) -> None:
        with self._busy_lock:
            self._busy -= 1

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until the pool is idle (queue empty and nothing in flight).

        Condition-based, so it wakes the instant the last task_done() drains the
        queue rather than polling — and it shares the queue lock with dispatch,
        so there is no "saw empty queue a tick before the worker marked busy"
        race (bug A2)."""
        return self.queue.wait_drained(timeout=timeout)
