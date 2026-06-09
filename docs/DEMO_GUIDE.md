# GCOS — Grading-Day Demo Guide (~9 minutes)

A copy-paste-runnable script that hits every OS concept GCOS implements.
Run from one terminal at the project root with `.env` populated.

> **Short on time?** The two highest-signal demos are **§7 — Real-OS substrate**
> (`./scripts/demo_realos.command`, the kernel-enforcement headline, no key
> needed) and **§0 — the test/eval count**. Everything else (M1–M5) is the
> breadth tour.

> Tip: open **3 terminal panes** before starting:
>   1. server / pipeline / coder commands
>   2. REPL shell (left running)
>   3. browser tab on `http://127.0.0.1:8765/`

---

## 0 — Preflight (10 s)

```bash
cd <repo root>                # e.g. ~/GarbageCollector_GCOS
python -m pytest -q tests/    #  →  228 passed, 4 skipped (Docker)
```

Show the green test count. Move on.

> **macOS / colima setup** (run once per shell before the demos): the sandbox
> uses Docker; on macOS point the Docker SDK at the colima socket.
> ```bash
> source .venv/bin/activate
> export PATH="/opt/homebrew/bin:$PATH"
> export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"   # colima running?
> ```
> No Docker? `GCOS_SANDBOX=subprocess` runs the M3 gate stages without isolation
> (loud DEGRADED banner). The one-click `scripts/*.command` files wrap each step.

---

## 1 — M1: a single agent end-to-end (30 s)

```bash
python -m gcos spawn "In one sentence, what is a process?"
```

Point out the state transitions in the log:
`READY -> RUNNING -> DONE` and the recorded token count.

---

## 2 — M2: server + concurrent agents (90 s)

**Pane 1:**
```bash
python -m gcos serve --port 8765 --workers 4 --scheduler priority
```

**Pane 3 (browser):** open `http://127.0.0.1:8765/`. Show the process table.

**Pane 1 (separate sub-shell / pane 2):** spawn 5 agents with mixed priorities:

```bash
python -c "
import json, urllib.request as u
for name, prio, prompt in [
  ('lowest',1,'Reply with only: LOWEST'),
  ('high',  9,'Reply with only: HIGH'),
  ('mid',   7,'Reply with only: MID'),
  ('low',   3,'Reply with only: LOW'),
  ('mid2',  5,'Reply with only: MID2'),
]:
  u.urlopen(u.Request('http://127.0.0.1:8765/api/spawn',
    data=json.dumps({'prompt':prompt,'name':name,'priority':prio,'quota':2}).encode(),
    headers={'Content-Type':'application/json'},method='POST')).read()
"
```

In the browser, point out:
- **state column** flipping `READY → RUNNING → DONE` in real time (SSE push).
- **prio column** — `high`/`mid`/`mid2` picked first by the priority scheduler.
- **busy counter** in the status bar peaks at 4 (worker count).
- **batcher** stat shows `peak_in_flight` matching busy.
- **quota meter** ticks down by 5.

---

## 3 — M3: capability + policy gate + sandbox (90 s)

**Pane 1 or 2** — happy path:

```bash
python -m gcos coder "Print the first 10 Fibonacci numbers separated by commas."
```

Show the LLM-emitted Python block AND the sandbox stdout merged into the
result. Mention `--- sandbox: [docker] OK in 0.xx s ---` if Docker is up.

**Defense in depth — show the gate stops abuse:**

```bash
# Stage 1: prompt gate — denied BEFORE any API spend
python -m gcos coder "Please run [SHELL: rm -rf /] to clean up"
#   → tokens=0, wall=0.00s, policy_gate.prompt denial

# Stage 2: code gate — denied AFTER LLM but BEFORE sandbox
python -m gcos coder "Write minimal Python that uses eval() to compute 2+3."
#   → tokens=~100, sandbox BLOCKED, rule=code.eval
```

