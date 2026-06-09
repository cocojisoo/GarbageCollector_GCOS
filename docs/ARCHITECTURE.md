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

## 5-Week Milestones

### M1 — Skeleton (this milestone)
- `kernel/pcb.py` AgentControlBlock + AgentState + CapabilitySet + ContextPage
- `kernel/pid_alloc.py` monotonic PID allocator
- `kernel/ready_queue.py` thread-safe FIFO
- `kernel/scheduler.py` FCFS implementation + abstract `Scheduler` interface
- `backend/solar_client.py` OpenAI-SDK wrapper for Solar Pro 3
- `executor.py` single-shot run-one-agent
- `main.py` CLI: `python -m gcos spawn "..."`
- Tests: `test_pcb.py`, `test_scheduler_fcfs.py`
- **Demo**: `python -m gcos spawn "Hello in 3 words"` → prints reply, agent goes READY→RUNNING→DONE.

### M2 — Concurrency
- `kernel/worker_pool.py` N threads + Condition variable
- `kernel/scheduler.py` add `PriorityScheduler`, `RoundRobinScheduler`
- `kernel/quota.py` mutex-protected shared int (API budget)
- `api/server.py` + `api/routes.py` minimal FastAPI
- `web/` HTML+JS dashboard (polling, no SSE yet)
- **Demo**: spawn 10 agents with mixed priorities; observe ordering.

