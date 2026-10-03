from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/api/opportunities/selective", tags=["selective-opportunities"])


@router.get("")
def current(request: Request):
    engine = request.app.state.opportunity_engine
    rounds = request.app.state.wp.dataset_service.clean_rounds.copy()
    return engine.current(rounds)


@router.post("/research")
async def run_research(request: Request):
    """Run the frozen chronological research procedure; never promotes to LIVE."""
    engine = request.app.state.opportunity_engine
    if engine._running:
        raise HTTPException(status_code=409, detail="Selective opportunity research is already running.")
    dataset = request.app.state.wp.dataset_service
    return await asyncio.to_thread(engine.run, dataset)
