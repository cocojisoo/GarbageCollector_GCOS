"""GCOS interactive REPL — `python -m gcos shell`.

Embeds a Kernel and serves the OS-feeling commands:

    spawn <name> <prio> <prompt...>     create an agent
    coder <name> <prompt...>            create a coder agent (sandbox-gated)
    ps                                  process table snapshot
    tree [pid]                          process tree (defaults to roots)
    top                                 live-updating ps until Ctrl-C
    kill <pid>                          kill an agent (cascades to children)
    cat <pid>                           show pcb.result / error
    mem <pid>                           context pager stats for that pid
    bus                                 mailboxes with pending messages
    quota                               API quota gauge
    batcher                             rate-limit/concurrency stats
    dmesg [N]                           last N entries from the ring trace log
    help                                this list
    exit / quit / Ctrl-D                leave (kernel shuts down cleanly)

The shell is the OS interface — it doesn't go through HTTP.
"""

from __future__ import annotations

import shlex
import sys
import time
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table

from gcos.kernel import (
    AgentControlBlock,
    AgentState,
    CapabilitySet,
    Kernel,
    KernelConfig,
)
from gcos.memory import ContextPager, default_policies


STATE_STYLE = {
    "NEW":     "dim",
    "READY":   "blue",
    "RUNNING": "yellow",
    "WAITING": "dim",
    "BLOCKED": "red",
    "DONE":    "green",
    "TIMEOUT": "red",
    "ERROR":   "red",
    "ZOMBIE":  "dim",
}


def _build_agents_table(kernel: Kernel) -> Table:
    tbl = Table(title=f"agents (sched={kernel.scheduler.name}, "
                      f"busy={kernel.pool.busy}/{kernel.config.workers}, "
                      f"queue={len(kernel.queue)})",
                expand=True)
    tbl.add_column("PID", justify="right")
    tbl.add_column("name")
    tbl.add_column("state")
    tbl.add_column("prio", justify="right")
    tbl.add_column("parent", justify="right")
    tbl.add_column("calls", justify="right")
    tbl.add_column("tokens", justify="right")
    tbl.add_column("wall", justify="right")
    tbl.add_column("result/error")
    for p in kernel.list_all():
        out = (p.error or p.result or "").splitlines()
        short = out[0] if out else ""
        if len(short) > 60:
            short = short[:60] + "…"
        tbl.add_row(
            str(p.pid), p.name,
            f"[{STATE_STYLE.get(p.state.value, 'white')}]{p.state.value}[/]",
            str(p.priority),
            "" if p.parent_pid is None else str(p.parent_pid),
            str(p.llm_calls_used), str(p.tokens_used),
            f"{p.wall_time():.2f}s",
            short,
        )
    return tbl


