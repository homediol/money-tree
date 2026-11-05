from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.round_distribution import _require_admin, _require_https_or_loopback
from app.api.signals import (PUBLISHER_HEARTBEAT_STALE_SECONDS, _authenticate_consumer,
                             _hub, _repository, evaluate_latest_signal_action)

router = APIRouter(prefix="/api/v1", tags=["API developer console"])
log = logging.getLogger(__name__)
READ_SCOPES = {"signals:read", "rules:read", "metrics:read"}
ALL_SCOPES = READ_SCOPES | {"signals:write"}
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
KEY_ID = re.compile(r"^[A-Fa-f0-9-]{16,64}$")
PUBLISHER_CONNECTED_SECONDS = PUBLISHER_HEARTBEAT_STALE_SECONDS


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    else:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _admin(request: Request) -> None:
    _require_admin(request)
    limiter = getattr(request.app.state, "round_distribution", None)
    if limiter is not None and request.client is not None:
        allowed, retry_after = limiter.allow_request("api-console-admin:" + str(request.client.host))
        if not allowed:
            raise HTTPException(429, detail={"error": "api_console_rate_limit_exceeded"},
                                headers={"Retry-After": str(retry_after)})


def _viewer(request: Request) -> None:
    """Apply transport and rate limits; outer API middleware enforces owner auth."""
    _require_https_or_loopback(request)
    limiter = getattr(request.app.state, "round_distribution", None)
    if limiter is not None and request.client is not None:
        allowed, retry_after = limiter.allow_request("api-console-viewer:" + str(request.client.host))
        if not allowed:
            raise HTTPException(429, detail={"error": "api_console_rate_limit_exceeded"},
                                headers={"Retry-After": str(retry_after)})


def _has_owner_access(request: Request) -> bool:
    try:
        _require_admin(request)
        return True
    except HTTPException:
        return False


def _publisher_status(request: Request) -> dict[str, Any]:
    heartbeat = getattr(request.app.state, "rules_publisher_heartbeat", None)
    last_seen = heartbeat.get("received_at") if isinstance(heartbeat, dict) else None
    parsed = _parse_timestamp(last_seen)
    age = (datetime.now(timezone.utc) - parsed).total_seconds() if parsed else None
    if parsed is None:
        status = "AUTHENTICATION_FAILED" if getattr(
            request.app.state, "rules_publisher_last_auth_failure_at", None) else "DISCONNECTED"
    elif age is None or age < 0 or age > PUBLISHER_CONNECTED_SECONDS:
        status = "STALE"
    else:
        status = "CONNECTED"
    last_auth_failure = _parse_timestamp(getattr(
        request.app.state, "rules_publisher_last_auth_failure_at", None))
    authentication_status = ("FAILED" if last_auth_failure and (parsed is None or last_auth_failure > parsed)
                              else "OK" if parsed is not None else "UNKNOWN")
    shadow_status = heartbeat.get("shadow_status", "UNKNOWN") if isinstance(heartbeat, dict) else "UNKNOWN"
    connection_status = "CONNECTED" if status == "CONNECTED" else status
    return {
        "status": status,
        "connection_status": connection_status,
        "authentication_status": authentication_status,
        "publisher_id": heartbeat.get("publisher_id") if isinstance(heartbeat, dict) else None,
        "shadow_status": shadow_status,
        "mode": "SHADOW" if shadow_status == "ACTIVE" else "STOPPED" if shadow_status == "STOPPED" else None,
        "last_heartbeat_at": _iso(last_seen),
        "heartbeat_age_seconds": round(age, 3) if age is not None else None,
        "last_successful_delivery_at": _iso(getattr(request.app.state, "rules_publisher_last_delivery", None)),
        "queue_depth": heartbeat.get("queue_depth") if isinstance(heartbeat, dict) else None,
        "queue_status": heartbeat.get("queue_status", "UNKNOWN") if isinstance(heartbeat, dict) else "UNKNOWN",
        "delivery_failures": heartbeat.get("delivery_failures") if isinstance(heartbeat, dict) else None,
        "heartbeat_interval_seconds": 15,
        "stale_after_seconds": PUBLISHER_CONNECTED_SECONDS,
        "last_auth_failure_at": _iso(getattr(request.app.state, "rules_publisher_last_auth_failure_at", None)),
    }


