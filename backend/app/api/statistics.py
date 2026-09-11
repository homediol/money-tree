from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["statistics"])


@router.get("/statistics")
async def get_statistics(request: Request):
    return request.app.state.wp.statistics()


@router.get("/status")
async def status(request: Request):
    wp = request.app.state.wp
    return {
        "app": "Winner Predict",
        "label": "STATISTICAL PATTERN ANALYSIS",
        "dataset_loaded": not wp.rounds.empty,
        "valid_rounds": int(len(wp.rounds)),
        "model_status": wp.model_registry.performance().get("status"),
        "data_source": str(wp.settings.data_path),
    }
