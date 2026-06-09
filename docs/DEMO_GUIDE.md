# GCOS — Grading-Day Demo Guide (7 minutes)

A copy-paste-runnable script that hits every OS concept GCOS implements.
Run from one terminal at the project root with `.env` populated.

> Tip: open **3 terminal panes** before starting:
>   1. server / pipeline / coder commands
>   2. REPL shell (left running)
>   3. browser tab on `http://127.0.0.1:8765/`

---

## 0 — Preflight (10 s)

```bash
cd <repo root>                # e.g. ~/GarbageCollector_GCOS
python -m pytest -q tests/    #  →  188 passed, 4 skipped (Docker)
```

> macOS/Docker-via-colima walkthrough: see `DEMO_RUN.md` at the repo root.

Show the green test count. Move on.

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

## Talking points for Q&A

| If asked… | Point at |
|---|---|
| "Is this a real kernel?" | No — userspace mini-OS. The novelty is the *concept mapping*, not ring-0 code. |
| "Why Solar for evict?" | Because we have it. The OS *managing the LLM with the LLM* is the project's signature — and that summarize call goes through the OS's own batcher + quota, not around them. |
| "Are the agents autonomous multi-step?" | Honestly, **no — single-shot today** (one prompt → one response). The worker pool, RR quantum, and re-queue path are multi-step-ready; the tool-use loop is future work. We don't oversell it. |
| "Why does RR look like FCFS sometimes?" | They differ in **preemption**, not selection (both pick FIFO). **FCFS is non-preemptive** (runs each agent to completion → convoy effect); **RR preempts** every quantum of LLM calls. With single-shot agents every job is one call, so RR (quantum ≥ burst) *degenerates* to FCFS — standard, not a stub. With multi-step agents they diverge: see the `scheduler_preemption` eval (FCFS `[1,1,1,1,2,2,2,2,3,3,3,3]` vs RR `[1,1,2,2,3,3,…]`, mean time-to-first-slice 4.0 → 2.0). |
| "Doesn't high priority starve low priority?" | No — `PriorityScheduler` adds **wait-time aging**, so a long-waiting agent's effective priority rises and it eventually runs. |
| "Is the 88/93% gate number a security metric?" | **No.** The gate is a cheap pre-filter + audit log; that % is filter recall, with documented blind spots. Security is the Docker sandbox. |
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
