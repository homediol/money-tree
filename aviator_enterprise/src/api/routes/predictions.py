"""
Prediction endpoints
====================

All predictions are generated from the historical round data in
``roundhistory.json``.  There is no live game feed, no crash simulator,
and no WebSocket auto-prediction loop.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.dependencies import get_services
from src.api.services import ServiceContainer
from src.core.helpers import safe_float

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
class PredictRequest(BaseModel):
    """
    Request a next-round prediction.

    The model reads the full round history, engineers features on the
    complete series, and returns a predicted class + probabilities for
    the *next* round.
    """
    model_name: str = Field(default="xgboost", description="Registered model to use")
    explain: bool = Field(default=True, description="Include SHAP top-features in the response")


class ResolveRequest(BaseModel):
    """Provide the actual outcome of a previously predicted round."""
    prediction_id: str = Field(..., description="ID returned by /predict")
    actual_multiplier: float = Field(..., ge=1.0, description="Actual multiplier from roundhistory")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/predict")
async def predict(
    req: PredictRequest,
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """
    Generate a next-round prediction from the complete round history.

    The pipeline:
    1. Load all rounds from ``roundhistory.json``
    2. Preprocess and engineer features on the full series
    3. Pass the latest feature vector to the trained model
    4. Return predicted class, class probabilities, confidence,
       uncertainty, SHAP top-features, risk level, and recommendation
    """
    response = await services.prediction_service.predict(
        model_name=req.model_name, explain=req.explain
    )
    return response.to_dict()


@router.post("/resolve")
async def resolve_prediction(
    req: ResolveRequest,
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """
    Mark a prediction as resolved with the real outcome.

    This updates accuracy tracking and appends the new round to the
    in-memory history so future predictions include it.
    """
    from config.settings import settings
    from src.data.collector import RoundRecord
    from src.core.helpers import utc_now

    multiplier = safe_float(req.actual_multiplier, default=0.0)
    if multiplier < 1.0:
        raise HTTPException(400, "actual_multiplier must be >= 1.0")

    boundaries = settings.category_boundary_list()
    names = settings.category_name_list()
    actual_class = len(boundaries)  # default: highest bucket
    for i, b in enumerate(boundaries):
        if multiplier < b:
            actual_class = i
            break

    ok = services.performance_tracker.resolve(
        req.prediction_id,
        actual_class,
        actual_multiplier=multiplier,
        class_names=names,
    )
    if not ok:
        raise HTTPException(404, "Prediction not found or already resolved")

    # Append the resolved round so the next prediction sees it
    records = await services.collector.collect()
    next_id = (max((r.round_id for r in records), default=0)) + 1
    rec = RoundRecord(
        round_id=next_id,
        multiplier=multiplier,
        timestamp=utc_now().timestamp(),
        source="resolved",
    )
    services.collector.append_round(rec)

    return {
        "resolved": True,
        "prediction_id": req.prediction_id,
        "actual_class": names[actual_class],
        "actual_multiplier": multiplier,
    }


@router.get("/recent")
async def recent_predictions(
    n: int = Query(default=50, ge=1, le=500),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """Return the N most recent prediction log entries."""
    return {"predictions": services.performance_tracker.recent(n)}


@router.get("/log")
async def prediction_log(
    n: int = Query(default=200, ge=1, le=1000),
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """Full prediction log (alias for /recent with a higher default)."""
    return {"predictions": services.performance_tracker.recent(n)}


@router.get("/counts")
async def prediction_counts(
    services: ServiceContainer = Depends(get_services),
) -> Dict[str, Any]:
    """Total / resolved / pending / correct counts."""
    return services.performance_tracker.counts()