def _shadow_bridge_status(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "shadow_signal_bridge", None)
    try:
        snapshot = bridge.status() if bridge is not None else getattr(
            request.app.state, "shadow_signal_bridge_status", None)
    except Exception:
        snapshot = None
    if not isinstance(snapshot, dict):
        return {"status": "UNAVAILABLE", "last_error": "bridge_status_unavailable",
                "published_count": 0, "duplicate_count": 0, "settled_count": 0}

    def count(name: str) -> int:
        value = snapshot.get(name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    raw_state = snapshot.get("state")
    state = raw_state.upper() if isinstance(raw_state, str) and raw_state else "UNKNOWN"
    raw_error = snapshot.get("last_error")
    return {
        "status": state,
        "last_attempt_at": _iso(snapshot.get("last_attempt_at")),
        "last_published_at": _iso(snapshot.get("last_published_at")),
        "last_target_round_id": str(snapshot.get("last_target_round_id") or "")[:256] or None,
        "last_target_round_index": (snapshot.get("last_target_round_index")
            if isinstance(snapshot.get("last_target_round_index"), int)
            and not isinstance(snapshot.get("last_target_round_index"), bool) else None),
        "target_identity_status": (str(snapshot.get("last_target_identity_status"))[:40]
            if snapshot.get("last_target_identity_status") else None),
        "last_error": str(raw_error)[:160] if raw_error else None,
        "published_count": count("published_count"),
        "duplicate_count": count("duplicate_count"),
        "settled_count": count("settled_count"),
    }


async def _overview(request: Request) -> dict[str, Any]:
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    database_status = "DISCONNECTED"
    try:
        if repository is None or not getattr(repository, "database_url", None):
            raise RuntimeError("authoritative database is not configured")
        def check_database():
            with repository.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        await asyncio.to_thread(check_database)
        database_status = "CONNECTED"
    except Exception:
        database_status = "DISCONNECTED"

    engine = getattr(request.app.state, "rules_engine", None)
    engine_status: dict[str, Any] = {}
    engine_status_error = False
    try:
        # status() deep-copies the persisted session/evaluation ledger. Keep
        # that CPU work off the ASGI event loop so a slow overview refresh
        # cannot make collector, health, and session-control requests appear
        # disconnected.
        engine_status = await asyncio.to_thread(engine.status) if engine is not None else {}
    except Exception:
        engine_status_error = True
        engine_status = {}
    raw_active_session = engine_status.get("active_session")
    active_session = raw_active_session if isinstance(raw_active_session, dict) else {}
    rules_active = bool(active_session.get("status") == "RUNNING"
                        and active_session.get("selection_status") == "ACTIVE")
    if engine is None:
        rules_engine_state = "UNAVAILABLE"
    elif engine_status_error:
        rules_engine_state = "ERROR"
    elif rules_active:
        rules_engine_state = "ACTIVE"
    elif isinstance(active_session, dict) and active_session:
        session_state = str(active_session.get("status") or "").upper()
        if session_state in {"PAUSED", "STOPPED", "COMPLETED"}:
            rules_engine_state = session_state
        elif str(active_session.get("selection_status") or "").upper() == "STOPPED":
            rules_engine_state = "IDLE"
        else:
            rules_engine_state = "UNKNOWN"
    else:
        engine_state = str(engine_status.get("status") or "").upper()
        rules_engine_state = engine_state if engine_state in {"READY", "NOT_CONFIGURED"} else "UNKNOWN"

    modes = getattr(request.app.state, "betting_mode", None)
    mode_name = getattr(modes, "mode", None) if modes is not None else None
    selected_shadow_mode = mode_name == "SHADOW_REALISTIC"
    transitioning = bool(modes is not None and getattr(modes, "transitioning", False))
    if modes is None or not isinstance(mode_name, str) or not mode_name:
        shadow_mode_state = "UNKNOWN"
        shadow_mode_detail = "Mode status unavailable"
    elif transitioning:
        shadow_mode_state = "TRANSITIONING"
        shadow_mode_detail = "A betting mode change is in progress"
    elif selected_shadow_mode and rules_active:
        shadow_mode_state = "ACTIVE"
        shadow_mode_detail = "SHADOW_REALISTIC selected with an active Rules Engine session"
    elif selected_shadow_mode:
        shadow_mode_state = "SELECTED"
        shadow_mode_detail = "SHADOW_REALISTIC selected; no active Rules Engine session"
    else:
        shadow_mode_state = "STOPPED"
        shadow_mode_detail = "SHADOW_REALISTIC is not the selected betting mode"

    hub = _hub(request)
    try:
        if repository is None:
            raise RuntimeError("repository not initialized")
        signals = await hub.list_signals(repository, 1)
        total = await asyncio.to_thread(repository.count_bet_signals)
    except Exception:
        signals, total = [], None

    session_signal_coverage = None
    latest_session = engine_status.get("latest_session")
    coverage_session = (active_session if isinstance(active_session, dict) and active_session
                        else latest_session if isinstance(latest_session, dict) else {})
    coverage_session_id = coverage_session.get("session_id") if coverage_session else None
    if repository is not None and engine is not None and coverage_session_id:
        try:
            session_detail, session_signals = await asyncio.gather(
                asyncio.to_thread(engine.session, str(coverage_session_id)),
                asyncio.to_thread(repository.list_bet_signals, 1000,
                                  session_id=str(coverage_session_id)),
            )
            selection_rows = []
            if isinstance(session_detail, dict):
                combined = session_detail.get("selections")
                if isinstance(combined, list):
                    selection_rows.extend(row for row in combined if isinstance(row, dict))
                per_rule = session_detail.get("rule_selections")
                if not selection_rows and isinstance(per_rule, dict):
                    for rows in per_rule.values():
                        if isinstance(rows, list):
                            selection_rows.extend(row for row in rows if isinstance(row, dict))
            unique_selections = {}
            for selection in selection_rows:
                identity = str(selection.get("decision_id") or selection.get("selection_id") or "")
                if identity:
                    unique_selections[identity] = selection
            published_decisions = {str(signal.get("decision_id") or "")
                                    for signal in session_signals
                                    if isinstance(signal, dict)
                                    and (signal.get("distribution_status") is None
                                         or signal.get("distribution_status") == "ACCEPTED")
                                    and signal.get("decision_id")}
            session_signal_coverage = {
                "session_id": str(coverage_session_id),
                "session_status": str(coverage_session.get("status") or "UNKNOWN"),
                "scope": "ACTIVE" if rules_active else "LATEST_SESSION",
                "selection_count": len(unique_selections),
                "accepted_signal_count": sum(
                    1 for identity in unique_selections if identity in published_decisions),
                "settled_outcomes_without_signal": sum(
                    1 for identity, selection in unique_selections.items()
                    if selection.get("status") in {"WIN", "LOSS"}
                    and identity not in published_decisions),
                "pending_selections": sum(
                    1 for selection in unique_selections.values()
                    if selection.get("status") == "PENDING"),
            }
        except Exception:
            log.exception("could not load current-session signal coverage")
    latest = signals[0] if signals else None
    hub_metrics = hub.metrics()
    api_ready = bool(getattr(request.app.state, "ready", False))
    api_state = "ONLINE" if api_ready and database_status == "CONNECTED" else "DEGRADED"
    consumer_counts = hub.consumer_counts()
    websocket_connections = consumer_counts["websocket"] + consumer_counts.get("dashboard", 0)
    try:
        _action_response, signal_action = await evaluate_latest_signal_action(request.app)
    except Exception as exc:
        # Keep failures visible to operators without leaking exception details,
        # credentials, or database connection information into the dashboard.
        log.warning("signal action diagnostics unavailable error_type=%s cause_type=%s",
                    type(exc).__name__,
                    type(exc.__cause__).__name__ if exc.__cause__ else None)
        signal_action = {
            "action": "UNAVAILABLE", "index": None,
            "reason": "ACTION_DIAGNOSTICS_UNAVAILABLE",
            "diagnostics_available": False,
            # Unknown is not the same as a confirmed negative status. The UI
            # renders these as UNAVAILABLE instead of falsely claiming the
            # signal was not persisted or accepted.
            "decision_found": None, "signal_persisted": None,
            "signal_accepted": None, "session_matches": None,
            "target_index_available": None, "target_identity_verified": None,
            "timing_verified": None, "rules_matched": None,
            "validation_passed": None,
            "wait_reason": "ACTION_DIAGNOSTICS_UNAVAILABLE",
        }
    return {
        "ok": api_state == "ONLINE",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "api_server": {"status": api_state, "ready": api_ready},
        "rules_engine": {"status": rules_engine_state,
                         "session_id": active_session.get("session_id") if isinstance(active_session, dict) else None,
                         "configured_rules": engine_status.get("rules_configured"),
                         "enabled_rules": engine_status.get("rules_enabled")},
        "shadow_mode": {"status": shadow_mode_state,
                        "mode": mode_name,
                        "detail": shadow_mode_detail},
        "shadow_signal_bridge": _shadow_bridge_status(request),
        # This authenticated console diagnostic is deliberately separate from
        # the public {action, index} response model.
        "signal_action": signal_action,
        # Keep the old field for clients that only want active-session data.
        # The reporting field also covers the latest completed session so a
        # session ending at its configured window cannot hide missed signals.
        "current_session_signal_coverage": session_signal_coverage if rules_active else None,
        "reporting_session_signal_coverage": session_signal_coverage,
        "node_publisher": _publisher_status(request),
        "postgresql": {"status": database_status},
        "websocket": {"status": "CONNECTED" if websocket_connections else "AVAILABLE",
                       "connected_consumers": websocket_connections},
        # Signal payloads remain scoped to signals:read credentials; this
        # overview exposes status metadata and aggregate counts only.
        "has_received_signal": getattr(request.app.state, "last_received_bet_signal", None) is not None,
        "last_received_at": _iso(getattr(request.app.state, "last_received_bet_signal_at", None)),
        "has_last_published_signal": latest is not None,
        "last_published_at": _iso(latest.get("timestamp")) if latest else None,
        "total_bet_signals": total,
        "connected_external_consumers": consumer_counts["websocket"] + consumer_counts["sse"],
        "consumer_connections": consumer_counts,
        "failed_deliveries": hub_metrics["failed_deliveries"],
        "average_delivery_latency_ms": hub_metrics["average_delivery_latency_ms"],
        "publisher_queue": _publisher_status(request),
    }


def _validate_key_request(body: Any, *, previous: dict[str, Any] | None = None) -> tuple[str, str, list[str], str | None, str]:
    if not isinstance(body, dict):
        raise HTTPException(422, detail={"error": "request_body_must_be_an_object"})
    name = str(body.get("name") or (previous or {}).get("label") or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(422, detail={"error": "key_name_required_max_100_characters"})
    key_type = str(body.get("key_type") or (previous or {}).get("key_type") or "consumer")
    if key_type not in {"consumer", "publisher"}:
        raise HTTPException(422, detail={"error": "key_type_must_be_consumer_or_publisher"})
    scopes = body.get("scopes", (previous or {}).get("scopes"))
    if scopes is None:
        scopes = sorted(READ_SCOPES) if key_type == "consumer" else ["signals:write"]
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise HTTPException(422, detail={"error": "scopes_must_be_a_list"})
    scopes = list(dict.fromkeys(scopes))
    if not scopes or not set(scopes).issubset(ALL_SCOPES):
        raise HTTPException(422, detail={"error": "invalid_or_empty_scopes"})
    if key_type == "consumer" and not set(scopes).issubset(READ_SCOPES):
        raise HTTPException(422, detail={"error": "consumer_keys_are_read_only"})
    if key_type == "publisher" and scopes != ["signals:write"]:
        raise HTTPException(422, detail={"error": "publisher_keys_require_signals_write_only"})
    expires_at = body.get("expires_at", (previous or {}).get("expires_at"))
    if expires_at in (None, ""):
        expires_at = None
    else:
        parsed = _parse_timestamp(expires_at)
        if parsed is None or parsed <= datetime.now(timezone.utc):
            raise HTTPException(422, detail={"error": "expires_at_must_be_a_future_timezone_aware_timestamp"})
        expires_at = parsed.isoformat()
    project_id = str(body.get("project_id") or (previous or {}).get("project_id") or
                     ("winner-predict-internal" if key_type == "publisher" else "external-project"))
    if not PROJECT_ID.fullmatch(project_id):
        raise HTTPException(422, detail={"error": "invalid_project_id"})
    return name, key_type, scopes, expires_at, project_id


async def _issue_key(request: Request, body: Any, *, rotate_token_id: str | None = None,
                     previous: dict[str, Any] | None = None) -> dict[str, Any]:
    name, key_type, scopes, expires_at, project_id = _validate_key_request(body, previous=previous)
    raw_key = ("wpp_" if key_type == "publisher" else "wpc_") + secrets.token_urlsafe(48)
    token_id = uuid.uuid4().hex
    prefix = raw_key[:12]
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    repository = _repository(request)
    try:
        record = await asyncio.to_thread(
            repository.create_managed_api_key,
            token_id=token_id, project_id=project_id, label=name,
            token_hash=digest, token_prefix=prefix, key_type=key_type,
            scopes=scopes, expires_at=expires_at, rotate_token_id=rotate_token_id)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "api_key_store_unavailable"}) from exc
    if record is None:
        raise HTTPException(404, detail={"error": "active_api_key_not_found"})
    if rotate_token_id:
        await request.app.state.round_distribution.revoke_token(rotate_token_id)
    return {"ok": True, **record, "token": raw_key,
            "warning": "Copy this key now. The server stores only its SHA-256 hash and will never return this secret again."}


