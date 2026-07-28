"""Monitoring, drift and continuous-learning endpoints."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import get_services
from src.api.services import ServiceContainer

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


class ContinuousLearningConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=60, le=86400)
    model_name: str = "xgboost"


@router.get("/drift")
async def get_drift(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return services.drift_service.compute().__dict__


@router.get("/drift/history")
async def get_drift_history(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return {"snapshots": services.drift_service.history(100)}


@router.get("/performance")
async def get_performance(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return {"metrics": services.performance_tracker.rolling_metrics(100)}


@router.get("/performance/resolved")
async def get_resolved(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return services.performance_tracker.counts()


@router.get("/system")
async def get_system(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return services.health.report().to_dict()


@router.get("/continuous-learning")
async def cl_status(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return {
        "running": services.continuous_learning._task is not None,
        "history": services.continuous_learning.history_dict(),
    }


@router.post("/continuous-learning/start")
async def cl_start(cfg: ContinuousLearningConfig, services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    await services.continuous_learning.start(model_name=cfg.model_name, interval_seconds=cfg.interval_seconds)
    return {"running": True, "next_run": services.continuous_learning.next_run(cfg.interval_seconds)}


@router.post("/continuous-learning/stop")
async def cl_stop(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    await services.continuous_learning.stop()
    return {"running": False}

