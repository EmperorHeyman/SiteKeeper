"""Sitekeeper backend entrypoint.

Run standalone for development:
    python -m uvicorn app.main:app --reload --port 8766

When launched by the Tauri shell, MYSQLRUNNER_PORT / MYSQLRUNNER_TOKEN are
injected and the token header becomes mandatory.

The heavy lifting lives in the mysql_runner package - the same code the PyQt
build uses - so both front ends share one vault, one settings file and one set
of tested protocol backends.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api import servers, sql, tools, transfer, vault
from app.core.config import allowed_origins, env_summary, get_settings
from app.core.state import state
from app.core.ws import hub
from app.services import mysql_service, transfer_service


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Worker threads push progress through the hub, so it needs the loop.
    hub.bind_loop(asyncio.get_running_loop())
    # A password-free vault (or a keyring-cached one) opens with no prompt.
    state.try_auto_unlock()
    try:
        yield
    finally:
        mysql_service.manager.close_all()
        transfer_service.manager.close_all()
        state.lock()


settings = get_settings()
app = FastAPI(title="Sitekeeper", version=settings.version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(vault.router)
app.include_router(servers.router)
app.include_router(sql.router)
app.include_router(transfer.router)
app.include_router(tools.router)


@app.get("/health")
def health() -> dict:
    """Liveness probe the shell polls before showing the window."""
    return {
        "status": "ok",
        **env_summary(),
        "vault": state.status(),
        "clients": hub.client_count,
    }


@app.websocket("/events")
async def events(ws: WebSocket) -> None:
    """Live backend events: transfer progress, queue completion, errors.

    The token cannot travel in a header here (browsers do not allow custom
    headers on a WebSocket handshake), so it comes in as a query parameter.
    """
    expected = get_settings().token
    if expected and ws.query_params.get("token") != expected:
        await ws.close(code=1008)
        return
    await hub.connect(ws)
    try:
        while True:
            # The client never sends anything meaningful; this just parks the
            # coroutine until it disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)


def _ensure_std_streams() -> None:
    """Give the logger somewhere safe to write before uvicorn starts.

    Frozen with console=False, sys.stdout and sys.stderr are None unless the
    parent process supplied pipes. uvicorn installs a StreamHandler on them
    during startup, and the windowed build then never finishes binding the port
    - it just sits there. Tauri does pipe both streams, which is exactly why
    this stayed hidden until the sidecar was launched without them.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        usable = False
        if stream is not None:
            try:
                stream.write("")
                stream.flush()
                usable = True
            except Exception:
                usable = False
        if not usable:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", buffering=1))


def main() -> None:
    """Console-script entrypoint used by the packaged sidecar."""
    _ensure_std_streams()

    import uvicorn

    conf = get_settings()
    port = int(os.environ.get("MYSQLRUNNER_PORT", conf.port))
    uvicorn.run(
        app,
        host=conf.host,
        port=port,
        log_level="warning",
        access_log=False,
        ws_ping_interval=None,
    )


if __name__ == "__main__":
    main()
