from __future__ import annotations

import asyncio
import os
import resource
import time
import psutil
from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


def _health(request: Request):
    return getattr(request.app.state, "system_health", None)


@router.get("/api/health")
async def health(request: Request):
    monitor = _health(request)
    if monitor is None:
        return {"ok": False, "state": "STARTING", "reason": "health monitor unavailable"}
    snapshot = await asyncio.to_thread(monitor.refresh)
    return {"ok": snapshot["state"] == "HEALTHY", **snapshot}


@router.get("/api/health/detailed")
async def detailed_health(request: Request):
    monitor = _health(request)
    if monitor is None:
        return {"ok": False, "state": "STARTING", "reason": "health monitor unavailable", "components": {}}
    return {"ok": True, **await asyncio.to_thread(monitor.refresh)}


@router.get("/api/metrics")
async def metrics(request: Request):
    monitor = _health(request)
    health = monitor.snapshot() if monitor else {"state": "STARTING"}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    process = psutil.Process(os.getpid())
    try:
        process_usage = {
            "cpu_percent": process.cpu_percent(interval=None),
            "rss_bytes": process.memory_info().rss,
            "thread_count": process.num_threads(),
        }
    except psutil.Error:
        process_usage = {"cpu_percent": None, "rss_bytes": None, "thread_count": None}
    admission = getattr(request.app.state, "api_request_admission", None)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    round_queue = getattr(request.app.state, "round_event_queue", None)
    return {"ok": True, "timestamp": time.time(), "process": {
        "pid": os.getpid(), "max_rss_kb": usage.ru_maxrss,
        "user_cpu_s": usage.ru_utime, "system_cpu_s": usage.ru_stime,
        **process_usage,
    }, "health": health,
        "api_admission": admission.snapshot() if admission else None,
        "round_event_queue": {
            "depth": round_queue.qsize() if round_queue is not None else 0,
            "capacity": round_queue.maxsize if round_queue is not None else 0,
            "worker_busy": bool(getattr(request.app.state, "round_event_processor_busy", False)),
        },
        "database_pool": repository.connection_pool_metrics() if repository else None}
