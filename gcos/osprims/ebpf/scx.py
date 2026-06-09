"""Probe + pointer for GCOS's own sched_ext CPU scheduler (scx_gcos.bpf.c).

scx_gcos is a real in-kernel CPU scheduler that GCOS authored against the
sched_ext (scx) API. It **does** load and arbitrate the CPU — the CI `scx-ext`
job builds it (bpftool + libbpf-from-source + scx v1.1.0 headers) and loads it via
the libbpf struct_ops loader in `scripts/scx/loader.c`, then confirms
`/sys/kernel/sched_ext/state` reads `enabled` with `ops: gcos`, i.e. our scheduler
is the one dispatching every task. See `docs/REAL_OS.md` §5.

What it needs: Linux >= 6.12 with CONFIG_SCHED_CLASS_EXT, plus the scx/libbpf
build toolchain. A plain dev box (e.g. colima 6.8) has neither, so on such a host
`available()` returns False with the reason. The actual load is driven by the
compiled C loader (`scripts/scx/scx_gcos`), not from Python — this module is the
honest *probe* (can this host run it?) and a pointer to the build+load path
(`scripts/scx/run.sh`, or `setup_vm.sh` to boot a 6.12+ VM on macOS).

Contrast: the bcc observability program (`gcos.osprims.ebpf.SchedObserver`) is
loaded directly from Python and is also CI-verified; scx_gcos is the heavier
struct_ops CPU scheduler, loaded by its C loader.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "scx_gcos.bpf.c")
_LOADER = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "scx")
)
_MIN_KERNEL = (6, 12)


def kernel_version() -> tuple[int, int] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        rel = os.uname().release  # e.g. "6.17.0-1015-azure"
        maj, minor = rel.split(".")[:2]
        return (int(maj), int(minor))
    except (ValueError, AttributeError, OSError):
        return None


def sched_ext_present() -> bool:
    return os.path.exists("/sys/kernel/sched_ext")


def unavailable_reason() -> str | None:
    """Why scx_gcos can't be loaded on *this* host, or None if it can."""
    if not sys.platform.startswith("linux"):
        return f"not Linux (platform={sys.platform}); sched_ext is Linux-only"
    kv = kernel_version()
    if kv is None:
        return "could not read kernel version"
    if kv < _MIN_KERNEL:
        return (f"sched_ext needs kernel >= {_MIN_KERNEL[0]}.{_MIN_KERNEL[1]}; "
                f"this host is {kv[0]}.{kv[1]}")
    if not sched_ext_present():
        return ("kernel >= 6.12 but CONFIG_SCHED_CLASS_EXT not enabled "
                "(/sys/kernel/sched_ext absent)")
    return None


def available() -> bool:
    """True when this host can build + load scx_gcos (Linux >= 6.12 + sched_ext).
    The load itself is driven by the C loader in scripts/scx (see load())."""
    return unavailable_reason() is None


def source_path() -> str:
    return _SRC


def load():
    """Point at the build+load path. scx_gcos is loaded by its compiled C loader
    (a struct_ops scheduler can't be attached from Python), not from here."""
    reason = unavailable_reason()
    if reason is not None:
        raise RuntimeError(
            f"scx_gcos cannot load on this host: {reason}. On macOS, boot a 6.12+ "
            f"VM first with {_LOADER}/setup_vm.sh."
        )
    raise RuntimeError(
        "scx_gcos is loaded by its compiled C loader, not from Python. Run "
        f"`make && sudo ./scx_gcos` in {_LOADER} (or {_LOADER}/run.sh). The CI "
        "`scx-ext` job does exactly this and verifies /sys/kernel/sched_ext/state."
    )


__all__ = ["available", "unavailable_reason", "kernel_version",
           "sched_ext_present", "source_path", "load"]
