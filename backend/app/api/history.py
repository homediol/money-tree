from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/history", tags=["history"])


def _round_stream_state(*, observed: dict | None, verified_live: dict | None,
                        age_seconds: float | None, freshness_limit_ms: float | None) -> str:
    if verified_live:
        if freshness_limit_ms is None:
            return "WAITING_FOR_CADENCE"
        if age_seconds is not None and age_seconds * 1000.0 <= float(freshness_limit_ms):
            return "LIVE"
        return "STALE"
    if observed and (observed.get("continuity_status") == "GAP_BEFORE"
                     or str(observed.get("delivery_mode") or "").upper() == "RECOVERED"):
        return "RECONCILING"
    if observed:
        return "GAP_SUSPECTED"
    return "WAITING_FOR_LIVE"


@router.get("")
def history(request: Request, limit: int = 250):
    limit = min(max(limit, 1), 5000)
    repository = request.app.state.wp.repository
    if repository.database_url:
        # The collector can continue writing after AppState's startup
        # snapshot. Read PostgreSQL directly so this page stays current.
        rows = repository.load_recent_rounds(limit)
        count = repository.round_history_status()["count"]
    else:
        all_rows = request.app.state.history_collector.rows()
        count = len(all_rows)
        rows = all_rows[-limit:]
    return {
        "count": count,
        "returned": len(rows),
        "rounds": rows,
    }


@router.get("/status")
async def collector_status(request: Request):
    payload = request.app.state.history_collector.status()
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    hub = getattr(request.app.state, "round_distribution", None)
    hub_status = await hub.metrics() if hub is not None else {}
    observed = hub_status.get("last_published_observed") or None
    now = datetime.now(timezone.utc)
    observed_at = None
    observed_age = None
    if isinstance(observed, dict):
        observed_at = observed.get("observed_at")
        try:
            stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            observed_age = max(0.0, (now - stamp.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            observed_age = None
    verified_observed = (observed if isinstance(observed, dict)
        and observed.get("continuity_status") == "VERIFIED"
        and str(observed.get("delivery_mode") or "").upper() == "LIVE" else None)
    inbox_status = inbox.snapshot() if inbox is not None else {}
    live_interval = hub_status.get("verified_live_round_interval_ms") or {}
    freshness_limit_ms = (live_interval.get("p95_ms")
                          if int(live_interval.get("samples") or 0) > 0 else None)
    consumer_task = getattr(request.app.state, "behaviour_round_consumer_task", None)
    collector_state = _round_stream_state(observed=observed, verified_live=verified_observed,
        age_seconds=observed_age, freshness_limit_ms=freshness_limit_ms)
    payload["round_stream"] = {
        "state": collector_state,
        "latest_observed_round_uid": ((observed or {}).get("round_uid")
                                       or (observed or {}).get("round_id")),
        "latest_observed_sequence": (observed or {}).get("canonical_sequence")
                                     or (observed or {}).get("round_index"),
        "latest_observed_multiplier": (observed or {}).get("multiplier"),
        "latest_multiplier": (verified_observed or {}).get("multiplier"),
        "latest_observed_at": observed_at,
        "observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "verified_live_round_interval_ms": live_interval,
        "freshness_limit_ms": freshness_limit_ms,
        "freshness_basis": ("OBSERVED_VERIFIED_LIVE_ROUND_INTERVAL_P95"
                            if freshness_limit_ms is not None else "WAITING_FOR_TWO_LIVE_ROUNDS"),
        "latest_hub_published_round_uid": ((observed or {}).get("round_uid")
                                             or (observed or {}).get("round_id")),
        "latest_verified_round_uid": ((verified_observed or {}).get("round_uid")
                                       or (verified_observed or {}).get("round_id")),
        "latest_verified_event_at": (verified_observed or {}).get("verified_at"),
        "hub_last_observed_publish_at": hub_status.get("last_observed_publish_at"),
        "hub_last_persisted_round_uid": ((hub_status.get("last_published_round") or {}).get("round_uid")
                                         or (hub_status.get("last_published_round") or {}).get("round_id")),
        "hub_last_publication_error": hub_status.get("last_publication_error"),
        "hub_subscriber_count": hub_status.get("connected_clients", 0),
        "hub_subscriber_queue_depth": hub_status.get("client_queue_depth", 0),
        "hub_delivery_ack_protocol": hub_status.get("delivery_ack_protocol"),
        "behaviour_consumer_state": ("RUNNING" if consumer_task and not consumer_task.done()
                                     else "STOPPED"),
        "behaviour_latest_round_uid": getattr(request.app.state,
                                             "behaviour_round_last_delivered_id", None),
        "behaviour_last_delivered_at": getattr(request.app.state,
                                                "behaviour_round_last_delivered_at", None),
        "behaviour_delivery_latency_ms": getattr(request.app.state,
                                                  "behaviour_round_last_delivery_ms", None),
        "behaviour_last_error": getattr(request.app.state, "behaviour_round_last_error", None),
        "behaviour_backlog": inbox_status.get("behaviour_pending"),
        "behaviour_checkpoint": inbox_status.get("behaviour_checkpoint"),
        "rules_checkpoint": inbox_status.get("rules_checkpoint"),
        "hub_backlog": inbox_status.get("backlog_size"),
        "rules_observed_pending": inbox_status.get("rules_observed_pending"),
        "rules_committed_pending": inbox_status.get("committed_rules_pending"),
        "latest_inbox_sequence": inbox_status.get("latest_inbox_sequence"),
        "checkpoint_namespace": inbox_status.get("checkpoint_namespace"),
    }
    return payload


@router.get("/latest")
def latest(request: Request):
    repository = request.app.state.wp.repository
    if repository.database_url:
        rows = repository.load_recent_rounds(2)
        return {"latest": rows[-1] if rows else None,
                "previous": rows[-2] if len(rows) > 1 else None}
    rows = request.app.state.history_collector.rows()
    return {"latest": rows[-1] if rows else None, "previous": rows[-2] if len(rows) > 1 else None}


@router.get("/recent")
def recent(request: Request, limit: int = 25):
    limit = min(max(limit, 1), 5000)
    repository = request.app.state.wp.repository
    if repository.database_url:
        rows = repository.load_recent_rounds(limit)
        return {"count": repository.round_history_status()["count"],
                "returned": len(rows), "rounds": rows}
    rows = request.app.state.history_collector.rows()
    return {"count": len(rows), "returned": min(limit, len(rows)), "rounds": rows[-limit:]}


@router.get("/stats")
def stats(request: Request):
    return request.app.state.history_collector.stats()


@router.post("/start")
async def start(request: Request):
    return await request.app.state.history_collector.start()


@router.post("/stop")
async def stop(request: Request):
    if request.app.state.history_collector.external:
        raise HTTPException(status_code=409, detail="collector_is_supervisor_managed")
    return await request.app.state.history_collector.stop()
