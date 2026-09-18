from fastapi import APIRouter, Request

router = APIRouter(tags=["data"])


@router.get("/api/data/status")
def data_status(request: Request):
    service = request.app.state.wp.dataset_service
    return {"quality": service.quality, "dataset": service.status()}


@router.get("/api/data/quality")
def data_quality(request: Request):
    return request.app.state.wp.dataset_service.quality


@router.get("/api/features")
def features(request: Request):
    return request.app.state.wp.dataset_service.metadata_payload()


@router.get("/api/features/latest")
def latest_features(request: Request):
    service = request.app.state.wp.dataset_service
    return {"status": service.status()["status"], "features": service.latest_features()}


@router.get("/api/dataset/status")
def dataset_status(request: Request):
    return request.app.state.wp.dataset_service.status()
