from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/operations", tags=["operations"])

def ops(request: Request): return request.app.state.operations

@router.get("/status")
async def status(request: Request): return await ops(request).evaluate()

@router.get("/incidents")
async def incidents(request: Request): return {"ok": True, "incidents": ops(request).incidents}

@router.get("/report/{period}")
async def report(request: Request, period: str): return {"ok": True, "report": ops(request).report(period)}

@router.post("/cleanup")
async def cleanup(request: Request): return {"ok": True, "deleted": ops(request).cleanup()}

@router.post("/backup")
async def backup(request: Request, body: dict):
    try: return {"ok": True, "backup": ops(request).backup(body.get("destination", "backups/winner-predict.sqlite"))}
    except Exception as exc: return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

@router.post("/restore")
async def restore(request: Request, body: dict):
    try:
        return {"ok": True, "restore": ops(request).restore(body.get("source", ""), confirmation=body.get("confirmation", ""))}
    except Exception as exc: return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
