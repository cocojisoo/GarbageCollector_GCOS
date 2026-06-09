# GCOS — the real-OS substrate (`gcos.osprims`)

GCOS started as a *userspace simulation*: the scheduler picked from a Python
list, "quota" was a `threading.Semaphore`, "paging" was a list slice, and
"preemption" only happened between LLM calls. This document describes the layer
that makes those claims **enforced by the host kernel** instead of faked — and,
just as importantly, lays out exactly what is verified, and where.

The strategy is hybrid, not a rewrite: GCOS stays a Python-orchestrated OS for
LLM agents (keeping its signature — the LLM-in-the-memory-manager, the
dashboard, the reproducible eval), but each OS claim is now backed by a real
kernel primitive, and GCOS even ships **its own in-kernel eBPF code**.

---

## 1. Metaphor → real kernel primitive

| GCOS claim | Old (simulated) | Now (kernel-enforced) | Module |
|---|---|---|---|
| Resource quota | `threading.Semaphore` | cgroup v2 `cpu.max` / `memory.max` / `pids.max` | `osprims/cgroup.py` |
| Priority | a sort key | cgroup v2 `cpu.weight` → real Linux **CFS** share | `osprims/cgroup.py` |
| Preemption | between LLM calls | `SIGSTOP`/`SIGCONT`/`SIGKILL` on real PIDs | `osprims/preempt.py` |
| Scheduler | pick from a list | real child processes, RR-preempted, under CFS weights | `osprims/realproc.py` |
| Paging / swap | JSON copy of a list | `mmap` + `madvise(MADV_DONTNEED)`; **real page faults** | `osprims/vmem.py` |
| IPC | `queue.Queue` | POSIX shared memory (`shm_open`) | `osprims/shm.py` |
| Syscall gate | regex over source | seccomp-bpf allowlist enforced by the kernel | `osprims/seccomp.py` |
| (new) in-kernel code | — | our own **eBPF** program on `sched_switch` | `osprims/ebpf/` |
| Multi-step agents | single-shot | real ReAct loop; scheduler time-slices a real agent | `agent_loop.py` |

The kernel sub­systems are reached through a small abstraction that **loudly
degrades** when a primitive isn't available (exactly like the sandbox layer):
`gcos.osprims.os_caps()` probes the host, `warn_if_degraded()` prints a banner,
and `caps_info()` surfaces the posture on the dashboard (`status().osprims`).

---

## 2. Portability — Linux is first-class, POSIX is supported, and we say which

| Primitive | macOS (dev) | Linux | Notes |
|---|:---:|:---:|---|
| Signal preemption (`preempt`, `realproc.rr_order`) | ✅ | ✅ | real `SIGSTOP`/`SIGCONT` |
| mmap demand paging (`vmem`) | ✅ | ✅ | real page faults; Linux also reports `majflt` |
| POSIX shared memory (`shm`) | ✅ | ✅ | `shm_open` segment |
| Multi-step agent loop (`agent_loop`) | ✅ | ✅ | pure Python |
| cgroup v2 CFS share / limits (`cgroup`) | ❌ degrade | ✅ | the proportional-scheduling core |
| seccomp-bpf profile (`seccomp`) | ❌ degrade | ✅ | applied by the container runtime |
| eBPF program (`ebpf/`) | ❌ degrade | ✅\* | \*needs bcc + root; sched_ext needs ≥6.12 |

When a primitive degrades, GCOS falls back to the in-process simulation and says
so — it never silently pretends. `os_caps().kernel_enforced` is `True` only when
cgroup delegation **and** signals are both live.

---

## 3. Verification matrix — what proves what, and where

Everything here is reproducible. The Linux-only rows are run automatically by
`.github/workflows/ci.yml` (including a `--privileged` job), so the
kernel-enforcement claim is checked on every push, not just asserted.

