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
    try:
        risk = _risk(request)
        if risk and risk.emergency_latched:
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "emergency_stop_latched",
                "message": "Reset emergency stop before starting a new session",
            })
        status = await _betting(request).start_session(body.as_session_request())
        if risk:
            await risk.on_session_start(body.profile)
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/stop")
async def stop_automatic(request: Request):
    try:
        status = await _betting(request).stop_session(emergency=False)
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


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
