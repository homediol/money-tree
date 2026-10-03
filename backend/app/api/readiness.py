import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/readiness", tags=["readiness"])


async def schedule_evaluation(request: Request) -> dict:
    """The existing ML POST routes may only queue the gated evaluation."""
    live = getattr(request.app.state, "live", None)
    if live and live.live_active:
        raise HTTPException(status_code=409, detail="Pause live betting before changing the model")
    readiness = getattr(request.app.state, "readiness", None)
    if readiness is None:
        raise HTTPException(status_code=503, detail="readiness gate unavailable")
    snapshot = await readiness.refresh()
    training = snapshot["automatic_training"]
    if training["next_evaluation"] != "TRAINING":
        raise HTTPException(status_code=409, detail={
            "reason": "evaluation_not_eligible", "blockers": training["blockers"],
        })
    return {"status": "EVALUATION_RUNNING", "next_evaluation": "TRAINING",
            "new_rounds": training["new_rounds_since_training"],
            "minimum_new_rounds": training["minimum_new_rounds"]}


@router.get("")
async def status(request: Request):
    readiness = request.app.state.readiness
    snapshot = readiness.cached_status()
    if snapshot is None:
        state = "STALE" if readiness._last_snapshot is not None else "STARTING"
        return JSONResponse(status_code=503, content={"ok": False, "status": state,
                                                     "reason": "readiness_snapshot_expired_or_history_changed"})
    return {"ok": True, "readiness": snapshot}


@router.get("/gate")
async def gate(request: Request):
    """Read-only view of the centralized REAL execution gate."""
    readiness = request.app.state.readiness
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(readiness.execution_gate), timeout=4.0,
        )
        return {"ok": True, "snapshot_source": "CURRENT", **result}
    except asyncio.TimeoutError:
        cached = readiness.cached_status()
        if cached is None:
            raise HTTPException(status_code=503, detail="readiness computation exceeded 4 seconds and no cached snapshot exists")
        return {"ok": True, "snapshot_source": "CACHED_TIMEOUT", "cached": True,
                "allowed": False, "state": "UNKNOWN", "reasons": ["readiness_computation_timeout"],
                "checked_at": cached.get("updated_at"), "readiness": cached}
