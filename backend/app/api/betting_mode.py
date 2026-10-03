from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.services.betting_mode import LIVE_REAL, SHADOW_REALISTIC


router = APIRouter(prefix="/api/betting-mode", tags=["betting mode"])


async def _require_live_credentials(request: Request) -> None:
    expected_key = get_settings().api_key
    if not expected_key:
        modes = getattr(request.app.state, "betting_mode", None)
        if modes is not None and modes.mode == LIVE_REAL:
            await modes.force_safe("security_incident_live_api_key_missing")
        raise HTTPException(status_code=503, detail={
            "reason": "backend_api_key_required_for_live_authorization",
            "message": "Configure API_KEY before requesting LIVE_REAL.",
        })
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {expected_key}"):
        modes = getattr(request.app.state, "betting_mode", None)
        if modes is not None and modes.mode == LIVE_REAL:
            await modes.force_safe("security_incident_invalid_live_authorization")
        raise HTTPException(status_code=401, detail="Valid backend credentials are required")


@router.get("/status")
async def status(request: Request):
    return {"ok": True, "mode": request.app.state.betting_mode.status()}


@router.get("/shadow-review")
async def shadow_review(request: Request):
    """Read the existing platform balance for the SHADOW session confirmation."""
    observation = await request.app.state.betting_mode._read_platform_balance()
    return {"ok": bool(observation.get("verified")), "platform_balance": observation,
            "configuration_live_enabled": False,
            "reason": None if observation.get("verified") else observation.get("error")}


@router.get("/live-review")
async def live_review(request: Request, profile: str = "PROFILE_A", goal_balance: float | None = None):
    # This endpoint is read-only. It deliberately exposes the complete
    # activation review and blockers so the dashboard can explain why LIVE is
    # unavailable. The mutating mode switch below still requires the backend
    # credential and repeats every value and safety check.
    try:
        return await request.app.state.betting_mode.live_review(
            profile=profile, goal_balance=goal_balance)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("")
async def switch_mode(request: Request, body: dict):
    requested = str(body.get("mode") or "").upper()
    modes = request.app.state.betting_mode
    if requested == SHADOW_REALISTIC:
        result = await modes.switch_shadow(
            profile=str(body.get("profile") or "PROFILE_A").upper(),
            goal_balance=body.get("goal_balance"),
            starting_balance=body.get("starting_balance"),
            configuration=body.get("configuration"),
        )
    elif requested == LIVE_REAL:
        await _require_live_credentials(request)
        result = await modes.switch_live(body)
    else:
        raise HTTPException(status_code=422, detail={
            "reason": "invalid_mode", "allowed": [SHADOW_REALISTIC, LIVE_REAL],
        })
    if not result.get("ok"):
        status_code = 409 if result.get("error") not in {"mode_switch_failed"} else 503
        raise HTTPException(status_code=status_code, detail=result)
    return result