@router.get("/console/access")
async def console_access(request: Request):
    _viewer(request)
    owner = _has_owner_access(request)
    return {"ok": True, "role": "owner" if owner else "viewer",
            "api_key_management": owner}


@router.get("/console/overview")
async def console_overview(request: Request):
    _viewer(request)
    return await _overview(request)


@router.get("/console/signals")
async def console_signals(request: Request, limit: int = Query(100, ge=1, le=1000)):
    _viewer(request)
    if not _has_owner_access(request):
        _authenticate_consumer(request, required_scope="signals:read")
    repository = _repository(request)
    rows = await _hub(request).list_signals(repository, limit)
    return {"ok": True, "signals": rows, "count": len(rows)}


@router.get("/diagnostics/database")
async def database_diagnostics(request: Request):
    _viewer(request)
    repository = _repository(request)
    started = time.perf_counter()
    try:
        import asyncio
        def check():
            with repository.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        await asyncio.to_thread(check)
    except Exception:
        raise HTTPException(503, detail={"ok": False, "database": "DISCONNECTED",
                                         "error": "database_connection_failed"})
    return {"ok": True, "database": "CONNECTED", "backend": "postgresql",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "checked_at": datetime.now(timezone.utc).isoformat()}


@router.get("/diagnostics/publisher")
async def publisher_diagnostics(request: Request):
    _viewer(request)
    hub = _hub(request)
    return {"ok": True, "publisher": _publisher_status(request),
            "delivery": hub.metrics(), "checked_at": datetime.now(timezone.utc).isoformat()}


