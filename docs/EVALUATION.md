# GCOS — Evaluation

This document defines how we measure GCOS and reports a reference run. Every
metric here is **reproducible offline** — it needs no Upstage key, makes no
network calls, and is driven by deterministic fakes — so a grader can rerun it
on any machine:

```bash
python -m gcos.eval                         # rich summary table
python -m gcos.eval --json                  # machine-readable JSON
python -m gcos.eval --out docs/RESULTS.md   # write a markdown report
```

The harness lives in `gcos/eval/` and is covered by `tests/test_eval.py`.

We picked metrics that quantify the **operating-system mechanisms** GCOS
implements, not the quality of the underlying LLM. Each maps to an OS concept
the project claims (see `docs/OS_MAPPING.md`).

---

## Why these metrics

The project's thesis is that GCOS is a real (userspace) OS for LLM agents, not
a thin API wrapper. The honest way to defend that is to show the OS mechanisms
do measurable work. So each metric isolates one mechanism and asks a yes/no or
how-much question about it.

| # | Metric | OS concept exercised | Question answered |
|---|--------|----------------------|-------------------|
| 1 | Concurrency speedup (single + repeated mean±CI) | threads, synchronization, scheduling | Does the worker pool actually parallelize agents, and is the speedup stable across runs? |
| 2 | Scheduler ordering | scheduling (priority) | Are higher-priority agents dispatched first? |
| 2b | FCFS vs RR preemption | scheduling (preemption) | Does FCFS run to completion (convoy) while RR rotates on a quantum — i.e. is RR actually distinct from FCFS? **(C8)** |
| 2c | Multi-worker no-double-dispatch | scheduling, synchronization | With W>1 workers, is each agent dispatched *exactly once* (no Priority race)? **(A1)** |
| 2d | Quota conservation | resource accounting | Does the shared OS budget charge only real LLM calls, refunding no-call exits? **(A3)** |
| 3 | Policy gate detection | syscall pre-filter (NOT security) | What fraction of obvious dangerous calls does the cheap filter catch, and does it over-block? |
| 4 | Eviction efficacy | virtual memory / paging | Does the context pager bring an over-budget context back under budget? |

> Metrics 2b–2d were added because the original harness ran the scheduler on a
> *single* worker with single-shot fakes — so it never exercised RR's quantum vs
> FCFS (C8), the multi-worker Priority dispatch race (A1), or the quota-refund
> path (A3). They drive a real worker pool and assert the behaviour/invariants
> those gaps hid.

---

## 1. Concurrency speedup

**Method.** Spawn N single-call agents and drain them with a `WorkerPool`,
once with 1 worker and once with W workers. The step runner is a fake that
`time.sleep(per_call_s)` to simulate one LLM round-trip; because `time.sleep`
releases the GIL, concurrent workers genuinely overlap. We report wall-clock
for each and the ratio.

**Config.** N = 8 agents, per_call_s = 0.05 s, W = 4 workers. The repeated
variant runs the measurement 5× (per_call_s = 0.02 s) and reports mean ± stddev
and a 95% CI half-width, because a single timing number is noisy.

**Reference result (machine-dependent — yours will differ).**

| | Wall-clock |
|---|---|
| Serial (1 worker) | ~0.44 s |
| Parallel (4 workers) | ~0.12 s |
| **Speedup (single run)** | **~3.6x** |
| **Speedup (mean of 5)** | **~3.3x ± 0.06** (95% CI ±0.05, range 3.2-3.4x; ideal 4x) |

The gap from the ideal 4x is thread-scheduling and queue-contention overhead,
which is the expected, honest result for a thread-pool model. Reporting the mean
± CI over repeats (not one lucky run) is what backs the claim. The exact number
is machine-dependent; the test asserts only that parallel beats serial by a
comfortable margin.

---

## 2b. FCFS (non-preemptive) vs RR (preemptive quantum) — C8

**Method.** Drive the **real worker pool** (1 worker) with 3 multi-step agents
(4 LLM calls each) once under FCFS and once under RR(quantum=2). Record the
dispatch order; report the longest consecutive run per agent and the mean
"time-to-first-slice" (how soon each agent first runs).

This is the metric that answers "is RR actually different from FCFS, or just a
`popleft` stub?" FCFS is **non-preemptive** (quantum=None) so it runs each agent
to completion; RR **preempts** every quantum so the agents interleave. (For
single-shot agents they coincide — RR with quantum ≥ burst == FCFS — so the
difference only shows up with multi-step jobs.)

