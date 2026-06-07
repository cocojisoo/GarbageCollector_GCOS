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
| 1 | Concurrency speedup | threads, synchronization, scheduling | Does the worker pool actually parallelize agents? |
| 2 | Scheduler ordering | scheduling (priority) | Are higher-priority agents dispatched first? |
| 3 | Policy gate detection | syscall gate / sandbox | What fraction of dangerous calls does the first-line filter catch, and does it over-block? |
| 4 | Eviction efficacy | virtual memory / paging | Does the context pager bring an over-budget context back under budget? |

---

## 1. Concurrency speedup

**Method.** Spawn N single-call agents and drain them with a `WorkerPool`,
once with 1 worker and once with W workers. The step runner is a fake that
`time.sleep(per_call_s)` to simulate one LLM round-trip; because `time.sleep`
releases the GIL, concurrent workers genuinely overlap. We report wall-clock
for each and the ratio.

**Config.** N = 8 agents, per_call_s = 0.05 s, W = 4 workers.

**Reference result.**

| | Wall-clock |
|---|---|
| Serial (1 worker) | ~0.45 s |
| Parallel (4 workers) | ~0.15 s |
| **Speedup** | **~2.8x** (2.7-3.0x across runs; ideal 4x, ~70% efficiency) |

The gap from the ideal 4x is thread-scheduling and queue-contention overhead,
which is the expected, honest result for a thread-pool model. Speedup scaling
with W is the evidence that the pool + ready-queue + scheduler are doing real
concurrent dispatch.

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

## 3. Policy gate detection

**Method.** Run a labelled corpus through `policy_gate.scan_prompt` /
`scan_code`: positives are prompts/code the gate should deny (jailbreak tags,
`os.system`, `subprocess`, `eval`/`exec`, `/etc` reads, raw sockets, HTTP
libraries, `rm -rf /`, `shutil.rmtree("/")`), negatives are realistic benign
prompts/code. We compute detection rate (recall), false-positive rate, and
precision. The corpus **deliberately includes two evasions the regex gate does
not catch** (`os.popen`, `os.remove('/etc/...')`) so the number reflects reality.

**Reference result.**

| Metric | Value |
|---|---|
| Attacks caught | 16 / 18 (**88.9%** detection) |
| False positives on benign input | 0 / 10 (**0.0% FPR**) |
| Precision | **100%** |
| Known misses | `os.popen(...)`, `os.remove('/etc/...')` |

The story is exactly the defense-in-depth design: the gate is a **cheap first
filter** with zero false alarms on benign code, and the ~11% it misses is what
the Docker sandbox (`--network=none --read-only --cap-drop=ALL`) exists to
contain. The gate is not claimed to be complete on its own.

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
audit. All four metrics are mechanism-level; they show the OS components work,
which is the claim the project needs to support.
