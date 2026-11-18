from __future__ import annotations

import json
import math
import asyncio
import logging
from time import perf_counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter(prefix="/api/rules-engine", tags=["rules engine"])
logger = logging.getLogger(__name__)
SHADOW_BOUNDARY_MAX_AGE_SECONDS = 120
_RETIRED_RISK_KEY_FRAGMENTS = (
    "risk", "bankroll", "drawdown", "consecutive_loss",
    "minimum_balance", "min_balance", "profile",
)
_NET_OUTCOME_LIMIT_KEYS = {"loss_limit_enabled", "loss_limit_count"}
_RULE_LOSS_GUARD_KEYS = {
    "loss_limit", "loss_limit_losses", "loss_limit_blocked",
    "loss_limit_reached_at", "loss_limit_reached_round_index",
}
_RETIRED_RISK_BLOCKERS = {
    "VERIFIED_BALANCE_AND_SUFFICIENT_FUNDS_REQUIRED",
    "MINIMUM_BALANCE_REQUIRED",
    "DRAWDOWN_LIMIT_REACHED",
    "SESSION_LOSS_LIMIT_REACHED",
    "CONSECUTIVE_LOSS_LIMIT_REACHED",
}
_RETIRED_RISK_STATUSES = {"RISK_WAITING", "RISK_REJECTED", "RISK_APPROVED"}


def _without_retired_risk_data(value, *, net_limits_enabled: bool = False):
    """Hide removed gate fields from API views while preserving DB history."""
    if isinstance(value, dict):
        net_limits_enabled = (net_limits_enabled
                              or value.get("net_outcome_limit_version") == 1)
        cleaned = {}
        for key, child in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if ("loss_limit" in normalized_key
                    and normalized_key not in _RULE_LOSS_GUARD_KEYS
                    and not (net_limits_enabled and normalized_key in _NET_OUTCOME_LIMIT_KEYS)):
                continue
            if any(fragment in normalized_key for fragment in _RETIRED_RISK_KEY_FRAGMENTS):
                continue
            if normalized_key == "blockers" and isinstance(child, list):
                child = [item for item in child
                         if not (isinstance(item, str)
                                 and (item.upper() in _RETIRED_RISK_BLOCKERS
                                      or item.upper() in _RETIRED_RISK_STATUSES
                                      or item.upper().startswith(("RISK_", "RISK:"))))]
            cleaned[key] = _without_retired_risk_data(child, net_limits_enabled=net_limits_enabled)
        return cleaned
    if isinstance(value, list):
        return [_without_retired_risk_data(item, net_limits_enabled=net_limits_enabled) for item in value]
    if isinstance(value, str) and value.upper() in _RETIRED_RISK_STATUSES:
        # Old approval labels are not accepted or surfaced as current evidence.
        return "BLOCKED_READ_ONLY"
    return value


