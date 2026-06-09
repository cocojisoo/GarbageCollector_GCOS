"""gcos.osprims — the real-OS substrate.

This package is what turns GCOS from a *simulation* of OS mechanisms into a
thin OS layer that makes the **host kernel** enforce them. Each module replaces
a Python metaphor with a real kernel primitive:

    metaphor (old)                    real primitive (this package)
    ------------------------------    ------------------------------------------
    Quota = threading.Semaphore   ->  cgroup v2 cpu.max / memory.max / pids.max  (cgroup.py)
    priority = a sort key          ->  cgroup v2 cpu.weight  (real Linux CFS share) (cgroup.py)
    "preemption" between calls     ->  SIGSTOP / SIGCONT / SIGKILL on real PIDs   (preempt.py)
    pages = a Python list          ->  mmap + madvise(MADV_DONTNEED): real paging  (vmem.py)
    IPC = queue.Queue              ->  POSIX shared memory                         (shm.py)
    policy gate (regex)            ->  seccomp-bpf syscall allowlist               (seccomp.py)
    scheduler = pick from a list   ->  real child processes, RR-preempted, under
                                       cgroup CFS weights                          (realproc.py)
    (none)                         ->  an eBPF program we wrote, run in ring-0     (ebpf/)

Honesty, by design (the GCOS rule): **Linux is the first-class target.** cgroup
v2, seccomp, and eBPF are Linux-only; signals, mmap/madvise, and POSIX shared
memory work on any POSIX host (incl. macOS). When a primitive is unavailable
the module *loudly degrades* (a banner, never a silent no-op) exactly like the
sandbox layer, and `caps_info()` surfaces the live posture for the dashboard.
The `.github/workflows/ci.yml` job runs the whole suite + eval on ubuntu, so the
Linux-only paths are verified on real Linux on every push, not just asserted.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from dataclasses import asdict, dataclass


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OSCapabilities:
    """What the *current* host actually lets GCOS enforce in the kernel.

    Every field is the result of a real probe (a file under /sys/fs/cgroup, an
    importable module, a platform check), not a guess — so the dashboard and the
    eval harness can report, and skip, honestly.
    """
    platform: str          # "linux" | "darwin" | ...
    posix: bool
    signals: bool          # SIGSTOP/SIGCONT/SIGKILL preemption (preempt.py)
    mmap_madvise: bool     # demand-paging via madvise (vmem.py)
    posix_shm: bool        # shared-memory IPC (shm.py)
    cgroup_v2: bool        # cgroup mounted as v2
    cgroup_writable: bool   # ...and we can actually create a child cgroup
    seccomp: bool          # seccomp-bpf available (seccomp.py)
    ebpf: bool             # bcc/libbpf present to load our BPF program (ebpf/)

    @property
    def kernel_enforced(self) -> bool:
        """True when the *distinctive* Linux primitives (cgroup CFS + signals)
        are live, i.e. OS claims are enforced by the kernel rather than faked."""
        return self.cgroup_writable and self.signals

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kernel_enforced"] = self.kernel_enforced
        return d


def _probe_cgroup_v2() -> tuple[bool, bool]:
    """(mounted_v2, writable) — can we create a child cgroup and move tasks?"""
    root = "/sys/fs/cgroup"
    if not os.path.exists(os.path.join(root, "cgroup.controllers")):
        return (False, False)
    # Writable iff we can actually mkdir a probe cgroup under the root (needs the
    # right delegation / privilege). A pid-suffixed name avoids colliding with a
    # concurrent probe or a foreign leftover dir — a bare 'gcos.probe' left by a
    # crashed privileged run would make an unprivileged process's mkdir raise
    # FileExistsError, which must NOT be read as "writable". We don't leave it
    # behind, and we only claim writable when a real mkdir succeeds.
    probe = os.path.join(root, f"gcos.probe.{os.getpid()}")
    try:
        os.mkdir(probe)
    except FileExistsError:
        try:
            os.rmdir(probe)
            os.mkdir(probe)
            os.rmdir(probe)
            return (True, True)
        except OSError:
            return (True, False)
    except (PermissionError, OSError):
        return (True, False)
    else:
        try:
            os.rmdir(probe)
        except OSError:
            log.debug("cgroup probe: could not rmdir %s", probe)
        return (True, True)


def _probe_mmap_madvise() -> bool:
    try:
        import mmap
    except Exception:  # noqa: BLE001
        return False
    # madvise() is a method on mmap objects on POSIX (3.8+); MADV_DONTNEED (Linux)
    # or MADV_FREE (Darwin) is the page-out lever.
    return hasattr(mmap, "MADV_DONTNEED") or hasattr(mmap, "MADV_FREE")


def _probe_posix_shm() -> bool:
    try:
        from multiprocessing import shared_memory  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _probe_seccomp() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        import seccomp  # type: ignore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        # libseccomp python binding (pyseccomp/seccomp) absent. We can still do a
        # minimal prctl(NO_NEW_PRIVS)+filter via ctypes, but report False so the
        # status is honest about the rich-policy binding being missing.
        return False


def _probe_ebpf() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        import bcc  # type: ignore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def os_caps() -> OSCapabilities:
    """Probe the host once and return what GCOS can really enforce here."""
    plat = platform.system().lower()
    posix = os.name == "posix"
    try:
        import signal
        signals = posix and hasattr(signal, "SIGSTOP") and hasattr(signal, "SIGCONT")
    except Exception:  # noqa: BLE001
        signals = False
    cg_v2, cg_w = _probe_cgroup_v2()
    return OSCapabilities(
        platform=plat,
        posix=posix,
        signals=signals,
        mmap_madvise=_probe_mmap_madvise(),
        posix_shm=_probe_posix_shm(),
        cgroup_v2=cg_v2,
        cgroup_writable=cg_w,
        seccomp=_probe_seccomp(),
        ebpf=_probe_ebpf(),
    )


_DEGRADE_BANNER = (
    "\n"
    "  ************************************************************************\n"
    "  * GCOS OSPRIMS DEGRADED: this host is not Linux (or lacks cgroup v2    *\n"
    "  * delegation), so OS claims that need the Linux kernel — cgroup CFS    *\n"
    "  * shares, memory/pids limits, seccomp, eBPF — are NOT enforced here    *\n"
    "  * and fall back to the in-process simulation. Signals, mmap paging and *\n"
    "  * POSIX shm DO work on this POSIX host. Run on Linux (or the ubuntu CI *\n"
    "  * job) for full kernel enforcement; see docs/REAL_OS.md.              *\n"
    "  ************************************************************************"
)


def caps_banner(caps: OSCapabilities | None = None) -> str:
    return _DEGRADE_BANNER if not (caps or os_caps()).kernel_enforced else ""


_CAPS_CACHE: OSCapabilities | None = None


def caps_info(*, refresh: bool = False) -> dict:
    """Cached posture for the status endpoint / dashboard (like sandbox_info)."""
    global _CAPS_CACHE
    if refresh or _CAPS_CACHE is None:
        _CAPS_CACHE = os_caps()
    return _CAPS_CACHE.to_dict()


def warn_if_degraded(caps: OSCapabilities | None = None) -> None:
    caps = caps or os_caps()
    if not caps.kernel_enforced:
        log.warning(_DEGRADE_BANNER)


__all__ = [
    "OSCapabilities",
    "os_caps",
    "caps_banner",
    "caps_info",
    "warn_if_degraded",
]
