"""Sanity tests for the offline evaluation harness (gcos.eval).

These assert the harness runs end-to-end and each metric stays in a sane range.
They are deliberately loose on the timing-dependent speedup (CI jitter) but
strict on the deterministic metrics (ordering, gate, eviction).
"""

from __future__ import annotations

from gcos.eval import (
    measure_concurrency_speedup,
    measure_eviction_efficacy,
    measure_policy_gate_detection,
    measure_scheduler_ordering,
    run_all,
)


def test_concurrency_speedup_beats_serial():
    r = measure_concurrency_speedup(n_agents=6, per_call_s=0.03, workers=4)
    assert r["all_completed"] is True
    # Parallel must be strictly faster than serial; speedup well above 1.
    assert r["parallel_wall_s"] < r["serial_wall_s"]
    assert r["speedup_x"] > 1.5


def test_priority_scheduler_orders_by_priority():
    r = measure_scheduler_ordering(priorities=(5, 1, 9, 3, 7))
    assert r["all_completed"] is True
    assert r["correct"] is True
    assert r["observed_order"] == [9, 7, 5, 3, 1]


def test_policy_gate_detection_high_recall_no_false_alarms():
    r = measure_policy_gate_detection()
    # Deterministic: the gate catches the common patterns and never blocks
    # benign input. Known evasions (os.popen / os.remove) are caught downstream.
    assert r["detection_rate_pct"] >= 80.0
    assert r["false_positive_rate_pct"] == 0.0
    assert r["precision_pct"] == 100.0


def test_eviction_brings_context_under_budget():
    r = measure_eviction_efficacy(budget=200, n_pages=20, tokens_per_page=30)
    for trial in (r["lru_only"], r["lru_then_summarize"]):
        assert trial["fits_budget"] is True
        assert trial["tokens_after"] <= r["budget_tokens"]
        assert trial["reduction_pct"] > 0.0


def test_run_all_returns_every_metric():
    r = run_all()
    assert set(r) == {
        "concurrency_speedup",
        "scheduler_ordering",
        "policy_gate_detection",
        "eviction_efficacy",
    }
