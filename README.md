# GCOS — Garbage Collector OS for LLM

> A userspace mini-OS that treats LLM agents as **processes**: PCBs, scheduler,
> memory manager, sandbox, IPC, capability-based permissions, and a `ps`/`top`/`kill` shell.
>
> Backend: **Upstage Solar Pro 3** (OpenAI-SDK-compatible).
> Team: *GarbageCollector* — Operating Systems term project.

---

## Why "OS for LLM"?

When multiple LLM agents run concurrently, the same questions an OS answers
appear all over again:

- Which agent runs first? (**scheduling**)
- How much context can each agent hold? (**memory management**)
- Can an agent touch the filesystem or network? (**permissions / sandbox**)
- How do agents talk to each other? (**IPC**)
- How do we keep one runaway agent from starving the rest? (**fair-share, quotas**)
- How do we observe and kill a misbehaving agent? (**process table, signals**)

GCOS provides each of these as a small, testable module. The project rests on
**two pillars** — and "OS *for LLM*" needs both:

1. **OS concepts, faithfully applied — and kernel-enforced.** Not metaphors: the
   scheduler is real CFS (cgroup `cpu.weight`), preemption is real `SIGSTOP`/
   `SIGKILL`, paging is real `mmap`+`madvise` faults, quota is cgroup limits, and
   agents can run as real OS processes. See [`docs/REAL_OS.md`](docs/REAL_OS.md).
2. **The LLM is woven *into* the OS, not just run as the workload.** The memory
   manager uses Solar **inside the kernel mechanism**: when an agent's context
   overflows, `SummarizeEvictionPolicy` issues a real (quota-metered, batched)
   LLM call to *compress* old pages into a summary page — **the OS uses the LLM
   to manage the LLM's own memory.** This is the signature that makes GCOS an OS
   *for* LLM, not an OS that merely hosts one. (Today it is the one such
   mechanism — see [`docs/OS_MAPPING.md`](docs/OS_MAPPING.md) "LLM inside the OS"
   for where it lives and where it could deepen.)

### Scope, stated honestly

So reviewers know exactly what is and isn't claimed:

