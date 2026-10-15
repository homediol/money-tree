from __future__ import annotations

import hmac
import hashlib

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.services.betting_mode import LIVE_REAL, SHADOW_REALISTIC


router = APIRouter(prefix="/api/betting-mode", tags=["betting mode"])


async def _require_live_credentials(request: Request) -> str:
    settings = get_settings()
    expected_key = settings.api_key
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
    # Persist only a non-reversible credential fingerprint as the activation
    # identity; never retain or log the bearer token itself.
    return f"api-key:{hashlib.sha256(expected_key.encode('utf-8')).hexdigest()[:16]}"


@router.get("/status")
async def status(request: Request):
    return {"ok": True, "mode": request.app.state.betting_mode.status()}


@router.get("/live-configuration")
async def live_configuration(request: Request):
    """Return the latest saved inert LIVE configuration without authorizing it."""
    return {"ok": True,
            "configuration": request.app.state.betting_mode.live_configuration()}


@router.post("/live-configuration")
async def save_live_configuration(request: Request, body: dict):
    """Save an inert operator draft; activation still requires a fresh review."""
    try:
        configuration = request.app.state.betting_mode.save_live_configuration_draft(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "configuration": configuration,
            "live_authorized": False, "activation_required": True}


@router.get("/shadow-review")
async def shadow_review(request: Request):
    """Retired profile-based financial shadow review."""
    del request
    raise HTTPException(status_code=410, detail={
        "error": "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED",
        "message": "Platform balance is no longer used to configure a paper executor.",
    })


@router.get("/live-review")
async def live_review(request: Request, execution_panel: int | None = None):
    # This endpoint is read-only. It deliberately exposes the complete
    # activation review and blockers so the dashboard can explain why LIVE is
    # unavailable. The mutating mode switch below still requires the backend
    # credential and repeats every value and safety check.
    try:
        return await request.app.state.betting_mode.live_review(execution_panel=execution_panel)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("")
async def switch_mode(request: Request, body: dict):
    requested = str(body.get("mode") or "").upper()
    modes = request.app.state.betting_mode
    if requested == SHADOW_REALISTIC:
        raise HTTPException(status_code=410, detail={
            "error": "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED",
            "message": "Financial shadow execution is retired. Use the observe-only Rules Engine.",
        })
    elif requested == LIVE_REAL:
        operator_identity = await _require_live_credentials(request)
        result = await modes.switch_live(body, operator_identity=operator_identity)
    else:
        raise HTTPException(status_code=422, detail={
            "reason": "invalid_mode", "allowed": [SHADOW_REALISTIC, LIVE_REAL],
        })
    if not result.get("ok"):
        status_code = 409 if result.get("error") not in {"mode_switch_failed"} else 503
        raise HTTPException(status_code=status_code, detail=result)
    return result
