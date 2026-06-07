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
    so = r["scheduler_ordering"]
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
        "",
        "## 2. Scheduler ordering (priority)",
        "",
        f"- Input priorities: `{so['input_priorities']}`",
        f"- Dispatch order observed: `{so['observed_order']}`",
        f"- Correct (priority-descending): **{so['correct']}**",
        "",
        "## 3. Policy gate detection (sandbox first line of defense)",
        "",
        f"- Attacks caught: **{pg['attacks_caught']}/{pg['attacks_total']}** "
        f"(detection {pg['detection_rate_pct']}%)",
        f"- False positives on benign input: "
        f"**{pg['benign_false_blocks']}/{pg['benign_total']}** "
        f"(FPR {pg['false_positive_rate_pct']}%)",
        f"- Precision: **{pg['precision_pct']}%**",
        f"- Known misses (handled by the Docker layer, not the gate): "
        f"`{pg['missed_samples']}`",
        "",
        "## 4. Context pager eviction efficacy (paging)",
        "",
        f"- Budget: {ev['budget_tokens']} tokens",
        f"- LRU only: {lru['tokens_before']} -> {lru['tokens_after']} tokens "
        f"({lru['reduction_pct']}% smaller), fits budget: **{lru['fits_budget']}**",
        f"- LRU + summarize: {sm['tokens_before']} -> {sm['tokens_after']} tokens "
        f"({sm['pages_before']} -> {sm['pages_after']} pages), "
        f"fits budget: **{sm['fits_budget']}**",
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
    so = r["scheduler_ordering"]
    pg = r["policy_gate_detection"]
    ev = r["eviction_efficacy"]

    t = Table(title="GCOS Evaluation", show_lines=True)
    t.add_column("OS concept", style="cyan", no_wrap=True)
    t.add_column("Metric")
    t.add_column("Result", style="bold")

    t.add_row(
        "Threads + scheduling",
        f"Speedup, {cs['n_agents']} agents, {cs['workers']} workers",
        f"{cs['speedup_x']}x  ({cs['serial_wall_s']}s -> {cs['parallel_wall_s']}s)",
    )
    t.add_row(
        "Scheduling (priority)",
        "Dispatch order == priority-descending",
        ("PASS" if so["correct"] else "FAIL") + f"  {so['observed_order']}",
    )
    t.add_row(
        "Syscall gate (sandbox)",
        "Attack detection / false-positive rate",
        f"{pg['detection_rate_pct']}% caught, {pg['false_positive_rate_pct']}% FPR",
    )
    lru, sm = ev["lru_only"], ev["lru_then_summarize"]
    t.add_row(
        "Paging (context)",
        f"Bring context under {ev['budget_tokens']}-token budget",
        f"LRU {lru['tokens_before']}->{lru['tokens_after']} (fits={lru['fits_budget']}); "
        f"+summarize {sm['pages_before']}->{sm['pages_after']} pages",
    )
    console.print(t)
    if pg["attacks_missed"]:
        console.print(
            f"[dim]Gate misses (by design, caught downstream by Docker): "
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