class Shell:
    def __init__(self, kernel: Optional[Kernel] = None) -> None:
        self.console = Console()
        self.kernel = kernel or Kernel(KernelConfig.from_env())
        self.pager = ContextPager(budget_tokens=4096, policies=default_policies())

    # ----- lifecycle -------------------------------------------------------

    def run(self) -> int:
        self.kernel.start()
        self.console.print(
            "[bold yellow]GCOS[/] interactive shell — "
            f"sched=[cyan]{self.kernel.scheduler.name}[/] "
            f"workers=[cyan]{self.kernel.config.workers}[/] "
            f"quota=[cyan]{self.kernel.quota.total}[/]"
        )
        self.console.print("type [bold]help[/] for commands, [bold]exit[/] to quit.\n")
        try:
            while True:
                try:
                    raw = self.console.input("[bold green]gcos>[/] ")
                except (EOFError, KeyboardInterrupt):
                    self.console.print()
                    break
                if not raw.strip():
                    continue
                if not self._dispatch(raw):
                    break
        finally:
            self.kernel.shutdown()
            self.console.print("[dim]kernel shut down.[/]")
        return 0

    # ----- dispatch --------------------------------------------------------

    def _dispatch(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as e:
            self.console.print(f"[red]parse error:[/] {e}")
            return True
        if not parts:
            return True
        cmd, *args = parts
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            self.console.print(f"[red]unknown command:[/] {cmd!r}  (try [bold]help[/])")
            return True
        try:
            return handler(args)
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[red]{type(e).__name__}:[/] {e}")
            return True

    # ----- commands --------------------------------------------------------

    def cmd_help(self, _args) -> bool:
        self.console.print(__doc__)
        return True

    def cmd_exit(self, _args) -> bool: return False
    def cmd_quit(self, _args) -> bool: return False

    def cmd_spawn(self, args) -> bool:
        if len(args) < 3:
            self.console.print("[red]usage:[/] spawn <name> <prio> <prompt...>")
            return True
        name, prio_s, *rest = args
        prio = int(prio_s)
        prompt = " ".join(rest)
        pid = self.kernel.spawn(prompt, name=name, priority=prio)
        self.console.print(f"[green]spawned[/] pid=[bold]{pid}[/] name={name} prio={prio}")
        return True

    def cmd_coder(self, args) -> bool:
        if len(args) < 2:
            self.console.print("[red]usage:[/] coder <name> <prompt...>")
            return True
        name, *rest = args
        prompt = " ".join(rest)
        pid = self.kernel.spawn(
            prompt, name=name, priority=5,
            capability=CapabilitySet.coder(),
        )
        self.console.print(f"[green]spawned coder[/] pid=[bold]{pid}[/] name={name}")
        return True

    def cmd_ps(self, _args) -> bool:
        self.console.print(_build_agents_table(self.kernel))
        return True

    def cmd_top(self, _args) -> bool:
        self.console.print("[dim]live ps — Ctrl-C to leave[/]")
        try:
            with Live(_build_agents_table(self.kernel),
                      console=self.console, refresh_per_second=4) as live:
                while True:
                    time.sleep(0.25)
                    live.update(_build_agents_table(self.kernel))
        except KeyboardInterrupt:
            self.console.print()
        return True

    def cmd_kill(self, args) -> bool:
        if not args:
            self.console.print("[red]usage:[/] kill <pid>")
            return True
        pid = int(args[0])
        ok = self.kernel.kill(pid)
        if ok:
            self.console.print(f"[yellow]killed[/] pid={pid} "
                               f"(descendants reaped)")
        else:
            self.console.print(f"[red]could not kill[/] pid={pid} "
                               f"(not found or already terminal)")
        return True

    def cmd_cat(self, args) -> bool:
        if not args:
            self.console.print("[red]usage:[/] cat <pid>")
            return True
        pcb = self._lookup(int(args[0]))
        if pcb is None:
            return True
        self.console.print(f"[bold]pid={pcb.pid} name={pcb.name} "
                           f"state={pcb.state.value}[/]")
        if pcb.error:
            self.console.print(f"[red]error:[/] {pcb.error}")
        if pcb.result:
            self.console.rule(f"result")
            self.console.print(pcb.result)
        return True

    def cmd_mem(self, args) -> bool:
        if not args:
            self.console.print("[red]usage:[/] mem <pid>")
            return True
        pcb = self._lookup(int(args[0]))
        if pcb is None:
            return True
        stats = self.pager.stats(pcb)
        tbl = Table(title=f"context pager — pid {pcb.pid}", expand=False)
        tbl.add_column("field"); tbl.add_column("value", justify="right")
        for k, v in stats.items():
            tbl.add_row(k, str(v))
        self.console.print(tbl)
        for i, page in enumerate(pcb.context_pages):
            flags = []
            if page.pinned: flags.append("pinned")
            if page.summarized: flags.append("summary")
            preview = (page.content[:80] + "…") if len(page.content) > 80 else page.content
            self.console.print(
                f"  [{i:>2}] {page.role:<9} tk={page.tokens:>4} "
                f"[{','.join(flags) or '-'}] {preview!r}"
            )
        return True

    def cmd_tree(self, args) -> bool:
        if args:
            root_pid = int(args[0])
            rows = self.kernel.tree.tree_view(root_pid)
        else:
            rows = []
            for r in self.kernel.tree.roots():
                rows.extend(self.kernel.tree.tree_view(r.pid))
        if not rows:
            self.console.print("[dim](empty tree)[/]")
            return True
        for r in rows:
            indent = "  " * r["depth"]
            self.console.print(
                f"{indent}└─ pid={r['pid']} name={r['name']} "
                f"state={r['state']} prio={r['prio']}"
            )
        return True

    def cmd_bus(self, _args) -> bool:
        snap = self.kernel.bus.snapshot()
        if not snap:
            self.console.print("[dim](no mailboxes have been opened)[/]")
            return True
        tbl = Table(title="message bus", expand=False)
        tbl.add_column("PID", justify="right")
        tbl.add_column("pending", justify="right")
        for pid, n in sorted(snap.items()):
            tbl.add_row(str(pid), str(n))
        self.console.print(tbl)
        return True

    def cmd_quota(self, _args) -> bool:
        q = self.kernel.quota.snapshot()
        self.console.print(
            f"quota: [green]{q['remaining']}[/]/{q['total']} "
            f"(used [yellow]{q['used']}[/])"
        )
        return True

    def cmd_batcher(self, _args) -> bool:
        client = getattr(self.kernel.pool, "client", None)
        if client is None or not hasattr(client, "stats"):
            self.console.print("[dim](no batcher client active — "
                               "pool not started or fake client in use)[/]")
            return True
        s = client.stats
        for k, v in s.items():
            self.console.print(f"  {k}: {v}")
        return True

    def cmd_dmesg(self, args) -> bool:
        limit = int(args[0]) if args else 25
        entries = self.kernel.trace.snapshot(limit=limit)
        if not entries:
            self.console.print("[dim](empty trace)[/]")
            return True
        for e in entries:
            ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            kind = e.get("kind", "")
            color = "red" if kind in ("error", "critical") else \
                    "yellow" if kind == "warning" else "dim"
            self.console.print(f"[{color}]{ts} [{e.get('level','')}] "
                               f"{e.get('logger','')}:[/] {e['msg']}")
        return True

    # ----- helpers ---------------------------------------------------------

    def _lookup(self, pid: int) -> Optional[AgentControlBlock]:
        pcb = self.kernel.get(pid)
        if pcb is None:
            self.console.print(f"[red]no such pid:[/] {pid}")
        return pcb


def main(argv: Optional[list[str]] = None) -> int:
    return Shell().run()


if __name__ == "__main__":
    sys.exit(main())
