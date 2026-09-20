from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/live", tags=["live activation"])


def live(request: Request):
    return request.app.state.live


@router.post("/readiness")
async def readiness(request: Request):
    return await live(request).production_readiness_check()


@router.post("/start")
async def start(request: Request, body: dict):
    return await live(request).start(body)


@router.post("/pause")
async def pause(request: Request, body: dict | None = None):
    return {"ok": True, "status": await live(request).pause((body or {}).get("reason", "manual_pause"))}


@router.post("/resume")
async def resume(request: Request, body: dict):
    return await live(request).resume(body.get("confirmation", ""))


@router.post("/stop")
async def stop(request: Request):
    return {"ok": True, "status": await live(request).stop(False)}


@router.post("/emergency-stop")
async def emergency_stop(request: Request):
    return {"ok": True, "status": await live(request).stop(True)}


@router.get("/status")
async def status(request: Request):
    return {"ok": True, **live(request).status()}


@router.get("/current")
async def current(request: Request):
    return {"ok": True, "current": live(request).status().get("session")}


@router.get("/session")
async def session(request: Request):
    return {"ok": True, "session": live(request).status().get("session")}


@router.get("/metrics")
async def metrics(request: Request):
    s = live(request).status().get("session") or {}
    return {"ok": True, "mode": live(request).mode, "session_id": s.get("session_id"),
            "executions": request.app.state.wp.repository.list_executions(100),
            "ledger": request.app.state.wp.repository.list_ledger(100)}


@router.get("/executions")
async def executions(request: Request, limit: int = 100):
    return {"ok": True, "executions": request.app.state.wp.repository.list_executions(limit)}


@router.get("/reconciliation")
async def reconciliation(request: Request, limit: int = 100):
    return {"ok": True, "records": request.app.state.wp.repository.list_reconciliations(limit)}


@router.get("/audit")
async def audit(request: Request, limit: int = 100):
    return {"ok": True, "events": request.app.state.wp.repository.list_audit_log(limit)}
