"""Resource-accounting tests (category B): summarize must go through the OS's
Solar throttle and be charged to the OS budget; the pager must be per-kernel."""

from __future__ import annotations

import time

import pytest

from gcos.backend.solar_client import ChatResult
from gcos.kernel import Kernel, KernelConfig
from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.kernel.quota import Quota
from gcos.memory.context_pager import ContextPager
from gcos.memory.evict_summarize import SummarizeEvictionPolicy


class RecordingClient:
    """A stand-in batcher: records that the summarize call went through *it*."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **_kw):
        self.calls += 1
        return ChatResult(content="[summary]", completion_tokens=8, total_tokens=8)


def _over_budget_pcb(n=6, tokens=30) -> AgentControlBlock:
    pcb = AgentControlBlock(pid=1, name="m", prompt="x")
    pcb.context_pages = [
        ContextPage(role="user" if i % 2 == 0 else "assistant",
                    content=f"turn {i}", tokens=tokens, last_access=float(i))
        for i in range(n)
    ]
    return pcb


# --- B5: summarize uses the passed client and charges quota -----------------

def test_summarize_uses_passed_client_not_a_bypass():
    pcb = _over_budget_pcb()
    rec = RecordingClient()
    freed = SummarizeEvictionPolicy(batch_size=4).evict(pcb, client=rec)
    assert freed == 3            # 4 pages compressed into 1
    assert rec.calls == 1        # went through the client we handed in


def test_summarize_charges_quota():
    pcb = _over_budget_pcb()
    rec = RecordingClient()
    quota = Quota(10)
    SummarizeEvictionPolicy(batch_size=4, quota=quota).evict(pcb, client=rec)
    assert quota.snapshot()["used"] == 1   # the OS budget recorded the call


def test_summarize_skipped_when_quota_exhausted():
    pcb = _over_budget_pcb()
    rec = RecordingClient()
    quota = Quota(0)
    freed = SummarizeEvictionPolicy(batch_size=4, quota=quota).evict(pcb, client=rec)
    assert freed == 0
    assert rec.calls == 0       # never spent a call we couldn't afford


def test_summarize_refunds_quota_on_failure():
    pcb = _over_budget_pcb()
    quota = Quota(5)

    class Boom:
        def chat(self, *_a, **_k):
            raise RuntimeError("solar down")

    with pytest.raises(RuntimeError):
        SummarizeEvictionPolicy(batch_size=4, quota=quota).evict(pcb, client=Boom())
    assert quota.snapshot()["used"] == 0   # reserved unit was handed back


def test_pager_threads_client_into_summarize():
    pcb = _over_budget_pcb(n=8, tokens=40)
    rec = RecordingClient()
    pager = ContextPager(
        budget_tokens=120,
        policies=[SummarizeEvictionPolicy(batch_size=4)],
    )
    pager.assemble(pcb, client=rec)
    assert rec.calls >= 1       # assemble(client=...) reached the policy


# --- H22: pager is per-kernel, not a shared process-global ------------------

def _fake_runner(pcb, _c):
    from gcos.kernel.pcb import AgentState
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.transition(AgentState.DONE)
    return False


def test_each_kernel_owns_its_pager():
    k1 = Kernel(KernelConfig(workers=0, context_budget_tokens=1000),
                client_factory=lambda: None, step_runner=_fake_runner)
    k2 = Kernel(KernelConfig(workers=0, context_budget_tokens=2000),
                client_factory=lambda: None, step_runner=_fake_runner)
    assert k1.pager is not k2.pager
    assert k1.pager.budget == 1000
    assert k2.pager.budget == 2000


def test_spawn_attaches_kernel_pager_to_pcb():
    k = Kernel(KernelConfig(workers=0, context_budget_tokens=777),
               client_factory=lambda: None, step_runner=_fake_runner)
    pid = k.spawn("hi")
    assert k.get(pid).pager is k.pager
