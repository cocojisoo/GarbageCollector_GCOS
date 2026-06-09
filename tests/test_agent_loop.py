"""Tests for the real multi-step (ReAct) agent loop and its interaction with
the scheduler — the A1 + A2 payoff: a *real* agent (not a synthetic eval runner)
is now time-sliced by the worker pool's quantum.
"""

from __future__ import annotations

from gcos.agent_loop import parse_action, run_react_step, TOOLS
from gcos.backend.solar_client import ChatResult
from gcos.kernel.pcb import AgentControlBlock, AgentState, CapabilitySet


# --- a deterministic, offline stand-in for Solar ----------------------------

class ScriptedClient:
    """Returns a fixed list of replies in order (one per LLM call)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, **kw) -> ChatResult:
        reply = self.replies[self.calls] if self.calls < len(self.replies) else "FINAL: done"
        self.calls += 1
        return ChatResult(content=reply, prompt_tokens=5, completion_tokens=5, total_tokens=10)


class ReActFakeClient:
    """Content-aware fake: takes `STEPS=n` tool steps then FINAL, regardless of
    how the scheduler interleaves it — so a multi-agent interleaving test is
    deterministic. Counts OBSERVATION turns already in the assembled context."""

    def chat(self, messages, **kw) -> ChatResult:
        obs = sum(1 for m in messages if str(m.get("content", "")).startswith("OBSERVATION:"))
        task = next((m["content"] for m in messages
                     if m["role"] == "user" and "STEPS=" in m["content"]), "STEPS=1")
        n = int(task.split("STEPS=")[1].split()[0])
        content = "FINAL: done" if obs >= n - 1 else "TOOL: note working"
        return ChatResult(content=content, prompt_tokens=5, completion_tokens=5, total_tokens=10)


def _agent(prompt: str, **kw) -> AgentControlBlock:
    return AgentControlBlock(pid=1, name="a", prompt=prompt,
                             capability=CapabilitySet.agent(**kw))


# --- protocol parsing -------------------------------------------------------

def test_parse_action_recognises_tool_and_final_and_bare():
    assert parse_action("TOOL: calc 6*7") == ("tool", "calc", "6*7")
    assert parse_action("FINAL: the answer is 42") == ("final", "", "the answer is 42")
    # A reply matching no protocol line is treated as a final answer (no loop).
    assert parse_action("just some prose")[0] == "final"


def test_calc_tool_is_a_real_safe_evaluator():
    assert TOOLS["calc"]("6*7", None) == "42"
    assert TOOLS["calc"]("2 ** 10", None) == "1024"
    assert "error" in TOOLS["calc"]("__import__('os')", None)  # no names/calls


# --- the loop itself --------------------------------------------------------

def test_multistep_agent_runs_many_steps_until_final():
    pcb = _agent("compute 6*7")
    client = ScriptedClient(["TOOL: calc 6*7", "TOOL: note got 42", "FINAL: 42"])
    # Step 1 and 2 return True (more work); step 3 finalises.
    assert run_react_step(pcb, client) is True
    assert run_react_step(pcb, client) is True
    assert run_react_step(pcb, client) is False
    assert pcb.state == AgentState.DONE
    assert pcb.result == "42"
    assert pcb.llm_calls_used == 3              # the scheduler now has 3 slices to time-slice
    # The calc observation actually went through the tool.
    obs = pcb.scratch["react"]["observations"]
    assert any("42" in o for o in obs)


def test_multistep_respects_max_tool_calls_cap():
    pcb = _agent("loop forever", max_tool_calls=3)
    client = ScriptedClient(["TOOL: note a", "TOOL: note b", "TOOL: note c", "TOOL: note d"])
    keep = True
    for _ in range(10):
        keep = run_react_step(pcb, client)
        if not keep:
            break
    assert pcb.state == AgentState.ERROR
    assert "max_tool_calls" in (pcb.error or "")


def test_multistep_errors_on_quota_exhaustion():
    pcb = _agent("x")
    pcb.quota_remaining = 0
    assert run_react_step(pcb, ScriptedClient(["FINAL: x"])) is False
    assert pcb.state == AgentState.ERROR


def test_multistep_threads_worker_client_into_pager():
    """Regression: summarize-eviction during a multi-step agent must reuse the
    worker's throttled client, not construct a fresh batcher off the pool."""
    from gcos.agent_loop import _build_messages

    seen = {}

    class FakePager:
        def assemble(self, pcb, *, client=None, extra_user_prompt=None):
            seen["client"] = client
            return [{"role": "user", "content": pcb.prompt}]

    pcb = _agent("x")
    pcb.pager = FakePager()
    sentinel = object()
    _build_messages(pcb, {"step": 0, "observations": []}, client=sentinel)
    assert seen["client"] is sentinel


def test_multistep_fallback_replays_assistant_tool_turns():
    """Regression: the no-pager path must replay the agent's own assistant TOOL
    turns (from context_pages), not just observations, so it matches the pager
    path and the model sees what it already tried."""
    from gcos.agent_loop import _build_messages
    pcb = _agent("compute")  # pager is None
    run_react_step(pcb, ScriptedClient(["TOOL: calc 1+1", "FINAL: 2"]))
    msgs = _build_messages(pcb, pcb.scratch["react"])
    assert any(m["role"] == "assistant" and "calc" in m["content"] for m in msgs)


# --- A1 + A2: real agent time-sliced by the scheduler -----------------------

def _run_two_multistep_agents(scheduler: str):
    """Drive two real multi-step agents through the real worker pool with one
    worker, recording the per-call dispatch order."""
    from gcos.executor import run_step
    from gcos.kernel.kernel import Kernel, KernelConfig

    order: list[int] = []

    def recording_runner(pcb, client):
        order.append(pcb.pid)
        return run_step(pcb, client)

    cfg = KernelConfig(scheduler=scheduler, workers=1, quota_total=100)
    k = Kernel(cfg, client_factory=lambda: ReActFakeClient(), step_runner=recording_runner)
    # Enqueue both agents before starting the single worker so the dispatch order
    # is decided by the scheduler, not by a worker-start vs second-spawn race.
    p1 = k.spawn("TASK STEPS=3 alpha", name="a1", capability=CapabilitySet.agent())
    p2 = k.spawn("TASK STEPS=3 beta", name="a2", capability=CapabilitySet.agent())
    k.start()
    k.wait_idle(timeout=10)
    k.shutdown()
    return p1, p2, order


def test_fcfs_runs_each_multistep_agent_to_completion():
    p1, p2, order = _run_two_multistep_agents("fcfs")
    # Non-preemptive: agent p1's three calls all happen before p2 starts.
    assert order == [p1, p1, p1, p2, p2, p2]


def test_rr_time_slices_real_multistep_agents():
    p1, p2, order = _run_two_multistep_agents("rr")
    # Preemptive quantum=2: p1 runs 2 calls, yields; p2 runs 2; p1 finishes its
    # 3rd; p2 finishes its 3rd. The two real agents interleave — the thing the
    # single-shot model could never show.
    assert order == [p1, p1, p2, p2, p1, p2]
    # Both completed all three of their steps.
    assert order.count(p1) == 3 and order.count(p2) == 3
