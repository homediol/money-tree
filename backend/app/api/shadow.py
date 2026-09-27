from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


def shadow(request: Request): return request.app.state.shadow


@router.post("/start")
async def start(request: Request):
    body = await request.json()
    result = await request.app.state.betting_mode.switch_shadow(
        profile=body.get("profile", "PROFILE_A"), goal_balance=body.get("goal_balance"))
    if not result.get("ok"):
        return JSONResponse(status_code=409, content=result)
    return result


@router.post("/stop")
async def stop(request: Request): return {"ok": True, "status": await shadow(request).stop()}
@router.post("/pause")
async def pause(request: Request): return {"ok": True, "status": await shadow(request).pause()}
@router.post("/resume")
async def resume(request: Request):
    modes = request.app.state.betting_mode
    if modes.mode != "SHADOW_REALISTIC" or not modes.shadow_ready:
        return JSONResponse(status_code=409, content={"ok": False,
            "error": "backend_mode_switch_required",
            "message": "Shadow resume requires the authoritative mode switch"})
    try: return {"ok": True, "status": await shadow(request).resume(request.app.state.system_health)}
    except ValueError as exc: return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
@router.get("/status")
async def status(request: Request): return {"ok": True, "status": shadow(request).status()}
@router.get("/current")
async def current(request: Request): return {"ok": True, "trade": next(iter(shadow(request)._pending.values()), None)}
@router.get("/metrics")
async def metrics(request: Request): return {"ok": True, "metrics": shadow(request).status()}
@router.get("/trades")
async def trades(request: Request, limit: int = 100): return {"ok": True, "trades": request.app.state.wp.repository.list_shadow_trades(limit=min(max(limit, 1), 500))}
@router.get("/predictions")
async def predictions(request: Request, limit: int = 50): return {"ok": True, "predictions": request.app.state.wp.repository.list_ml_predictions(limit)}
@router.get("/sessions")
async def sessions(request: Request, limit: int = 50): return {"ok": True, "sessions": request.app.state.wp.repository.list_shadow_sessions(limit)}
@router.get("/sessions/{session_id}")
async def session(request: Request, session_id: str): return {"ok": True, "session": request.app.state.wp.repository.shadow_session(session_id)}
@router.get("/equity")
async def equity(request: Request, limit: int = 200): return {"ok": True, "equity": request.app.state.wp.repository.list_shadow_ledger(limit=limit)}
@router.get("/drift")
async def drift(request: Request): return {"ok": True, "drift": shadow(request).drift()}