**Reference result.**

| | Dispatch order | Longest run | Mean time-to-first-slice |
|---|---|---|---|
| FCFS | `[1,1,1,1, 2,2,2,2, 3,3,3,3]` | 4 (run to completion → convoy) | 4.0 |
| RR(2) | `[1,1, 2,2, 3,3, 1,1, 2,2, 3,3]` | 2 (rotates at quantum) | 2.0 |

RR halves the mean wait for a first slice (4.0 → 2.0) and caps any agent's
monopoly at the quantum — the textbook RR-fixes-FCFS-convoy result. **PASS**.

## 2c. Multi-worker priority — no double-dispatch (A1)

**Method.** Drain 40 agents through **8** workers + `PriorityScheduler`, with a
fake runner that counts executions per PID under a lock. Assert every agent ran
**exactly once**. This is an **invariant check on the current atomic dispatch**
(`pop_best()` selects + removes under one lock).

**Honesty note.** This metric does *not* fail on the old `snapshot()`+`max()`+
`pop()` code: under CPython's GIL the select→remove window almost never opens, so
the racy version passes it too. It guards the new contract, not the old bug. The
*deterministic* reproduction — which injects a select→remove window and shows the
old pattern double-dispatching while `pop_best` does not — lives in
`tests/test_concurrency_correctness.py::test_old_nonatomic_selection_can_double_dispatch_but_pop_best_cannot`.

**Reference result.** 40/40 agents dispatched, max runs/agent = **1** → **PASS**.

## 2d. Quota conservation (A3)

**Method.** Run 8 agents that make a real call and 8 that exit *without* one
(simulating a gate DENY / per-agent-quota-0 / timeout). Assert the shared OS
quota's `used` equals the number of **real** calls (8), not the number of steps
(16).

**Reference result.** Global quota used = **8** == real calls → conserved
**True**. (Before the fix it would read 16: the worker spent a unit per step and
never refunded the no-call exits.)

---

## 2. Scheduler ordering

**Method.** Enqueue agents with mixed priorities **before** starting a single
worker, then record the order `PriorityScheduler` actually dispatches them in.
One worker makes the order deterministic, so it reflects scheduler policy alone.

**Reference result.** Input priorities `[5, 1, 9, 3, 7, 2, 8]` were dispatched
as `[9, 8, 7, 5, 3, 2, 1]` — strictly priority-descending. **PASS.**

This confirms the scheduler is not just FCFS in disguise for the priority case
(the RR scheduler, by contrast, is intentionally FCFS-equivalent for
single-step agents — documented in `docs/OS_MAPPING.md`).

---

## 3. Policy gate detection — a cheap pre-filter, **NOT** security

**Read this before quoting the number.** The gate is a **bypassable regex scan
of source text**. Its value is (a) blocking the obvious stuff before paying for
an LLM call and (b) producing an auditable DENY log — *not* security. The real
isolation boundary is the Docker sandbox. So treat the percentage below as "how
good is the cheap filter", never as a safety guarantee. (See `docs/OS_MAPPING.md`
item 16/17 and the D12 reframing.)

**Method.** Run a labelled corpus through `policy_gate.scan_prompt` /
`scan_code`: positives are prompts/code the gate should deny (jailbreak tags,
`os.system`, `subprocess`, `eval`/`exec`/`compile`, dynamic imports, `os.popen`,
`pty`, `/etc` reads, absolute-path deletes, sockets, HTTP libraries, `rm -rf /`,
`shutil.rmtree("/")`), negatives are realistic benign prompts/code. The corpus
**deliberately keeps known blind spots** — aliasing (`f = os.system; f('id')`)
and reflection (`getattr(os, 'sys'+'tem')`) — so the number reflects reality.

**Reference result.**

| Metric | Value |
|---|---|
| Attacks caught | 27 / 29 (**93.1%** recall) |
| False positives on benign input | 0 / 21 (**0.0% FPR**) |
| Precision | **100%** |
| Known blind spots (by design) | `f = os.system; f('id')`, `getattr(os, 'sys'+'tem')('id')` |

The two misses are exactly the point: a regex can't see intent through aliasing
or reflection, and no amount of added patterns closes that class — which is why
the gate is the *first* line and Docker (`--network=none --read-only
--cap-drop=ALL`, non-root) is the boundary. The corpus is still small and
hand-built; it demonstrates the filter's behaviour and blind spots, not an
exhaustive audit.

