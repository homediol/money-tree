from __future__ import annotations

import asyncio
import hmac
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import analysis, backtesting, betting as betting_api, data, decisions, evidence, history, health, ml, models, patterns, research, results, risk as risk_api, shadow, signals, statistics, live as live_api, operations as operations_api, recovery as recovery_api
from app.betting.config import get_betting_settings
from app.betting.session import BettingManager, configure_default_manager
from app.betting.schemas import DecisionIntent
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.runtime import InstanceLock
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine
from app.services.system_health import SystemHealth
from app.services.system_orchestrator import SystemOrchestrator
from app.shadow import ShadowManager
from app.risk.engine import RiskManager
from app.history_collector import HistoryCollectorManager
from app.decision.engine import DecisionEngine
from app.reconciliation import ReconciliationService
from app.live import LiveActivationManager
from app.services.operations import OperationsManager
from app.services.disaster_recovery import DisasterRecoveryManager

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


async def process_history_update(app: FastAPI):
    """Process one Part 3 history event and publish dependent live views."""
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
    if status.get("latest") and hasattr(app.state, "shadow"):
        await app.state.shadow.process_round(app, status["latest"])
    if status.get("latest") and hasattr(app.state, "reconciliation"):
        await app.state.reconciliation.reconcile_round(status["latest"])
    dataset = app.state.wp.dataset_service
    await manager.broadcast({"type": "data:updated", "quality": dataset.quality, "dataset": dataset.status()})
    await manager.broadcast({"type": "features:updated", "features": dataset.latest_features()})
    pattern_report = await asyncio.to_thread(app.state.wp.pattern_report)
    await manager.broadcast({"type": "patterns:updated", "baseline": pattern_report["baseline"],
                             "patterns": pattern_report["patterns"][:20]})
    registry = app.state.wp.model_registry
    # The live contract exposes why inference is suppressed before any
    # Evidence/Decision/Risk work is considered. Legacy test doubles may only
    # implement predict_latest; production always provides prediction_payload.
    if hasattr(registry, "prediction_payload"):
        inference = await asyncio.to_thread(registry.prediction_payload, dataset)
        await manager.broadcast({"type": "prediction:status", **inference})
        prediction = inference["prediction"] if inference["usable"] else None
    else:
        prediction = await asyncio.to_thread(registry.predict_latest, dataset)
    if prediction:
        await manager.broadcast({"type": "prediction:new", "prediction": prediction})
        snapshot = await asyncio.to_thread(app.state.wp.build_evidence, prediction)
        if snapshot:
            await manager.broadcast({"type": "prediction:evidence_updated", "evidence": snapshot})
            betting_status = app.state.betting.status()
            betting_status["emergency_stop"] = app.state.risk.emergency_latched
            if not dataset.clean_rounds.empty:
                betting_status["latest_history_round_id"] = str(dataset.clean_rounds.iloc[-1]["round_id"])
            decision = await app.state.decision_engine.evaluate(
                prediction, snapshot,
                model_status=app.state.wp.model_registry.status(dataset),
                betting_status=betting_status,
                risk_manager=app.state.risk,
            )
            await manager.broadcast({"type": "decision:updated", "decision": decision})
            if decision.get("status") == "READY_FOR_EXECUTION":
                # Internal queue handoff. The executor still performs its own
                # lock/contract/browser validation before touching the page.
                current = app.state.betting.status()
                if current.get("mode") == "REAL":
                    live_gate = app.state.live.can_execute_live_bet(decision, current)
                    if not live_gate.get("allowed"):
                        await manager.broadcast({"type": "execution:rejected", "decision_id": decision.get("decision_id"), "reasons": live_gate.get("reasons", [])})
                        return
                blocks = app.state.risk.validate_execution(decision, current)
                if not blocks and str(decision.get("source_round_id")) == str(
                        dataset.clean_rounds.iloc[-1]["round_id"]):
                    approved = decision.get("risk") or decision.get("risk_evaluation") or {}
                    try:
                        await app.state.betting.submit_decision(DecisionIntent(
                            decision_id=decision["decision_id"],
                            round_id=decision["target_round_id"], execute=True,
                            profile=decision["profile"],
                            cashout=approved["cashout_target"],
                            bet_amount=approved["approved_bet_amount"],
                            expires_at=decision["expires_at"], source="part9-queue",
                        ))
                    except Exception as exc:
                        log.warning("[EXECUTION] queue handoff refused decision=%s: %s",
                                    decision.get("decision_id"), exc)
                elif blocks:
                    await manager.broadcast({"type": "execution:rejected",
                                             "decision_id": decision.get("decision_id"),
                                             "reasons": blocks})


