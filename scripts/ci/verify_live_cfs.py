#!/usr/bin/env python3
"""CI gate: the LIVE dispatch path (process executor) runs agents as real processes.

Drives CPU-bound agents at priorities 1/5/9 through the process-backed executor —
each agent a real OS process placed in its own per-agent cgroup with
cpu.weight=priority — and asserts the live path dispatches and runs them ALL to
completion as separate processes.

Scope (honest): that cgroup cpu.weight actually steers the CFS share is proven
robustly + reproducibly by the cgroup CFS-share gate in verify_kernel_enforcement.py
(weights 100/300/900 -> ~8/23/68%, top >3x bottom). The *contended* per-agent share
in this live path is environment-noisy (CPU-budget dependent; and under scx_gcos the
scheduler doesn't read cgroup weight), so we don't gate on its ordering — we verify
the part that is robust here: the real executor runs every agent. Needs Linux +
cgroup v2 delegation + runtime deps (imports the Kernel). Exits non-zero on failure.

    docker run --rm --privileged --cgroupns=host -v "$PWD":/app -w /app \
      python:3.11-slim bash -c 'pip install -q -e . && python3 scripts/ci/verify_live_cfs.py'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gcos.eval import measure_live_per_agent_cfs


def main() -> int:
    r = measure_live_per_agent_cfs(priorities=(1, 5, 9))
    print(r)
    assert r.get("enforced"), f"expected cgroup-enforced CFS here: {r.get('reason')}"
    # Robust criterion: the live executor dispatched and ran EVERY agent as a real
    # OS process in its own cpu.weight cgroup (per-priority share is the cgroup
    # CFS-share gate's job — see verify_kernel_enforcement.py).
    assert r["all_agents_ran"], \
        f"live executor did not run all agents: {r['agents_ran']}/{len(r['priorities'])} ({r})"
    print("OK: live process-executor path ran all agents as real processes in "
          "per-agent cpu.weight cgroups.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
