from __future__ import annotations

import os
import resource
import time
from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


def _health(request: Request):
    return getattr(request.app.state, "system_health", None)


@router.get("/api/health")
async def health(request: Request):
    monitor = _health(request)
    if monitor is None:
        return {"ok": False, "state": "STARTING", "reason": "health monitor unavailable"}
    snapshot = monitor.refresh()
    return {"ok": snapshot["state"] == "HEALTHY", **snapshot}


@router.get("/api/health/detailed")
async def detailed_health(request: Request):
    monitor = _health(request)
    if monitor is None:
        return {"ok": False, "state": "STARTING", "reason": "health monitor unavailable", "components": {}}
    return {"ok": True, **monitor.refresh()}


@router.get("/api/metrics")
async def metrics(request: Request):
    monitor = _health(request)
    health = monitor.snapshot() if monitor else {"state": "STARTING"}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    manager = getattr(request.app.state, "betting", None)
    status = manager.status() if manager else {}
    return {"ok": True, "timestamp": time.time(), "process": {
        "pid": os.getpid(), "max_rss_kb": usage.ru_maxrss,
        "user_cpu_s": usage.ru_utime, "system_cpu_s": usage.ru_stime,
    }, "health": health, "betting": {
        "placed": status.get("placed_count", 0), "resolved": status.get("resolved_count", 0),
        "unknown": status.get("unknown", 0), "state": status.get("state"),
    }}
