# OS Concept Mapping (grading reference)

This table shows how each classical OS concept is realized in GCOS.
Use this as the *first* thing to point at when defending the project.

## The LLM inside the OS (the "for LLM" part)

Most of this project is "OS concepts applied to LLM agents" — the agents are the
*workload*. The distinctive claim of **OS _for_ LLM** is the opposite direction:
the OS itself **uses the LLM inside its own mechanisms**. Today that lives in one
place, and it is the signature feature:

- **Memory manager → summarize-eviction** (`memory/evict_summarize.py`,
  concept #14). When an agent's context exceeds its token budget, the pager
  doesn't just drop old pages (LRU) — it issues a **real Solar call to compress**
  the oldest N pages into a single summary page. Crucially, that call goes
  *through the OS's own machinery*: it is rate-limited by the request batcher and
  **charged to the shared OS quota** (refunded on failure), so memory management
  can't sneak around the OS's throttle (B5). **The OS uses the LLM to manage the
  LLM's own memory** — the workload is also a kernel mechanism.

Honest scope: this is currently the *only* place the LLM drives an OS decision;
everywhere else the policies are classical (FCFS/Priority/RR scheduling, LRU
choice, regex gate). Natural ways to deepen the thread — LLM-informed page-
replacement *choice* (semantic importance, not just compression), or
LLM-classified agent priority/quantum — are noted as the next step, not claimed
as done.

| # | OS Concept | Where it lives | What it does | Direction-A requirement matched |
|---|---|---|---|---|
| 1 | Process | `kernel/pcb.py::AgentControlBlock` | One running LLM agent = one PCB | "multiple concurrent LLM processes" |
| 2 | PID | `kernel/pid_alloc.py` | Monotonic integer allocator | — |
| 3 | Process state | `kernel/pcb.py::AgentState` | `NEW/READY/RUNNING/WAITING/BLOCKED/DONE/TIMEOUT/ERROR/ZOMBIE` | — |
| 4 | Ready queue | `kernel/ready_queue.py` | Thread-safe FIFO between scheduler and workers | — |
| 5 | Scheduler | `kernel/scheduler.py` | `FCFSScheduler` (FIFO, **non-preemptive** — quantum=None, runs each agent to completion → convoy effect); `PriorityScheduler` (highest *effective* priority with wait-time **aging** — C9; atomic `pop_best` — A1; O(n) by design — C10; **preemptive**, re-picks each call); `RoundRobinScheduler` (FIFO selection + **preemptive quantum** of LLM calls — C8). FCFS and RR share `popleft()` selection on purpose; they differ in *preemption*, not ordering. For single-shot agents all three coincide (RR with quantum ≥ burst == FCFS); they diverge on multi-step jobs (`scheduler_preemption` eval). | "Scheduling … tuned for LLM workloads" |
| 6 | Preemption boundary | `worker_pool.py` (quantum = N LLM calls) | A worker runs the picked agent for up to `scheduler.quantum` calls, then yields it to the tail. **FCFS quantum=None → non-preemptive** (run to completion); **Priority=1** (re-pick each call); **RR=k** (rotate every k). Preemption *inside* a single non-streaming call isn't possible, so the boundary is between calls. | "GPU/CPU hand-off"-like |
| 7 | CPU / cores | `kernel/worker_pool.py` | N threads, Condition var, fair pick | "fair CPU allocation" |
| 8 | Quota / rlimit | `kernel/quota.py` | Mutex-protected shared API budget | "tool-quota allocation" |
| 9 | Capability-based ACL | `kernel/pcb.py::CapabilitySet` | Per-agent `{can_call_llm, can_exec_code, can_net, can_fs_write, can_spawn_child, multi_step, allowed_paths, max_tokens, max_tool_calls}` | "file/shell tool permissions" |
| 10 | Process tree (fork) | `kernel/process_tree.py::ProcessTree` | `children_of`, `descendants_of`, `reap_descendants` (cascade ZOMBIE on parent kill), `tree_view` for `ps --tree` | — |
| 11 | IPC pipe / mailbox | `ipc/message_bus.py::MessageBus` | bounded `queue.Queue` per PID, `send/recv/has_pending`, `resolve_input_placeholder({INPUT})` | "concurrent LLM processes" |
| 11b | Atomic spawn wiring | `kernel/pid_alloc.py::PidAllocator.peek()` | Lets producer & consumer be wired (`pipe_to`+`input_from`) before either runs — fixes the obvious race | — |
| 12 | Paging | `memory/context_pager.py::ContextPager` | Per-PCB list of `ContextPage`; `assemble()` loops `policies[]` until size ≤ budget; respects `pinned`/`summarized` flags. Budget is per-kernel and configurable (`KernelConfig.context_budget_tokens` / `GCOS_CONTEXT_BUDGET`); each kernel owns its pager (no process-global singleton). Single-shot agents rarely overflow 4096, so set a smaller budget to exercise paging live (B6). | "memory policies … KV-cache eviction" |
| 13 | LRU page replacement | `memory/evict_lru.py::LRUEvictionPolicy` | Evict oldest non-pinned by `last_access`, honoring `min_keep` floor | — |
| 14 | Compression (OS uses LLM!) | `memory/evict_summarize.py::SummarizeEvictionPolicy` | Take oldest N non-pinned non-summarized pages → Solar summary in 2-3 sentences → insert in their place (marked `summarized=True`). Net frees N-1 pages per Solar call. | "memory policies tuned for LLM inference workloads" |
| 14b | Swap to disk | `memory/swap.py::SwapEvictionPolicy` + `swap_in()` / `Kernel.swap_in(pid)` | Serializes batch to `logs/swap/<pid>/<ts>.json`. **One-way offload with an explicit restore**, not demand paging: swap-out is automatic on overflow, swap-in is an explicit OS op (no page-fault trigger). Round trip is lossless (tests/test_swap.py). Auto-prefetch is deliberately future work, not faked (B7). | "KV-cache eviction" / GPU-CPU hand-off analogue |
| 15 | Pinned pages | `pcb.py::ContextPage.pinned` | System prompt (e.g. CODER_SYSTEM) is never evicted by LRU/Summarize/Swap | — |
| 16 | Syscall / sandbox | `sandbox/docker_runner.py` (+ `subprocess_runner.py` fallback) | LLM-generated code runs in hardened container (`--network=none --read-only --cap-drop=ALL --memory=128m --pids-limit=64 --tmpfs /work`). Subprocess fallback for CI/dev clearly marks itself as insecure. | "secure execution sandbox" |
| 17 | Defense in depth | `sandbox/policy_gate.py` (prompt + code rules) | 1) `scan_prompt` rejects `[SHELL:]/[KERNEL:]/[NET:]/[SUDO:]/[EXFIL:]/[EXEC:]` tags **before paying for an LLM call**. 2) `scan_code` rejects 17 dangerous patterns (os.system, subprocess, eval/exec/compile, dynamic imports, os.popen, pty, sockets, requests, absolute-path deletes, `rm -rf /`, … — see `iter_code_rule_ids()`) **before sandbox**, but it's a bypassable pre-filter/audit log, **not** a security boundary. Docker is the real boundary. | "file/shell tool permissions" |
| 17b | Capability dispatch | `executor.run_step` + `coder.run_coder_step` | `pcb.capability.can_exec_code` decides whether the agent goes through plain chat or the policy-gate + sandbox pipeline. | — |
| 18 | Device driver | `backend/solar_client.py::SolarClient` | OpenAI SDK pointed at Upstage; the only place that knows the model name & base URL | "LLM backend (Upstage Solar Pro 3)" |
| 19 | I/O throttle (rate-limit + concurrency cap) | `backend/batcher.py::BatchingSolarClient` + `TokenBucket` | Bounded semaphore caps in-flight calls; token bucket smooths per-second issuance; stats counter (`in_flight`/`peak`/`avg_wait_ms`/`last_429_ts`) drives the dashboard. The OS owns Solar rate-limit policy, not the agents — including the *summarize-eviction* call, which is routed through the same batched client and charged to the shared quota (B5), so memory management can't sneak past the throttle. | "batching / scheduling for LLM inference workloads" |
| 20 | Trace log / dmesg | `kernel/ring_log.py::RingTraceLog` | Bounded `deque(maxlen=cap)` attached as a `logging.Handler` on the `gcos` logger. Lossy but in-memory, zero disk I/O. Exposed at `/api/log` and via REPL `dmesg`. | "observability" |
| 21 | Process table / `ps` | `shell/repl.py::cmd_ps` + `/api/agents` | `rich` table of every PCB with color-coded state. | — |
| 22 | `top` | `shell/repl.py::cmd_top` | `rich.Live` refresh-loop until Ctrl-C. | — |
| 23 | `kill` / signals | `shell/repl.py::cmd_kill` → `Kernel.kill` | Sets agent to ZOMBIE, *cascades* via `ProcessTree.reap_descendants`. | — |
| 24 | `/proc`-like dashboard | `web/` + `api/sse.py` | SSE-driven; `/api/events` pushes snapshot deltas every 0.5 s; browser auto-reconnects on disconnect; falls back to polling on browsers without `EventSource`. | "observability" |
| 25 | Process tree view | `shell/repl.py::cmd_tree` + `ProcessTree.tree_view` | `ps --tree` style indented listing. | — |
| 26 | `mem` / pager stats | `shell/repl.py::cmd_mem` + `ContextPager.stats()` | Per-PID page list with flags (`pinned`, `summarized`), tokens, budget, overflow. | "memory inspection" |

