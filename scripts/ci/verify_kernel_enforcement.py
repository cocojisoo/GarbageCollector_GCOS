#!/usr/bin/env python3
"""CI gate: assert the Linux kernel ACTUALLY enforces GCOS's OS claims.

Run inside a privileged `--cgroupns=host` container on Linux (see
.github/workflows/ci.yml). Exits non-zero on any failure, so a regression in the
kernel-enforcement path fails the build — the claim is verified by execution, not
asserted. Runnable locally too:

    docker run --rm --privileged --cgroupns=host -v "$PWD":/app -w /app \
      python:3.11-slim python3 scripts/ci/verify_kernel_enforcement.py
"""
import os
import sys

# Running a script file puts its own dir on sys.path, not the repo root, so make
# `gcos` importable regardless of how this is invoked (CI / local / any cwd).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gcos.osprims import os_caps
from gcos.osprims import cgroup as cg
from gcos.osprims.realproc import RealProcessScheduler, block_count


def main() -> int:
    caps = os_caps()
    print("caps:", caps.to_dict())
    assert caps.kernel_enforced, "expected kernel enforcement on Linux"
    assert cg.available(), "cgroup v2 must be writable in a privileged container"

    sched = RealProcessScheduler()
    # Real preemption: jitter-proof block-count invariant — FCFS keeps each child
    # as one contiguous block (convoy); RR interleaves into more blocks.
    rr = sched.rr_order(3, 6, 0.01, preempt=True)
    fcfs = sched.rr_order(3, 6, 0.01, preempt=False)
    assert block_count(fcfs) == 3 and block_count(rr) > 3, f"rr={rr} fcfs={fcfs}"
    print(f"real SIGSTOP/SIGCONT preemption: fcfs blocks={block_count(fcfs)} "
          f"rr blocks={block_count(rr)}")

    # cgroup v2 CFS proportional share tracks cpu.weight.
    share = sched.cpu_share([100, 300, 900], duration_s=0.8)
    assert share is not None, "cgroup cpu.weight measurement failed"
    m = share["measured_share_pct"]
    assert m == sorted(m) and m[-1] > m[0] * 3, f"share not tracking weight: {m}"
    print(f"cgroup CFS share tracks cpu.weight 100/300/900 -> {m}%")

    # Daemon-cgroup placement: the OS's own resource budget is kernel-enforced.
    dg = cg.place_daemon(name="ci-daemon", pids_max=256)
    assert dg is not None and dg.enforced, "daemon cgroup placement failed"
    assert str(os.getpid()) in open(dg.path + "/cgroup.procs").read().split()
    print(f"daemon cgroup: {dg.path} pids.max="
          f"{open(dg.path + '/pids.max').read().strip()} (this pid placed)")

    print("OK: Linux kernel enforces GCOS resource control.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
