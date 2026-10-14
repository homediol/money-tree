"""REST surface for the betting module.

Lifecycle events are additionally pushed over /ws/live as flat dicts with
``type`` starting ``betting:`` (state / ledger / notice).
"""
from __future__ import annotations

import logging
import math
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.betting.browser_client import AviatorBrowserClient
from app.betting.cashout import inspect_cashout_configuration
from app.betting.profiles import profile_keys
from app.betting.schemas import (
    AutomaticStartRequest,
    DecisionIntent,
    SessionStartRequest,
    profile_choices,
)
from app.betting.session import BettingError

log = logging.getLogger("betting.api")
router = APIRouter(prefix="/api/betting", tags=["betting"])


def _betting(request: Request):
    manager = getattr(request.app.state, "betting", None)
    if manager is None:
        raise BettingError("betting manager not initialised")
    return manager


def _risk(request: Request):
    return getattr(request.app.state, "risk", None)


def _health_gate(request: Request, mode: str = "REAL"):
    health = getattr(request.app.state, "system_health", None)
    return health.can_bet_now(mode=mode) if health else {"allowed": False, "reasons": ["health monitor unavailable"]}


def _conflict(exc: BettingError) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "ok": False,
        "error": exc.code,
        "message": str(exc),
    })


@router.get("/status")
async def status(request: Request):
    manager = _betting(request)
    return {"ok": True, "status": manager.status()}


@router.get("/profiles")
async def profiles(request: Request):
    return {"ok": True, "profiles": profile_choices(),
            "keys": profile_keys()}


@router.post("/start")
async def start_automatic(request: Request, body: AutomaticStartRequest):
    """Automatic start stays blocked until a configured Rules Engine exists."""
    return JSONResponse(status_code=409, content={
        "ok": False, "error": "live_activation_required",
        "message": "Use POST /api/live/start with explicit ENABLE LIVE BETTING "
                   "confirmation and platform verification",
    })


@router.post("/stop")
async def stop_automatic(request: Request):
    try:
        modes = getattr(request.app.state, "betting_mode", None)
        if modes and modes.mode == "LIVE_REAL":
            return {"ok": True, "status": await modes.force_safe("manual_stop")}
        status = await _betting(request).stop_session(emergency=False)
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/pause")
async def pause_automatic(request: Request):
    try:
        modes = getattr(request.app.state, "betting_mode", None)
        if modes and modes.mode == "LIVE_REAL":
            return {"ok": True, "status": await modes.force_safe("manual_pause")}
        status = await _betting(request).stop_session(reason="manual_pause")
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/resume")
async def resume_automatic(request: Request):
    manager = _betting(request)
    if getattr(request.app.state, "betting_mode", None):
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "backend_mode_switch_required",
            "message": "Session resumes require the reviewed backend mode switch",
        })
    if manager.last_request is not None and manager.last_request.mode == "REAL":
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "fresh_live_activation_required",
            "message": "REAL sessions require a fresh /api/live/start activation",
        })
    try:
        health = getattr(request.app.state, "system_health", None)
        gate = health.can_bet_now(mode="REAL") if health else {"allowed": False, "reasons": ["health unavailable"]}
        if not gate.get("allowed"):
            return JSONResponse(status_code=409, content={"ok": False,
                "error": "safety_gate_blocked", "message": "NO RESUME",
                "reasons": gate.get("reasons", [])})
        status = await manager.resume_session()
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/observe")
async def observe(request: Request):
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "orchestrator_unavailable"})
    return {"ok": True, "status": await orchestrator.observe()}


@router.get("/balance")
async def balance(request: Request):
    status = _betting(request).status()
    return {
        "ok": True,
        "verified": status.get("current_balance") is not None,
        "current_balance": status.get("current_balance"),
        "balance_text": status.get("last_balance_text", ""),
    }


@router.get("/current")
async def current_bet(request: Request):
    status = _betting(request).status()
    return {"ok": True, "current_bet": status.get("current_bet"),
            "current_round": status.get("current_round")}


