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

GCOS provides each of these as a small, testable module — and uses Solar Pro 3
not only as a workload, but also *inside the OS* (e.g. for summarize-evict in
the memory manager).

### Scope, stated honestly

So reviewers know exactly what is and isn't claimed:

- **Agents are single-shot.** Each agent is one prompt → one response; there is
  no autonomous multi-step tool-use loop yet (the worker pool, quantum, and
  re-queue machinery are multi-step-ready, but the executor isn't). GCOS today
  is single-shot agents *composed* over IPC, not autonomous agents. (F17)
- **The policy gate is a cheap pre-filter + audit log, not security.** It's a
  bypassable regex source scan; the real isolation boundary is the Docker
  sandbox. Treat its detection % as "how good is the cheap filter", not a
  safety guarantee. (D12)
- **Swap is a one-way offload with an explicit restore**, not demand paging. (B7)
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
```

---

## OS concept mapping

See [`docs/OS_MAPPING.md`](docs/OS_MAPPING.md) for the full table. Highlights:

| OS Concept | GCOS Module |
|---|---|
| Process / PCB | `gcos.kernel.pcb.AgentControlBlock` |
| Ready queue | `gcos.kernel.ready_queue` |
| Scheduler (FCFS / Priority / RR) | `gcos.kernel.scheduler` |
| Worker pool (CPU cores) | `gcos.kernel.worker_pool` |
| Capability-based permissions | `gcos.kernel.capability` |
| Paged context / KV-cache eviction | `gcos.memory.context_pager` |
| Swap in/out | `gcos.memory.swap` |
| IPC (pipes, message bus) | `gcos.ipc.message_bus` |
| Process tree (fork-ish) | `gcos.kernel.process_tree` |
| Syscall + sandbox | `gcos.sandbox.docker_runner` |
| Policy gate (1st line of defense) | `gcos.sandbox.policy_gate` |
| Device driver (LLM) | `gcos.backend.solar_client` |
| Request batching | `gcos.backend.batcher` |
| Trace log (ring buffer) | `gcos.kernel.ring_log` |

---

## Roadmap

5-week plan in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Status:

- [x] **M1** — Skeleton + FCFS + Solar client + 1 agent end-to-end
- [x] **M2** — Worker pool + Priority/RR + shared quota + FastAPI + Web dashboard (polling)
- [x] **M3** — Sandbox (Docker + Subprocess fallback) + policy gate + capability-gated coder
- [x] **M4** — Context pager (LRU + Solar-summarize-evict + Swap) + MessageBus + Process tree + producer/consumer pipeline
- [x] **M5** — Request batcher + REPL shell (rich) + SSE dashboard + ring trace log + final tests

**180 passing tests + 4 Docker-conditional skipped.**
See [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) for the 7-minute grading-day demo script.

---

## Evaluation

Four reproducible, offline metrics (no Upstage key needed) quantify the OS
mechanisms — not the LLM. See [`docs/EVALUATION.md`](docs/EVALUATION.md) for
methodology and a reference run.

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