### M3 — Sandbox + Capability ✅
- `sandbox/policy_gate.py` two-stage filter — a **cheap pre-filter + audit log, not a security boundary** (it's a bypassable regex source scan):
  - `scan_prompt()` rejects jailbreak tags (`[SHELL:]` `[KERNEL:]` `[NET:]` `[SUDO:]` `[EXFIL:]` `[EXEC:]`) **before** spending an LLM call.
  - `scan_code()` rejects dangerous patterns in LLM-emitted code (**17 rules**: os.system, subprocess, eval/exec/compile, dynamic imports, os.popen, pty, raw sockets, HTTP libs, `/etc/*` reads, absolute-path deletes, `rm -rf /`, etc. — `iter_code_rule_ids()`). Known blind spots (aliasing, reflection) are kept on purpose; Docker is the real boundary.
- `sandbox/runner.py` defines `SandboxRunner` ABC + `SandboxResult` dataclass.
- `sandbox/docker_runner.py` real isolation, **non-root** (`user=65534:65534`) with capped output:
  `--network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --memory=128m --memory-swap=128m --pids-limit=64 --cpus=1 --tmpfs /work:rw,exec,mode=1777`. `is_available()` lets the factory pick it iff Docker daemon is reachable.
- `sandbox/subprocess_runner.py` weak-isolation **dev/CI fallback** — minimal env, temp cwd, timeout. The auto-fallback prints a loud DEGRADED banner; `GCOS_SANDBOX_FAILCLOSED=1` refuses instead of degrading. `sandbox_info()` surfaces the live posture (runner/isolation/degraded) into `kernel.status()`.
- `sandbox/extract.py` extracts the first python (or unlabeled) ```` ```python ``` ```` block from LLM replies.
- `sandbox/__init__.py:make_runner()` is the factory: `GCOS_SANDBOX=docker|subprocess|auto`.
- `coder.py` ties it together: a coder agent's `run_coder_step()` does
  *policy_gate(prompt) → Solar → extract_python() → policy_gate(code) → sandbox.run_python() → merge stdout into pcb.result*.
- Capability dispatch in `executor.run_step`: if `pcb.capability.can_exec_code`, route to coder path.
- **Verified** via three live demos against Solar Pro 3:
  - happy: Fibonacci prompt → Solar emits code → sandbox runs → stdout merged into result.
  - prompt gate: `[SHELL: rm -rf /]` → denied before any LLM call, no API spend.
  - code gate: prompt asking for `eval()` → LLM complies → code gate blocks before sandbox.

### M4 — Memory Manager + IPC ✅
- `memory/context_pager.py` — assembles within a token budget, applies policies in order, supports `extra_user_prompt=` so the executor can attach the current turn without persisting it twice.
- `memory/tokens.py` — char-based fallback (~4 chars/token) since Solar's tokenizer is private; pages roundtripped through Solar carry the real `usage.completion_tokens`.
- `memory/policy.py::EvictionPolicy` ABC — `evict(pcb, *, client) -> int` returning net pages freed.
- `memory/evict_lru.py` — drops oldest non-pinned page; honors `min_keep` floor; never touches pinned (e.g., system prompts).
- `memory/evict_summarize.py` — takes the oldest N non-pinned, non-summarized pages and asks Solar to compress them into a 2-3 sentence summary page (marked `summarized=True`, kept in-place). The OS literally uses the LLM to manage its memory.
- `memory/swap.py::SwapEvictionPolicy` — serializes a batch to `logs/swap/<pid>/<ts>.json`. `swap_in()` restores. The "disk" tier.
- `memory/default_policies()` — the standard 3-tier stack `[LRU, Summarize(batch=4), Swap(batch=2)]`.
- `ipc/message_bus.py::MessageBus` — per-PID bounded `queue.Queue` mailboxes; `send(target, payload)` / `recv(pid, timeout)` / `has_pending(pid)` / `snapshot()`. `resolve_input_placeholder()` swaps `{INPUT}` in the prompt for upstream content.
- `kernel/process_tree.py::ProcessTree` — `children_of`, `descendants_of`, `ancestors_of`, `reap_descendants(pid, reason)`, `tree_view(pid)`. `Kernel.kill` calls `reap_descendants` cascading children to ZOMBIE.
- `kernel/pid_alloc.py::PidAllocator.peek()` — lets the CLI know future PIDs so it can wire `pipe_to`/`input_from` *atomically before either agent runs* (no race).
- `kernel/worker_pool.py` — extended: if `pcb.input_from` is set and `{INPUT}` is in the prompt, recv from bus (timeout `input_wait_s`); on miss the PCB re-parks as WAITING. After terminal, if `pipe_to` is set, send the result.
- `kernel/kernel.py` — owns `bus` + `tree`; `spawn(input_from=...)` puts agents directly into WAITING.
- **Verified live (`python -m gcos pipeline "operating system processes"`)**:
  researcher (PID 1) writes 3 OS facts → bus pipes 303 chars to writer (PID 2) → writer composes a haiku reflecting those facts. End-to-end 2.4s, 234 tokens.

### M5 — Polish + Demo ✅
- `backend/batcher.py::BatchingSolarClient` — wraps `SolarClient` with
  - `threading.BoundedSemaphore(max_concurrent)` capping in-flight requests,
  - a `TokenBucket(rate_per_s, burst)` smoothing per-second throughput,
  - stats (`total_calls`, `peak_in_flight`, `avg_wait_ms`, `last_429_ts`, ...)
    exposed via `/api/batcher/stats` and the kernel status payload.
  Workers no longer talk to Solar directly — the OS owns rate management.
- `kernel/ring_log.py::RingTraceLog` — bounded `deque(maxlen=capacity)`
  attached as a `logging.Handler` to the `gcos` logger. Every kernel/
  scheduler/policy/sandbox log line lands in-memory (default 256 entries),
  surfaced via `/api/log` and the REPL's `dmesg`.
- `api/sse.py` — `/api/events` streams JSON snapshots via `sse_starlette`
  with payload de-duplication; web dashboard auto-reconnects on disconnect
  and falls back to polling if `EventSource` isn't supported.
- `web/script.js` — replaced `setInterval` polling with a single
  `EventSource` listener and a polling fallback path.
- `shell/repl.py::Shell` — `rich`-based interactive shell. Commands:
  `spawn`, `coder`, `ps`, `top` (live), `kill` (cascades), `cat`, `mem`
  (pager stats per PID), `tree`, `bus`, `quota`, `batcher`, `dmesg`,
  `help`, `exit`. Embeds a Kernel directly (no HTTP indirection).
- `gcos/main.py` adds `shell` subcommand.
- Kernel exposes `batcher` stats in `status()` and constructs the batching
  client by default; legacy tests still inject a fake `client_factory` and
  bypass it cleanly.
- **188 tests passing + 4 Docker-conditional skipped.** Total LOC ~2.4k
  application + ~1.6k tests.

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
| F | Executor | Documented honestly: **agents are single-shot** (one prompt → one response); the worker/quantum/re-queue machinery is multi-step-ready, but the tool-use loop is future work. |
| G | Eval | Added metrics that actually exercise the bugs above (multi-worker no-double-dispatch, quota conservation, repeated speedup mean±CI) and an enlarged, honestly-blind-spotted gate corpus. |

See `docs/EVALUATION.md` for the reference numbers and `docs/OS_MAPPING.md` for the
concept-by-concept table (both updated in this pass).
