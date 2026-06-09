"""Executor — drives an agent's LLM steps.

Two entry points:

- `run_step(pcb, client)` — does **one** LLM call. Returns True if the agent
  has more work and should be re-queued, False if it reached a terminal state.
  This is what the worker pool calls; it lets RR scheduling enforce a quantum
  measured in LLM calls.

- `run_agent(pcb, client)` — convenience wrapper: loops `run_step` until
  the agent terminates. Used by the M1 CLI and tests.

Agent model — honest scope (F17, updated): there are now **two** agent kinds.
- *Single-shot* (default for plain chat and the coder): one LLM call → terminal
  ("one successful call → DONE"); `run_step` returns False.
- *Multi-step* (`CapabilitySet.agent()`, `multi_step=True`): a real ReAct loop in
  `gcos.agent_loop` — think → tool → observe → repeat → FINAL — that makes one
  LLM call per step and returns True until the agent finishes. This is what makes
  the worker pool's quantum / re-queue / RR preemption time-slice a *real* agent
  against its peers, not just the eval's synthetic runners.
The multi-step tool set is intentionally small and offline-capable (calc, note)
so the loop is fully testable without a network; richer tools (sandbox exec, IPC)
build on the same protocol. We don't oversell it: the default path is still
single-shot, and `multi_step` must be opted into per agent.
"""

from __future__ import annotations

import logging
from typing import Optional

from gcos.backend.solar_client import SolarClient
from gcos.kernel.pcb import AgentControlBlock, AgentState, ContextPage
from gcos.memory import ContextPager, default_policies


log = logging.getLogger(__name__)


# Module-level singleton pager — the executor doesn't know which kernel it's
# in, and constructing policies per-call is wasteful. The kernel could swap
# this out via set_default_pager() if it wants different settings per boot.
_DEFAULT_PAGER: Optional[ContextPager] = None


def get_default_pager() -> ContextPager:
    global _DEFAULT_PAGER
    if _DEFAULT_PAGER is None:
        _DEFAULT_PAGER = ContextPager(
            budget_tokens=4096, policies=default_policies(),
        )
    return _DEFAULT_PAGER


def set_default_pager(pager: ContextPager) -> None:
    global _DEFAULT_PAGER
    _DEFAULT_PAGER = pager


def _pager_for(pcb: AgentControlBlock) -> ContextPager:
    """Use the kernel's pager when one is wired onto the PCB (H22), else the
    module default. This keeps two kernels in one process from sharing a budget."""
    pager = getattr(pcb, "pager", None)
    return pager if isinstance(pager, ContextPager) else get_default_pager()


def _build_messages(
    pcb: AgentControlBlock, pager: ContextPager, client: Optional[SolarClient] = None,
) -> list[dict]:
    """Assemble messages within the token budget. Appends the current user
    turn only on the *first* step (subsequent steps just consume context).

    `client` is the worker's (batched) Solar client; passing it through lets a
    summarize-eviction reuse the OS's throttle instead of a bypass client (B5).
    """
    extra = pcb.prompt if pcb.llm_calls_used == 0 else None
    return pager.assemble(pcb, client=client, extra_user_prompt=extra)


def run_step(pcb: AgentControlBlock, client: Optional[SolarClient] = None) -> bool:
    """Dispatch one step.

    If the agent has `capability.can_exec_code`, route through the coder path
    (policy gate → Solar → code extract → sandbox). Otherwise, plain chat.
    """
    if pcb.capability.multi_step:
        # Real multi-step ReAct agent: many LLM calls per task, so the
        # scheduler's quantum genuinely time-slices it against peers (A1).
        from gcos.agent_loop import run_react_step
        return run_react_step(pcb, client)
    if pcb.capability.can_exec_code:
        # Lazy import: coder imports sandbox which is independent of executor.
        from gcos.coder import run_coder_step
        return run_coder_step(pcb, client)
    return _run_plain_step(pcb, client)


def _run_plain_step(pcb: AgentControlBlock, client: Optional[SolarClient] = None) -> bool:
    """The original M1/M2 plain-chat path."""
    client = client or SolarClient()

    if pcb.is_terminal():
        return False

    if pcb.quota_remaining <= 0:
        pcb.transition(AgentState.ERROR)
        pcb.error = "per-agent quota exhausted"
        return False

    if pcb.state != AgentState.RUNNING:
        pcb.transition(AgentState.RUNNING)
    log.info("PID %d (%s) step=%d", pcb.pid, pcb.name, pcb.llm_calls_used + 1)

    try:
        result = client.chat(
            _build_messages(pcb, _pager_for(pcb), client=client),
            max_tokens=min(pcb.capability.max_tokens, 1024),
            timeout=pcb.timeout_s,
        )
    except TimeoutError as e:
        pcb.transition(AgentState.TIMEOUT)
        pcb.error = str(e)
        return False
    except Exception as e:  # noqa: BLE001
        pcb.transition(AgentState.ERROR)
        pcb.error = f"{type(e).__name__}: {e}"
        return False

    pcb.quota_remaining -= 1
    pcb.llm_calls_used += 1
    pcb.tokens_used += result.tokens

    # Cooperative cancellation (A4): the LLM call is non-preemptible, but if the
    # agent was killed while we were inside it, discard the result rather than
    # overwriting ZOMBIE with DONE. (transition() would also reject DONE now,
    # but this avoids recording a dead agent's output and piping it downstream.)
    if pcb.state == AgentState.ZOMBIE:
        log.info("PID %d killed mid-step; discarding result", pcb.pid)
        return False

    pcb.result = result.content

    # Persist exchange — first step also records the original prompt
    if pcb.llm_calls_used == 1:
        pcb.context_pages.append(
            ContextPage(role="user", content=pcb.prompt, tokens=result.prompt_tokens)
        )
    pcb.context_pages.append(
        ContextPage(role="assistant", content=result.content, tokens=result.completion_tokens)
    )

    # M2 completion: one successful call → DONE.
    # M4 will replace this with "keep going while assistant emitted a tool call".
    pcb.transition(AgentState.DONE)
    log.info("PID %d (%s) DONE — %d tokens total", pcb.pid, pcb.name, pcb.tokens_used)
    return False


def run_agent(pcb: AgentControlBlock, client: Optional[SolarClient] = None) -> AgentControlBlock:
    """Loop `run_step` until the agent reaches a terminal state."""
    client = client or SolarClient()
    safety = 32  # hard cap on steps per call
    while not pcb.is_terminal() and safety > 0:
        keep_going = run_step(pcb, client)
        safety -= 1
        if not keep_going:
            break
    return pcb
