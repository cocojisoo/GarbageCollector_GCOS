"""Kernel — the façade that ties all subsystems together.

This is what the API and the REPL talk to. Hides:
  - PID allocation
  - ProcessTable
  - ReadyQueue
  - Scheduler
  - WorkerPool
  - global Quota
  - (later) ContextPager, MessageBus, Sandbox

Lifecycle:
    k = Kernel(scheduler="priority", workers=4, quota_total=100)
    k.start()                       # spin up worker pool
    pid = k.spawn("hello", ...)     # enqueue agent
    k.wait_idle()                   # block until all ready+running drain
    k.shutdown()
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from gcos.backend.batcher import BatchingSolarClient
from gcos.backend.solar_client import SolarClient
from gcos.ipc.message_bus import MessageBus
from gcos.memory import ContextPager, default_policies
from gcos.kernel.pcb import AgentControlBlock, AgentState, CapabilitySet
from gcos.kernel.pid_alloc import PidAllocator
from gcos.kernel.process_table import ProcessTable
from gcos.kernel.process_tree import ProcessTree
from gcos.kernel.quota import Quota
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.ring_log import RingTraceLog
from gcos.kernel.scheduler import Scheduler, make as make_scheduler
from gcos.kernel.worker_pool import WorkerPool


log = logging.getLogger(__name__)


@dataclass
class KernelConfig:
    scheduler: str = "fcfs"
    workers: int = 4
    quota_total: int = 100
    default_quota: int = 10
    default_timeout: float = 30.0
    batcher_max_concurrent: int = 4
    batcher_rate_per_s: float = 5.0
    ring_log_capacity: int = 256
    # Context-pager budget per agent. The 3-tier memory manager (LRU → summarize
    # → swap) only fires when an agent's context exceeds this, so a smaller
    # budget makes paging reachable in live (not just eval) runs — see B6.
    context_budget_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "KernelConfig":
        return cls(
            scheduler=os.getenv("GCOS_SCHEDULER", "fcfs"),
            workers=int(os.getenv("GCOS_WORKERS", "4")),
            quota_total=int(os.getenv("GCOS_QUOTA_TOTAL", "100")),
            default_quota=int(os.getenv("GCOS_DEFAULT_QUOTA", "10")),
            default_timeout=float(os.getenv("GCOS_DEFAULT_TIMEOUT", "30")),
            batcher_max_concurrent=int(os.getenv("GCOS_BATCHER_CONCURRENT", "4")),
            batcher_rate_per_s=float(os.getenv("GCOS_BATCHER_RATE", "5")),
            ring_log_capacity=int(os.getenv("GCOS_RING_LOG", "256")),
            context_budget_tokens=int(os.getenv("GCOS_CONTEXT_BUDGET", "4096")),
        )


class Kernel:
    def __init__(
        self,
        config: Optional[KernelConfig] = None,
        *,
        client_factory: Optional[Callable[[], object]] = None,
        step_runner=None,
    ) -> None:
        self.config = config or KernelConfig.from_env()
        self.pids = PidAllocator()
        self.table = ProcessTable()
        self.queue = ReadyQueue()
        self.quota = Quota(self.config.quota_total)
        self.scheduler: Scheduler = make_scheduler(self.config.scheduler)
        self.bus = MessageBus()
        self.tree = ProcessTree(self.table)
        self.trace = RingTraceLog(capacity=self.config.ring_log_capacity)
        self.trace.attach_to_logger("gcos")

        # Default: wrap SolarClient in the rate-limited batcher so the OS,
        # not the agents, owns Solar throttling. Tests inject their own
        # client_factory (typically `lambda: None` with a fake step_runner).
        if client_factory is None:
            def _default_factory():
                return BatchingSolarClient(
                    max_concurrent=self.config.batcher_max_concurrent,
                    rate_per_s=self.config.batcher_rate_per_s,
                )
            client_factory = _default_factory
        pool_kwargs = {"client_factory": client_factory}
        if step_runner is not None:
            pool_kwargs["step_runner"] = step_runner
        # Remember the factory so the API can introspect the live client.
        self._client_factory = client_factory
        self._live_client: Optional[object] = None

        # This kernel owns its ContextPager (H22) — no process-global singleton,
        # so two kernels in one process can't share a budget. The summarize
        # policy is metered against this kernel's quota and, when it has to fall
        # back to its own client, uses the throttled batcher factory (B5).
        self.pager = ContextPager(
            budget_tokens=self.config.context_budget_tokens,
            policies=default_policies(
                summarize_client_factory=self._client_factory,
                quota=self.quota,
            ),
        )

        self.pool = WorkerPool(
            self.config.workers,
            self.queue,
            self.scheduler,
            self.table,
            self.quota,
            bus=self.bus,
            **pool_kwargs,
        )
        self._started = False

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.pool.start()
        self._started = True
        log.info("Kernel started: scheduler=%s workers=%d quota_total=%d",
                 self.scheduler.name, self.config.workers, self.quota.total)

    def shutdown(self) -> None:
        if not self._started:
            return
        self.pool.shutdown()
        # Drop every mailbox so the bus doesn't leak across kernel lifecycles
        # (E14). Safe at shutdown: any undelivered message is moot now.
        for pid in list(self.bus.snapshot().keys()):
            self.bus.drop_mailbox(pid)
        self._started = False

    def __enter__(self) -> "Kernel":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # --- public API ---------------------------------------------------------

    def spawn(
        self,
        prompt: str,
        *,
        name: str = "anon",
        priority: int = 5,
        timeout_s: Optional[float] = None,
        quota: Optional[int] = None,
        capability: Optional[CapabilitySet] = None,
        parent_pid: Optional[int] = None,
        pipe_to: Optional[int] = None,
        input_from: Optional[int] = None,
    ) -> int:
        pcb = AgentControlBlock(
            pid=self.pids.next(),
            name=name,
            prompt=prompt,
            priority=priority,
            timeout_s=timeout_s if timeout_s is not None else self.config.default_timeout,
            quota_remaining=quota if quota is not None else self.config.default_quota,
            capability=capability or CapabilitySet.default_user(),
            parent_pid=parent_pid,
            pipe_to=pipe_to,
            input_from=input_from,
        )
        pcb.pager = self.pager   # per-kernel memory config (H22)
        self.table.add(pcb)
        if parent_pid is not None:
            parent = self.table.get(parent_pid)
            if parent is not None:
                parent.children.append(pcb.pid)
        # Agents waiting on upstream input start in WAITING; the worker
        # promotes them to READY once it sees a message on the bus. We still
        # put them on the queue so a worker eventually picks them up — they
        # cheaply re-park if no input yet.
        if pcb.input_from is not None:
            pcb.transition(AgentState.WAITING)
        self.queue.put(pcb)
        log.info("spawned pid=%d name=%s prio=%d parent=%s pipe_to=%s",
                 pcb.pid, pcb.name, pcb.priority, parent_pid, pipe_to)
        return pcb.pid

    def get(self, pid: int) -> Optional[AgentControlBlock]:
        return self.table.get(pid)

    def list_all(self) -> list[AgentControlBlock]:
        return self.table.snapshot()

    def kill(self, pid: int) -> bool:
        """Kill a PID and reap all its descendants (cascade)."""
        pcb = self.table.get(pid)
        if pcb is None or pcb.is_terminal():
            return False
        # Collect descendants before reaping so we can free their mailboxes too.
        victims = [pid] + [d.pid for d in self.tree.descendants_of(pid)]
        pcb.transition(AgentState.ZOMBIE)
        pcb.error = "killed by user"
        self.queue.pop(pcb)
        self.tree.reap_descendants(pid, reason="killed by user")
        # Free the mailboxes of everything we just killed so they don't leak
        # (E14). The agents are terminal — nothing will read these again.
        for vpid in victims:
            self.bus.drop_mailbox(vpid)
        return True

    def reap_terminal(self) -> int:
        """Remove finished agents from the process table and free their
        mailboxes (E14). Explicit (admin/REPL) — not automatic, so the dashboard
        keeps showing completed agents until a reap is requested. Returns the
        number of entries reaped."""
        reaped = 0
        for pcb in self.table.snapshot():
            if pcb.is_terminal():
                self.table.remove(pcb.pid)
                self.bus.drop_mailbox(pcb.pid)
                reaped += 1
        if reaped:
            log.info("reaped %d terminal agents", reaped)
        return reaped

    def wait_idle(self, timeout: float = 30.0) -> bool:
        return self.pool.wait_idle(timeout=timeout)

    def swap_in(self, pid: int) -> int:
        """Explicitly restore an agent's swapped-out context pages (B7).

        Swap-out is automatic (the pager offloads on overflow); swap-in is an
        explicit OS operation, not a page fault. Returns the number of pages
        restored, or -1 if the PID is unknown."""
        from gcos.memory.swap import swap_in as _swap_in
        pcb = self.table.get(pid)
        if pcb is None:
            return -1
        return _swap_in(pcb)

    def status(self) -> dict:
        snap = self.table.snapshot()
        by_state: dict[str, int] = {}
        for p in snap:
            by_state[p.state.value] = by_state.get(p.state.value, 0) + 1
        batcher_stats = None
        client = getattr(self.pool, "client", None)
        if client is not None and hasattr(client, "stats"):
            batcher_stats = client.stats
        from gcos.sandbox import sandbox_info  # lazy: avoids docker import at boot
        return {
            "scheduler": self.scheduler.name,
            "workers": self.config.workers,
            "busy": self.pool.busy,
            "queue_len": len(self.queue),
            "in_flight": self.queue.in_flight,
            "total_agents": len(snap),
            "by_state": by_state,
            "quota": self.quota.snapshot(),
            "batcher": batcher_stats,
            "bus_pending": self.bus.snapshot(),
            "sandbox": sandbox_info(),
            "trace_size": len(self.trace),
        }
