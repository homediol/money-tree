from fastapi import APIRouter, BackgroundTasks, Request

router = APIRouter(prefix="/api/models", tags=["models"])


def _train(wp):
    result = wp.model_registry.train(wp.dataset_service)
    with wp._cache_lock:
        wp._analysis_cache = None
    return result


@router.get("")
def models(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.get("/performance")
def performance(request: Request):
    return request.app.state.wp.model_registry.performance()


@router.post("/train")
def train_models(request: Request, background_tasks: BackgroundTasks, background: bool = False):
    wp = request.app.state.wp
    live = getattr(request.app.state, "live", None)
    if live and live.live_active:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Pause live betting before changing the model")
    if background:
        background_tasks.add_task(_train, wp)
        return {"status": "TRAINING SCHEDULED", "dataset_size": int(len(wp.dataset_service.dataset))}
    result = _train(wp)
    return result.model_dump()
