# OS Concept Mapping (grading reference)

This table shows how each classical OS concept is realized in GCOS.
Use this as the *first* thing to point at when defending the project.

| # | OS Concept | Where it lives | What it does | Direction-A requirement matched |
|---|---|---|---|---|
| 1 | Process | `kernel/pcb.py::AgentControlBlock` | One running LLM agent = one PCB | "multiple concurrent LLM processes" |
| 2 | PID | `kernel/pid_alloc.py` | Monotonic integer allocator | — |
| 3 | Process state | `kernel/pcb.py::AgentState` | `NEW/READY/RUNNING/WAITING/BLOCKED/DONE/TIMEOUT/ERROR/ZOMBIE` | — |
| 4 | Ready queue | `kernel/ready_queue.py` | Thread-safe FIFO between scheduler and workers | — |
| 5 | Scheduler | `kernel/scheduler.py` | `FCFSScheduler`, `PriorityScheduler`, `RoundRobinScheduler` | "Scheduling … tuned for LLM workloads" |
| 6 | Preemption boundary | `worker_pool.py` (after each LLM call) | RR re-queues after every Solar call | "GPU/CPU hand-off"-like |
| 7 | CPU / cores | `kernel/worker_pool.py` | N threads, Condition var, fair pick | "fair CPU allocation" |
| 8 | Quota / rlimit | `kernel/quota.py` | Mutex-protected shared API budget | "tool-quota allocation" |
| 9 | Capability-based ACL | `kernel/capability.py::CapabilitySet` | Per-agent `{can_net, can_fs_write, can_spawn_child, can_exec_code, max_tokens, max_tool_calls}` | "file/shell tool permissions" |
| 10 | Process tree (fork) | `kernel/process_tree.py::ProcessTree` | `children_of`, `descendants_of`, `reap_descendants` (cascade ZOMBIE on parent kill), `tree_view` for `ps --tree` | — |
| 11 | IPC pipe / mailbox | `ipc/message_bus.py::MessageBus` | bounded `queue.Queue` per PID, `send/recv/has_pending`, `resolve_input_placeholder({INPUT})` | "concurrent LLM processes" |
| 11b | Atomic spawn wiring | `kernel/pid_alloc.py::PidAllocator.peek()` | Lets producer & consumer be wired (`pipe_to`+`input_from`) before either runs — fixes the obvious race | — |
| 12 | Paging | `memory/context_pager.py::ContextPager` | Per-PCB list of `ContextPage`; `assemble()` loops `policies[]` until size ≤ budget; respects `pinned`/`summarized` flags | "memory policies … KV-cache eviction" |
| 13 | LRU page replacement | `memory/evict_lru.py::LRUEvictionPolicy` | Evict oldest non-pinned by `last_access`, honoring `min_keep` floor | — |
| 14 | Compression (OS uses LLM!) | `memory/evict_summarize.py::SummarizeEvictionPolicy` | Take oldest N non-pinned non-summarized pages → Solar summary in 2-3 sentences → insert in their place (marked `summarized=True`). Net frees N-1 pages per Solar call. | "memory policies tuned for LLM inference workloads" |
| 14b | Swap to disk | `memory/swap.py::SwapEvictionPolicy` + `swap_in()` | Serializes batch to `logs/swap/<pid>/<ts>.json`; standalone `swap_in()` restores | "KV-cache eviction" / GPU-CPU hand-off analogue |
| 15 | Pinned pages | `pcb.py::ContextPage.pinned` | System prompt (e.g. CODER_SYSTEM) is never evicted by LRU/Summarize/Swap | — |
| 16 | Syscall / sandbox | `sandbox/docker_runner.py` (+ `subprocess_runner.py` fallback) | LLM-generated code runs in hardened container (`--network=none --read-only --cap-drop=ALL --memory=128m --pids-limit=64 --tmpfs /work`). Subprocess fallback for CI/dev clearly marks itself as insecure. | "secure execution sandbox" |
| 17 | Defense in depth | `sandbox/policy_gate.py` (prompt + code rules) | 1) `scan_prompt` rejects `[SHELL:]/[KERNEL:]/[NET:]/[SUDO:]/[EXFIL:]/[EXEC:]` tags **before paying for an LLM call**. 2) `scan_code` rejects 10 dangerous patterns (os.system, subprocess, eval/exec, sockets, requests, `rm -rf /`, …) **before sandbox**. Docker is the third line. | "file/shell tool permissions" |
| 17b | Capability dispatch | `executor.run_step` + `coder.run_coder_step` | `pcb.capability.can_exec_code` decides whether the agent goes through plain chat or the policy-gate + sandbox pipeline. | — |
| 18 | Device driver | `backend/solar_client.py::SolarClient` | OpenAI SDK pointed at Upstage; the only place that knows the model name & base URL | "LLM backend (Upstage Solar Pro 3)" |
| 19 | I/O throttle (rate-limit + concurrency cap) | `backend/batcher.py::BatchingSolarClient` + `TokenBucket` | Bounded semaphore caps in-flight calls; token bucket smooths per-second issuance; stats counter (`in_flight`/`peak`/`avg_wait_ms`/`last_429_ts`) drives the dashboard. The OS owns Solar rate-limit policy, not the agents. | "batching / scheduling for LLM inference workloads" |
| 20 | Trace log / dmesg | `kernel/ring_log.py::RingTraceLog` | Bounded `deque(maxlen=cap)` attached as a `logging.Handler` on the `gcos` logger. Lossy but in-memory, zero disk I/O. Exposed at `/api/log` and via REPL `dmesg`. | "observability" |
| 21 | Process table / `ps` | `shell/repl.py::cmd_ps` + `/api/agents` | `rich` table of every PCB with color-coded state. | — |
| 22 | `top` | `shell/repl.py::cmd_top` | `rich.Live` refresh-loop until Ctrl-C. | — |
| 23 | `kill` / signals | `shell/repl.py::cmd_kill` → `Kernel.kill` | Sets agent to ZOMBIE, *cascades* via `ProcessTree.reap_descendants`. | — |
| 24 | `/proc`-like dashboard | `web/` + `api/sse.py` | SSE-driven; `/api/events` pushes snapshot deltas every 0.5 s; browser auto-reconnects on disconnect; falls back to polling on browsers without `EventSource`. | "observability" |
| 25 | Process tree view | `shell/repl.py::cmd_tree` + `ProcessTree.tree_view` | `ps --tree` style indented listing. | — |
| 26 | `mem` / pager stats | `shell/repl.py::cmd_mem` + `ContextPager.stats()` | Per-PID page list with flags (`pinned`, `summarized`), tokens, budget, overflow. | "memory inspection" |

## What we deliberately did *not* implement (and why)

- **Kernel-mode code / real syscalls** — Direction A is explicitly application-level OS for LLMs.
- **True preemption inside an LLM call** — HTTP calls are atomic from the OS's POV. We preempt *between* calls (quantum = 1 LLM call).
- **Multi-tenant networking** — out of scope; one Solar key, one machine.
