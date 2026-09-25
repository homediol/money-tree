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
    current = engine.current()
    wp = request.app.state.wp
    dataset = wp.dataset_service
    model = wp.model_registry.status(dataset)
    inference = wp.model_registry.prediction_payload(dataset)
    betting = request.app.state.betting.status()
    emergency = bool(request.app.state.risk.emergency_latched)
    blockers: list[str] = []
    if not model.get("deployable"):
        blockers.append(f"model:{model.get('status', 'UNKNOWN')}")
    if not inference.get("usable"):
        blockers.append(f"prediction:{inference.get('reason', 'UNAVAILABLE')}")
    if not engine.config.automatic_mode:
        blockers.append("decision_automatic_mode_off")
    if not betting.get("automatic_enabled"):
        blockers.append("betting_session_off")
    if emergency:
        blockers.append("emergency_stop")
    current_executable = bool(
        current
        and current.get("status") == "READY_FOR_EXECUTION"
        and current.get("risk_status") == "APPROVED"
        and current.get("execution_status") == "READY"
    )
    if not current_executable:
        blockers.append(f"current_decision:{current.get('status') if current else 'MISSING'}")
    history = wp.repository.list_decisions(500)
    return {
        "current": current,
        "config": engine.config.model_dump(),
        "places_bets": False,
        "risk_manager_required": True,
        "decision_file": {
            "role": "CURRENT_HANDOFF_ONLY",
            "contains_one_record_by_design": True,
        },
        "history_count": len(history),
        "pipeline": {
            "state": "READY" if not blockers else "BLOCKED",
            "blockers": list(dict.fromkeys(blockers)),
            "model_status": model.get("status"),
            "model_deployable": bool(model.get("deployable")),
            "prediction_usable": bool(inference.get("usable")),
            "prediction_reason": inference.get("reason"),
            "session_mode": betting.get("mode", "OFF"),
            "automatic_enabled": bool(betting.get("automatic_enabled")),
            "emergency_stop": emergency,
            "current_executable": current_executable,
        },
    }


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
