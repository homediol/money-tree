from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from app.round_observation_inbox import run_round_observation_inbox

router = APIRouter(prefix="/api/v1", tags=["external round distribution"])
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _require_https_or_loopback(request: Request) -> None:
    peer = request.client.host if request.client else ""
    host = str(request.url.hostname or "").strip("[]").lower()
    loopback_peers = {"127.0.0.1", "::1", "localhost", "testclient"}
    local_request = peer == "testclient" or (peer in loopback_peers and host in loopback_peers)
    if request.url.scheme != "https" and not local_request:
        raise HTTPException(426, detail={"error": "https_required_for_external_access"})


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = set(request.app.state.settings.round_stream_allowed_origins)
    if origin not in allowed:
        raise HTTPException(403, detail={"error": "external_project_origin_not_allowed"})


def _repository(request: Request):
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        raise HTTPException(503, detail={"error": "authoritative_postgresql_unavailable"})
    return repository


async def _authenticate_project(request: Request) -> dict:
    _require_https_or_loopback(request)
    _validate_origin(request)
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"error": "project_token_required"},
                            headers={"WWW-Authenticate": "Bearer"})
    raw_token = authorization[7:].strip()
    if len(raw_token) < 32 or len(raw_token) > 256:
        raise HTTPException(401, detail={"error": "invalid_project_token"})
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    try:
        token = await asyncio.to_thread(_repository(request).external_project_token, digest)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"error": "project_token_store_unavailable"}) from exc
    if not token:
        raise HTTPException(401, detail={"error": "invalid_or_revoked_project_token"})
    allowed, retry_after = request.app.state.round_distribution.allow_request(token["token_id"])
    if not allowed:
        raise HTTPException(429, detail={"error": "project_rate_limit_exceeded"},
                            headers={"Retry-After": str(retry_after)})
    return token


def _require_admin(request: Request) -> None:
    _require_https_or_loopback(request)
    master = request.app.state.settings.api_key
    if not master:
        raise HTTPException(503, detail={"error": "API_KEY_must_be_configured_to_manage_project_tokens"})
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {master}"
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, detail={"error": "admin_api_key_required"},
                            headers={"WWW-Authenticate": "Bearer"})


def _resync_response(snapshot: dict, reason: str) -> dict:
    return {
        "ok": False,
        "error": "resync_required",
        "reason": reason,
        "stream_id": snapshot["stream_id"],
        "retention": {"earliest_sequence": snapshot.get("earliest_sequence"),
                      "latest_sequence": snapshot.get("latest_sequence", 0)},
        "resync_after_sequence": max(0, int(snapshot.get("earliest_sequence") or 1) - 1),
    }


def _check_cursor(snapshot: dict, after_sequence: int, stream_id: str | None) -> None:
    latest = int(snapshot.get("latest_sequence") or 0)
    earliest = snapshot.get("earliest_sequence")
    reason = None
    if after_sequence > 0 and not stream_id:
        reason = "stream_id_required_for_resume"
    elif stream_id and stream_id != snapshot["stream_id"]:
        reason = "stream_id_changed"
    elif after_sequence > latest:
        reason = "cursor_ahead_of_stream"
    elif earliest is not None and after_sequence < int(earliest) - 1:
        reason = "cursor_before_retention"
    if reason:
        raise HTTPException(409, detail=_resync_response(snapshot, reason))


def _sse(event: dict, *, event_id: str | None = None) -> str:
    event_type = str(event.get("event_type") or "message")
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append("data: " + json.dumps(event, sort_keys=True, separators=(",", ":"), default=str))
    return "\n".join(lines) + "\n\n"


async def _send_event(hub, event: dict, stream_id: str) -> str:
    cursor = ""
    if event.get("event_type") == "round.persisted" and event.get("stream_sequence") is not None:
        cursor = f"{stream_id}:{int(event['stream_sequence'])}"
        hub.record_delivery(event)
    return _sse(event, event_id=cursor or None)


