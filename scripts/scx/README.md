# scx_gcos — running GCOS's own kernel CPU scheduler (Linux ≥ 6.12)

`gcos/osprims/ebpf/scx_gcos.bpf.c` is a real **sched_ext** scheduler GCOS wrote:
it scales each task's CPU time slice by its `cpu.weight` (which GCOS derives from
agent priority), so the **kernel arbitrates the CPU with our scheduler**. This
directory builds and loads it.

**Verified in CI.** The `scx-ext` job (`.github/workflows/ci.yml`) runs exactly
this build+load on a sched_ext-capable hosted runner: it builds the BPF object +
skeleton + loader, attaches the scheduler, and confirms
`/sys/kernel/sched_ext/state` == `enabled` with `ops` == `gcos` — i.e. *our*
scheduler is dispatching every task — then re-checks per-agent CFS under it.
sched_ext needs Linux **≥ 6.12** with `CONFIG_SCHED_CLASS_EXT` + the scx/libbpf
toolchain; a plain dev box (colima 6.8) lacks it, so the steps below reproduce the
same thing locally inside a throwaway VM.

## One-command path (on macOS, via a throwaway VM)

```bash
./scripts/scx/setup_vm.sh        # boot Ubuntu 25.04 (kernel 6.14) in Lima + toolchain
limactl shell scx-gcos
cd GarbageCollector_GCOS         # the mounted repo
sudo ./scripts/scx/run.sh        # build → load scx_gcos → run GCOS agents under our scheduler
limactl delete -f scx-gcos       # tear down
```

`run.sh` refuses (clearly) if the host kernel is < 6.12 or lacks sched_ext.

## Files
- `../../gcos/osprims/ebpf/scx_gcos.bpf.c` — the BPF scheduler (struct_ops `gcos_ops`).
- `loader.c` — libbpf user-space loader that attaches it.
- `Makefile` — vmlinux.h → BPF object → skeleton → loader.
- `setup_vm.sh` — provision a 6.12+ VM + toolchain.
- `run.sh` — build + load + run GCOS agents under our scheduler (state=enabled, ops=gcos).

## What "verified" means here
With scx_gcos loaded, `/sys/kernel/sched_ext/state` reads `enabled` and `ops`
names `gcos` — i.e. **GCOS's own in-kernel scheduler is dispatching every task on
the box**, not the default CFS — and GCOS agents then run as real processes under
it to completion. The CI `scx-ext` job asserts this on every push (see its log:
`scx_gcos LOADED … ops: gcos … OK: live process-executor path ran all agents …`).
That is the capability that separates GCOS from the field — a kernel CPU scheduler
we wrote, actually running the machine. (scx_gcos scales each task's slice by its
weight; it does not read cgroup `cpu.weight`, so per-priority CPU **share** is
verified separately on the CFS path — see `docs/REAL_OS.md`.)
