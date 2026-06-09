"""Sanity tests for the offline evaluation harness (gcos.eval).

These assert the harness runs end-to-end and each metric stays in a sane range.
They are deliberately loose on the timing-dependent speedup (CI jitter) but
strict on the deterministic metrics (ordering, gate, eviction).
"""

from __future__ import annotations

from gcos.eval import (
    measure_concurrency_speedup,
    measure_concurrency_speedup_stats,
    measure_eviction_efficacy,
    measure_policy_gate_detection,
    measure_priority_no_double_dispatch,
    measure_quota_conservation,
    measure_scheduler_ordering,
    measure_scheduler_preemption,
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
    # benign input. A couple of evasions (aliasing / reflection) are missed on
    # purpose — that's the honest "pre-filter, not a boundary" evidence.
    assert r["detection_rate_pct"] >= 80.0
    assert r["recall_pct"] == r["detection_rate_pct"]
    assert r["false_positive_rate_pct"] == 0.0
    assert r["precision_pct"] == 100.0
    assert r["attacks_missed"] >= 1            # honest blind spots present
    assert "not a security" in r["note"]


def test_scheduler_preemption_fcfs_convoy_vs_rr_rotation():
    r = measure_scheduler_preemption(n_agents=3, steps_each=4, rr_quantum=2)
    assert r["fcfs"]["completed"] and r["rr"]["completed"]
    # FCFS runs each agent to completion; RR caps consecutive runs at the quantum.
    assert r["fcfs"]["max_consecutive_run"] == 4
    assert r["rr"]["max_consecutive_run"] == 2
    assert r["rr_preempts"] is True
    # RR gets later agents their first slice sooner (less convoy waiting).
    assert r["rr_improves_fairness"] is True
    assert r["rr"]["mean_time_to_first_slice"] < r["fcfs"]["mean_time_to_first_slice"]


def test_priority_no_double_dispatch_multi_worker():
    r = measure_priority_no_double_dispatch(n_agents=30, workers=8)
    assert r["all_completed"] is True
    assert r["each_run_exactly_once"] is True
    assert r["max_runs_per_agent"] == 1
    assert r["distinct_agents_run"] == 30


def test_quota_conservation_no_leak_on_no_call_exits():
    r = measure_quota_conservation(n_ok=6, n_denied=6)
    assert r["all_completed"] is True
    assert r["quota_used"] == r["real_calls"] == 6
    assert r["conserved"] is True


def test_concurrency_speedup_stats_reports_spread():
    r = measure_concurrency_speedup_stats(repeats=3, n_agents=6, per_call_s=0.02, workers=4)
    assert r["all_completed"] is True
    assert len(r["speedups"]) == 3
    assert r["mean_speedup_x"] > 1.0
    assert r["std_speedup_x"] >= 0.0


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
        "concurrency_speedup_stats",
        "scheduler_ordering",
        "scheduler_preemption",
        "priority_no_double_dispatch",
        "quota_conservation",
        "policy_gate_detection",
        "eviction_efficacy",
        # real-OS substrate (gcos.osprims)
        "os_capabilities",
        "multistep_agents",
        "real_preemption",
        "demand_paging",
        "cgroup_cpu_share",
        "live_per_agent_cfs",
    }


def test_real_preemption_metric_rr_beats_fcfs():
    from gcos.eval import measure_real_preemption
    r = measure_real_preemption(n_procs=3, chunks=6, quantum_s=0.01)
    # Real child processes, real SIGSTOP/SIGCONT. Jitter-proof invariant: FCFS
    # keeps each child as one contiguous block (3 blocks), RR interleaves them
    # into more blocks than children.
    assert r["fcfs"]["blocks"] == 3
    assert r["rr"]["blocks"] > 3
    assert r["rr_preempts"] is True


def test_multistep_agents_metric_rr_interleaves_real_agents():
    from gcos.eval import measure_multistep_agents
    r = measure_multistep_agents()
    assert r["rr_interleaves"] is True
    assert r["fcfs_max_run"] == 3 and r["rr_max_run"] == 2


def test_demand_paging_metric_faults_pages_back_in():
    from gcos.eval import measure_demand_paging
    r = measure_demand_paging(n_pages=16, payload_bytes=8000)
    assert r["resident_after_pageout"] < r["resident_before"]
    assert r["fault_ins"] == 16
    assert r["demand_paging_works"] is True


def test_cgroup_cpu_share_metric_is_honest_about_enforcement():
    from gcos.eval import measure_cgroup_cpu_share
    from gcos.osprims import cgroup as cg
    r = measure_cgroup_cpu_share(weights=(100, 900), duration_s=0.3)
    if cg.available():
        assert r["enforced"] is True and "measured_share_pct" in r
    else:
        assert r["enforced"] is False and "reason" in r
