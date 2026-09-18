from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def history(request: Request, limit: int = 250):
    wp = request.app.state.wp
    limit = min(max(limit, 1), 5000)
    rows = wp.rounds.tail(limit)
    return {
        "count": int(len(wp.rounds)),
        "returned": int(len(rows)),
        "rounds": [
            {
                "round_id": str(r.round_index),
                "round_index": int(r.round_index),
                "multiplier": float(r.multiplier),
                "timestamp": r.timestamp,
                "target": int(float(r.multiplier) >= wp.settings.target_multiplier),
            }
            for r in rows.itertuples()
        ],
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
    return await request.app.state.history_collector.stop()