def _engine(request: Request):
    engine = getattr(request.app.state, "rules_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="rules_engine_unavailable")
    return engine


def _history(request: Request, *, lightweight: bool = False,
             collector_status: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        collector = (collector_status if collector_status is not None else
                     request.app.state.history_collector.health_status(persist=False)
                     if lightweight else request.app.state.history_collector.status())
    except Exception:
        collector = {"state": "UNKNOWN", "status": "UNKNOWN",
                     "reason": "COLLECTOR_STATUS_UNAVAILABLE"}
    result = {
        "collector_status": collector,
        "latest_real_round": collector.get("latest") or None,
        "total_history": (int(collector["count"]) if collector.get("count") is not None else None),
    }
    return result


def _require_fresh_shadow_boundary(boundary: dict[str, Any], *, now: datetime | None = None) -> None:
    """Require a recent committed round so stale history cannot start a live shadow window."""
    raw = boundary.get("observed_at")
    if not raw:
        raise ValueError("SHADOW_ROUND_TIMESTAMP_UNAVAILABLE")
    try:
        observed_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - observed_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("SHADOW_ROUND_TIMESTAMP_INVALID") from exc
    if age < -60 or age > SHADOW_BOUNDARY_MAX_AGE_SECONDS:
        raise ValueError("SHADOW_ROUND_FEED_STALE")


def _timestamp_age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        observed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - observed_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh_watch_boundary(request: Request) -> dict[str, Any] | None:
    event = getattr(request.app.state, "latest_watch_observation", None)
    if not isinstance(event, dict):
        return None
    metadata = event.get("source_metadata")
    age = _timestamp_age_seconds(event.get("observed_at"))
    if (not isinstance(metadata, dict) or metadata.get("source") != "ORBIT_DATA_WATCH_API"
            or event.get("event_type") != "round.observed" or event.get("durability") != "PENDING"
            or event.get("delivery_mode") != "LIVE" or event.get("continuity_status") != "VERIFIED"
            or age is None or age < -2 or age > SHADOW_BOUNDARY_MAX_AGE_SECONDS):
        return None
    return event


def _shadow_readiness(request: Request, engine=None,
                      collector_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Readiness for prospective, durable SHADOW observation only.

    This checks only verified round delivery and durable SHADOW state.
    """
    engine = engine or _engine(request)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    blockers: list[str] = []
    database_connected = False
    boundary = None
    boundary_fresh = False
    if collector_status is None:
        collector = getattr(request.app.state, "history_collector", None)
        try:
            collector_status = collector.health_status(persist=False) if collector else {}
        except Exception:
            collector_status = {}
    # The Orbit Data Watch route supplies a current, verified observation in
    # process memory. Reading the full PostgreSQL distribution snapshot here
    # is redundant and puts a database history query on the 15 second status
    # poll. Session start still performs the authoritative durable checks.
    watch_boundary = _fresh_watch_boundary(request)
    collector_is_watch_api = bool(
        (collector_status or {}).get("data_source") == "ORBIT_DATA_WATCH_API")
    if repository is None or not getattr(repository, "database_url", None):
        blockers.append("AUTHORITATIVE_POSTGRESQL_NOT_CONFIGURED")
    else:
        try:
            with repository.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            database_connected = True
        except Exception:
            blockers.append("POSTGRESQL_UNAVAILABLE")
        if database_connected and not (collector_is_watch_api and watch_boundary):
            try:
                boundary = repository.round_distribution_snapshot().get("latest") or None
            except Exception:
                blockers.append("COMMITTED_ROUND_FEED_UNAVAILABLE")

    if collector_is_watch_api and watch_boundary:
        boundary = watch_boundary

    if boundary:
        if (boundary.get("event_type") != "round.persisted"
                or boundary.get("durability") != "COMMITTED"
                or boundary.get("continuity_status") != "VERIFIED"):
            blockers.append("VERIFIED_COMMITTED_ROUND_REQUIRED")
        else:
            try:
                _require_fresh_shadow_boundary(boundary)
                boundary_fresh = True
            except ValueError as exc:
                blockers.append(str(exc))
    elif database_connected:
        blockers.append("COMMITTED_ROUND_FEED_UNAVAILABLE")

    if collector_status.get("data_source") == "ORBIT_DATA_WATCH_API" and watch_boundary:
        boundary = watch_boundary
        boundary_fresh = True
        blockers = [item for item in blockers if item not in {
            "COMMITTED_ROUND_FEED_UNAVAILABLE", "VERIFIED_COMMITTED_ROUND_REQUIRED",
            "SHADOW_ROUND_FEED_STALE", "SHADOW_ROUND_TIMESTAMP_UNAVAILABLE",
            "SHADOW_ROUND_TIMESTAMP_INVALID"}]
    elif collector_status.get("data_source") == "ORBIT_DATA_WATCH_API":
        boundary_fresh = False
        blockers.append("FRESH_DATA_WATCH_ROUND_REQUIRED")
    observed_age = _timestamp_age_seconds(collector_status.get("last_observed_at"))
    traditional_collector_live = (
        collector_status.get("collector_process_status") == "RUNNING"
        and collector_status.get("heartbeat_state") == "FRESH"
        and collector_status.get("last_observed_round")
        and observed_age is not None and -60 <= observed_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
    )
    raw_orbit_api = collector_status.get("orbit_api")
    orbit_api = raw_orbit_api if isinstance(raw_orbit_api, dict) else {}
    api_sync_age = _timestamp_age_seconds(orbit_api.get("lastSuccessfulSync"))
    api_reading_age = _timestamp_age_seconds(orbit_api.get("latestReadingAt"))
    process_live = (
        collector_status.get("collector_process_status") == "RUNNING"
        or (collector_status.get("collector_process_status") == "UNKNOWN"
            and collector_status.get("collector_reported_running") is True)
    )
    api_collector_available = (
        collector_status.get("data_source") == "ORBIT_DATA_WATCH_API"
        and process_live
        and collector_status.get("heartbeat_state") == "FRESH"
        and orbit_api.get("status") == "CONNECTED"
        and orbit_api.get("sourceStatus") in ("HEALTHY", "UNKNOWN")
        and api_sync_age is not None
        and -60 <= api_sync_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
        and api_reading_age is not None
        and -60 <= api_reading_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
    )
    api_collector_live = api_collector_available and boundary_fresh
    collector_live = traditional_collector_live or api_collector_available
    latest_round_age = observed_age
    if api_collector_live and not collector_status.get("last_observed_round") and boundary:
        latest_round_age = _timestamp_age_seconds(boundary.get("observed_at"))
    if not collector_live:
        blockers.append("LIVE_ROUND_COLLECTOR_UNAVAILABLE")

    # Policy configuration is not a SHADOW infrastructure requirement.
    # Automatic sessions build their candidate pool from the selected mode;
    # Manual sessions validate each explicitly selected rule in start_session.
    # A mode with no policy entries safely produces no selections.
    blockers = list(dict.fromkeys(blockers))
    return {
        "mode": "SHADOW",
        "data_source": collector_status.get("data_source"),
        "status": "READY" if not blockers else "BLOCKED",
        "ready": not blockers,
        "blockers": blockers,
        "database_connected": database_connected,
        "collector_state": collector_status.get("status", "UNKNOWN"),
        "collector_heartbeat": collector_status.get("heartbeat_state", "UNKNOWN"),
        "latest_observed_round": collector_status.get("last_observed_round") or
            (boundary.get("round_id") if api_collector_live and boundary else None),
        "latest_observed_age_seconds": round(latest_round_age, 3) if latest_round_age is not None else None,
        "committed_boundary": boundary,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
@router.get("/status")
def status(request: Request, response: Response):
    started = perf_counter()
    engine = _engine(request)
    engine_started = perf_counter()
    engine_status = engine.status(include_sessions=False)
    engine_elapsed = perf_counter() - engine_started
    history_started = perf_counter()
    collector = getattr(request.app.state, "history_collector", None)
    try:
        collector_snapshot = collector.health_status(persist=False) if collector else {}
    except Exception:
        collector_snapshot = {}
    history = _history(request, lightweight=True, collector_status=collector_snapshot)
    history_elapsed = perf_counter() - history_started
    readiness_started = perf_counter()
    readiness = _shadow_readiness(request, engine, collector_snapshot)
    readiness_elapsed = perf_counter() - readiness_started
    result = _without_retired_risk_data({**engine_status, **history,
            "shadow_readiness": readiness})
    result["shadow_signal_bridge"] = getattr(
        request.app.state, "shadow_signal_bridge_status", {"state": "UNAVAILABLE"})
    elapsed = perf_counter() - started
    response.headers["Server-Timing"] = (
        f"engine;dur={engine_elapsed * 1000:.1f}, "
        f"history;dur={history_elapsed * 1000:.1f}, "
        f"readiness;dur={readiness_elapsed * 1000:.1f}, "
        f"total;dur={elapsed * 1000:.1f}"
    )
    if elapsed >= 1.0:
        logger.warning("rules_engine_status_slow total_ms=%.1f engine_ms=%.1f history_ms=%.1f readiness_ms=%.1f",
                       elapsed * 1000, engine_elapsed * 1000, history_elapsed * 1000,
                       readiness_elapsed * 1000)
    return result


@router.get("/registry")
def registry(request: Request):
    return _without_retired_risk_data(_engine(request).registry())


@router.get("/sessions")
def sessions(request: Request):
    return _without_retired_risk_data({"sessions": _engine(request).sessions_status()})


@router.get("/rule-performance")
def rule_performance(request: Request):
    """Show continuous committed-collector results per rule without actions."""
    try:
        return _without_retired_risk_data(_engine(request).rule_performance())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="rule_performance_unavailable") from exc


@router.post("/rule-selection/preview")
async def preview_rule_selection(request: Request):
    """Return current Automatic/Adaptive candidates without starting a session."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_json_body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid_rule_selection_preview")
    mode = body.get("mode")
    selection_mode = body.get("selection_mode")
    engine = _engine(request)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    boundary = None
    context: list[dict[str, Any]] = []
    if repository is not None and getattr(repository, "database_url", None):
        try:
            boundary = (await asyncio.to_thread(repository.round_distribution_snapshot)).get("latest")
            if (not isinstance(boundary, dict) or boundary.get("event_type") != "round.persisted"
                    or boundary.get("durability") != "COMMITTED"
                    or boundary.get("continuity_status") != "VERIFIED"):
                boundary = None
            elif boundary:
                try:
                    _require_fresh_shadow_boundary(boundary)
                except ValueError:
                    boundary = None
            if boundary and boundary.get("stream_id"):
                first_sequence = max(0, int(boundary.get("stream_sequence") or 0) - 5)
                context = await asyncio.to_thread(repository.round_distribution_events,
                    boundary["stream_id"], first_sequence,
                    int(boundary.get("stream_sequence") or 0), 10)
        except Exception:
            boundary = None
            context = []
    try:
        return await asyncio.to_thread(engine.preview_rule_selection,
            mode=mode, selection_mode=selection_mode, source=boundary, history=context)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="rule_selection_preview_unavailable") from exc


@router.post("/rule-performance/reset")
async def restart_rule_performance(request: Request):
    """Restart every chooser observer from the latest verified round boundary."""
    try:
        return _without_retired_risk_data(await asyncio.to_thread(
            _engine(request).restart_rule_performance))
    except ValueError as exc:
        code = str(exc)
        status_code = 503 if code == "RULE_PERFORMANCE_OBSERVER_UNAVAILABLE" else 409
        raise HTTPException(status_code=status_code, detail=code) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="rule_performance_reset_unavailable") from exc


