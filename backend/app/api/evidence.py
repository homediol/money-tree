from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _latest(request: Request):
    return request.app.state.wp.repository.latest_evidence()


@router.get("/current")
def current(request: Request):
    return {"evidence": _latest(request)}


@router.get("/recent")
def recent(request: Request, limit: int = 25):
    rows = request.app.state.wp.repository.list_evidence(min(max(limit, 1), 500))
    return {"evidence": rows, "count": len(rows)}


@router.get("/prediction/{prediction_id}")
def prediction_evidence(prediction_id: str, request: Request):
    row = request.app.state.wp.repository.evidence_for_prediction(prediction_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evidence snapshot not found")
    return row


@router.get("/patterns")
def patterns(request: Request):
    row = _latest(request)
    return {"pattern": row.get("pattern") if row else None}


@router.get("/stability")
def stability(request: Request):
    row = _latest(request)
    return {"stability": row.get("stability") if row else "INSUFFICIENT_DATA"}


@router.get("/model-quality")
def model_quality(request: Request):
    row = _latest(request)
    return {"model_quality": row.get("model_quality") if row else None}


@router.get("/baselines")
def baselines(request: Request):
    row = _latest(request)
    return {"baselines": row.get("baselines") if row else None}
