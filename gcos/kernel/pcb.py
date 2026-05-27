"""Agent Control Block — the PCB of a GCOS process.

Each running LLM agent is represented by exactly one AgentControlBlock.
All scheduler/memory/sandbox decisions key off this struct.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentState(str, Enum):
    NEW = "NEW"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"      # waiting on IPC input
    BLOCKED = "BLOCKED"      # waiting on quota / batcher
    DONE = "DONE"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    ZOMBIE = "ZOMBIE"        # finished, awaiting parent reap


@dataclass
class CapabilitySet:
    """Per-agent capability set — the *only* thing the sandbox layer trusts.

    Tag-based policy gate (sandbox/policy_gate.py) is the 1st line of defense;
    this struct is what the kernel itself enforces.
    """
    can_call_llm: bool = True
    can_exec_code: bool = False
    can_net: bool = False
    can_fs_write: bool = False
    can_spawn_child: bool = False
    allowed_paths: list[str] = field(default_factory=list)
    max_tokens: int = 4096
    max_tool_calls: int = 20

    @classmethod
    def default_user(cls) -> "CapabilitySet":
        return cls()

    @classmethod
    def coder(cls) -> "CapabilitySet":
        return cls(can_exec_code=True, can_spawn_child=True, max_tokens=8192)


@dataclass
class ContextPage:
    """One page of an agent's conversation context.

    The memory manager (M4) treats these as evictable pages — `pinned=True`
    pages (e.g. the system prompt) are never evicted.
    """
    role: str               # "system" | "user" | "assistant"
    content: str
    tokens: int = 0
    last_access: float = field(default_factory=time.time)
    pinned: bool = False
    summarized: bool = False   # set True after summarize-evict

    def touch(self) -> None:
        self.last_access = time.time()


@dataclass
class AgentControlBlock:
    """The PCB. One per agent. Everything the OS needs to manage it lives here."""

    pid: int
    name: str
    prompt: str

    state: AgentState = AgentState.NEW
    priority: int = 5                          # 0-9, higher = more urgent

    parent_pid: Optional[int] = None
    children: list[int] = field(default_factory=list)

    capability: CapabilitySet = field(default_factory=CapabilitySet.default_user)
    quota_remaining: int = 10                  # LLM call budget
    timeout_s: float = 30.0

    context_pages: list[ContextPage] = field(default_factory=list)

    # IPC
    pipe_to: Optional[int] = None              # forward result to this PID
    input_from: Optional[int] = None           # consume {INPUT} from this PID

    # Timing
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    # Results
    result: Optional[str] = None
    error: Optional[str] = None
    llm_calls_used: int = 0
    tokens_used: int = 0

    # --- state transitions -------------------------------------------------

    def transition(self, new_state: AgentState) -> None:
        """Move to a new state, updating timestamps on entry/exit."""
        if new_state == AgentState.RUNNING and self.started_at is None:
            self.started_at = time.time()
        if new_state in {AgentState.DONE, AgentState.TIMEOUT,
                         AgentState.ERROR, AgentState.ZOMBIE}:
            self.finished_at = time.time()
        self.state = new_state

    def is_terminal(self) -> bool:
        return self.state in {AgentState.DONE, AgentState.TIMEOUT,
                              AgentState.ERROR, AgentState.ZOMBIE}

    def wall_time(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    # --- /proc-style view --------------------------------------------------

    def to_row(self) -> dict:
        """One row for the `ps` / web process table."""
        return {
            "pid": self.pid,
            "name": self.name,
            "state": self.state.value,
            "prio": self.priority,
            "parent": self.parent_pid,
            "quota": self.quota_remaining,
            "tokens": self.tokens_used,
            "calls": self.llm_calls_used,
            "wall": round(self.wall_time(), 2),
        }
