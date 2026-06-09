"""agent_loop.py — a real multi-step (ReAct-style) agent executor.

This closes the single biggest honesty gap the project documented (F17): agents
were *single-shot* (`run_step` always returned False), so the worker pool's
quantum / re-queue / RR machinery was only ever exercised by synthetic eval
runners, never by real agents. Here an agent takes **many** steps — think → act
(tool) → observe → repeat → finalize — making one LLM call per step. Now:

  - `pcb.llm_calls_used` grows across a task, so an RR quantum genuinely
    time-slices a real agent against its peers (not just in the eval);
  - the scheduler's preemption is the difference between a long agent running to
    completion (FCFS) and fairly interleaving (RR) — for real workloads.

The tool protocol is deliberately simple and offline-capable so the loop is
fully testable without a network: the model replies with either

    TOOL: <name> <arg>      (e.g.  TOOL: calc 6*7)
    FINAL: <answer>

The worker calls `run_react_step(pcb, client)` once per dispatch step; it does
exactly one LLM call and returns True while the agent has more work.
"""

from __future__ import annotations

import ast
import logging
import operator
from typing import Callable, Optional

from gcos.kernel.pcb import AgentControlBlock, AgentState, ContextPage


log = logging.getLogger(__name__)


SYSTEM = (
    "You are a GCOS agent that solves a task in steps. On each step reply with "
    "exactly one line, either:\n"
    "  TOOL: <name> <arg>   to use a tool (tools: calc <expr>, note <text>)\n"
    "  FINAL: <answer>      when you are done.\n"
    "Use calc for arithmetic. Keep going until you can give FINAL."
)


# --- tools (deterministic, offline) ----------------------------------------

_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_calc(expr: str) -> str:
    """Evaluate an arithmetic expression with no names/calls — a real tool the
    agent can call without us shelling out to eval()."""
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        return str(ev(tree.body))
    except Exception as e:  # noqa: BLE001
        return f"calc error: {e}"


TOOLS: dict[str, Callable[[str, AgentControlBlock], str]] = {
    "calc": lambda arg, pcb: _safe_calc(arg),
    "note": lambda arg, pcb: f"noted: {arg.strip()[:120]}",
}


# --- protocol parsing ------------------------------------------------------

def parse_action(content: str) -> tuple[str, str, str]:
    """Return (kind, name, arg). kind ∈ {tool, final}. A reply that matches no
    protocol line is treated as a FINAL answer (graceful, never an infinite loop)."""
    for line in content.splitlines():
        s = line.strip()
        if s.upper().startswith("FINAL:"):
            return ("final", "", s[6:].strip())
        if s.upper().startswith("TOOL:"):
            body = s[5:].strip()
            name, _, arg = body.partition(" ")
            return ("tool", name.strip().lower(), arg.strip())
    return ("final", "", content.strip())


# --- the step ---------------------------------------------------------------

def _react_state(pcb: AgentControlBlock) -> dict:
    return pcb.scratch.setdefault("react", {"step": 0, "observations": []})


def run_react_step(pcb: AgentControlBlock, client: Optional[object] = None) -> bool:
    """One step of a multi-step agent. Returns True if more steps remain."""
    if pcb.is_terminal():
        return False
    if pcb.quota_remaining <= 0:
        pcb.transition(AgentState.ERROR)
        pcb.error = "per-agent quota exhausted"
        return False

    state = _react_state(pcb)
    if state["step"] >= pcb.capability.max_tool_calls:
        pcb.transition(AgentState.ERROR)
        pcb.error = f"exceeded max_tool_calls={pcb.capability.max_tool_calls}"
        return False

    if pcb.state != AgentState.RUNNING:
        pcb.transition(AgentState.RUNNING)

    if client is None:
        from gcos.backend.solar_client import SolarClient
        client = SolarClient()

    messages = _build_messages(pcb, state, client=client)
    try:
        result = client.chat(
            messages,
            max_tokens=min(pcb.capability.max_tokens, 512),
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
    pcb.tokens_used += getattr(result, "tokens", 0)

    # Cooperative cancellation (A4): killed mid-step → don't resurrect.
    if pcb.state == AgentState.ZOMBIE:
        return False

    kind, name, arg = parse_action(result.content)
    if state["step"] == 0:
        pcb.context_pages.append(
            ContextPage(role="user", content=pcb.prompt,
                        tokens=getattr(result, "prompt_tokens", 0))
        )
    pcb.context_pages.append(
        ContextPage(role="assistant", content=result.content,
                    tokens=getattr(result, "completion_tokens", 0))
    )
    state["step"] += 1

    if kind == "final":
        pcb.result = arg
        pcb.transition(AgentState.DONE)
        log.info("PID %d (%s) FINAL after %d steps", pcb.pid, pcb.name, state["step"])
        return False

    # Tool step: execute, record the observation, keep going.
    tool = TOOLS.get(name)
    obs = tool(arg, pcb) if tool else f"error: unknown tool '{name}'"
    state["observations"].append(f"{name}({arg}) -> {obs}")
    pcb.context_pages.append(
        ContextPage(role="user", content=f"OBSERVATION: {obs}")
    )
    log.debug("PID %d step=%d TOOL %s(%s) -> %s", pcb.pid, state["step"], name, arg, obs)
    return True


def _build_messages(pcb: AgentControlBlock, state: dict,
                    client: Optional[object] = None) -> list[dict]:
    pager = getattr(pcb, "pager", None)
    if pager is not None and hasattr(pager, "assemble"):
        # Reuse the kernel's pager so multi-step context stays within budget and
        # demand-paging/eviction actually fires for long agents. Thread the
        # worker's batched client through so a summarize-eviction here reuses the
        # OS's shared concurrency-capped batcher instead of a fresh one (B5).
        extra = pcb.prompt if state["step"] == 0 else None
        msgs = pager.assemble(pcb, client=client, extra_user_prompt=extra)
        return [{"role": "system", "content": SYSTEM}] + msgs
    # No pager: replay the recorded pages — including the agent's own assistant
    # TOOL turns, not just observations — so this path matches the pager path and
    # the model sees what it already tried. On step 0 the prompt page isn't
    # recorded yet, so add it explicitly.
    msgs: list[dict] = [{"role": "system", "content": SYSTEM}]
    if pcb.context_pages:
        msgs += [{"role": p.role, "content": p.content} for p in pcb.context_pages]
    else:
        msgs.append({"role": "user", "content": pcb.prompt})
    return msgs
