from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import analysis, betting as betting_api, history, models, patterns, risk as risk_api, signals, statistics
from app.betting.config import get_betting_settings
from app.betting.session import BettingManager, configure_default_manager
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine
from app.risk.engine import RiskManager
from app.history_collector import HistoryCollectorManager

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
            # File parsing, database sync and statistical analysis are
            # synchronous/CPU-bound. Keep them off FastAPI's event loop so
            # health checks and UI requests remain responsive.
            await asyncio.to_thread(app.state.wp.reload)
            analysis_payload = await asyncio.to_thread(app.state.wp.current_analysis)
            await manager.broadcast(
                {
                    "type": "updated_analysis",
                    "new_round": analysis_payload.get("recent_multipliers", [])[-1:],
                    "analysis": analysis_payload,
                    "system_status": {"valid_rounds": int(len(app.state.wp.rounds))},
                }
            )
            history = app.state.history_collector
            status = history.status()
            await manager.broadcast({"type": "history:new_round", "round": status["latest"]})
            await manager.broadcast({"type": "history:updated", "history": {"count": status["count"]}})
            await manager.broadcast({"type": "history:stats", "stats": history.stats()})


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
    app.state.risk = RiskManager(
        broadcaster=manager.broadcast,
        betting_manager=betting_manager,
    )
    app.state.history_collector = HistoryCollectorManager(settings.data_path, manager.broadcast)
    await app.state.history_collector.start()

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
        await app.state.history_collector.stop()


app = FastAPI(title="Winner Predict", description="STATISTICAL PATTERN ANALYSIS platform for Aviator round history research.", version="1.0.0", lifespan=lifespan)
settings = get_settings()


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Require a bearer token when API_KEY is configured.

    Authentication is opt-in so existing local development and tests continue
    to work. Production deployments should always set API_KEY.
    """
    if settings.api_key and request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {settings.api_key}"
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API credentials"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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
app.include_router(risk_api.router)


@app.get("/", tags=["system"])
async def root():
    """Human-friendly landing response for direct browser visits."""
    return {
        "ok": True,
        "service": "Winner Predict API",
        "status": "running",
        "docs": "/docs",
        "betting_status": "/api/betting/status",
        "risk_status": "/api/risk/status",
        "frontend": "http://localhost:5173",
    }


@app.get("/health", tags=["system"])
async def health():
    return {"ok": True, "status": "healthy"}


@app.get("/api/system/status", tags=["system"])
async def system_status(request: Request):
    """Expose non-sensitive runtime configuration and dataset freshness."""
    wp = request.app.state.wp
    # Freshness metadata is already available from the loader; do not wait on
    # statistical analysis while the collector is updating a large dataset.
    quality = wp.quality.model_dump()
    cfg = wp.settings
    return {
        "ok": True,
        "configuration": {
            "target_multiplier": cfg.target_multiplier,
            "minimum_sample_size": cfg.min_sample_size,
            "signal_threshold": cfg.signal_threshold,
            "strong_signal_threshold": cfg.strong_signal_threshold,
            "api_security_enabled": bool(cfg.api_key),
        },
        "dataset": {
            "source": cfg.data_path.name,
            "valid_records": quality.get("valid_records", 0),
            "first_timestamp": quality.get("first_timestamp"),
            "last_timestamp": quality.get("last_timestamp"),
            "last_round_index": quality.get("last_round_index"),
        },
    }


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    if settings.api_key:
        supplied = websocket.query_params.get("token", "")
        if not hmac.compare_digest(supplied, settings.api_key):
            await websocket.close(code=1008, reason="Invalid API credentials")
            return
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