---

## 4. Context pager eviction efficacy

**Method.** Build a PCB whose context is far over budget (one pinned system
page + 20 conversation pages, 620 tokens total, budget 200), then call
`ContextPager.assemble` and measure tokens/pages before and after. A fake
summarizer stands in for Solar so the **mechanism** (N old pages compressed
into 1 summary page) runs deterministically; we test two policy stacks.

**Reference result.** Budget = 200 tokens, starting size = 620 tokens / 21 pages.

| Policy stack | Tokens after | Pages after | Fits budget |
|---|---|---|---|
| LRU only | 200 (−67.7%) | 7 | yes |
| LRU + summarize | 148 (−76.1%) | 6 | yes |

Both bring the context under budget; the pinned system page is never evicted.
This is the paging mechanism doing its job.

---

## 5. Real-OS substrate — kernel-enforced, not simulated (`gcos.osprims`)

The metrics above measure the userspace *orchestration*. This group measures the
**real kernel primitives** that back the same claims. Full design + verification
matrix in `docs/REAL_OS.md`. Linux is first-class; on macOS the Linux-only rows
report `enforced=False` and the ubuntu CI jobs exercise them on real Linux
(including loading GCOS's own `scx_gcos` CPU scheduler into the kernel).

| Metric | OS concept | What it proves | Runs on |
|---|---|---|---|
| `os_capabilities` | — | honest posture: is the host kernel-enforcing or simulating? | all |
| `multistep_agents` | scheduling | real ReAct agents interleave under RR, run-to-completion under FCFS (A1) | all |
| `real_preemption` | scheduling | RR vs FCFS over **real processes**, SIGSTOP/SIGCONT preemption | all (POSIX) |
| `demand_paging` | virtual memory | mmap + madvise page-out, real fault-in on access | all (POSIX) |
| `cgroup_cpu_share` | resource control | measured CPU share tracks cgroup `cpu.weight` (real Linux CFS) | Linux / CI |

**Method.** `real_preemption` forks real CPU-bound children and time-slices them
with `SIGSTOP`/`SIGCONT` (RR) or runs each to completion (FCFS). The jitter-proof
signal of preemption: FCFS keeps each child as **one contiguous block** (N
children → N blocks, the convoy), while RR **interleaves** them into many more
blocks than there are children. (RR's per-child max run isn't exactly 1 — the
last child legitimately runs alone at the tail — so we assert on block count,
not max run.) True preemption, not cooperative yielding.
`demand_paging` stores pages in a file-backed `mmap`, `madvise(MADV_DONTNEED)`s
them out (the kernel drops the physical page), and faults them back in on read.
`cgroup_cpu_share` puts one CPU-bound child per `cpu.weight` in its own cgroup,
pins them to one CPU so they compete, and reads `cpu.stat` back.

**Reference result (real Linux, privileged container).**

| `cpu.weight` | expected share | measured share |
|---:|---:|---:|
| 100 | 7.7% | 7.9% |
| 300 | 23.1% | 23.5% |
| 900 | 69.2% | 68.5% |

The measured CPU share tracks the weights to within ~1% — the **Linux CFS
scheduler**, not a Python loop, is allocating the CPU. This is the reproducible
analogue of the hand-measured CFS-share benchmarks in the strongest kernel
projects, and the headline evidence that GCOS's resource control is real.

---

## What this harness does NOT measure (and why)

- **Summarize-eviction quality.** The reproducible run uses a fake summarizer,
  so it measures that pages are compressed, not how good the summary is.
  Judging summary fidelity needs a live Solar key and a held-out QA set; that
  is future work, not a reproducible CI metric.
- **Real Solar throughput / batching under load.** The batcher's semaphore and
  token bucket are exercised live in the demo (`/api/batcher/stats`), but a
  reproducible throughput number would need a stable, rate-limited Solar
  endpoint, so we keep it out of the offline harness.
- **End-to-end agent task accuracy.** GCOS is infrastructure; task quality is a
  property of Solar Pro 3, not of the OS layer, so we deliberately do not
  conflate the two.

---

## Limitations

The speedup metric is timing-dependent and will vary with machine load; the
test asserts only that parallel beats serial by a comfortable margin, not an
exact ratio. The gate corpus is small and hand-built — it demonstrates the
filter's behaviour and its known blind spots, but is not an exhaustive security
audit. Every metric here is mechanism-level; they show the OS components work,
which is the claim the project needs to support.
