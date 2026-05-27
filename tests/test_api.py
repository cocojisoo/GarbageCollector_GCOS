"""End-to-end API tests using FastAPI TestClient (no Solar)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gcos.api.server import create_app
from gcos.kernel import AgentState, Kernel, KernelConfig


def fake_runner(pcb, _client) -> bool:
    pcb.transition(AgentState.RUNNING)
    pcb.llm_calls_used += 1
    pcb.tokens_used += 7
    pcb.result = f"reply-{pcb.pid}"
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


def test_spawn_then_list_shows_agent():
    with _client() as c:
        r = c.post("/api/spawn", json={"prompt": "hello", "name": "t1"})
        assert r.status_code == 200
        pid = r.json()["pid"]
        # Wait for worker pool to finish (fake_runner is fast)
        c.app.state.kernel.wait_idle(timeout=2.0)

        r = c.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()["agents"]
        assert len(agents) == 1
        assert agents[0]["pid"] == pid
        assert agents[0]["state"] == "DONE"
        assert agents[0]["result"] == f"reply-{pid}"


def test_get_agent_404_for_unknown_pid():
    with _client() as c:
        assert c.get("/api/agents/9999").status_code == 404


def test_kill_then_409_on_second_kill():
    with _client() as c:
        # Start a kernel with no workers so the agent is forever READY
        c.app.state.kernel.shutdown()
        c.app.state.kernel = Kernel(
            KernelConfig(scheduler="fcfs", workers=0, quota_total=10),
            client_factory=lambda: None,
            step_runner=fake_runner,
        )
        # We didn't start() — workers aren't running, agent stays READY
        r = c.post("/api/spawn", json={"prompt": "x"})
        pid = r.json()["pid"]
        assert c.delete(f"/api/agents/{pid}").status_code == 200
        # second kill: already terminal (ZOMBIE)
        assert c.delete(f"/api/agents/{pid}").status_code == 409


def test_kernel_status_endpoint():
    with _client() as c:
        for i in range(3):
            c.post("/api/spawn", json={"prompt": f"p{i}"})
        c.app.state.kernel.wait_idle(timeout=2.0)
        r = c.get("/api/kernel/status")
        assert r.status_code == 200
        s = r.json()
        assert s["scheduler"] == "fcfs"
        assert s["total_agents"] == 3
        assert s["by_state"]["DONE"] == 3
        assert s["quota"]["used"] == 3


def test_quota_topup_endpoint():
    with _client() as c:
        before = c.get("/api/kernel/status").json()["quota"]["total"]
        r = c.post("/api/kernel/quota/topup?amount=25")
        assert r.status_code == 200
        assert r.json()["total"] == before + 25
