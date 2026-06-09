"""HTTP routes for GCOS.

The kernel lives in `app.state.kernel`. These handlers translate HTTP into
kernel calls and back.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter()


class SpawnRequest(BaseModel):
    prompt: str
    name: str = "anon"
    priority: int = Field(5, ge=0, le=9)
    timeout_s: Optional[float] = None
    quota: Optional[int] = None
    parent_pid: Optional[int] = None
    pipe_to: Optional[int] = None


class SpawnResponse(BaseModel):
    pid: int


def _row(pcb) -> dict:
    """Add a few extra fields beyond pcb.to_row() for the dashboard."""
    base = pcb.to_row()
    base["result"] = pcb.result
    base["error"] = pcb.error
    base["prompt"] = pcb.prompt
    base["pipe_to"] = pcb.pipe_to
    base["children"] = pcb.children
    return base


@router.post("/spawn", response_model=SpawnResponse)
def spawn(req: SpawnRequest, request: Request) -> SpawnResponse:
    k = request.app.state.kernel
    pid = k.spawn(
        req.prompt,
        name=req.name,
        priority=req.priority,
        timeout_s=req.timeout_s,
        quota=req.quota,
        parent_pid=req.parent_pid,
        pipe_to=req.pipe_to,
    )
    return SpawnResponse(pid=pid)


@router.get("/agents")
def list_agents(request: Request) -> dict:
    k = request.app.state.kernel
    return {"agents": [_row(p) for p in k.list_all()]}


@router.get("/agents/{pid}")
def get_agent(pid: int, request: Request) -> dict:
    k = request.app.state.kernel
    pcb = k.get(pid)
    if pcb is None:
        raise HTTPException(status_code=404, detail=f"PID {pid} not found")
    return _row(pcb)


@router.delete("/agents/{pid}")
def kill_agent(pid: int, request: Request) -> dict:
    k = request.app.state.kernel
    ok = k.kill(pid)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"PID {pid} cannot be killed (not found or already terminal)",
        )
    return {"pid": pid, "killed": True}


@router.get("/kernel/status")
def kernel_status(request: Request) -> dict:
    k = request.app.state.kernel
    return k.status()


@router.post("/kernel/quota/topup")
def quota_topup(amount: int, request: Request) -> dict:
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    k = request.app.state.kernel
    k.quota.topup(amount)
    return k.quota.snapshot()


@router.post("/kernel/reap")
def kernel_reap(request: Request) -> dict:
    """Reap finished agents (drop them from the table + free mailboxes)."""
    k = request.app.state.kernel
    return {"reaped": k.reap_terminal()}
