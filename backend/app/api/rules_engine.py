from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from app.betting.browser_client import AviatorBrowserClient

router = APIRouter(prefix="/api/rules-engine", tags=["rules engine"])
SHADOW_BOUNDARY_MAX_AGE_SECONDS = 120
CASHOUT_READBACK_MAX_AGE_SECONDS = 300
CASHOUT_REFRESH_READBACK_MAX_AGE_SECONDS = 60
_RETIRED_RISK_KEY_FRAGMENTS = (
    "risk", "bankroll", "drawdown", "consecutive_loss",
    "minimum_balance", "min_balance", "profile",
)
_NET_OUTCOME_LIMIT_KEYS = {"loss_limit_enabled", "loss_limit_count"}
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
        panel.get("status") == "READY"
        and panel.get("auto_bet") == "OFF"
        and panel.get("auto_cashout") == "ON"
        and str(panel.get("multiplier") or "").strip() == "2.00"
        and panel.get("auto_tab_selected") is True
        for panel in panels
    )
    cashout_ready = frame_ready and (
        cashout.get("status") == "READY"
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
        "browser_connected": state(health.get("browser_connected") is True and snapshot_current,
                                    known=snapshot_current),
        "aviator_page_ready": state(page_ready, known=snapshot_current),
        "game_iframe_ready": state(frame_ready, known=page_ready),
        "history_observer_ready": state(history_ready, known=page_ready),
        "betting_controls_ready": state(betting_controls_ready,
            known=frame_ready and ui_config.get("status") in {"READY", "BLOCKED"},
            stale=bool(frame_ready and ui_age is not None and ui_age > CASHOUT_READBACK_MAX_AGE_SECONDS)),
        "cashout_configured": state(cashout_ready,
            known=frame_ready and cashout.get("status") in {"READY", "FAILED"},
            stale=bool(frame_ready and cashout_age is not None and cashout_age > CASHOUT_READBACK_MAX_AGE_SECONDS)),
        "browser_pages": pages if snapshot_current else {},
        "betting_ui_configuration": ui_config,
        "cashout_initialization": {
            **cashout,
            "age_seconds": round(cashout_age, 3) if cashout_age is not None else None,
        },
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
            "status": row.get("status", "UNKNOWN"),
            "auto_bet": row.get("auto_bet", "UNKNOWN"),
            "auto_cashout": row.get("auto_cashout", "UNKNOWN"),
            "multiplier": row.get("multiplier"),
            "auto_tab_selected": row.get("auto_tab_selected"),
            "observed_at": refreshed.get("observed_at"),
            "error_reason": ",".join(row.get("failures") or []) or None,
        } for row in refreshed_panels]
        fresh_cashout_ready = (refreshed.get("status") == "READY"
                               and len(panel_rows) == 2
                               and all(row.get("status") == "READY" for row in panel_rows))
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
    result = _without_retired_risk_data({**engine.status(), **_history(request),
            "shadow_readiness": _shadow_readiness(request, engine)})
    result["shadow_signal_bridge"] = getattr(
        request.app.state, "shadow_signal_bridge_status", {"state": "UNAVAILABLE"})
    return result


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
    return _without_retired_risk_data(_engine(request).registry())


@router.get("/sessions")
def sessions(request: Request):
    return _without_retired_risk_data({"sessions": _engine(request).sessions_status()})


