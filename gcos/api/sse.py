"""Server-Sent Events endpoints — kernel state pushed to the dashboard.

`/api/events` emits a JSON snapshot every `interval_s` seconds containing
status + the agent list. The browser hooks it up with `EventSource()`. No
polling, no thundering herd; closing the tab just cleans the generator up.

We also emit on-demand events when state changes happen (sandbox blocks,
agent terminations) — but for simplicity M5 uses a fixed periodic push.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse


log = logging.getLogger(__name__)


router = APIRouter()


def _row(pcb) -> dict:
    base = pcb.to_row()
    base["result"] = pcb.result
    base["error"] = pcb.error
    base["pipe_to"] = pcb.pipe_to
    base["children"] = pcb.children
    return base


def _snapshot(kernel) -> dict:
    return {
        "status": kernel.status(),
        "agents": [_row(p) for p in kernel.list_all()],
    }


@router.get("/events")
async def events(request: Request, interval_s: float = 0.5) -> EventSourceResponse:
    kernel = request.app.state.kernel

    async def stream() -> AsyncIterator[dict]:
        last_payload: str | None = None
        while True:
            if await request.is_disconnected():
                log.debug("SSE client disconnected")
                break
            snap = _snapshot(kernel)
            data = json.dumps(snap, default=str)
            # Deduplicate: only push when something changed (saves bytes when idle)
            if data != last_payload:
                yield {"event": "snapshot", "data": data}
                last_payload = data
            await asyncio.sleep(interval_s)

    return EventSourceResponse(stream())


@router.get("/log")
def log_snapshot(request: Request, limit: int = 100) -> dict:
    kernel = request.app.state.kernel
    return {"entries": kernel.trace.snapshot(limit=limit)}


@router.get("/batcher/stats")
def batcher_stats(request: Request) -> dict:
    kernel = request.app.state.kernel
    client = getattr(kernel.pool, "client", None)
    if client is None or not hasattr(client, "stats"):
        return {"batcher": None}
    return {"batcher": client.stats}
