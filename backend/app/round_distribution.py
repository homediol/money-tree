"""Bounded in-process fanout for the PostgreSQL-backed round stream."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class RoundStreamResyncRequired(Exception):
    def __init__(self, detail: dict[str, Any]):
        super().__init__("round_stream_resync_required")
        self.detail = detail


@dataclass
class Subscriber:
    subscription_id: str
    token_id: str
    project_id: str
    queue: asyncio.Queue
    pending_event_ids: set[str]
    closed: bool = False


class RoundDistributionHub:
    """Slow clients get disconnected with an explicit overflow notice; producers never wait on them."""

    QUEUE_LIMIT = 256
    MAX_CONNECTIONS_PER_TOKEN = 4
    MAX_CONNECTIONS_TOTAL = 200
    REQUESTS_PER_MINUTE = 120

    def __init__(self):
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, Subscriber] = {}
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=2000))
        self._measured: dict[str, set[str]] = defaultdict(set)
        self._measured_order: dict[str, deque[str]] = defaultdict(deque)
        self._committed_event_ids: set[str] = set()
        self._committed_event_order: deque[str] = deque()
        self._last_published: dict[str, Any] | None = None
        self._delivery_lag_ms: deque[float] = deque(maxlen=2000)
        self._reconnect_backlogs: deque[int] = deque(maxlen=1000)
        self._shutdown = False
        self._disconnects = 0
        self._overflows = 0

    def allow_request(self, token_id: str, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else float(now)
        bucket = self._requests[str(token_id)]
        while bucket and bucket[0] <= now - 60.0:
            bucket.popleft()
        if len(bucket) >= self.REQUESTS_PER_MINUTE:
            return False, max(1, math.ceil(60.0 - (now - bucket[0])))
        bucket.append(now)
        return True, 0

    @staticmethod
    def _cursor_error(reason: str, snapshot: dict[str, Any]) -> RoundStreamResyncRequired:
        return RoundStreamResyncRequired({
            "ok": False,
            "error": "resync_required",
            "reason": reason,
            "stream_id": snapshot["stream_id"],
            "retention": {
                "earliest_sequence": snapshot.get("earliest_sequence"),
                "latest_sequence": snapshot.get("latest_sequence", 0),
            },
            "resync_after_sequence": max(0, int(snapshot.get("earliest_sequence") or 1) - 1),
        })

    async def reserve_subscriber(self, token: dict[str, Any]) -> Subscriber:
        async with self._lock:
            active = sum(1 for item in self._subscribers.values() if item.token_id == token["token_id"])
            if active >= self.MAX_CONNECTIONS_PER_TOKEN:
                raise PermissionError("project_connection_limit_reached")
            if len(self._subscribers) >= self.MAX_CONNECTIONS_TOTAL:
                raise PermissionError("server_connection_limit_reached")
            subscriber = Subscriber(uuid.uuid4().hex, token["token_id"], token["project_id"],
                                    asyncio.Queue(maxsize=self.QUEUE_LIMIT), set())
            self._subscribers[subscriber.subscription_id] = subscriber
            return subscriber

    async def prepare_replay(self, repository, subscriber: Subscriber, cursor: int | None,
                             requested_stream_id: str | None) -> tuple[dict[str, Any], int, int]:
        snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
        latest = int(snapshot.get("latest_sequence") or 0)
        earliest = snapshot.get("earliest_sequence")
        supplied_cursor = cursor is not None
        if requested_stream_id and str(requested_stream_id) != snapshot["stream_id"]:
            raise self._cursor_error("stream_id_changed", snapshot)
        if cursor is None:
            cursor = latest
        if requested_stream_id is None and supplied_cursor:
            raise self._cursor_error("stream_id_required_for_resume", snapshot)
        if cursor > latest:
            raise self._cursor_error("cursor_ahead_of_stream", snapshot)
        if earliest is not None and cursor < int(earliest) - 1:
            raise self._cursor_error("cursor_before_retention", snapshot)
        reconnect_backlog = await asyncio.to_thread(
            repository.count_round_distribution_after,
            snapshot["stream_id"], int(cursor), latest)
        async with self._lock:
            self._reconnect_backlogs.append(reconnect_backlog)
        return snapshot, latest, reconnect_backlog

    async def subscribe(self, repository, token: dict[str, Any], cursor: int | None,
                        requested_stream_id: str | None) -> tuple[Subscriber, dict[str, Any], int, int]:
        subscriber = await self.reserve_subscriber(token)
        try:
            snapshot, boundary, reconnect_backlog = await self.prepare_replay(
                repository, subscriber, cursor, requested_stream_id)
            return subscriber, snapshot, boundary, reconnect_backlog
        except Exception:
            await self.unsubscribe(subscriber)
            raise

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        async with self._lock:
            if self._subscribers.pop(subscriber.subscription_id, None) is not None:
                self._disconnects += 1

    async def revoke_token(self, token_id: str) -> None:
        async with self._lock:
            for subscriber in self._subscribers.values():
                if subscriber.token_id != str(token_id) or subscriber.closed:
                    continue
                subscriber.closed = True
                while not subscriber.queue.empty():
                    try:
                        subscriber.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                subscriber.queue.put_nowait({"event_type": "auth.revoked", "reason": "project_token_revoked"})

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            event_type = event.get("event_type")
            observation_id = str(event.get("observation_id") or event.get("round_id") or "")
            if event_type == "round.persisted":
                self._last_published = dict(event)
                self._remember_committed(observation_id)
            elif event_type == "round.observed" and observation_id in self._committed_event_ids:
                return
            self._record_event_latencies(event)
            for subscriber in tuple(self._subscribers.values()):
                if subscriber.closed:
                    continue
                try:
                    subscriber.queue.put_nowait(dict(event))
                    if event_type == "round.observed" and observation_id:
                        subscriber.pending_event_ids.add(observation_id)
                except asyncio.QueueFull:
                    self._overflows += 1
                    subscriber.closed = True
                    subscriber.pending_event_ids.clear()
                    while not subscriber.queue.empty():
                        try:
                            subscriber.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    notice = {"event_type": "stream.overflow", "stream_id": event.get("stream_id"),
                              "reason": "client_queue_full", "resume_with_rest": True}
                    subscriber.queue.put_nowait(notice)

    def remember_replayed_commit(self, event: dict[str, Any]) -> None:
        """Suppress a queued pending duplicate after its committed replay was sent."""
        observation_id = str(event.get("observation_id") or event.get("round_id") or "")
        if observation_id:
            self._remember_committed(observation_id)

    def _remember_committed(self, event_id: str) -> None:
        if not event_id or event_id in self._committed_event_ids:
            return
        self._committed_event_ids.add(event_id)
        self._committed_event_order.append(event_id)
        if len(self._committed_event_order) > 10000:
            self._committed_event_ids.discard(self._committed_event_order.popleft())

    async def was_committed(self, event_id: str) -> bool:
        async with self._lock:
            return str(event_id) in self._committed_event_ids

    async def shutdown(self) -> None:
        async with self._lock:
            self._shutdown = True
            for subscriber in tuple(self._subscribers.values()):
                subscriber.closed = True
                while not subscriber.queue.empty():
                    try:
                        subscriber.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                subscriber.queue.put_nowait({"event_type": "server.shutdown",
                                             "reason": "backend_shutdown",
                                             "stream_id": (self._last_published or {}).get("stream_id")})

    async def reset_history_epoch(self, stream_id: str, epoch: int) -> None:
        """Close old-stream subscribers and discard disposable per-stream metrics."""
        async with self._lock:
            notice = {"event_type": "stream.overflow", "stream_id": str(stream_id),
                      "reason": "history_epoch_reset", "history_epoch": int(epoch),
                      "resume_with_rest": True}
            for subscriber in self._subscribers.values():
                subscriber.closed = True
                subscriber.pending_event_ids.clear()
                while not subscriber.queue.empty():
                    try:
                        subscriber.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                subscriber.queue.put_nowait(dict(notice))
            self._committed_event_ids.clear()
            self._committed_event_order.clear()
            self._last_published = None
            self._latencies.clear()
            self._measured.clear()
            self._measured_order.clear()
            self._delivery_lag_ms.clear()
            self._reconnect_backlogs.clear()

    def _record_once(self, stage: str, event_id: str, value: float) -> None:
        known = self._measured[stage]
        if event_id in known:
            return
        known.add(event_id)
        order = self._measured_order[stage]
        order.append(event_id)
        if len(order) > 10000:
            known.discard(order.popleft())
        if math.isfinite(value) and value >= 0:
            self._latencies[stage].append(value)

    @staticmethod
    def _timestamp_ms(value: Any) -> float | None:
        if not value:
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() * 1000.0
        except (TypeError, ValueError, OverflowError):
            return None

    def _record_event_latencies(self, event: dict[str, Any]) -> None:
        observation_id = str(event.get("observation_id") or event.get("round_id") or "")
        event_id = str(event.get("event_id") or observation_id)
        observed = self._timestamp_ms(event.get("observed_at"))
        if event.get("event_type") == "round.observed" and observation_id:
            api_elapsed = event.get("api_receipt_to_sse_publication_ms")
            if isinstance(api_elapsed, (int, float)) and math.isfinite(float(api_elapsed)):
                self._record_once("api_receipt_to_sse_publication_ms", event_id, float(api_elapsed))
        if event.get("event_type") == "round.persisted" and observation_id:
            commit_ack = self._timestamp_ms(event.get("commit_acknowledged_at"))
            commit_latency = event.get("observation_to_commit_ms")
            if (event.get("commit_latency_measurement_clock") == "COLLECTOR_MONOTONIC"
                    and isinstance(commit_latency, (int, float))
                    and math.isfinite(float(commit_latency))):
                self._record_once("observation_to_commit_ms", observation_id, float(commit_latency))
            if commit_ack is not None:
                # Collector and API clocks may differ. Keep this diagnostic
                # only when the event carries a backend-local monotonic span.
                handoff = event.get("commit_ack_to_sse_publication_ms")
                if isinstance(handoff, (int, float)) and math.isfinite(float(handoff)):
                    self._record_once("commit_to_publish_ms", observation_id, float(handoff))

    def record_delivery(self, event: dict[str, Any]) -> None:
        published = self._timestamp_ms(event.get("published_at"))
        if published is not None:
            self._delivery_lag_ms.append(max(0.0, datetime.now(timezone.utc).timestamp() * 1000.0 - published))

    @staticmethod
    def _summary(values: deque[float]) -> dict[str, Any]:
        ordered = sorted(values)
        if not ordered:
            return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        def percentile(p: float) -> float:
            return round(ordered[min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))], 2)
        return {"samples": len(ordered), "p50_ms": percentile(0.50), "p95_ms": percentile(0.95),
                "max_ms": round(ordered[-1], 2)}

    async def metrics(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "connected_clients": len(self._subscribers),
                "client_queue_capacity": self.QUEUE_LIMIT,
                "client_queue_depth": sum(item.queue.qsize() for item in self._subscribers.values()),
                "client_overflows": self._overflows,
                "client_disconnects": self._disconnects,
                "shutdown": self._shutdown,
                "latency_ms": {key: self._summary(value) for key, value in self._latencies.items()},
                "delivery_lag_ms": self._summary(self._delivery_lag_ms),
                "reconnect_backlog_events": {
                    "samples": len(self._reconnect_backlogs),
                    "last": self._reconnect_backlogs[-1] if self._reconnect_backlogs else None,
                    "p95": (sorted(self._reconnect_backlogs)[max(0, math.ceil(0.95 * len(self._reconnect_backlogs)) - 1)]
                            if self._reconnect_backlogs else None),
                },
                "last_published_round": self._last_published,
            }
