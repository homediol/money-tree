from fastapi import APIRouter, BackgroundTasks, Request

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def models(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.get("/performance")
def performance(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.post("/train")
def train_models(request: Request, background_tasks: BackgroundTasks, background: bool = False):
    wp = request.app.state.wp
    if background:
        background_tasks.add_task(wp.model_registry.train, wp.rounds)
        return {"status": "TRAINING SCHEDULED", "dataset_size": int(len(wp.rounds))}
    result = wp.model_registry.train(wp.rounds)
    wp.repository.save_model_version(result.trained_at, "ensemble", None, result.model_dump())
    return result.model_dump()