@router.get("/execution-intents")
def execution_intents(request: Request, limit: int = 100):
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    try:
        return _without_retired_risk_data({
            "intents": repository.list_rule_execution_intents(limit),
        })
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
    reporting_session = active or status_row.get("latest_session") or {}
    reporting_detail = (detail if active else
                        engine.session(reporting_session.get("session_id"))
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
    config = ((detail or reporting_detail or {}).get("configuration_snapshot") or {})
    reconciled_financials = ((detail or {}).get("reconciled_financials")
                             or (active or {}).get("reconciled_financials") or {})
    blockers = ["REAL_MONEY_EXECUTION_DISABLED_UNTIL_SEPARATE_AUTHORIZATION_AND_VERIFICATION"]
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
    if repository:
        try:
            if repository.open_executions():
                blockers.append("UNRECONCILED_EXECUTION_REQUIRES_RECONCILIATION")
        except Exception:
            blockers.append("EXECUTION_DATABASE_UNAVAILABLE")
    evaluations = (latest_evaluation or {}).get("rule_evaluations") or []
    return _without_retired_risk_data({
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
        "reconciled_financials": reconciled_financials,
        "session_controls": {
            "scope": "combined rule session; one result per shared selection",
            "profit_target": config.get("profit_target_rwf"),
            "profit_target_enabled": config.get("profit_target_enabled"),
            "win_limit_enabled": config.get("win_limit_enabled", False),
            "win_limit_count": config.get("win_limit_count"),
            "loss_limit_enabled": config.get("loss_limit_enabled", False),
            "loss_limit_count": config.get("loss_limit_count"),
            "net_outcome_limit_version": config.get("net_outcome_limit_version"),
            "maximum_selected_rounds": config.get("max_selections"),
            "manual_stop_available": True,
        },
        "execution_readiness": {"status": "BLOCKED", "ready": False,
                                "blockers": list(dict.fromkeys(blockers))},
        "realized_betting_pl": (financials or {}).get("realized_betting_pl"),
        "shadow_result_summary": {
            "session_id": reporting_session.get("session_id"),
            "session_status": reporting_session.get("status"),
            "net_profit_rwf": (reporting_detail or {}).get("net_profit_rwf"),
            "wins": (reporting_detail or {}).get("total_wins", 0),
            "losses": (reporting_detail or {}).get("total_losses", 0),
            "pending": (reporting_detail or {}).get("results", {}).get("PENDING", 0),
            "net_outcome": (reporting_detail or {}).get("net_outcome", 0),
            "stop_reason": (reporting_detail or {}).get("stop_reason"),
            "stop_reason_source": (reporting_detail or {}).get("stop_reason_source"),
            "stop_reason_at": (reporting_detail or {}).get("stop_reason_at"),
            "label": "RULE OBSERVATION RESULTS; NOT RECONCILED BETTING P/L",
        },
        "execution_intents": repository.list_rule_execution_intents(50) if repository else [],
        "real_money_execution": "DISABLED",
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
def session_detail(request: Request, session_id: str):
    result = _engine(request).session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return _without_retired_risk_data(result)


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
        replace_session_id = body.get("replace_session_id")
        if replace_session_id is not None and not isinstance(replace_session_id, str):
            raise ValueError("invalid_session_replacement")
        session = await asyncio.to_thread(_engine(request).start_session,
            mode=body.get("mode"), rule_ids=body.get("rule_ids"),
            boundary=boundary, prestart_context=context, options=body.get("options"),
            replace_session_id=replace_session_id)
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
        status_code = 409 if str(exc) in {
            "SESSION_ALREADY_ACTIVE_FOR_MODE", "SESSION_NOT_RESTARTABLE",
            "SESSION_REQUIRES_RECONCILIATION", "SESSION_RECONCILIATION_STATUS_UNAVAILABLE",
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

    modes = getattr(request.app.state, "betting_mode", None)
    live = getattr(request.app.state, "live", None)
    betting = getattr(request.app.state, "betting", None)
    betting_status = await asyncio.to_thread(betting.status) if betting is not None else {}
    if (getattr(modes, "mode", None) == "LIVE_REAL"
            or bool(getattr(modes, "live_authorized", False))
            or bool(getattr(live, "live_active", False))
            or (betting_status.get("mode") == "REAL" and betting_status.get("automatic_enabled"))):
        raise HTTPException(status_code=409, detail="DISARM_LIVE_BEFORE_RULES_SESSION_RECONFIGURATION")

    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        raise HTTPException(status_code=503, detail="authoritative_postgresql_required")
    readiness = await asyncio.to_thread(_shadow_readiness, request, engine)
    if not readiness["ready"]:
        raise HTTPException(status_code=503,
            detail="RULES_SESSION_RECONFIGURATION_BLOCKED:" + ",".join(readiness["blockers"]))

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