| Claim | Evidence | Where it runs |
|---|---|---|
| Real preemption (RR rotates, FCFS convoys) | `real_preemption` metric / `test_osprims` | macOS, Linux, CI |
| Real demand paging (page-out → fault-in) | `demand_paging` metric / `test_osprims` | macOS, Linux, CI |
| Multi-step agent time-sliced by scheduler | `multistep_agents` metric / `test_agent_loop` | macOS, Linux, CI |
| **cgroup CFS share tracks `cpu.weight`** | `scripts/ci/verify_kernel_enforcement.py` (CI `kernel-enforcement` job) | Linux container, CI |
| **Daemon-cgroup placement (OS budget kernel-enforced, live)** | same script + `Kernel.start` → `place_daemon` | Linux container, CI |
| **Per-agent CFS in the LIVE path (process executor)** | `live_per_agent_cfs` metric — agents as real processes, higher priority finishes sooner | Linux container, CI |
| **Our eBPF actually loads + collects kernel data** | `scripts/ci/verify_ebpf.py` (CI `ebpf` job) — compiles, loads, reads per-PID `sched_switch` | Linux container, CI |
| seccomp profile present / denies syscalls | sandbox profile + `test_osprims` | Linux, CI |
| **Our own sched_ext CPU scheduler loads + dispatches every task** | CI `scx-ext` job: `/sys/kernel/sched_ext/state`=`enabled`, `ops: gcos` (*our* scheduler owns every CPU), then `verify_live_cfs.py` runs GCOS agents under it to completion | Linux ≥ 6.12 runner, CI |

### Measured: cgroup CFS share (real Linux, `cpu.weight` = 100 / 300 / 900)

Run inside a privileged Linux container (the exact command is in §4):

| cpu.weight | expected share | **measured share** |
|---:|---:|---:|
| 100 | 7.7% | **7.9%** |
| 300 | 23.1% | **23.5%** |
| 900 | 69.2% | **68.5%** |

The measured CPU share tracks the weights to within a percent — the Linux CFS
scheduler, not a Python loop, is allocating the CPU. This is the reproducible
analogue of the hand-measured CFS-share benchmarks in the strongest kernel
projects.

---

## 4. Verify it yourself

```bash
# Portable primitives (works on macOS or Linux):
python -m gcos.eval                 # see the "Real-OS substrate" rows
pytest tests/test_osprims.py -q

# Linux kernel enforcement (cgroup CFS + preemption + daemon cgroup), in a container
# — the exact script the CI 'kernel-enforcement' job runs:
docker run --rm --privileged --cgroupns=host -v "$PWD":/app -w /app \
  python:3.11-slim python3 scripts/ci/verify_kernel_enforcement.py

# Load our own eBPF program and read back real kernel sched data (the CI 'ebpf' job):
docker run --rm --privileged -v "$PWD":/app -w /app ubuntu:24.04 bash -c '
  apt-get update -qq && apt-get install -y -qq python3-bpfcc linux-headers-$(uname -r)
  python3 scripts/ci/verify_ebpf.py'

# Load OUR OWN sched_ext CPU scheduler and run GCOS agents under it.
# Needs a Linux >= 6.12 host (the CI 'scx-ext' job runs exactly this on the 6.x
# hosted runner). On macOS, scripts/scx/setup_vm.sh boots a 6.12+ VM first.
cd scripts/scx && make && sudo ./scx_gcos &     # /sys/kernel/sched_ext/state -> enabled, ops: gcos
sudo python3 scripts/ci/verify_live_cfs.py      # agents run as real processes UNDER scx_gcos
```

---

## 5. The live dispatch path — what the kernel enforces, end to end

The headline measurements above (§3) run in the eval/`realproc` path. What does
the **live** worker pool enforce? More than before — and we're precise about the
boundary:

- **OS budget, kernel-enforced (live).** At boot the kernel places the whole GCOS
  daemon in a cgroup with `pids.max` (anti fork-bomb) and optional
  `memory.max`/`cpu.max` (`Kernel.start()` → `osprims.cgroup.place_daemon`;
  `GCOS_CGROUP_*` env). The OS's own resource budget is the kernel's to enforce,
  not a Python counter. Surfaced at `status().daemon_cgroup`.
