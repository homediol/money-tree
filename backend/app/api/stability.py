from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/backend", tags=["backend-stability"])


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    """Report runtime features that must be active before the dashboard is safe to operate."""
    orchestrator = getattr(request.app.state, "orchestrator", None)
    tasks = getattr(orchestrator, "tasks", ()) if orchestrator else ()
    balance_task = next((task for task in tasks if task.get_name() == "platform-balance"), None)
    balance_monitor_running = bool(balance_task and not balance_task.done())
    history = getattr(request.app.state, "history_collector", None)
    return {
        "ok": True,
        "service": "winner-predict-backend",
        "capabilities": {
            "read_only_platform_balance_monitor": balance_monitor_running,
            "history_collector_status": history is not None,
        },
    }


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
    reliability_path = Path(__file__).resolve().parents[3] / "data" / "bot" / "browser-reliability.json"
    try:
        browser_reliability = json.loads(reliability_path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(browser_reliability["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            browser_reliability["snapshot_stale"] = True
            browser_reliability["state"] = "DEGRADED"
            browser_reliability["reason"] = "Supervisor heartbeat is stale"
        payload["browser_reliability"] = browser_reliability
    except (OSError, ValueError, KeyError, TypeError):
        payload["browser_reliability"] = {"state": "UNKNOWN", "reason": "Supervisor snapshot unavailable"}
    return {"ok": True, "stability": payload}
