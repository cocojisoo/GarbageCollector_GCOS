"""CLI for the GCOS evaluation harness.

    python -m gcos.eval                        # pretty table
    python -m gcos.eval --json                 # JSON to stdout
    python -m gcos.eval --out docs/RESULTS.md  # write a markdown report
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from gcos.eval import run_all


def _fmt_markdown(r: dict[str, Any]) -> str:
    cs = r["concurrency_speedup"]
    css = r["concurrency_speedup_stats"]
    so = r["scheduler_ordering"]
    sp = r["scheduler_preemption"]
    nd = r["priority_no_double_dispatch"]
    qc = r["quota_conservation"]
    pg = r["policy_gate_detection"]
    ev = r["eviction_efficacy"]
    lru = ev["lru_only"]
    sm = ev["lru_then_summarize"]
    lines = [
        "# GCOS — Evaluation Results (reference run)",
        "",
        "Regenerate with `python -m gcos.eval`. All metrics run offline "
        "(no Upstage key). See `docs/EVALUATION.md` for methodology.",
        "",
        "## 1. Concurrency speedup (worker pool + scheduler)",
        "",
        f"- Workload: {cs['n_agents']} agents x {cs['per_call_s']}s simulated "
        f"LLM latency",
        f"- Serial (1 worker): **{cs['serial_wall_s']}s**",
        f"- Parallel ({cs['workers']} workers): **{cs['parallel_wall_s']}s**",
        f"- Speedup: **{cs['speedup_x']}x** "
        f"(ideal {cs['ideal_speedup_x']}x, efficiency {cs['efficiency_pct']}%)",
        f"- Over {css['repeats']} runs: mean **{css['mean_speedup_x']}x** "
        f"± {css['std_speedup_x']} (95% CI ±{css['ci95_half_width']}, "
        f"range {css['min_speedup_x']}-{css['max_speedup_x']}x)",
        "",
        "## 2. Scheduler ordering (priority)",
        "",
        f"- Input priorities: `{so['input_priorities']}`",
        f"- Dispatch order observed: `{so['observed_order']}`",
        f"- Correct (priority-descending): **{so['correct']}**",
        f"- Multi-worker ({nd['workers']} workers, {nd['n_agents']} agents): "
        f"each agent dispatched exactly once: **{nd['each_run_exactly_once']}** "
        f"(max runs/agent = {nd['max_runs_per_agent']}) — no double-dispatch (A1)",
        "",
        "## 2b. FCFS (non-preemptive) vs RR (preemptive quantum)",
        "",
        f"- Workload: {sp['n_agents']} multi-step agents x {sp['steps_each']} calls, 1 worker",
        f"- FCFS order: `{sp['fcfs']['order']}` — max run {sp['fcfs']['max_consecutive_run']} "
        f"(runs each agent to completion: the convoy effect)",
        f"- RR(q={sp['rr_quantum']}) order: `{sp['rr']['order']}` — max run "
        f"{sp['rr']['max_consecutive_run']} (rotates every quantum)",
        f"- Mean time-to-first-slice: FCFS {sp['fcfs']['mean_time_to_first_slice']} vs "
        f"RR {sp['rr']['mean_time_to_first_slice']} → RR fairer: **{sp['rr_improves_fairness']}**; "
        f"RR preempts: **{sp['rr_preempts']}**",
        "",
        "## 3. Quota conservation (shared OS budget)",
        "",
        f"- {qc['real_calls']} real calls + {qc['no_call_exits']} no-call exits "
        f"(gate DENY / timeout)",
        f"- Global quota used: **{qc['quota_used']}** "
        f"(== real calls: **{qc['conserved']}**) — no leak on no-call exits (A3)",
        "",
        "## 4. Policy gate detection (cheap pre-filter, NOT security)",
        "",
        f"- Attacks caught: **{pg['attacks_caught']}/{pg['attacks_total']}** "
        f"(recall {pg['recall_pct']}%)",
        f"- False positives on benign input: "
        f"**{pg['benign_false_blocks']}/{pg['benign_total']}** "
        f"(FPR {pg['false_positive_rate_pct']}%, precision {pg['precision_pct']}%)",
        f"- Known blind spots (by design — Docker is the boundary): "
        f"`{pg['missed_samples']}`",
        f"- _{pg['note']}_",
        "",
        "## 5. Context pager eviction efficacy (paging)",
        "",
        f"- Budget: {ev['budget_tokens']} tokens",
        f"- LRU only: {lru['tokens_before']} -> {lru['tokens_after']} tokens "
        f"({lru['reduction_pct']}% smaller), fits budget: **{lru['fits_budget']}**",
        f"- LRU + summarize: {sm['tokens_before']} -> {sm['tokens_after']} tokens "
        f"({sm['pages_before']} -> {sm['pages_after']} pages), "
        f"fits budget: **{sm['fits_budget']}**",
        "",
    ]

    # --- 6. real-OS substrate (kernel-enforced) ---
    caps = r["os_capabilities"]
    ms = r["multistep_agents"]
    rp = r["real_preemption"]
    dp = r["demand_paging"]
    cs2 = r["cgroup_cpu_share"]
    lc = r["live_per_agent_cfs"]
    lines += [
        "## 6. Real-OS substrate (kernel-enforced, `gcos.osprims`)",
        "",
        f"- **Posture:** `{caps['platform']}` — kernel-enforced: "
        f"**{caps['kernel_enforced']}** (cgroup={caps['cgroup_writable']}, "
        f"signals={caps['signals']}, seccomp={caps['seccomp']}, ebpf={caps['ebpf']}). "
        "Linux-only primitives are verified by the ubuntu CI job.",
        f"- **Multi-step agents (A1):** real ReAct agents, FCFS max-run "
        f"{ms['fcfs_max_run']} vs RR max-run {ms['rr_max_run']} → RR interleaves: "
        f"**{ms['rr_interleaves']}**",
        f"- **Real preemption:** RR vs FCFS over real child processes "
        f"(SIGSTOP/SIGCONT), max consecutive run "
        f"{rp['fcfs']['max_consecutive_run']} -> {rp['rr']['max_consecutive_run']} "
        f"→ preempts: **{rp['rr_preempts']}**",
        f"- **Demand paging:** {dp['page_outs']} pages madvise'd out, "
        f"{dp['fault_ins']} faulted back in"
        + (f" (kernel majflt +{dp['kernel_majflt_delta']})" if dp["kernel_majflt_delta"] else "")
        + f" → works: **{dp['demand_paging_works']}**",
        (f"- **cgroup CFS share:** weights {cs2['weights']} measured "
         f"{cs2['measured_share_pct']}% (expected {cs2['expected_share_pct']}%) "
         f"→ tracks weight: **{cs2['tracks_weight']}**"
         if cs2.get("enforced")
         else f"- **cgroup CFS share:** degraded — {cs2.get('reason', 'n/a')} "
              "(enforced + verified on the ubuntu CI job)"),
        (f"- **Per-agent CFS (LIVE, process executor):** priorities "
         f"{lc['priorities']} → all ran as real processes: "
         f"**{lc['all_agents_ran']}** ({lc['agents_ran']}/{len(lc['priorities'])}), "
         f"each in its own per-agent cgroup cpu.weight=priority (live share "
         f"{list(lc['cpu_share_pct_by_priority'].values())}% — that cpu.weight steers "
         f"the CFS share is proven by the cgroup CFS-share row above)"
         if lc.get("enforced")
         else f"- **Per-agent CFS (LIVE, process executor):** degraded — "
              f"{lc.get('reason', 'n/a')} (verified on the ubuntu CI job)"),
        "",
    ]
    return "\n".join(lines)


def _print_table(r: dict[str, Any]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # noqa: BLE001 — rich is a dep, but degrade gracefully
        print(json.dumps(r, indent=2, default=str))
        return

    console = Console()
    cs = r["concurrency_speedup"]
    css = r["concurrency_speedup_stats"]
    so = r["scheduler_ordering"]
    sp = r["scheduler_preemption"]
    nd = r["priority_no_double_dispatch"]
    qc = r["quota_conservation"]
    pg = r["policy_gate_detection"]
    ev = r["eviction_efficacy"]

    t = Table(title="GCOS Evaluation", show_lines=True)
    t.add_column("OS concept", style="cyan", no_wrap=True)
    t.add_column("Metric")
    t.add_column("Result", style="bold")

    t.add_row(
        "Threads + scheduling",
        f"Speedup, {cs['n_agents']} agents, {cs['workers']} workers "
        f"(mean of {css['repeats']})",
        f"{css['mean_speedup_x']}x ± {css['std_speedup_x']} "
        f"(CI ±{css['ci95_half_width']})",
    )
    t.add_row(
        "Scheduling (priority)",
        "Dispatch order == priority-descending",
        ("PASS" if so["correct"] else "FAIL") + f"  {so['observed_order']}",
    )
    t.add_row(
        "Scheduling (A1, multi-worker)",
        f"Each of {nd['n_agents']} agents dispatched once ({nd['workers']} workers)",
        ("PASS" if nd["each_run_exactly_once"] else "FAIL")
        + f"  max runs/agent={nd['max_runs_per_agent']}",
    )
    t.add_row(
        "Preemption (FCFS vs RR, C8)",
        f"Multi-step: FCFS runs-to-completion, RR rotates @ q={sp['rr_quantum']}",
        ("PASS" if (sp["rr_preempts"] and sp["rr_improves_fairness"]) else "FAIL")
        + f"  run {sp['fcfs']['max_consecutive_run']}→{sp['rr']['max_consecutive_run']}, "
        f"ttf {sp['fcfs']['mean_time_to_first_slice']}→{sp['rr']['mean_time_to_first_slice']}",
    )
    t.add_row(
        "Quota accounting (A3)",
        f"used == real calls ({qc['real_calls']} ok + {qc['no_call_exits']} no-call)",
        ("PASS" if qc["conserved"] else "FAIL") + f"  used={qc['quota_used']}",
    )
    t.add_row(
        "Gate (pre-filter, NOT security)",
        "Recall / false-positive rate on labelled corpus",
        f"{pg['recall_pct']}% recall, {pg['false_positive_rate_pct']}% FPR, "
        f"{pg['precision_pct']}% prec",
    )
    lru, sm = ev["lru_only"], ev["lru_then_summarize"]
    t.add_row(
        "Paging (context)",
        f"Bring context under {ev['budget_tokens']}-token budget",
        f"LRU {lru['tokens_before']}->{lru['tokens_after']} (fits={lru['fits_budget']}); "
        f"+summarize {sm['pages_before']}->{sm['pages_after']} pages",
    )

    # --- real-OS substrate (kernel-enforced, gcos.osprims) ---
    caps = r["os_capabilities"]
    ms = r["multistep_agents"]
    rp = r["real_preemption"]
    dp = r["demand_paging"]
    cs2 = r["cgroup_cpu_share"]
    lc = r["live_per_agent_cfs"]
    t.add_row(
        "Real-OS posture (osprims)",
        "Does the host enforce OS claims in the kernel?",
        ("KERNEL-ENFORCED" if caps["kernel_enforced"] else "SIMULATED (degraded)")
        + f"  [{caps['platform']}: cgroup={caps['cgroup_writable']} sig={caps['signals']}]",
    )
    t.add_row(
        "Multi-step agents (A1)",
        "Real ReAct agents time-sliced by the scheduler (FCFS vs RR)",
        ("PASS" if ms["rr_interleaves"] else "FAIL")
        + f"  FCFS run {ms['fcfs_max_run']}, RR run {ms['rr_max_run']} (real agents)",
    )
    t.add_row(
        "Preemption — real procs",
        "RR vs FCFS over real child processes (SIGSTOP/SIGCONT)",
        ("PASS" if rp["rr_preempts"] else "FAIL")
        + f"  run {rp['fcfs']['max_consecutive_run']}->{rp['rr']['max_consecutive_run']}",
    )
    t.add_row(
        "Demand paging — mmap",
        "madvise page-out, fault-in on access",
        ("PASS" if dp["demand_paging_works"] else "FAIL")
        + f"  {dp['page_outs']} out / {dp['fault_ins']} faulted in"
        + (f", majflt+{dp['kernel_majflt_delta']}" if dp["kernel_majflt_delta"] else ""),
    )
    if cs2.get("enforced"):
        t.add_row(
            "cgroup CFS share (Linux)",
            "Measured CPU share tracks cpu.weight",
            ("PASS" if cs2["tracks_weight"] else "FAIL")
            + f"  w={cs2['weights']} -> {cs2['measured_share_pct']}%",
        )
    else:
        t.add_row(
            "cgroup CFS share (Linux)",
            "Measured CPU share tracks cpu.weight",
            f"DEGRADED — {cs2.get('reason', 'unavailable')} (verified in CI)",
        )
    if lc.get("enforced"):
        t.add_row(
            "Per-agent CFS — LIVE (process)",
            "Live executor runs each agent as a real process in its own cpu.weight cgroup",
            ("PASS" if lc["all_agents_ran"] else "FAIL")
            + f"  {lc['agents_ran']}/{len(lc['priorities'])} ran; share "
            + f"{list(lc['cpu_share_pct_by_priority'].values())}% (per-priority: see cgroup CFS row)",
        )
    else:
        t.add_row(
            "Per-agent CFS — LIVE (process)",
            "Agents as real processes under per-agent cgroup cpu.weight",
            f"DEGRADED — {lc.get('reason', 'unavailable')} (verified in CI)",
        )
    console.print(t)
    if pg["attacks_missed"]:
        console.print(
            f"[dim]Gate blind spots (by design — Docker is the boundary): "
            f"{pg['missed_samples']}[/dim]"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gcos.eval", description=__doc__)
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--out", metavar="PATH", help="write a markdown report")
    args = ap.parse_args(argv)

    results = run_all()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(_fmt_markdown(results))
        print(f"[gcos.eval] wrote {args.out}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
