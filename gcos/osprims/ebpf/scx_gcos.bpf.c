/* scx_gcos.bpf.c — GCOS's own sched_ext CPU scheduler (Linux >= 6.12).
 *
 * ┌─ STATUS (verified) ───────────────────────────────────────────────────────┐
 * │ This is a real in-kernel CPU scheduler written against the sched_ext (scx) │
 * │ API. It LOADS and ARBITRATES THE CPU, verified by execution in CI: the     │
 * │ `scx-ext` job builds it (bpftool + libbpf-from-source + scx v1.1.0 headers,│
 * │ BPF compiled -mcpu=v3), loads it via the libbpf struct_ops loader in        │
 * │ scripts/scx/loader.c, and confirms /sys/kernel/sched_ext/state == "enabled"│
 * │ with ops == "gcos" — i.e. THIS scheduler, not the stock one, dispatches    │
 * │ every task on the box. It then re-runs the per-agent CFS check UNDER it.    │
 * │ Needs Linux >= 6.12 + CONFIG_SCHED_CLASS_EXT + the scx/libbpf toolchain. A │
 * │ plain dev box (colima 6.8) lacks sched_ext; scripts/scx/setup_vm.sh boots  │
 * │ a 6.12+ VM to reproduce locally. The lighter bcc observability program is  │
 * │ gcos_sched.bpf.c (also CI-loaded).                                          │
 * └────────────────────────────────────────────────────────────────────────────┘
 *
 * What it does: a minimal weighted scheduler. Each task carries `p->scx.weight`
 * (the kernel's per-task scheduling weight); we scale the time slice by it, so a
 * heavier-weighted task is dispatched with a proportionally larger slice — weighted
 * CPU arbitration in ring-0, authored by GCOS. This is the "our own kernel
 * scheduler" capstone the host-kernel approach otherwise lacks: once loaded, OUR
 * code decides how every task on the box is dispatched (verified in CI: state=
 * enabled, ops=gcos).
 *
 * Honesty on priority: `p->scx.weight` is NOT the cgroup `cpu.weight` GCOS sets from
 * agent priority — plain scx schedulers don't inherit cgroup weight (that needs scx
 * cgroup support). So this scheduler is verified to RUN THE MACHINE, but we do not
 * claim it splits CPU by GCOS priority; that per-priority share is enforced + checked
 * on the CFS path via cgroup cpu.weight (osprims/cgroup.py, cgroup_cpu_share metric).
 * Feeding GCOS priority into p->scx.weight (or a BPF priority map) is the next step.
 *
 * Build + load (on a 6.12+ host with the scx framework): see scripts/scx/ —
 *   make && sudo ./scx_gcos        # generates the skeleton + attaches struct_ops
 * The Makefile/loader.c there are exactly what the CI scx-ext job runs.
 */

#include <scx/common.bpf.h>

char _license[] SEC("license") = "GPL";

#define GCOS_DSQ 0                /* one shared dispatch queue */

UEI_DEFINE(uei);                  /* scx user-exit info (clean unload) */

/* Pick a CPU: prefer an idle one (the scx default helper); dispatch locally if
 * we found an idle CPU so the task starts immediately. */
s32 BPF_STRUCT_OPS(gcos_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle)
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
	return cpu;
}

/* Enqueue: scale the time slice by the task's scheduling weight (p->scx.weight) —
 * a heavier task gets a larger slice, arbitrated by our own scheduler in the
 * kernel. (See the header note: p->scx.weight is the kernel's per-task weight, not
 * the cgroup cpu.weight GCOS sets from priority; per-priority share is verified on
 * the CFS path, not here.) */
void BPF_STRUCT_OPS(gcos_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice = SCX_SLICE_DFL * p->scx.weight / 100;
	if (slice < SCX_SLICE_DFL / 8)
		slice = SCX_SLICE_DFL / 8;          /* floor so nothing starves */
	scx_bpf_dsq_insert(p, GCOS_DSQ, slice, enq_flags);
}

/* Dispatch: move the next task from our shared queue onto this CPU. */
void BPF_STRUCT_OPS(gcos_dispatch, s32 cpu, struct task_struct *prev)
{
	scx_bpf_dsq_move_to_local(GCOS_DSQ);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(gcos_init)
{
	return scx_bpf_create_dsq(GCOS_DSQ, -1);
}

void BPF_STRUCT_OPS(gcos_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SEC(".struct_ops.link")
struct sched_ext_ops gcos_ops = {
	.select_cpu = (void *)gcos_select_cpu,
	.enqueue    = (void *)gcos_enqueue,
	.dispatch   = (void *)gcos_dispatch,
	.init       = (void *)gcos_init,
	.exit       = (void *)gcos_exit,
	.name       = "gcos",
};
