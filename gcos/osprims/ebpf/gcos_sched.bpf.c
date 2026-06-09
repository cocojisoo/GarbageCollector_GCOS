// gcos_sched.bpf.c — GCOS's own eBPF program, run by the kernel in ring-0.
//
// This is the piece that gives a Python project genuine in-kernel code: the
// kernel runs this BPF bytecode on every context switch (the sched:sched_switch
// tracepoint) and maintains, per PID, (a) how many times it was scheduled onto a
// CPU and (b) the nanoseconds it actually spent on-CPU. That is real, measured
// kernel-scheduler data about GCOS agents — not a simulated counter.
//
// Loaded from Python via bcc (gcos/osprims/ebpf/__init__.py). Requires Linux +
// bcc/libbpf + privilege; on any other host the loader degrades loudly.

BPF_HASH(oncpu_ns, u32, u64);   // pid -> total on-CPU nanoseconds
BPF_HASH(switches, u32, u64);   // pid -> times scheduled in
BPF_HASH(last_in, u32, u64);    // pid -> timestamp it was last scheduled in

TRACEPOINT_PROBE(sched, sched_switch) {
    u64 ts = bpf_ktime_get_ns();
    u32 prev = args->prev_pid;
    u32 next = args->next_pid;

    // The task going OFF-cpu: add the slice it just ran to its on-CPU total.
    u64 *t = last_in.lookup(&prev);
    if (t) {
        u64 delta = ts - *t;
        u64 zero = 0;
        u64 *acc = oncpu_ns.lookup_or_init(&prev, &zero);
        if (acc) { *acc += delta; }
    }

    // The task coming ON-cpu: stamp it and bump its scheduled-in counter.
    last_in.update(&next, &ts);
    u64 zero2 = 0;
    u64 *c = switches.lookup_or_init(&next, &zero2);
    if (c) { *c += 1; }

    return 0;
}
