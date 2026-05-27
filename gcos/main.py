"""GCOS CLI — `python -m gcos <subcommand>`.

M1 subcommands:
    spawn   — create one agent and run it (FCFS, single agent)
    demo    — spawn N agents with different priorities, run with selected scheduler

Later milestones add:  serve (FastAPI), shell (REPL), ps, kill, top.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional


# On Windows the default console codec is cp949/cp1252 which chokes on Solar
# output (CJK, em-dashes, etc.). Force UTF-8 so we don't lose characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from gcos.executor import run_agent
from gcos.kernel.pcb import AgentControlBlock, CapabilitySet
from gcos.kernel.pid_alloc import next_pid
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import make as make_scheduler
from gcos.sandbox import make_runner


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_spawn(args: argparse.Namespace) -> int:
    """Spawn one agent, run it, print the result."""
    pcb = AgentControlBlock(
        pid=next_pid(),
        name=args.name,
        prompt=args.prompt,
        priority=args.priority,
        timeout_s=args.timeout,
        quota_remaining=args.quota,
        capability=CapabilitySet.default_user(),
    )
    print(f"[gcos] spawned pid={pcb.pid} name={pcb.name} prio={pcb.priority}")
    run_agent(pcb)
    print(f"[gcos] pid={pcb.pid} state={pcb.state.value} "
          f"tokens={pcb.tokens_used} wall={pcb.wall_time():.2f}s")
    if pcb.error:
        print(f"[gcos] error: {pcb.error}", file=sys.stderr)
        return 1
    print("--- result ---")
    print(pcb.result or "")
    return 0


def cmd_shell(_args: argparse.Namespace) -> int:
    """Interactive REPL — ps / top / kill / spawn / mem / tree / dmesg ..."""
    from gcos.shell.repl import Shell
    return Shell().run()


def cmd_pipeline(args: argparse.Namespace) -> int:
    """M4 demo: producer agent pipes its output to a consumer agent.

    Spawns:
      researcher: writes 3 facts about <topic>
      writer:     takes those facts via {INPUT} and turns them into a haiku
    """
    from gcos.kernel import Kernel, KernelConfig
    k = Kernel(KernelConfig(scheduler="fcfs", workers=2, quota_total=20))
    k.start()
    try:
        # Peek at the two PIDs that will be assigned next so we can wire
        # pipe_to AND input_from atomically before either agent runs.
        producer_pid = k.pids.peek()
        consumer_pid = producer_pid + 1

        producer = k.spawn(
            f"Give 3 short interesting facts about {args.topic}, "
            "one per line. No numbering. No explanations.",
            name="researcher",
            pipe_to=consumer_pid,
        )
        consumer = k.spawn(
            "Turn the following 3 facts into a single haiku (5-7-5 syllables). "
            "Facts:\n{INPUT}\nReply with only the haiku.",
            name="writer",
            input_from=producer_pid,
        )
        assert producer == producer_pid and consumer == consumer_pid
        print(f"[gcos] researcher pid={producer}  ->  writer pid={consumer}")
        if not k.wait_idle(timeout=60.0):
            print("[gcos] timeout waiting for idle", file=sys.stderr)
            return 1

        for pid in (producer, consumer):
            p = k.get(pid)
            print(f"\n--- pid={pid} name={p.name} state={p.state.value} "
                  f"tokens={p.tokens_used} wall={p.wall_time():.2f}s ---")
            print((p.result or p.error or "").strip())
        return 0
    finally:
        k.shutdown()


def cmd_coder(args: argparse.Namespace) -> int:
    """Single coder agent: LLM -> policy gate -> sandbox."""
    import os
    if args.sandbox:
        os.environ["GCOS_SANDBOX"] = args.sandbox
    pcb = AgentControlBlock(
        pid=next_pid(),
        name=args.name,
        prompt=args.prompt,
        priority=args.priority,
        timeout_s=args.timeout,
        quota_remaining=args.quota,
        capability=CapabilitySet.coder(),
    )
    print(f"[gcos] spawned coder pid={pcb.pid} sandbox={args.sandbox or 'auto'}")
    # The capability dispatch in executor.run_step routes to coder path
    run_agent(pcb)
    print(f"[gcos] pid={pcb.pid} state={pcb.state.value} "
          f"tokens={pcb.tokens_used} wall={pcb.wall_time():.2f}s")
    if pcb.error:
        print(f"[gcos] error: {pcb.error}", file=sys.stderr)
    print("--- result ---")
    print(pcb.result or "")
    return 0 if pcb.state.value == "DONE" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Start FastAPI + worker pool. Web dashboard at http://host:port/."""
    import uvicorn
    from gcos.api.server import create_app
    from gcos.kernel import Kernel, KernelConfig

    # Build the kernel up front so CLI flags can override env
    kernel = Kernel(KernelConfig(
        scheduler=args.scheduler,
        workers=args.workers,
        quota_total=args.quota_total,
    ))
    app = create_app(kernel)
    print(f"[gcos] serving on http://{args.host}:{args.port}  "
          f"(scheduler={args.scheduler}, workers={args.workers}, "
          f"quota={args.quota_total})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Spawn 3 agents with mixed priorities, run them via the chosen scheduler."""
    queue = ReadyQueue()
    sched = make_scheduler(args.scheduler)
    print(f"[gcos] scheduler = {sched.name}")

    samples = [
        ("low",  2, "Say 'low priority done' and nothing else."),
        ("high", 9, "Say 'high priority done' and nothing else."),
        ("mid",  5, "Say 'mid priority done' and nothing else."),
    ]
    for name, prio, prompt in samples:
        pcb = AgentControlBlock(
            pid=next_pid(), name=name, prompt=prompt, priority=prio,
            timeout_s=args.timeout, quota_remaining=args.quota,
        )
        queue.put(pcb)
        print(f"[gcos] queued pid={pcb.pid} name={name} prio={prio}")

    order: list[str] = []
    while True:
        nxt = sched.pick_next(queue)
        if nxt is None:
            break
        run_agent(nxt)
        order.append(nxt.name)
        print(f"[gcos] ran pid={nxt.pid} name={nxt.name} state={nxt.state.value} "
              f"result={(nxt.result or '').strip()[:60]!r}")

    print(f"\n[gcos] execution order: {' -> '.join(order)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gcos", description="GCOS - OS for LLM agents")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="Spawn and run a single agent")
    sp.add_argument("prompt", help="The prompt to send to the agent")
    sp.add_argument("--name", default="anon")
    sp.add_argument("--priority", type=int, default=5)
    sp.add_argument("--timeout", type=float, default=30.0)
    sp.add_argument("--quota", type=int, default=10)
    sp.set_defaults(func=cmd_spawn)

    sd = sub.add_parser("demo", help="M1 demo: 3 agents with mixed priorities")
    sd.add_argument("--scheduler", default="fcfs", choices=["fcfs", "priority", "rr"])
    sd.add_argument("--timeout", type=float, default=30.0)
    sd.add_argument("--quota", type=int, default=10)
    sd.set_defaults(func=cmd_demo)

    sh = sub.add_parser("shell", help="M5: interactive REPL (ps/top/kill/spawn/...)")
    sh.set_defaults(func=cmd_shell)

    pl = sub.add_parser("pipeline", help="M4 demo: producer -> consumer via {INPUT}")
    pl.add_argument("topic", help="Topic the researcher digs into")
    pl.set_defaults(func=cmd_pipeline)

    sc = sub.add_parser("coder", help="Spawn a single coder agent (LLM + sandbox)")
    sc.add_argument("prompt", help="Task for the coder (will be told to emit a python block)")
    sc.add_argument("--name", default="coder")
    sc.add_argument("--priority", type=int, default=5)
    sc.add_argument("--timeout", type=float, default=30.0)
    sc.add_argument("--quota", type=int, default=3)
    sc.add_argument("--sandbox", choices=["auto", "docker", "subprocess"], default=None,
                    help="Override GCOS_SANDBOX for this run")
    sc.set_defaults(func=cmd_coder)

    sv = sub.add_parser("serve", help="Start FastAPI + worker pool (M2)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--scheduler", default="fcfs", choices=["fcfs", "priority", "rr"])
    sv.add_argument("--workers", type=int, default=4)
    sv.add_argument("--quota-total", type=int, default=100)
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
