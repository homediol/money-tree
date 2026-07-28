"""Model & training endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.dependencies import get_services
from src.api.services import ServiceContainer
from src.core.exceptions import HyperparameterOptimizationError, ModelNotFoundError
from src.evaluation.metrics import EvaluationService

router = APIRouter(prefix="/api/models", tags=["models"])


class TrainRequest(BaseModel):
    model_name: str = Field(default="xgboost")
    optimize: bool = Field(default=False)
    n_optuna_trials: Optional[int] = None
    cross_validate: bool = Field(default=True)
    calibrate: bool = Field(default=True)
    class_imbalance: str = Field(default="class_weight")
    hyperparameters: Optional[Dict[str, Any]] = None
    persist: bool = Field(default=True)


class TrainAllRequest(BaseModel):
    models: List[str] = Field(default_factory=lambda: ["random_forest", "xgboost", "lightgbm"])
    optimize: bool = False
    n_optuna_trials: Optional[int] = None


@router.post("/train")
async def train_model(
    req: TrainRequest,
    background: BackgroundTasks,
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    try:
        result = await services.trainer.train(
            model_name=req.model_name,
            optimize=req.optimize,
            n_optuna_trials=req.n_optuna_trials,
            cross_validate=req.cross_validate,
            calibrate=req.calibrate,
            class_imbalance=req.class_imbalance,
            hyperparameters=req.hyperparameters,
            persist=req.persist,
        )
        return _result_to_dict(result)
    except Exception as exc:
        raise HTTPException(500, f"Training failed: {exc}") from exc


@router.post("/train-all")
async def train_all(
    req: TrainAllRequest,
    background: BackgroundTasks,
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for name in req.models:
        try:
            r = await services.trainer.train(
                model_name=name,
                optimize=req.optimize,
                n_optuna_trials=req.n_optuna_trials,
                cross_validate=True,
                calibrate=True,
            )
            results.append(_result_to_dict(r))
        except Exception as exc:
            results.append({"model": name, "error": str(exc)})
    return {"results": results}


@router.get("/registry")
async def model_registry(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    versions = services.model_registry.list()
    return {
        "versions": [v.to_dict() for v in versions],
        "active": {name: services.model_registry.get_active(name).version for name in {v.model_name for v in versions}},
    }


@router.get("/registry/{model_name}")
async def registry_for_model(model_name: str, services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    try:
        return {
            "model_name": model_name,
            "versions": [v.to_dict() for v in services.model_registry.list(model_name)],
            "active_version": services.model_registry.get_active(model_name).version,
        }
    except ModelNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/registry/{model_name}/activate/{version}")
async def activate_version(
    model_name: str,
    version: str,
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    try:
        services.model_registry.set_active(model_name, version)
        return {"model_name": model_name, "active_version": version}
    except ModelNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/compare")
async def compare_models(
    model_name: str = Query(...),
    metric: str = Query(default="f1_macro"),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    return {"model_name": model_name, "metric": metric, "comparison": services.model_registry.compare(model_name, metric)}


@router.get("/best")
async def best_model(
    metric: str = Query(default="f1_macro"),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    best = services.model_registry.best_overall(metric)
    if best is None:
        return {"best": None}
    return {"best": best.to_dict()}


def _result_to_dict(r) -> Dict[str, Any]:
    cv = r.cv_report
    out = {
        "model_name": r.model_name,
        "model_version": r.model_version,
        "metrics": r.metrics,
        "n_samples": r.n_samples,
        "n_features": r.n_features,
        "class_distribution": r.class_distribution,
        "feature_names": r.feature_names,
        "duration_seconds": r.duration_seconds,
        "artifact_path": r.artifact_path,
        "training_history": r.training_history,
    }
    if cv is not None:
        out["cv"] = {
            "n_folds": cv.n_folds,
            "mean_metrics": cv.mean_metrics,
            "std_metrics": cv.std_metrics,
            "bootstrap_ci": {k: list(v) for k, v in cv.bootstrap_ci.items()},
            "fold_metrics": cv.fold_metrics,
        }
    if r.optimization:
        out["optimization"] = {
            "best_params": r.optimization.best_params,
            "best_score": r.optimization.best_score,
            "n_trials": r.optimization.n_trials,
            "duration_seconds": r.optimization.duration_seconds,
        }
    return out

