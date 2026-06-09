"""gcos.osprims.ebpf — load GCOS's own BPF program into the kernel.

`gcos_sched.bpf.c` is real ring-0 code we wrote; this loader attaches it via bcc
and reads back per-PID scheduling data the *kernel* collected. It is the
hybrid-strategy capstone: a Python project that nonetheless ships and runs its
own kernel scheduler-observability code.

Honest scope: this needs Linux + bcc/libbpf + privilege (CAP_BPF/root). When any
of that is missing — including this macOS dev box and most CI without
`--privileged` — `available()` returns False and `SchedObserver` raises
`EbpfUnavailable`, with the reason. We never pretend it loaded. Verify on a
Linux host with bcc installed (see docs/REAL_OS.md for the one-liner), not here.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_BPF_SRC = os.path.join(os.path.dirname(__file__), "gcos_sched.bpf.c")


class EbpfUnavailable(RuntimeError):
    pass


_TRACEFS_FORMATS = (
    "/sys/kernel/debug/tracing/events/sched/sched_switch/format",
    "/sys/kernel/tracing/events/sched/sched_switch/format",
)


def _tracefs_ready() -> bool:
    return any(os.path.exists(p) for p in _TRACEFS_FORMATS)


def _ensure_tracefs() -> bool:
    """bcc generates the TRACEPOINT_PROBE args struct by reading the tracepoint
    format from tracefs/debugfs; without it the BPF program fails to COMPILE
    ("incomplete definition of struct tracepoint__sched__sched_switch"). Mount it
    best-effort (we already require root to load BPF) so loading just works."""
    if _tracefs_ready():
        return True
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        for src, tgt, fs in ((b"debugfs", "/sys/kernel/debug", b"debugfs"),
                             (b"tracefs", "/sys/kernel/tracing", b"tracefs")):
            try:
                os.makedirs(tgt, exist_ok=True)
                libc.mount(src, tgt.encode(), fs, 0, None)
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — best-effort only
        pass
    return _tracefs_ready()


def unavailable_reason() -> str | None:
    """Return why eBPF can't load here, or None if it can be attempted."""
    if not sys.platform.startswith("linux"):
        return f"not Linux (platform={sys.platform}); eBPF is Linux-only"
    try:
        import bcc  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return "bcc/libbpf python binding not installed (pip install bcc / apt bpfcc-tools)"
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return "not privileged (loading BPF needs root / CAP_BPF+CAP_PERFMON)"
    if not _ensure_tracefs():
        return ("tracefs/debugfs not mounted (bcc needs the sched_switch format); "
                "mount -t debugfs none /sys/kernel/debug")
    import platform
    kver = platform.release()
    if not (os.path.exists(f"/lib/modules/{kver}/build")
            or os.path.exists("/usr/src/linux-headers-" + kver)):
        return (f"kernel headers for {kver} not found (bcc compiles the BPF "
                f"program against them); install linux-headers-{kver}")
    return None


def available() -> bool:
    return unavailable_reason() is None


def bpf_source() -> str:
    with open(_BPF_SRC, "r", encoding="utf-8") as fh:
        return fh.read()


class SchedObserver:
    """Attach gcos_sched.bpf.c and read kernel-measured per-PID scheduling stats.

        with SchedObserver() as obs:
            ... run workload ...
            stats = obs.snapshot([pid1, pid2])   # {pid: {switches, oncpu_ns}}
    """

    def __init__(self) -> None:
        reason = unavailable_reason()
        if reason is not None:
            raise EbpfUnavailable(reason)
        from bcc import BPF  # type: ignore
        try:
            self._bpf = BPF(text=bpf_source())  # compiles + loads + auto-attaches
        except Exception as e:  # noqa: BLE001 — turn the bare bcc error into a clear one
            raise EbpfUnavailable(
                f"bcc failed to compile/load gcos_sched.bpf.c ({e}); usually "
                "missing kernel headers or tracefs — see unavailable_reason()"
            ) from e
        log.info("ebpf: loaded gcos_sched.bpf.c into the kernel")

    def snapshot(self, pids: list[int] | None = None) -> dict[int, dict]:
        switches = self._bpf["switches"]
        oncpu = self._bpf["oncpu_ns"]
        out: dict[int, dict] = {}
        for k, v in switches.items():
            pid = int(k.value)
            if pids is not None and pid not in pids:
                continue
            out[pid] = {"switches": int(v.value), "oncpu_ns": 0}
        for k, v in oncpu.items():
            pid = int(k.value)
            if pids is not None and pid not in pids:
                continue
            out.setdefault(pid, {"switches": 0, "oncpu_ns": 0})
            out[pid]["oncpu_ns"] = int(v.value)
        return out

    def close(self) -> None:
        try:
            self._bpf.cleanup()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "SchedObserver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["SchedObserver", "EbpfUnavailable", "available", "unavailable_reason", "bpf_source"]