@router.get("/browser")
async def browser_readiness(request: Request, include_snapshot: bool = False):
    """Read-only live browser verification (no session required).

    Pure observation: discovers the Aviator page/frame targets through the CDP
    endpoint and, when the game DOM is reachable, reports balance + panel
    readiness. Never clicks, never mutates.
    """
    manager = _betting(request)
    client = AviatorBrowserClient(manager.settings)
    readiness = None
    try:
        readiness = await client.readiness()
        payload: dict = {"ok": True, "browser": readiness}
        if include_snapshot:
            snap = await client.snapshot()
            snapshot = snap.as_dict() if hasattr(snap, "as_dict") else snap
            payload["snapshot"] = snapshot
            balance = snapshot.get("balance") if isinstance(snapshot, dict) else None
            authentication_state = str((snapshot or {}).get("authentication_state") or
                                       readiness.get("authentication_state") or "UNKNOWN")
            numeric_balance = bool(isinstance(snapshot, dict)
                            and snapshot.get("ok") and isinstance(balance, (int, float))
                            and not isinstance(balance, bool) and math.isfinite(balance) and balance >= 0)
            verified = bool(readiness.get("connected") and numeric_balance
                            and authentication_state == "AUTHENTICATED")
            cashout = inspect_cashout_configuration(next((item for item in
                (snapshot or {}).get("cashout_controls", []) if item.get("slot") == 0), None))
            live_controller = getattr(request.app.state, "live", None)
            live_session = ((live_controller.status() or {}).get("session") or {}) if live_controller else {}
            rules_engine = getattr(request.app.state, "rules_engine", None)
            rules_session = ((rules_engine.status() or {}).get("active_session") or {}) if rules_engine else {}
            observation = {
                "verified": verified, "balance": balance if verified else None,
                "displayed_amount": balance if numeric_balance else None,
                "currency": snapshot.get("balance_currency") if isinstance(snapshot, dict) else None,
                "currency_verified": bool((snapshot or {}).get("balance_currency")),
                "authentication_state": authentication_state,
                "provider_identity": (snapshot or {}).get("provider_identity"),
                "session_identity": (snapshot or {}).get("session_identity"),
                "application_session_id": live_session.get("session_id") or rules_session.get("session_id"),
                "cashout_configuration": cashout,
                "balance_text": snapshot.get("balance_text") if isinstance(snapshot, dict) else None,
                "ui_ready": bool(snapshot.get("ui_ready")) if isinstance(snapshot, dict) else False,
                "browser_status": "CONNECTED" if readiness.get("connected") else "UNAVAILABLE",
                "observed_at": (snapshot or {}).get("balance_observed_at") or datetime.now(timezone.utc).isoformat(),
                "source": "existing_cdp_page_read_only",
                "error": None if verified else ("authenticated_account_not_verified"
                    if authentication_state != "AUTHENTICATED" else "UI balance unavailable or invalid"),
            }
            repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
            if repository is not None and hasattr(repository, "record_platform_balance_observation"):
                saved = await asyncio.to_thread(repository.record_platform_balance_observation, observation)
                if saved:
                    observation["observation_id"] = saved["observation_id"]
            health = getattr(request.app.state, "system_health", None)
            if health is not None:
                health.record_browser_observation(observation)
                observation = health.browser_observation()
            payload["platform_balance"] = observation
        else:
            # Expose the latest monitor observation without opening another
            # browser connection. The monitor reads the existing authenticated
            # CDP context every few seconds and SystemHealth adds freshness.
            health = getattr(request.app.state, "system_health", None)
            if health is not None:
                payload["platform_balance"] = health.browser_observation()
        return payload
    except Exception as exc:  # structured failure, not a 500 crash
        log.exception("browser readiness check failed")
        browser = dict(readiness) if isinstance(readiness, dict) else {
            "connected": False, "stage": "error", "page_found": None,
            "game_found": None, "aviator_next_reachable": None,
        }
        browser["error"] = str(exc)
        return JSONResponse(status_code=200, content={
            "ok": False,
            "browser": browser,
        })
    finally:
        await client.close()


@router.post("/session")
async def session_control(request: Request, body: SessionStartRequest):
    manager = _betting(request)
    try:
        if body.action == "start":
            if getattr(request.app.state, "betting_mode", None):
                return JSONResponse(status_code=409, content={
                    "ok": False, "error": "backend_mode_switch_required",
                    "message": "Use the authoritative SHADOW_REALISTIC/LIVE_REAL mode switch",
                })
            if body.mode == "REAL":
                return JSONResponse(status_code=409, content={
                    "ok": False, "error": "live_activation_required",
                    "message": "REAL sessions require /api/live/start and "
                               "platform verification",
                })
            risk = _risk(request)
            if risk and risk.emergency_latched:
                return JSONResponse(status_code=409, content={
                    "ok": False, "error": "emergency_stop_latched",
                    "message": "Reset emergency stop before starting a new session",
                })
            status = await manager.start_session(body)
            if risk:
                await risk.on_session_start(body.profile)
        else:
            status = await manager.stop_session(emergency=False)
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/emergency-stop")
async def emergency_stop(request: Request):
    manager = _betting(request)
    risk = _risk(request)
    if risk:
        await risk.emergency_stop()
    modes = getattr(request.app.state, "betting_mode", None)
    if modes:
        return {"ok": True, "status": await modes.force_safe("emergency_stop")}
    try:
        status = await manager.stop_session(emergency=True)
    except BettingError as exc:
        return _conflict(exc)
    return {"ok": True, "status": status}


@router.post("/decisions")
async def submit_decision(request: Request, body: DecisionIntent):
    engine = getattr(request.app.state, "rules_engine", None)
    evaluation = (engine.last_evaluation or engine.evaluate()
                  if engine is not None else None)
    rule_status = engine.status() if engine is not None else {}
    rule_execution_path_ready = bool(
        engine is not None
        and callable(getattr(engine, "execution_authorized", None))
        and engine.execution_authorized() is True
    )
    configured = int(rule_status.get("rules_enabled") or 0) > 0
    blocker = ("decision_interface_not_connected" if configured
               else "rules_engine_not_configured")
    reasons = []
    if not configured:
        reasons.append("No configured, enabled Rules Engine rule can authorize execution")
    if not rule_execution_path_ready:
        reasons.append("Rule selections are not connected to a target-bound decision intent")
    reasons.extend([
        "No final Risk Engine approval is attached to this intent",
        "The next stream/index target is not bound to a verified provider round ID",
        "Platform acceptance and settlement receipts are not verified",
    ])
    return JSONResponse(status_code=409, content={
        "ok": False,
        "error": blocker,
        "blocker": blocker,
        "message": "Execution remains blocked until the complete rule, target, risk, and platform contract is ready.",
        "decision": "NO_ACTION",
        "automatic_execution": "OFF",
        "rule_evaluation": evaluation,
        "rule_execution_path_ready": rule_execution_path_ready,
        "reasons": list(dict.fromkeys(reasons)),
    })


@router.get("/ledger")
async def ledger(request: Request, limit: int = 50):
    manager = _betting(request)
    return {"ok": True, "entries": manager.ledger(limit)}


@router.get("/executions")
async def executions(request: Request, limit: int = 50):
    repo = getattr(getattr(request.app.state, "wp", None), "repository", None)
    return {"ok": True, "executions": repo.list_executions(limit) if repo else []}
