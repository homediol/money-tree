from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/ml", tags=["ml"])


@router.get("/status")
def status(request: Request):
    wp = request.app.state.wp
    return wp.model_registry.status(wp.dataset_service)


@router.get("/model")
def model(request: Request):
    return request.app.state.wp.model_registry.model_info()


@router.get("/metrics")
def metrics(request: Request):
    return request.app.state.wp.model_registry.metrics()


@router.get("/prediction/latest")
def latest_prediction(request: Request):
    wp = request.app.state.wp
    prediction = wp.model_registry.latest_prediction()
    latest_id = (str(wp.dataset_service.clean_rounds.iloc[-1]["round_id"])
                 if not wp.dataset_service.clean_rounds.empty else None)
    if (prediction and prediction.get("source_round_id") == latest_id
            and wp.model_registry.status(wp.dataset_service)["status"] == "READY"):
        return {"prediction": prediction, "reason": "OK"}
    return {"prediction": None, "reason": wp.model_registry.prediction_payload(wp.dataset_service)["reason"]}


@router.get("/estimate")
def estimate(request: Request):
    """Display-only inference; a frequency is never an executable prediction."""
    wp = request.app.state.wp
    payload = wp.model_registry.prediction_payload(wp.dataset_service)
    if payload["usable"]:
        return {"status": "VALIDATED_MODEL", **payload, "estimate": payload["prediction"]}
    return {"status": "INFORMATIONAL_FALLBACK", **payload,
            "estimate": wp.model_registry.fallback_estimate(wp.dataset_service)}


@router.get("/predictions/recent")
def recent_predictions(request: Request, limit: int = 25):
    rows = request.app.state.wp.model_registry.recent_predictions(min(max(limit, 1), 500))
    return {"predictions": rows, "count": len(rows)}


@router.post("/train")
def train(request: Request):
    wp = request.app.state.wp
    live = getattr(request.app.state, "live", None)
    if live and live.live_active:
        raise HTTPException(status_code=409, detail="Pause live betting before changing the model")
    if wp.model_registry.status(wp.dataset_service)["status"] == "TRAINING":
        raise HTTPException(status_code=409, detail="Training is already running")
    return wp.model_registry.train(wp.dataset_service).model_dump()