@router.get("/metrics")
async def public_metrics(request: Request):
    token = _authenticate_consumer(request, required_scope="metrics:read")
    overview = await _overview(request)
    # Never return internal session or consumer credentials in public metrics.
    return {"ok": True, "total_bet_signals": overview["total_bet_signals"],
            "connected_external_consumers": overview["connected_external_consumers"],
            "failed_deliveries": overview["failed_deliveries"],
            "average_delivery_latency_ms": overview["average_delivery_latency_ms"],
            "checked_at": overview["checked_at"], "consumer_project": token.get("project_id")}


@router.get("/decisions/latest")
async def latest_decision(request: Request):
    _authenticate_consumer(request, required_scope="rules:read")
    engine = getattr(request.app.state, "rules_engine", None)
    evaluation = getattr(engine, "last_evaluation", None) if engine is not None else None
    if callable(evaluation):
        evaluation = evaluation()
    decision = (evaluation or {}).get("shared_decision") if isinstance(evaluation, dict) else None
    return {"ok": True, "decision": _json_safe(decision)}


@router.get("/keys")
async def list_api_keys(request: Request):
    _admin(request)
    try:
        keys = await asyncio.to_thread(_repository(request).list_managed_api_keys)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "api_key_store_unavailable"}) from exc
    return {"ok": True, "keys": [_json_safe(item) for item in keys]}


