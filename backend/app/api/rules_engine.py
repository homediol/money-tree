from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from app.betting.browser_client import AviatorBrowserClient

router = APIRouter(prefix="/api/rules-engine", tags=["rules engine"])
SHADOW_BOUNDARY_MAX_AGE_SECONDS = 120
CASHOUT_READBACK_MAX_AGE_SECONDS = 300
CASHOUT_REFRESH_READBACK_MAX_AGE_SECONDS = 60


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
    """Start the existing paper executor from the explicit Rules session setup."""
    import asyncio

    from app.api.live import _manual_setup_observation
    from app.betting.browser_client import AviatorBrowserClient
    from app.services.betting_mode import SHADOW_REALISTIC

    app = request.app
    engine = _engine(request)
    modes = getattr(app.state, "betting_mode", None)
    shadow = getattr(app.state, "shadow", None)
    live = getattr(app.state, "live", None)
    risk = getattr(app.state, "risk", None)
    if (modes is None or modes.mode != SHADOW_REALISTIC
            or modes.live_authorized or bool(live and live.live_active)):
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "SHADOW_REALISTIC_MODE_REQUIRED", "real_money_action_submitted": False})
    if risk is None or risk.emergency_latched:
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "EMERGENCY_STOP_ACTIVE_OR_RISK_UNAVAILABLE",
            "real_money_action_submitted": False})

    configuration = engine.ui_setup_configuration()
    active = (engine.session(configuration.get("session_id"))
              if configuration.get("session_id") else None)
    if (configuration.get("status") != "CONFIGURED" or not active
            or active.get("status") != "RUNNING"
            or active.get("selection_status") != "ACTIVE"):
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "ACTIVE_RULES_SESSION_WITH_PANEL_AND_BIF_STAKE_REQUIRED",
            "rules_configuration": configuration, "real_money_action_submitted": False})
    panel = configuration.get("execution_panel")
    stake = configuration.get("requested_stake")
    currency = str(configuration.get("requested_stake_currency") or "").upper()
    if type(panel) is not int or panel not in (0, 1) or type(stake) is not int or currency != "BIF":
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "SELECTED_PANEL_FIXED_BIF_STAKE_REQUIRED",
            "rules_configuration": configuration, "real_money_action_submitted": False})

    # The risk profile is an explicit saved operator configuration, not the
    # Risk Manager's startup default. It must match this Rules session.
    saved = modes.live_configuration()
    if (saved.get("configuration_valid") is not True
            or saved.get("execution_panel") != panel
            or saved.get("stake_amount") != stake
            or str(saved.get("currency") or "").upper() != currency):
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "SAVED_OPERATOR_CONFIGURATION_MISMATCH",
            "expected": {"panel": panel + 1, "stake": stake, "currency": currency},
            "saved": {"panel": (saved.get("execution_panel") + 1
                                    if saved.get("execution_panel") in (0, 1) else None),
                      "stake": saved.get("stake_amount"), "currency": saved.get("currency")},
            "real_money_action_submitted": False})

    client = AviatorBrowserClient(app.state.betting.settings)
    try:
        snapshot = await asyncio.wait_for(client.snapshot(), timeout=8)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"ok": False,
            "error": "SHARED_AVIATOR_READBACK_UNAVAILABLE",
            "detail": type(exc).__name__, "real_money_action_submitted": False})
    finally:
        try:
            await client.close()
        except Exception:
            pass
    observation, readbacks = _manual_setup_observation(snapshot, configuration,
        getattr(live, "manual_setup_readbacks", None))
    if live is not None:
        live.manual_setup_readbacks = readbacks
    checklist = {row.get("item"): row.get("status") for row in observation.get("checklist", [])}
    cashout = (observation.get("cashout_panels") or [])[panel]
    blockers = []
    if checklist.get("Stake manually entered") != "STAKE_VERIFIED":
        blockers.append("STAKE_MISMATCH" if checklist.get("Stake manually entered") == "STAKE_MISMATCH"
                        else "STAKE_READBACK_UNAVAILABLE")
    if cashout.get("status") != "CASHOUT_VERIFIED":
        blockers.append("AUTO_CASHOUT_CONFIGURATION_NOT_VERIFIED")
    if str(observation.get("account_currency") or "").upper() != currency:
        blockers.append("ACCOUNT_CURRENCY_MISMATCH")
    if checklist.get("No active or pending bet") != "VERIFIED":
        blockers.append("ACTIVE_OR_PENDING_BET")
    if blockers:
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "MANUAL_AVIATOR_SETUP_REQUIRES_OPERATOR_CORRECTION",
            "blockers": blockers, "manual_setup": observation,
            "real_money_action_submitted": False})

    profile = str(saved.get("profile") or "").upper()
    if shadow and shadow.mode == "SHADOW" and modes.shadow_ready:
        current_config = shadow.session.get("config") or {}
        panels = current_config.get("panels") or []
        matching = (current_config.get("mode") == "MANUAL" and len(panels) == 2
                    and sum(bool(row.get("enabled")) for row in panels) == 1
                    and panels[panel].get("enabled") is True
                    and panels[panel].get("stake") == stake
                    and float(panels[panel].get("cashout") or 0) == 2.0)
        if matching:
            return {"ok": True, "already_running": True,
                    "status": modes.status(), "manual_setup": observation,
                    "real_money_action_submitted": False}
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "SHADOW_EXECUTOR_ALREADY_RUNNING_WITH_DIFFERENT_CONFIGURATION",
            "status": modes.status(), "real_money_action_submitted": False})

    shadow_configuration = {
        "mode": "MANUAL",
        "panels": [{"enabled": slot == panel,
                    "stake": stake if slot == panel else None,
                    "cashout": 2.0} for slot in (0, 1)],
    }
    result = await modes.switch_shadow(profile=profile, configuration=shadow_configuration)
    if not result.get("ok"):
        return JSONResponse(status_code=409, content={**result,
            "manual_setup": observation, "real_money_action_submitted": False})
    return {**result, "manual_setup": observation, "real_money_action_submitted": False,
            "execution_kind": "SHADOW_REALISTIC_PAPER_ONLY"}


