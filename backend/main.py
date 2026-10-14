from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hmac
import json
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from app.api import betting as betting_api, history, health, results, risk as risk_api, shadow
from app.api import live as live_api, operations as operations_api, recovery as recovery_api
from app.api import betting_mode as betting_mode_api, stability as stability_api
from app.api import round_distribution as round_distribution_api
from app.api import rules_engine as rules_engine_api
from app.betting.config import get_betting_settings
from app.betting.session import BettingManager, configure_default_manager
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.runtime import InstanceLock
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine
from app.services.system_health import SystemHealth
from app.services.system_orchestrator import SystemOrchestrator
from app.services.betting_mode import BettingModeManager
from app.shadow import ShadowManager
from app.risk.engine import RiskManager
from app.history_collector import HistoryCollectorManager
from app.reconciliation import ReconciliationService
from app.live import LiveActivationManager
from app.services.operations import OperationsManager
from app.services.disaster_recovery import DisasterRecoveryManager
from app.services.process_lifecycle import ProcessLifecycle
from app.round_distribution import RoundDistributionHub
from app.api.round_distribution import observed_round_event
from app.round_observation_inbox import (
    ObservationIdentityConflict, ObservationInboxCapacityError, RoundObservationInbox,
    run_round_observation_inbox,
)
from app.rules_engine import RulesEngine

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
        # Connections may join or leave while send_json yields to the loop.
        for ws in tuple(self.active):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


manager = ConnectionManager()




async def process_history_update(app: FastAPI):
    """Refresh canonical history and settle existing records without prediction."""
    await asyncio.to_thread(app.state.wp.reload)
    history = app.state.history_collector
    status = history.status()
    latest = status.get("latest")
    if latest:
        if hasattr(app.state, "shadow"):
            await app.state.shadow.reconcile_round(latest)
        if hasattr(app.state, "reconciliation"):
            await app.state.reconciliation.reconcile_round(latest)
    evaluation = None
    try:
        repository = app.state.wp.repository
        distribution = await asyncio.to_thread(repository.round_distribution_snapshot)
        current_sequence = int(distribution.get("latest_sequence") or 0)
        stream_id = str(distribution.get("stream_id") or "")
        after_sequence = app.state.rules_engine.stream_cursor(stream_id, current_sequence)
        if stream_id and current_sequence > after_sequence:
            events = await asyncio.to_thread(repository.round_distribution_events,
                stream_id, after_sequence, current_sequence, 500)
            latest_marker = await asyncio.to_thread(repository.latest_round_marker)
            for committed_event in events:
                target_already_completed = (str(committed_event.get("delivery_mode") or "LIVE").upper() == "RECOVERED"
                    or latest_marker is None
                    or int(latest_marker[0]) >= int(committed_event.get("round_index") or 0) + 1)
                evaluation = await asyncio.to_thread(app.state.rules_engine.observe_round, committed_event,
                    target_already_completed=target_already_completed)
        if evaluation is None:
            evaluation = app.state.rules_engine.status()
        await manager.broadcast({"type": "rules-engine:evaluated", "evaluation": evaluation})
    except Exception as exc:
        # Rules are observational consumers. A rules persistence/evaluation
        # error is visible but must not interrupt collector/history delivery.
        log.exception("rules engine committed-event processing failed")
        await manager.broadcast({"type": "rules-engine:error", "error": type(exc).__name__})
    await manager.broadcast({"type": "history:new_round", "round": latest})
    await manager.broadcast({"type": "history:updated", "history": {"count": status.get("count", 0)}})
    await manager.broadcast({"type": "history:stats", "stats": history.stats()})
    await manager.broadcast({"type": "rules-engine:status", "status": app.state.rules_engine.status()})


