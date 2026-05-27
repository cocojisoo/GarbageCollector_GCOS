"""FastAPI app factory + static file mount.

`create_app(kernel)` returns an app pre-wired to a kernel instance. Tests
use this directly; the `serve` CLI command starts a uvicorn around it.
"""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gcos.api.routes import router
from gcos.api.sse import router as sse_router
from gcos.kernel import Kernel, KernelConfig


WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"


def create_app(kernel: Optional[Kernel] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.kernel is None:
            app.state.kernel = Kernel(KernelConfig.from_env())
        app.state.kernel.start()
        try:
            yield
        finally:
            app.state.kernel.shutdown()

    app = FastAPI(title="GCOS", version="0.2.0", lifespan=lifespan)
    app.state.kernel = kernel  # may be None — lifespan will build a default

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    app.include_router(sse_router, prefix="/api")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def root() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

    return app
