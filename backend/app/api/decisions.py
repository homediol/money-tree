from fastapi import APIRouter, Request

from app.decision.schemas import DecisionConfigUpdate

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


def _engine(request: Request):
    return request.app.state.decision_engine


def _states(request: Request):
    betting = request.app.state.betting.status()
    betting["emergency_stop"] = request.app.state.risk.emergency_latched
    wp = request.app.state.wp
    if not wp.dataset_service.clean_rounds.empty:
        betting["latest_history_round_id"] = str(wp.dataset_service.clean_rounds.iloc[-1]["round_id"])
    return wp.model_registry.latest_prediction(), wp.repository.latest_evidence(), wp.model_registry.status(wp.dataset_service), betting


@router.get("/current")
def current(request: Request):
    return {"decision": _engine(request).current()}


@router.get("/recent")
def recent(request: Request, limit: int = 25):
    rows = request.app.state.wp.repository.list_decisions(min(max(limit, 1), 500))
    return {"decisions": rows, "count": len(rows)}


@router.get("/status")
def status(request: Request):
    engine = _engine(request)
    return {"current": engine.current(), "config": engine.config.model_dump(),
            "places_bets": False, "risk_manager_required": True}


@router.get("/config")
def config(request: Request):
    return _engine(request).config.model_dump()


@router.patch("/config")
def update_config(request: Request, body: DecisionConfigUpdate):
    values = body.model_dump(exclude_none=True)
    return {"config": _engine(request).update_config(values)}


@router.post("/evaluate")
async def evaluate(request: Request):
    prediction, evidence, model, betting = _states(request)
    decision = await _engine(request).evaluate(prediction, evidence, model_status=model,
                                               betting_status=betting,
                                               risk_manager=request.app.state.risk)
    return {"decision": decision}


@router.post("/reload")
def reload_current(request: Request):
    """Revalidate the persisted contract; this never evaluates or executes it."""
    return {"decision": _engine(request).current()}


@router.get("/{decision_id}/audit")
def audit(decision_id: str, request: Request):
    rows = request.app.state.wp.repository.decision_audit(decision_id)
    return {"decision_id": decision_id, "entries": rows, "count": len(rows)}
