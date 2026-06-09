"""Coder agent — the executor path for agents with `capability.can_exec_code`.

Flow:
    1. policy_gate.scan_prompt(pcb.prompt)
       └── DENY → ERROR
    2. Solar chat (with a system prompt that asks for a ```python``` block)
    3. extract_python(reply)
       └── nothing extracted → DONE with reply as-is (LLM didn't propose code)
    4. policy_gate.scan_code(code)
       └── DENY → ERROR (logged) — code is also stored on PCB for audit
    5. sandbox_runner.run_python(code)
    6. merge sandbox stdout/stderr into pcb.result

Capability checks happen *before* steps 5 and (implicitly) 1: if the agent
doesn't have `can_exec_code`, we just return the LLM reply text without
touching the sandbox at all.

This module is independent of the worker pool — the worker just calls
`run_coder_step` instead of the plain `run_step` when the agent's capability
says it's a coder.

Scope (F17): like the plain path, the coder is **single-shot** — one LLM call,
extract+run once, then terminal. It does not iterate (read sandbox output →
revise code → re-run). That tool-use loop is future work; see executor.py.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Optional

from gcos.backend.solar_client import SolarClient
from gcos.executor import _pager_for
from gcos.kernel.pcb import AgentControlBlock, AgentState, ContextPage
from gcos.sandbox import extract_python, make_runner, scan_code, scan_prompt
from gcos.sandbox.runner import SandboxRunner


log = logging.getLogger(__name__)


CODER_SYSTEM = textwrap.dedent("""\
    You are a Python coder agent running inside GCOS, a sandboxed agent OS.
    When given a task, reply with exactly one fenced Python block:

        ```python
        # your code here
        print(...)
        ```

    Constraints (enforced by the kernel — violations are auto-rejected):
      - No network calls, no subprocess, no os.system, no eval/exec.
      - Use only the Python standard library.
      - Read from stdin is unavailable; write your answer to stdout via print().
      - Keep the code short and self-contained.
""").strip()


def _has_coder_system_pinned(pcb: AgentControlBlock) -> bool:
    return any(p.role == "system" and p.pinned for p in pcb.context_pages)


def _ensure_coder_system_pinned(pcb: AgentControlBlock) -> None:
    if not _has_coder_system_pinned(pcb):
        pcb.context_pages.insert(
            0, ContextPage(role="system", content=CODER_SYSTEM, pinned=True)
        )


def _format_sandbox_result(reply: str, sandbox_out) -> str:
    parts = [reply.rstrip(), ""]
    parts.append(f"--- sandbox: {sandbox_out.short_summary()} ---")
    if sandbox_out.stdout:
        parts.append("[stdout]")
        parts.append(sandbox_out.stdout.rstrip())
    if sandbox_out.stderr:
        parts.append("[stderr]")
        parts.append(sandbox_out.stderr.rstrip())
    return "\n".join(parts)


def run_coder_step(
    pcb: AgentControlBlock,
    client: Optional[SolarClient] = None,
    sandbox: Optional[SandboxRunner] = None,
) -> bool:
    """One full coder turn. Returns False (single-shot in M3)."""
    client = client or SolarClient()

    if pcb.is_terminal():
        return False

    # 1. Prompt-side policy gate (free — runs before any API spend)
    gate_in = scan_prompt(pcb.prompt)
    if not gate_in.allowed:
        pcb.transition(AgentState.ERROR)
        pcb.error = f"policy_gate.prompt: {gate_in.reason} (matched {gate_in.matched!r})"
        log.warning("PID %d denied at prompt gate: rule=%s", pcb.pid, gate_in.rule)
        return False

    if pcb.quota_remaining <= 0:
        pcb.transition(AgentState.ERROR)
        pcb.error = "per-agent quota exhausted"
        return False

    pcb.transition(AgentState.RUNNING)
    _ensure_coder_system_pinned(pcb)

    # 2. LLM call — pager assembles within token budget, applying LRU /
    # summarize / swap as needed. Pass the worker's batched client through so a
    # summarize-eviction goes via the OS throttle, not a bypass client (B5).
    pager = _pager_for(pcb)
    messages = pager.assemble(pcb, client=client, extra_user_prompt=pcb.prompt)
    try:
        result = client.chat(
            messages,
            max_tokens=min(pcb.capability.max_tokens, 1024),
            timeout=pcb.timeout_s,
        )
    except TimeoutError as e:
        pcb.transition(AgentState.TIMEOUT); pcb.error = str(e); return False
    except Exception as e:  # noqa: BLE001
        pcb.transition(AgentState.ERROR); pcb.error = f"{type(e).__name__}: {e}"; return False

    pcb.quota_remaining -= 1
    pcb.llm_calls_used += 1
    pcb.tokens_used += result.tokens

    # Cooperative cancellation (A4): killed mid-call → discard, don't sandbox.
    if pcb.state == AgentState.ZOMBIE:
        log.info("PID %d killed mid-step; discarding coder result", pcb.pid)
        return False

    reply = result.content

    # Persist exchange for context
    if pcb.llm_calls_used == 1:
        pcb.context_pages.append(
            ContextPage(role="user", content=pcb.prompt, tokens=result.prompt_tokens)
        )
    pcb.context_pages.append(
        ContextPage(role="assistant", content=reply, tokens=result.completion_tokens)
    )

    # 3. Extract code
    code = extract_python(reply)

    # 4. Capability check + code-side policy gate + sandbox
    if not code:
        # LLM answered in prose only — that's fine for "explain X" style prompts
        pcb.result = reply
        pcb.transition(AgentState.DONE)
        return False

    if not pcb.capability.can_exec_code:
        pcb.result = reply + "\n\n--- sandbox: SKIPPED (capability.can_exec_code=False) ---"
        pcb.transition(AgentState.DONE)
        return False

    gate_code = scan_code(code)
    if not gate_code.allowed:
        pcb.error = (
            f"policy_gate.code: {gate_code.reason} (matched {gate_code.matched!r}, "
            f"rule={gate_code.rule})"
        )
        pcb.result = reply + f"\n\n--- sandbox: BLOCKED ({gate_code.rule}) ---"
        pcb.transition(AgentState.ERROR)
        log.warning("PID %d code blocked: rule=%s", pcb.pid, gate_code.rule)
        return False

    # 5. Sandbox
    sb = sandbox or make_runner()
    sandbox_out = sb.run_python(code, timeout=min(pcb.timeout_s, 10.0))
    pcb.result = _format_sandbox_result(reply, sandbox_out)
    if sandbox_out.ok:
        pcb.transition(AgentState.DONE)
    else:
        pcb.error = f"sandbox: {sandbox_out.short_summary()}"
        pcb.transition(AgentState.ERROR)
    return False