@router.get("/dashboard")
def dashboard(request: Request):
    """Return current SHADOW rule and verified-round state for the dashboard."""
    engine = _engine(request)
    status_row = engine.status(include_sessions=False)
    latest = _history(request)
    active = status_row.get("active_session") or {}
    dashboard_reader = getattr(engine, "session_dashboard_context", engine.session)
    detail = dashboard_reader(active.get("session_id")) if active.get("session_id") else None
    reporting_session = active or status_row.get("latest_session") or {}
    reporting_detail = (detail if active else
                        dashboard_reader(reporting_session.get("session_id"))
                        if reporting_session.get("session_id") else None)
    latest_evaluation = ((detail or {}).get("evaluations") or [None])[-1]
    decision = (latest_evaluation or {}).get("decision") or {"action": "WAIT", "reason": "NO_EVALUATION"}
    shared_decision = (latest_evaluation or {}).get("shared_decision")
    if shared_decision is None:
        shared_decision = {
            "action": ("SELECT" if decision.get("action") == "SELECT"
                       else "NO_SELECT" if latest_evaluation else "NOT_EVALUATED"),
            "reason": decision.get("reason"),
            "true_rule_ids": list(decision.get("true_rule_ids") or []),
            "selection_id": decision.get("selection_id"),
        }
    results = (reporting_detail or {}).get("results") or {}
    return _without_retired_risk_data({
        "ok": True,
        "current_round": latest.get("latest_real_round"),
        "collector_status": latest.get("collector_status"),
        "selected_rules": list((detail or {}).get("rule_ids") or []),
        "rule_evaluations": (latest_evaluation or {}).get("rule_evaluations") or [],
        "shared_decision": shared_decision,
        "shared_selection": decision if decision.get("action") == "SELECT" else None,
        "session": active or None,
        "shadow_result_summary": {
            "session_id": reporting_session.get("session_id"),
            "session_status": reporting_session.get("status"),
            "wins": results.get("WIN", 0),
            "losses": results.get("LOSS", 0),
            "pending": results.get("PENDING", 0),
            "net_outcome": (reporting_detail or {}).get("net_outcome", 0),
            "stop_reason": (reporting_detail or {}).get("stop_reason"),
            "stop_reason_source": (reporting_detail or {}).get("stop_reason_source"),
            "stop_reason_at": (reporting_detail or {}).get("stop_reason_at"),
            "label": "VERIFIED SHADOW ROUND OUTCOMES",
        },
    })


