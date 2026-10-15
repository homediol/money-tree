"""Execution-integrity endpoint: emergency latch only.

Bankroll strategy and risk-profile APIs are retired from the main application.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/risk", tags=["execution integrity"])


def _risk(request: Request):
    return getattr(request.app.state, "risk", None)


def _integrity_state(risk) -> dict:
    if risk is None:
        return {"emergency_stop": None, "execution_state": "BLOCKED",
                "reason": "EMERGENCY_STOP_STATE_UNAVAILABLE"}
    return {"emergency_stop": bool(risk.emergency_latched),
            "execution_state": "BLOCKED" if risk.emergency_latched else "NOT_AUTHORIZED",
            "reason": "EMERGENCY_STOP_LATCHED" if risk.emergency_latched else
                      "REAL_MONEY_EXECUTION_DISABLED"}


@router.get("/status")
async def status(request: Request):
    return {"ok": True, "execution_integrity": _integrity_state(_risk(request))}


@router.get("/session")
async def session(request: Request):
    return {"ok": True, "execution_integrity": _integrity_state(_risk(request))}


@router.get("/profiles")
async def profiles_removed():
    return JSONResponse(status_code=410, content={"ok": False,
        "error": "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED"})


@router.post("/profile")
async def profile_removed():
    return JSONResponse(status_code=410, content={"ok": False,
        "error": "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED"})


@router.get("/audit")
async def audit(request: Request, limit: int = 100):
    """Read historical risk audit entries without exposing a profile selector."""
    risk = _risk(request)
    if risk is None:
        return {"ok": True, "entries": []}
    return {"ok": True, "entries": risk.audit_log[-max(1, min(limit, 500)): ]}


@router.post("/reset-emergency")
async def reset_emergency(request: Request):
    risk = _risk(request)
    if risk is None:
        return JSONResponse(status_code=503, content={"ok": False,
            "error": "EMERGENCY_STOP_STATE_UNAVAILABLE"})
    await risk.reset_emergency()
    return {"ok": True, "execution_integrity": _integrity_state(risk)}