async def monitor_file(app: FastAPI):
    monitor = MonitoringEngine(app.state.wp.settings.data_path)
    repository = app.state.wp.repository
    last_round_marker = None
    while True:
        try:
            await asyncio.sleep(3)
            if repository.database_url:
                marker = await asyncio.to_thread(repository.latest_round_marker)
                changed = marker != last_round_marker
                last_round_marker = marker
            else:
                changed = monitor.has_changed()
            if changed:
                # File parsing and database sync are
                # synchronous/CPU-bound. Keep them off FastAPI's event loop so
                # health checks and UI requests remain responsive.
                await process_history_update(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def _process_direct_collector_observation(app: FastAPI, event: dict, marker: tuple) -> dict:
    """Acknowledge only a round already committed to canonical PostgreSQL history."""
    round_id = str(event["round_id"])
    current = await asyncio.to_thread(app.state.wp.repository.latest_round_marker)
    if not current or (int(current[0]), str(current[1])) != (int(marker[0]), round_id):
        raise RuntimeError("collector_round_marker_changed_before_dispatch")
    await process_history_update(app)
    return {"round_id": round_id, "round_index": int(marker[0]),
            "decision": "NO_ACTION", "reason": "NOT_CONFIGURED"}




async def monitor_collector_round_observations(app: FastAPI):
    """Deliver canonical collector rounds without running prediction observers."""
    repository = app.state.wp.repository
    log.info("[COLLECTOR_EVENTS] canonical round dispatcher started")
    while True:
        try:
            if not repository.database_url:
                await asyncio.sleep(1.0)
                continue
            inbox = getattr(app.state, "round_observation_inbox", None)
            if inbox is not None:
                pending = await run_round_observation_inbox(app, inbox.unstaged_observations, limit=100)
                if pending:
                    try:
                        await asyncio.to_thread(repository.stage_collector_round_observations, pending)
                        await run_round_observation_inbox(
                            app, inbox.mark_staged_many,
                            [item["observation_id"] for item in pending])
                    except Exception:
                        for item in pending:
                            try:
                                await asyncio.to_thread(repository.stage_collector_round_observations, [item])
                                await run_round_observation_inbox(
                                    app, inbox.mark_staged_many, [item["observation_id"]])
                            except Exception as exc:
                                attempts = await run_round_observation_inbox(
                                    app, inbox.record_stage_failure, item["observation_id"],
                                    f"{type(exc).__name__}:{exc}")
                                log.warning("[COLLECTOR_EVENTS] stage deferred id=%s attempts=%s category=%s",
                                            item["observation_id"], attempts, type(exc).__name__)
            events = await asyncio.to_thread(repository.pending_collector_round_observations, 100)
            if not events:
                await asyncio.sleep(0.05)
                continue
            marker = await asyncio.to_thread(repository.latest_round_marker)
            if marker is None:
                await asyncio.sleep(0.1)
                continue
            for row in events:
                round_id = str(row["round_id"])
                round_index = int(row["canonical_round_index"])
                direct_value = float(row["direct_multiplier"])
                canonical_value = float(row["canonical_multiplier"])
                payload = json.loads(row["payload"])
                if (str(row["canonical_round_id"]) != round_id
                        or not math.isclose(direct_value, canonical_value, rel_tol=0, abs_tol=0.0001)
                        or str(payload.get("round_id")) != round_id):
                    await asyncio.to_thread(repository.finish_collector_round_observation,
                        round_id, "IDENTITY_CONFLICT", round_index,
                        "direct-page event does not match the canonical PostgreSQL round")
                    log.error("[COLLECTOR_EVENTS] identity mismatch round_id=%s index=%s", round_id, round_index)
                    continue
                samples = _collector_alignment_status().get("roundCommitSamples") or []
                sample = next((item for item in reversed(samples) if isinstance(item, dict)
                               and str(item.get("round_id")) == round_id), None)
                if sample and sample.get("committed_at"):
                    payload["commit_acknowledged_at"] = sample["committed_at"]
                try:
                    distribution = await asyncio.to_thread(
                        repository.persist_round_distribution_event, round_id,
                        canonical_value, payload,
                        int(os.environ.get("ROUND_STREAM_RETENTION", "100000")))
                    if distribution and distribution.get("published_at") is None:
                        distribution = await asyncio.to_thread(
                            repository.mark_round_distribution_event_published,
                            distribution["stream_id"], distribution["stream_sequence"])
                    if distribution:
                        await app.state.round_distribution.publish(distribution)
                except Exception:
                    log.exception("[ROUND_DISTRIBUTION] outbox failed round_id=%s", round_id)
                if (round_index, round_id) != (int(marker[0]), str(marker[1])):
                    if round_index < int(marker[0]):
                        await asyncio.to_thread(repository.finish_collector_round_observation,
                            round_id, "SUPERSEDED", round_index,
                            f"newer PostgreSQL source {marker[1]} was current before dispatch")
                    continue
                try:
                    result = await _process_direct_collector_observation(app, row, marker)
                    finish = "SUPERSEDED" if result.get("superseded") else "PROCESSED"
                    await asyncio.to_thread(repository.finish_collector_round_observation,
                        round_id, finish, round_index,
                        "newer round arrived during dispatch" if finish == "SUPERSEDED" else None)
                    log.info("[COLLECTOR_EVENTS] canonical round handled round_id=%s result=%s", round_id, result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = min(10.0, 0.25 * (2 ** min(int(row.get("attempts", 0)), 5)))
                    await asyncio.to_thread(repository.retry_collector_round_observation,
                        round_id, f"{type(exc).__name__}:{str(exc)[:800]}", delay)
                    log.exception("[COLLECTOR_EVENTS] dispatch deferred round_id=%s category=%s",
                                  round_id, type(exc).__name__)
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[COLLECTOR_EVENTS] dispatcher cycle failed category=%s", type(exc).__name__)
            await asyncio.sleep(1.0)














def _collector_alignment_status() -> dict:
    status_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _collector_runtime_status() -> dict:
    """Read the separately supervised collector's current sidecar status."""
    source = _collector_alignment_status()
    now = datetime.now(timezone.utc)
    def parse(value):
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp
        except (TypeError, ValueError, OverflowError):
            return None
    heartbeat_at = parse(source.get("heartbeatAt"))
    collected_at = parse(source.get("lastSuccessfulCollection"))
    heartbeat_age = (now - heartbeat_at).total_seconds() if heartbeat_at else None
    collection_age = (now - collected_at).total_seconds() if collected_at else None
    heartbeat_limit = float(os.environ.get("COLLECTOR_HEARTBEAT_STALE_SECONDS", "20"))
    heartbeat_state = ("UNKNOWN" if heartbeat_age is None else
                       "FRESH" if 0 <= heartbeat_age <= heartbeat_limit else "STALE")
    return {
        "running": source.get("collectorRunning") is True,
        "collector_process_status": ("RUNNING" if source.get("collectorRunning") is True else
                                     "STOPPED" if source.get("collectorRunning") is False else "UNKNOWN"),
        "runtime_status": source.get("state") or source.get("health") or "UNKNOWN",
        "status": source.get("state") or source.get("health") or "UNKNOWN",
        "health": source.get("health"),
        "browser_connected": source.get("browserConnected") is True,
        "page_connected": source.get("pageConnected") is True,
        "frame_connected": source.get("frameConnected") is True,
        "logged_in": source.get("loggedIn") if isinstance(source.get("loggedIn"), bool) else None,
        "auth_required": source.get("authRequired") is True,
        "heartbeat_at": source.get("heartbeatAt"),
        "heartbeat_state": heartbeat_state,
        "heartbeat_age_seconds": heartbeat_age,
        "collection_fresh": collection_age is not None and 0 <= collection_age <= 120.0,
        "last_successful_collection": source.get("lastSuccessfulCollection"),
        "last_round_id": source.get("lastRoundId"),
        "last_round_index": source.get("lastRoundIndex"),
        "cashout_initialization": source.get("cashout_initialization") or {
            "target": "2.00", "auto_bet": "UNKNOWN", "status": "UNKNOWN",
            "panel_1_cashout": "UNKNOWN", "panel_2_cashout": "UNKNOWN",
            "panels": [], "updated_at": None,
            "error_reason": "COLLECTOR_CASHOUT_INITIALIZATION_NOT_REPORTED",
        },
        "bet_amount_initialization": source.get("bet_amount_initialization") or {
            "status": "UNKNOWN", "execution_panel": None, "requested_stake": None,
            "requested_stake_currency": None, "platform_currency": None,
            "observed_value": None, "updated_at": None,
            "error_reason": "COLLECTOR_STAKE_INITIALIZATION_NOT_REPORTED",
        },
        "betting_ui_configuration": source.get("betting_ui_configuration") or {
            "status": "UNKNOWN", "updated_at": None,
            "error_reason": "COLLECTOR_BETTING_UI_CONFIGURATION_NOT_REPORTED",
        },
        "authentication_state": source.get("authenticationState")
            if source.get("authenticationState") in {"AUTHENTICATED", "AUTH_REQUIRED", "UNKNOWN"}
            else "UNKNOWN",
        "browser_pages": source.get("browser_pages") or {},
    }














async def monitor_health(app: FastAPI):
    while True:
        try:
            await app.state.system_health.enforce_safety()
            modes = getattr(app.state, "betting_mode", None)
            if modes is not None:
                await modes.monitor_safety()
            lifecycle = getattr(app.state, "lifecycle", None)
            if lifecycle is not None:
                await manager.broadcast({"type": "backend:stability",
                                         "stability": await backend_stability_snapshot(app)})
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system health monitor cycle failed; retrying")
            await asyncio.sleep(2)






async def monitor_platform_balance(app: FastAPI):
    """Keep UI balance fresh using the collector's existing CDP browser."""
    while True:
        try:
            await app.state.betting_mode._read_platform_balance()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("read-only browser balance monitor failed; retrying")
        await asyncio.sleep(5)


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


async def backend_stability_snapshot(app: FastAPI) -> dict:
    """Build a cheap, non-blocking stability view for the dashboard."""
    lifecycle = getattr(app.state, "lifecycle", None)
    snapshot = lifecycle.status() if lifecycle is not None else {
        "status": "STARTING", "pid": os.getpid(), "ppid": os.getppid(),
    }
    history = getattr(app.state, "history_collector", None)
    snapshot["collector"] = history.health_status() if history else {"status": "STARTING"}
    current = _collector_runtime_status()
    if current.get("collector_process_status") == "STOPPED":
        current["runtime_status"] = "STOPPED"
    elif (current.get("heartbeat_state") != "FRESH" or current.get("collection_fresh") is not True
          or current.get("logged_in") is not True or not current.get("browser_connected")
          or not current.get("frame_connected")):
        current["runtime_status"] = "STALE"
    else:
        current["runtime_status"] = "HEALTHY"
    snapshot["collector_current"] = current
    snapshot["rules_engine"] = app.state.rules_engine.status()
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if repository is None:
        snapshot["database"] = {"status": "STARTING"}
    else:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            snapshot["database"] = {"status": "OK"}
        except Exception as exc:
            snapshot["database"] = {"status": "ERROR", "reason": str(exc)}
    health = getattr(app.state, "system_health", None)
    if health is None:
        snapshot["browser"] = {"status": "STARTING"}
    else:
        try:
            browser = health.snapshot()["components"]["browser"]
            snapshot["browser"] = {"status": browser["state"], "reason": browser["last_error"]}
        except Exception as exc:
            snapshot["browser"] = {"status": "ERROR", "reason": str(exc)}
    reliability_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "browser-reliability.json"
    try:
        reliability = await asyncio.to_thread(lambda: json.loads(reliability_path.read_text(encoding="utf-8")))
        updated = datetime.fromisoformat(reliability["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            reliability["snapshot_stale"] = True
            reliability["state"] = "DEGRADED"
            reliability["reason"] = "Supervisor heartbeat is stale"
        snapshot["browser_reliability"] = reliability
    except (OSError, ValueError, KeyError, TypeError):
        snapshot["browser_reliability"] = {"state": "UNKNOWN", "reason": "Supervisor snapshot unavailable"}
    return snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.instance_id = uuid.uuid4().hex
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    lock = InstanceLock(settings.instance_lock_path, port=settings.backend_port,
                        instance_id=app.state.instance_id)
    app.state.instance_lock = lock
    lifecycle = ProcessLifecycle(settings.data_path.parent / "backend_lifecycle.jsonl")
    app.state.lifecycle = lifecycle
    inbox_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round-observation-inbox")
    app.state.round_observation_inbox_executor = inbox_executor
    orchestrator = None
    app.state.ready = False
    shutdown_reason = "graceful_lifespan_exit"
    try:
        if settings.enforce_single_instance:
            lock.acquire()
        prior = next((event for event in reversed(lifecycle.events())
                      if event.get("event") == "backend_start" and event.get("pid") != os.getpid()), None)
        lifecycle.start(startup_reason=os.environ.get("WINNER_STARTUP_REASON", "manual_or_process_manager"),
                        previous_pid=(prior or {}).get("pid"),
                        restart_count=int(os.environ.get("WINNER_RESTART_COUNT", "0") or 0))
        log.info("backend startup begin pid=%s host=%s port=%s", os.getpid(),
                 settings.backend_host, settings.backend_port)
        app.state.manager = manager
        app.state.settings = settings
        app.state.round_distribution = RoundDistributionHub()
        inbox_path = Path(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_PATH") or
                          (settings.data_path.parent / "backend" / "round-observation-inbox.sqlite3"))
        app.state.round_observation_inbox = RoundObservationInbox(
            inbox_path,
            max_records=int(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_MAX_RECORDS", "50000")),
            max_bytes=int(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_MAX_BYTES", str(64 * 1024 * 1024))),
        )
        app.state.started_monotonic = time.monotonic()
        app.state.wp = await initialize_app_state(settings)
        lifecycle.bind_repository(app.state.wp.repository)
        app.state.round_stream_id = await asyncio.to_thread(
            app.state.wp.repository.round_distribution_stream_id)
        log.info("[DATA] Loaded %s canonical rounds", len(app.state.wp.rounds))

        repository = app.state.wp.repository
        betting = BettingManager(get_betting_settings(), broadcaster=manager.broadcast,
                                 repository=repository)
        reconciled = repository.reconcile_incomplete_executions()
        if reconciled:
            log.warning("[EXECUTION] reconciled %s incomplete executions; none will be retried", reconciled)
        app.state.betting = betting
        configure_default_manager(betting)
        app.state.risk = RiskManager(broadcaster=manager.broadcast, betting_manager=betting)
        app.state.reconciliation = ReconciliationService(
            repository, broadcaster=manager.broadcast,
            betting_manager=betting, risk_manager=app.state.risk)
        betting.reconciler = app.state.reconciliation
        app.state.history_collector = HistoryCollectorManager(
            settings.data_path, manager.broadcast,
            round_loader=repository.load_rounds if repository.database_url else None,
            repository=repository)
        app.state.shadow = ShadowManager(repository=repository, broadcaster=manager.broadcast)
        app.state.shadow.risk_manager = app.state.risk
        app.state.system_health = SystemHealth(
            broadcaster=manager.broadcast, betting_manager=betting,
            history_collector=app.state.history_collector, repository=repository)
        app.state.rules_engine = RulesEngine(persistence=repository)
        # Rules decisions must consult observations accepted by the durable
        # API inbox as well as PostgreSQL. The inbox can lead PostgreSQL by
        # several seconds while its consumer retries a slow commit.
        app.state.rules_engine.observation_inbox = app.state.round_observation_inbox
        app.state.reconciliation.rules_engine = app.state.rules_engine
        app.state.live = LiveActivationManager(app)
        app.state.operations = OperationsManager(app)
        app.state.disaster_recovery = DisasterRecoveryManager(app, app.state.operations)
        app.state.betting_mode = BettingModeManager(app)
        betting.execution_gate = app.state.betting_mode.final_execution_gate
        betting.execution_failure_handler = app.state.betting_mode.handle_execution_failure
        app.state.system_health.heartbeat("rules_engine", ok=False,
            error="Rules Engine is not configured", metadata=app.state.rules_engine.status())
        for component in ("risk", "executor", "reconciliation", "api"):
            app.state.system_health.heartbeat(component, ok=True,
                metadata={"initialized": True}, stale_after_s=3600)
        betting.safety_gate = app.state.system_health.can_bet_now
        app.state.history_monitor = monitor_file
        app.state.collector_observation_monitor = monitor_collector_round_observations
        app.state.system_health_monitor = monitor_health
        app.state.platform_balance_monitor = monitor_platform_balance
        app.state.operations_monitor = monitor_operations
        orchestrator = SystemOrchestrator(app)
        app.state.orchestrator = orchestrator
        try:
            await app.state.disaster_recovery.recover("startup")
        except Exception:
            log.exception("startup recovery failed; continuing in safe mode")
        known_rounds = {str(row["round_id"]): row for row in app.state.history_collector.rows()}
        for execution in repository.open_executions():
            observed = known_rounds.get(str(execution.get("target_round_id")))
            if observed:
                try:
                    await app.state.reconciliation.reconcile_round(observed)
                except Exception:
                    log.exception("startup reconciliation failed execution=%s", execution.get("execution_id"))
        await orchestrator.start()
        app.state.ready = True
        log.info("backend startup complete pid=%s instance=%s", os.getpid(), app.state.instance_id)
        yield
    except Exception as exc:
        shutdown_reason = f"startup_or_runtime_exception:{type(exc).__name__}"
        lifecycle.exception(exc, phase="startup_or_runtime")
        log.exception("backend startup/runtime failure")
        raise
    finally:
        app.state.ready = False
        hub = getattr(app.state, "round_distribution", None)
        if hub is not None:
            await hub.shutdown()
        lifecycle.shutdown(reason=shutdown_reason)
        log.info("backend graceful shutdown begin pid=%s", os.getpid())
        if orchestrator is not None:
            await orchestrator.shutdown()
        inbox_executor.shutdown(wait=True, cancel_futures=True)
        lock.release()
        log.info("backend graceful shutdown complete pid=%s", os.getpid())


app = FastAPI(title="Winner Predict", description="Aviator round history and betting safety console.", version="1.0.0", lifespan=lifespan)
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

    PUBLIC_ACCESS opens dashboard/API actions to visitors. The legacy
    PUBLIC_READ_ONLY switch continues to expose GET requests only.
    """
    public_read = settings.public_read_only and request.method == "GET"
    external_round_read = request.method == "GET" and request.url.path in {
        "/api/v1/rounds", "/api/v1/rounds/latest", "/api/v1/rounds/stream",
        "/api/v1/rounds/pending",
        "/api/v1/collector/status",
    }
    if (settings.api_key and request.url.path.startswith("/api/")
            and not (settings.public_access or public_read or external_round_read)):
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {settings.api_key}"
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API credentials"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


def _utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_collector_game_phase(raw: dict, *, require_source: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("game phase evidence must be an object")
    phase = str(raw.get("phase") or "").upper()
    if phase not in {"BETTING_OPEN", "BETTING_CLOSED", "UNKNOWN"}:
        raise ValueError("phase must be BETTING_OPEN, BETTING_CLOSED, or UNKNOWN")
    observed_at = str(raw.get("observed_at") or "")
    try:
        observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("game phase observed_at must be an ISO timestamp") from exc
    if observed_time.tzinfo is None:
        raise ValueError("game phase observed_at must include a timezone")
    event_id = str(raw.get("event_id") or "").strip()
    if not event_id or len(event_id) > 512:
        raise ValueError("game phase event_id is missing or too long")
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("game phase evidence details are missing")
    evidence_fields = {
        "source": str(evidence.get("source") or "")[:80],
        "selector": str(evidence.get("selector") or "")[:120],
        "button_visible": evidence.get("button_visible") is True,
        "button_enabled": evidence.get("button_enabled") is True,
        "stake_input_visible": evidence.get("stake_input_visible") is True,
        "bet_tab_active": evidence.get("bet_tab_active") is True,
        "button_text": str(evidence.get("button_text") or "")[:40],
        "button_class": str(evidence.get("button_class") or "")[:160],
    }
    if phase == "BETTING_OPEN" and not (
        evidence_fields["button_visible"] and evidence_fields["button_enabled"]
        and evidence_fields["stake_input_visible"] and evidence_fields["bet_tab_active"]
    ):
        raise ValueError("BETTING_OPEN requires a visible enabled game button and active betting controls")
    if phase == "BETTING_CLOSED" and not (
        evidence.get("source") == "AVIATOR_BETTING_PANEL_READ_ONLY"
        and evidence.get("selector") == ".bet-block .btn-success.bet"
        and evidence.get("button_visible") is True
        and evidence.get("button_enabled") is False
        and evidence.get("stake_input_visible") is True
        and evidence.get("bet_tab_active") is True
    ):
        raise ValueError("BETTING_CLOSED requires a visible disabled game button and active betting controls")
    if phase == "BETTING_OPEN" and not (
        evidence.get("source") == "AVIATOR_BETTING_PANEL_READ_ONLY"
        and evidence.get("selector") == ".bet-block .btn-success.bet"
    ):
        raise ValueError("BETTING_OPEN requires the owned Aviator betting-panel selector")
    result = {"event_id": event_id, "phase": phase,
              "observed_at": observed_time.astimezone(timezone.utc).isoformat(),
              "evidence": evidence_fields}
    if require_source:
        source_id = str(raw.get("source_round_id") or "").strip()
        if not source_id or len(source_id) > 256:
            raise ValueError("source_round_id is missing or invalid")
        try:
            source_index = int(raw.get("source_round_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("source_round_index must be an integer") from exc
        if source_index < 1:
            raise ValueError("source_round_index must be positive")
        result.update({"source_round_id": source_id, "source_round_index": source_index})
    return result


def _collector_target_identity(raw: dict) -> dict:
    """Retain target identity only when the collector supplies explicit proof."""
    target_id_value = raw.get("targetRoundId") if isinstance(raw, dict) else None
    target_round_id = str(target_id_value).strip() if target_id_value is not None else ""
    target_round_id = target_round_id if target_round_id and len(target_round_id) <= 256 else None
    started_value = raw.get("targetStarted") if isinstance(raw, dict) else None
    target_started = started_value if isinstance(started_value, bool) else None
    proof = raw.get("targetIdentityProof") if isinstance(raw, dict) else None
    valid_proof = (
        isinstance(proof, dict)
        and proof.get("target_round_id") == target_round_id
        and proof.get("target_not_started") is True
        and proof.get("proof_method") == "platform_target_id_before_start"
    )
    verified = bool(
        target_round_id
        and isinstance(raw, dict)
        and raw.get("targetIdentityVerified") is True
        and target_started is False
        and valid_proof
    )
    return {
        "target_round_id": target_round_id,
        "target_identity_verified": verified,
        "target_started": target_started,
        "target_identity_proof": proof if verified else None,
    }


def _validate_direct_collector_observations(body: dict) -> list[dict]:
    raw_rounds = body.get("rounds") if isinstance(body, dict) else None
    if not isinstance(raw_rounds, list) or not raw_rounds or len(raw_rounds) > 50:
        raise ValueError("rounds must contain between 1 and 50 observations")
    observations = []
    seen_ids = set()
    for raw in raw_rounds:
        if not isinstance(raw, dict):
            raise ValueError("each round observation must be an object")
        round_id = str(raw.get("round_id") or "").strip()
        if not round_id or len(round_id) > 256 or round_id in seen_ids:
            raise ValueError("round_id is missing, too long, or duplicated")
        seen_ids.add(round_id)
        legacy_event_id = f"aviator-round:{round_id}"
        event_id = str(raw.get("event_id") or legacy_event_id)
        if event_id not in {legacy_event_id, f"{legacy_event_id}:observed"}:
            raise ValueError("event_id must identify the round observation")
        observation_id = str(raw.get("observation_id") or round_id).strip()
        if not observation_id or len(observation_id) > 256:
            raise ValueError("observation_id is missing or invalid")
        try:
            multiplier = float(raw.get("multiplier"))
        except (TypeError, ValueError) as exc:
            raise ValueError("multiplier must be numeric") from exc
        if not math.isfinite(multiplier) or not 1 <= multiplier <= 1_000_000:
            raise ValueError("multiplier must be a finite platform value at least 1x")
        multiplier = round(multiplier, 2)
        observed_at = str(raw.get("observed_at") or "")
        try:
            observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO timestamp") from exc
        if observed_time.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        try:
            local_index = int(raw.get("round_index", raw.get("local_round_index")))
        except (TypeError, ValueError) as exc:
            raise ValueError("round_index must be an integer") from exc
        if local_index < 1:
            raise ValueError("round_index must be positive")
        platform_round_index = raw.get("platform_round_index")
        if platform_round_index is not None:
            try:
                platform_round_index = int(platform_round_index)
            except (TypeError, ValueError) as exc:
                raise ValueError("platform_round_index must be an integer") from exc
            if platform_round_index < 1:
                raise ValueError("platform_round_index must be positive")
        platform_round_id = raw.get("platform_round_id")
        if platform_round_id is not None:
            platform_round_id = str(platform_round_id).strip()
            if not platform_round_id or len(platform_round_id) > 256:
                raise ValueError("platform_round_id is invalid")
        platform_timestamp = raw.get("platform_timestamp")
        if platform_timestamp is not None:
            parsed_platform_timestamp = _utc_timestamp(platform_timestamp)
            if parsed_platform_timestamp is None:
                raise ValueError("platform_timestamp must be an ISO timestamp or null")
            platform_timestamp = parsed_platform_timestamp.isoformat()
        game_phase = raw.get("game_phase")
        if game_phase is not None:
            game_phase = _validate_collector_game_phase(game_phase)
        delivery_mode = str(raw.get("delivery_mode") or "LIVE").upper()
        if delivery_mode not in {"LIVE", "RECOVERED"}:
            raise ValueError("delivery_mode must be LIVE or RECOVERED")
        round_index_source = str(raw.get("round_index_source") or "UNKNOWN").upper()
        if round_index_source not in {"PLATFORM", "LOCAL_SEQUENCE", "UNKNOWN"}:
            raise ValueError("round_index_source must be PLATFORM, LOCAL_SEQUENCE, or UNKNOWN")
        local_appended_at = raw.get("local_appended_at")
        collector_http_started_at = raw.get("collector_http_started_at")
        for field_name, field_value in (("local_appended_at", local_appended_at),
                                        ("collector_http_started_at", collector_http_started_at)):
            if field_value is not None and _utc_timestamp(field_value) is None:
                raise ValueError(f"{field_name} must be an ISO timestamp or null")
        observations.append({
            "event_id": legacy_event_id,
            "observation_id": observation_id,
            "round_id": round_id,
            "multiplier": multiplier,
            "observed_at": observed_time.astimezone(timezone.utc).isoformat(),
            "timestamp": raw.get("timestamp"),
            "platform_round_id": platform_round_id,
            "platform_round_index": platform_round_index,
            "platform_timestamp": platform_timestamp,
            "round_index": local_index,
            "round_index_source": round_index_source,
            "continuity_verified": raw.get("continuity_verified") is True,
            "gap_before": raw.get("gap_before") is True,
            "identity_confidence": str(raw.get("identity_confidence") or "UNKNOWN")[:80],
            "round_identity_type": str(raw.get("round_identity_type") or "UNKNOWN")[:80],
            "game_phase": game_phase,
            "delivery_mode": delivery_mode,
            "local_appended_at": local_appended_at,
            "collector_http_started_at": collector_http_started_at,
            "commit_acknowledged_at": raw.get("commit_acknowledged_at"),
            "observation_to_commit_ms": (float(raw["observation_to_commit_ms"])
                if isinstance(raw.get("observation_to_commit_ms"), (int, float))
                and math.isfinite(float(raw["observation_to_commit_ms"]))
                and float(raw["observation_to_commit_ms"]) >= 0 else None),
        })
    return observations


@app.post("/api/collector/round-observed", tags=["collector"])
async def collector_round_observed(request: Request):
    """Fan out a pending stage after collector durability, then persist API resume state."""
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    stream_id = getattr(request.app.state, "round_stream_id", None)
    if inbox is None or not stream_id:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_inbox_unavailable",
        })
    started = time.perf_counter()
    received_at = datetime.now(timezone.utc).isoformat()
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "WINNER_DIRECT_PAGE_HISTORY":
            raise ValueError("round observation source must be WINNER_DIRECT_PAGE_HISTORY")
        observations = _validate_direct_collector_observations(body)
        events = []
        for observation in observations:
            event = observed_round_event(observation, stream_id)
            event["api_received_at"] = received_at
            event["api_receipt_to_sse_publication_ms"] = None
            events.append(event)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except ClientDisconnect:
        log.warning("[COLLECTOR_EVENTS] collector disconnected before completing the observation request; its durable local outbox will retry")
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "collector_request_disconnected_retry",
        })
    except Exception as exc:
        log.exception("[COLLECTOR_EVENTS] could not validate direct page observation category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_validation_failed",
        })
    published_at = None
    publication_ms = 0.0
    try:
        # The collector's fsynced outbox is the durable source at this point.
        # Fan out before API-side SQLite writes, which only support reconnect
        # catch-up and must never delay a currently connected SSE client.
        for event in events:
            event["published_at"] = datetime.now(timezone.utc).isoformat()
            event["api_receipt_to_sse_publication_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            published_at = event["published_at"]
            publication_ms = event["api_receipt_to_sse_publication_ms"]
            await request.app.state.round_distribution.publish(event)
    except Exception as exc:
        log.exception("[ROUND_DISTRIBUTION] observed event remains in collector outbox; fanout deferred category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_fanout_deferred",
        })
    append_started = time.perf_counter()
    try:
        accepted = await run_round_observation_inbox(
            request.app, inbox.append, events, observations, received_at=received_at)
    except ObservationIdentityConflict as exc:
        log.error("[COLLECTOR_EVENTS] event was published but API inbox rejected an identity conflict; collector retains it: %s", exc)
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
    except ObservationInboxCapacityError as exc:
        log.error("[COLLECTOR_EVENTS] event was published; API inbox is full and collector retains its durable outbox: %s", exc)
        return JSONResponse(status_code=507, content={"ok": False, "error": "round_observation_inbox_capacity_reached"})
    except Exception as exc:
        log.exception("[COLLECTOR_EVENTS] event was published but API resume append failed; collector retains its durable outbox category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_inbox_write_failed",
        })
    inbox_append_ms = (time.perf_counter() - append_started) * 1000.0
    # Observed events are not committed round results. Do not run session rule
    # evaluation here: a backlog batch can take longer than the collector's
    # acknowledgement deadline, which then prevents the collector from moving
    # on to its PostgreSQL persistence stage. RulesEngine consumes only the
    # ordered round.persisted stream after the canonical PG write succeeds.
    log.info("[COLLECTOR_EVENTS] round.observed published and resume state durable count=%s new=%s backlog=%s api_to_sse_ms=%.3f inbox_append_ms=%.3f",
             len(observations), len(accepted["events"]), accepted["backlog_size"], publication_ms, inbox_append_ms)
    return {"ok": True, "status": "OBSERVED_PUBLISHED_PENDING_POSTGRESQL",
            "accepted": len(observations), "new_events": len(accepted["events"]),
            "duplicate_committed": accepted["duplicates"], "api_received_at": received_at,
            "api_published_at": published_at,
            "api_receipt_to_sse_publication_ms": publication_ms,
            "rule_evaluation_stage": "WAITING_FOR_COMMITTED_POSTGRES_EVENT",
            "rules_engine_evaluations": [],
            "events": [{"event_id": event["event_id"], "observation_id": event["observation_id"]}
                       for event in accepted["events"]]}


@app.post("/api/collector/round-persisted", tags=["collector"])
async def collector_round_persisted(request: Request):
    """Write the replayable committed event after PostgreSQL has acknowledged the round."""
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    if repository is None or not repository.database_url:
        return JSONResponse(status_code=503, content={"ok": False, "error": "authoritative_postgresql_unavailable"})
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "WINNER_CANONICAL_POSTGRESQL_COMMIT":
            raise ValueError("round persisted source must identify a collector PostgreSQL commit")
        observations = _validate_direct_collector_observations(body)
        for observation in observations:
            observation["commit_acknowledged_at"] = observation.get("commit_acknowledged_at") or body.get("commit_acknowledged_at")
            observation["delivery_mode"] = observation.get("delivery_mode") or "LIVE"
        distributions = await asyncio.to_thread(
            repository.persist_round_distribution_batch, observations,
            int(os.environ.get("ROUND_STREAM_RETENTION", "100000")))
        accepted = []
        receipts = []
        for observation, distribution in zip(observations, distributions, strict=True):
            await request.app.state.round_distribution.publish(distribution)
            receipts.append((observation["observation_id"],
                             distribution.get("committed_sequence") or distribution["stream_sequence"]))
            accepted.append({"event_id": distribution["event_id"],
                             "observation_id": observation["observation_id"],
                             "round_id": observation["round_id"],
                             "committed_sequence": distribution["committed_sequence"]})
        if inbox is not None:
            await run_round_observation_inbox(request.app, inbox.mark_committed_many, receipts)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        log.exception("[ROUND_DISTRIBUTION] committed-event handoff failed category=%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "error": "committed_event_handoff_unavailable"})
    return {"ok": True, "accepted": len(accepted), "events": accepted}


@app.post("/api/collector/game-phase", tags=["collector"])
async def collector_game_phase_observed(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "OWNED_AVIATOR_GAME_FRAME_READ_ONLY":
            raise ValueError("game phase source must be the owned read-only Aviator frame")
        _validate_collector_game_phase(body, require_source=True)
        return {"ok": True, "accepted": False, "reason": "RULES_ENGINE_NOT_CONFIGURED"}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        log.warning("collector game-phase observation rejected: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "game_phase_persistence_unavailable",
        })


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([*settings.cors_origins, *settings.round_stream_allowed_origins])),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(history.router)
app.include_router(betting_api.router)
app.include_router(risk_api.router)
app.include_router(results.router)
app.include_router(health.router)
app.include_router(shadow.router)
app.include_router(live_api.router)
app.include_router(operations_api.router)
app.include_router(recovery_api.router)
app.include_router(betting_mode_api.router)
app.include_router(stability_api.router)
app.include_router(round_distribution_api.router)
app.include_router(rules_engine_api.router)


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
            if repository.database_url:
                database = {"status": "ok", "backend": "postgresql"}
            else:
                database = {"status": "ok", "backend": "sqlite", "path": str(repository.database_path)}
        except Exception as exc:
            database = {"status": "error", "error": str(exc)}
            log.exception("database health check failed")
    ready = bool(getattr(request.app.state, "ready", False))
    database_ok = database["status"] == "ok"
    payload = {
        "ok": ready and database_ok,
        "service": "winner-predict-backend",
        "auth_required": bool(settings.api_key and not settings.public_access),
        "public_read_only": bool(settings.public_read_only and not settings.public_access),
        "public_access": settings.public_access,
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
        "single_instance": (getattr(request.app.state, "instance_lock", None).status()
                            if getattr(request.app.state, "instance_lock", None) else None),
        "database": database,
        "stability": (getattr(request.app.state, "lifecycle", None).status()
                      if getattr(request.app.state, "lifecycle", None) else None),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("health check backend=%s database=%s",
             payload["backend"]["status"], database["status"])
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)


@app.get("/api/system/status", tags=["system"])
async def system_status(request: Request):
    """Expose non-sensitive runtime configuration and canonical history size."""
    wp = request.app.state.wp
    quality = wp.quality.model_dump() if hasattr(wp.quality, "model_dump") else dict(wp.quality or {})
    cfg = wp.settings
    return {
        "ok": True,
        "configuration": {
            "api_security_enabled": bool(cfg.api_key and not cfg.public_access),
            "public_access_enabled": bool(cfg.public_access),
        },
        "dataset": {
            "source": cfg.data_path.name,
            "valid_records": quality.get("valid_records", len(wp.rounds)),
            "first_timestamp": quality.get("first_timestamp"),
            "last_timestamp": quality.get("last_timestamp"),
            "last_round_index": quality.get("last_round_index"),
        },
    }


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    if settings.api_key and not settings.public_read_only and not settings.public_access:
        supplied = websocket.query_params.get("token", "")
        if not hmac.compare_digest(supplied, settings.api_key):
            await websocket.close(code=1008, reason="Invalid API credentials")
            return
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "system_status", "status": "connected", "label": "ROUND HISTORY AND SAFETY"})
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