- **Per-agent CFS priority on the CPU-bound work that exists (live).** An agent's
  actual CPU-bound work is its **sandboxed code**, which already runs in a Docker
  container with kernel cgroup limits; we now also map the agent's priority to the
  container's `cpu_shares` (`coder.py` → `priority_to_cpu_shares`), so a
  higher-priority agent's code gets a larger CFS share when sandboxes compete —
  real per-agent kernel scheduling.
- **Per-agent CFS for *all* agent work — via a process executor.**
  `Kernel(executor_backend="process")` / `GCOS_EXEC=process` runs each agent as a
  real OS process in a per-agent cgroup whose `cpu.weight` = priority, so the
  Linux CFS scheduler — not a Python loop — arbitrates between agents in the
  **live dispatch path**. Verified two ways: the `live_per_agent_cfs` CI gate
  confirms the live executor dispatches and runs every agent as a real process in
  its own `cpu.weight` cgroup; and that those weights actually steer the CFS share
  is the separate `cgroup_cpu_share` gate (`cpu.weight` 100/300/900 → ~8/23/68%,
  robust). (We keep these two claims apart on purpose: the *contended* live share is
  CPU-budget-noisy, so we don't assert its ordering.) kill becomes a real `SIGKILL`.
  We do **not** put per-thread `cpu.weight` on the *thread* pool: under CPython's
  **GIL**, CPU-bound threads never truly compete (we measured a flat 0/0) — it
  would be theater. The thread pool stays the default (it has the shared batcher
  + simpler lifecycle); the process backend is the opt-in for real per-agent CFS,
  trading the shared in-process batcher for process isolation (stated, not hidden).
- **eBPF** (`osprims/ebpf/gcos_sched.bpf.c`) is real, GCOS-authored ring-0 code
  that **now actually loads and is verified by execution in CI** (the `ebpf` job
  compiles it, loads it, and reads back real per-PID `sched_switch` data). It
  needs Linux + bcc + kernel headers + root; the loader auto-mounts tracefs.
  It is *observability/accounting*, not a CPU-arbitrating scheduler.
- **Our own sched_ext CPU scheduler** (`osprims/ebpf/scx_gcos.bpf.c`) — a real BPF
  CPU scheduler we wrote against the scx API, that **now actually loads into the
  kernel and dispatches every task, verified by execution in CI.** The `scx-ext` job
  builds it (bpftool + libbpf-from-source + the scx v1.1.0 headers, BPF compiled
  `-mcpu=v3`), loads it via the libbpf struct_ops loader in `scripts/scx/loader.c`,
  and confirms `/sys/kernel/sched_ext/state` reads **`enabled`** with **`ops:
  gcos`** — i.e. *our* scheduler, not the stock one, is dispatching every task on
  the box. It then runs `verify_live_cfs.py` *under* scx_gcos to confirm GCOS agents
  still dispatch and run to completion while our scheduler owns every CPU. The
  scheduler scales each task's slice by `p->scx.weight` (a real weighted design); we
  do **not** claim a measured per-priority CPU *split* under scx — scx_gcos doesn't
  read cgroup `cpu.weight`, and the contended share is noisy, so the verified
  per-priority **share** is the cgroup-CFS gate above (`cpu.weight` 100/300/900 →
  8/23/68%), while scx_gcos's verified claim is "our ring-0 scheduler runs the
  machine". On a dev box without
  sched_ext (e.g. colima 6.8) `osprims.ebpf.scx.available()` reports False with the
  reason; `scripts/scx/setup_vm.sh` boots a 6.12+ VM (Ubuntu 25.04 / kernel 6.14) on
  macOS via Lima and `run.sh` reproduces the same build+load+verify locally. This is
  the strict "kernel must arbitrate CPU with our own code" capstone — and it is
  checked on every push, not merely asserted.
- **seccomp** is delivered via the Docker profile (kernel-enforced on the
  container); the in-process `self_lockdown()` path needs the libseccomp binding.

These are stated so a reviewer knows exactly where the kernel boundary is — the
same discipline as the policy-gate "pre-filter, not security" framing.