@router.get("/performance")
async def performance(request: Request):
    """Expose measured, correlation-linked round processing stage latencies."""
    hub = getattr(request.app.state, "round_distribution", None)
    metrics = await hub.metrics() if hub is not None else {}
    backend = metrics.get("latency_ms") or {}
    latest = metrics.get("latest_samples") or {}
    collector_path = Path(getattr(
        request.app.state, "collector_performance_status_path",
        Path(__file__).resolve().parents[3] / "data" / "bot" / "status.json"))
    try:
        collector = json.loads(collector_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        collector = {}
    if not isinstance(collector, dict):
        collector = {}
    collector_last_round_at = collector.get("lastSuccessfulCollection")
    collector_last_round_age = _timestamp_age_seconds(collector_last_round_at)
    collector_metrics_fresh = (
        collector.get("collectorRunning") is True
        and collector_last_round_age is not None
        and 0 <= collector_last_round_age <= 120.0
    )
    backend_sampled_at = max(
        (str(sample.get("sampled_at")) for sample in latest.values()
         if isinstance(sample, dict) and sample.get("sampled_at")),
        default=None,
    )
    round_delivery = ((collector.get("storage") or {}).get("round_delivery")
                      if isinstance(collector, dict) else {})
    collector_latency = ((round_delivery or {}).get("latency_ms") or {})
    queue = getattr(request.app.state, "round_event_queue", None)
    persistence_queue = getattr(request.app.state, "rules_state_persistence_queue", None)
    dashboard_queue = getattr(request.app.state, "rules_dashboard_notification_queue", None)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    pool_metrics = (repository.connection_pool_metrics()
                    if repository is not None and callable(
                        getattr(repository, "connection_pool_metrics", None)) else None)
    total = latest.get("backend_receive_to_decision_ready_ms") or {}
    total_ms = total.get("latency_ms")
    if isinstance(total_ms, (int, float)):
        status = "GREEN" if total_ms < 500 else "WARNING" if total_ms <= 2000 else "CRITICAL"
    else:
        status = "UNKNOWN"
    end_to_end = latest.get("collector_detection_to_signal_api_acceptance_ms") or {}
    end_to_end_ms = end_to_end.get("latency_ms")
    end_to_end_summary = backend.get("collector_detection_to_signal_api_acceptance_ms")
    end_to_end_target_ms = 2000
    if isinstance(end_to_end_ms, (int, float)) and math.isfinite(float(end_to_end_ms)):
        end_to_end_status = "GREEN" if float(end_to_end_ms) < end_to_end_target_ms else "CRITICAL"
    else:
        end_to_end_status = "UNKNOWN"
    rows = [
        ("Collector mutation to extraction", collector_latency.get("page_mutation_to_multiplier_extraction_ms")),
        ("Collector durable append", collector_latency.get("observation_to_durable_append_ms")),
        ("Collector HTTP acknowledgment", collector_latency.get("collector_http_roundtrip_to_api_ack_ms")),
        ("Backend inbox append", backend.get("api_durable_inbox_append_ms")),
        ("Backend round queue wait", backend.get("round_event_processor_queue_wait_ms")),
        ("Backend event loop lag", backend.get("backend_event_loop_lag_ms")),
        ("Backend receive to decision ready", backend.get("backend_receive_to_decision_ready_ms")),
        ("Rules Engine lock wait", backend.get("rules_engine_lock_wait_ms")),
        ("USER_PLAYING_POLICY evaluation", backend.get("user_playing_policy_evaluation_ms")),
        ("Standard rule evaluation", backend.get("standard_rule_evaluation_ms")),
        ("Selected result evaluation", backend.get("selected_result_evaluation_ms")),
        ("TWO_BET_WINS transition", backend.get("two_bet_wins_transition_ms")),
        ("Target identity check", backend.get("target_identity_check_ms")),
        ("Decision aggregation", backend.get("decision_aggregation_ms")),
        ("Rules Engine total", backend.get("rules_engine_evaluation_ms")),
        ("Rules persistence queue wait", backend.get("rules_state_persistence_queue_wait_ms")),
        ("Rules snapshot capture", backend.get("rules_snapshot_capture_ms")),
        ("Rules database persistence", backend.get("rules_database_persistence_ms")
            or backend.get("database_persistence_ms")),
        ("Rules persistence total", backend.get("rules_persistence_total_ms")),
        ("Signal create and durable acknowledgment", backend.get("signal_create_to_durable_ack_ms")),
        ("Decision to Signal API acceptance", backend.get("decision_to_signal_api_acceptance_ms")),
        ("Orbit collector receipt to Signal API acceptance", backend.get("collector_receipt_to_signal_api_acceptance_ms")),
        ("Backend receive to Signal API acceptance", backend.get("backend_receive_to_signal_api_acceptance_ms")),
        ("Collector detection to Signal API acceptance", end_to_end_summary),
        ("Backend database pool acquisition", pool_metrics),
        ("PostgreSQL round write", collector_latency.get("database_write_ms")),
        ("Backend receive to final decision", backend.get("backend_receive_to_final_decision_ms")),
    ]
    available = [(name, summary) for name, summary in rows
                 if isinstance(summary, dict) and int(summary.get("samples") or 0) > 0]
    collector_stage_names = {
        "Collector mutation to extraction", "Collector durable append",
        "Collector HTTP acknowledgment", "PostgreSQL round write",
    }
    current_available = [
        (name, summary) for name, summary in available
        if collector_metrics_fresh or name not in collector_stage_names
    ]
    slowest = max(current_available,
                  key=lambda item: float(item[1].get("average_ms")
                                         if item[1].get("average_ms") is not None
                                         else item[1].get("p50_ms") or 0),
                  default=(None, None))
    queue_depth = queue.qsize() if queue is not None else 0
    return {
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sample_freshness": {
            "backend_started_at": getattr(request.app.state, "started_at", None),
            "backend_latest_sample_at": backend_sampled_at,
            "collector_last_round_at": collector_last_round_at,
            "collector_last_round_age_seconds": (
                round(collector_last_round_age, 3)
                if collector_last_round_age is not None else None),
            "collector_current": collector_metrics_fresh,
            "collector_current_max_age_seconds": 120,
            "collector_state": collector.get("state") or "UNKNOWN",
        },
        "correlation_key": "round_id",
        "status": status,
        "backend_path_status": status,
        "end_to_end_target_status": end_to_end_status,
        "end_to_end_target_ms": end_to_end_target_ms,
        "end_to_end_latest_ms": end_to_end_ms,
        "end_to_end_latest_correlation_id": end_to_end.get("correlation_id"),
        "end_to_end_latest_sampled_at": end_to_end.get("sampled_at"),
        "total_stage": "backend_receive_to_decision_ready_ms",
        "total_latest_ms": total_ms,
        "total_latest_correlation_id": total.get("correlation_id"),
        "slowest_stage": slowest[0],
        "slowest_stage_metrics": slowest[1],
        "queue": {"depth": queue_depth, "capacity": getattr(queue, "maxsize", None),
                  "busy": bool(getattr(request.app.state, "round_event_processor_busy", False))},
        "rules_state_persistence_queue": {
            "depth": persistence_queue.qsize() if persistence_queue is not None else 0,
            "capacity": getattr(persistence_queue, "maxsize", None),
            "busy": bool(getattr(request.app.state, "rules_state_persistence_busy", False)),
        },
        "dashboard_notifications": {
            "depth": dashboard_queue.qsize() if dashboard_queue is not None else 0,
            "capacity": getattr(dashboard_queue, "maxsize", None),
            "coalesced": int(getattr(request.app.state,
                                     "rules_dashboard_notifications_coalesced", 0)),
        },
        "stages": {name: summary for name, summary in rows},
        "backend_latency_ms": backend,
        "latest_correlated_samples": latest,
        "measurement_availability": {
            "provider_round_available_timestamp": "UNAVAILABLE_PROVIDER_DOES_NOT_EXPOSE_EVENT_TIME",
            "collector_page_to_node_one_way": "UNAVAILABLE_DIFFERENT_MONOTONIC_CLOCK_ORIGINS",
            "external_signal_consumer_ack": "UNAVAILABLE_NO_CLIENT_ACK_PROTOCOL",
            "current_end_to_end_total": ("AVAILABLE" if end_to_end_status != "UNKNOWN"
                                          else "UNAVAILABLE_NO_COMPLETE_CORRELATED_DETECTION_TO_ACCEPTANCE_SPAN"),
            "collector_detection_to_signal_api_acceptance": ("AVAILABLE" if end_to_end_status != "UNKNOWN"
                else "UNAVAILABLE_NO_LOOPBACK_DETECTION_TIMESTAMP_OR_DURABLE_ACCEPTANCE_SPAN"),
        },
    }


