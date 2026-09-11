from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis, betting as betting_api, history, models, patterns, signals, statistics
from app.betting.config import get_betting_settings
from app.betting.session import BettingManager, configure_default_manager
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine

configure_logging()
log = get_logger("APP")


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def broadcast(self, payload: dict):
        stale = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


manager = ConnectionManager()


async def monitor_file(app: FastAPI):
    monitor = MonitoringEngine(app.state.wp.settings.data_path)
    while True:
        await asyncio.sleep(3)
        if monitor.has_changed():
            app.state.wp.reload()
            analysis_payload = app.state.wp.current_analysis()
            await manager.broadcast(
                {
                    "type": "updated_analysis",
                    "new_round": analysis_payload.get("recent_multipliers", [])[-1:],
                    "analysis": analysis_payload,
                    "system_status": {"valid_rounds": int(len(app.state.wp.rounds))},
                }
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.wp = AppState(settings)
    log.info("[DATA] Loaded %s rounds", len(app.state.wp.rounds))

    # Betting automation module (Part 1 — read-only browser verification).
    betting_manager = BettingManager(
        get_betting_settings(),
        broadcaster=manager.broadcast,
    )
    app.state.betting = betting_manager
    configure_default_manager(betting_manager)

    task = asyncio.create_task(monitor_file(app))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await betting_manager.shutdown()


app = FastAPI(title="Winner Predict", description="STATISTICAL PATTERN ANALYSIS platform for Aviator round history research.", version="1.0.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(statistics.router)
app.include_router(analysis.router)
app.include_router(signals.router)
app.include_router(history.router)
app.include_router(patterns.router)
app.include_router(models.router)
app.include_router(betting_api.router)


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "system_status", "status": "connected", "label": "STATISTICAL PATTERN ANALYSIS"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

