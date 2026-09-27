"""REST surface for the betting module.

Lifecycle events are additionally pushed over /ws/live as flat dicts with
``type`` starting ``betting:`` (state / ledger / notice).
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.betting.browser_client import AviatorBrowserClient
from app.betting.profiles import get_profile, profile_keys
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
    """Start REAL automatic mode; no prediction is created here."""
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
    try:
        readiness = await client.readiness()
        payload: dict = {"ok": True, "browser": readiness}
        if include_snapshot:
            snap = await client.snapshot()
            payload["snapshot"] = snap.as_dict() if hasattr(snap, "as_dict") else snap
        return payload
    except Exception as exc:  # structured failure, not a 500 crash
        log.exception("browser readiness check failed")
        return JSONResponse(status_code=200, content={
            "ok": False,
            "browser": {"connected": False, "stage": "error",
                        "error": str(exc)},
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
    manager = _betting(request)
    decision_engine = getattr(request.app.state, "decision_engine", None)
    try:
        authorized = decision_engine.current() if decision_engine else None
        if (not authorized or authorized.get("status") != "READY_FOR_EXECUTION"
                or authorized.get("decision_id") != body.decision_id
                or authorized.get("target_round_id") != body.round_id
                or authorized.get("profile") != body.profile):
            modes = getattr(request.app.state, "betting_mode", None)
            if modes and modes.mode == "LIVE_REAL":
                await modes.force_safe("pre_bet_failure:decision_not_authorized_or_stale")
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "decision_not_authorized",
                "message": "Only the matching, unexpired Part 8 risk-approved decision may reach the executor",
            })
        approved = authorized.get("risk_evaluation") or {}
        if not approved.get("approved") or not approved.get("approved_bet"):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "risk_approval_missing",
                "message": "Decision has no valid risk approval",
            })
        betting_status = manager.status()
        live_controller = getattr(request.app.state, "live", None)
        if betting_status.get("mode") == "REAL" and live_controller is None:
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "live_activation_unavailable",
                "message": "NO BET",
            })
        if betting_status.get("mode") == "REAL" and live_controller:
            live_gate = live_controller.can_execute_live_bet(authorized, betting_status)
            if not live_gate.get("allowed"):
                modes = getattr(request.app.state, "betting_mode", None)
                if modes:
                    await modes.force_safe("pre_bet_gate_blocked:" + ";".join(
                        live_gate.get("reasons", []) or ["live_safety_gate_blocked"]))
                return JSONResponse(status_code=409, content={"ok": False, "error": "live_safety_gate_blocked", "message": "NO BET", "reasons": live_gate.get("reasons", [])})
        gate = _health_gate(request, betting_status.get("mode", "REAL"))
        if (betting_status.get("automatic_enabled") and
                betting_status.get("mode") != "SIMULATION" and
                not gate.get("allowed")):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "safety_gate_blocked", "message": "NO BET",
                "reasons": gate.get("reasons", []),
            })
        risk_manager = _risk(request)
        # A stopped/idle manager returns its domain-specific conflict below.
        # The identity check isolates test/embedded apps from stale app.state.
        final_blocks = (risk_manager.validate_execution(authorized, betting_status)
                        if risk_manager and risk_manager.betting_manager is manager
                        and betting_status.get("automatic_enabled") else [])
        wp = getattr(request.app.state, "wp", None)
        if (wp is not None and manager.repository is not None
                and not wp.dataset_service.clean_rounds.empty):
            latest = str(wp.dataset_service.clean_rounds.iloc[-1]["round_id"])
            if latest != str(authorized.get("source_round_id")):
                final_blocks.append("target_round_missed")
        if final_blocks:
            modes = getattr(request.app.state, "betting_mode", None)
            if modes and modes.mode == "LIVE_REAL":
                await modes.force_safe("pre_bet_risk_blocked:" + ";".join(final_blocks))
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "final_validation_failed",
                "message": "NO BET", "reasons": sorted(set(final_blocks)),
            })
        profile = get_profile(authorized["profile"])
        body = body.model_copy(update={
            "execute": True, "amount_bif": approved["approved_bet"],
            "bet_amount": approved["approved_bet"], "cashout": profile.base_target,
            "target_multiplier": profile.base_target,
            "expires_at": datetime.fromisoformat(authorized["expires_at"]),
        })
        entry = await manager.submit_decision(body)
    except BettingError as exc:
        return _conflict(exc)
    return {"ok": True,
            "risk": authorized.get("risk_evaluation"),
            "entry": entry}


@router.get("/ledger")
async def ledger(request: Request, limit: int = 50):
    manager = _betting(request)
    return {"ok": True, "entries": manager.ledger(limit)}


@router.get("/executions")
async def executions(request: Request, limit: int = 50):
    repo = getattr(getattr(request.app.state, "wp", None), "repository", None)
    return {"ok": True, "executions": repo.list_executions(limit) if repo else []}