async def monitor_file(app: FastAPI):
    monitor = MonitoringEngine(app.state.wp.settings.data_path)
    while True:
        try:
            await asyncio.sleep(3)
            if monitor.has_changed():
                # File parsing, database sync and statistical analysis are
                # synchronous/CPU-bound. Keep them off FastAPI's event loop so
                # health checks and UI requests remain responsive.
                await process_history_update(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def monitor_health(app: FastAPI):
    while True:
        try:
            await app.state.system_health.enforce_safety()
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system health monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def monitor_operations(app: FastAPI):
    while True:
        try:
            await app.state.operations.evaluate()
        except Exception as exc:
            log.error("operations evaluation failed: %s", exc)
        await asyncio.sleep(15)


async def initialize_app_state(settings, attempts: int = 3) -> AppState:
    """Initialize disk/database state with bounded retries for transient locks."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(AppState, settings)
        except sqlite3.OperationalError as exc:
            if attempt == attempts:
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            log.warning("database startup attempt %s/%s failed: %s; retrying in %.1fs",
                        attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("application state initialization exhausted retries")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    instance_lock = InstanceLock(settings.instance_lock_path)
    orchestrator = None
    started = time.monotonic()
    app.state.ready = False
    try:
        if settings.enforce_single_instance:
            instance_lock.acquire()
        log.info("backend startup begin pid=%s host=%s port=%s", os.getpid(),
                 settings.backend_host, settings.backend_port)
        app.state.manager = manager
        app.state.started_monotonic = started
        app.state.started_at = datetime.now(timezone.utc).isoformat()
        app.state.instance_id = uuid.uuid4().hex
        app.state.wp = await initialize_app_state(settings)
        log.info("[DATA] Loaded %s rounds", len(app.state.wp.rounds))

        # Betting automation module (Part 1 — read-only browser verification).
        betting_manager = BettingManager(
            get_betting_settings(), broadcaster=manager.broadcast,
            repository=app.state.wp.repository,
        )
        reconciled = app.state.wp.repository.reconcile_incomplete_executions()
        if reconciled:
            log.warning("[EXECUTION] reconciled %s incomplete executions; none will be retried", reconciled)
        app.state.betting = betting_manager
        configure_default_manager(betting_manager)
        app.state.risk = RiskManager(
            broadcaster=manager.broadcast, betting_manager=betting_manager,
        )
        app.state.reconciliation = ReconciliationService(
            app.state.wp.repository, broadcaster=manager.broadcast,
            betting_manager=betting_manager, risk_manager=app.state.risk,
        )
        betting_manager.reconciler = app.state.reconciliation
        app.state.decision_engine = DecisionEngine(
            settings.decision_path, app.state.wp.repository,
            broadcaster=manager.broadcast,
        )
        app.state.history_collector = HistoryCollectorManager(settings.data_path, manager.broadcast)
        app.state.shadow = ShadowManager(repository=app.state.wp.repository, broadcaster=manager.broadcast)
        app.state.system_health = SystemHealth(
            broadcaster=manager.broadcast, betting_manager=betting_manager,
            history_collector=app.state.history_collector, repository=app.state.wp.repository,
        )
        app.state.live = LiveActivationManager(app)
        app.state.operations = OperationsManager(app)
        app.state.disaster_recovery = DisasterRecoveryManager(app, app.state.operations)
        for component in ("features", "patterns", "ml", "evidence", "decision", "risk", "api"):
            app.state.system_health.heartbeat(
                component, ok=True, metadata={"initialized": True}, stale_after_s=3600,
            )
        betting_manager.safety_gate = app.state.system_health.can_bet_now
        app.state.history_monitor = monitor_file
        app.state.system_health_monitor = monitor_health
        app.state.operations_monitor = monitor_operations
        orchestrator = SystemOrchestrator(app)
        app.state.orchestrator = orchestrator
        try:
            await app.state.disaster_recovery.recover("startup")
        except Exception:
            log.exception("startup recovery failed; continuing in safe mode")
        known_rounds = {str(row["round_id"]): row for row in app.state.history_collector.rows()}
        for execution in app.state.wp.repository.open_executions():
            known = known_rounds.get(str(execution.get("target_round_id")))
            if known:
                try:
                    await app.state.reconciliation.reconcile_round(known)
                except Exception:
                    log.exception("startup reconciliation failed execution=%s",
                                  execution.get("execution_id"))
        await orchestrator.start()
        app.state.ready = True
        log.info("backend startup complete pid=%s instance=%s", os.getpid(),
                 app.state.instance_id)
        yield
    except Exception:
        log.exception("backend startup/runtime failure")
        raise
    finally:
        app.state.ready = False
        log.info("backend graceful shutdown begin pid=%s", os.getpid())
        if orchestrator is not None:
            await orchestrator.shutdown()
        instance_lock.release()
        log.info("backend graceful shutdown complete pid=%s", os.getpid())


app = FastAPI(title="Winner Predict", description="STATISTICAL PATTERN ANALYSIS platform for Aviator round history research.", version="1.0.0", lifespan=lifespan)
settings = get_settings()


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Return a stable error response without terminating the ASGI process."""
    request_id = getattr(request.state, "request_id", "unknown")
    log.exception("unhandled API failure request_id=%s method=%s path=%s",
                  request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error",
                 "request_id": request_id},
    )


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        log.exception("API request crashed request_id=%s method=%s path=%s duration_ms=%.1f",
                      request_id, request.method, request.url.path, elapsed)
        response = JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_server_error",
                     "request_id": request_id},
        )
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    if response.status_code >= 500:
        log.error("API failure request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                  request_id, request.method, request.url.path,
                  response.status_code, elapsed)
    elif request.url.path != "/health":
        log.info("API request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                 request_id, request.method, request.url.path,
                 response.status_code, elapsed)
    return response


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
app.include_router(data.router)
app.include_router(patterns.router)
app.include_router(models.router)
app.include_router(ml.router)
app.include_router(evidence.router)
app.include_router(decisions.router)
app.include_router(betting_api.router)
app.include_router(risk_api.router)
app.include_router(results.router)
app.include_router(health.router)
app.include_router(backtesting.router)
app.include_router(research.router)
app.include_router(shadow.router)
app.include_router(live_api.router)
app.include_router(operations_api.router)
app.include_router(recovery_api.router)


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
async def health(request: Request):
    """Liveness/readiness report used by the frontend and process supervisor."""
    database = {"status": "unavailable", "error": "repository not initialized"}
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is not None:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            database = {"status": "ok", "path": str(repository.database_path)}
        except Exception as exc:
            database = {"status": "error", "error": str(exc)}
            log.exception("database health check failed")
    ready = bool(getattr(request.app.state, "ready", False))
    database_ok = database["status"] == "ok"
    payload = {
        "ok": ready and database_ok,
        "service": "winner-predict-backend",
        "status": "healthy" if ready and database_ok else "degraded",
        "backend": {
            "status": "ok" if ready else "starting",
            "pid": os.getpid(),
            "instance_id": getattr(request.app.state, "instance_id", None),
            "started_at": getattr(request.app.state, "started_at", None),
            "uptime_seconds": round(
                time.monotonic() - getattr(request.app.state, "started_monotonic", time.monotonic()),
                3,
            ),
        },
        "database": database,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("health check backend=%s database=%s",
             payload["backend"]["status"], database["status"])
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)


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

    # Reload mode intentionally remains opt-in through the uvicorn CLI. An
    # implicit reloader creates an extra process and recurring brief outages.
    uvicorn.run(app, host=settings.backend_host, port=settings.backend_port,
                reload=False, log_level="info")
