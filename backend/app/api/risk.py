"""Risk management API. All profile input is validated server-side."""
from fastapi import APIRouter, Request

from app.risk.schemas import ProfileSelection

router = APIRouter(prefix="/api/risk", tags=["risk"])


def _managers(request: Request):
    return request.app.state.risk, request.app.state.betting


@router.get("/status")
async def status(request: Request):
    risk, betting = _managers(request)
    return {"ok": True, "status": risk.status(betting.status())}


@router.get("/profiles")
async def profiles(request: Request):
    risk, _ = _managers(request)
    return {"ok": True, "profiles": risk.profiles()}


@router.post("/profile")
async def profile(request: Request, body: ProfileSelection):
    risk, betting = _managers(request)
    if betting.status().get("state") not in {"IDLE", "STOPPED", "ERROR"}:
        return {"ok": False, "message": "Stop the active session before changing profile"}
    selected = await risk.select_profile(body.profile)
    return {"ok": True, "profile": selected}


@router.get("/session")
async def session(request: Request):
    risk, betting = _managers(request)
    return {"ok": True, "session": risk.status(betting.status())}


@router.get("/audit")
async def audit(request: Request, limit: int = 100):
    risk, _ = _managers(request)
    return {"ok": True, "entries": risk.audit_log[-max(1, min(limit, 500)):]}


@router.post("/reset-emergency")
async def reset_emergency(request: Request):
    risk, _ = _managers(request)
    await risk.reset_emergency()
    return {"ok": True}
