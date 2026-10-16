from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from app.live import live_execution_blocker_matrix

router = APIRouter(prefix="/api/rules-engine", tags=["rules engine"])
SHADOW_BOUNDARY_MAX_AGE_SECONDS = 120
CASHOUT_READBACK_MAX_AGE_SECONDS = 300
CASHOUT_REFRESH_READBACK_MAX_AGE_SECONDS = 60
PUBLIC_EXECUTION_INTENT_FIELDS = (
    "intent_id", "decision_id", "session_id", "source_round_id",
    "source_round_index", "target_round_id", "target_round_index",
    "target_stream_id", "selected_rule_ids", "execution_status",
    "execution_eligibility", "blockers", "created_at", "expires_at",
)
PUBLIC_RULE_SESSION_OPTION_FIELDS = {
    "window_size", "max_observed_rounds", "auto_next_check_enabled",
    "win_limit_enabled", "win_limit_count", "loss_limit_enabled",
    "loss_limit_count", "max_selections",
}


def _public_execution_intent(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in PUBLIC_EXECUTION_INTENT_FIELDS if key in row}


def _public_rule_session(row: dict[str, Any]) -> dict[str, Any]:
    """Hide legacy bankroll/executor settings while keeping rule and receipt history."""
    value = deepcopy(row)
    options = value.get("configuration_snapshot")
    if isinstance(options, dict):
        value["configuration_snapshot"] = {
            key: item for key, item in options.items() if key in PUBLIC_RULE_SESSION_OPTION_FIELDS
        }
    value.pop("financial_stop", None)
    value.pop("live_execution_stop", None)
    for selection in value.get("selections", []):
        intent = selection.get("execution_intent")
        if isinstance(intent, dict):
            selection["execution_intent"] = _public_execution_intent(intent)
    rule_selections = value.get("rule_selections")
    if isinstance(rule_selections, dict):
        for selections in rule_selections.values():
            for selection in (selections if isinstance(selections, list) else []):
                intent = selection.get("execution_intent")
                if isinstance(intent, dict):
                    selection["execution_intent"] = _public_execution_intent(intent)
    integrity = value.get("settlement_integrity")
    if not isinstance(integrity, dict):
        legacy = row.get("financial_stop") if isinstance(row.get("financial_stop"), dict) else {}
        integrity = {key: legacy.get(key) for key in (
            "status", "reason", "reconciled_bets", "updated_at") if key in legacy}
        integrity.setdefault("status", "CLEAR")
        integrity.setdefault("reconciled_bets", 0)
    value["settlement_integrity"] = integrity
    return value


