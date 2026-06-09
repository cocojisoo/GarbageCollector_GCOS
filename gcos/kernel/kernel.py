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

    # Daemon cgroup limits (Linux only; the OS's own resource budget enforced by
    # the kernel). pids_max is safe-by-default (anti fork-bomb); memory/cpu are
    # off by default so a misconfig can never OOM-kill the OS. Opt in via env.
    cgroup_pids_max: Optional[int] = 512
    cgroup_memory_max: Optional[int] = None
    cgroup_cpu_max: Optional[str] = None
    # Execution backend: "thread" (default — agents on worker threads) or
    # "process" (each agent is a real OS process under a per-agent cgroup whose
    # cpu.weight = priority, giving true per-agent CFS in the live path on Linux).
    executor_backend: str = "thread"

    @classmethod
    def from_env(cls) -> "KernelConfig":
        def _opt_int(v: Optional[str], default: Optional[int]) -> Optional[int]:
            return int(v) if v not in (None, "") else default
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
            cgroup_pids_max=_opt_int(os.getenv("GCOS_CGROUP_PIDS_MAX"), 512),
            cgroup_memory_max=_opt_int(os.getenv("GCOS_CGROUP_MEMORY_MAX"), None),
            cgroup_cpu_max=os.getenv("GCOS_CGROUP_CPU_MAX") or None,
            executor_backend=os.getenv("GCOS_EXEC", "thread"),
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
        self._daemon_cgroup: Optional[object] = None  # set at start() on Linux

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

        if self.config.executor_backend == "process":
            from gcos.kernel.process_pool import ProcessWorkerPool
            pool_cls = ProcessWorkerPool
        else:
            pool_cls = WorkerPool
        self.pool = pool_cls(
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
        # Surface the OS-enforcement posture loudly, like the sandbox does: if the
        # host can't enforce cgroup CFS / signals (non-Linux), say so rather than
        # quietly running the in-process simulation (osprims degrade banner).
        from gcos.osprims import warn_if_degraded
        warn_if_degraded()
        # Place the whole GCOS daemon in a cgroup so its resource budget (pids,
        # and optionally memory/cpu) is enforced by the kernel, not a Python
        # counter. Best-effort: no-op off Linux / without cgroup delegation.
        from gcos.osprims import cgroup as _cg
        self._daemon_cgroup = _cg.place_daemon(
            pids_max=self.config.cgroup_pids_max,
            memory_max=self.config.cgroup_memory_max,
            cpu_max=self.config.cgroup_cpu_max,
        )
        if self._daemon_cgroup is not None:
            log.info("kernel: OS budget kernel-enforced via %s "
                     "(pids_max=%s memory_max=%s cpu_max=%s)",
                     self._daemon_cgroup.path, self.config.cgroup_pids_max,
                     self.config.cgroup_memory_max, self.config.cgroup_cpu_max)
        self.pool.start()
        self._started = True
        log.info("Kernel started: scheduler=%s workers=%d quota_total=%d",
                 self.scheduler.name, self.config.workers, self.quota.total)

    def shutdown(self) -> None:
        if not self._started:
            return
        self.pool.shutdown()
        # Best-effort: drop the daemon cgroup (rmdir defers while the process is
        # still in it, which is fine — it's reclaimed on exit / next boot).
        if self._daemon_cgroup is not None:
            self._daemon_cgroup.remove()
            self._daemon_cgroup = None
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
        # If this agent is running as a real process (process backend), SIGKILL
        # it for real — kill becomes a genuine kernel signal, not just a flag.
        if hasattr(self.pool, "kill_running"):
            for vpid in victims:
                self.pool.kill_running(vpid)
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
        from gcos.osprims import caps_info     # real-OS enforcement posture
        daemon_cg = None
        if self._daemon_cgroup is not None:
            daemon_cg = {
                "path": self._daemon_cgroup.path,
                "memory_current": self._daemon_cgroup.memory_current(),
                "cpu_usage_us": self._daemon_cgroup.cpu_usage_us(),
                "pids_max": self.config.cgroup_pids_max,
            }
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
            "osprims": caps_info(),
            "daemon_cgroup": daemon_cg,
            "trace_size": len(self.trace),
        }
