from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import hmac
import ipaddress
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import history, health, shadow
from app.api import stability as stability_api
from app.api import round_distribution as round_distribution_api
from app.api import rules_engine as rules_engine_api
from app.api import signals as signals_api
from app.api import developer_console as developer_console_api
from app.api import adie as adie_api
from app.adie_engine import ADIEEngine
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.runtime import InstanceLock
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine
from app.services.system_health import SystemHealth
from app.services.system_orchestrator import SystemOrchestrator
from app.services.shadow_signal_bridge import ShadowSignalBridge
from app.services.request_admission import ApiRequestAdmission
from app.shadow import ShadowManager
from app.history_collector import HistoryCollectorManager
from app.services.process_lifecycle import ProcessLifecycle
from app.round_distribution import RoundDistributionHub
from app.api.round_distribution import observed_round_event
from app.round_observation_inbox import (
    ObservationIdentityConflict, ObservationInboxCapacityError, RoundObservationInbox,
    run_round_observation_inbox,
)
from app.rules_engine import RULE_PERFORMANCE_CHECKPOINT_ID, RulesEngine

configure_logging()
log = get_logger("APP")


async def _forward_live_round_to_behaviour(app: FastAPI, event: dict) -> bool:
    """Deliver one durable hub event to Behaviour's idempotent local inbox."""
    if (event.get("event_type") != "round.observed"
            or event.get("durability") != "PENDING"
            or event.get("continuity_status") not in {"VERIFIED", "GAP_BEFORE"}
            or str(event.get("delivery_mode") or "").upper() not in {"LIVE", "RECOVERED"}
            or str(event.get("round_uid") or event.get("round_id") or "")
                != str(event.get("round_id") or "")):
        return True
    started = time.perf_counter()
    base_url = os.getenv("BEHAVIOUR_API_URL", "http://127.0.0.1:8100").rstrip("/")
    boundary = getattr(app.state, "behaviour_live_boundary_at", None)
    behaviour_provenance, recovery_reason = _behaviour_event_provenance(event, boundary)
    event = {**event, "behaviour_provenance": behaviour_provenance,
             "behaviour_recovery_reason": recovery_reason}
    request = urllib.request.Request(
        base_url + "/internal/round-observed",
        data=json.dumps(event, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with await asyncio.to_thread(urllib.request.urlopen, request, timeout=2.5) as response:
            result = json.loads(response.read().decode("utf-8"))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.info("[BEHAVIOUR_API_ROUND] round_id=%s status=%s inbox_status=%s promoted=%s elapsed_ms=%.1f",
                 event.get("round_id"), result.get("status"), result.get("inbox_status"),
                 result.get("promoted"), elapsed_ms)
        return bool(result.get("ok")) and result.get("status") in {"STAGED", "DUPLICATE"}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": "behaviour_api_http_error"}
        log.info("[BEHAVIOUR_API_ROUND] rejected round_id=%s status=%s detail=%s elapsed_ms=%.1f",
                 event.get("round_id"), exc.code, detail.get("detail", detail),
                 (time.perf_counter() - started) * 1000.0)
        return False
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.info("[BEHAVIOUR_API_ROUND] deferred round_id=%s error=%s elapsed_ms=%.1f",
                 event.get("round_id"), type(exc).__name__, elapsed_ms)
        return False


def _trusted_live_round_after_boundary(event: dict, boundary: str | None) -> bool:
    """Require source-side Orbit timing as well as post-boundary journal receipt.

    A round can be delivered to the backend after startup while still being an
    old outbox replay. The collector's source reading and receipt timestamps
    distinguish that case from a fresh authenticated SSE observation.
    """
    from datetime import datetime, timezone

    metadata = event.get("source_metadata")
    if (not isinstance(metadata, dict)
            or metadata.get("source") != "ORBIT_DATA_WATCH_API"
            or str(event.get("delivery_mode") or "").upper() != "LIVE"):
        return False

    def parse(value):
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None

    boundary_at = parse(boundary)
    journal_at = parse(event.get("journal_received_at"))
    collector_at = parse(metadata.get("collector_received_at"))
    reading_at = parse(metadata.get("reading_checked_at"))
    return bool(boundary_at and journal_at and collector_at and reading_at
                and journal_at >= boundary_at and collector_at >= boundary_at
                and reading_at >= boundary_at)


def _behaviour_event_provenance(event: dict, boundary: str | None) -> tuple[str, str | None]:
    """Only the newest verified event in the durable inbox can create future intent."""
    try:
        behind_latest = (int(event.get("latest_verified_round_index") or 0)
                         > int(event.get("round_index") or 0))
    except (TypeError, ValueError, OverflowError):
        behind_latest = True
    live = (not behind_latest
        and event.get("continuity_status") == "VERIFIED"
        and str(event.get("delivery_mode") or "").upper() == "LIVE"
        and _trusted_live_round_after_boundary(event, boundary))
    if live:
        return "LIVE_VERIFIED", None
    return "RECOVERY_REPLAY", ("BACKLOG_AHEAD_OF_DECISION" if behind_latest else
                                "LIVE_BOUNDARY_OR_PROVENANCE_UNVERIFIED")


async def _run_behaviour_round_consumer(app: FastAPI) -> None:
    """Replay the durable verified-round inbox to Behaviour independently of Rules/PG."""
    inbox = getattr(app.state, "round_observation_inbox", None)
    if inbox is None:
        return
    wakeup = app.state.behaviour_round_wakeup
    while True:
        stream_id = str(getattr(app.state, "round_stream_id", "") or "") or None
        pending = await asyncio.to_thread(inbox.pending_behaviour_events, limit=1,
                                          stream_id=stream_id)
        if not pending:
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            wakeup.clear()
            continue
        observation_id, event = pending[0]
        try:
            delivery_started = time.perf_counter()
            delivered = await _forward_live_round_to_behaviour(app, event)
            if delivered:
                await asyncio.to_thread(inbox.mark_behaviour_processed, observation_id)
                app.state.behaviour_round_last_delivered_at = datetime.now(timezone.utc).isoformat()
                app.state.behaviour_round_last_delivered_id = str(event.get("round_uid") or event.get("round_id"))
                app.state.behaviour_round_last_delivery_ms = round(
                    (time.perf_counter() - delivery_started) * 1000.0, 3)
                app.state.behaviour_round_last_error = None
                continue
            attempts = await asyncio.to_thread(inbox.defer_behaviour_event, observation_id,
                                               "behaviour_inbox_not_acknowledged")
            app.state.behaviour_round_last_error = "behaviour_inbox_not_acknowledged"
            log.warning("[BEHAVIOUR_HUB_DELIVERY] deferred round_uid=%s attempts=%s",
                        event.get("round_uid") or event.get("round_id"), attempts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts = await asyncio.to_thread(inbox.defer_behaviour_event, observation_id,
                                               type(exc).__name__)
            app.state.behaviour_round_last_error = type(exc).__name__
            log.exception("[BEHAVIOUR_HUB_DELIVERY] consumer failed round_uid=%s attempts=%s",
                          event.get("round_uid") or event.get("round_id"), attempts)
        # Preserve per-round ordering: don't pass a failed event until its retry is due.
        await asyncio.sleep(0)


async def _reconcile_behaviour_round_checkpoint(app: FastAPI) -> int:
    """Skip only inbox events covered by Behaviour's own durable contiguous checkpoint."""
    inbox = getattr(app.state, "round_observation_inbox", None)
    stream_id = str(getattr(app.state, "round_stream_id", "") or "")
    if inbox is None or not stream_id:
        return 0
    base_url = os.getenv("BEHAVIOUR_API_URL", "http://127.0.0.1:8100").rstrip("/")
    url = base_url + "/internal/round-checkpoint?stream_id=" + urllib.parse.quote(stream_id, safe="")

    def fetch_checkpoint():
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        checkpoint = await asyncio.to_thread(fetch_checkpoint)
        if checkpoint.get("stream_id") != stream_id or not checkpoint.get("round_id"):
            return 0
        reconciled = await asyncio.to_thread(inbox.reconcile_behaviour_checkpoint,
            stream_id=stream_id, round_id=str(checkpoint["round_id"]),
            round_index=int(checkpoint["round_index"]))
        if reconciled:
            log.info("[BEHAVIOUR_HUB_REPLAY] reconciled prior inbox events=%s checkpoint=%s/%s",
                     reconciled, checkpoint["round_index"], checkpoint["round_id"])
        return reconciled
    except Exception as exc:
        # No checkpoint means replay remains pending and idempotent; never
        # discard retained events just because Behaviour is temporarily offline.
        log.info("[BEHAVIOUR_HUB_REPLAY] prior checkpoint unavailable error=%s", type(exc).__name__)
        return 0


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def broadcast(self, payload: dict):
        stale = []
        # Connections may join or leave while send_json yields to the loop.
        for ws in tuple(self.active):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


manager = ConnectionManager()
# The durable SQLite/PostgreSQL handoffs retain work when this in-memory queue
# fills. Keep the volatile queue deliberately small so overload is surfaced as
# backpressure instead of hiding minutes of signal delay in RAM.
ROUND_EVENT_QUEUE_CAPACITY = 512
ROUND_COMMITTED_BRIDGE_QUEUE_CAPACITY = ROUND_EVENT_QUEUE_CAPACITY * 4
RULES_DASHBOARD_NOTIFICATION_QUEUE_CAPACITY = 256
RULES_STATE_PERSISTENCE_QUEUE_CAPACITY = 512
ROUND_HISTORY_ANALYTICS_MIN_INTERVAL_SECONDS = 15.0
# The collector keeps an observed round in its fsynced outbox until this
# priority decision handoff succeeds. Leave headroom under its default 15s
# request deadline; a timeout is a retryable 503, never an early acknowledgement.
ROUND_OBSERVED_DECISION_WAIT_SECONDS = 12.0

PUBLIC_API_READS = {
    ("GET", "/api/v1/rounds"),
    ("GET", "/api/v1/rounds/latest"),
    ("GET", "/api/v1/signals/latest/action"),
}
MAX_API_BODY_BYTES = 1_048_576
LOCAL_BROWSER_SESSION_COOKIE = "winner_predict_local_owner_session"
LOCAL_BROWSER_SESSION_TTL_SECONDS = 12 * 60 * 60
LOCAL_BROWSER_SESSION_PATH = "/api/auth/local-browser-session"
SCOPED_BEARER_ROUTES = {
    ("GET", "/api/v1/rounds/pending"),
    ("GET", "/api/v1/rounds/clock"),
    ("GET", "/api/v1/collector/status"),
    ("GET", "/api/v1/rounds/stream"),
    ("GET", "/api/v1/signals/latest"),
    ("GET", "/api/v1/signals"),
    ("GET", "/api/v1/rules/selected"),
    ("GET", "/api/v1/rules/matched"),
    ("GET", "/api/v1/decisions/latest"),
    ("GET", "/api/v1/metrics"),
    ("GET", "/api/v1/stream/signals"),
    ("GET", "/api/v1/console/signals"),
    ("POST", "/api/v1/signals"),
    ("POST", "/api/v1/publisher/heartbeat"),
    ("POST", "/api/v1/ws/ticket"),
}


def _uses_scoped_bearer(method: str, path: str) -> bool:
    return ((method.upper(), path) in SCOPED_BEARER_ROUTES
            or (method.upper() == "GET" and path.startswith("/api/v1/signals/")))


def _is_loopback_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    host = str(request.url.hostname or "").strip("[]").lower()
    loopback = {"127.0.0.1", "::1", "localhost"}
    test_hosts = {"test", "testserver"}
    if peer == "testclient":
        return True
    if peer not in loopback:
        return False
    headers = getattr(request, "headers", {})
    if "x-winner-proxy-client" in headers:
        forwarded = str(headers.get("x-winner-proxy-client") or "").strip()
        try:
            client_ip = ipaddress.ip_address(forwarded)
            if isinstance(client_ip, ipaddress.IPv6Address) and client_ip.ipv4_mapped:
                client_ip = client_ip.ipv4_mapped
            return client_ip.is_loopback
        except ValueError:
            return False
    return host in (loopback | test_hosts)


def _is_loopback_origin(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
        return (parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                and not parsed.path and not parsed.params and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _local_browser_session_value(expires_at: int, nonce: str, origin: str) -> str:
    origin_signature = hmac.new(
        settings.api_key.encode("utf-8"),
        f"winner-predict-local-browser-origin-v1:{origin}".encode("utf-8"),
        "sha256",
    ).hexdigest()
    message = f"winner-predict-local-browser-v1:{expires_at}:{nonce}:{origin_signature}".encode("utf-8")
    signature = hmac.new(settings.api_key.encode("utf-8"), message, "sha256").hexdigest()
    return f"{expires_at}.{nonce}.{origin_signature}.{signature}"


def _has_local_browser_session(request: Request | WebSocket) -> bool:
    if not settings.api_key or not _is_loopback_request(request):
        return False
    raw = request.cookies.get(LOCAL_BROWSER_SESSION_COOKIE, "")
    parts = raw.split(".")
    if len(parts) != 4:
        return False
    expires_raw, nonce, origin_signature, supplied_signature = parts
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce.lower()):
        return False
    if (len(origin_signature) != 64 or len(supplied_signature) != 64
            or any(char not in "0123456789abcdef" for char in origin_signature + supplied_signature)):
        return False
    try:
        expires_at = int(expires_raw)
        now = int(time.time())
    except (TypeError, ValueError, OverflowError):
        return False
    if expires_at <= now or expires_at > now + LOCAL_BROWSER_SESSION_TTL_SECONDS + 60:
        return False
    message = f"winner-predict-local-browser-v1:{expires_at}:{nonce}:{origin_signature}".encode("utf-8")
    expected_signature = hmac.new(settings.api_key.encode("utf-8"), message, "sha256").hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return False
    request_origin = request.headers.get("origin")
    if request_origin:
        expected_origin_signature = hmac.new(
            settings.api_key.encode("utf-8"),
            f"winner-predict-local-browser-origin-v1:{request_origin}".encode("utf-8"),
            "sha256",
        ).hexdigest()
        return hmac.compare_digest(origin_signature, expected_origin_signature)
    return True




def _round_event_queue(app: FastAPI):
    queue = getattr(app.state, "round_event_queue", None)
    if queue is None:
        queue = asyncio.PriorityQueue(maxsize=ROUND_EVENT_QUEUE_CAPACITY)
        app.state.round_event_queue = queue
    queued_ids = getattr(app.state, "round_event_queued_ids", None)
    if queued_ids is None:
        queued_ids = set()
        app.state.round_event_queued_ids = queued_ids
    if not hasattr(app.state, "round_event_processor_busy"):
        app.state.round_event_processor_busy = False
    if not hasattr(app.state, "round_event_enqueue_sequence"):
        app.state.round_event_enqueue_sequence = 0
    if not hasattr(app.state, "round_event_retry_ids"):
        app.state.round_event_retry_ids = set()
    if not hasattr(app.state, "round_event_retry_attempts"):
        app.state.round_event_retry_attempts = {}
    if not hasattr(app.state, "round_event_completion_waiters"):
        app.state.round_event_completion_waiters = {}
    task = getattr(app.state, "round_event_processor_task", None)
    if task is None or task.done():
        app.state.round_event_processor_task = asyncio.create_task(
            _run_round_event_processor(app), name="round-event-processor")
    return queue, queued_ids


def _round_event_key(stage: str, event: dict) -> str | None:
    identity = str(event.get("event_id") or event.get("observation_id")
                   or event.get("round_id") or "")
    return f"{stage}:{identity}" if identity else None


def _round_event_completion_future(app: FastAPI, stage: str,
                                   event: dict) -> tuple[str, asyncio.Future] | None:
    key = _round_event_key(stage, event)
    if key is None:
        return None
    _round_event_queue(app)
    waiters = app.state.round_event_completion_waiters
    future = waiters.get(key)
    if future is None or future.cancelled():
        future = asyncio.get_running_loop().create_future()
        waiters[key] = future
    return key, future


def _forget_round_event_completion(app: FastAPI, key: str) -> None:
    waiters = getattr(app.state, "round_event_completion_waiters", {})
    waiters.pop(key, None)


def _rules_state_persistence_queue(app: FastAPI):
    queue = getattr(app.state, "rules_state_persistence_queue", None)
    if queue is None:
        queue = asyncio.Queue(maxsize=RULES_STATE_PERSISTENCE_QUEUE_CAPACITY)
        app.state.rules_state_persistence_queue = queue
    task = getattr(app.state, "rules_state_persistence_task", None)
    if task is None or task.done():
        app.state.rules_state_persistence_task = asyncio.create_task(
            _run_rules_state_persistence_worker(app), name="rules-state-persistence-worker")
    return queue


def _committed_bridge_queue(app: FastAPI):
    """Keep committed settlement work off the latency-sensitive round worker."""
    queue = getattr(app.state, "committed_bridge_queue", None)
    if queue is None:
        queue = asyncio.Queue(maxsize=ROUND_COMMITTED_BRIDGE_QUEUE_CAPACITY)
        app.state.committed_bridge_queue = queue
    task = getattr(app.state, "committed_bridge_task", None)
    if task is None or task.done():
        app.state.committed_bridge_task = asyncio.create_task(
            _run_committed_bridge_processor(app), name="committed-shadow-bridge-worker")
    return queue


def _remember_collector_detection_timestamps(app: FastAPI, events: list[dict], *,
                                             peer_is_loopback: bool) -> None:
    """Retain local collector detection epochs for post-commit end-to-end timing."""
    if not peer_is_loopback:
        return
    timestamps = getattr(app.state, "collector_detection_timestamps", None)
    if timestamps is None:
        timestamps = {}
        app.state.collector_detection_timestamps = timestamps
    for event in events:
        round_id = str(event.get("round_id") or "")
        source_metadata = event.get("source_metadata")
        if isinstance(source_metadata, Mapping) and source_metadata.get("source") == "ORBIT_DATA_WATCH_API":
            # Orbit checkedAt is a page-check timestamp, not a verified
            # collector detection timestamp. Do not manufacture an E2E sample.
            receipt = source_metadata.get("collector_received_at")
            try:
                parsed_receipt = datetime.fromisoformat(str(receipt).replace("Z", "+00:00"))
                if round_id and parsed_receipt.tzinfo is not None:
                    receipts = getattr(app.state, "collector_receipt_timestamps", None)
                    if receipts is None:
                        receipts = {}
                        app.state.collector_receipt_timestamps = receipts
                    receipts.setdefault(round_id, receipt)
                    while len(receipts) > ROUND_EVENT_QUEUE_CAPACITY:
                        receipts.pop(next(iter(receipts)))
            except (ValueError, TypeError, OverflowError):
                pass
            continue
        observed_at = event.get("observed_at")
        if not round_id or not isinstance(observed_at, str):
            continue
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        # The Playwright page observer and Node collector run on this host;
        # the collector sends observations to this API over loopback. Keep this
        # cross-process wall-clock span only for that verified same-host path.
        timestamps.setdefault(round_id, observed_at)
        while len(timestamps) > ROUND_EVENT_QUEUE_CAPACITY:
            timestamps.pop(next(iter(timestamps)))


def _enqueue_rules_dashboard_notification(app: FastAPI, payload: dict) -> bool:
    """Keep slow WebSocket dashboards off the ordered round-processing worker."""
    queue = getattr(app.state, "rules_dashboard_notification_queue", None)
    if queue is None:
        return False
    try:
        queue.put_nowait(payload)
        return True
    except asyncio.QueueFull:
        # Evaluations and signals are already durable. WebSocket messages are
        # refresh hints; retain the newest view and let the dashboard/API read
        # the full persisted history if intermediate hints are coalesced.
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            pass
        queue.put_nowait(payload)
        app.state.rules_dashboard_notifications_coalesced = int(
            getattr(app.state, "rules_dashboard_notifications_coalesced", 0)) + 1
        return True


async def _run_rules_dashboard_notification_worker(app: FastAPI) -> None:
    queue = app.state.rules_dashboard_notification_queue
    while True:
        payload = await queue.get()
        try:
            notifier = getattr(app.state, "manager", manager)
            await notifier.broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Rules Engine dashboard notification failed")
        finally:
            queue.task_done()


async def _monitor_backend_event_loop(app: FastAPI, interval_seconds: float = 0.1) -> None:
    """Sample actual event-loop scheduling lag with the backend monotonic clock."""
    interval = max(0.02, float(interval_seconds))
    sample = 0
    while True:
        deadline = time.perf_counter() + interval
        await asyncio.sleep(interval)
        now = time.perf_counter()
        sample += 1
        hub = getattr(app.state, "round_distribution", None)
        if hub is not None:
            hub.record_priority_latency(
                "backend_event_loop_lag_ms", f"event-loop:{sample}",
                max(0.0, (now - deadline) * 1000.0))


def _enqueue_round_events(app: FastAPI, stage: str, events: list[dict], *,
                          request_started_ns: int | None = None,
                          validation_finished_ns: int | None = None) -> bool:
    """Queue ordered rule work after its durable round handoff has succeeded."""
    queue, queued_ids = _round_event_queue(app)
    pending = []
    retry_ids = getattr(app.state, "round_event_retry_ids", set())
    seen = set(queued_ids) | set(retry_ids)
    for event in events:
        key = _round_event_key(stage, event)
        if key is None:
            continue
        if key not in seen:
            pending.append((key, dict(event)))
            seen.add(key)
    if len(pending) > queue.maxsize - queue.qsize():
        return False
    enqueued_ns = time.perf_counter_ns()
    for key, event in pending:
        sequence = int(app.state.round_event_enqueue_sequence)
        app.state.round_event_enqueue_sequence = sequence + 1
        # Round observations are processed FIFO ahead of commit confirmations.
        # Policy transitions still run on one worker and observed events never
        # overtake one another; commit confirmations only upgrade durable
        # evidence for a round already evaluated on the ordered observed path.
        priority = 0 if stage == "observed" else 1
        queue.put_nowait((priority, sequence, stage, event, key, enqueued_ns,
                          request_started_ns, validation_finished_ns))
        queued_ids.add(key)
    return True


async def _recover_pending_observed_rounds(app: FastAPI) -> int:
    """Replay durable observed-stage work before the API accepts live rounds.

    Inbox reads are paged and the worker queue is bounded. Backpressure waits
    for the existing ordered worker instead of dropping accepted rounds or
    creating an unbounded in-memory recovery list.
    """
    inbox = getattr(app.state, "round_observation_inbox", None)
    if inbox is None:
        return 0
    cursor = 0
    recovered = 0
    while True:
        page = await run_round_observation_inbox(
            app, inbox.pending_rule_events_page, after_sequence=cursor,
            limit=500, priority=True)
        events = page.get("events") or []
        if not events:
            break
        while not _enqueue_round_events(app, "observed", events):
            await asyncio.sleep(0.01)
        recovered += len(events)
        next_cursor = int(page.get("next_cursor") or cursor)
        if next_cursor <= cursor:
            raise RuntimeError("round observation recovery cursor did not advance")
        cursor = next_cursor
        if not page.get("has_more"):
            break
    if recovered:
        log.warning("[ROUND_EVENT_RECOVERY] queued durable observed rounds=%s", recovered)
    committed_cursor = 0
    recovered_committed = 0
    while True:
        page = await run_round_observation_inbox(
            app, inbox.pending_committed_rule_events_page, after_sequence=committed_cursor,
            limit=500, priority=True)
        events = page.get("events") or []
        if not events:
            break
        while not _enqueue_round_events(app, "committed", events):
            await asyncio.sleep(0.01)
        recovered_committed += len(events)
        next_cursor = int(page.get("next_cursor") or committed_cursor)
        if next_cursor <= committed_cursor:
            raise RuntimeError("committed round recovery cursor did not advance")
        committed_cursor = next_cursor
        if not page.get("has_more"):
            break
    if recovered_committed:
        log.warning("[ROUND_EVENT_RECOVERY] queued durable committed rounds=%s", recovered_committed)
    return recovered + recovered_committed


def _target_already_completed(event: dict, marker: tuple | None) -> bool:
    if (str(event.get("delivery_mode") or "LIVE").upper() != "LIVE"
            or event.get("continuity_status") != "VERIFIED" or marker is None):
        return True
    try:
        return int(marker[0]) >= int(event.get("round_index") or 0) + 1
    except (TypeError, ValueError, OverflowError):
        return True


def _has_round_event_consumers(engine: Any, stage: str) -> bool:
    """Include the report-only observer for commits without changing engine adapters."""
    has_consumers = bool(engine.has_round_event_consumers())
    return has_consumers or (
        stage == "committed"
        and getattr(engine, "_performance_observer_enabled", False) is True
    )


async def _process_round_event(app: FastAPI, stage: str, event: dict,
                               enqueued_ns: int, request_started_ns: int | None,
                               validation_finished_ns: int | None,
                               worker_started_ns: int | None = None) -> tuple[bool, set[str], dict | None]:
    engine = getattr(app.state, "rules_engine", None)
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if engine is None or repository is None:
        raise RuntimeError("round event processor dependencies are unavailable")
    # ADIE consumes the same durable, ordered observed stream as SHADOW rules.
    # Its isolated worker and error boundary ensure a research failure cannot
    # change the existing Rules Engine decision or its collector handoff.
    if stage in {"observed", "committed"}:
        adie = getattr(app.state, "adie_engine", None)
        if adie is not None and adie.ready:
            try:
                executor = getattr(app.state, "adie_executor", None)
                adie_event = dict(event)

                def process_adie_event():
                    # A queued ADIE operation may run after a later round has
                    # already settled. Preserve the context record, but never
                    # allow a late worker to create a backdated forecast.
                    source_index = adie_event.get("round_index")
                    marker = getattr(app.state, "latest_committed_round_marker", None)
                    try:
                        adie_event["adie_target_already_completed"] = (
                            marker is not None and int(marker[0]) >= int(source_index) + 1)
                    except (TypeError, ValueError, OverflowError):
                        adie_event["adie_target_already_completed"] = True
                    started = time.perf_counter_ns()
                    result = adie.process_observed(adie_event, recovery=(stage == "committed"))
                    return result, (time.perf_counter_ns() - started) / 1_000_000.0

                loop = asyncio.get_running_loop()
                future = (loop.run_in_executor(executor, process_adie_event)
                          if executor is not None else asyncio.to_thread(process_adie_event))

                def report_adie_result(done):
                    try:
                        result, duration_ms = done.result()
                        selected = result.get("selected") if isinstance(result, dict) else None
                        if selected:
                            log.info("[ADIE_FORECAST_LOCKED] forecast_id=%s round_id=%s target=%s..%s status=%s",
                                     selected["forecast_id"], event.get("round_id"),
                                     selected["target_start_position"],
                                     selected["target_end_position"],
                                     selected["selection_status"])
                        if duration_ms >= 1000:
                            log.warning("[ADIE_ROUND_PROCESSING] stage=%s round_id=%s duration_ms=%.1f late_target=%s",
                                        stage, event.get("round_id"), duration_ms,
                                        bool(adie_event.get("adie_target_already_completed")))
                    except Exception:
                        log.exception("[ADIE] observed event processing failed; existing SHADOW path continues round_id=%s",
                                      event.get("round_id"))

                asyncio.ensure_future(future).add_done_callback(report_adie_result)
            except Exception:
                log.exception("[ADIE] observed event processing failed; existing SHADOW path continues round_id=%s",
                              event.get("round_id"))
    if stage == "observed":
        if event.get("continuity_status") not in {"VERIFIED", "GAP_BEFORE"}:
            return False, set(), None
        rules_executor = getattr(app.state, "round_processing_executor", None)
        if rules_executor is None:
            has_consumers = await asyncio.to_thread(_has_round_event_consumers,
                                                   engine, stage)
        else:
            has_consumers = await asyncio.get_running_loop().run_in_executor(
                rules_executor, partial(_has_round_event_consumers, engine, stage))
        if not has_consumers:
            return False, set(), None
    elif stage == "committed":
        rules_executor = getattr(app.state, "round_processing_executor", None)
        if rules_executor is None:
            has_consumers = await asyncio.to_thread(_has_round_event_consumers,
                                                   engine, stage)
        else:
            has_consumers = await asyncio.get_running_loop().run_in_executor(
                rules_executor, partial(_has_round_event_consumers, engine, stage))
        if not has_consumers:
            return False, set(), None
    correlation_id = str(event.get("correlation_id") or event.get("round_id") or "")
    event_stage_id = str(event.get("event_type") or stage)
    collector_detection_at = getattr(app.state, "collector_detection_timestamps", {}).get(
        correlation_id)
    collector_receipt_at = getattr(app.state, "collector_receipt_timestamps", {}).get(correlation_id)
    worker_started_ns = (time.perf_counter_ns() if worker_started_ns is None
                         else worker_started_ns)
    marker_started_ns = time.perf_counter_ns()
    priority_executor = getattr(app.state, "signal_priority_executor", None)
    metadata = event.get("source_metadata")
    watch_observation = (stage == "observed" and isinstance(metadata, dict)
                         and metadata.get("source") == "ORBIT_DATA_WATCH_API")
    if watch_observation:
        try:
            observed = datetime.fromisoformat(str(event.get("observed_at") or "").replace("Z", "+00:00"))
            received = datetime.fromisoformat(str(event.get("api_received_at") or "").replace("Z", "+00:00"))
            if observed.tzinfo is None or received.tzinfo is None:
                raise ValueError("missing timezone")
            # Judge freshness when the API accepted the observation, not when
            # this queued worker eventually evaluates it. A newer event may
            # arrive while this one waits; that must not retroactively make a
            # valid, ordered LIVE observation stale.
            age = (received.astimezone(timezone.utc)
                   - observed.astimezone(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            age = float("inf")
        target_already_completed = (event.get("delivery_mode") != "LIVE"
            or event.get("continuity_status") != "VERIFIED" or not -2 <= age <= 15)
    elif stage == "committed":
        # A committed round has already been durably written to PostgreSQL.
        # Use the marker initialized at startup and advanced by the collector's
        # commit transaction instead of making the ordered worker wait on a
        # second PostgreSQL read. During DB stalls that redundant read used to
        # block the only ordered worker and make live observed requests time out.
        marker = getattr(app.state, "latest_committed_round_marker", None)
        if marker is None:
            marker = (await asyncio.to_thread(repository.latest_round_marker)
                      if priority_executor is None else
                      await asyncio.get_running_loop().run_in_executor(
                          priority_executor, repository.latest_round_marker))
        target_already_completed = _target_already_completed(event, marker)
    elif priority_executor is None:
        marker = await asyncio.to_thread(repository.latest_round_marker)
        target_already_completed = _target_already_completed(event, marker)
    else:
        marker = await asyncio.get_running_loop().run_in_executor(
            priority_executor, repository.latest_round_marker)
        target_already_completed = _target_already_completed(event, marker)
    marker_finished_ns = time.perf_counter_ns()
    received_ns = time.perf_counter_ns()
    received_at = datetime.now(timezone.utc).isoformat()
    rules_executor = getattr(app.state, "round_processing_executor", None)
    performance_observer_deferred = (
        stage == "committed"
        and getattr(engine, "_performance_observer_enabled", False) is True
        and callable(getattr(engine, "observe_rule_performance_round", None))
    )
    observe_kwargs = {
        "target_already_completed": target_already_completed,
        "defer_persistence": True,
    }
    if performance_observer_deferred:
        observe_kwargs["defer_performance_observer"] = True
    if rules_executor is None:
        evaluation = await asyncio.to_thread(
            engine.observe_round, event, **observe_kwargs)
    else:
        evaluation = await asyncio.get_running_loop().run_in_executor(
            rules_executor, partial(engine.observe_round, event, **observe_kwargs))
    persistence_session_ids = set(evaluation.pop("_deferred_persistence_session_ids", []))
    completed_ns = time.perf_counter_ns()
    hub = getattr(app.state, "round_distribution", None)
    if hub is not None:
        hub.record_priority_latency("backend_marker_lookup_ms",
                                    f"{correlation_id}:{event_stage_id}:backend_marker_lookup_ms",
                                    (marker_finished_ns - marker_started_ns) / 1_000_000.0)
        # Queue wait ends when the ordered worker dequeues the event. Marker
        # lookup has its own metric and must not inflate the queue percentile.
        queue_wait_ms = max(0.0, (worker_started_ns - enqueued_ns) / 1_000_000.0)
        evaluation_ms = max(0.0, (completed_ns - received_ns) / 1_000_000.0)
        hub.record_priority_latency("round_event_processor_queue_wait_ms",
                                    f"{correlation_id}:{event_stage_id}:round_event_processor_queue_wait_ms",
                                    queue_wait_ms)
        hub.record_priority_latency("rules_engine_evaluation_ms",
                                    f"{correlation_id}:{event_stage_id}:rules_engine_evaluation_ms",
                                    evaluation_ms)
        if request_started_ns is not None:
            hub.record_priority_latency("backend_receive_to_decision_ready_ms",
                f"{correlation_id}:backend_receive_to_decision_ready_ms",
                max(0.0, (completed_ns - request_started_ns) / 1_000_000.0))
        if request_started_ns is not None:
            hub.record_priority_latency("api_request_to_rules_engine_receive_ms",
                                        f"{correlation_id}:{event_stage_id}:api_request_to_rules_engine_receive_ms",
                                        max(0.0, (received_ns - request_started_ns) / 1_000_000.0))
        if validation_finished_ns is not None:
            hub.record_priority_latency("api_validation_to_rules_engine_receive_ms",
                                        f"{correlation_id}:{event_stage_id}:api_validation_to_rules_engine_receive_ms",
                                        max(0.0, (received_ns - validation_finished_ns) / 1_000_000.0))
        timings = evaluation.get("event_timings_ms") or {}
        for field in ("target_settlement_processing_ms", "target_settlement_durable_ms"):
            value = timings.get(field)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                hub.record_priority_latency(field,
                                            f"{correlation_id}:{event_stage_id}:{field}",
                                            float(value))
        for field, value in timings.items():
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
                hub.record_priority_latency(field, f"{correlation_id}:{event_stage_id}:{field}", float(value))

    bridge = getattr(app.state, "shadow_signal_bridge", None)
    observed_signal_accepted = False
    committed_bridge_job = None
    if stage == "observed":
        if bridge is not None:
            bridge_result = await bridge.process_observed_round(
                app, event, evaluation,
                decision_started_ns=completed_ns,
                backend_received_ns=request_started_ns,
                collector_detection_at=collector_detection_at, collector_receipt_at=collector_receipt_at)
            observed_signal_accepted = int(bridge_result.get("published") or 0) > 0
    else:
        if bridge is not None:
            bridge_sessions = []
            for session_record in evaluation.get("sessions", []) if isinstance(evaluation, Mapping) else []:
                if not isinstance(session_record, Mapping):
                    continue
                decision = session_record.get("decision")
                if not isinstance(decision, Mapping) or decision.get("action") != "SELECT":
                    continue
                bridge_sessions.append({field: session_record.get(field) for field in (
                    "session_id", "persistence_status", "source_round", "target_round", "decision",
                )})
            committed_bridge_job = {
                "event": dict(event),
                "evaluation": {"sessions": bridge_sessions},
                "collector_detection_at": collector_detection_at,
                "collector_receipt_at": collector_receipt_at,
                "correlation_id": correlation_id,
                "event_stage_id": event_stage_id,
                "event_key": _round_event_key("committed", event),
                "observation_id": str(event.get("observation_id") or event.get("round_id") or ""),
                "enqueued_ns": enqueued_ns,
                "queued_ns": time.perf_counter_ns(),
                "persistence_session_ids": sorted(persistence_session_ids),
                "performance_observer_deferred": performance_observer_deferred,
            }

    bridge_finished_ns = time.perf_counter_ns()
    if hub is not None:
        if request_started_ns is not None and stage == "observed":
            hub.record_priority_latency(
                "backend_receive_to_final_decision_ms", f"{correlation_id}:backend_receive_to_final_decision_ms",
                (bridge_finished_ns - request_started_ns) / 1_000_000.0)
        if stage == "observed" or committed_bridge_job is None:
            hub.record_priority_latency(
                "backend_decision_and_signal_path_ms",
                f"{correlation_id}:{event_stage_id}:backend_decision_and_signal_path_ms",
                (bridge_finished_ns - enqueued_ns) / 1_000_000.0)

        log.info("[ROUND_EVENT_WORKER] stage=%s correlation_id=%s round_id=%s queue_ms=%.3f evaluation_ms=%.3f total_ms=%.3f",
             stage, correlation_id, event.get("round_id"),
             max(0.0, (worker_started_ns - enqueued_ns) / 1_000_000.0),
             max(0.0, (completed_ns - received_ns) / 1_000_000.0),
             max(0.0, (bridge_finished_ns - enqueued_ns) / 1_000_000.0))
    if performance_observer_deferred and committed_bridge_job is None:
        observer_started_ns = time.perf_counter_ns()
        observe_performance = partial(
            engine.observe_rule_performance_round, event, defer_persistence=True)
        if rules_executor is None:
            performance_changed = await asyncio.to_thread(observe_performance)
        else:
            performance_changed = await asyncio.get_running_loop().run_in_executor(
                rules_executor, observe_performance)
        if performance_changed:
            persistence_session_ids.add(RULE_PERFORMANCE_CHECKPOINT_ID)
        if hub is not None:
            hub.record_priority_latency(
                "rule_performance_observer_ms",
                f"{correlation_id}:rule_performance_observer_ms",
                (time.perf_counter_ns() - observer_started_ns) / 1_000_000.0)
    _enqueue_rules_dashboard_notification(app, {
        "type": "rules-engine:evaluated", "evaluation": evaluation,
        "received_at": received_at,
    })
    if stage == "committed" and committed_bridge_job is None:
        getattr(app.state, "collector_detection_timestamps", {}).pop(correlation_id, None)
        getattr(app.state, "collector_receipt_timestamps", {}).pop(correlation_id, None)
    return True, persistence_session_ids, committed_bridge_job


async def _run_rules_state_persistence_worker(app: FastAPI) -> None:
    """Checkpoint the latest ordered engine state after urgent signal dispatch.

    The durable inbox remains unacknowledged until PostgreSQL commits a state
    snapshot. Adjacent updates are coalesced because a full snapshot contains
    every earlier ordered transition; this keeps database pressure bounded
    without losing replay information on a crash.
    """
    queue = _rules_state_persistence_queue(app)
    while True:
        first = await queue.get()
        batch = [first]
        # Allow a short coalescing window; each snapshot covers the complete
        # current Rules Engine state, so one commit can cover these events.
        await asyncio.sleep(0.02)
        app.state.rules_state_persistence_busy = True
        while True:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        stage_entries = [(stage, item[1], item[3]) for item in batch for stage in item[0]]
        stage_ids = [(stage, observation_id)
                     for stage, observation_id, _enqueued_ns in stage_entries]
        covered_keys = [item[2] for item in batch]
        session_ids = set().union(*(set(item[4]) for item in batch))
        worker_started_ns = time.perf_counter_ns()
        queue_waits_ms = [max(0.0, (worker_started_ns - enqueued_ns) / 1_000_000.0)
                          for _stage, _observation_id, enqueued_ns in stage_entries]
        attempts = 0
        try:
            while True:
                try:
                    engine = getattr(app.state, "rules_engine", None)
                    if engine is None:
                        raise RuntimeError("rules engine unavailable for durable state checkpoint")
                    executor = getattr(app.state, "round_persistence_executor", None)
                    started_ns = time.perf_counter_ns()
                    persist = partial(engine.persist_current_snapshot, session_ids=session_ids)
                    if executor is None:
                        timings = await asyncio.to_thread(persist)
                    else:
                        timings = await asyncio.get_running_loop().run_in_executor(executor, persist)
                    persistence_total_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                    inbox = getattr(app.state, "round_observation_inbox", None)
                    if inbox is not None:
                        await run_round_observation_inbox(
                            app, inbox.mark_rules_processed_many, stage_ids, priority=True)
                    hub = getattr(app.state, "round_distribution", None)
                    if hub is not None:
                        for (_, observation_id), queue_wait_ms in zip(stage_ids, queue_waits_ms):
                            suffix = observation_id or "unknown"
                            hub.record_priority_latency("rules_state_persistence_queue_wait_ms",
                                f"{suffix}:rules_state_persistence_queue_wait_ms", queue_wait_ms)
                            hub.record_priority_latency("rules_snapshot_capture_ms",
                                f"{suffix}:rules_snapshot_capture_ms",
                                float(timings.get("snapshot_serialization_ms") or 0.0))
                            hub.record_priority_latency("rules_database_persistence_ms",
                                f"{suffix}:rules_database_persistence_ms",
                                float(timings.get("database_persistence_ms") or 0.0))
                            hub.record_priority_latency("rules_persistence_total_ms",
                                f"{suffix}:rules_persistence_total_ms", persistence_total_ms)
                    queued_ids = getattr(app.state, "round_event_queued_ids", set())
                    retry_attempts = getattr(app.state, "round_event_retry_attempts", {})
                    for key in covered_keys:
                        queued_ids.discard(key)
                        retry_attempts.pop(key, None)
                        _forget_round_event_completion(app, key)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    attempts += 1
                    log.exception("[RULES_STATE_PERSISTENCE] snapshot commit failed; durable inbox remains pending batch=%s attempt=%s",
                                  len(batch), attempts)
                    delay = min(10.0, 0.1 * (2 ** min(attempts - 1, 7)))
                    await asyncio.sleep(delay)
        finally:
            app.state.rules_state_persistence_busy = False
            for _ in batch:
                queue.task_done()


async def _run_committed_bridge_processor(app: FastAPI) -> None:
    """Settle committed results without holding the live decision worker."""
    queue = getattr(app.state, "committed_bridge_queue", None)
    if queue is None:
        return
    while True:
        job = await queue.get()
        attempt = 0
        try:
            while True:
                try:
                    bridge_started_ns = time.perf_counter_ns()
                    bridge = getattr(app.state, "shadow_signal_bridge", None)
                    if bridge is not None:
                        await bridge.process_committed_round(
                            app, job["event"], job["evaluation"],
                            collector_detection_at=job.get("collector_detection_at"),
                            collector_receipt_at=job.get("collector_receipt_at"))

                    persistence_session_ids = set(job.get("persistence_session_ids") or [])
                    if job.get("performance_observer_deferred"):
                        engine = getattr(app.state, "rules_engine", None)
                        observe_performance = getattr(engine, "observe_rule_performance_round", None)
                        if callable(observe_performance):
                            started_ns = time.perf_counter_ns()
                            executor = getattr(app.state, "round_processing_executor", None)
                            operation = partial(observe_performance, job["event"], defer_persistence=True)
                            if executor is None:
                                performance_changed = await asyncio.to_thread(operation)
                            else:
                                performance_changed = await asyncio.get_running_loop().run_in_executor(
                                    executor, operation)
                            if performance_changed:
                                persistence_session_ids.add(RULE_PERFORMANCE_CHECKPOINT_ID)
                            hub = getattr(app.state, "round_distribution", None)
                            if hub is not None:
                                hub.record_priority_latency(
                                    "rule_performance_observer_ms",
                                    f"{job['correlation_id']}:rule_performance_observer_ms",
                                    (time.perf_counter_ns() - started_ns) / 1_000_000.0)

                    observation_id = str(job.get("observation_id") or "")
                    event_key = str(job.get("event_key") or "")
                    persistence_queue = _rules_state_persistence_queue(app)
                    if observation_id and event_key:
                        await persistence_queue.put((
                            ["observed", "committed"], observation_id, event_key,
                            time.perf_counter_ns(), sorted(persistence_session_ids)))

                    correlation_id = str(job.get("correlation_id") or "")
                    getattr(app.state, "collector_detection_timestamps", {}).pop(correlation_id, None)
                    getattr(app.state, "collector_receipt_timestamps", {}).pop(correlation_id, None)
                    finished_ns = time.perf_counter_ns()
                    hub = getattr(app.state, "round_distribution", None)
                    if hub is not None:
                        hub.record_priority_latency(
                            "committed_bridge_queue_wait_ms",
                            f"{correlation_id}:committed_bridge_queue_wait_ms",
                            max(0.0, (bridge_started_ns - int(job.get("queued_ns") or bridge_started_ns))
                                / 1_000_000.0))
                        hub.record_priority_latency(
                            "committed_bridge_processing_ms",
                            f"{correlation_id}:committed_bridge_processing_ms",
                            max(0.0, (finished_ns - bridge_started_ns)
                                / 1_000_000.0))
                        hub.record_priority_latency(
                            "backend_decision_and_signal_path_ms",
                            f"{correlation_id}:{job.get('event_stage_id')}:backend_decision_and_signal_path_ms",
                            max(0.0, (finished_ns - int(job.get("enqueued_ns") or finished_ns))
                                / 1_000_000.0))
                    completion = getattr(app.state, "round_event_completion_waiters", {}).get(event_key)
                    if completion is not None and not completion.done():
                        completion.set_result({"decision_ready": True, "stage": "committed"})
                    log.info("[COMMITTED_SHADOW_BRIDGE] correlation_id=%s queue_ms=%.3f total_ms=%.3f",
                             correlation_id,
                             max(0.0, (finished_ns - int(job.get("queued_ns") or finished_ns))
                                 / 1_000_000.0),
                             max(0.0, (finished_ns - int(job.get("enqueued_ns") or finished_ns))
                                 / 1_000_000.0))
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    attempt += 1
                    key = str(job.get("event_key") or "")
                    attempts = getattr(app.state, "round_event_retry_attempts", {})
                    attempts[key] = int(attempts.get(key, 0)) + 1
                    log.exception("[COMMITTED_SHADOW_BRIDGE] retrying source=%s attempt=%s",
                                  (job.get("event") or {}).get("round_id"), attempt)
                    await asyncio.sleep(min(0.1 * (2 ** min(attempt - 1, 7)), 10.0))
        finally:
            queue.task_done()


async def _run_round_event_processor(app: FastAPI) -> None:
    queue, queued_ids = _round_event_queue(app)
    while True:
        (_, _, stage, event, key, enqueued_ns, request_started_ns,
         validation_finished_ns) = await queue.get()
        worker_started_ns = time.perf_counter_ns()
        app.state.round_event_processor_busy = True
        deferred_to_persistence = False
        deferred_to_committed_bridge = False
        try:
            attempt = 0
            while True:
                try:
                    requires_checkpoint, persistence_session_ids, committed_bridge_job = await _process_round_event(
                        app, stage, event, enqueued_ns,
                        request_started_ns, validation_finished_ns,
                        worker_started_ns)
                    observation_id = str(event.get("observation_id") or event.get("round_id") or "")
                    # The verified observation is already durable in the inbox,
                    # and the urgent signal has been dispatched before reaching
                    # this point. Avoid a second full Rules Engine snapshot for
                    # the same round: leave the observed row unacknowledged so
                    # startup can replay it if the process stops before commit.
                    # The committed checkpoint will cover and acknowledge both
                    # stages atomically after the state is safely stored.
                    defer_observed_checkpoint = (
                        stage == "observed" and requires_checkpoint
                        and bool(persistence_session_ids))
                    if committed_bridge_job is not None:
                        bridge_queue = _committed_bridge_queue(app)
                        await bridge_queue.put(committed_bridge_job)
                        deferred_to_committed_bridge = True
                    elif defer_observed_checkpoint:
                        # The ordered in-memory engine already consumed it. The
                        # durable inbox remains the crash-recovery authority.
                        queued_ids.discard(key)
                        app.state.round_event_retry_attempts.pop(key, None)
                    elif requires_checkpoint and observation_id:
                        persistence_queue = _rules_state_persistence_queue(app)
                        covered_stages = [stage]
                        if stage == "committed":
                            covered_stages.insert(0, "observed")
                        await persistence_queue.put((covered_stages, observation_id, key,
                                                     time.perf_counter_ns(),
                                                     sorted(persistence_session_ids)))
                        deferred_to_persistence = True
                    else:
                        inbox = getattr(app.state, "round_observation_inbox", None)
                        if inbox is not None and observation_id:
                            await run_round_observation_inbox(
                                app, inbox.mark_rules_processed_many, [(stage, observation_id)],
                                priority=True)
                    completion = getattr(app.state, "round_event_completion_waiters", {}).get(key)
                    if (not deferred_to_committed_bridge and completion is not None
                            and not completion.done()):
                        completion.set_result({
                            "decision_ready": bool(requires_checkpoint),
                            "stage": stage,
                        })
                    if not deferred_to_persistence and not deferred_to_committed_bridge:
                        app.state.round_event_retry_attempts.pop(key, None)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    attempt += 1
                    log.exception("[ROUND_EVENT_WORKER] retrying stage=%s round_id=%s attempt=%s",
                                  stage, event.get("round_id"), attempt)
                    if stage == "committed":
                        attempts = int(app.state.round_event_retry_attempts.get(key, 0)) + 1
                        app.state.round_event_retry_attempts[key] = attempts
                        retry_ids = app.state.round_event_retry_ids
                        retry_ids.add(key)
                        delay = min(10.0, 0.1 * (2 ** min(attempts - 1, 7)))

                        async def retry_committed_round(
                            retry_stage=stage, retry_event=dict(event), retry_key=key,
                            retry_after=delay,
                        ):
                            await asyncio.sleep(retry_after)
                            while True:
                                app.state.round_event_retry_ids.discard(retry_key)
                                if _enqueue_round_events(app, retry_stage, [retry_event]):
                                    return
                                app.state.round_event_retry_ids.add(retry_key)
                                await asyncio.sleep(0.01)

                        asyncio.create_task(retry_committed_round(),
                                            name=f"round-commit-retry:{event.get('round_id')}")
                        break
                    await asyncio.sleep(min(0.1 * (2 ** min(attempt - 1, 7)), 10.0))
        finally:
            app.state.round_event_processor_busy = False
            if not deferred_to_persistence and not deferred_to_committed_bridge:
                queued_ids.discard(key)
                _forget_round_event_completion(app, key)
            queue.task_done()


def _schedule_history_analytics_refresh(app: FastAPI) -> None:
    """Coalesce dashboard history scans into a low-priority background refresh."""
    app.state.history_analytics_refresh_requested = True
    task = getattr(app.state, "history_analytics_refresh_task", None)
    if task is None or task.done():
        app.state.history_analytics_refresh_task = asyncio.create_task(
            _run_history_analytics_refresh(app), name="history-analytics-refresh")


async def _run_history_analytics_refresh(app: FastAPI) -> None:
    while getattr(app.state, "history_analytics_refresh_requested", False):
        last_refresh = float(getattr(app.state, "history_analytics_last_refresh_monotonic", 0.0))
        wait_seconds = max(0.0, ROUND_HISTORY_ANALYTICS_MIN_INTERVAL_SECONDS
                           - (time.monotonic() - last_refresh))
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        app.state.history_analytics_refresh_requested = False
        repository = app.state.wp.repository
        executor = getattr(app.state, "history_analytics_executor", None)
        try:
            if executor is None:
                summary = await asyncio.to_thread(repository.round_history_stats)
            else:
                summary = await asyncio.get_running_loop().run_in_executor(
                    executor, repository.round_history_stats)
            app.state.history_analytics_last_refresh_monotonic = time.monotonic()
            quality = getattr(app.state.wp, "quality", None)
            if isinstance(quality, dict):
                quality["valid_records"] = int(summary.get("total") or 0)
            await manager.broadcast({"type": "history:updated", "history": {
                "count": int(summary.get("total") or 0)}})
            await manager.broadcast({"type": "history:stats", "stats": summary})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history analytics refresh failed")


async def process_history_update(app: FastAPI):
    """Refresh canonical history and enqueue committed events for settlement."""
    await asyncio.to_thread(app.state.wp.reload)
    history = app.state.history_collector
    repository = app.state.wp.repository
    if repository.database_url:
        latest_rows = await asyncio.to_thread(repository.load_recent_rounds, 2)
        latest = latest_rows[-1] if latest_rows else None
        _schedule_history_analytics_refresh(app)
    else:
        status = history.status()
        latest = status.get("latest")
    evaluation = None
    if latest:
        if hasattr(app.state, "shadow"):
            await app.state.shadow.reconcile_round(latest)
    try:
        distribution = await asyncio.to_thread(repository.round_distribution_snapshot)
        current_sequence = int(distribution.get("latest_sequence") or 0)
        stream_id = str(distribution.get("stream_id") or "")
        after_sequence = await asyncio.to_thread(
            app.state.rules_engine.stream_cursor, stream_id, current_sequence)
        if stream_id and current_sequence > after_sequence:
            events = await asyncio.to_thread(repository.round_distribution_events,
                stream_id, after_sequence, current_sequence, 500)
            if not _enqueue_round_events(app, "committed", events):
                log.error("[ROUND_EVENT_WORKER] committed queue full; history replay will retry count=%s",
                          len(events))
        if evaluation is None:
            evaluation = await asyncio.to_thread(app.state.rules_engine.status)
        if not (stream_id and current_sequence > after_sequence):
            await manager.broadcast({"type": "rules-engine:evaluated", "evaluation": evaluation})
    except Exception as exc:
        # Rules are observational consumers. A rules persistence/evaluation
        # error is visible but must not interrupt collector/history delivery.
        log.exception("rules engine committed-event processing failed")
        failed_round_id = str((latest or {}).get("round_id") or "")
        await manager.broadcast({"type": "rules-engine:error", "error": type(exc).__name__,
                                 "round_id": failed_round_id or None,
                                 "correlation_id": failed_round_id or None})
    await manager.broadcast({"type": "history:new_round", "round": latest})
    if not repository.database_url:
        await manager.broadcast({"type": "history:updated", "history": {
            "count": status.get("count", 0)}})
        await manager.broadcast({"type": "history:stats", "stats": history.stats()})


async def monitor_file(app: FastAPI):
    monitor = MonitoringEngine(app.state.wp.settings.data_path)
    repository = app.state.wp.repository
    last_round_marker = None
    while True:
        try:
            await asyncio.sleep(3)
            if repository.database_url:
                marker = await asyncio.to_thread(repository.latest_round_marker)
                changed = marker != last_round_marker
                last_round_marker = marker
            else:
                changed = monitor.has_changed()
            if changed:
                # File parsing and database sync are
                # synchronous/CPU-bound. Keep them off FastAPI's event loop so
                # health checks and UI requests remain responsive.
                await process_history_update(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def _process_direct_collector_observation(app: FastAPI, event: dict, marker: tuple) -> dict:
    """Acknowledge only a round already committed to canonical PostgreSQL history."""
    round_id = str(event["round_id"])
    current = await asyncio.to_thread(app.state.wp.repository.latest_round_marker)
    if not current or (int(current[0]), str(current[1])) != (int(marker[0]), round_id):
        raise RuntimeError("collector_round_marker_changed_before_dispatch")
    await process_history_update(app)
    return {"round_id": round_id, "round_index": int(marker[0]),
            "decision": "NO_ACTION", "reason": "NOT_CONFIGURED"}




async def monitor_collector_round_observations(app: FastAPI):
    """Deliver canonical collector rounds without running prediction observers."""
    repository = app.state.wp.repository
    log.info("[COLLECTOR_EVENTS] canonical round dispatcher started")
    last_inbox_checkpoint_at = time.monotonic()
    while True:
        try:
            if not repository.database_url:
                await asyncio.sleep(1.0)
                continue
            inbox = getattr(app.state, "round_observation_inbox", None)
            if inbox is not None:
                if time.monotonic() - last_inbox_checkpoint_at >= 15.0:
                    last_inbox_checkpoint_at = time.monotonic()
                    try:
                        await run_round_observation_inbox(app, inbox.checkpoint)
                    except Exception as exc:
                        log.warning("[COLLECTOR_EVENTS] inbox WAL checkpoint deferred category=%s",
                                    type(exc).__name__)
                pending = await run_round_observation_inbox(app, inbox.unstaged_observations, limit=100)
                if pending:
                    try:
                        await asyncio.to_thread(repository.stage_collector_round_observations, pending)
                        await run_round_observation_inbox(
                            app, inbox.mark_staged_many,
                            [item["observation_id"] for item in pending], priority=True)
                    except Exception:
                        for item in pending:
                            try:
                                await asyncio.to_thread(repository.stage_collector_round_observations, [item])
                                await run_round_observation_inbox(
                                    app, inbox.mark_staged_many, [item["observation_id"]],
                                    priority=True)
                            except Exception as exc:
                                attempts = await run_round_observation_inbox(
                                    app, inbox.record_stage_failure, item["observation_id"],
                                    f"{type(exc).__name__}:{exc}", priority=True)
                                log.warning("[COLLECTOR_EVENTS] stage deferred id=%s attempts=%s category=%s",
                                            item["observation_id"], attempts, type(exc).__name__)
            events = await asyncio.to_thread(repository.pending_collector_round_observations, 100)
            if not events:
                await asyncio.sleep(0.05)
                continue
            marker = await asyncio.to_thread(repository.latest_round_marker)
            if marker is None:
                await asyncio.sleep(0.1)
                continue
            for row in events:
                round_id = str(row["round_id"])
                round_index = int(row["canonical_round_index"])
                direct_value = float(row["direct_multiplier"])
                canonical_value = float(row["canonical_multiplier"])
                payload = json.loads(row["payload"])
                if (str(row["canonical_round_id"]) != round_id
                        or not math.isclose(direct_value, canonical_value, rel_tol=0, abs_tol=0.0001)
                        or str(payload.get("round_id")) != round_id):
                    await asyncio.to_thread(repository.finish_collector_round_observation,
                        round_id, "IDENTITY_CONFLICT", round_index,
                        "direct-page event does not match the canonical PostgreSQL round")
                    log.error("[COLLECTOR_EVENTS] identity mismatch round_id=%s index=%s", round_id, round_index)
                    continue
                samples = _collector_alignment_status().get("roundCommitSamples") or []
                sample = next((item for item in reversed(samples) if isinstance(item, dict)
                               and str(item.get("round_id")) == round_id), None)
                if sample and sample.get("committed_at"):
                    payload["commit_acknowledged_at"] = sample["committed_at"]
                try:
                    distribution = await asyncio.to_thread(
                        repository.persist_round_distribution_event, round_id,
                        canonical_value, payload,
                        int(os.environ.get("ROUND_STREAM_RETENTION", "100000")))
                    if distribution and distribution.get("published_at") is None:
                        distribution = await asyncio.to_thread(
                            repository.mark_round_distribution_event_published,
                            distribution["stream_id"], distribution["stream_sequence"])
                    if distribution:
                        await app.state.round_distribution.publish(distribution)
                except Exception:
                    log.exception("[ROUND_DISTRIBUTION] outbox failed round_id=%s", round_id)
                if (round_index, round_id) != (int(marker[0]), str(marker[1])):
                    if round_index < int(marker[0]):
                        await asyncio.to_thread(repository.finish_collector_round_observation,
                            round_id, "SUPERSEDED", round_index,
                            f"newer PostgreSQL source {marker[1]} was current before dispatch")
                    continue
                try:
                    result = await _process_direct_collector_observation(app, row, marker)
                    finish = "SUPERSEDED" if result.get("superseded") else "PROCESSED"
                    await asyncio.to_thread(repository.finish_collector_round_observation,
                        round_id, finish, round_index,
                        "newer round arrived during dispatch" if finish == "SUPERSEDED" else None)
                    log.info("[COLLECTOR_EVENTS] canonical round handled round_id=%s result=%s", round_id, result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = min(10.0, 0.25 * (2 ** min(int(row.get("attempts", 0)), 5)))
                    await asyncio.to_thread(repository.retry_collector_round_observation,
                        round_id, f"{type(exc).__name__}:{str(exc)[:800]}", delay)
                    log.exception("[COLLECTOR_EVENTS] dispatch deferred round_id=%s category=%s",
                                  round_id, type(exc).__name__)
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[COLLECTOR_EVENTS] dispatcher cycle failed category=%s", type(exc).__name__)
            await asyncio.sleep(1.0)














def _collector_alignment_status() -> dict:
    status_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _collector_runtime_status() -> dict:
    """Read the separately supervised collector's current sidecar status."""
    source = _collector_alignment_status()
    now = datetime.now(timezone.utc)
    def parse(value):
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp
        except (TypeError, ValueError, OverflowError):
            return None
    heartbeat_at = parse(source.get("heartbeatAt"))
    collected_at = parse(source.get("lastSuccessfulCollection"))
    heartbeat_age = (now - heartbeat_at).total_seconds() if heartbeat_at else None
    collection_age = (now - collected_at).total_seconds() if collected_at else None
    heartbeat_limit = float(os.environ.get("COLLECTOR_HEARTBEAT_STALE_SECONDS", "20"))
    heartbeat_state = ("UNKNOWN" if heartbeat_age is None else
                       "FRESH" if 0 <= heartbeat_age <= heartbeat_limit else "STALE")
    return {
        "running": source.get("collectorRunning") is True,
        "collector_process_status": ("RUNNING" if source.get("collectorRunning") is True else
                                     "STOPPED" if source.get("collectorRunning") is False else "UNKNOWN"),
        "runtime_status": source.get("state") or source.get("health") or "UNKNOWN",
        "status": source.get("state") or source.get("health") or "UNKNOWN",
        "health": source.get("health"),
        "browser_connected": source.get("browserConnected") is True,
        "page_connected": source.get("pageConnected") is True,
        "frame_connected": source.get("frameConnected") is True,
        "logged_in": source.get("loggedIn") if isinstance(source.get("loggedIn"), bool) else None,
        "auth_required": source.get("authRequired") is True,
        "heartbeat_at": source.get("heartbeatAt"),
        "heartbeat_state": heartbeat_state,
        "heartbeat_age_seconds": heartbeat_age,
        "collection_fresh": collection_age is not None and 0 <= collection_age <= 120.0,
        "last_successful_collection": source.get("lastSuccessfulCollection"),
        "last_round_id": source.get("lastRoundId"),
        "last_round_index": source.get("lastRoundIndex"),
        "authentication_state": source.get("authenticationState")
            if source.get("authenticationState") in {"AUTHENTICATED", "AUTH_REQUIRED", "UNKNOWN"}
            else "UNKNOWN",
        "browser_pages": source.get("browser_pages") or {},
    }














async def monitor_health(app: FastAPI):
    while True:
        try:
            await app.state.system_health.refresh_and_emit()
            # Full stability snapshots read up to 5,000 lifecycle rows and
            # assemble several database backed views. That operator report
            # must not run in this two-second event-loop monitor; the full
            # snapshot remains available from /api/backend/stability.
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system health monitor cycle failed; retrying")
            await asyncio.sleep(2)






async def initialize_app_state(settings, attempts: int = 3) -> AppState:
    """Initialize disk/database state with bounded retries for transient locks."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(AppState, settings)
        except sqlite3.OperationalError as exc:
            if attempt == attempts:
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            log.warning("database startup attempt %s/%s failed: %s; retrying in %.1fs",
                        attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("application state initialization exhausted retries")


async def backend_stability_snapshot(app: FastAPI) -> dict:
    """Build a cheap, non-blocking stability view for the dashboard."""
    lifecycle = getattr(app.state, "lifecycle", None)
    snapshot = await asyncio.to_thread(lifecycle.status) if lifecycle is not None else {
        "status": "STARTING", "pid": os.getpid(), "ppid": os.getppid(),
    }
    history = getattr(app.state, "history_collector", None)
    snapshot["collector"] = (await asyncio.to_thread(history.health_status)
                              if history else {"status": "STARTING"})
    current = await asyncio.to_thread(_collector_runtime_status)
    if current.get("collector_process_status") == "STOPPED":
        current["runtime_status"] = "STOPPED"
    elif (current.get("heartbeat_state") != "FRESH" or current.get("collection_fresh") is not True
          or current.get("logged_in") is not True or not current.get("browser_connected")
          or not current.get("frame_connected")):
        current["runtime_status"] = "STALE"
    else:
        current["runtime_status"] = "HEALTHY"
    snapshot["collector_current"] = current
    snapshot["rules_engine"] = await asyncio.to_thread(app.state.rules_engine.status)
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if repository is None:
        snapshot["database"] = {"status": "STARTING"}
    else:
        try:
            def check_database():
                with repository.connect() as conn:
                    conn.execute("SELECT 1").fetchone()

            await asyncio.to_thread(check_database)
            snapshot["database"] = {"status": "OK"}
        except Exception as exc:
            snapshot["database"] = {"status": "ERROR", "reason": str(exc)}
    health = getattr(app.state, "system_health", None)
    if health is None:
        snapshot["browser"] = {"status": "STARTING"}
    else:
        try:
            browser = health.snapshot()["components"]["browser"]
            snapshot["browser"] = {"status": browser["state"], "reason": browser["last_error"]}
        except Exception as exc:
            snapshot["browser"] = {"status": "ERROR", "reason": str(exc)}
    reliability_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "browser-reliability.json"
    try:
        reliability = await asyncio.to_thread(lambda: json.loads(reliability_path.read_text(encoding="utf-8")))
        updated = datetime.fromisoformat(reliability["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            reliability["snapshot_stale"] = True
            reliability["state"] = "DEGRADED"
            reliability["reason"] = "Supervisor heartbeat is stale"
        snapshot["browser_reliability"] = reliability
    except (OSError, ValueError, KeyError, TypeError):
        snapshot["browser_reliability"] = {"state": "UNKNOWN", "reason": "Supervisor snapshot unavailable"}
    return snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.instance_id = uuid.uuid4().hex
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    lock = InstanceLock(settings.instance_lock_path, port=settings.backend_port,
                        instance_id=app.state.instance_id)
    app.state.instance_lock = lock
    lifecycle = ProcessLifecycle(settings.data_path.parent / "backend_lifecycle.jsonl")
    app.state.lifecycle = lifecycle
    inbox_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round-observation-inbox")
    inbox_priority_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round-observation-priority")
    round_processing_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round-rules-priority")
    signal_priority_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="round-signal-priority")
    round_persistence_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="round-rules-persistence")
    history_analytics_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-analytics")
    adie_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adie-observed-round")
    app.state.round_observation_inbox_executor = inbox_executor
    app.state.round_observation_inbox_priority_executor = inbox_priority_executor
    app.state.round_processing_executor = round_processing_executor
    # Keep source sequence allocation and enqueue order in the same critical
    # section so concurrent collector handoffs cannot reach the observer out
    # of stream order.
    app.state.round_event_commit_lock = asyncio.Lock()
    app.state.signal_priority_executor = signal_priority_executor
    app.state.round_persistence_executor = round_persistence_executor
    app.state.history_analytics_executor = history_analytics_executor
    app.state.adie_executor = adie_executor
    orchestrator = None
    app.state.ready = False
    shutdown_reason = "graceful_lifespan_exit"
    try:
        if settings.enforce_single_instance:
            lock.acquire()
        prior = next((event for event in reversed(lifecycle.events())
                      if event.get("event") == "backend_start" and event.get("pid") != os.getpid()), None)
        lifecycle.start(startup_reason=os.environ.get("WINNER_STARTUP_REASON", "manual_or_process_manager"),
                        previous_pid=(prior or {}).get("pid"),
                        restart_count=int(os.environ.get("WINNER_RESTART_COUNT", "0") or 0))
        log.info("backend startup begin pid=%s host=%s port=%s", os.getpid(),
                 settings.backend_host, settings.backend_port)
        app.state.manager = manager
        app.state.rules_dashboard_notification_queue = asyncio.Queue(
            maxsize=RULES_DASHBOARD_NOTIFICATION_QUEUE_CAPACITY)
        app.state.rules_dashboard_notifications_coalesced = 0
        app.state.rules_dashboard_notification_task = asyncio.create_task(
            _run_rules_dashboard_notification_worker(app),
            name="rules-dashboard-notification-worker")
        app.state.settings = settings
        app.state.round_distribution = RoundDistributionHub()
        app.state.backend_event_loop_monitor = asyncio.create_task(
            _monitor_backend_event_loop(app), name="backend-event-loop-latency-monitor")
        # Provider phase evidence is volatile and must be refreshed after a restart.
        app.state.latest_provider_phase_observation = None
        inbox_path = Path(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_PATH") or
                          (settings.data_path.parent / "backend" / "round-observation-inbox.sqlite3"))
        app.state.round_observation_inbox = RoundObservationInbox(
            inbox_path,
            max_records=int(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_MAX_RECORDS", "50000")),
            max_bytes=int(os.environ.get("COLLECTOR_OBSERVATION_SPOOL_MAX_BYTES", str(64 * 1024 * 1024))),
        )
        # Events already in the journal at process startup are recovery replay,
        # never newly occurring live decisions. A post-boundary event still
        # requires the source's LIVE + VERIFIED classification below.
        app.state.behaviour_live_boundary_at = datetime.now(timezone.utc).isoformat()
        app.state.behaviour_round_wakeup = asyncio.Event()
        app.state.behaviour_round_last_delivered_at = None
        app.state.behaviour_round_last_delivered_id = None
        app.state.behaviour_round_last_delivery_ms = None
        app.state.behaviour_round_last_error = None
        app.state.started_monotonic = time.monotonic()
        app.state.wp = await initialize_app_state(settings)
        try:
            app.state.adie_engine = await asyncio.to_thread(ADIEEngine, app.state.wp.repository)
            log.info("[ADIE] initialized policy=%s cohort=%s", app.state.adie_engine.status().get("policy_version"),
                     app.state.adie_engine.status().get("cohort"))
        except Exception:
            # ADIE is optional to the existing prediction runtime.
            app.state.adie_engine = None
            log.exception("[ADIE] initialization failed; existing SHADOW remains available")
        app.state.fast_signal_persistence_task = asyncio.create_task(
            signals_api.run_fast_signal_persistence(app), name="fast-signal-persistence")
        lifecycle.bind_repository(app.state.wp.repository)
        app.state.round_stream_id = await asyncio.to_thread(
            app.state.wp.repository.round_distribution_stream_id)
        # Reconcile against Behaviour's own durable checkpoint before the
        # ordered consumer starts, so a backend restart does not replay every
        # already-processed historic event ahead of the live tail.
        await _reconcile_behaviour_round_checkpoint(app)
        # Behaviour is a durable consumer of the verified inbox. It runs
        # independently of the Rules Engine worker and PostgreSQL handoff.
        app.state.behaviour_round_consumer_task = asyncio.create_task(
            _run_behaviour_round_consumer(app), name="behaviour-verified-round-consumer")
        log.info("[DATA] Loaded %s canonical rounds", len(app.state.wp.rounds))

        repository = app.state.wp.repository
        # Cache the latest canonical round once at startup. /round-persisted
        # advances this after each PostgreSQL commit, so committed-stage replay
        # never performs a blocking marker query on the ordered round worker.
        app.state.latest_committed_round_marker = await asyncio.to_thread(
            repository.latest_round_marker)
        log.info("[ROUND_MARKER_CACHE] initialized round_index=%s",
                 app.state.latest_committed_round_marker[0]
                 if app.state.latest_committed_round_marker else None)
        app.state.history_collector = HistoryCollectorManager(
            settings.data_path, manager.broadcast,
            round_loader=repository.load_rounds if repository.database_url else None,
            repository=repository)
        app.state.shadow = ShadowManager(repository=repository, broadcaster=manager.broadcast)
        app.state.system_health = SystemHealth(
            broadcaster=manager.broadcast, history_collector=app.state.history_collector,
            repository=repository)
        app.state.rules_engine = RulesEngine(persistence=repository)
        app.state.shadow_signal_bridge = ShadowSignalBridge()
        app.state.shadow_signal_bridge_status = app.state.shadow_signal_bridge.status()
        # Rules decisions must consult observations accepted by the durable
        # API inbox as well as PostgreSQL. The inbox can lead PostgreSQL by
        # several seconds while its consumer retries a slow commit.
        app.state.rules_engine.observation_inbox = app.state.round_observation_inbox
        app.state.system_health.heartbeat("rules_engine", ok=True,
            metadata=app.state.rules_engine.status())
        app.state.system_health.heartbeat("api", ok=True,
            metadata={"initialized": True}, stale_after_s=3600)
        app.state.history_monitor = monitor_file
        app.state.collector_observation_monitor = monitor_collector_round_observations
        app.state.system_health_monitor = monitor_health
        orchestrator = SystemOrchestrator(app)
        app.state.orchestrator = orchestrator
        # The inbox accepted these events durably before the collector received
        # its HTTP acknowledgement. Resume their observed Rules Engine stage
        # before starting lower-priority monitors or accepting new rounds.
        await _recover_pending_observed_rounds(app)
        await orchestrator.start()
        app.state.ready = True
        app.state.shadow_signal_bridge.start_pending_settlement_recovery(app)
        log.info("backend startup complete pid=%s instance=%s", os.getpid(), app.state.instance_id)
        yield
    except Exception as exc:
        shutdown_reason = f"startup_or_runtime_exception:{type(exc).__name__}"
        lifecycle.exception(exc, phase="startup_or_runtime")
        log.exception("backend startup/runtime failure")
        raise
    finally:
        app.state.ready = False
        behaviour_consumer_task = getattr(app.state, "behaviour_round_consumer_task", None)
        if behaviour_consumer_task is not None and not behaviour_consumer_task.done():
            behaviour_consumer_task.cancel()
            try:
                await behaviour_consumer_task
            except asyncio.CancelledError:
                pass
        fast_signal_task = getattr(app.state, "fast_signal_persistence_task", None)
        if fast_signal_task is not None:
            fast_signal_task.cancel()
            try:
                await fast_signal_task
            except asyncio.CancelledError:
                pass
        log.info("backend graceful shutdown begin pid=%s", os.getpid())
        if orchestrator is not None:
            await orchestrator.shutdown()
        notification_task = getattr(app.state, "rules_dashboard_notification_task", None)
        if notification_task is not None and not notification_task.done():
            notification_task.cancel()
            try:
                await notification_task
            except asyncio.CancelledError:
                pass
        event_loop_monitor = getattr(app.state, "backend_event_loop_monitor", None)
        if event_loop_monitor is not None and not event_loop_monitor.done():
            event_loop_monitor.cancel()
            try:
                await event_loop_monitor
            except asyncio.CancelledError:
                pass
        processor_task = getattr(app.state, "round_event_processor_task", None)
        if processor_task is not None and not processor_task.done():
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass
        persistence_task = getattr(app.state, "rules_state_persistence_task", None)
        if persistence_task is not None and not persistence_task.done():
            persistence_task.cancel()
            try:
                await persistence_task
            except asyncio.CancelledError:
                pass
        analytics_task = getattr(app.state, "history_analytics_refresh_task", None)
        if analytics_task is not None and not analytics_task.done():
            analytics_task.cancel()
            try:
                await analytics_task
            except asyncio.CancelledError:
                pass
        hub = getattr(app.state, "round_distribution", None)
        if hub is not None:
            await hub.shutdown()
        lifecycle.shutdown(reason=shutdown_reason)
        inbox_executor.shutdown(wait=True, cancel_futures=True)
        inbox_priority_executor.shutdown(wait=True, cancel_futures=True)
        round_processing_executor.shutdown(wait=True, cancel_futures=True)
        signal_priority_executor.shutdown(wait=True, cancel_futures=True)
        round_persistence_executor.shutdown(wait=True, cancel_futures=True)
        history_analytics_executor.shutdown(wait=True, cancel_futures=True)
        adie_executor.shutdown(wait=True, cancel_futures=True)
        inbox = getattr(app.state, "round_observation_inbox", None)
        if callable(getattr(inbox, "close", None)):
            inbox.close()
        repository = getattr(getattr(app.state, "wp", None), "repository", None)
        if repository is not None:
            repository.close()
        lock.release()
        log.info("backend graceful shutdown complete pid=%s", os.getpid())


app = FastAPI(title="Winner Predict", description="Aviator round history and SHADOW rules console.", version="1.0.0", lifespan=lifespan)
app.state.api_request_admission = ApiRequestAdmission.from_environment()
settings = get_settings()


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Return a stable error response without terminating the ASGI process."""
    request_id = getattr(request.state, "request_id", "unknown")
    log.exception("unhandled API failure request_id=%s method=%s path=%s",
                  request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error",
                 "request_id": request_id},
    )


@app.exception_handler(StarletteHTTPException)
async def public_api_http_exception(request: Request, exc: StarletteHTTPException):
    if (request.method.upper(), request.url.path) not in PUBLIC_API_READS:
        return await http_exception_handler(request, exc)
    detail = exc.detail
    if isinstance(detail, dict):
        payload = {"ok": False, **detail}
    else:
        payload = {"ok": False, "error": "request_failed", "message": str(detail)}
    payload.setdefault("request_id", getattr(request.state, "request_id", "unknown"))
    return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def public_api_validation_error(request: Request, exc: RequestValidationError):
    if (request.method.upper(), request.url.path) not in PUBLIC_API_READS:
        return await request_validation_exception_handler(request, exc)
    details = [{"location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "value_error")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={
        "ok": False, "error": "validation_error", "details": details,
        "request_id": getattr(request.state, "request_id", "unknown"),
    })


@app.middleware("http")
async def api_request_admission(request: Request, call_next):
    """Apply bounded rate and in-flight limits to API HTTP requests."""
    path = request.url.path
    if request.method.upper() == "OPTIONS" or not path.startswith("/api/"):
        return await call_next(request)
    admission = getattr(request.app.state, "api_request_admission", None)
    if admission is None:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "api_admission_not_ready",
        }, headers={"Retry-After": "1"})

    authorization = request.headers.get("authorization", "")
    if authorization:
        client_key = "credential:" + hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    else:
        client_key = "ip:" + (request.client.host if request.client else "unknown")
    rate_allowed, retry_after = admission.allow_request(client_key)
    if not rate_allowed:
        request.state.api_admission_rejection = True
        rejected_total = admission.record_rejection(429)
        if rejected_total == 1 or rejected_total % 100 == 0:
            log.warning("[API_ADMISSION] rate limited path=%s retry_after=%s total=%s",
                        path, retry_after, rejected_total)
        return JSONResponse(status_code=429, content={
            "ok": False, "error": "api_rate_limit_exceeded",
        }, headers={"Retry-After": str(retry_after)})
    if not admission.try_acquire():
        request.state.api_admission_rejection = True
        rejected_total = admission.record_rejection(503)
        if rejected_total == 1 or rejected_total % 100 == 0:
            log.warning("[API_ADMISSION] overloaded path=%s total=%s", path, rejected_total)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "api_overloaded_retry",
        }, headers={"Retry-After": "1"})
    started = time.perf_counter()
    try:
        response = await call_next(request)
        admission.record_response(response.status_code,
                                 (time.perf_counter() - started) * 1000.0)
        return response
    except Exception:
        admission.record_response(500, (time.perf_counter() - started) * 1000.0)
        raise
    finally:
        admission.release()


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        log.exception("API request crashed request_id=%s method=%s path=%s duration_ms=%.1f",
                      request_id, request.method, request.url.path, elapsed)
        response = JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_server_error",
                     "request_id": request_id},
        )
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    if response.status_code >= 500 and not getattr(request.state, "api_admission_rejection", False):
        log.error("API failure request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                  request_id, request.method, request.url.path,
                  response.status_code, elapsed)
    elif request.url.path != "/health" and elapsed >= 500:
        log.info("API request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                 request_id, request.method, request.url.path,
                 response.status_code, elapsed)
    return response


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Keep public routes explicit and fail closed for private APIs."""
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    if request.url.scheme != "https" and not _is_loopback_request(request):
        return JSONResponse(status_code=426,
                            content={"ok": False, "error": "https_required_for_external_access"})
    origin = request.headers.get("origin")
    allowed_origins = set(settings.cors_origins or []) | set(settings.round_stream_allowed_origins or [])
    if origin and origin not in allowed_origins:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "origin_not_allowed"})

    raw_content_length = request.headers.get("content-length")
    try:
        content_length = int(raw_content_length) if raw_content_length is not None else 0
    except ValueError:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "invalid_content_length"})
    if content_length < 0:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "invalid_content_length"})
    if content_length > MAX_API_BODY_BYTES:
        return JSONResponse(status_code=413,
                            content={"ok": False, "error": "request_body_too_large"})
    if (request.method.upper(), request.url.path) in PUBLIC_API_READS and content_length:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "request_body_not_supported"})
    if (request.method.upper() in {"POST", "PUT", "PATCH"}
            and request.headers.get("transfer-encoding") and raw_content_length is None):
        return JSONResponse(status_code=411,
                            content={"ok": False, "error": "content_length_required"})

    if (request.method.upper(), request.url.path) in PUBLIC_API_READS:
        return await call_next(request)
    if _uses_scoped_bearer(request.method, request.url.path):
        return await call_next(request)
    if request.method.upper() == "POST" and request.url.path == LOCAL_BROWSER_SESSION_PATH:
        return await call_next(request)
    if _has_local_browser_session(request):
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and origin not in allowed_origins:
            return JSONResponse(status_code=403,
                                content={"ok": False, "error": "local_session_origin_required"})
        request.state.local_owner_session = True
        return await call_next(request)

    # A missing owner key must never turn private operations into anonymous APIs.
    # Loopback remains available for local development; remote access fails closed.
    if not settings.api_key:
        if _is_loopback_request(request):
            return await call_next(request)
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "api_key_not_configured"},
                            headers={"WWW-Authenticate": "Bearer"})

    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.api_key}"
    if not hmac.compare_digest(supplied, expected):
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "authentication_required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def _utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_collector_game_phase(raw: dict, *, require_source: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("game phase evidence must be an object")
    phase = str(raw.get("phase") or "").upper()
    if phase not in {"BETTING_OPEN", "BETTING_CLOSED", "UNKNOWN"}:
        raise ValueError("phase must be BETTING_OPEN, BETTING_CLOSED, or UNKNOWN")
    observed_at = str(raw.get("observed_at") or "")
    try:
        observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("game phase observed_at must be an ISO timestamp") from exc
    if observed_time.tzinfo is None:
        raise ValueError("game phase observed_at must include a timezone")
    event_id = str(raw.get("event_id") or "").strip()
    if not event_id or len(event_id) > 512:
        raise ValueError("game phase event_id is missing or too long")
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("game phase evidence details are missing")
    evidence_fields = {
        "source": str(evidence.get("source") or "")[:80],
        "selector": str(evidence.get("selector") or "")[:120],
        "provider_phase_source": str(evidence.get("provider_phase_source") or "")[:80],
        "provider_phase_selector": str(evidence.get("provider_phase_selector") or "")[:180],
        "button_visible": evidence.get("button_visible") is True,
        "button_enabled": evidence.get("button_enabled") is True,
        "stake_input_visible": evidence.get("stake_input_visible") is True,
        "bet_tab_active": evidence.get("bet_tab_active") is True,
        "button_text": str(evidence.get("button_text") or "")[:40],
        "button_class": str(evidence.get("button_class") or "")[:160],
    }
    provider_round_id = str(raw.get("provider_round_id") or "").strip()
    canonical_target_round_id = str(raw.get("canonical_target_round_id") or "").strip()
    betting_deadline = raw.get("betting_deadline")
    deadline_time = _utc_timestamp(betting_deadline)
    target_started = raw.get("target_started")
    proof = raw.get("target_identity_proof")
    proof_valid = bool(
        isinstance(proof, dict)
        and proof.get("provider_round_id") == provider_round_id
        and proof.get("target_round_id") == canonical_target_round_id
        and proof.get("target_not_started") is (target_started is False)
        and proof.get("proof_method") == "platform_target_id_before_start"
    )
    identity_verified = bool(
        raw.get("target_identity_verified") is True
        and provider_round_id and canonical_target_round_id
        and target_started is False and proof_valid
    )
    try:
        target_round_index = int(raw.get("target_round_index"))
    except (TypeError, ValueError, OverflowError):
        target_round_index = None
    provider_evidence_valid = bool(
        evidence.get("source") == "AVIATOR_GAME_PHASE_READ_ONLY"
        and evidence.get("provider_phase_source") == "EXPLICIT_PROVIDER_DATA_ATTRIBUTES"
        and evidence.get("provider_phase_selector") == (
            "[data-aviator-phase][data-provider-round-id][data-canonical-target-round-id][data-betting-deadline]"
        )
    )
    controls_valid = bool(
        evidence.get("selector") == ".bet-block .btn-success.bet"
        and evidence.get("stake_input_visible") is True
        and evidence.get("bet_tab_active") is True
    )
    if phase in {"BETTING_OPEN", "BETTING_CLOSED"} and not (
        provider_evidence_valid and identity_verified and deadline_time is not None
        and target_round_index is not None and controls_valid
    ):
        raise ValueError("known betting phase requires fresh provider phase, target identity, deadline, and panel evidence")
    if phase == "BETTING_OPEN" and not (
        target_started is False and evidence.get("button_visible") is True
        and evidence.get("button_enabled") is True
    ):
        raise ValueError("BETTING_OPEN requires explicit provider-open evidence and an enabled betting panel")
    result = {"event_id": event_id, "source": "AVIATOR_GAME_PHASE_READ_ONLY", "phase": phase,
              "observed_at": observed_time.astimezone(timezone.utc).isoformat(),
              "provider_round_id": provider_round_id or None,
              "canonical_target_round_id": canonical_target_round_id or None,
              "target_round_index": target_round_index,
              "betting_deadline": deadline_time.isoformat() if deadline_time else None,
              "target_identity_verified": identity_verified,
              "target_started": target_started if isinstance(target_started, bool) else None,
              "target_identity_proof": proof if identity_verified else None,
              "evidence": evidence_fields}
    if require_source:
        source_id = str(raw.get("source_round_id") or "").strip()
        if not source_id or len(source_id) > 256:
            raise ValueError("source_round_id is missing or invalid")
        try:
            source_index = int(raw.get("source_round_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("source_round_index must be an integer") from exc
        if source_index < 1:
            raise ValueError("source_round_index must be positive")
        result.update({"source_round_id": source_id, "source_round_index": source_index})
        if target_round_index is not None and target_round_index != source_index + 1:
            raise ValueError("provider target must be the immediate successor of its source round")
    return result


def _collector_target_identity(raw: dict) -> dict:
    """Retain target identity only when the collector supplies explicit proof."""
    target_id_value = raw.get("targetRoundId") if isinstance(raw, dict) else None
    target_round_id = str(target_id_value).strip() if target_id_value is not None else ""
    target_round_id = target_round_id if target_round_id and len(target_round_id) <= 256 else None
    started_value = raw.get("targetStarted") if isinstance(raw, dict) else None
    target_started = started_value if isinstance(started_value, bool) else None
    proof = raw.get("targetIdentityProof") if isinstance(raw, dict) else None
    valid_proof = (
        isinstance(proof, dict)
        and proof.get("target_round_id") == target_round_id
        and proof.get("target_not_started") is True
        and proof.get("proof_method") == "platform_target_id_before_start"
    )
    verified = bool(
        target_round_id
        and isinstance(raw, dict)
        and raw.get("targetIdentityVerified") is True
        and target_started is False
        and valid_proof
    )
    return {
        "target_round_id": target_round_id,
        "target_identity_verified": verified,
        "target_started": target_started,
        "target_identity_proof": proof if verified else None,
    }


def _validate_direct_collector_observations(body: dict) -> list[dict]:
    raw_rounds = body.get("rounds") if isinstance(body, dict) else None
    if not isinstance(raw_rounds, list) or not raw_rounds or len(raw_rounds) > 50:
        raise ValueError("rounds must contain between 1 and 50 observations")
    observations = []
    seen_ids = set()
    for raw in raw_rounds:
        if not isinstance(raw, dict):
            raise ValueError("each round observation must be an object")
        round_id = str(raw.get("round_id") or "").strip()
        if not round_id or len(round_id) > 256 or round_id in seen_ids:
            raise ValueError("round_id is missing, too long, or duplicated")
        seen_ids.add(round_id)
        legacy_event_id = f"aviator-round:{round_id}"
        event_id = str(raw.get("event_id") or legacy_event_id)
        if event_id not in {legacy_event_id, f"{legacy_event_id}:observed"}:
            raise ValueError("event_id must identify the round observation")
        observation_id = str(raw.get("observation_id") or round_id).strip()
        if not observation_id or len(observation_id) > 256:
            raise ValueError("observation_id is missing or invalid")
        try:
            multiplier = float(raw.get("multiplier"))
        except (TypeError, ValueError) as exc:
            raise ValueError("multiplier must be numeric") from exc
        if not math.isfinite(multiplier) or not 1 <= multiplier <= 1_000_000:
            raise ValueError("multiplier must be a finite platform value at least 1x")
        multiplier = round(multiplier, 2)
        observed_at = str(raw.get("observed_at") or "")
        try:
            observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO timestamp") from exc
        if observed_time.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        try:
            local_index = int(raw.get("round_index", raw.get("local_round_index")))
        except (TypeError, ValueError) as exc:
            raise ValueError("round_index must be an integer") from exc
        if local_index < 1:
            raise ValueError("round_index must be positive")
        platform_round_index = raw.get("platform_round_index")
        if platform_round_index is not None:
            try:
                platform_round_index = int(platform_round_index)
            except (TypeError, ValueError) as exc:
                raise ValueError("platform_round_index must be an integer") from exc
            if platform_round_index < 1:
                raise ValueError("platform_round_index must be positive")
        platform_round_id = raw.get("platform_round_id")
        if platform_round_id is not None:
            platform_round_id = str(platform_round_id).strip()
            if not platform_round_id or len(platform_round_id) > 256:
                raise ValueError("platform_round_id is invalid")
        platform_timestamp = raw.get("platform_timestamp")
        if platform_timestamp is not None:
            parsed_platform_timestamp = _utc_timestamp(platform_timestamp)
            if parsed_platform_timestamp is None:
                raise ValueError("platform_timestamp must be an ISO timestamp or null")
            platform_timestamp = parsed_platform_timestamp.isoformat()
        game_phase = raw.get("game_phase")
        if game_phase is not None:
            game_phase = _validate_collector_game_phase(game_phase)
        delivery_mode = str(raw.get("delivery_mode") or "LIVE").upper()
        if delivery_mode not in {"LIVE", "RECOVERED"}:
            raise ValueError("delivery_mode must be LIVE or RECOVERED")
        round_index_source = str(raw.get("round_index_source") or "UNKNOWN").upper()
        if round_index_source not in {"PLATFORM", "LOCAL_SEQUENCE", "UNKNOWN"}:
            raise ValueError("round_index_source must be PLATFORM, LOCAL_SEQUENCE, or UNKNOWN")
        local_appended_at = raw.get("local_appended_at")
        collector_http_started_at = raw.get("collector_http_started_at")
        for field_name, field_value in (("local_appended_at", local_appended_at),
                                        ("collector_http_started_at", collector_http_started_at)):
            if field_value is not None and _utc_timestamp(field_value) is None:
                raise ValueError(f"{field_name} must be an ISO timestamp or null")
        observations.append({
            "event_id": legacy_event_id,
            "observation_id": observation_id,
            "round_id": round_id,
            "multiplier": multiplier,
            "observed_at": observed_time.astimezone(timezone.utc).isoformat(),
            "timestamp": raw.get("timestamp"),
            "platform_round_id": platform_round_id,
            "platform_round_index": platform_round_index,
            "platform_timestamp": platform_timestamp,
            "round_index": local_index,
            "round_index_source": round_index_source,
            "continuity_verified": raw.get("continuity_verified") is True,
            "gap_before": raw.get("gap_before") is True,
            "identity_confidence": str(raw.get("identity_confidence") or "UNKNOWN")[:80],
            "round_identity_type": str(raw.get("round_identity_type") or "UNKNOWN")[:80],
            "game_phase": game_phase,
            "delivery_mode": delivery_mode,
            "local_appended_at": local_appended_at,
            "collector_http_started_at": collector_http_started_at,
            "source_metadata": raw.get("source_metadata") if isinstance(raw.get("source_metadata"), dict) else None,
            "commit_acknowledged_at": raw.get("commit_acknowledged_at"),
            "observation_to_commit_ms": (float(raw["observation_to_commit_ms"])
                if isinstance(raw.get("observation_to_commit_ms"), (int, float))
                and math.isfinite(float(raw["observation_to_commit_ms"]))
                and float(raw["observation_to_commit_ms"]) >= 0 else None),
        })
    return observations


@app.post("/api/collector/round-observed", tags=["collector"])
async def collector_round_observed(request: Request):
    """Acknowledge only after verified priority rule processing is decision-ready."""
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    stream_id = getattr(request.app.state, "round_stream_id", None)
    if inbox is None or not stream_id:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_inbox_unavailable",
        })
    request_started_ns = time.perf_counter_ns()
    received_at = datetime.now(timezone.utc).isoformat()
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "WINNER_DIRECT_PAGE_HISTORY":
            raise ValueError("round observation source must be WINNER_DIRECT_PAGE_HISTORY")
        observations = _validate_direct_collector_observations(body)
        events = []
        for observation in observations:
            event = observed_round_event(observation, stream_id)
            event["correlation_id"] = str(event.get("round_id") or "")
            event["api_received_at"] = received_at
            event["api_receipt_to_sse_publication_ms"] = None
            events.append(event)
        validation_finished_ns = time.perf_counter_ns()
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except ClientDisconnect:
        log.warning("[COLLECTOR_EVENTS] collector disconnected before completing the observation request; its durable local outbox will retry")
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "collector_request_disconnected_retry",
        })
    except Exception as exc:
        log.exception("[COLLECTOR_EVENTS] could not validate direct page observation category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_validation_failed",
        })
    hub = request.app.state.round_distribution
    for event in events:
        correlation_id = str(event.get("correlation_id") or event.get("round_id") or "")
        hub.record_priority_latency("collector_request_validation_ms",
            f"{correlation_id}:collector_request_validation_ms",
            (validation_finished_ns - request_started_ns) / 1_000_000.0)
    # A verified observation must reach the ordered Rules Engine path before
    # the collector is allowed to advance to PostgreSQL persistence. That
    # keeps the next target round from becoming visible before its source rule
    # decision has been made.
    rules_engine = getattr(request.app.state, "rules_engine", None)
    if any(event.get("continuity_status") in {"VERIFIED", "GAP_BEFORE"} for event in events) and rules_engine is None:
        log.error("[RULES_PRIORITY] Rules Engine unavailable; collector will retry durable observations")
        return JSONResponse(status_code=503, content={"ok": False,
            "error": "priority_rules_engine_unavailable"})
    # Persist the replay copy before acknowledging the collector. The
    # collector's fsynced outbox remains the source for retry/recovery.
    append_started = time.perf_counter()
    try:
        accepted = await run_round_observation_inbox(
            request.app, inbox.append, events, observations, received_at=received_at,
            priority=True)
        inbox_append_ms = round((time.perf_counter() - append_started) * 1000.0, 3)
        inbox_durable_at = datetime.now(timezone.utc).isoformat()
        wakeup = getattr(request.app.state, "behaviour_round_wakeup", None)
        if wakeup is not None:
            wakeup.set()
    except ObservationIdentityConflict as exc:
        log.error("[COLLECTOR_EVENTS] API inbox rejected an identity conflict; collector retains it: %s", exc)
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
    except ObservationInboxCapacityError as exc:
        log.error("[COLLECTOR_EVENTS] API inbox is full; collector retains its durable outbox: %s", exc)
        return JSONResponse(status_code=507, content={"ok": False, "error": "round_observation_inbox_capacity_reached"})
    except Exception as exc:
        log.exception("[COLLECTOR_EVENTS] API inbox append failed; collector retains its durable outbox category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_observation_inbox_write_failed",
        })

    priority_events = [event for event in accepted["events"]
                       if event.get("continuity_status") in {"VERIFIED", "GAP_BEFORE"}]
    latest_event = accepted["events"][-1] if accepted.get("events") else None
    request.app.state.latest_verified_live_observation = (
        dict(latest_event)
        if isinstance(latest_event, dict)
        and latest_event.get("continuity_status") == "VERIFIED"
        and str(latest_event.get("delivery_mode") or "").upper() == "LIVE"
        else None)
    # Preserve the original verified round.observed envelope across its later
    # PostgreSQL commit. Behaviour may finish analyzing that same fresh round
    # a moment later; retaining this evidence lets SHADOW validate the still
    # open immediate target without reconstructing or trusting a DB snapshot.
    request.app.state.latest_live_observed_envelope = (
        dict(latest_event)
        if isinstance(latest_event, dict)
        and latest_event.get("event_type") == "round.observed"
        and latest_event.get("durability") == "PENDING"
        and latest_event.get("continuity_status") == "VERIFIED"
        and str(latest_event.get("delivery_mode") or "").upper() == "LIVE"
        else None)
    for event in priority_events:
        metadata = event.get("source_metadata")
        if (isinstance(metadata, dict) and metadata.get("source") == "ORBIT_DATA_WATCH_API"
                and event.get("delivery_mode") == "LIVE"
                and event.get("continuity_status") == "VERIFIED"):
            request.app.state.latest_watch_observation = dict(event)
    client = getattr(request, "client", None)
    peer = client.host if client is not None else ""
    try:
        peer_is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_is_loopback = False
    request_url = getattr(request, "url", None)
    request_host = str(getattr(request_url, "hostname", "") or "").strip("[]").lower()
    peer_is_loopback = peer_is_loopback and request_host in {"127.0.0.1", "::1", "localhost"}
    _remember_collector_detection_timestamps(
        request.app, observations, peer_is_loopback=peer_is_loopback)
    completion_waiters = []
    for event in priority_events:
        waiter = _round_event_completion_future(request.app, "observed", event)
        if waiter is not None:
            completion_waiters.append((event, *waiter))
    if len(completion_waiters) != len(priority_events):
        for _, key, future in completion_waiters:
            if not future.done():
                _forget_round_event_completion(request.app, key)
        log.error("[ROUND_DECISION_BARRIER] verified observation is missing a completion identity")
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "round_decision_tracking_unavailable_retry",
        })
    if not _enqueue_round_events(request.app, "observed", priority_events,
                                 request_started_ns=request_started_ns,
                                 validation_finished_ns=validation_finished_ns):
        queued_ids = getattr(request.app.state, "round_event_queued_ids", set())
        for _, key, future in completion_waiters:
            if key not in queued_ids and not future.done():
                _forget_round_event_completion(request.app, key)
        log.error("[ROUND_EVENT_WORKER] observed queue full; collector will retry count=%s",
                  len(priority_events))
        return JSONResponse(status_code=503, content={"ok": False,
            "error": "round_event_processing_queue_full"})

    # Publish the observation to feed consumers while the priority worker
    # produces the decision. This SSE is informational; only the worker result
    # below is allowed to acknowledge the collector's observed stage.
    published_at = None
    publication_ms = 0.0
    try:
        for event in accepted["events"]:
            await hub.publish(event)
            fanout_finished_ns = time.perf_counter_ns()
            published_at = datetime.now(timezone.utc).isoformat()
            observation_id = str(event.get("observation_id") or event.get("round_id") or "")
            publication_ms = round((fanout_finished_ns - request_started_ns) / 1_000_000.0, 3)
            hub.record_priority_latency("api_receipt_to_sse_publication_ms",
                str(event.get("event_id") or observation_id), publication_ms)
    except Exception as exc:
        if hub is not None:
            hub.record_publication_error(type(exc).__name__)
        log.exception("[ROUND_DISTRIBUTION] event is durable and queued for Rules Engine; fanout deferred category=%s",
                      type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False,
            "error": "round_observation_fanout_deferred"})

    collector_timing = body.get("collector_timing") if isinstance(body.get("collector_timing"), list) else []
    for item in collector_timing:
        if not isinstance(item, dict) or not item.get("round_id"):
            continue
        round_id = str(item["round_id"])
        for field, metric in (
            ("page_mutation_to_extraction_ms", "bet_page_mutation_to_multiplier_extraction_ms"),
            ("page_extraction_ms", "bet_page_multiplier_extraction_ms"),
            ("node_receive_to_validation_ms", "collector_node_receive_to_validation_ms"),
            ("validation_to_api_dispatch_ms", "collector_validation_to_api_dispatch_ms"),
        ):
            value = item.get(field)
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
                hub.record_priority_latency(metric, f"{round_id}:{metric}", float(value))

    for event in accepted["events"]:
        correlation_id = str(event.get("correlation_id") or event.get("round_id") or "")
        hub.record_priority_latency("api_durable_inbox_append_ms",
            f"{correlation_id}:api_durable_inbox_append_ms", float(inbox_append_ms))
        hub.record_priority_latency("api_receive_to_sse_publication_ms",
            f"{correlation_id}:api_receive_to_sse_publication_ms", float(publication_ms))

    decision_wait_started = time.perf_counter()
    waiter_futures = {future for _, _, future in completion_waiters}
    if waiter_futures:
        _, pending_futures = await asyncio.wait(
            waiter_futures, timeout=ROUND_OBSERVED_DECISION_WAIT_SECONDS)
        if pending_futures:
            pending_ids = [str(event.get("round_id") or "")
                           for event, _, future in completion_waiters
                           if future in pending_futures]
            log.error("[ROUND_DECISION_BARRIER] decision is still pending; collector must retry rounds=%s timeout_s=%.1f",
                      ",".join(pending_ids), ROUND_OBSERVED_DECISION_WAIT_SECONDS)
            return JSONResponse(status_code=503, content={
                "ok": False, "error": "rules_engine_decision_pending_retry",
                "round_ids": pending_ids,
            })
    decision_wait_ms = round((time.perf_counter() - decision_wait_started) * 1000.0, 3)
    decision_ready_at = datetime.now(timezone.utc).isoformat()
    decision_ready_by_round = {}
    for event, _, future in completion_waiters:
        result = future.result()
        round_id = str(event.get("round_id") or "")
        decision_ready_by_round[round_id] = bool(result.get("decision_ready"))
        hub.record_priority_latency("collector_ack_wait_for_rules_decision_ms",
            f"{round_id}:collector_ack_wait_for_rules_decision_ms",
            decision_wait_ms)

    priority_evaluations = [
        {"round_id": str(event.get("round_id") or ""),
         "status": ("BOUNDARY_STATE_ADVANCED" if event.get("continuity_status") == "GAP_BEFORE"
                    and decision_ready_by_round.get(str(event.get("round_id") or ""))
                    else "DECISION_READY" if decision_ready_by_round.get(
                        str(event.get("round_id") or ""))
                    else "PROCESSED_NO_ACTIVE_SESSION"
                    if event.get("continuity_status") in {"VERIFIED", "GAP_BEFORE"}
                    else "NOT_DISPATCHED_UNVERIFIED")}
        for event in accepted["events"]
    ]
    log.info("[ROUND_RECEIVE] correlation_ids=%s accepted=%s queued=%s validation_ms=%.3f inbox_ms=%.3f api_to_sse_ms=%.3f decision_wait_ms=%.3f",
             ",".join(str(event.get("correlation_id") or event.get("round_id") or "")
                     for event in accepted["events"]),
             len(observations), len(accepted["events"]),
             (validation_finished_ns - request_started_ns) / 1_000_000.0,
             inbox_append_ms, publication_ms, decision_wait_ms)
    priority_timings = [
        {"round_id": str(event.get("round_id") or ""),
         "status": ("DECISION_READY" if decision_ready_by_round.get(
                        str(event.get("round_id") or ""))
                    else "PROCESSED_NO_ACTIVE_SESSION"),
         "collector_ack_wait_for_rules_decision_ms": decision_wait_ms}
        for event, _, _ in completion_waiters
    ]
    return {"ok": True, "status": "ROUND_OBSERVED_RULES_DECISION_READY",
            "accepted": len(observations), "new_events": len(accepted["events"]),
            "duplicate_committed": accepted["duplicates"], "api_received_at": received_at,
            "api_durable_inbox_at": inbox_durable_at,
            "api_published_at": published_at,
            "api_decision_ready_at": decision_ready_at,
            "api_receipt_to_sse_publication_ms": publication_ms,
            "correlation_ids": [str(event.get("correlation_id") or event.get("round_id") or "")
                                for event in accepted["events"]],
            "rule_evaluation_stage": "DECISION_READY_BEFORE_COLLECTOR_ACK",
            "rules_engine_evaluations": priority_evaluations,
            "priority_timings_ms": priority_timings,
            "durable_inbox_append_ms": inbox_append_ms,
            "measurement_availability": {
                "bet_page_round_available_at": "UNAVAILABLE_PROVIDER_EVENT_NOT_EXPOSED",
                "page_to_collector_monotonic_latency": "UNAVAILABLE_SEPARATE_CLOCK_DOMAINS",
                "other_consumer_client_ack": "UNAVAILABLE_NO_CLIENT_ACK_PROTOCOL",
            },
            "events": [{"event_id": event["event_id"], "observation_id": event["observation_id"]}
                       for event in accepted["events"]]}


@app.post("/api/collector/round-persisted", tags=["collector"])
async def collector_round_persisted(request: Request):
    """Write the replayable committed event after PostgreSQL has acknowledged the round."""
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    inbox = getattr(request.app.state, "round_observation_inbox", None)
    if repository is None or not repository.database_url:
        return JSONResponse(status_code=503, content={"ok": False, "error": "authoritative_postgresql_unavailable"})
    stream_id = getattr(request.app.state, "round_stream_id", None)
    if not stream_id:
        return JSONResponse(status_code=503, content={"ok": False, "error": "round_distribution_stream_unavailable"})
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "WINNER_CANONICAL_POSTGRESQL_COMMIT":
            raise ValueError("round persisted source must identify a collector PostgreSQL commit")
        observations = _validate_direct_collector_observations(body)
        for observation in observations:
            observation["commit_acknowledged_at"] = observation.get("commit_acknowledged_at") or body.get("commit_acknowledged_at")
            observation["delivery_mode"] = observation.get("delivery_mode") or "LIVE"
        commit_lock = request.app.state.round_event_commit_lock
        async with commit_lock:
            distributions = await asyncio.to_thread(
                repository.persist_round_distribution_batch, observations,
                int(os.environ.get("ROUND_STREAM_RETENTION", "100000")), stream_id=stream_id)
            accepted = []
            receipts = []
            ordered_distributions = sorted(distributions,
                key=lambda item: int(item.get("stream_sequence") or item.get("committed_sequence") or 0))
            for distribution in ordered_distributions:
                observation_id = str(distribution.get("observation_id") or distribution.get("round_id") or "")
                receipts.append((observation_id,
                                 distribution.get("committed_sequence") or distribution["stream_sequence"],
                                 distribution))
                accepted.append({"event_id": distribution["event_id"],
                                 "observation_id": observation_id,
                                 "round_id": distribution["round_id"],
                                 "committed_sequence": distribution["committed_sequence"]})
            if inbox is not None:
                await run_round_observation_inbox(request.app, inbox.mark_committed_many,
                                                   receipts, priority=True)
            latest_distribution = ordered_distributions[-1] if ordered_distributions else None
            request.app.state.latest_verified_live_observation = (
                dict(latest_distribution)
                if isinstance(latest_distribution, dict)
                and latest_distribution.get("continuity_status") == "VERIFIED"
                and str(latest_distribution.get("delivery_mode") or "").upper() == "LIVE"
                else None)
            # Advance the cache as soon as the PostgreSQL transaction and
            # durable inbox acknowledgement succeed, even if volatile queue
            # backpressure makes the collector retry the later processing step.
            latest_marker = getattr(request.app.state, "latest_committed_round_marker", None)
            for distribution in ordered_distributions:
                try:
                    marker = (
                        int(distribution["round_index"]),
                        str(distribution["round_id"]),
                        str(distribution.get("platform_timestamp") or distribution.get("stored_at") or ""),
                        float(distribution["multiplier"]),
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if latest_marker is None or marker[0] >= int(latest_marker[0]):
                    latest_marker = marker
            request.app.state.latest_committed_round_marker = latest_marker
            if not _enqueue_round_events(request.app, "committed", ordered_distributions):
                log.error("[ROUND_EVENT_WORKER] committed queue full; collector will retry count=%s",
                          len(ordered_distributions))
                return JSONResponse(status_code=503, content={"ok": False,
                    "error": "round_event_processing_queue_full"})
        for distribution in ordered_distributions:
            await request.app.state.round_distribution.publish(distribution)
        # The durable round stream remains the replay source. This bounded
        # WebSocket wakeup lets the existing dashboard refresh immediately.
        if ordered_distributions:
            pending = getattr(request.app.state, "round_ui_broadcast_tasks", None)
            if pending is None:
                pending = set()
                request.app.state.round_ui_broadcast_tasks = pending
            if len(pending) < 32:
                task = asyncio.create_task(manager.broadcast({
                    "type": "history:verified-rounds",
                    "rounds": ordered_distributions,
                }))
                pending.add(task)
                task.add_done_callback(pending.discard)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        log.exception("[ROUND_DISTRIBUTION] committed-event handoff failed category=%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "error": "committed_event_handoff_unavailable"})
    return {"ok": True, "accepted": len(accepted), "events": accepted}


@app.post("/api/collector/game-phase", tags=["collector"])
async def collector_game_phase_observed(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("source") != "OWNED_AVIATOR_GAME_FRAME_READ_ONLY":
            raise ValueError("game phase source must be the owned read-only Aviator frame")
        phase = _validate_collector_game_phase(body, require_source=True)
        latest = getattr(request.app.state, "latest_provider_phase_observation", None)
        if latest:
            old_time = _utc_timestamp(latest.get("observed_at"))
            new_time = _utc_timestamp(phase.get("observed_at"))
            if (old_time and new_time and new_time < old_time) or (
                    int(phase.get("source_round_index") or 0)
                    < int(latest.get("source_round_index") or 0)):
                return {"ok": True, "accepted": False, "status": "STALE_EVENT_IGNORED"}
        request.app.state.latest_provider_phase_observation = phase
        signal_bridge = getattr(request.app.state, "shadow_signal_bridge", None)
        if signal_bridge is not None and phase.get("phase") == "BETTING_OPEN":
            await signal_bridge.recover_for_open_phase(request.app, phase)
        return {"ok": True, "accepted": True,
                "status": "RECORDED" if phase.get("phase") != "UNKNOWN" else "RECORDED_UNKNOWN"}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        log.warning("collector game-phase observation rejected: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "game_phase_persistence_unavailable",
        })


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([*settings.cors_origins, *settings.round_stream_allowed_origins])),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(history.router)
app.include_router(health.router)
app.include_router(shadow.router)
app.include_router(stability_api.router)
app.include_router(round_distribution_api.router)
app.include_router(adie_api.router)
app.include_router(rules_engine_api.router)
app.include_router(signals_api.router)
app.include_router(developer_console_api.router)


@app.get("/", tags=["system"])
async def root():
    """Human-friendly landing response for direct browser visits."""
    return {
        "ok": True,
        "service": "Winner Predict API",
        "status": "running",
        "docs": "/docs",
        "frontend": "http://localhost:5174",
    }


@app.post("/api/auth/local-browser-session", tags=["system"], include_in_schema=False)
async def create_local_browser_session(request: Request):
    """Create a short-lived HttpOnly owner session for the local dashboard only."""
    if not settings.api_key:
        return JSONResponse(status_code=409,
                            content={"ok": False, "error": "owner_api_key_not_configured"})
    origin = request.headers.get("origin")
    allowed_origins = set(settings.cors_origins or []) | set(settings.round_stream_allowed_origins or [])
    if (not _is_loopback_request(request) or origin not in allowed_origins
            or not _is_loopback_origin(origin)):
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "local_browser_session_not_allowed"})

    expires_at = int(time.time()) + LOCAL_BROWSER_SESSION_TTL_SECONDS
    cookie = _local_browser_session_value(expires_at, uuid.uuid4().hex, origin)
    response = JSONResponse({
        "ok": True,
        "session": "LOCAL_BROWSER",
        "expires_in_seconds": LOCAL_BROWSER_SESSION_TTL_SECONDS,
    })
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        LOCAL_BROWSER_SESSION_COOKIE,
        cookie,
        max_age=LOCAL_BROWSER_SESSION_TTL_SECONDS,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/health", tags=["system"])
async def health(request: Request):
    """Liveness/readiness report used by the frontend and process supervisor."""
    database = {"status": "unavailable", "error": "repository not initialized"}
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is not None:
        backend_name = "postgresql" if repository.database_url else "sqlite"
        monitor = getattr(request.app.state, "system_health", None)
        try:
            # /health is polled every 10 seconds. Do not acquire a PostgreSQL
            # connection on this hot path; thread-pool/pool saturation made
            # the liveness probe itself time out. Report the monitor's latest
            # bounded-age DB heartbeat instead. /health/ready remains the
            # direct dependency probe for operators and readiness checks.
            components = monitor.snapshot().get("components", {}) if monitor else {}
            db_component = components.get("database", {})
            heartbeat_age = db_component.get("age_s")
            db_fresh = (db_component.get("state") == "HEALTHY"
                        and isinstance(heartbeat_age, (int, float))
                        and heartbeat_age <= 60)
            database = {"status": "ok" if db_fresh else
                        "stale" if db_component.get("last_success") else "unknown",
                        "backend": backend_name}
        except Exception:
            database = {"status": "unknown", "backend": backend_name}
    ready = bool(getattr(request.app.state, "ready", False))
    database_ok = database["status"] == "ok"
    payload = {
        "ok": ready and database_ok,
        "service": "winner-predict-backend",
        "auth_required": bool(settings.api_key) or not _is_loopback_request(request),
        "api_key_configured": bool(settings.api_key),
        "local_browser_session_available": bool(settings.api_key) and _is_loopback_request(request),
        "public_read_only": False,
        "public_access": False,
        "status": "healthy" if ready and database_ok else "degraded",
        "backend": {
            "status": "ok" if ready else "starting",
            "pid": os.getpid(),
            "instance_id": getattr(request.app.state, "instance_id", None),
            "started_at": getattr(request.app.state, "started_at", None),
            "uptime_seconds": round(
                time.monotonic() - getattr(request.app.state, "started_monotonic", time.monotonic()),
                3,
            ),
        },
        "single_instance": (getattr(request.app.state, "instance_lock", None).status()
                            if getattr(request.app.state, "instance_lock", None) else None),
        "database": database,
        # Full lifecycle.status() queries and serializes up to 5,000 history
        # rows. Keep this frequently-polled endpoint lightweight; the complete
        # diagnostic history remains available from /api/backend/stability.
        "stability": ({
            "status": "RUNNING",
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "started_at": getattr(request.app.state, "started_at", None),
            "uptime_seconds": round(
                time.monotonic() - getattr(request.app.state, "started_monotonic", time.monotonic()),
                3,
            ),
        } if getattr(request.app.state, "lifecycle", None) else None),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("health check backend=%s database=%s",
             payload["backend"]["status"], database["status"])
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)


@app.get("/health/live", tags=["system"])
async def health_live():
    """Process liveness. A 200 means FastAPI can serve requests."""
    return {"ok": True, "status": "ONLINE", "checked_at": datetime.now(timezone.utc).isoformat()}


@app.get("/health/ready", tags=["system"])
async def health_ready(request: Request):
    """Sanitized dependency readiness for monitoring and API tests."""
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    database = "DISCONNECTED"
    backend = "unknown"
    if repository is not None:
        backend = "postgresql" if getattr(repository, "database_url", None) else "sqlite"
        try:
            def check_database():
                with repository.connect() as conn:
                    conn.execute("SELECT 1").fetchone()

            await asyncio.to_thread(check_database)
            database = "CONNECTED"
        except Exception:
            database = "DISCONNECTED"
    ready = bool(getattr(request.app.state, "ready", False)) and database == "CONNECTED"
    payload = {"ok": ready, "status": "ONLINE" if ready else "DEGRADED",
               "database": {"status": database, "backend": backend},
               "checked_at": datetime.now(timezone.utc).isoformat()}
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@app.get("/api/system/status", tags=["system"])
async def system_status(request: Request):
    """Expose non-sensitive runtime configuration and canonical history size."""
    wp = request.app.state.wp
    quality = wp.quality.model_dump() if hasattr(wp.quality, "model_dump") else dict(wp.quality or {})
    cfg = wp.settings
    return {
        "ok": True,
        "configuration": {
            "api_security_enabled": bool(cfg.api_key),
            "api_key_configured": bool(cfg.api_key),
            "public_access_enabled": False,
            "public_read_routes": ["GET /api/v1/rounds", "GET /api/v1/rounds/latest",
                                   "GET /api/v1/signals/latest/action"],
        },
        "dataset": {
            "source": cfg.data_path.name,
            "valid_records": quality.get("valid_records", len(wp.rounds)),
            "first_timestamp": quality.get("first_timestamp"),
            "last_timestamp": quality.get("last_timestamp"),
            "last_round_index": quality.get("last_round_index"),
        },
    }


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and origin not in set(settings.cors_origins):
        await websocket.close(code=4403, reason="ORIGIN_NOT_ALLOWED")
        return
    local_request = _is_loopback_request(websocket)
    if websocket.url.scheme != "wss" and not local_request:
        await websocket.close(code=4403, reason="HTTPS_REQUIRED")
        return
    if not settings.api_key and not local_request:
        await websocket.close(code=1013, reason="API_KEY_NOT_CONFIGURED")
        return
    if settings.api_key:
        supplied_header = websocket.headers.get("authorization", "")
        supplied = supplied_header[7:].strip() if supplied_header.startswith("Bearer ") else ""
        if not supplied:
            protocols = [part.strip() for part in websocket.headers.get("sec-websocket-protocol", "").split(",")]
            encoded = next((part[len("auth."):] for part in protocols if part.startswith("auth.")), "")
            if encoded:
                try:
                    supplied = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    supplied = ""
        allowed_origins = set(settings.cors_origins or [])
        local_cookie_session = (_has_local_browser_session(websocket)
                                and origin in allowed_origins and _is_loopback_origin(origin))
        owner_key_valid = hmac.compare_digest(supplied, settings.api_key)
        if not owner_key_valid and not local_cookie_session:
            await websocket.close(code=1008, reason="Invalid API credentials")
            return
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "system_status", "status": "connected", "label": "ROUND HISTORY AND SAFETY"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "API_KEY or scoped token",
        "description": (
        "Use the owner API_KEY on protected application routes. External signal and "
            "round-stream routes use their documented scoped bearer token. Public "
            "committed-round and action-only signal reads do not require Authorization."
        ),
    }
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            verb = method.upper()
            if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            if (verb, path) in PUBLIC_API_READS:
                operation["security"] = []
                operation["x-public-access"] = True
                operation["x-api-key-required"] = False
            elif path.startswith("/api/"):
                operation["security"] = [{"BearerAuth": []}]
                scoped = _uses_scoped_bearer(verb, path)
                operation["x-api-key-required"] = not scoped
                operation["x-authentication"] = (
                    "Scoped bearer token required" if scoped else "Owner API_KEY required"
                )
            else:
                operation["security"] = []
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    # Reload mode intentionally remains opt-in through the uvicorn CLI. An
    # implicit reloader creates an extra process and recurring brief outages.
    uvicorn.run(app, host=settings.backend_host, port=settings.backend_port,
                reload=False, log_level="info")
