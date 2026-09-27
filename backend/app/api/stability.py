from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/backend", tags=["backend-stability"])


@router.get("/stability")
async def stability(request: Request) -> dict:
    """Return the persisted lifecycle and isolated worker health snapshot."""
    lifecycle = getattr(request.app.state, "lifecycle", None)
    payload = lifecycle.status() if lifecycle else {
        "status": "STARTING", "pid": None, "ppid": None,
    }
    history = getattr(request.app.state, "history_collector", None)
    payload["collector"] = history.health_status() if history else {"status": "STARTING"}
    readiness = getattr(request.app.state, "readiness", None)
    cached = readiness.cached_status() if readiness else None
    training = cached.get("automatic_training", {}) if isinstance(cached, dict) else {}
    payload["ml_worker"] = {
        "status": training.get("status", "IDLE"),
        "last_result": training.get("last_result"),
        "reason": training.get("reason"),
    }
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None:
        payload["database"] = {"status": "STARTING"}
    else:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            payload["database"] = {"status": "OK"}
        except Exception as exc:
            payload["database"] = {"status": "ERROR", "reason": str(exc)}
    system_health = getattr(request.app.state, "system_health", None)
    if system_health is None:
        payload["browser"] = {"status": "STARTING"}
    else:
        try:
            browser = system_health.snapshot()["components"]["browser"]
            payload["browser"] = {"status": browser["state"], "reason": browser["last_error"]}
        except Exception as exc:
            payload["browser"] = {"status": "ERROR", "reason": str(exc)}
    return {"ok": True, "stability": payload}
