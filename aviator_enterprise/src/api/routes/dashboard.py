"""
Dashboard data endpoints
========================

Every panel the enterprise UI needs.  All data originates from the local
``roundhistory.json`` file — there is no live game feed, no WebSocket
prediction loop, and no crash simulator.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from src.api.dependencies import get_services
from src.api.services import ServiceContainer
from src.evaluation.metrics import EvaluationService

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _sanitize(value: Any) -> Any:
    """Recursively replace NaN / Inf with None so JSON serialises cleanly."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
@router.get("/overview")
async def overview(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    """Top KPI cards — model info, counts, health, drift."""
    best = services.model_registry.best_overall()
    active = (
        services.prediction_service.active_model_version.to_dict()
        if services.prediction_service.active_model_version
        else None
    )
    counts = services.performance_tracker.counts()
    health = services.health.report(
        rolling_accuracy=services.performance_tracker.rolling_metrics(50).get("accuracy", 0.0)
    )
    return _sanitize({
        "active_model": active,
        "best_model": best.to_dict() if best else None,
        "counts": counts,
        "health": health.to_dict(),
        "drift": services.drift_service.compute().__dict__,
    })


# ---------------------------------------------------------------------------
# Next-round prediction  (based on round history — NOT a live game feed)
# ---------------------------------------------------------------------------
@router.get("/next-round-prediction")
async def next_round_prediction(
    model_name: str = Query(default="xgboost"),
    explain: bool = Query(default=True),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """
    Generate a prediction for the *next* round using all historical rounds
    from ``roundhistory.json`` as context.

    This endpoint loads the round history, engineers features on the full
    series, and asks the trained model what bucket the next round will fall
    into.  It is the only way predictions are generated — there is no
    auto-polling, no game loop, and no crash simulator.
    """
    response = await services.prediction_service.predict(
        model_name=model_name, explain=explain
    )
    return _sanitize(response.to_dict())


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
@router.get("/confidence")
async def confidence(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize(services.performance_tracker.confidence_histogram())


@router.get("/probability-distribution")
async def probability_distribution(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    recents = list(services.performance_tracker.predictions)[-200:]
    if not recents:
        return {"classes": [], "data": []}
    from config.settings import settings
    classes = (
        services.prediction_service._model.class_names
        if services.prediction_service._model
        else settings.category_name_list()
    )
    return {
        "classes": classes,
        "data": [
            {"class": c, "values": [p.probabilities.get(c, 0.0) for p in recents]}
            for c in classes
        ],
    }


@router.get("/accuracy-trend")
async def accuracy_trend(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize({"trend": services.performance_tracker.accuracy_trend(50)})


@router.get("/metrics-trend")
async def metrics_trend(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize({"trend": services.performance_tracker.accuracy_trend(50)})


@router.get("/confusion-matrix")
async def confusion_matrix(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    resolved = [p for p in services.performance_tracker.predictions if p.resolved]
    if not resolved:
        return {"matrix": [], "labels": [], "normalized": []}
    from config.settings import settings
    classes = settings.category_name_list()
    y_true = np.array([p.actual_class for p in resolved])
    y_pred = np.array([p.predicted_class for p in resolved])
    return _sanitize(EvaluationService().confusion_matrix(y_true, y_pred, classes))


@router.get("/roc-curve")
async def roc_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    resolved = [p for p in services.performance_tracker.predictions if p.resolved]
    if not resolved:
        return {}
    from config.settings import settings
    class_names = settings.category_name_list()
    y_true = np.array([p.actual_class for p in resolved])
    proba = np.array([[p.probabilities.get(c, 0.0) for c in class_names] for p in resolved])
    return _sanitize(EvaluationService().roc_curve(y_true, proba, len(class_names)))


@router.get("/pr-curve")
async def pr_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    resolved = [p for p in services.performance_tracker.predictions if p.resolved]
    if not resolved:
        return {}
    from config.settings import settings
    class_names = settings.category_name_list()
    y_true = np.array([p.actual_class for p in resolved])
    proba = np.array([[p.probabilities.get(c, 0.0) for c in class_names] for p in resolved])
    return _sanitize(EvaluationService().pr_curve(y_true, proba, len(class_names)))


@router.get("/calibration-curve")
async def calibration_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    resolved = [p for p in services.performance_tracker.predictions if p.resolved]
    if not resolved:
        return {}
    from config.settings import settings
    class_names = settings.category_name_list()
    y_true = np.array([p.actual_class for p in resolved])
    proba = np.array([[p.probabilities.get(c, 0.0) for c in class_names] for p in resolved])
    return _sanitize(EvaluationService().calibration_curve(y_true, proba, len(class_names)))


@router.get("/feature-importance")
async def feature_importance(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    if services.prediction_service._model is None:
        return {"features": []}
    return _sanitize({"features": services.prediction_service._feature_importance()})


@router.get("/class-distribution")
async def class_distribution(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return {"distribution": services.performance_tracker.class_distribution()}


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------
@router.get("/drift")
async def drift(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize(services.drift_service.compute().__dict__)


@router.get("/drift-history")
async def drift_history(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return {"snapshots": services.drift_service.history(100)}


# ---------------------------------------------------------------------------
# Training curves
# ---------------------------------------------------------------------------
@router.get("/training-history")
async def training_history(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    model = services.prediction_service._model
    return _sanitize({"history": model.training_history if model else {}})


@router.get("/learning-curve")
async def learning_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    model = services.prediction_service._model
    return _sanitize({"history": model.training_history if model else {}})


@router.get("/loss-curve")
async def loss_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    h = services.prediction_service._model.training_history if services.prediction_service._model else {}
    return _sanitize({"history": {k: v for k, v in h.items() if "loss" in k}})


@router.get("/validation-curve")
async def validation_curve(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    h = services.prediction_service._model.training_history if services.prediction_service._model else {}
    return _sanitize({"history": {k: v for k, v in h.items() if k.startswith("val")}})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@router.get("/model-comparison")
async def model_comparison(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    rows = [
        {"model_name": v.model_name, "version": v.version,
         "is_active": v.is_active, "created_at": v.created_at, **v.metrics}
        for v in services.model_registry.list()
    ]
    return _sanitize({"rows": rows})


# ---------------------------------------------------------------------------
# Metrics / rolling
# ---------------------------------------------------------------------------
@router.get("/confidence-histogram")
async def confidence_histogram(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize(services.performance_tracker.confidence_histogram())


@router.get("/rolling-metrics")
async def rolling_metrics(
    window: int = Query(default=100, ge=10, le=1000),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    return _sanitize({"metrics": services.performance_tracker.rolling_metrics(window)})


@router.get("/prediction-log")
async def prediction_log(
    n: int = Query(default=50, ge=1, le=500),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    return {"predictions": services.performance_tracker.recent(n)}


# ---------------------------------------------------------------------------
# Dataset / system
# ---------------------------------------------------------------------------
@router.get("/dataset-stats")
async def dataset_stats(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    records = await services.collector.collect()
    df = services.preprocessor.transform(records)
    class_dist: Dict[str, int] = {}
    if "category_name" in df.columns:
        class_dist = {k: int(v) for k, v in df["category_name"].value_counts().to_dict().items()}
    num_cols = df.select_dtypes(include="number").columns[:20]
    stats = {
        c: {
            "mean": float(df[c].mean()),
            "std": float(df[c].std()),
            "min": float(df[c].min()),
            "max": float(df[c].max()),
            "null_pct": float(df[c].isna().mean()),
        }
        for c in num_cols
    }
    dataset_version = "--"
    try:
        meta = await services.feature_store.metadata("aviator_features")
        dataset_version = meta.version
    except Exception:
        pass
    return _sanitize({
        "total_rounds": len(records),
        "total_features": len(df.columns),
        "class_distribution": class_dist,
        "stats": stats,
        "first_round_ts": records[0].timestamp if records else None,
        "last_round_ts": records[-1].timestamp if records else None,
        "dataset_version": dataset_version,
    })


@router.get("/system-stats")
async def system_stats(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize(services.health.report().to_dict())


@router.get("/health")
async def health(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    return _sanitize(
        services.health.report(
            rolling_accuracy=services.performance_tracker.rolling_metrics(50).get("accuracy", 0.0)
        ).to_dict()
    )


@router.get("/retraining-info")
async def retraining_info(services: ServiceContainer = Depends(get_services)) -> Dict[str, Any]:
    from config.settings import settings as s
    model_version = "--"
    last_training_time = "--"
    try:
        mv = services.model_registry.get_active("xgboost")
        model_version = mv.version
        last_training_time = mv.created_at
    except Exception:
        pass
    dataset_version = "--"
    try:
        meta = await services.feature_store.metadata("aviator_features")
        dataset_version = meta.version
    except Exception:
        pass
    cl_running = services.continuous_learning._task is not None
    next_retrain = services.continuous_learning.next_run(s.drift_check_interval) if cl_running else "--"
    return {
        "model_version": model_version,
        "dataset_version": dataset_version,
        "last_training_time": last_training_time,
        "next_scheduled_retrain": next_retrain,
        "cl_running": cl_running,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
@router.get("/export/{kind}")
async def export(
    kind: str,
    n: int = Query(default=200, ge=1, le=5000),
    services: ServiceContainer = Depends(get_services),
):
    if kind not in ("csv", "excel", "json", "pdf"):
        raise HTTPException(400, "kind must be csv|excel|json|pdf")
    data = services.performance_tracker.recent(n)
    if kind == "csv":
        path = services.report_generator.export_predictions_csv(data)
    elif kind == "excel":
        path = services.report_generator.export_predictions_excel(data)
    elif kind == "json":
        path = services.report_generator.export_predictions_json(data)
    else:
        path = services.report_generator.export_pdf(
            "Aviator ML — Prediction Report", {"rows": len(data)}, data
        )
    return FileResponse(path, filename=path.name)
