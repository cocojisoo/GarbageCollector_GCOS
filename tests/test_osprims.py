"""Tests for the real-OS substrate (gcos.osprims).

Signals, mmap paging, and POSIX shm work on any POSIX host, so those are tested
unconditionally (incl. macOS / CI). cgroup v2, seccomp, and eBPF are Linux-only;
here we assert the *degrade* contract (honest no-op + reason) and rely on the
ubuntu CI job to exercise the enforcing path on real Linux.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from gcos.osprims import os_caps, OSCapabilities, caps_info


# --- capability detection ---------------------------------------------------

def test_os_caps_is_honest_about_this_host():
    caps = os_caps()
    assert isinstance(caps, OSCapabilities)
    # Signals/mmap/shm are POSIX and must be present wherever the suite runs.
    if os.name == "posix":
        assert caps.signals and caps.mmap_madvise and caps.posix_shm
    # Linux-only primitives must never report True off Linux.
    if not sys.platform.startswith("linux"):
        assert not caps.cgroup_v2 and not caps.seccomp and not caps.ebpf
    # kernel_enforced ⇔ cgroup delegation + signals.
    assert caps.kernel_enforced == (caps.cgroup_writable and caps.signals)


def test_caps_info_has_kernel_enforced_flag():
    info = caps_info(refresh=True)
    assert "kernel_enforced" in info and "platform" in info


# --- vmem: real mmap demand paging ------------------------------------------

def test_mmap_page_store_page_out_and_fault_in():
    from gcos.osprims.vmem import MmapPageStore
    with MmapPageStore(capacity_pages=64) as st:
        st.store("a", b"hello" * 1000)
        st.store("b", b"world" * 1000)
        assert st.resident_pages() == 2
        assert st.page_out("a") is True
        assert st.resident_pages() == 1
        # Reading a paged-out page faults it back in from the backing file.
        assert st.read("a").startswith(b"hello")
        stats = st.fault_stats()
        assert stats["app_page_outs"] == 1
        assert stats["app_fault_ins"] == 1
        assert stats["page_size"] >= 4096


def test_mmap_page_store_capacity_guard():
    from gcos.osprims.vmem import MmapPageStore
    with MmapPageStore(capacity_pages=1) as st:
        with pytest.raises(MemoryError):
            st.store("big", b"x" * (st.capacity_bytes + 1))


def test_mmap_page_store_cleans_up_on_construction_failure(monkeypatch):
    """Regression: if mmap fails during __init__, the fd and owned temp file
    must not leak (close()/__exit__ never run when __init__ raises)."""
    import glob
    import tempfile
    from gcos.osprims import vmem
    pattern = tempfile.gettempdir() + "/gcos-vmem-*.swap"
    before = set(glob.glob(pattern))
    monkeypatch.setattr(vmem.mmap, "mmap",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        vmem.MmapPageStore(capacity_pages=16)
    assert set(glob.glob(pattern)) == before  # no leaked backing file


# --- shm: real POSIX shared memory IPC --------------------------------------

def test_shm_ring_roundtrip_and_framing():
    from gcos.osprims.shm import ShmRing
    with ShmRing(capacity=4096) as ring:
        assert ring.send(b"first")
        assert ring.send(b"second")
        assert ring.recv() == b"first"
        assert ring.recv() == b"second"
        assert ring.recv() is None  # empty


def test_shm_ring_backpressure_when_full():
    from gcos.osprims.shm import ShmRing
    with ShmRing(capacity=64) as ring:
        # Fill until it refuses (returns False) — no overwrite/backpressure.
        refused = False
        for _ in range(100):
            if not ring.send(b"x" * 16):
                refused = True
                break
        assert refused


def test_shm_consumer_does_not_unlink_producers_segment():
    """Regression: a consumer attaching create=False must NOT unlink the
    producer's still-live segment when it exits (resource_tracker bug)."""
    import subprocess
    import sys
    import textwrap
    from gcos.osprims.shm import ShmRing
    with ShmRing(capacity=4096) as ring:
        ring.send(b"payload")
        child = textwrap.dedent(f"""
            from gcos.osprims.shm import ShmRing
            r = ShmRing(name={ring.name!r}, create=False)
            assert r.recv() == b"payload"
            r.close()
        """)
        proc = subprocess.run([sys.executable, "-c", child],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        # The consumer exited; our segment must still be alive and usable...
        assert ring.send(b"again")
        assert ring.recv() == b"again"
        # ...and the consumer must not have warned about a "leaked" object.
        assert "leaked shared_memory" not in proc.stderr


# --- preempt: real SIGSTOP/SIGCONT/SIGKILL ----------------------------------

def test_preemptor_stop_cont_kill_real_child():
    import warnings
    from gcos.osprims.preempt import Preemptor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # see realproc._fork
        pid = os.fork()
    if pid == 0:  # child: just sleep, parent drives it
        time.sleep(30)
        os._exit(0)
    p = Preemptor(pid)
    assert p.alive
    assert p.stop() and p.stopped
    assert p.cont() and not p.stopped
    assert p.kill()
    assert not p.alive
    os.waitpid(pid, 0)  # reap


# --- realproc: real preemptive scheduling of real processes -----------------

def test_real_rr_preempts_but_fcfs_runs_to_completion():
    from gcos.osprims.realproc import (
        RealProcessScheduler, max_consecutive_run, block_count,
    )
    sched = RealProcessScheduler()
    rr = sched.rr_order(3, chunks=6, quantum_s=0.01, preempt=True)
    fcfs = sched.rr_order(3, chunks=6, quantum_s=0.01, preempt=False)
    # Both run all three real child processes to completion...
    assert set(rr) == {0, 1, 2} and set(fcfs) == {0, 1, 2}
    # ...but FCFS keeps each child as ONE contiguous block (convoy: 3 children →
    # 3 blocks) while RR interleaves them into MORE blocks than there are
    # children (real kernel preemption via signals). This is jitter-proof, unlike
    # asserting RR's max-run == 1 (the last child legitimately runs alone at the
    # tail, so a contended CPU can show 2-3).
    assert block_count(fcfs) == 3
    assert block_count(rr) > 3
    assert max_consecutive_run(rr) < max_consecutive_run(fcfs)


def test_rr_order_completes_all_children_even_at_small_quantum():
    """Regression: a tiny quantum used to exhaust a chunks-based guard and
    silently drop whole processes; the deadline-based loop must still dispatch
    every child to completion."""
    from gcos.osprims.realproc import RealProcessScheduler
    fcfs = RealProcessScheduler().rr_order(3, chunks=8, quantum_s=0.001, preempt=False)
    assert set(fcfs) == {0, 1, 2}


# --- cgroup / seccomp / ebpf: degrade contract off Linux --------------------

def test_cgroup_degrades_honestly_when_unavailable():
    from gcos.osprims import cgroup as cg
    if cg.available():
        pytest.skip("cgroup v2 is enforceable here; degrade path not applicable")
    grp = cg.Cgroup("test-degrade", weight=500)
    assert grp.enforced is False
    assert grp.add_pid(os.getpid()) is False
    assert grp.cpu_usage_us() is None
    assert grp.set_weight(100) is False


def test_cgroup_priority_to_weight_is_monotonic():
    from gcos.osprims.cgroup import priority_to_weight
    weights = [priority_to_weight(p) for p in range(10)]
    assert weights == sorted(weights)              # higher prio → higher weight
    assert all(10 <= w <= 10000 for w in weights)  # valid cgroup v2 range


def test_priority_to_cpu_shares_is_monotonic_and_valid():
    from gcos.osprims.cgroup import priority_to_cpu_shares
    shares = [priority_to_cpu_shares(p) for p in range(10)]
    assert shares == sorted(shares)         # higher prio → larger CFS share
    assert all(s >= 2 for s in shares)      # Docker cpu_shares minimum is 2
    assert priority_to_cpu_shares(9) > priority_to_cpu_shares(0)


def test_place_daemon_degrades_when_cgroup_unavailable():
    from gcos.osprims import cgroup as cg
    if cg.available():
        pytest.skip("cgroup enforceable here; the enforce path is covered in CI")
    # Off Linux / without delegation, placing the daemon is a safe no-op (None),
    # never a crash — the kernel-enforcement claim degrades honestly.
    assert cg.place_daemon(pids_max=512) is None


def test_cpu_share_returns_none_without_cgroup():
    from gcos.osprims import cgroup as cg
    from gcos.osprims.realproc import RealProcessScheduler
    if cg.available():
        pytest.skip("cgroup enforceable here; covered by the Linux path / CI")
    assert RealProcessScheduler().cpu_share([100, 800], duration_s=0.1) is None


def test_seccomp_profile_denies_dangerous_syscalls():
    from gcos.osprims import seccomp
    prof = seccomp.docker_seccomp_profile()
    assert prof["defaultAction"] == "SCMP_ACT_ALLOW"
    rule = prof["syscalls"][0]
    assert rule["action"] == "SCMP_ACT_ERRNO"
    for must_block in ("socket", "ptrace", "mount", "bpf"):
        assert must_block in rule["names"]
    assert seccomp.supported() == sys.platform.startswith("linux")


def test_scx_scheduler_probe_is_honest_and_loads_only_via_c_loader():
    """scx_gcos is GCOS's own sched_ext CPU scheduler — it loads + arbitrates the
    CPU (verified by the CI `scx-ext` job). The Python module is the honest probe:
    available() tracks the host, and the actual attach is the compiled C loader's
    job, so Python's load() always points there rather than claiming to attach."""
    from gcos.osprims.ebpf import scx
    # available() honestly reflects the host (needs Linux >= 6.12 + sched_ext).
    reason = scx.unavailable_reason()
    if scx.available():
        assert reason is None                   # this host can build + load it
    else:
        assert isinstance(reason, str) and reason
        if not sys.platform.startswith("linux"):
            assert "Linux" in reason
    # The scheduler source ships and uses the real scx (sched_ext) API.
    src = open(scx.source_path()).read()
    assert "sched_ext_ops" in src and "scx_bpf_dsq_insert" in src
    # A struct_ops scheduler can't be attached from Python — load() always points
    # at the C loader (raises with that guidance), it never fakes a Python attach.
    with pytest.raises(RuntimeError):
        scx.load()


def test_ebpf_loader_reports_unavailable_honestly_off_linux():
    from gcos.osprims import ebpf
    assert "sched_switch" in ebpf.bpf_source()  # our real BPF program ships
    if not sys.platform.startswith("linux"):
        assert ebpf.available() is False
        assert "Linux" in (ebpf.unavailable_reason() or "")
        with pytest.raises(ebpf.EbpfUnavailable):
            ebpf.SchedObserver()
