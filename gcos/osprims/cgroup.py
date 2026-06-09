"""cgroup.py — resource control via real cgroup v2.

This is the module that makes GCOS's "quota" and "priority" stop being a Python
semaphore and a sort key, and become **kernel-enforced** resource control:

  - `cpu.weight`   <- agent priority   (real Linux CFS proportional share)
  - `cpu.max`      <- CPU quota         (hard cap: the kernel throttles the agent)
  - `memory.max`   <- memory quota      (the kernel OOM-kills past this)
  - `pids.max`     <- fork limit        (anti fork-bomb)

We create a child cgroup per agent under a GCOS root, move the agent's real PID
into it, and read `cpu.stat` back to *measure* the CPU share the kernel actually
gave it — that's the `cgroup_cpu_share` eval metric (replicating, reproducibly,
the kind of CFS-share benchmark the top kernel teams hand-measured).

Linux-only. On any other host (or without cgroup delegation) every method is a
loud no-op returning `enforced=False`, and the caller falls back to the
in-process simulation — see gcos.osprims.warn_if_degraded / docs/REAL_OS.md.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from gcos.osprims import os_caps


log = logging.getLogger(__name__)

CGROUP_ROOT = "/sys/fs/cgroup"
GCOS_ROOT = os.path.join(CGROUP_ROOT, "gcos")

# cgroup v2 cpu.weight is 1..10000, default 100. Map GCOS priority 0..9 onto a
# wide, monotonic band so a prio-9 agent gets a markedly larger CFS share than a
# prio-0 one (and the measured share tracks the weight, like a real CFS bench).
_WEIGHT_MIN, _WEIGHT_MAX = 10, 10000


def priority_to_weight(priority: int) -> int:
    p = max(0, min(9, int(priority)))
    return int(_WEIGHT_MIN + (_WEIGHT_MAX - _WEIGHT_MIN) * (p / 9.0))


# Docker --cpu-shares (cgroup cpu.weight equivalent the daemon maps): default 1024.
# Map GCOS priority 0..9 onto a band so a higher-priority agent's sandboxed code
# gets a proportionally larger CFS share when containers compete (real, since each
# sandbox is its own process the kernel schedules — unlike GIL-bound threads).
def priority_to_cpu_shares(priority: int) -> int:
    p = max(0, min(9, int(priority)))
    return 256 * (p + 1)  # 256 (prio 0) .. 2560 (prio 9)


def available() -> bool:
    return os_caps().cgroup_writable


def place_daemon(*, name: str = "daemon", pids_max: Optional[int] = None,
                 memory_max: Optional[int] = None, cpu_max: Optional[str] = None
                 ) -> Optional["Cgroup"]:
    """Move THIS process (the GCOS daemon) into a cgroup with kernel-enforced
    limits — the OS's own resource budget, enforced by the kernel rather than a
    Python counter. Safe by default: only the limits you pass are set (a runaway
    memory_max could OOM-kill the daemon, so it's opt-in). Returns the Cgroup, or
    None when cgroup v2 isn't enforceable here (non-Linux / no delegation)."""
    if not available():
        return None
    g = Cgroup(name, pids_max=pids_max, mem_max=memory_max, cpu_max=cpu_max)
    if not g.enforced:
        return None
    g.add_pid(os.getpid())  # the whole daemon + its worker threads are now the kernel's to account/limit
    return g


def _write(path: str, value: str) -> bool:
    try:
        with open(path, "w") as fh:
            fh.write(value)
        return True
    except (OSError, PermissionError) as e:
        log.debug("cgroup: write %s=%r failed: %s", path, value, e)
        return False


def _ensure_root() -> bool:
    if not available():
        return False
    try:
        os.makedirs(GCOS_ROOT, exist_ok=True)
    except OSError as e:
        log.debug("cgroup: cannot create gcos root: %s", e)
        return False
    # Delegate cpu/memory/pids controllers down to children (best-effort; some
    # may already be enabled or be unavailable, which is fine).
    subtree = os.path.join(GCOS_ROOT, "cgroup.subtree_control")
    for ctrl in ("+cpu", "+memory", "+pids"):
        _write(subtree, ctrl)
    return True


class Cgroup:
    """A child cgroup for one agent (or a benchmark cohort). Context-managed:
    cleans up the directory on exit. `enforced` is False when we degraded."""

    def __init__(self, name: str, *, weight: Optional[int] = None,
                 cpu_max: Optional[str] = None, mem_max: Optional[int] = None,
                 pids_max: Optional[int] = None) -> None:
        self.name = name
        self.path = os.path.join(GCOS_ROOT, name)
        self.enforced = False
        # `enforced` = the cgroup dir exists; `cpu_enforced` = the cpu.weight knob
        # actually wrote (the cpu controller could be absent even when the dir
        # exists, so directory creation alone must NOT imply CFS weighting).
        self.cpu_enforced = False
        if not _ensure_root():
            log.debug("cgroup(%s): not enforced (no cgroup v2 delegation)", name)
            return
        try:
            os.makedirs(self.path, exist_ok=True)
            self.enforced = True
        except OSError as e:
            log.debug("cgroup(%s): mkdir failed: %s", name, e)
            return
        if weight is not None:
            self.cpu_enforced = self.set_weight(weight)
        if cpu_max is not None:
            _write(os.path.join(self.path, "cpu.max"), cpu_max)
        if mem_max is not None:
            _write(os.path.join(self.path, "memory.max"), str(mem_max))
        if pids_max is not None:
            _write(os.path.join(self.path, "pids.max"), str(pids_max))

    # --- knobs -------------------------------------------------------------

    def set_weight(self, weight: int) -> bool:
        """Set cpu.weight (1..10000) — the agent's proportional CFS share."""
        if not self.enforced:
            return False
        return _write(os.path.join(self.path, "cpu.weight"), str(int(weight)))

    def set_weight_from_priority(self, priority: int) -> bool:
        return self.set_weight(priority_to_weight(priority))

    def add_pid(self, pid: int) -> bool:
        """Move a real process into this cgroup (it and its CPU/mem are now the
        kernel's to account and limit)."""
        if not self.enforced:
            return False
        return _write(os.path.join(self.path, "cgroup.procs"), str(pid))

    def add_self(self) -> bool:
        """Move the calling process into this cgroup. Used by a forked agent
        child to place itself under its per-agent CFS weight before running."""
        return self.add_pid(os.getpid())

    # --- measurement -------------------------------------------------------

    def cpu_usage_us(self) -> Optional[int]:
        """usage_usec from cpu.stat — the CPU time the kernel charged this group.
        This is how we measure the *actual* CFS share, not a simulated one."""
        if not self.enforced:
            return None
        try:
            with open(os.path.join(self.path, "cpu.stat")) as fh:
                for line in fh:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except OSError:
            return None
        return None

    def memory_current(self) -> Optional[int]:
        if not self.enforced:
            return None
        try:
            with open(os.path.join(self.path, "memory.current")) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    # --- lifecycle ---------------------------------------------------------

    def remove(self) -> None:
        # A cgroup directory only rmdir's once empty (all procs moved/exited).
        if not self.enforced:
            return
        try:
            os.rmdir(self.path)
        except OSError as e:
            log.debug("cgroup(%s): rmdir deferred: %s", self.name, e)

    def __enter__(self) -> "Cgroup":
        return self

    def __exit__(self, *exc) -> None:
        self.remove()
