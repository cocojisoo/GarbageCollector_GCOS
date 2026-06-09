# GCOS Architecture

## Layered view

```
┌──────────────────────────────────────────────────────────────┐
│  User                                                        │
│  ├── Web dashboard (HTML/CSS/JS + SSE)                       │
│  └── REPL shell (rich) — ps / top / kill / spawn             │
├──────────────────────────────────────────────────────────────┤
│  API layer  (FastAPI + SSE)                                  │
│  POST /spawn  GET /agents  DELETE /agents/{pid}  GET /events │
├──────────────────────────────────────────────────────────────┤
│  Kernel                                                      │
│  ┌──────────────┐ ┌────────────────┐ ┌──────────────────┐    │
│  │  Scheduler   │ │ Worker Pool    │ │ Process Tree     │    │
│  │  FCFS/Pri/RR │ │ N threads      │ │ parent/child reap│    │
│  └──────────────┘ └────────────────┘ └──────────────────┘    │
│  ┌──────────────┐ ┌────────────────┐ ┌──────────────────┐    │
│  │ Ready Queue  │ │ Quota (mutex)  │ │ Capability Set   │    │
│  └──────────────┘ └────────────────┘ └──────────────────┘    │
│  ┌──────────────┐ ┌────────────────┐ ┌──────────────────┐    │
│  │ PCB / PID    │ │ Ring Trace Log │ │ IPC Message Bus  │    │
│  └──────────────┘ └────────────────┘ └──────────────────┘    │
├──────────────────────────────────────────────────────────────┤
│  Memory                                                      │
│  ┌──────────────────┐ ┌─────────────────┐ ┌──────────────┐   │
│  │ Context Pager    │ │ Eviction policy │ │ Disk swap    │   │
│  │ (per-PID pages)  │ │ LRU / Summarize │ │ in/out       │   │
│  └──────────────────┘ └─────────────────┘ └──────────────┘   │
├──────────────────────────────────────────────────────────────┤
│  Sandbox / Syscall                                           │
│  ┌──────────────────┐ ┌─────────────────────────────────┐    │
│  │ Policy Gate      │ │ Docker Runner                   │    │
│  │ pre-filter+audit │ │ non-root / no-net / ro-fs /     │    │
│  │ (NOT security)   │ │ cap-drop / 128M  (fail-closed)  │    │
│  └──────────────────┘ └─────────────────────────────────┘    │
├──────────────────────────────────────────────────────────────┤
│  Backend                                                     │
│  ┌──────────────────┐ ┌─────────────────────────────────┐    │
│  │ Solar Client     │ │ Request Batcher                 │    │
│  │ (OpenAI SDK)     │ │ (rate-limit window, futures)    │    │
│  └──────────────────┘ └─────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

## Lifecycle of one agent

Reflects the current dispatch path (after the A–H hardening pass below):

```
spawn(prompt, prio, cap, [pipe_to|input_from])
  └── pid = pid_alloc();  pcb = AgentControlBlock(state=NEW);  pcb.pager = kernel.pager
       └── ready_queue.put(pcb)          # → READY  (refused if already terminal)
            └── scheduler.pick_next()  ◄── worker thread
                 │     • Priority: atomic pop_best() — select+remove under one lock (no
                 │       double-dispatch), effective priority = base + wait-time aging
                 │     • in-flight += 1 as part of the pop (so the system never reads
                 │       "idle" while a dispatched agent hasn't finished)
                 ├── if pcb already terminal (killed): drop it, task_done(), done
                 ├── if input_from & "{INPUT}": bus.recv()
                 │       miss → re-park WAITING; past the input deadline → ERROR (no hang)
                 └── run up to scheduler.quantum LLM calls (RR > 1, FCFS/Pri = 1):
                      ├── quota.acquire(1)            (exhausted → BLOCKED + park)
                      ├── state = RUNNING
                      ├── context_pager.assemble(pcb, client=batcher)
                      │     └── over budget → LRU → summarize (via batcher, charges quota)
                      │                              → swap-out to disk
                      ├── batcher.chat(messages)     (rate-limit window + concurrency cap)
                      ├── if the step made NO LLM call → quota.refund(1)   (no budget leak)
                      ├── if killed mid-call (ZOMBIE) → discard result (cooperative cancel)
                      ├── if coder & cap.can_exec_code:
                      │     policy_gate(code) → docker_runner.run()
                      │       (non-root, --network=none --read-only --cap-drop=ALL …)
                      └── state = DONE / TIMEOUT / ERROR   (terminal states are absorbing)
                 ├── if DONE & pipe_to: bus.send(target, result)   (best-effort, non-blocking)
                 ├── if keep_going & not terminal: ready_queue.put(pcb)   # yield (RR quantum)
                 └── task_done()   # in-flight -= 1; wakes wait_idle() once fully drained

  kill(pid): state = ZOMBIE → process_tree.reap_descendants() → drop their mailboxes
  reap_terminal() / shutdown(): free finished entries' mailboxes
```

## Build log — 5-week milestones (M1–M5)

The 5-week plan, all delivered. Per-file detail is in `docs/OS_MAPPING.md` (the
canonical concept→module reference); this is the progression at a glance.

| Milestone | Delivered |
|---|---|
| **M1 — Skeleton** | PCB + states + PID alloc + ready queue + FCFS + Solar client + single-agent CLI |
| **M2 — Concurrency** | N-thread worker pool + Priority/RR schedulers + shared quota + FastAPI + web dashboard |
| **M3 — Sandbox + capability** | policy gate (prompt+code) → Docker runner (non-root, `--network=none --read-only --cap-drop=ALL`) + subprocess fallback; capability-gated coder |
| **M4 — Memory + IPC** | context pager (LRU + **summarize-evict** + swap) + per-PID message bus + process tree (fork/reap) + producer/consumer pipeline |
| **M5 — Polish** | request batcher (rate-limit + concurrency cap) + ring trace log (`dmesg`) + SSE dashboard + `rich` REPL (ps/top/kill/mem/tree) |

> Signature M4 detail: **summarize-eviction** is where the OS uses the LLM
> *inside* its own memory manager — see `docs/OS_MAPPING.md` "LLM inside the OS".

**228 tests passing + 4 Docker-conditional skipped (232 collected).** ~2.4k app
LOC + ~1.6k test LOC.

---

## Hardening pass — correctness, accounting, honesty (A–H)

After M5, a review pass found that the milestone code had real concurrency bugs
(masked because the eval only ran the scheduler on one worker) and a few claims
that ran ahead of the implementation. This pass fixed them and made the docs
match the code. Mapping each area onto the architecture above:

| # | Area | What changed |
|---|---|---|
| A | Scheduler + Worker Pool | **Atomic dispatch.** `ReadyQueue.pop_best()` selects+removes under one lock (no double-dispatch on multiple workers); in-flight accounting so `is_idle()`/`wait_idle()` can't read idle mid-dispatch; per-step quota **refund** when a step makes no LLM call; **terminal states are absorbing** + cooperative cancel, so a killed agent can't be resurrected to DONE. |
| B | Memory + Backend + Quota | Summarize-eviction now goes through the **batcher** and is **charged to the quota** (the OS owns *its own* Solar calls too). The pager is **per-kernel** (no process-global singleton) with a configurable budget. Swap is honestly a one-way offload + explicit `Kernel.swap_in`. |
| C | Scheduler | Preemption made textbook-correct: **FCFS is non-preemptive** (runs an agent to completion → convoy effect) while **RoundRobin preempts on a real quantum** of LLM calls — they share FIFO selection but differ in preemption, so RR is genuinely distinct, not an FCFS stub (single-shot agents make them coincide, which is the correct RR-degenerates-to-FCFS case). **Priority has wait-time aging** so nothing starves and is preemptive (re-picks each call). O(n) selection is a documented trade-off. Quantified by the `scheduler_preemption` eval. |
| D | Sandbox | Docker-absent fallback is **loud / fail-closeable**; container runs **non-root** with capped output; the gate is reframed as a **pre-filter/audit log, not security** (more rules, but documented blind spots). `kernel.status()` exposes the live posture. |
| E | Process Tree + IPC | **Reaping** — `kill`/`reap_terminal`/`shutdown` free mailboxes; ZOMBIE entries no longer leak. A WAITING consumer is **bounded by an input deadline** (no forever-hang on a lost pipe message). State machine has a legal-transition table. |
| F | Executor | Two agent kinds, documented honestly: *single-shot* (plain/coder: one prompt → one response) and *multi-step* (`CapabilitySet.agent()`: a real ReAct loop in `agent_loop.py`, one LLM call per step) so the scheduler's quantum time-slices a real agent (`multistep_agents` eval). Multi-step is opt-in, not claimed for every agent. |
| K | Real-OS substrate | `gcos.osprims` makes the OS claims **kernel-enforced** on Linux: cgroup v2 CFS shares/limits, `SIGSTOP`/`SIGCONT` real-process preemption, `mmap`+`madvise` demand paging, POSIX shm, seccomp-bpf, an in-kernel eBPF program, **and GCOS's own sched_ext CPU scheduler (`scx_gcos`) that the kernel actually loads and dispatches with** (CI `scx-ext` job: `state`=enabled, `ops`=gcos). Linux-first; macOS degrades loudly; six ubuntu CI jobs verify the Linux paths. See `docs/REAL_OS.md`. |
| G | Eval | Added metrics that actually exercise the bugs above (multi-worker no-double-dispatch, quota conservation, repeated speedup mean±CI) and an enlarged, honestly-blind-spotted gate corpus. |

See `docs/EVALUATION.md` for the reference numbers and `docs/OS_MAPPING.md` for the
concept-by-concept table (both updated in this pass).
