"""
FastAPI application factory
===========================

Data source: roundhistory.json only.
No live game feed, no crash simulator, no WebSocket auto-prediction loop.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import configure_logging, get_logger, settings
from config.settings import settings as app_settings
from src.api.services import build_services
from src.api.routes import predictions, metrics, dashboard, monitoring
from src.api.websocket import router as ws_router
from src.core.exceptions import AviatorBaseError

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "Starting Aviator ML Enterprise",
        extra={"version": app_settings.app_version,
               "data": str(app_settings.round_history_path)},
    )
    services = build_services()
    app.state.services = services

    # Pre-warm: try to load the active model so the first prediction is fast
    try:
        await services.prediction_service.load_active("xgboost")
        logger.info("Model pre-warmed successfully")
    except Exception as exc:
        logger.warning("Model pre-warm skipped (train a model first): %s", exc)

    yield

    # Graceful shutdown
    try:
        await services.continuous_learning.stop()
    except Exception:
        pass
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Aviator ML Enterprise API",
        version=app_settings.app_version,
        description=(
            "Production-grade ML platform for Aviator round prediction. "
            "All predictions are derived from roundhistory.json — no live game feed."
        ),
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers
    app.include_router(predictions.router)
    app.include_router(metrics.router)
    app.include_router(dashboard.router)
    app.include_router(monitoring.router)
    app.include_router(ws_router)          # metrics-push WebSocket only

    # Serve the dashboard as static files
    dashboard_dir = Path(__file__).resolve().parent.parent.parent / "dashboard"
    if dashboard_dir.exists():
        app.mount("/static", StaticFiles(directory=str(dashboard_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def root_index():
            return FileResponse(str(dashboard_dir / "index.html"))

    # Domain exception → proper HTTP response
    @app.exception_handler(AviatorBaseError)
    async def handle_domain_error(request: Request, exc: AviatorBaseError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.get("/health", include_in_schema=False)
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "version": app_settings.app_version}

    return app


app = create_app()
