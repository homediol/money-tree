from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.research.runner import ResearchConfig, StrategyExperimentRunner

router = APIRouter(prefix="/api/research", tags=["strategy-research"])


def runner(request: Request):
    return StrategyExperimentRunner(request.app.state.wp.settings.data_path,
                                    repository=request.app.state.wp.repository)


@router.post("/run")
async def run(request: Request):
    payload = await request.json()
    result = runner(request).run(ResearchConfig(**payload))
    return {"ok": True, "result": result}


@router.post("/stop")
async def stop(request: Request):
    return {"ok": True, "stopped": False, "reason": "research runs complete atomically"}


@router.get("")
async def list_runs(request: Request, limit: int = 25):
    return {"ok": True, "runs": request.app.state.wp.repository.list_research(min(max(limit, 1), 100))}


@router.get("/{research_id}")
async def get_run(request: Request, research_id: str):
    result = request.app.state.wp.repository.research_by_id(research_id)
    if result is None:
        raise HTTPException(status_code=404, detail="research run not found")
    return {"ok": True, "result": result}
