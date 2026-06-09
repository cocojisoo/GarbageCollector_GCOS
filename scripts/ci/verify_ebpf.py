#!/usr/bin/env python3
"""CI gate: compile + load GCOS's OWN eBPF program into the kernel and read back
real per-PID sched_switch data.

This is what makes "we ship our own ring-0 code" verified by EXECUTION rather than
by construction. Needs Linux + bcc + kernel headers + privilege; the loader
auto-mounts tracefs. Exits non-zero on failure. See .github/workflows/ci.yml.
"""
import os
import sys
import time

# Make `gcos` importable when run as a script file from any cwd (CI / local).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gcos.osprims.ebpf import available, unavailable_reason, SchedObserver


def main() -> int:
    print("available:", available(), "| reason:", unavailable_reason())
    assert available(), f"our eBPF should be loadable here: {unavailable_reason()}"
    with SchedObserver() as obs:
        deadline = time.time() + 0.5
        while time.time() < deadline:
            os.sched_yield()  # generate context switches for the BPF to count
        snap = obs.snapshot()
    total = sum(v["switches"] for v in snap.values())
    print(f"our ring-0 eBPF ran: pids={len(snap)} switches={total}")
    assert len(snap) > 0 and total > 0, "BPF collected no sched_switch data"
    print("OK: GCOS-authored eBPF loaded and collected real kernel data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
