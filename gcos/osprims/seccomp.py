"""seccomp.py — a kernel-enforced syscall boundary for the sandbox.

The policy gate is a regex over source text: bypassable, and (honestly) not
security. seccomp-bpf is the real thing — a BPF program the **kernel** runs on
every syscall the sandboxed process makes, returning an error (or killing it)
for disallowed calls. It cannot be bypassed by aliasing or reflection because it
acts on the syscall, not the source.

Two delivery paths, both real and Linux-only:

  1. `docker_seccomp_profile()` -> a Docker-compatible seccomp JSON that denies a
     curated set of dangerous syscalls (socket, ptrace, mount, kexec, ...). The
     sandbox passes it via `--security-opt seccomp=...`, so the kernel enforces
     it on the container. This is the path GCOS actually uses, and it's verified
     by a test that runs a `socket()` attempt inside the profiled container.
  2. `self_lockdown()` -> applies a filter to the *current* process via the
     libseccomp python binding when present (optional).

On non-Linux hosts these degrade: the profile is still emitted (so the wiring is
testable) but the kernel that enforces it is only present on Linux.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional


log = logging.getLogger(__name__)


# Syscalls a sandboxed, network-isolated code runner has no legitimate need for.
# Denying these with SCMP_ACT_ERRNO shrinks the kernel attack surface beyond what
# --network=none / --cap-drop already do, and unlike the regex gate it survives
# any source-level obfuscation.
DANGEROUS_SYSCALLS = [
    # networking (defence in depth behind --network=none)
    "socket", "socketcall", "connect", "bind", "listen", "accept", "accept4",
    "sendto", "recvfrom", "sendmsg", "recvmsg",
    # debugging / introspection of other tasks
    "ptrace", "process_vm_readv", "process_vm_writev",
    # mounting / namespaces / privilege
    "mount", "umount", "umount2", "pivot_root", "chroot",
    "setns", "unshare",
    # kernel module / reboot / kexec
    "init_module", "finit_module", "delete_module",
    "kexec_load", "kexec_file_load", "reboot",
    # raw device / io-uring (a well-known sandbox escape surface)
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    # bpf / perf
    "bpf", "perf_event_open",
]


def supported() -> bool:
    return sys.platform.startswith("linux")


def docker_seccomp_profile(default_action: str = "SCMP_ACT_ALLOW") -> dict:
    """A Docker/OCI seccomp profile: default-allow, but ERRNO on the dangerous
    set. Default-allow (rather than default-deny) keeps the Python interpreter
    working while still hard-blocking the calls that matter — a safe, verifiable
    tightening on top of Docker's own default profile."""
    return {
        "defaultAction": default_action,
        "archMap": [
            {"architecture": "SCMP_ARCH_X86_64",
             "subArchitectures": ["SCMP_ARCH_X86", "SCMP_ARCH_X32"]},
            {"architecture": "SCMP_ARCH_AARCH64",
             "subArchitectures": ["SCMP_ARCH_ARM"]},
        ],
        "syscalls": [
            {
                "names": list(DANGEROUS_SYSCALLS),
                "action": "SCMP_ACT_ERRNO",
                "errnoRet": 1,  # EPERM
            }
        ],
    }


def write_profile(path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(docker_seccomp_profile(), fh, indent=2)
    return path


def self_lockdown(allow: Optional[list[str]] = None) -> bool:
    """Apply a seccomp filter to *this* process via libseccomp (if installed).

    Default-allow with ERRNO on DANGEROUS_SYSCALLS, mirroring the Docker profile.
    Returns True if a real kernel filter was installed, False if degraded
    (non-Linux or no python binding). Irreversible once applied — a process can
    only ever tighten its own seccomp filter."""
    if not supported():
        return False
    try:
        import seccomp  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning("seccomp: libseccomp python binding absent; cannot self-lock "
                    "(use the Docker profile path instead)")
        return False
    f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for name in (allow or DANGEROUS_SYSCALLS):
        try:
            f.add_rule(seccomp.ERRNO(1), name)
        except Exception:  # noqa: BLE001 — unknown-on-arch syscalls are skippable
            continue
    f.load()
    log.info("seccomp: installed in-process filter (%d denied)", len(DANGEROUS_SYSCALLS))
    return True
