"""Read-only execution and settlement-evidence APIs."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/results", tags=["results"])


def repo(request: Request):
    return request.app.state.wp.repository


@router.get("/executions")
async def executions(request: Request, limit: int = 100):
    return {"ok": True, "executions": repo(request).list_executions(min(max(limit, 1), 500))}


@router.get("/executions/{execution_id}")
async def execution_detail(request: Request, execution_id: str):
    execution = repo(request).execution_by_id(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="execution not found")
    return {"ok": True, "execution": execution,
            "timeline": repo(request).execution_events(execution_id)}


@router.get("/ledger")
async def ledger(request: Request, limit: int = 100):
    return {"ok": True, "entries": repo(request).list_ledger(min(max(limit, 1), 500))}


@router.get("/reconciliations")
async def reconciliations(request: Request, limit: int = 100):
    return {"ok": True, "reconciliations": repo(request).list_reconciliations(min(max(limit, 1), 500))}


@router.get("/session-metrics")
async def session_metrics(request: Request, limit: int = 50):
    del request, limit
    return JSONResponse(status_code=410, content={"ok": False,
        "error": "OPTIONAL_BANKROLL_ANALYTICS_REMOVED",
        "message": "Use the read-only execution and settlement evidence endpoints."})
