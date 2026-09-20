from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.backtesting.engine import BacktestConfig, BacktestEngine

router = APIRouter(prefix="/api/backtests", tags=["backtesting"])


def _repo(request: Request):
    return request.app.state.wp.repository


def _engine(request: Request) -> BacktestEngine:
    return BacktestEngine(request.app.state.wp.settings.data_path, repository=_repo(request))


@router.post("/run")
async def run_backtest(request: Request):
    payload = await request.json()
    result = _engine(request).run(BacktestConfig(**payload))
    return {"ok": True, "result": result}


@router.post("/stop")
async def stop_backtest(request: Request):
    return {"ok": True, "stopped": False, "reason": "backtests complete atomically"}


@router.get("")
async def list_backtests(request: Request, limit: int = 25):
    return {"ok": True, "runs": _repo(request).list_backtests(min(max(limit, 1), 100))}


@router.get("/{run_id}")
async def get_backtest(request: Request, run_id: str):
    result = _repo(request).backtest_by_id(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return {"ok": True, "result": result}