@router.post("/projects/{project_id}/tokens")
async def create_project_token(project_id: str, request: Request):
    _require_admin(request)
    if not PROJECT_ID.fullmatch(project_id):
        raise HTTPException(422, detail={"error": "invalid_project_id"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    label = str(body.get("label") or "")[:100] if isinstance(body, dict) else ""
    token = "wpr_" + secrets.token_urlsafe(36)
    token_id = uuid.uuid4().hex
    prefix = token[:12]
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        await asyncio.to_thread(
            _repository(request).create_external_project_token,
            token_id, project_id, label, digest, prefix)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"error": "project_token_create_failed"}) from exc
    return {"ok": True, "token_id": token_id, "project_id": project_id,
            "label": label, "token_prefix": prefix, "token": token,
            "warning": "Store this read-only token now; it will not be returned again."}


@router.delete("/projects/{project_id}/tokens/{token_id}")
async def revoke_project_token(project_id: str, token_id: str, request: Request):
    _require_admin(request)
    if not PROJECT_ID.fullmatch(project_id) or not re.fullmatch(r"[A-Fa-f0-9-]{16,64}", token_id):
        raise HTTPException(422, detail={"error": "invalid_project_or_token_id"})
    try:
        revoked = await asyncio.to_thread(
            _repository(request).revoke_external_project_token, project_id, token_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"error": "project_token_revoke_failed"}) from exc
    if not revoked:
        raise HTTPException(404, detail={"error": "active_project_token_not_found"})
    await request.app.state.round_distribution.revoke_token(token_id)
    return {"ok": True, "token_id": token_id, "project_id": project_id, "revoked": True}


@router.get("/rounds/latest")
async def latest_round(request: Request):
    await _authenticate_project(request)
    try:
        snapshot = await asyncio.to_thread(_repository(request).round_distribution_snapshot)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "round_stream_unavailable"}) from exc
    return {"ok": True, "stream_id": snapshot["stream_id"], "round": snapshot.get("latest"),
            "retention": {"earliest_sequence": snapshot.get("earliest_sequence"),
                          "latest_sequence": snapshot.get("latest_sequence", 0),
                          "retained_events": snapshot.get("retained_events", 0)}}


@router.get("/rounds")
async def committed_rounds(request: Request, after_sequence: int = 0,
                          limit: int = 500, stream_id: str | None = None):
    token = await _authenticate_project(request)
    if after_sequence < 0:
        raise HTTPException(422, detail={"error": "after_sequence_must_be_nonnegative"})
    bounded = min(max(int(limit), 1), 500)
    repository = _repository(request)
    try:
        snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
        _check_cursor(snapshot, after_sequence, stream_id)
        rounds = await asyncio.to_thread(
            repository.round_distribution_events, snapshot["stream_id"], after_sequence,
            int(snapshot["latest_sequence"]), bounded)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"error": "round_stream_unavailable"}) from exc
    next_cursor = int(rounds[-1]["stream_sequence"]) if rounds else after_sequence
    return {"ok": True, "project_id": token["project_id"], "stream_id": snapshot["stream_id"],
            "rounds": rounds, "next_cursor": next_cursor,
            "has_more": next_cursor < int(snapshot["latest_sequence"] or 0),
            "retention": {"earliest_sequence": snapshot.get("earliest_sequence"),
                          "latest_sequence": snapshot.get("latest_sequence", 0),
                          "retained_events": snapshot.get("retained_events", 0)}}


@router.get("/rounds/pending")
async def pending_round_observations(request: Request, observation_id: str | None = None,
                                     after_sequence: int = 0, limit: int = 100):
    """Resume the non-committed stage by stable observation_id."""
    token = await _authenticate_project(request)
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    if inbox is None:
        raise HTTPException(503, detail={"error": "pending_observation_inbox_unavailable"})
    try:
        page = await run_round_observation_inbox(
            request.app, inbox.pending_page, after_sequence=max(0, int(after_sequence)),
            limit=min(max(int(limit), 1), 500), observation_id=observation_id, recovered=True)
        stream_id = request.app.state.round_stream_id
    except Exception as exc:
        raise HTTPException(503, detail={"error": "pending_observation_resume_unavailable"}) from exc
    return {"ok": True, "project_id": token["project_id"], "stream_id": stream_id,
            "observations": page["events"], "next_cursor": page["next_cursor"],
            "has_more": page["has_more"], "latest_pending_sequence": page["latest_sequence"],
            "resume_by": "observation_id_or_pending_sequence",
            "backlog": await run_round_observation_inbox(request.app, inbox.snapshot)}


