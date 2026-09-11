from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/patterns", tags=["patterns"])


@router.get("")
def patterns(request: Request):
    return {"patterns": request.app.state.wp.patterns()}


@router.get("/{pattern_id}")
def pattern_detail(pattern_id: str, request: Request):
    for item in request.app.state.wp.patterns():
        if item["pattern_id"] == pattern_id:
            return item
    raise HTTPException(status_code=404, detail="Pattern not found")

