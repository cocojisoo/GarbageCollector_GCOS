"""Agent Control Block — the PCB of a GCOS process.

Each running LLM agent is represented by exactly one AgentControlBlock.
All scheduler/memory/sandbox decisions key off this struct.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


log = logging.getLogger(__name__)


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


# Terminal states are *absorbing*: once an agent is DONE/TIMEOUT/ERROR/ZOMBIE it
# can never transition again. This is the safety property that stops a killed
# (ZOMBIE) agent from being resurrected to DONE by a worker that was mid-step
# when the kill landed (bug A4), and more generally prevents any late writer
# from clobbering a finalized PCB.
_TERMINAL_STATES = frozenset(
    {AgentState.DONE, AgentState.TIMEOUT, AgentState.ERROR, AgentState.ZOMBIE}
)

# Legal non-terminal transitions. This is intentionally permissive (the runtime
# legitimately bounces agents through READY<->WAITING<->BLOCKED as IPC/quota
# resolve); its job is to document the intended graph and surface anomalies,
# not to police every edge. Terminal sources are handled separately (absorbing).
_LEGAL_TRANSITIONS: dict["AgentState", frozenset] = {
    AgentState.NEW: frozenset({
        AgentState.READY, AgentState.WAITING, AgentState.RUNNING,
        AgentState.ZOMBIE,
    }),
    AgentState.READY: frozenset({
        AgentState.RUNNING, AgentState.WAITING, AgentState.BLOCKED,
        AgentState.DONE, AgentState.TIMEOUT, AgentState.ERROR, AgentState.ZOMBIE,
    }),
    AgentState.RUNNING: frozenset({
        AgentState.READY, AgentState.WAITING, AgentState.BLOCKED,
        AgentState.DONE, AgentState.TIMEOUT, AgentState.ERROR, AgentState.ZOMBIE,
    }),
    AgentState.WAITING: frozenset({
        AgentState.READY, AgentState.RUNNING, AgentState.BLOCKED,
        AgentState.DONE, AgentState.TIMEOUT, AgentState.ERROR, AgentState.ZOMBIE,
    }),
    AgentState.BLOCKED: frozenset({
        AgentState.READY, AgentState.RUNNING, AgentState.WAITING,
        AgentState.DONE, AgentState.TIMEOUT, AgentState.ERROR, AgentState.ZOMBIE,
    }),
}


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

    # Runtime wiring: the kernel attaches *its* ContextPager here at spawn so the
    # executor uses per-kernel memory config (budget + policies + quota) instead
    # of a process-global singleton (H22). Loosely typed to avoid a pcb<->memory
    # import cycle. None → executor falls back to the module-default pager.
    pager: Optional[object] = None

    # IPC
    pipe_to: Optional[int] = None              # forward result to this PID
    input_from: Optional[int] = None           # consume {INPUT} from this PID
    waiting_since: Optional[float] = None       # when this agent first parked WAITING (E16)

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

    def transition(self, new_state: AgentState) -> bool:
        """Move to a new state, updating timestamps on entry/exit.

        Returns True if the state changed, False if the transition was rejected.
        Terminal states are absorbing — once finalized, a PCB never moves again,
        so a late writer (e.g. a worker finishing a step after the agent was
        killed) cannot clobber it.
        """
        if new_state == self.state:
            return False
        if self.state in _TERMINAL_STATES:
            # Absorbing: ignore. This is the A4 guard.
            log.debug("PID %s: ignoring %s->%s (terminal is absorbing)",
                      self.pid, self.state.value, new_state.value)
            return False
        if new_state not in _LEGAL_TRANSITIONS.get(self.state, frozenset()):
            # Not fatal — perform it but flag it, so an unexpected edge shows up
            # in the trace log instead of silently corrupting state.
            log.warning("PID %s: unexpected transition %s->%s",
                        self.pid, self.state.value, new_state.value)

        if new_state == AgentState.RUNNING and self.started_at is None:
            self.started_at = time.time()
        if new_state in _TERMINAL_STATES:
            self.finished_at = time.time()
        self.state = new_state
        return True

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

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
