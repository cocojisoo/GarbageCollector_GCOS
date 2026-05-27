"""Process tree — parent/child traversal + reaping.

The parent_pid + children fields live on AgentControlBlock directly (set by
Kernel.spawn). This module provides traversal helpers and the reap policy:
when a parent enters a terminal state, every still-running descendant is
forcibly transitioned to ZOMBIE.

This is gcos's equivalent of the kernel reaping orphan processes — except
"orphan" here means "your launcher died, so your output has nowhere to go".
"""

from __future__ import annotations

import logging
from typing import Iterable

from gcos.kernel.pcb import AgentControlBlock, AgentState
from gcos.kernel.process_table import ProcessTable


log = logging.getLogger(__name__)


class ProcessTree:
    def __init__(self, table: ProcessTable) -> None:
        self.table = table

    # --- traversal ---------------------------------------------------------

    def children_of(self, pid: int) -> list[AgentControlBlock]:
        parent = self.table.get(pid)
        if parent is None:
            return []
        return [
            child for cid in parent.children
            if (child := self.table.get(cid)) is not None
        ]

    def descendants_of(self, pid: int) -> list[AgentControlBlock]:
        """All descendants (BFS), excluding `pid` itself."""
        out: list[AgentControlBlock] = []
        stack = list(self.children_of(pid))
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.children_of(cur.pid))
        return out

    def ancestors_of(self, pid: int) -> list[AgentControlBlock]:
        out: list[AgentControlBlock] = []
        cur = self.table.get(pid)
        while cur is not None and cur.parent_pid is not None:
            parent = self.table.get(cur.parent_pid)
            if parent is None:
                break
            out.append(parent)
            cur = parent
        return out

    # --- reaping -----------------------------------------------------------

    def reap_descendants(self, pid: int, *, reason: str = "parent died") -> int:
        """Mark every still-running descendant ZOMBIE. Returns the count."""
        n = 0
        for child in self.descendants_of(pid):
            if not child.is_terminal():
                child.transition(AgentState.ZOMBIE)
                child.error = f"reaped: {reason} (root={pid})"
                n += 1
        if n:
            log.info("process_tree: reaped %d descendants of PID %d (%s)",
                     n, pid, reason)
        return n

    # --- ergonomics --------------------------------------------------------

    def link(self, parent_pid: int, child_pid: int) -> None:
        """Establish parent->child. PCB.parent_pid is set by Kernel.spawn;
        this method exists for tests and detached attachments."""
        parent = self.table.get(parent_pid)
        child = self.table.get(child_pid)
        if parent is None or child is None:
            return
        if child_pid not in parent.children:
            parent.children.append(child_pid)
        child.parent_pid = parent_pid

    def tree_view(self, root_pid: int) -> list[dict]:
        """Flat list, depth-prefixed, for `ps --tree` style output."""
        out: list[dict] = []

        def walk(pid: int, depth: int) -> None:
            pcb = self.table.get(pid)
            if pcb is None:
                return
            row = pcb.to_row()
            row["depth"] = depth
            out.append(row)
            for cid in pcb.children:
                walk(cid, depth + 1)

        walk(root_pid, 0)
        return out

    def roots(self) -> Iterable[AgentControlBlock]:
        return [p for p in self.table.snapshot() if p.parent_pid is None]