@router.get("/ui-setup")
async def ui_setup(request: Request):
    """Read-only, fail-closed contract for synchronizing one panel's stake.

    The endpoint does not depend on a TRUE rule and cannot authorize a bet.
    """
    import asyncio
    configuration = _engine(request).ui_setup_configuration()
    betting = getattr(request.app.state, "betting", None)
    if betting is None or getattr(betting, "settings", None) is None:
        return {**configuration, "status": "BLOCKED", "reason": "BETTING_BROWSER_SETTINGS_UNAVAILABLE",
                "authentication_state": "UNKNOWN", "page_role": "UNKNOWN",
                "browser_topology_verified": False, "platform_currency": None,
                "pending_bet_states": []}

    client = AviatorBrowserClient(betting.settings)
    try:
        readiness = await client.readiness()
        snapshot = await client.snapshot()
    except Exception:
        readiness, snapshot = {}, None
    finally:
        try:
            await client.close()
        except Exception:
            pass

    authentication_state = readiness.get("authentication_state", "UNKNOWN")
    platform_currency = getattr(snapshot, "balance_currency", None) if snapshot else None
    panel_states = getattr(snapshot, "bet_panel_states", []) if snapshot else []
    pending_states = [row.get("pending_bet_state", "UNKNOWN") for row in panel_states]
    blockers = []
    if configuration.get("status") != "CONFIGURED":
        blockers.append(configuration.get("reason") or "SESSION_STAKE_CONFIGURATION_NOT_READY")
    if authentication_state != "AUTHENTICATED":
        blockers.append(f"SHARED_AVIATOR_AUTHENTICATION_{authentication_state}")
    if readiness.get("page_role") != "SHARED_AVIATOR" or readiness.get("browser_topology_verified") is not True:
        blockers.append("SHARED_AVIATOR_PAGE_TOPOLOGY_UNVERIFIED")
    if not snapshot or snapshot.ok is not True or len(panel_states) != 2:
        blockers.append("TWO_PANEL_BETTING_UI_UNVERIFIED")
    elif any(state != "CLEAR" for state in pending_states):
        blockers.append("ACTIVE_OR_UNKNOWN_BET_STATE")
    if not platform_currency:
        blockers.append("PLATFORM_CURRENCY_UNVERIFIED")
    elif configuration.get("requested_stake_currency") != platform_currency:
        blockers.append("STAKE_CURRENCY_MISMATCH")
    reason = blockers[0] if blockers else None
    return {
        **configuration,
        "status": "READY" if not blockers else "BLOCKED",
        "reason": reason,
        "blockers": list(dict.fromkeys(blockers)),
        "authentication_state": authentication_state,
        "page_role": readiness.get("page_role", "UNKNOWN"),
        "browser_topology_verified": readiness.get("browser_topology_verified") is True,
        "platform_currency": platform_currency,
        "pending_bet_states": pending_states,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "automatic_execution": "OFF",
    }


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
        return {"intents": repository.list_rule_execution_intents(limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="execution_intent_store_unavailable") from exc


@router.get("/dashboard")
def dashboard(request: Request):
    engine = _engine(request)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    status_row = engine.status()
    latest = _history(request)
    shared_readiness = _shared_aviator_readiness(request)
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
    mode = (getattr(request.app.state, "betting_mode", None).status()
            if getattr(request.app.state, "betting_mode", None) else {})
    balance = repository.latest_platform_balance_observation() if repository else None
    balance_age_s = None
    if balance and balance.get("observed_at"):
        try:
            observed = datetime.fromisoformat(str(balance["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            balance_age_s = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
        except (TypeError, ValueError):
            balance_age_s = None
    live_component = getattr(request.app.state, "live", None)
    live_session = ((getattr(live_component, "status", lambda: {})() or {}).get("session") or {})
    application_session_id = (live_session.get("session_id") or active.get("session_id")
                              or (balance or {}).get("application_session_id"))
    financials = (repository.rules_session_financials(str(application_session_id))
                  if repository and application_session_id else None)
    config = (detail or {}).get("configuration_snapshot") or {}
    financial_stop = (detail or {}).get("financial_stop") or (active or {}).get("financial_stop") or {
        "status": "ACTIVE", "reason": None, "realized_betting_pl": None,
        "currency": None, "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
    }
    blockers = ["REAL_MONEY_EXECUTION_DISABLED_UNTIL_SEPARATE_AUTHORIZATION_AND_VERIFICATION"]
    if not balance or balance.get("verified") is not True:
        blockers.append("AUTHENTICATED_PLATFORM_BALANCE_UNVERIFIED")
    elif balance_age_s is None or balance_age_s > 20:
        blockers.append("PLATFORM_BALANCE_OBSERVATION_STALE")
    if not balance or not balance.get("currency"):
        blockers.append("PLATFORM_BALANCE_CURRENCY_UNVERIFIED")
    browser_pages = shared_readiness.get("browser_pages") or {}
    browser_topology_verified = (shared_readiness.get("aviator_page_ready") == "READY"
                                 and shared_readiness.get("game_iframe_ready") == "READY")
    if not browser_topology_verified:
        blockers.append("SHARED_AVIATOR_PAGE_READY_UNVERIFIED")
    if shared_readiness.get("betting_controls_ready") != "READY":
        blockers.append("BETTING_PANEL_CONTROLS_UNAVAILABLE")
    cashout_initialization = shared_readiness.get("cashout_initialization") or {}
    panels = cashout_initialization.get("panels") or []
    panel_cashout_readback = shared_readiness.get("cashout_configured") == "READY"
    balance_cashout = (balance or {}).get("cashout_configuration") or {}
    cashout_acknowledged = balance_cashout.get("platform_acknowledged") is True
    cashout = {
        "target": 2.0,
        "verified": panel_cashout_readback,
        "status": shared_readiness.get("cashout_configured", "UNKNOWN"),
        "updated_at": cashout_initialization.get("updated_at"),
        "age_seconds": cashout_initialization.get("age_seconds"),
        "panel_1_cashout": cashout_initialization.get("panel_1_cashout", "UNKNOWN"),
        "panel_2_cashout": cashout_initialization.get("panel_2_cashout", "UNKNOWN"),
        "panels": panels,
        "platform_acknowledged": True if cashout_acknowledged else None,
        "failures": (cashout_initialization.get("error_reason") and [cashout_initialization.get("error_reason")])
            or ([] if panel_cashout_readback else ["CASHOUT_CONFIGURATION_UNVERIFIED_OR_STALE"]),
    }
    if not panel_cashout_readback:
        blockers.extend(cashout.get("failures") or ["CASHOUT_CONFIGURATION_UNVERIFIED"])
    if not cashout_acknowledged:
        blockers.append("PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED")
    if mode.get("live_authorized") is not True:
        blockers.append("EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED")
    if financial_stop.get("status") == "RECONCILIATION_REQUIRED":
        blockers.append("RULE_SESSION_TRANSACTION_RECONCILIATION_REQUIRED")
    elif financial_stop.get("status") == "STOPPED":
        blockers.append("RULE_SESSION_STOP_LIMIT_REACHED")
    if repository:
        try:
            if repository.open_executions():
                blockers.append("UNRECONCILED_EXECUTION_REQUIRES_RECONCILIATION")
        except Exception:
            blockers.append("EXECUTION_DATABASE_UNAVAILABLE")
    evaluations = (latest_evaluation or {}).get("rule_evaluations") or []
    return {
        "ok": True,
        "current_round": latest.get("latest_real_round"),
        "collector_status": latest.get("collector_status"),
        "selected_rules": list((detail or {}).get("rule_ids") or []),
        "rule_evaluations": evaluations,
        "shared_decision": shared_decision,
        "shared_selection": decision if decision.get("action") == "SELECT" else None,
        "session": active or None,
        "cashout_configuration": cashout,
        "shared_aviator_readiness": shared_readiness,
        "balance": balance,
        "balance_age_seconds": balance_age_s,
        "browser_topology": {"verified": browser_topology_verified, "pages": browser_pages},
        "financials": financials,
        "financial_stop": financial_stop,
        "session_controls": {
            "scope": "combined rule session; one result per shared selection",
            "profit_target": config.get("profit_target_rwf"),
            "profit_target_enabled": config.get("profit_target_enabled"),
            "loss_limit": config.get("loss_limit_rwf"),
            "loss_limit_enabled": config.get("loss_limit_enabled"),
            "win_limit_count": config.get("win_limit_count"),
            "loss_limit_count": config.get("loss_limit_count"),
            "reconciled_profit_target_rwf": config.get("reconciled_profit_target_rwf"),
            "reconciled_profit_target_enabled": config.get("reconciled_profit_target_enabled"),
            "reconciled_loss_limit_rwf": config.get("reconciled_loss_limit_rwf"),
            "reconciled_loss_limit_enabled": config.get("reconciled_loss_limit_enabled"),
            "reconciled_win_limit_count": config.get("reconciled_win_limit_count"),
            "reconciled_win_limit_enabled": config.get("reconciled_win_limit_enabled"),
            "reconciled_loss_count_limit": config.get("reconciled_loss_count_limit"),
            "reconciled_loss_count_limit_enabled": config.get("reconciled_loss_count_limit_enabled"),
            "maximum_selected_rounds": config.get("max_selections"),
            "manual_stop_available": True,
            "financial_stop_status": financial_stop.get("status"),
            "financial_stop_reason": financial_stop.get("reason"),
        },
        "execution_readiness": {"status": "BLOCKED", "ready": False,
                                "blockers": list(dict.fromkeys(blockers))},
        "realized_betting_pl": (financials or {}).get("realized_betting_pl"),
        "shadow_result_summary": {
            "net_profit_rwf": (detail or {}).get("net_profit_rwf"),
            "wins": (detail or {}).get("total_wins", 0),
            "losses": (detail or {}).get("total_losses", 0),
            "label": "RULE OBSERVATION RESULTS; NOT RECONCILED BETTING P/L",
        },
        "execution_intents": repository.list_rule_execution_intents(50) if repository else [],
        "real_money_execution": "DISABLED",
    }


@router.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str):
    result = _engine(request).session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return result


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
        session = _engine(request).start_session(mode=body.get("mode"), rule_ids=body.get("rule_ids"),
            boundary=boundary, prestart_context=context, options=body.get("options"))
        # Balance is optional enrichment for the dashboard. It must never turn
        # a successfully persisted SHADOW session into a 503 response.
        try:
            balance = await asyncio.to_thread(repository.latest_platform_balance_observation)
        except Exception:
            balance = None
        if isinstance(balance, dict) and balance.get("verified") is True:
            try:
                observed = datetime.fromisoformat(str(balance.get("observed_at") or "").replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                age = float("inf")
            if 0 <= age <= 20:
                start_balance = await asyncio.to_thread(
                    repository.bind_rule_session_starting_balance,
                    str(session["session_id"]), balance)
                if start_balance:
                    session["starting_balance_observation"] = {
                        "observation_id": balance.get("observation_id"),
                        "amount": start_balance.get("starting_balance"),
                        "currency": start_balance.get("starting_balance_currency"),
                        "observed_at": balance.get("observed_at"),
                        "provider_identity": balance.get("provider_identity"),
                        "session_identity": balance.get("session_identity"),
                    }
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
