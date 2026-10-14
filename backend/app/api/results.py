"""Read-only Part 10 execution, reconciliation, ledger and metrics APIs."""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/results", tags=["results"])


def repo(request: Request):
    return request.app.state.wp.repository


def verified_session_metrics(metrics: list[dict], executions: list[dict]) -> list[dict]:
    """Only expose real session totals backed by a reconciled platform result."""
    verified_sessions = {
        str(execution.get("session_id"))
        for execution in executions
        if execution.get("session_id") is not None
        and str(execution.get("status", "")).upper() == "RECONCILED"
        and str(execution.get("balance_status", "")).upper() == "VERIFIED"
        and execution.get("profit_loss") is not None
        and execution.get("simulated") is not True
    }
    return [
        item for item in metrics
        if str(item.get("session_id", "")) in verified_sessions
        and str(item.get("mode", "")).upper() == "REAL"
        and item.get("simulated") is not True
    ]


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
    repository = repo(request)
    metrics = repository.list_session_metrics(min(max(limit, 1), 500))
    executions = repository.list_executions(500)
    return {"ok": True, "sessions": verified_session_metrics(metrics, executions)}
