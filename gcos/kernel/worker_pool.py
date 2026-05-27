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
        return self.busy == 0 and len(self.queue) == 0

    # --- internal -----------------------------------------------------------

    def _loop(self, idx: int, client: object) -> None:
        while not self._stop.is_set():
            if not self.queue.wait_nonempty(timeout=self.idle_poll_s):
                continue
            pcb = self.scheduler.pick_next(self.queue)
            if pcb is None:
                continue

            # ----- IPC: resolve upstream input if any --------------------
            if pcb.input_from is not None and "{INPUT}" in pcb.prompt:
                if self.bus is None:
                    pcb.transition(AgentState.ERROR)
                    pcb.error = "input_from set but kernel has no message bus"
                    continue
                msg = self.bus.recv(pcb.pid, timeout=self.input_wait_s)
                if msg is None:
                    # Still no upstream output; re-park as WAITING
                    pcb.transition(AgentState.WAITING)
                    self.queue.put(pcb)
                    continue
                # Substitute and proceed
                pcb.prompt = resolve_input_placeholder(pcb.prompt, msg)
                log.debug("PID %d resolved {INPUT} from PID %d (%d chars)",
                          pcb.pid, msg.get("from_pid"), len(msg.get("content", "")))

            # ----- Quota ------------------------------------------------
            if not self.quota.acquire(1):
                pcb.transition(AgentState.BLOCKED)
                log.debug("PID %d BLOCKED on quota", pcb.pid)
                got = self.quota.acquire_blocking(1, timeout=self.blocked_wait_s)
                if self._stop.is_set():
                    self.queue.put(pcb)
                    break
                if not got:
                    self.queue.put(pcb)
                    continue

            # ----- Run one step -----------------------------------------
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

            # ----- IPC: forward result if pipe_to is set ----------------
            if pcb.is_terminal() and pcb.pipe_to is not None and self.bus is not None:
                payload = self.bus.make_result(
                    from_pid=pcb.pid, content=pcb.result or "",
                )
                if self.bus.send(pcb.pipe_to, payload):
                    log.info("pipe: PID %d -> PID %d (%d chars)",
                             pcb.pid, pcb.pipe_to, len(payload["content"]))

            if keep_going and not pcb.is_terminal():
                self.queue.put(pcb)

    def _enter_busy(self) -> None:
        with self._busy_lock:
            self._busy += 1

    def _leave_busy(self) -> None:
        with self._busy_lock:
            self._busy -= 1

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Poll until the pool is idle. Useful in tests and demos."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_idle():
                return True
            time.sleep(0.05)
        return False
