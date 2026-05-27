"""SSE / extra endpoints — quick smoke tests via TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gcos.api.server import create_app
from gcos.kernel import AgentState, Kernel, KernelConfig


def fake_runner(pcb, _client) -> bool:
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.tokens_used += 5
    pcb.result = f"ok-{pcb.pid}"
    pcb.transition(AgentState.DONE)
    return False


def _client():
    kernel = Kernel(
        KernelConfig(scheduler="fcfs", workers=2, quota_total=50),
        client_factory=lambda: None,
        step_runner=fake_runner,
    )
    app = create_app(kernel)
    return TestClient(app)


def test_log_endpoint_returns_recent_entries():
    with _client() as c:
        # Spawn a couple of agents to generate log lines
        for _ in range(2):
            c.post("/api/spawn", json={"prompt": "x"})
        c.app.state.kernel.wait_idle(timeout=2.0)
        r = c.get("/api/log?limit=20")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert isinstance(entries, list)
        # We expect at least the "spawned" log lines from the kernel
        msgs = " ".join(e.get("msg", "") for e in entries)
        assert "spawned" in msgs


def test_batcher_stats_endpoint_handles_fake_client():
    with _client() as c:
        r = c.get("/api/batcher/stats")
        assert r.status_code == 200
        # Test kernel uses a fake client_factory → batcher field is None
        assert r.json()["batcher"] is None


def test_kernel_status_now_includes_trace_size_and_bus():
    with _client() as c:
        c.post("/api/spawn", json={"prompt": "x"})
        c.app.state.kernel.wait_idle(timeout=2.0)
        s = c.get("/api/kernel/status").json()
        assert "trace_size" in s and s["trace_size"] > 0
        assert "bus_pending" in s
        # batcher slot is present (None when fake client is used)
        assert "batcher" in s