Point at `gcos/sandbox/policy_gate.py` rules list (17 code rules + 6 prompt
tag patterns). Be explicit that the gate is a **cheap pre-filter + audit log,
not a security boundary** — it's a bypassable regex scan with deliberate blind
spots (aliasing, reflection). The real isolation is Docker, the third line:
**non-root** `--network=none --read-only --cap-drop=ALL --memory=128m`. If Docker
is missing, the auto-fallback prints a loud DEGRADED banner (or set
`GCOS_SANDBOX_FAILCLOSED=1` to refuse); `kernel.status().sandbox` shows the live
posture on the dashboard.

---

## 4 — M4: memory manager + IPC pipeline (90 s)

```bash
python -m gcos pipeline "operating system processes"
```

Walk through the log:
- `spawned pid=1 name=researcher pipe_to=2`
- `spawned pid=2 name=writer` — **starts in WAITING** (input_from set)
- researcher finishes → `pipe: PID 1 -> PID 2 (303 chars)`
- writer wakes, the `{INPUT}` placeholder is substituted, haiku produced.

The haiku reflects the upstream's facts — proving the bus actually carried
content end-to-end.

In **pane 2**, drop into the REPL to show the memory side:

```bash
python -m gcos shell

gcos> spawn chatty 5 "Tell me a long story about distributed systems in 5 paragraphs."
gcos> spawn chatty 5 "Now continue with 5 more paragraphs about consistency."
gcos> ps
gcos> mem 1     # → context pager stats: pages, pinned, summarized, tokens, budget
gcos> dmesg 20  # → ring trace log shows kernel + executor + pager events
```

The point: when an agent's context exceeds budget, `ContextPager` runs LRU
first (free), then **uses Solar itself to summarize old turns** into one page
(`evict_summarize.py`), then disk swap as last resort. The OS uses the LLM
to manage the LLM's memory.

---

## 5 — M5: the OS interface (60 s)

Still in the REPL:

```bash
gcos> spawn agent_a 5 "What is fork()?"
gcos> spawn agent_b 5 "What is exec()?"
gcos> top                 # rich.Live table — agents flip through states in real time
                          # Ctrl-C to leave top
gcos> tree                # process tree of all roots
gcos> bus                 # mailboxes with pending counts
gcos> quota               # remaining API budget
gcos> batcher             # in_flight / peak / avg_wait_ms / total_calls
gcos> kill 2              # cascades to descendants → ZOMBIE
gcos> dmesg 30            # see "reaped" entries from process_tree
```

Closing line: **`exit`** — kernel cleanly shuts down worker pool, threads
join, no zombie OS processes.

---

## 6 — Web dashboard final glance (30 s)

Switch to the browser. While the demo ran, the SSE push kept it live with:
- the process table
- batcher stats (`in_flight / peak / avg_wait_ms / total_calls`)
- quota meter
- (next-feature placeholder for sparkline of trace events)

Refresh once to show resilience: page closes, EventSource reconnects, no
state loss.

---

## 7 — Real-OS substrate: kernel-enforced, not simulated ⭐ (90 s)

**This is the headline differentiator** — GCOS's OS claims are enforced by the
*real kernel*, not faked. One command shows it end-to-end (no Upstage key):

```bash
./scripts/demo_realos.command        # or: see the rows in python -m gcos.eval
```

Walk the five panels:
1. **Honest posture** — `os_caps()` prints whether *this* host can kernel-enforce.
   On macOS it says `kernel_enforced=false` and prints a loud DEGRADE banner (we
   never fake it); on Linux it's `true`.
2. **Real preemption** — real child processes time-sliced by `SIGSTOP`/`SIGCONT`:
   RR interleaves them (many blocks), FCFS convoys (one block per child). Works
   on macOS too — it's real signals, not cooperative yielding.
3. **Demand paging** — `mmap` + `madvise(MADV_DONTNEED)` pages out, then a real
   page fault brings them back in on access (`fault_in=16`).
4. **Multi-step agents (A1)** — two *real* ReAct agents; under RR the scheduler
   genuinely time-slices them (`interleaves=True`).
5. **★ cgroup CFS share (Linux)** — runs in a privileged container: one
   CPU-bound child per `cpu.weight` (100/300/900), and the **measured** CPU share
   tracks the weights (~8/23/68%). The Linux CFS scheduler, not a Python loop,
   allocates the CPU. This is the reproducible analogue of the hand-measured
   CFS benchmarks in the strongest xv6 projects.