@router.get("/rounds/clock")
async def round_stream_clock(request: Request):
    """UTC clock sample for receiver-side latency measurements with skew bounds."""
    await _authenticate_project(request)
    return {"server_time": datetime.now(timezone.utc).isoformat(),
            "clock_reference": "API_SYSTEM_UTC"}


def _collector_status_payload(request: Request, stream: dict, metrics: dict) -> dict:
    path = Path(__file__).resolve().parents[3] / "data" / "bot" / "status.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        collector = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        collector = {}
    running = collector.get("collectorRunning")
    if running is False:
        state = "OFFLINE"
    elif collector.get("authRequired") is True or collector.get("loggedIn") is False:
        state = "AUTH_REQUIRED"
    else:
        def age(field):
            value = collector.get(field)
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        heartbeat_age = age("heartbeatAt")
        collection_age = age("lastSuccessfulCollection")
        if (running is not True or heartbeat_age is None or heartbeat_age > 20
                or collection_age is None or collection_age > 120
                or collector.get("loggedIn") is not True
                or collector.get("browserConnected") is not True
                or collector.get("pageConnected") is not True
                or collector.get("frameConnected") is not True):
            state = "STALE" if running is True else "UNKNOWN"
        else:
            state = "HEALTHY"
    latest = stream.get("latest") or {}
    continuity = latest.get("continuity_status", "UNKNOWN")
    commit_samples = collector.get("roundCommitSamples") or []
    values = sorted(float(item["latency_ms"]) for item in commit_samples
                    if isinstance(item, dict)
                    and str(item.get("round_id") or "")
                    and isinstance(item.get("latency_ms"), (int, float))
                    and float(item["latency_ms"]) >= 0)
    if values:
        summary = {
            "samples": len(values),
            "p50_ms": round(values[min(len(values) - 1, max(0, (len(values) - 1) // 2))], 2),
            "p95_ms": round(values[min(len(values) - 1, max(0, math.ceil(len(values) * 0.95) - 1))], 2),
            "max_ms": round(values[-1], 2),
        }
    else:
        summary = {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    metrics = {**metrics, "latency_ms": {**metrics.get("latency_ms", {}),
                                            "observation_to_commit_ms": summary}}
    event_outbox = Path(__file__).resolve().parents[3] / "data" / "bot" / "round-event-outbox.json"
    round_outbox = Path(__file__).resolve().parents[3] / "data" / "bot" / "round-outbox.json"
    def outbox_size(file_path):
        try:
            raw = file_path.read_text(encoding="utf-8")
            if raw.lstrip().startswith("["):
                data = json.loads(raw)
                return len(data) if isinstance(data, list) else None
            records = {}
            for line in raw.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("op") == "append" and entry.get("record", {}).get("round_id") is not None:
                    records[str(entry["record"]["round_id"])] = entry["record"]
                elif entry.get("op") == "append_batch":
                    for record in entry.get("records", []):
                        records[str(record["round_id"])] = record
                elif entry.get("op") == "update":
                    for record in entry.get("records", []):
                        records[str(record["round_id"])] = record
                elif entry.get("op") == "remove":
                    for round_id in entry.get("round_ids", []):
                        records.pop(str(round_id), None)
            return len(records)
        except (OSError, json.JSONDecodeError):
            return None
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    inbox_snapshot = inbox.snapshot() if inbox is not None else {
        "backlog_size": 0, "oldest_pending_observation_age_seconds": None,
        "oldest_pending_observation_received_at": None,
    }
    return {
        "ok": True,
        "collector_status": state,
        "continuity_status": continuity,
        "continuity_gap": continuity == "GAP_BEFORE",
        "continuity_gaps_total": int(stream.get("continuity_gaps") or 0),
        "continuity_unknown_total": int(stream.get("continuity_unknown") or 0),
        "collector": {key: collector.get(key) for key in (
            "state", "health", "heartbeatAt", "lastSuccessfulCollection", "lastRoundId",
            "lastObservedRoundId", "lastRoundIndex", "authRequired", "browserConnected",
            "pageConnected", "frameConnected", "loggedIn")},
        "last_error_category": ((collector.get("lastError") or {}).get("category")
                                if isinstance(collector.get("lastError"), dict) else None),
        "stream": {"stream_id": stream["stream_id"],
                   "retention": {"earliest_sequence": stream.get("earliest_sequence"),
                                 "latest_sequence": stream.get("latest_sequence", 0),
                                 "retained_events": stream.get("retained_events", 0)},
                   "last_published_round": stream.get("latest")},
        "outbox": {"collector_observations_pending": outbox_size(event_outbox),
                   "rounds_waiting_for_postgresql": outbox_size(round_outbox),
                   "api_pending_observations": inbox_snapshot["backlog_size"],
                   "oldest_pending_observation_age_seconds": inbox_snapshot["oldest_pending_observation_age_seconds"],
                   "oldest_pending_observation_received_at": inbox_snapshot["oldest_pending_observation_received_at"]},
        "metrics": {**metrics,
                    "collector_round_delivery": (collector.get("storage", {}).get("round_delivery")
                        if isinstance(collector.get("storage"), dict) else None)},
    }


def _collector_health_state(request: Request) -> str:
    """Read only the collector heartbeat for SSE keepalives.

    The full status payload parses durable outbox files and reads PostgreSQL.
    Those operations are useful for the status endpoint, but must not be on the
    SSE heartbeat path: a slow database must not make a healthy SSE connection
    look dead.
    """
    path = Path(__file__).resolve().parents[3] / "data" / "bot" / "status.json"
    try:
        collector = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(collector, dict):
            collector = {}
    except (OSError, json.JSONDecodeError):
        collector = {}
    running = collector.get("collectorRunning")
    if running is False:
        return "OFFLINE"
    if collector.get("authRequired") is True or collector.get("loggedIn") is False:
        return "AUTH_REQUIRED"

    def age(field: str) -> float | None:
        value = collector.get(field)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None

    heartbeat_age = age("heartbeatAt")
    collection_age = age("lastSuccessfulCollection")
    if (running is not True or heartbeat_age is None or heartbeat_age > 20
            or collection_age is None or collection_age > 120
            or collector.get("loggedIn") is not True
            or collector.get("browserConnected") is not True
            or collector.get("pageConnected") is not True
            or collector.get("frameConnected") is not True):
        return "STALE" if running is True else "UNKNOWN"
    return "HEALTHY"


@router.get("/collector/status")
async def external_collector_status(request: Request):
    await _authenticate_project(request)
    try:
        repository = _repository(request)
        snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
        metrics = await request.app.state.round_distribution.metrics()
        payload = _collector_status_payload(request, snapshot, metrics)
        queue = await asyncio.to_thread(repository.collector_observation_queue_snapshot)
        payload["outbox"]["postgres_observations_waiting_for_round"] = queue.get("depth", 0)
        payload["outbox"]["oldest_observation_age_seconds"] = queue.get("oldest_job_age_seconds")
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"error": "collector_status_unavailable"}) from exc


@router.get("/rounds/stream")
async def subscribe_round_stream(request: Request, after_sequence: int | None = None,
                                 stream_id: str | None = None,
                                 pending_after_sequence: int = 0):
    token = await _authenticate_project(request)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            stream_id, raw_sequence = last_event_id.rsplit(":", 1)
            after_sequence = int(raw_sequence)
        except (ValueError, TypeError):
            raise HTTPException(400, detail={"error": "invalid_last_event_id"})
    if after_sequence is not None and after_sequence < 0:
        raise HTTPException(422, detail={"error": "after_sequence_must_be_nonnegative"})
    repository = _repository(request)
    hub = request.app.state.round_distribution
    try:
        subscriber = await hub.reserve_subscriber(token)
    except Exception as exc:
        if isinstance(exc, PermissionError):
            raise HTTPException(429, detail={"error": str(exc)}) from exc
        raise HTTPException(503, detail={"error": "round_stream_unavailable"}) from exc

    async def stream_body():
        try:
            # Release headers immediately; the subscriber queue protects the
            # replay/live boundary while the committed snapshot is queried.
            yield ": connected\n\n"
            live_stream_id = str(stream_id or getattr(request.app.state, "round_stream_id", ""))
            sent_observation_ids: set[str] = set()
            deferred_live_events: list[dict[str, Any]] = []
            next_prepare_heartbeat_at = time.monotonic() + SSE_HEARTBEAT_INTERVAL_SECONDS

            def prepare_heartbeat_chunk(force: bool = False) -> str | None:
                nonlocal next_prepare_heartbeat_at
                now = time.monotonic()
                if not force and now < next_prepare_heartbeat_at:
                    return None
                next_prepare_heartbeat_at = now + SSE_HEARTBEAT_INTERVAL_SECONDS
                return _sse({"event_type": "heartbeat", "at": datetime.now(timezone.utc).isoformat(),
                             "collector_status": _collector_health_state(request),
                             "continuity_status": "UNKNOWN", "stream_id": live_stream_id or None,
                             "latest_sequence": after_sequence})

            async def send_queued_observation(event: dict[str, Any]) -> str | None:
                observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                if (not observation_id or event.get("event_type") != "round.observed"
                        or str(event.get("stream_id") or "") != live_stream_id):
                    return None
                if observation_id in sent_observation_ids or await hub.was_committed(observation_id):
                    subscriber.pending_event_ids.discard(observation_id)
                    return None
                sent_observation_ids.add(observation_id)
                subscriber.pending_event_ids.discard(observation_id)
                return await _send_event(hub, event, live_stream_id)

            # Do not make fresh observations wait behind the PostgreSQL replay
            # boundary query. Keep the subscriber queue draining while that
            # query runs; committed replay still starts from its stable DB
            # boundary once available.
            replay_task = asyncio.create_task(hub.prepare_replay(
                repository, subscriber, after_sequence, stream_id))
            try:
                while not replay_task.done():
                    queue_task = asyncio.create_task(subscriber.queue.get())
                    done, _ = await asyncio.wait(
                        {replay_task, queue_task}, timeout=max(
                            0.0, next_prepare_heartbeat_at - time.monotonic()),
                        return_when=asyncio.FIRST_COMPLETED)
                    if queue_task in done:
                        queued = queue_task.result()
                        if queued.get("event_type") == "round.observed":
                            chunk = await send_queued_observation(queued)
                            if chunk:
                                yield chunk
                        elif queued.get("event_type") in {"stream.overflow", "server.shutdown", "auth.revoked"}:
                            yield _sse(queued)
                            return
                        else:
                            deferred_live_events.append(queued)
                    else:
                        queue_task.cancel()
                        await asyncio.gather(queue_task, return_exceptions=True)
                        if not done:
                            chunk = prepare_heartbeat_chunk(force=True)
                            if chunk:
                                yield chunk
                    chunk = prepare_heartbeat_chunk()
                    if chunk:
                        yield chunk
                snapshot, boundary, _reconnect_backlog = await replay_task
            except Exception as exc:
                if not replay_task.done():
                    replay_task.cancel()
                from app.round_distribution import RoundStreamResyncRequired
                if isinstance(exc, RoundStreamResyncRequired):
                    yield _sse({"event_type": "stream.error", "detail": exc.detail})
                else:
                    yield _sse({"event_type": "stream.error", "detail": {"error": "round_stream_unavailable"}})
                return
            cursor = int(after_sequence if after_sequence is not None else boundary)
            latest_continuity = str((snapshot.get("latest") or {}).get("continuity_status") or "UNKNOWN")
            live_stream_id = str(snapshot["stream_id"])
            ready = {"event_type": "stream.ready", "stream_id": snapshot["stream_id"],
                     "replay_boundary": boundary, "resume_after_sequence": cursor,
                     "replay_is_bounded": True, "pending_resume": "GET /api/v1/rounds/pending"}
            yield "retry: 3000\n" + _sse(ready)
            next_heartbeat_at = time.monotonic() + SSE_HEARTBEAT_INTERVAL_SECONDS

            def heartbeat_chunk(force: bool = False) -> str | None:
                nonlocal next_heartbeat_at
                now = time.monotonic()
                if not force and now < next_heartbeat_at:
                    return None
                next_heartbeat_at = now + SSE_HEARTBEAT_INTERVAL_SECONDS
                return _sse({"event_type": "heartbeat", "at": datetime.now(timezone.utc).isoformat(),
                             "collector_status": _collector_health_state(request),
                             "continuity_status": latest_continuity,
                             "stream_id": snapshot["stream_id"],
                             "latest_sequence": cursor})

            async def drain_queued_observations():
                while True:
                    try:
                        queued = subscriber.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    if queued.get("event_type") == "round.observed":
                        chunk = await send_queued_observation(queued)
                        if chunk:
                            yield chunk
                    else:
                        deferred_live_events.append(queued)
                    heartbeat = heartbeat_chunk()
                    if heartbeat:
                        yield heartbeat

            # Recovered pending rows go out before a potentially large
            # committed replay. Then live observed events are drained while
            # each database page is fetched, so catch-up history cannot block
            # a connected client from seeing a just-observed round.
            inbox = getattr(request.app.state, "round_observation_inbox", None)
            pending_cursor = max(0, int(pending_after_sequence))
            if inbox is not None:
                while True:
                    pending_task = asyncio.create_task(run_round_observation_inbox(
                        request.app, inbox.pending_page, after_sequence=pending_cursor,
                        limit=500, recovered=True))
                    try:
                        while not pending_task.done():
                            queue_task = asyncio.create_task(subscriber.queue.get())
                            done, _ = await asyncio.wait(
                                {pending_task, queue_task}, timeout=max(
                                    0.0, next_heartbeat_at - time.monotonic()),
                                return_when=asyncio.FIRST_COMPLETED)
                            if queue_task in done:
                                queued = queue_task.result()
                                if queued.get("event_type") == "round.observed":
                                    chunk = await send_queued_observation(queued)
                                    if chunk:
                                        yield chunk
                                else:
                                    deferred_live_events.append(queued)
                            else:
                                queue_task.cancel()
                                await asyncio.gather(queue_task, return_exceptions=True)
                                if not done:
                                    chunk = heartbeat_chunk(force=True)
                                    if chunk:
                                        yield chunk
                            chunk = heartbeat_chunk()
                            if chunk:
                                yield chunk
                        pending_page = await pending_task
                    except BaseException:
                        if not pending_task.done():
                            pending_task.cancel()
                        raise
                    for event in pending_page["events"]:
                        heartbeat = heartbeat_chunk()
                        if heartbeat:
                            yield heartbeat
                        observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                        if not observation_id or observation_id in sent_observation_ids:
                            continue
                        if await hub.was_committed(observation_id):
                            continue
                        subscriber.pending_event_ids.add(observation_id)
                        chunk = await send_queued_observation(event)
                        if chunk:
                            yield chunk
                    pending_cursor = int(pending_page["next_cursor"])
                    if not pending_page["has_more"]:
                        break

            while cursor < boundary:
                page_task = asyncio.create_task(asyncio.to_thread(
                    repository.round_distribution_events, snapshot["stream_id"], cursor, boundary, 500))
                try:
                    while not page_task.done():
                        queue_task = asyncio.create_task(subscriber.queue.get())
                        done, _ = await asyncio.wait(
                            {page_task, queue_task}, timeout=max(
                                0.0, next_heartbeat_at - time.monotonic()),
                            return_when=asyncio.FIRST_COMPLETED)
                        if queue_task in done:
                            queued = queue_task.result()
                            if queued.get("event_type") == "round.observed":
                                chunk = await send_queued_observation(queued)
                                if chunk:
                                    yield chunk
                            else:
                                deferred_live_events.append(queued)
                        else:
                            queue_task.cancel()
                            await asyncio.gather(queue_task, return_exceptions=True)
                            if not done:
                                chunk = heartbeat_chunk(force=True)
                                if chunk:
                                    yield chunk
                        chunk = heartbeat_chunk()
                        if chunk:
                            yield chunk
                    page = await page_task
                except BaseException:
                    if not page_task.done():
                        page_task.cancel()
                    raise
                if not page:
                    break
                for event in page:
                    async for chunk in drain_queued_observations():
                        yield chunk
                    yield await _send_event(hub, event, snapshot["stream_id"])
                    latest_continuity = str(event.get("continuity_status") or latest_continuity)
                    observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                    if observation_id:
                        subscriber.pending_event_ids.add(observation_id)
                    hub.remember_replayed_commit(event)
                    cursor = int(event["stream_sequence"])
                    heartbeat = heartbeat_chunk()
                    if heartbeat:
                        yield heartbeat
                if len(page) < 500:
                    break

            async for chunk in drain_queued_observations():
                yield chunk
            yield _sse({"event_type": "stream.replay_complete", "stream_id": snapshot["stream_id"],
                        "replay_boundary": boundary, "next_cursor": cursor,
                        "pending_next_cursor": pending_cursor if inbox is not None else max(0, int(pending_after_sequence))})
            while not await request.is_disconnected():
                if deferred_live_events:
                    event = deferred_live_events.pop(0)
                else:
                    try:
                        event = await asyncio.wait_for(subscriber.queue.get(), timeout=max(
                            0.0, next_heartbeat_at - time.monotonic()))
                    except asyncio.TimeoutError:
                        chunk = heartbeat_chunk(force=True)
                        if chunk:
                            yield chunk
                        if subscriber.closed:
                            break
                        continue
                # Idle-only heartbeats disappear when round traffic is faster
                # than the timeout. Keep status and liveness fresh on a fixed
                # cadence even while committed events continue to arrive.
                chunk = heartbeat_chunk()
                if chunk:
                    yield chunk
                if event.get("event_type") == "round.persisted":
                    sequence = int(event.get("committed_sequence") or event.get("stream_sequence") or 0)
                    if sequence <= boundary:
                        continue
                    cursor = sequence
                    latest_continuity = str(event.get("continuity_status") or latest_continuity)
                    observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                    if observation_id:
                        subscriber.pending_event_ids.discard(observation_id)
                elif event.get("event_type") == "round.observed":
                    observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                    subscriber.pending_event_ids.discard(observation_id)
                    if (observation_id in sent_observation_ids
                            or await hub.was_committed(observation_id)):
                        continue
                    sent_observation_ids.add(observation_id)
                yield await _send_event(hub, event, snapshot["stream_id"])
                if event.get("event_type") in {"stream.overflow", "server.shutdown", "auth.revoked"}:
                    break
        finally:
            await hub.unsubscribe(subscriber)

    return StreamingResponse(stream_body(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Content-Encoding": "identity",
        "Vary": "Accept-Encoding",
    })


def observed_round_event(observation: dict, stream_id: str) -> dict:
    """Build the prompt, durable pending stage from validated collector evidence."""
    platform_id = observation.get("platform_round_id")
    platform_index = observation.get("platform_round_index")
    identity_source = ("PLATFORM_ROUND_ID" if platform_id is not None else
                       "PLATFORM_ROUND_INDEX" if platform_index is not None else
                       "COLLECTOR_OBSERVATION_HASH"
                       if observation.get("round_identity_type") == "COLLECTOR_OBSERVATION_HASH"
                       else "LOCAL_SEQUENCE" if observation.get("round_index_source") == "LOCAL_SEQUENCE"
                       else "UNKNOWN")
    continuity_status = ("GAP_BEFORE" if observation.get("gap_before") is True else
                         "VERIFIED" if observation.get("continuity_verified") is True else "UNVERIFIED")
    round_id = str(observation["round_id"])
    observation_id = str(observation.get("observation_id") or round_id)
    return {
        "schema_version": "1.0",
        "event_id": f"aviator-round:{round_id}:observed",
        "legacy_event_id": f"aviator-round:{round_id}",
        "event_type": "round.observed",
        "stage": "OBSERVED",
        "stream_id": stream_id,
        "stream_sequence": None,
        "committed_sequence": None,
        "observation_id": observation_id,
        "round_id": round_id,
        "identity_source": identity_source,
        "multiplier": float(observation["multiplier"]),
        "platform_timestamp": observation.get("platform_timestamp"),
        "observed_at": observation.get("observed_at"),
        "stored_at": None,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "durability": "PENDING",
        "continuity_status": continuity_status,
        "delivery_mode": observation.get("delivery_mode") or "LIVE",
        "timing_uncertainty": {"next_round_open": "UNKNOWN"},
        "round_index": int(observation["round_index"]),
        "platform_round_id": str(platform_id) if platform_id is not None else None,
        "platform_round_index": int(platform_index) if platform_index is not None else None,
        "local_appended_at": observation.get("local_appended_at"),
        "collector_http_started_at": observation.get("collector_http_started_at"),
        "api_received_at": observation.get("api_received_at"),
        "api_receipt_to_sse_publication_ms": observation.get("api_receipt_to_sse_publication_ms"),
        "game_phase": observation.get("game_phase"),
    }
