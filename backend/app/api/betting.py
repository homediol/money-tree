"""REST surface for the betting module.

Lifecycle events are additionally pushed over /ws/live as flat dicts with
``type`` starting ``betting:`` (state / ledger / notice).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.betting.browser_client import AviatorBrowserClient
from app.betting.profiles import profile_keys
from app.betting.schemas import DecisionIntent, SessionStartRequest, profile_choices
from app.betting.session import BettingError

log = logging.getLogger("betting.api")
router = APIRouter(prefix="/api/betting", tags=["betting"])


def _betting(request: Request):
    manager = getattr(request.app.state, "betting", None)
    if manager is None:
        raise BettingError("betting manager not initialised")
    return manager


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
            status = await manager.start_session(body)
        else:
            status = await manager.stop_session(emergency=False)
        return {"ok": True, "status": status}
    except BettingError as exc:
        return _conflict(exc)


@router.post("/emergency-stop")
async def emergency_stop(request: Request):
    manager = _betting(request)
    try:
        status = await manager.stop_session(emergency=True)
    except BettingError as exc:
        return _conflict(exc)
    return {"ok": True, "status": status}


@router.post("/decisions")
async def submit_decision(request: Request, body: DecisionIntent):
    manager = _betting(request)
    try:
        entry = await manager.submit_decision(body)
    except BettingError as exc:
        return _conflict(exc)
    return {"ok": True, "entry": entry}


@router.get("/ledger")
async def ledger(request: Request, limit: int = 50):
    manager = _betting(request)
    return {"ok": True, "entries": manager.ledger(limit)}

