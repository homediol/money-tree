from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/patterns", tags=["patterns"])


def _report(request: Request, target: float) -> dict:
    if target <= 0:
        raise HTTPException(status_code=422, detail="target must be positive")
    return request.app.state.wp.pattern_report(target)


def _period_view(item: dict, period: str) -> dict:
    if period == "all":
        return item
    measured = item.get("recent", {}).get(period, {})
    return {**item, "selected_period": period, "selected_period_evidence": measured}


@router.get("")
def patterns(request: Request, target: float = 2.0, kind: str | None = None,
             period: str = Query("all", pattern="^(all|250|1000)$"), limit: int = 100):
    items = _report(request, target)["patterns"]
    if kind:
        items = [item for item in items if item["kind"] == kind]
    return {"patterns": [_period_view(item, period) for item in items[:min(max(limit, 1), 500)]]}


@router.get("/top")
def top_patterns(request: Request, target: float = 2.0, limit: int = 20):
    report = _report(request, target)
    return {"baseline": report["baseline"], "ranking_method": report["ranking_method"],
            "patterns": report["patterns"][:min(max(limit, 1), 100)]}


@router.get("/streaks")
def streaks(request: Request, target: float = 2.0, threshold: float | None = None,
            streak_length: str | None = None, period: str = Query("all", pattern="^(all|250|1000)$")):
    items = [item for item in _report(request, target)["patterns"] if item["kind"] == "streak"]
    if threshold is not None:
        items = [item for item in items if item["definition"]["threshold"] == threshold]
    if streak_length is not None:
        items = [item for item in items if str(item["definition"]["streak_length"]) == streak_length]
    return {"patterns": [_period_view(item, period) for item in items]}


@router.get("/sequences")
def sequences(request: Request, target: float = 2.0, length: int | None = None,
              period: str = Query("all", pattern="^(all|250|1000)$"), limit: int = 100):
    items = [item for item in _report(request, target)["patterns"] if item["kind"] == "sequence"]
    if length is not None:
        if length not in (2, 3, 4, 5):
            raise HTTPException(status_code=422, detail="length must be 2, 3, 4, or 5")
        items = [item for item in items if item["definition"]["length"] == length]
    return {"patterns": [_period_view(item, period) for item in items[:min(max(limit, 1), 500)]]}


@router.get("/baseline")
def baseline(request: Request, target: float = 2.0):
    return _report(request, target)["baseline"]


@router.get("/report")
def report(request: Request, target: float = 2.0):
    return _report(request, target)


@router.get("/{pattern_id}")
def pattern_detail(pattern_id: str, request: Request, target: float = 2.0):
    for item in _report(request, target)["patterns"]:
        if item["pattern_id"] == pattern_id:
            return item
    raise HTTPException(status_code=404, detail="Pattern not found")