@router.get("/sessions/{session_id}")
async def session_detail(request: Request, session_id: str,
                         include_history: bool = False):
    engine = _engine(request)
    reader = (engine.session if include_history else
              getattr(engine, "session_live_detail", engine.session))
    result = await asyncio.to_thread(reader, session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    payload = _without_retired_risk_data(result)
    serialized = await asyncio.to_thread(
        json.dumps, payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return Response(content=serialized, media_type="application/json")


@router.post("/sessions/delete-batch")
async def delete_sessions(request: Request):
    import asyncio

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_json_body") from exc
    session_ids = body.get("session_ids") if isinstance(body, dict) else None
    if (not isinstance(session_ids, list) or not session_ids
            or len(session_ids) > 1000
            or any(not isinstance(session_id, str) or not session_id.strip()
                   for session_id in session_ids)):
        raise HTTPException(status_code=422, detail="invalid_session_ids")
    try:
        result = await asyncio.to_thread(_engine(request).delete_sessions, session_ids)
        return _without_retired_risk_data(result)
    except ValueError as exc:
        code = str(exc)
        status_code = 422 if code == "INVALID_SESSION_IDS" else 503
        raise HTTPException(status_code=status_code, detail=code) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="rules_session_bulk_delete_unavailable") from exc


@router.delete("/sessions/{session_id}")
def delete_session(request: Request, session_id: str):
    try:
        return _without_retired_risk_data(_engine(request).delete_session(session_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="rules_session_delete_unavailable") from exc


@router.put("/configuration/{rule_id}")
async def configure_rule(request: Request, rule_id: str):
    import asyncio

    try:
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise ValueError("enabled_must_be_boolean")
        saved = await asyncio.to_thread(_engine(request).configure, rule_id,
            enabled=body["enabled"], parameters=body.get("parameters"))
        return _without_retired_risk_data({"rule_id": rule_id, **saved})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/presets")
async def save_preset(request: Request):
    import asyncio

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("invalid_preset")
        saved = await asyncio.to_thread(_engine(request).save_preset,
            body.get("name"), body.get("mode"), body.get("rule_ids"), body.get("options"))
        return _without_retired_risk_data(saved)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/presets")
def presets(request: Request):
    return _without_retired_risk_data({"presets": _engine(request).status().get("presets", {})})


@router.post("/sessions")
async def start_session(request: Request):
    import asyncio

    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    readiness = await asyncio.to_thread(_shadow_readiness, request)
    if not readiness["ready"]:
        raise HTTPException(status_code=503,
            detail="SHADOW_SESSION_START_BLOCKED:" + ",".join(readiness["blockers"]))
    try:
        boundary = _fresh_watch_boundary(request)
        if boundary is None:
            if readiness.get("data_source") == "ORBIT_DATA_WATCH_API":
                raise ValueError("FRESH_DATA_WATCH_ROUND_REQUIRED")
            snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
            boundary = snapshot.get("latest") or {}
        if boundary.get("event_type") not in ("round.persisted", "round.observed"):
            raise ValueError("verified_committed_round_boundary_required")
        _require_fresh_shadow_boundary(boundary)
        context: list[dict[str, Any]] = []
        first_sequence = max(0, int(boundary.get("stream_sequence") or 0) - 5)
        if boundary.get("event_type") == "round.persisted" and boundary.get("stream_id"):
            context = await asyncio.to_thread(repository.round_distribution_events,
                boundary["stream_id"], first_sequence, int(boundary.get("stream_sequence") or 0), 10)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("invalid_session_configuration")
        replace_session_id = body.get("replace_session_id")
        if replace_session_id is not None and not isinstance(replace_session_id, str):
            raise ValueError("invalid_session_replacement")
        session = await asyncio.to_thread(_engine(request).start_session,
            mode=body.get("mode"), rule_ids=body.get("rule_ids"),
            boundary=boundary, prestart_context=context, options=body.get("options"),
            replace_session_id=replace_session_id)
        return session
    except ValueError as exc:
        status_code = 409 if str(exc) in {
            "SESSION_ALREADY_ACTIVE_FOR_MODE", "SESSION_NOT_RESTARTABLE",
        } else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"rules_session_start_unavailable:{type(exc).__name__}") from exc


@router.post("/sessions/{session_id}/reconfigure")
async def reconfigure_session(request: Request, session_id: str):
    """Close one historical session and start a new, explicitly confirmed one."""
    import asyncio

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_json_body") from exc
    if not isinstance(body, dict) or body.get("confirmation") != f"RECONFIGURE SESSION {session_id}":
        raise HTTPException(status_code=409, detail={
            "error": "operator_confirmation_required",
            "confirmation": f"RECONFIGURE SESSION {session_id}",
            "settings_changed": False,
        })
    engine = _engine(request)
    previous = await asyncio.to_thread(engine.session, session_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if previous.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}:
        raise HTTPException(status_code=409, detail="rules_session_is_not_active")

    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    readiness = await asyncio.to_thread(_shadow_readiness, request, engine)
    if not readiness["ready"]:
        raise HTTPException(status_code=503,
            detail="RULES_SESSION_RECONFIGURATION_BLOCKED:" + ",".join(readiness["blockers"]))

    try:
        boundary = _fresh_watch_boundary(request)
        if boundary is None:
            if readiness.get("data_source") == "ORBIT_DATA_WATCH_API":
                raise ValueError("FRESH_DATA_WATCH_ROUND_REQUIRED")
            snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
            boundary = snapshot.get("latest") or {}
        if boundary.get("event_type") not in ("round.persisted", "round.observed"):
            raise ValueError("verified_committed_round_boundary_required")
        _require_fresh_shadow_boundary(boundary)
        context: list[dict[str, Any]] = []
        first_sequence = max(0, int(boundary.get("stream_sequence") or 0) - 5)
        if boundary.get("event_type") == "round.persisted" and boundary.get("stream_id"):
            context = await asyncio.to_thread(repository.round_distribution_events,
                boundary["stream_id"], first_sequence,
                int(boundary.get("stream_sequence") or 0), 10)
        mode = body.get("mode")
        rule_ids = body.get("rule_ids")
        options = body.get("options")
        if not isinstance(mode, str) or not isinstance(rule_ids, list) or not isinstance(options, dict):
            raise ValueError("new_mode_rule_ids_and_options_required")
        def apply_reconfiguration():
            # Keep conflict checking and both persisted changes serialized
            # against round delivery, while the blocking PostgreSQL work runs
            # outside the ASGI event loop.
            with engine._lock:
                conflicts = [row for row in engine.sessions.values()
                             if row.get("session_id") != session_id
                             and row.get("mode") == mode
                             and row.get("status") in {"RUNNING", "SETTLING", "PAUSED"}
                             and row.get("selection_status") == "ACTIVE"]
                if conflicts:
                    raise ValueError("another_active_session_for_mode_exists")
                new_session = engine.start_session(mode=mode, rule_ids=rule_ids,
                    boundary=boundary, prestart_context=context, options=options,
                    reconfigure_session_id=session_id)
                try:
                    closed = engine.close_for_reconfiguration(
                        session_id, new_session["session_id"], body["confirmation"])
                except Exception:
                    engine.session_action(new_session["session_id"], "stop")
                    raise
                return new_session, closed

        new_session, closed = await asyncio.to_thread(apply_reconfiguration)
        return {"ok": True, "previous_session": _without_retired_risk_data(closed),
                "new_session": _without_retired_risk_data(new_session),
                "history_preserved": True, "settings_changed_only_by_explicit_request": True}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"rules_session_reconfiguration_unavailable:{type(exc).__name__}") from exc


@router.post("/sessions/{session_id}/{action}")
async def session_action(request: Request, session_id: str, action: str):
    import asyncio

    try:
        session = await asyncio.to_thread(_engine(request).session_action, session_id, action)
        return _without_retired_risk_data(session)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/evaluate")
def evaluate(request: Request):
    # Selection is created only from the ordered committed-event dispatcher.
    latest = _history(request).get("latest_real_round")
    return _without_retired_risk_data(_engine(request).evaluate(latest))