## Real-OS substrate — kernel-enforced primitives (`gcos.osprims`)

The rows above describe the userspace *orchestration*; these make the same
claims **enforced by the host kernel** (Linux first-class; macOS degrades
loudly). Full detail + verification matrix in [`REAL_OS.md`](REAL_OS.md).

| # | OS Concept | Kernel primitive | Module | Verified |
|---|---|---|---|---|
| K1 | Proportional CPU scheduling | cgroup v2 `cpu.weight` → Linux CFS (measured share tracks weight) | `osprims/cgroup.py` | Linux container + CI |
| K2 | Resource limits | cgroup v2 `cpu.max`/`memory.max`/`pids.max` | `osprims/cgroup.py` | Linux + CI |
| K3 | True preemption | `SIGSTOP`/`SIGCONT`/`SIGKILL` on real processes | `osprims/preempt.py`, `realproc.py` | macOS + Linux + CI |
| K4 | Demand paging | `mmap` + `madvise(MADV_DONTNEED)`, fault-in on access | `osprims/vmem.py` | macOS + Linux + CI |
| K5 | IPC | POSIX shared memory (`shm_open`) | `osprims/shm.py` | macOS + Linux + CI |
| K6 | Syscall boundary | seccomp-bpf allowlist (Docker `--security-opt`) | `osprims/seccomp.py` | Linux + CI |
| K7 | In-kernel code | our own eBPF on `sched_switch` (CI compiles + loads it) | `osprims/ebpf/` | Linux + CI (bcc+root) |
| K8 | Multi-step agents | real ReAct loop; scheduler time-slices a real agent | `agent_loop.py` | macOS + Linux + CI |
| K9 | OS budget (live) | daemon cgroup `pids.max`/`memory.max`/`cpu.max` at boot | `kernel.start`→`cgroup.place_daemon` | Linux + CI |
| K10 | Per-agent CFS (live, sandbox) | sandboxed code's `cpu_shares` from agent priority | `coder.py`→`priority_to_cpu_shares` | Linux + CI |
| K11 | Per-agent CFS (live, all work) | each agent a real process under per-agent `cpu.weight` | `kernel/process_pool.py` (`GCOS_EXEC=process`) | Linux + CI |
| K12 | **Our OWN ring-0 CPU scheduler** | sched_ext scheduler we wrote (slice ∝ `p->scx.weight`); CI loads it so **our code dispatches every task** (`state`=enabled, `ops`=gcos) and runs GCOS agents under it. (Per-priority CPU *share* is K10/cgroup CFS, not scx — scx_gcos doesn't read cgroup weight.) | `osprims/ebpf/scx_gcos.bpf.c` + `scripts/scx/` | Linux ≥ 6.12 + CI |

## What we deliberately did *not* (fully) implement (and why)
- **True preemption inside an LLM call** — HTTP calls are atomic from the OS's
  POV. We preempt *between* calls for in-process agents, and with real SIGSTOP/
  SIGKILL for real agent processes (`process_pool`, `realproc`).
- **Multi-tenant networking** — out of scope; one Solar key, one machine.
