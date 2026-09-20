from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/recovery", tags=["disaster recovery"])

def recovery(request: Request): return request.app.state.disaster_recovery

@router.get("/status")
async def status(request: Request): return {"ok": True, **recovery(request).status()}

@router.post("/health-check")
async def health_check(request: Request): return {"ok": True, "integrity": recovery(request).integrity_check()}

@router.post("/integrity")
async def integrity(request: Request): return {"ok": True, "integrity": recovery(request).integrity_check()}

@router.post("/backup")
async def backup(request: Request, body: dict):
    try: return {"ok": True, "backup": recovery(request).backup_verified(body.get("destination", "backups/winner-predict.sqlite"))}
    except Exception as exc: return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

@router.post("/recover")
async def recover(request: Request, body: dict | None = None): return {"ok": True, "recovery": await recovery(request).recover((body or {}).get("reason", "manual"))}

@router.post("/restore")
async def restore(request: Request, body: dict):
    try:
        result = recovery(request).operations.restore(body.get("source", ""), confirmation=body.get("confirmation", ""))
        return {"ok": True, "restore": result}
    except Exception as exc: return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
