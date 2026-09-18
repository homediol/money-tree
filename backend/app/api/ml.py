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
    return {"prediction": request.app.state.wp.model_registry.latest_prediction()}


@router.get("/predictions/recent")
def recent_predictions(request: Request, limit: int = 25):
    rows = request.app.state.wp.model_registry.recent_predictions(min(max(limit, 1), 500))
    return {"predictions": rows, "count": len(rows)}


@router.post("/train")
def train(request: Request):
    wp = request.app.state.wp
    if wp.model_registry.status(wp.dataset_service)["status"] == "TRAINING":
        raise HTTPException(status_code=409, detail="Training is already running")
    return wp.model_registry.train(wp.dataset_service).model_dump()
