from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def history(request: Request, limit: int = 250):
    limit = min(max(limit, 1), 5000)
    repository = request.app.state.wp.repository
    if repository.database_url:
        # The collector can continue writing after AppState's startup
        # snapshot. Read PostgreSQL directly so this page stays current.
        rows = repository.load_recent_rounds(limit)
        count = repository.round_history_status()["count"]
    else:
        all_rows = request.app.state.history_collector.rows()
        count = len(all_rows)
        rows = all_rows[-limit:]
    return {
        "count": count,
        "returned": len(rows),
        "rounds": rows,
    }


@router.get("/status")
def collector_status(request: Request):
    return request.app.state.history_collector.status()


@router.get("/latest")
def latest(request: Request):
    rows = request.app.state.history_collector.rows()
    return {"latest": rows[-1] if rows else None, "previous": rows[-2] if len(rows) > 1 else None}


@router.get("/recent")
def recent(request: Request, limit: int = 25):
    rows = request.app.state.history_collector.rows()
    limit = min(max(limit, 1), 5000)
    return {"count": len(rows), "returned": min(limit, len(rows)), "rounds": rows[-limit:]}


@router.get("/stats")
def stats(request: Request):
    return request.app.state.history_collector.stats()


@router.post("/start")
async def start(request: Request):
    return await request.app.state.history_collector.start()


@router.post("/stop")
async def stop(request: Request):
    if request.app.state.history_collector.external:
        raise HTTPException(status_code=409, detail="collector_is_supervisor_managed")
    return await request.app.state.history_collector.stop()