def _engine(request: Request):
    engine = getattr(request.app.state, "rules_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="rules_engine_unavailable")
    return engine


def _history(request: Request) -> dict[str, Any]:
    try:
        collector = request.app.state.history_collector.status()
    except Exception:
        collector = {"state": "UNKNOWN", "status": "UNKNOWN",
                     "reason": "COLLECTOR_STATUS_UNAVAILABLE"}
    result = {
        "collector_status": collector,
        "latest_real_round": collector.get("latest") or None,
        "total_history": int(collector["count"]) if collector.get("count") is not None else None,
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


def _shadow_readiness(request: Request, engine=None) -> dict[str, Any]:
    """Readiness for prospective, durable SHADOW observation only.

    This deliberately does not inspect the account balance, betting panels,
    cashout acknowledgement, or LIVE authorization. Those belong to the
    separately reported LIVE_REAL execution gate.
    """
    engine = engine or _engine(request)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    blockers: list[str] = []
    database_connected = False
    boundary = None
    if repository is None or not getattr(repository, "database_url", None):
        blockers.append("AUTHORITATIVE_POSTGRESQL_NOT_CONFIGURED")
    else:
        try:
            with repository.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            database_connected = True
        except Exception:
            blockers.append("POSTGRESQL_UNAVAILABLE")
        if database_connected:
            try:
                boundary = repository.round_distribution_snapshot().get("latest") or None
            except Exception:
                blockers.append("COMMITTED_ROUND_FEED_UNAVAILABLE")

    if boundary:
        if (boundary.get("event_type") != "round.persisted"
                or boundary.get("durability") != "COMMITTED"
                or boundary.get("continuity_status") != "VERIFIED"):
            blockers.append("VERIFIED_COMMITTED_ROUND_REQUIRED")
        else:
            try:
                _require_fresh_shadow_boundary(boundary)
            except ValueError as exc:
                blockers.append(str(exc))
    elif database_connected:
        blockers.append("COMMITTED_ROUND_FEED_UNAVAILABLE")

    collector = getattr(request.app.state, "history_collector", None)
    try:
        collector_status = collector.health_status(persist=False) if collector else {}
    except Exception:
        collector_status = {}
    observed_age = _timestamp_age_seconds(collector_status.get("last_observed_at"))
    collector_live = (
        collector_status.get("collector_process_status") == "RUNNING"
        and collector_status.get("heartbeat_state") == "FRESH"
        and collector_status.get("last_observed_round")
        and observed_age is not None and -60 <= observed_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
    )
    if not collector_live:
        blockers.append("LIVE_ROUND_COLLECTOR_UNAVAILABLE")

    engine_status = engine.status()
    if int(engine_status.get("rules_configured") or 0) < 1:
        blockers.append("RULES_ENGINE_NOT_CONFIGURED")
    if int(engine_status.get("rules_enabled") or 0) < 1:
        blockers.append("NO_ENABLED_RULES")
    blockers = list(dict.fromkeys(blockers))
    return {
        "mode": "SHADOW",
        "status": "READY" if not blockers else "BLOCKED",
        "ready": not blockers,
        "blockers": blockers,
        "database_connected": database_connected,
        "collector_state": collector_status.get("status", "UNKNOWN"),
        "collector_heartbeat": collector_status.get("heartbeat_state", "UNKNOWN"),
        "latest_observed_round": collector_status.get("last_observed_round"),
        "latest_observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "committed_boundary": boundary,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "automatic_execution": "OFF",
    }
def _shared_aviator_readiness(request: Request) -> dict[str, Any]:
    """Expose separately verified browser/page/frame/control evidence.

    Collector heartbeat freshness is required before its point-in-time page
    snapshot can be used. A fresh READY frame snapshot is not downgraded just
    because another cached status object still says UNKNOWN.
    """
    collector = getattr(request.app.state, "history_collector", None)
    try:
        health = collector.health_status(persist=False) if collector else {}
    except Exception:
        health = {}
    now = datetime.now(timezone.utc)
    observed_age = _timestamp_age_seconds(health.get("last_observed_at"), now=now)
    snapshot_current = (
        health.get("collector_process_status") == "RUNNING"
        and health.get("heartbeat_state") == "FRESH"
        and bool(health.get("last_observed_round"))
        and observed_age is not None
        and -60 <= observed_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
    )
    pages = health.get("browser_pages") or {}
    target = pages.get("aviator_target_id")
    topology = (
        pages.get("browser_count") == 1
        and pages.get("context_count") == 1
        and pages.get("page_count") == 1
        and pages.get("aviator_page_count") == 1
        and bool(target)
        and pages.get("aviator_page_state") == "OPEN"
    )
    page_ready = snapshot_current and health.get("browser_connected") is True and topology
    frame_ready = page_ready and pages.get("game_frame_count") == 1 and pages.get("game_frame_state") == "READY"
    history_ready = (
        page_ready
        and pages.get("history_target_id") == target
        and pages.get("history_page_state") == "OPEN"
        and observed_age is not None
        and observed_age <= SHADOW_BOUNDARY_MAX_AGE_SECONDS
    )

    ui_config = health.get("betting_ui_configuration") or {}
    ui_age = _timestamp_age_seconds(ui_config.get("updated_at"), now=now)
    betting_controls_ready = (
        frame_ready
        and pages.get("betting_target_id") == target
        and pages.get("betting_page_state") == "OPEN"
        and ui_config.get("status") == "READY"
        and ui_age is not None
        and -60 <= ui_age <= CASHOUT_READBACK_MAX_AGE_SECONDS
    )

    cashout = health.get("cashout_initialization") or {}
    cashout_age = _timestamp_age_seconds(cashout.get("updated_at"), now=now)
    panels = cashout.get("panels") or []
    panel_values_ready = len(panels) == 2 and all(
        panel.get("status") == "CASHOUT_VERIFIED"
        and panel.get("auto_bet") == "OFF"
        and panel.get("auto_cashout") == "ON"
        and str(panel.get("multiplier") or "").strip() == "2.00"
        and panel.get("auto_tab_selected") is True
        for panel in panels
    )
    cashout_ready = frame_ready and (
        cashout.get("status") == "CASHOUT_VERIFIED"
        and panel_values_ready
        and cashout_age is not None
        and -60 <= cashout_age <= CASHOUT_READBACK_MAX_AGE_SECONDS
    )

    def state(ready: bool, *, known: bool, stale: bool = False) -> str:
        if ready:
            return "READY"
        if stale:
            return "STALE"
        return "BLOCKED" if known else "UNKNOWN"

    result = {
        "checked_at": now.isoformat(),
        "source": "fresh_collector_sidecar",
        "collector_snapshot_current": snapshot_current,
        "collector_state": health.get("status", "UNKNOWN"),
        "heartbeat_state": health.get("heartbeat_state", "UNKNOWN"),
        "last_observed_round": health.get("last_observed_round"),
        "last_observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "betting_window": health.get("betting_window") or {"state": "UNKNOWN", "observed_at": None},
        "target_deadline_evidence": health.get("target_deadline_evidence") or {
            "status": "UNKNOWN", "deadline_at": None,
            "reason": "COLLECTOR_DEADLINE_OBSERVATION_NOT_CURRENT",
        },
        "panel_observations": health.get("panel_observations") or [
            {"panel_id": "PANEL_1", "slot": 0, "state": "UNKNOWN",
             "button_text": None, "displayed_stake": None, "currency": None,
             "cashout_amount": None, "observed_at": None, "source": "UNKNOWN",
             "evidence": {"reason": "COLLECTOR_PANEL_OBSERVATION_NOT_CURRENT"},
             "confidence": 0.0, "frame_identity": None, "pending_bet_state": "UNKNOWN"},
            {"panel_id": "PANEL_2", "slot": 1, "state": "UNKNOWN",
             "button_text": None, "displayed_stake": None, "currency": None,
             "cashout_amount": None, "observed_at": None, "source": "UNKNOWN",
             "evidence": {"reason": "COLLECTOR_PANEL_OBSERVATION_NOT_CURRENT"},
             "confidence": 0.0, "frame_identity": None, "pending_bet_state": "UNKNOWN"},
        ],
        "browser_connected": state(health.get("browser_connected") is True and snapshot_current,
                                    known=snapshot_current),
        "aviator_page_ready": state(page_ready, known=snapshot_current),
        "game_iframe_ready": state(frame_ready, known=page_ready),
        "history_observer_ready": state(history_ready, known=page_ready),
        "betting_controls_ready": state(betting_controls_ready,
            known=frame_ready and ui_config.get("status") in {"READY", "BLOCKED"},
            stale=bool(frame_ready and ui_age is not None and ui_age > CASHOUT_READBACK_MAX_AGE_SECONDS)),
        "cashout_configured": state(cashout_ready,
            known=frame_ready and cashout.get("status") in {"CASHOUT_VERIFIED", "CASHOUT_MISMATCH", "CASHOUT_UNAVAILABLE"},
            stale=bool(frame_ready and cashout_age is not None and cashout_age > CASHOUT_READBACK_MAX_AGE_SECONDS)),
        "browser_pages": pages if snapshot_current else {},
        "betting_ui_configuration": ui_config,
        "cashout_initialization": {
            **cashout,
            "age_seconds": round(cashout_age, 3) if cashout_age is not None else None,
        },
        "panel_observations": health.get("panel_observations") or [
            {"panel_id": "PANEL_1", "slot": 0, "state": "UNKNOWN",
             "button_text": None, "displayed_stake": None, "currency": None,
             "cashout_amount": None, "observed_at": None, "source": "UNKNOWN",
             "evidence": {"reason": "COLLECTOR_PANEL_OBSERVATION_NOT_CURRENT"},
             "confidence": 0.0, "frame_identity": None, "pending_bet_state": "UNKNOWN"},
            {"panel_id": "PANEL_2", "slot": 1, "state": "UNKNOWN",
             "button_text": None, "displayed_stake": None, "currency": None,
             "cashout_amount": None, "observed_at": None, "source": "UNKNOWN",
             "evidence": {"reason": "COLLECTOR_PANEL_OBSERVATION_NOT_CURRENT"},
             "confidence": 0.0, "frame_identity": None, "pending_bet_state": "UNKNOWN"},
        ],
        "platform_cashout_acknowledged": None,
        "automatic_execution": "OFF",
    }
    # A newer verified iframe readback supersedes the collector's older
    # point-in-time UNKNOWN/STALE snapshot. It is accepted only from the
    # shared page, with a recent timestamp and a verified one-page topology.
    live = getattr(request.app.state, "live", None)
    refreshed = getattr(live, "last_cashout_refresh", None) if live else None
    refresh_age = _timestamp_age_seconds((refreshed or {}).get("observed_at"), now=now)
    if (page_ready and isinstance(refreshed, dict)
            and refreshed.get("source") == "SHARED_AVIATOR_FRAME_READBACK"
            and refreshed.get("page_role") == "SHARED_AVIATOR"
            and refreshed.get("browser_topology_verified") is True
            and refresh_age is not None
            and -5 <= refresh_age <= CASHOUT_REFRESH_READBACK_MAX_AGE_SECONDS):
        refreshed_panels = refreshed.get("panels") or []
        panel_rows = [{
            "slot": int(row["panel"]) - 1,
            "status": row.get("status", "CASHOUT_UNAVAILABLE"),
            "auto_bet": row.get("auto_bet", "UNKNOWN"),
            "auto_cashout": row.get("auto_cashout", "UNKNOWN"),
            "multiplier": row.get("multiplier"),
            "auto_tab_selected": row.get("auto_tab_selected"),
            "observed_at": refreshed.get("observed_at"),
            "error_reason": ",".join(row.get("failures") or []) or None,
        } for row in refreshed_panels]
        fresh_cashout_ready = (refreshed.get("status") == "CASHOUT_VERIFIED"
                               and len(panel_rows) == 2
                               and all(row.get("status") == "CASHOUT_VERIFIED" for row in panel_rows))
        result["cashout_configured"] = "READY" if fresh_cashout_ready else "BLOCKED"
        result["cashout_initialization"] = {
            "target": "2.00", "status": refreshed.get("status", "UNKNOWN"),
            "auto_bet": refreshed.get("auto_bet", "UNKNOWN"),
            "panel_1_cashout": panel_rows[0]["status"] if len(panel_rows) > 0 else "UNKNOWN",
            "panel_2_cashout": panel_rows[1]["status"] if len(panel_rows) > 1 else "UNKNOWN",
            "panels": panel_rows, "updated_at": refreshed.get("observed_at"),
            "age_seconds": round(refresh_age, 3),
            "source": refreshed.get("source"), "error_reason": refreshed.get("reason"),
        }
        fresh_controls = refreshed.get("betting_controls") or {}
        if fresh_controls:
            result["betting_controls_ready"] = fresh_controls.get("status", "UNKNOWN")
            result["betting_ui_configuration"] = fresh_controls
        result["platform_cashout_acknowledged"] = refreshed.get("platform_acknowledged")
        result["cashout_readback_source"] = "FRESH_SHARED_AVIATOR_FRAME"
    else:
        result["cashout_readback_source"] = "COLLECTOR_SNAPSHOT"
    return result


@router.get("/status")
def status(request: Request):
    engine = _engine(request)
    mode_manager = getattr(request.app.state, "betting_mode", None)
    mode_status = mode_manager.status() if mode_manager else {}
    shadow_running = mode_status.get("shadow_running") is True
    live_authorized = mode_status.get("live_authorized") is True
    return {**engine.status(), **_history(request),
            "execution": {
                "mode": mode_status.get("mode", "UNKNOWN"),
                "enabled": shadow_running or live_authorized,
                "shadow_running": shadow_running,
                "live_authorized": live_authorized,
                "automatic_execution": "OFF",
            },
            "shadow_readiness": _shadow_readiness(request, engine)}


@router.post("/shadow-executor/start")
async def start_shadow_executor(request: Request):
    """Deprecated entry point: rule observation sessions do not run executors."""
    return JSONResponse(status_code=410, content={"ok": False,
        "error": "RULES_ENGINE_IS_OBSERVE_ONLY",
        "message": "Configure the platform manually; execution remains disabled.",
        "real_money_action_submitted": False})


@router.get("/ui-setup")
async def ui_setup(request: Request):
    """Compatibility response: manual stake setup belongs to the operator."""
    return _engine(request).ui_setup_configuration()


@router.get("/registry")
def registry(request: Request):
    return _engine(request).registry()


@router.get("/sessions")
def sessions(request: Request):
    return {"sessions": _engine(request).sessions_status()}


@router.get("/execution-intents")
def execution_intents(request: Request, limit: int = 100):
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    try:
        return {"intents": [_public_execution_intent(row)
                            for row in repository.list_rule_execution_intents(limit)]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="execution_intent_store_unavailable") from exc


@router.get("/dashboard")
def dashboard(request: Request):
    engine = _engine(request)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    status_row = engine.status()
    latest = _history(request)
    active = status_row.get("active_session") or {}
    detail = engine.session(active.get("session_id")) if active.get("session_id") else None
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
    settlement = ((detail or {}).get("settlement_integrity")
                  or (active or {}).get("settlement_integrity")
                  or (detail or {}).get("financial_stop")
                  or (active or {}).get("financial_stop")
                  or {"status": "CLEAR", "reason": None, "reconciled_bets": 0})
    settlement = {key: settlement.get(key) for key in
                  ("status", "reason", "reconciled_bets", "updated_at")}
    blockers = ["REAL_MONEY_EXECUTION_DISABLED_PENDING_LIVE_QUALIFICATION",
                "EXPLICIT_OPERATOR_AUTHORIZATION_REQUIRED"]
    if settlement.get("status") == "RECONCILIATION_REQUIRED":
        blockers.append("RULE_SESSION_TRANSACTION_RECONCILIATION_REQUIRED")
    if repository:
        try:
            if repository.open_executions():
                blockers.append("UNRECONCILED_EXECUTION_REQUIRES_RECONCILIATION")
        except Exception:
            blockers.append("EXECUTION_DATABASE_UNAVAILABLE")
    evaluations = (latest_evaluation or {}).get("rule_evaluations") or []
    public_intents = ([_public_execution_intent(row)
                       for row in repository.list_rule_execution_intents(50)]
                      if repository else [])
    return {
        "ok": True,
        "current_round": latest.get("latest_real_round"),
        "collector_status": latest.get("collector_status"),
        "selected_rules": list((detail or {}).get("rule_ids") or []),
        "rule_evaluations": evaluations,
        "shared_decision": shared_decision,
        "shared_selection": ({"action": "SELECT", "reason": decision.get("reason"),
                               "true_rule_ids": list(decision.get("true_rule_ids") or []),
                               "selection_id": decision.get("selection_id")}
                              if decision.get("action") == "SELECT" else None),
        "session": active or None,
        "settlement_integrity": settlement,
        "execution_readiness": {"status": "BLOCKED", "ready": False,
                                "blockers": list(dict.fromkeys(blockers)),
                                "blocker_matrix": live_execution_blocker_matrix()},
        "shadow_result_summary": {
            "wins": (detail or {}).get("total_wins", 0),
            "losses": (detail or {}).get("total_losses", 0),
            "label": "RULE OBSERVATION OUTCOMES",
        },
        "execution_intents": public_intents,
        "real_money_execution": "DISABLED",
    }


@router.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str):
    result = _engine(request).session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return _public_rule_session(result)


@router.put("/configuration/{rule_id}")
async def configure_rule(request: Request, rule_id: str):
    try:
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise ValueError("enabled_must_be_boolean")
        saved = _engine(request).configure(rule_id, enabled=body["enabled"], parameters=body.get("parameters"))
        return {"rule_id": rule_id, **saved}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/presets")
async def save_preset(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("invalid_preset")
        return _engine(request).save_preset(body.get("name"), body.get("mode"),
                                             body.get("rule_ids"), body.get("options"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/presets")
def presets(request: Request):
    return {"presets": _engine(request).status().get("presets", {})}


@router.post("/sessions")
async def start_session(request: Request):
    import asyncio

    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    readiness = _shadow_readiness(request)
    if not readiness["ready"]:
        raise HTTPException(status_code=503,
            detail="SHADOW_SESSION_START_BLOCKED:" + ",".join(readiness["blockers"]))
    try:
        snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
        boundary = snapshot.get("latest") or {}
        if boundary.get("event_type") != "round.persisted" or boundary.get("durability") != "COMMITTED":
            raise ValueError("verified_committed_round_boundary_required")
        _require_fresh_shadow_boundary(boundary)
        context: list[dict[str, Any]] = []
        first_sequence = max(0, int(boundary.get("stream_sequence") or 0) - 5)
        if boundary.get("stream_id"):
            context = await asyncio.to_thread(repository.round_distribution_events,
                boundary["stream_id"], first_sequence, int(boundary.get("stream_sequence") or 0), 10)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("invalid_session_configuration")
        replace_active_session = body.get("replace_active_session", False)
        if not isinstance(replace_active_session, bool):
            raise ValueError("invalid_replace_active_session")
        session = _engine(request).start_session(mode=body.get("mode"), rule_ids=body.get("rule_ids"),
            boundary=boundary, prestart_context=context, options=body.get("options"),
            replace_active_session=replace_active_session)
        return session
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"rules_session_start_unavailable:{type(exc).__name__}") from exc


@router.post("/sessions/{session_id}/{action}")
def session_action(request: Request, session_id: str, action: str):
    try:
        return _engine(request).session_action(session_id, action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/evaluate")
def evaluate(request: Request):
    # Selection is created only from the ordered committed-event dispatcher.
    latest = _history(request).get("latest_real_round")
    return _engine(request).evaluate(latest)