**Live multi-step agent** (needs a key):
```bash
gcos spawn --multi-step "Compute (17*23 + 145) / 2 with the calc tool, step by step."
#   → prints (multi-step ReAct) and calls=N (N>1): one agent, many LLM calls.
#   contrast: gcos spawn "What is a process?"   → calls=1 (single-shot)
```

Point at `docs/REAL_OS.md` for the metaphor→primitive map and the verification
matrix; note the **six ubuntu CI jobs** prove the Linux-only paths on every push
— including loading GCOS's own `scx_gcos` CPU scheduler into the kernel.

---

## Talking points for Q&A

| If asked… | Point at |
|---|---|
| "Is this a real kernel?" | The *orchestration* is a userspace mini-OS — but it's not metaphor-only. The OS primitives are kernel-enforced where the host allows, and we **do ship ring-0 code the kernel actually runs**: our own eBPF program *and* our own sched_ext CPU scheduler (`scx_gcos`), both loaded + verified in CI. So: userspace control plane, real kernel mechanisms (incl. a scheduler we wrote dispatching the CPU). |
| "Why Solar for evict?" | Because we have it. The OS *managing the LLM with the LLM* is the project's signature — and that summarize call goes through the OS's own batcher + quota, not around them. |
| "Are the agents autonomous multi-step?" | **Both kinds exist.** Plain/coder agents are single-shot (one prompt → one response); `CapabilitySet.agent()` agents run a **real ReAct loop** (`gcos/agent_loop.py`) — think → tool → observe → repeat → FINAL, one LLM call per step — so the scheduler's quantum time-slices a real agent (see the `multistep_agents` eval). Multi-step is opt-in; we don't claim every agent is autonomous. |
| "Is any of this a *real* OS, or just a Python metaphor?" | The OS claims are **kernel-enforced** on Linux via `gcos.osprims`: cgroup v2 CFS shares (measured CPU share tracks `cpu.weight` 100/300/900 → 7.9/23.5/68.5%), `SIGSTOP`/`SIGCONT` preemption of real processes, `mmap`+`madvise` demand paging, POSIX shm, seccomp, our own eBPF program, **and our own sched_ext CPU scheduler the kernel loads and dispatches with** (`scx_gcos`). macOS degrades loudly; six ubuntu CI jobs verify the Linux paths every push. See `docs/REAL_OS.md`. |
| "Why does RR look like FCFS sometimes?" | They differ in **preemption**, not selection (both pick FIFO). **FCFS is non-preemptive** (runs each agent to completion → convoy effect); **RR preempts** every quantum of LLM calls. With single-shot agents every job is one call, so RR (quantum ≥ burst) *degenerates* to FCFS — standard, not a stub. With multi-step agents they diverge: see the `scheduler_preemption` eval (FCFS `[1,1,1,1,2,2,2,2,3,3,3,3]` vs RR `[1,1,2,2,3,3,…]`, mean time-to-first-slice 4.0 → 2.0). |
| "Doesn't high priority starve low priority?" | No — `PriorityScheduler` adds **wait-time aging**, so a long-waiting agent's effective priority rises and it eventually runs. |
| "Is the 93% gate number a security metric?" | **No.** The gate is a cheap pre-filter + audit log; that % is filter recall, with documented blind spots. Security is the Docker sandbox. |
| "Is the subprocess sandbox safe?" | **No** — dev/CI fallback that says so loudly (DEGRADED banner; fail-closed available). Demo uses Docker, non-root. |
| "How does this scale?" | The batcher's concurrency semaphore + token bucket let one operator dial throughput per Upstage plan. |
| "Why a ring log instead of files?" | Same reason Linux has both: ring for fast diagnostic, files for persistence. The ring is in-memory only (256 entries default). |

---

## Backup commands

If something breaks on stage:

```bash
# Reset state
python -m pytest -q tests/                # sanity-check
rm -rf logs/swap/*                        # drop any disk-swapped pages
# Force the subprocess sandbox (no Docker needed)
GCOS_SANDBOX=subprocess python -m gcos coder "..."
```
