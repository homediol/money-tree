from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response


router = APIRouter(prefix="/api/opportunities/selective", tags=["selective-opportunities"])


def _timestamp_age_seconds(value) -> float | None:
    if not value:
        return None
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return None


def _collector_runtime(live: dict) -> dict:
    root = Path(__file__).resolve().parents[3]
    try:
        status = json.loads((root / "data/bot/status.json").read_text())
    except (OSError, json.JSONDecodeError):
        status = {}
    try:
        browser = json.loads((root / "data/bot/browser-reliability.json").read_text())
    except (OSError, json.JSONDecodeError):
        browser = {}
    latest_round = live.get("last_real_round") or {}
    collector_age = _timestamp_age_seconds(status.get("lastSuccessfulCollection"))
    database_write_age = _timestamp_age_seconds(latest_round.get("stored_at"))
    identity_verified = bool(latest_round.get("continuity_verified"))
    collector_live = bool(
        status.get("collectorRunning") is True
        and status.get("health") == "HEALTHY"
        and status.get("state") == "COLLECTING"
        and status.get("browserConnected") is True
        and status.get("pageConnected") is True
        and status.get("frameConnected") is True
        and status.get("loggedIn") is True
        and status.get("authRequired") is not True
        and collector_age is not None and collector_age <= 120
        and database_write_age is not None and database_write_age <= 120
    )
    reason = None
    if status.get("authRequired") is True or status.get("loggedIn") is not True:
        reason = "authentication is not verified"
    elif status.get("frameConnected") is not True:
        reason = "Aviator frame is disconnected"
    elif database_write_age is None or database_write_age > 120:
        reason = "no fresh PostgreSQL round write within 120 seconds"
    elif collector_age is None or collector_age > 120:
        reason = "collector persistence heartbeat is stale"
    elif not collector_live:
        reason = str(status.get("reason") or status.get("health") or "collector is not healthy and collecting")
    return {
        "status": "LIVE" if collector_live else "STALE",
        "reason": reason,
        "authenticated": status.get("loggedIn") is True and status.get("authRequired") is not True,
        "browser_connected": status.get("browserConnected") is True,
        "history_page_connected": status.get("pageConnected") is True,
        "aviator_frame_connected": status.get("frameConnected") is True,
        "history_page": browser.get("history_page"),
        "betting_page": browser.get("betting_page"),
        "browser_state": browser.get("state"),
        "collector_health": status.get("health"),
        "collector_state": status.get("state"),
        "last_successful_collection": status.get("lastSuccessfulCollection"),
        "collector_freshness_seconds": collector_age,
        "last_postgresql_write": latest_round.get("stored_at"),
        "postgresql_write_freshness_seconds": database_write_age,
        "last_real_round_id": latest_round.get("round_id"),
        "last_real_round_index": latest_round.get("round_index"),
        "last_platform_round_id": latest_round.get("platform_round_id"),
        "last_platform_round_index": latest_round.get("platform_round_index"),
        "last_platform_timestamp": latest_round.get("platform_timestamp"),
        "identity_verified": identity_verified,
    }


def _frozen_live_response(request: Request, engine, *, lightweight: bool = False):
    result = (engine.lightweight_live_dashboard() if lightweight else engine.live_dashboard())
    collector = _collector_runtime(result)
    result["collector"] = collector
    if collector["status"] != "LIVE":
        result["observer_active"] = False
        result["observer_state"] = ("PAUSED_REQUIRES_OPERATOR"
                                     if collector.get("reason") == "authentication is not verified"
                                     else "PAUSED_SOURCE_STALE")
        result["observer_status"] = "PAUSED"
        result["observer_reason"] = collector.get("reason") or "collector source is stale"
    result["pipeline_state"] = ("LIVE" if collector["status"] == "LIVE" and result.get("observer_active")
                                else "LIVE_OBSERVATION_PAUSED")
    result["automatic_real_money_execution"] = "OFF"
    return result


@router.get("")
def current(request: Request, response: Response):
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Frozen V3 prospective observer is not loaded.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    # Keep older clients from repeatedly running expensive historical
    # diagnostics while the product surface is prospective-only.
    return _frozen_live_response(request, engine, lightweight=True)


@router.get("/v4/status")
def current_v4(request: Request, response: Response):
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="V4 prospective observer is not loaded.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return _frozen_live_response(request, engine)


@router.get("/v4/observer")
def v4_observer_status(request: Request, response: Response):
    """Expose light, durable observer liveness without historical diagnostics."""
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="V4 prospective observer is not loaded.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return engine.observer_status()


@router.get("/v4/live")
def v4_live_board(request: Request, response: Response):
    """Serve only frozen prospective data for the live observation board."""
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Frozen V3 prospective observer is not loaded.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return _frozen_live_response(request, engine, lightweight=True)


@router.get("/v4/experiment/status")
def v4_experiment_status(request: Request, response: Response):
    """Return authoritative durable experiment state independently of live diagnostics."""
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Frozen V3 prospective observer is not loaded.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    try:
        return engine.prospective_experiment_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "reason": "PostgreSQL prospective experiment state could not be read",
            "error_type": type(exc).__name__,
        }) from exc


@router.post("/v4/experiment/start")
async def start_new_prospective_experiment(request: Request, response: Response):
    """Explicitly start a fresh prospective timeline at the current real DB tail."""
    engine = getattr(request.app.state, "opportunity_v4_engine", None)
    repository = getattr(getattr(getattr(request.app.state, "wp", None), "repository", None), "database_url", None)
    if engine is None or not repository:
        raise HTTPException(status_code=503, detail="Frozen observer and authoritative PostgreSQL are required.")
    board = engine.live_dashboard()
    collector = _collector_runtime(board)
    if collector.get("status") != "LIVE":
        raise HTTPException(status_code=409, detail={"reason": collector.get("reason") or "real collector is not fresh"})
    warmup = board.get("scoring_warmup") or {}
    if (board.get("observer_active") is not True
            or int(warmup.get("verified_rounds") or 0) < int(warmup.get("required_rounds") or 102)):
        raise HTTPException(status_code=409, detail={
            "reason": "frozen observer must be active and its 102-round verified warm-up complete",
            "observer_status": board.get("observer_status"),
            "warmup_progress": f"{warmup.get('verified_rounds', 0)}/{warmup.get('required_rounds', 102)}",
        })
    active = engine.repository.active_opportunity_experiment()
    if active:
        raise HTTPException(status_code=409, detail={"reason": "an experiment is already active",
                                                     "experiment_id": active.get("experiment_id")})
    result = await asyncio.to_thread(engine.start_prospective_experiment, explicit=True)
    if result.get("status") != "ACTIVE":
        raise HTTPException(status_code=409, detail=result.get("reason") or result)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return result


@router.post("/research")
async def run_research(request: Request):
    """Research is disabled while the frozen prospective observation is active."""
    raise HTTPException(status_code=409, detail="Research and optimization are disabled; the frozen V3 prospective observer is active.")


@router.post("/research-v3")
async def run_research_v3(request: Request):
    """Discovery is disabled; prospective outcomes cannot tune the frozen ranker."""
    raise HTTPException(status_code=409, detail="V3 discovery is disabled; keep the current frozen configuration unchanged.")