- **Agents come in two kinds.** *Single-shot* (default: plain chat, coder) is one
  prompt → one response. *Multi-step* (`CapabilitySet.agent()`) is a real ReAct
  loop (`gcos/agent_loop.py`) — think → tool → observe → repeat → finalize, one
  LLM call per step — so the scheduler's quantum genuinely time-slices a real
  agent against its peers (not just the eval's synthetic runners). Multi-step is
  opt-in per agent; we don't claim every agent is autonomous. (F17)
- **OS claims are kernel-enforced where the host allows it.** `gcos.osprims`
  backs the OS mechanisms with real kernel primitives — cgroup v2 CFS shares /
  limits, `SIGSTOP`/`SIGCONT` preemption of real processes, `mmap`+`madvise`
  demand paging, POSIX shared memory, seccomp-bpf, and our own eBPF program.
  **Linux is first-class; macOS degrades loudly** to the in-process simulation.
  See [`docs/REAL_OS.md`](docs/REAL_OS.md) for the metaphor→primitive map and the
  verification matrix (six ubuntu CI jobs prove the Linux-only paths on every push
  — including **loading our own kernel CPU scheduler**, next bullet).
- **We ship — and the kernel runs — our own CPU scheduler.** `scx_gcos`
  (`osprims/ebpf/scx_gcos.bpf.c`) is a real **sched_ext** scheduler (it scales each
  task's slice by the task's weight). CI doesn't just build it: the `scx-ext` job
  loads it into the kernel (`/sys/kernel/sched_ext/state` = `enabled`, `ops` =
  `gcos` — *our* scheduler, not the stock one) and runs GCOS agents under it,
  confirming they dispatch and complete while our code owns every CPU. Beyond
  mapping OS metaphors, GCOS contributes ring-0 scheduling code that actually
  dispatches the machine. (Per-agent CPU **share** by priority is enforced and
  verified separately via cgroup `cpu.weight` on CFS — see
  [`docs/REAL_OS.md`](docs/REAL_OS.md).) See [`scripts/scx/`](scripts/scx/).
- **The policy gate is a cheap pre-filter + audit log, not security.** It's a
  bypassable regex source scan; the real isolation boundary is the Docker
  sandbox (now also with a kernel-enforced **seccomp** syscall allowlist). Treat
  its detection % as "how good is the cheap filter", not a safety guarantee. (D12)
- **Two paging stories.** The context pager's swap is a one-way offload with an
  explicit restore (B7); separately, `osprims/vmem.py` does *real* demand paging
  (mmap + madvise, faulting pages back in on access).
- **Secrets:** never commit a real key. `.env` is git-ignored; if a key was ever
  shared (e.g. in a zip), rotate it. (H20)

---

## Quickstart

```bash
# 1. Create env file
cp .env.example .env
# Edit .env — set UPSTAGE_API_KEY=...

# 2. Install (with uv or pip)
pip install -e .[dev]

# 3. Run a single agent end-to-end (M1)
python -m gcos spawn "Explain what an operating system process is in 3 sentences."

# 4. Daemon + Web dashboard (M2)  —  http://127.0.0.1:8765/
python -m gcos serve --port 8765 --workers 4 --scheduler priority

# 5. Coder agent: LLM writes code, sandbox runs it (M3)
python -m gcos coder "Print the first 10 fibonacci numbers separated by commas."
# Optionally force a sandbox:
GCOS_SANDBOX=docker python -m gcos coder "Compute the SHA-256 of 'hello'."
GCOS_SANDBOX=subprocess python -m gcos coder "..."         # dev fallback

# 6. Producer / consumer pipeline (M4)
python -m gcos pipeline "operating system processes"
# researcher writes 3 facts -> writer turns them into a haiku via {INPUT} bus

# 7. (M5) Interactive REPL: ps / top / kill / spawn
python -m gcos shell

# 8. Multi-step ReAct agent (A1) — one agent, many LLM calls (think→tool→…→FINAL)
python -m gcos spawn --multi-step "Compute (17*23 + 145) / 2 with the calc tool, step by step."
#   prints "(multi-step ReAct)" + calls=N (N>1); plain spawn is single-shot (calls=1)

# 9. ⭐ Real-OS substrate — OS claims enforced by the host kernel (no key needed)
./scripts/demo_realos.command     # caps · real preemption · demand paging · cgroup CFS share
```

> One-click macOS demos live in `scripts/*.command` — `demo_realos` (kernel
> enforcement), `demo_multistep` (A1 live), `demo_eval` (mechanism metrics), and
> the M1–M5 tour. See [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) and
> [`docs/REAL_OS.md`](docs/REAL_OS.md).

---

## OS concept mapping

See [`docs/OS_MAPPING.md`](docs/OS_MAPPING.md) for the full table. Highlights:

| OS Concept | GCOS Module | Kernel-enforced primitive (`gcos.osprims`) |
|---|---|---|
| Process / PCB | `gcos.kernel.pcb.AgentControlBlock` | real PIDs (`osprims.realproc`) |
| Ready queue | `gcos.kernel.ready_queue` | — |
| Scheduler (FCFS / Priority / RR) | `gcos.kernel.scheduler` | cgroup v2 `cpu.weight` → Linux CFS (`osprims.cgroup`) |
| Preemption | `gcos.kernel.worker_pool` (quantum) | `SIGSTOP`/`SIGCONT` real preemption (`osprims.preempt`) |
| Multi-step agent loop | `gcos.agent_loop` | — |
| Capability-based permissions | `gcos.kernel.pcb.CapabilitySet` | — |
| Resource quota | `gcos.kernel.quota` | cgroup v2 `cpu.max`/`memory.max`/`pids.max` (`osprims.cgroup`) |
| Paged context / KV-cache eviction | `gcos.memory.context_pager` | mmap + `madvise` demand paging (`osprims.vmem`) |
| Swap in/out | `gcos.memory.swap` | mmap-backed page-out/fault-in (`osprims.vmem`) |
| IPC (pipes, message bus) | `gcos.ipc.message_bus` | POSIX shared memory (`osprims.shm`) |
| Process tree (fork-ish) | `gcos.kernel.process_tree` | — |
| Syscall + sandbox | `gcos.sandbox.docker_runner` | seccomp-bpf allowlist (`osprims.seccomp`) |
| Policy gate (1st line of defense) | `gcos.sandbox.policy_gate` | — |
| In-kernel observability | — | our own eBPF on `sched_switch` (`osprims.ebpf`) |
| In-kernel CPU scheduler (our own) | `gcos.kernel.scheduler` | **sched_ext `scx_gcos`** — slice ∝ agent priority, CI-loaded (`osprims/ebpf/scx_gcos.bpf.c`, `scripts/scx/`) |
| Device driver (LLM) | `gcos.backend.solar_client` | — |
| Request batching | `gcos.backend.batcher` | — |
| Trace log (ring buffer) | `gcos.kernel.ring_log` | — |

---

## Roadmap

5-week plan in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Status:

- [x] **M1** — Skeleton + FCFS + Solar client + 1 agent end-to-end
- [x] **M2** — Worker pool + Priority/RR + shared quota + FastAPI + Web dashboard (polling)
- [x] **M3** — Sandbox (Docker + Subprocess fallback) + policy gate + capability-gated coder
- [x] **M4** — Context pager (LRU + Solar-summarize-evict + Swap) + MessageBus + Process tree + producer/consumer pipeline
- [x] **M5** — Request batcher + REPL shell (rich) + SSE dashboard + ring trace log + final tests

**228 passing tests + 4 Docker-conditional skipped.**
See [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) for the 7-minute grading-day demo script.

---

## Evaluation

Reproducible, offline metrics (no Upstage key needed) quantify the OS
mechanisms — not the LLM. See [`docs/EVALUATION.md`](docs/EVALUATION.md) for
methodology and a reference run, and [`docs/REAL_OS.md`](docs/REAL_OS.md) for the
kernel-enforced substrate.

```bash
python -m gcos.eval                         # summary table
python -m gcos.eval --json                  # machine-readable
python -m gcos.eval --out docs/RESULTS.md   # markdown report
```

| Metric | OS concept | Reference result |
|---|---|---|
| Concurrency speedup (8 agents, 4 workers) | threads + scheduling | ~3.3x vs serial (mean of 5, 95% CI ±0.05; machine-dependent) |
| Priority dispatch order | scheduling | priority-descending (PASS) |
| FCFS (non-preemptive) vs RR (preemptive quantum) | scheduling | RR rotates @ quantum, halves time-to-first-slice vs FCFS convoy (PASS) |
| Multi-worker no-double-dispatch (A1) | scheduling + sync | 40/40 agents run exactly once (PASS) |
| Quota conservation (A3) | resource accounting | used == real calls; no leak on no-call exits (PASS) |
| Policy gate detection (cheap pre-filter, **not** security) | syscall pre-filter | 93.1% recall, 0% false positives (2 blind spots by design) |
| Context eviction under budget | paging | 620 → ≤200 tokens, fits |
| **Multi-step agents (A1)** | scheduling | real ReAct agents interleave under RR, run-to-completion under FCFS (PASS) |
| **Real preemption** | scheduling | RR vs FCFS over real child processes via SIGSTOP/SIGCONT: RR interleaves (many blocks), FCFS convoys (one block/child) (PASS) |
| **Demand paging** | virtual memory | mmap + madvise page-out, fault-in on access (PASS) |
| **cgroup CFS share** (Linux) | resource control | `cpu.weight` 100/300/900 → CPU share tracks the weights, ~8/23/68% (machine-dependent; CI-verified on Linux, `kernel_enforced=False` on macOS) |