@router.post("/keys")
async def create_api_key(request: Request):
    _admin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, detail={"error": "invalid_json"}) from exc
    return await _issue_key(request, body)


@router.post("/keys/{token_id}/rotate")
async def rotate_api_key(token_id: str, request: Request):
    _admin(request)
    _require_https_or_loopback(request)
    if not KEY_ID.fullmatch(token_id):
        raise HTTPException(422, detail={"error": "invalid_api_key_id"})
    try:
        keys = await asyncio.to_thread(_repository(request).list_managed_api_keys)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "api_key_store_unavailable"}) from exc
    previous = next((row for row in keys if row["token_id"] == token_id and not row.get("revoked_at")), None)
    if previous is None:
        raise HTTPException(404, detail={"error": "active_api_key_not_found"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _issue_key(request, body if isinstance(body, dict) else {},
                            rotate_token_id=token_id, previous=previous)


@router.delete("/keys/{token_id}")
async def revoke_api_key(token_id: str, request: Request):
    _admin(request)
    _require_https_or_loopback(request)
    if not KEY_ID.fullmatch(token_id):
        raise HTTPException(422, detail={"error": "invalid_api_key_id"})
    try:
        revoked = await asyncio.to_thread(
            _repository(request).revoke_managed_api_key, token_id)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "api_key_store_unavailable"}) from exc
    if not revoked:
        raise HTTPException(404, detail={"error": "active_api_key_not_found"})
    await request.app.state.round_distribution.revoke_token(token_id)
    return {"ok": True, "token_id": token_id, "revoked": True}
