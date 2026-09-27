from fastapi import APIRouter, Request

from app.api.readiness import schedule_evaluation

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def models(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.get("/performance")
def performance(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.post("/train")
async def train_models(request: Request):
    return await schedule_evaluation(request)
