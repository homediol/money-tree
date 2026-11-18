from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/adie", tags=["dominance intelligence"])


def _engine(request: Request):
    engine = getattr(request.app.state, "adie_engine", None)
    if engine is None or not engine.ready:
        raise HTTPException(status_code=503, detail={"error": "adie_unavailable"})
    return engine


@router.get("/status")
async def status(request: Request):
    result = _engine(request).status()
    collector = getattr(request.app.state, "history_collector", None)
    if collector is not None:
        try:
            source = collector.status() or {}
            result["collector"] = {key: source.get(key) for key in (
                "status", "collector_process_status", "browser_connected", "data_source",
                "last_round_at", "last_error", "rounds_per_minute", "source_status")}
        except Exception:
            result["collector"] = {"status": "UNKNOWN", "reason": "collector_status_unavailable"}
    else:
        result["collector"] = {"status": "UNKNOWN", "reason": "collector_unavailable"}
    return result


@router.get("/forecasts")
async def forecasts(request: Request, limit: int = Query(100, ge=1, le=500),
                    status: str | None = None):
    return {"items": _engine(request).forecasts(limit=limit, status=status)}


@router.get("/decisions")
async def decisions(request: Request, limit: int = Query(100, ge=1, le=500)):
    return {"items": _engine(request).decisions(limit=limit)}


@router.get("/performance")
async def performance(request: Request):
    return _engine(request).performance()
