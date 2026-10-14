from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.requests import ClientDisconnect

from app.api import round_distribution as round_api
from app.round_distribution import RoundDistributionHub
from app.round_observation_inbox import (
    ObservationIdentityConflict,
    ObservationInboxCapacityError,
    RoundObservationInbox,
)
from main import collector_round_observed


def observation(round_id: str, multiplier: float = 1.25):
    observed_at = datetime.now(timezone.utc).isoformat()
    row = {
        "observation_id": round_id,
        "round_id": round_id,
        "multiplier": multiplier,
        "observed_at": observed_at,
        "timestamp": observed_at,
        "platform_timestamp": None,
        "platform_round_id": None,
        "platform_round_index": None,
        "round_index": 10,
        "continuity_verified": False,
        "gap_before": False,
        "identity_confidence": "UNKNOWN",
        "round_identity_type": "UNKNOWN",
        "delivery_mode": "LIVE",
    }
    event = {
        "schema_version": "1.0",
        "event_id": f"aviator-round:{round_id}:observed",
        "event_type": "round.observed",
        "stage": "OBSERVED",
        "stream_id": "stream-test",
        "stream_sequence": None,
        "committed_sequence": None,
        "observation_id": round_id,
        "round_id": round_id,
        "identity_source": "LOCAL_SEQUENCE",
        "multiplier": multiplier,
        "platform_timestamp": None,
        "observed_at": observed_at,
        "published_at": None,
        "stored_at": None,
        "durability": "PENDING",
        "continuity_status": "UNVERIFIED",
        "delivery_mode": "LIVE",
    }
    return event, row


def test_inbox_restarts_with_pending_rows_and_keeps_pending_cursor_separate(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = RoundObservationInbox(path)
    events_rows = [observation("r1", 1.5), observation("r2", 1.5)]
    accepted = inbox.append([item[0] for item in events_rows], [item[1] for item in events_rows],
                            received_at=datetime.now(timezone.utc).isoformat())
    assert accepted["backlog_size"] == 2
    assert [event["round_id"] for event in inbox.pending_page(after_sequence=0)["events"]] == ["r1", "r2"]

    restarted = RoundObservationInbox(path)
    page = restarted.pending_page(after_sequence=0, recovered=True)
    assert page["events"][0]["delivery_mode"] == "RECOVERED"
    assert page["events"][0]["committed_sequence"] is None
    restarted.mark_committed("r1", 41)
    next_page = restarted.pending_page(after_sequence=page["events"][0]["pending_sequence"])
    assert [event["round_id"] for event in next_page["events"]] == ["r2"]
    assert restarted.snapshot()["backlog_size"] == 1


def test_append_acknowledgement_uses_committed_counts_without_second_snapshot(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "fast-ack.sqlite3")
    event, row = observation("fast-ack")
    original_snapshot = inbox.snapshot
    inbox.snapshot = lambda: (_ for _ in ()).throw(AssertionError("append must not rescan the inbox"))

    accepted = inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())

    assert accepted["backlog_size"] == 1
    assert accepted["backlog_bytes"] > 0
    assert accepted["events"] == [event]
    inbox.snapshot = original_snapshot
    assert inbox.snapshot()["backlog_size"] == 1


def test_pending_round_index_lookup_tracks_observed_target_until_commit(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "pending-target.sqlite3")
    event, row = observation("pending-target")
    event["round_index"] = row["round_index"] = 13
    inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())

    assert inbox.has_pending_round_index(13) is True
    assert inbox.has_pending_round_index(14) is False

    inbox.mark_committed("pending-target", 13)
    assert inbox.has_pending_round_index(13) is False


def test_collector_disconnect_is_a_concise_retryable_response(caplog):
    class Inbox:
        pass

    async def disconnected_json():
        raise ClientDisconnect()

    app = SimpleNamespace(state=SimpleNamespace(
        round_observation_inbox=Inbox(), round_stream_id="stream-test"))
    request = SimpleNamespace(app=app, json=disconnected_json)
    response = asyncio.run(collector_round_observed(request))
    assert response.status_code == 503
    assert b"collector_request_disconnected_retry" in response.body
    assert "durable local outbox will retry" in caplog.text
    assert "Traceback" not in caplog.text


def test_mark_staged_many_is_idempotent_and_preserves_pending_rows(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "staged-batch.sqlite3")
    rows = [observation("stage-a"), observation("stage-b")]
    inbox.append([item[0] for item in rows], [item[1] for item in rows],
                 received_at=datetime.now(timezone.utc).isoformat())
    inbox.mark_staged_many(["stage-a", "stage-b", "stage-a"])
    inbox.mark_staged_many(["stage-a", "stage-b"])
    assert inbox.unstaged_observations() == []
    assert inbox.snapshot()["backlog_size"] == 2


def test_observed_publication_uses_dedicated_inbox_executor(tmp_path, monkeypatch):
    async def scenario():
        async def unavailable_default_pool(*_args, **_kwargs):
            raise AssertionError("observation publication used the shared default worker pool")

        monkeypatch.setattr(asyncio, "to_thread", unavailable_default_pool)
        inbox_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-inbox")
        inbox = RoundObservationInbox(tmp_path / "isolated-inbox.sqlite3")
        append_started = threading.Event()
        release_append = threading.Event()
        original_append = inbox.append

        def slow_append(*args, **kwargs):
            append_started.set()
            if not release_append.wait(timeout=3):
                raise TimeoutError("test append was not released")
            return original_append(*args, **kwargs)

        inbox.append = slow_append
        event, row = observation("fast-observed", 1.9)
        published = []
        published_signal = asyncio.Event()

        async def publish(item):
            published.append(item)
            published_signal.set()

        async def request_json():
            return {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [row]}

        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=inbox,
            round_observation_inbox_executor=inbox_executor,
            round_stream_id="stream-test",
            round_distribution=SimpleNamespace(publish=publish),
        ))
        request = SimpleNamespace(app=app, json=request_json)
        route_task = asyncio.create_task(collector_round_observed(request))
        started = time.perf_counter()
        try:
            await asyncio.wait_for(published_signal.wait(), timeout=0.5)
            delivery_ms = (time.perf_counter() - started) * 1000.0
            assert append_started.is_set()
            assert not route_task.done(), "route should await API resume durability after SSE publication"
            assert delivery_ms < 500.0
        finally:
            release_append.set()
            inbox_executor.shutdown(wait=True, cancel_futures=True)
        response = await asyncio.wait_for(route_task, timeout=2.0)
        assert response["ok"] is True
        assert delivery_ms < 500.0
        assert len(published) == 1
        assert inbox.snapshot()["backlog_size"] == 1
        print(f"observation reached SSE queue in {delivery_ms:.2f} ms before delayed API resume append")

    asyncio.run(scenario())


def test_inbox_idempotency_conflicts_and_capacity_never_drop_existing_rows(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "bounded.sqlite3", max_records=1, max_bytes=4096)
    first_event, first_row = observation("same-round", 1.5)
    received_at = datetime.now(timezone.utc).isoformat()
    inbox.append([first_event], [first_row], received_at=received_at)
    duplicate = inbox.append([first_event], [first_row], received_at=received_at)
    assert duplicate["backlog_size"] == 1
    conflicting_event, conflicting_row = observation("same-round", 2.5)
    with pytest.raises(ObservationIdentityConflict):
        inbox.append([conflicting_event], [conflicting_row], received_at=received_at)
    event2, row2 = observation("second-round", 1.5)
    with pytest.raises(ObservationInboxCapacityError):
        inbox.append([event2], [row2], received_at=received_at)
    assert [item["round_id"] for item in inbox.pending_events()] == ["same-round"]


def test_pending_observation_ack_does_not_run_rules_engine_before_postgres_commit(tmp_path):
    class EngineThatMustWaitForCommit:
        def needs_round_observation(self):
            raise AssertionError("pending observations must not synchronously evaluate rules")

        def observe_round(self, *_args, **_kwargs):
            raise AssertionError("only committed events may advance a session")

    async def scenario():
        class FastDurableInbox:
            def __init__(self):
                self.count = 0

            def append(self, events, _observations, *, received_at):
                self.count += len(events)
                return {"events": events, "duplicates": 0, "backlog_size": self.count}

            def snapshot(self):
                return {"backlog_size": self.count}

        inbox = FastDurableInbox()
        inbox_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="commit-only-test-inbox")
        _, row = observation("pending-before-commit", 1.91)

        async def request_json():
            return {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [row]}

        async def publish(_event):
            return None

        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=inbox,
            round_observation_inbox_executor=inbox_executor,
            round_stream_id="stream-test",
            round_distribution=SimpleNamespace(publish=publish),
            rules_engine=EngineThatMustWaitForCommit(),
        ))
        try:
            response = await asyncio.wait_for(
                collector_round_observed(SimpleNamespace(app=app, json=request_json)), timeout=3)
            assert response["ok"] is True
            assert response["rule_evaluation_stage"] == "WAITING_FOR_COMMITTED_POSTGRES_EVENT"
            assert response["rules_engine_evaluations"] == []
            assert inbox.snapshot()["backlog_size"] == 1
        finally:
            inbox_executor.shutdown(wait=True, cancel_futures=True)

    asyncio.run(scenario())


def test_hub_slow_client_isolated_and_sse_flushes_before_replay_query(monkeypatch, tmp_path):
    async def scenario():
        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(round_api.asyncio, "to_thread", inline_to_thread)
        hub = RoundDistributionHub()
        fast = await hub.reserve_subscriber({"token_id": "fast", "project_id": "p"})
        slow = await hub.reserve_subscriber({"token_id": "slow", "project_id": "p"})
        delivery_samples = []
        for number in range(hub.QUEUE_LIMIT + 5):
            started = time.perf_counter()
            event = {"event_type": "round.observed", "event_id": f"e{number}",
                     "observation_id": f"r{number}", "stream_id": "s"}
            await hub.publish(event)
            delivery_samples.append((time.perf_counter() - started) * 1000.0)
            fast.queue.get_nowait()
        assert slow.closed is True
        assert fast.closed is False
        assert (await hub.metrics())["client_overflows"] == 1
        ordered = sorted(delivery_samples)
        measured = {"samples": len(ordered),
                    "p50_ms": round(ordered[len(ordered) // 2], 3),
                    "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
                    "max_ms": round(ordered[-1], 3)}
        print(f"isolated hub publication-to-queue-receipt latency {measured}")
        await hub.unsubscribe(fast)
        await hub.unsubscribe(slow)

        class Repository:
            reads = 0

            def round_distribution_snapshot(self):
                self.reads += 1
                return {"stream_id": "s", "latest_sequence": 0, "earliest_sequence": None,
                        "retained_events": 0, "continuity_gaps": 0, "continuity_unknown": 0}

            def count_round_distribution_after(self, *_args):
                return 0

        repository = Repository()
        repository.database_url = "postgresql://isolated-test"
        pending_inbox = RoundObservationInbox(tmp_path / "pending-sse.sqlite3")
        pending_event, pending_row = observation("sse-pending", 1.7)
        pending_event["stream_id"] = "s"
        pending_inbox.append([pending_event], [pending_row],
                             received_at=datetime.now(timezone.utc).isoformat())
        stream_hub = RoundDistributionHub()
        app_state = SimpleNamespace(round_distribution=stream_hub,
                                    wp=SimpleNamespace(repository=repository),
                                    round_observation_inbox=pending_inbox,
                                    round_stream_id="s")
        fake_request = SimpleNamespace(app=SimpleNamespace(state=app_state), headers={},
                                       is_disconnected=lambda: asyncio.sleep(0, result=False))
        monkeypatch.setattr(round_api, "_authenticate_project", lambda _request: asyncio.sleep(
            0, result={"token_id": "sse", "project_id": "p"}))
        response = await round_api.subscribe_round_stream(fake_request)
        assert response.media_type == "text/event-stream"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["content-encoding"] == "identity"
        first_chunk = await anext(response.body_iterator)
        assert first_chunk == ": connected\n\n"
        assert repository.reads == 0
        ready_chunk = await asyncio.wait_for(anext(response.body_iterator), timeout=3)
        assert "event: stream.ready" in ready_chunk
        pending_chunk = await anext(response.body_iterator)
        assert "event: round.observed" in pending_chunk
        assert '"durability":"PENDING"' in pending_chunk
        assert '"committed_sequence":null' in pending_chunk
        replay_chunk = await anext(response.body_iterator)
        assert "event: stream.replay_complete" in replay_chunk
        assert '"pending_next_cursor":1' in replay_chunk
        await response.body_iterator.aclose()
        assert (await stream_hub.metrics())["connected_clients"] == 0
    asyncio.run(scenario())


def test_live_observation_is_sent_while_committed_history_replay_is_slow(monkeypatch):
    async def scenario():
        replay_started = asyncio.Event()
        release_replay = asyncio.Event()

        class Repository:
            database_url = "postgresql://isolated-test"

            def round_distribution_snapshot(self):
                return {"stream_id": "stream-live-first", "latest_sequence": 1,
                        "earliest_sequence": 1, "retained_events": 1,
                        "continuity_gaps": 0, "continuity_unknown": 0}

            def count_round_distribution_after(self, *_args):
                return 1

            def round_distribution_events(self, *_args):
                return [{"schema_version": "1.0", "event_id": "old-round:persisted",
                         "event_type": "round.persisted", "stage": "PERSISTED",
                         "stream_id": "stream-live-first", "stream_sequence": 1,
                         "committed_sequence": 1, "observation_id": "old-round",
                         "round_id": "old-round", "multiplier": 1.3,
                         "observed_at": "2026-10-10T00:00:00+00:00",
                         "published_at": "2026-10-10T00:00:01+00:00",
                         "durability": "COMMITTED"}]

        repository = Repository()

        async def controlled_to_thread(function, /, *args, **kwargs):
            if function == repository.round_distribution_events:
                replay_started.set()
                await release_replay.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(round_api.asyncio, "to_thread", controlled_to_thread)
        hub = RoundDistributionHub()
        app_state = SimpleNamespace(round_distribution=hub,
                                    wp=SimpleNamespace(repository=repository),
                                    round_observation_inbox=None,
                                    round_stream_id="stream-live-first")
        fake_request = SimpleNamespace(app=SimpleNamespace(state=app_state), headers={},
                                       is_disconnected=lambda: asyncio.sleep(0, result=False))
        monkeypatch.setattr(round_api, "_authenticate_project", lambda _request: asyncio.sleep(
            0, result={"token_id": "live", "project_id": "p"}))
        response = await round_api.subscribe_round_stream(
            fake_request, after_sequence=0, stream_id="stream-live-first")
        try:
            assert await anext(response.body_iterator) == ": connected\n\n"
            assert "event: stream.ready" in await anext(response.body_iterator)
            replay_read = asyncio.create_task(anext(response.body_iterator))
            await asyncio.wait_for(replay_started.wait(), timeout=1)
            await hub.publish({"schema_version": "1.0", "event_id": "new-round:observed",
                               "event_type": "round.observed", "stage": "OBSERVED",
                               "stream_id": "stream-live-first", "stream_sequence": None,
                               "committed_sequence": None, "observation_id": "new-round",
                               "round_id": "new-round", "multiplier": 1.7,
                               "observed_at": "2026-10-10T00:00:02+00:00",
                               "published_at": "2026-10-10T00:00:02.010+00:00",
                               "durability": "PENDING"})
            observed_chunk = await asyncio.wait_for(replay_read, timeout=0.5)
            assert "event: round.observed" in observed_chunk
            assert '"round_id":"new-round"' in observed_chunk

            release_replay.set()
            committed_chunk = await anext(response.body_iterator)
            assert "event: round.persisted" in committed_chunk
            complete_chunk = await anext(response.body_iterator)
            assert "event: stream.replay_complete" in complete_chunk
            assert (await hub.metrics())["connected_clients"] == 1
        finally:
            release_replay.set()
            await response.body_iterator.aclose()
            assert (await hub.metrics())["connected_clients"] == 0

    asyncio.run(scenario())


def test_live_observation_is_sent_before_slow_postgres_replay_boundary(monkeypatch):
    async def scenario():
        snapshot_started = asyncio.Event()
        release_snapshot = asyncio.Event()

        class Repository:
            database_url = "postgresql://isolated-test"

            def round_distribution_snapshot(self):
                return {"stream_id": "stream-before-boundary", "latest_sequence": 0,
                        "earliest_sequence": None, "retained_events": 0,
                        "continuity_gaps": 0, "continuity_unknown": 0, "latest": None}

            def count_round_distribution_after(self, *_args):
                return 0

        repository = Repository()

        async def controlled_to_thread(function, /, *args, **kwargs):
            if function == repository.round_distribution_snapshot:
                snapshot_started.set()
                await release_snapshot.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(round_api.asyncio, "to_thread", controlled_to_thread)
        hub = RoundDistributionHub()
        app_state = SimpleNamespace(round_distribution=hub,
                                    wp=SimpleNamespace(repository=repository),
                                    round_observation_inbox=None,
                                    round_stream_id="stream-before-boundary")
        request = SimpleNamespace(app=SimpleNamespace(state=app_state), headers={},
                                  is_disconnected=lambda: asyncio.sleep(0, result=False))
        monkeypatch.setattr(round_api, "_authenticate_project", lambda _request: asyncio.sleep(
            0, result={"token_id": "pre-boundary", "project_id": "p"}))
        response = await round_api.subscribe_round_stream(request)
        try:
            assert await anext(response.body_iterator) == ": connected\n\n"
            next_chunk = asyncio.create_task(anext(response.body_iterator))
            await asyncio.wait_for(snapshot_started.wait(), timeout=1)
            event = {"schema_version": "1.0", "event_id": "fast-round:observed",
                     "event_type": "round.observed", "stage": "OBSERVED",
                     "stream_id": "stream-before-boundary", "stream_sequence": None,
                     "committed_sequence": None, "observation_id": "fast-observation",
                     "round_id": "fast-round", "multiplier": 1.7,
                     "observed_at": "2026-10-10T00:00:02+00:00",
                     "published_at": "2026-10-10T00:00:02.010+00:00",
                     "durability": "PENDING", "delivery_mode": "LIVE"}
            started = time.perf_counter()
            await hub.publish(event)
            observed_chunk = await asyncio.wait_for(next_chunk, timeout=0.5)
            delivery_ms = (time.perf_counter() - started) * 1000.0
            assert "event: round.observed" in observed_chunk
            assert '"durability":"PENDING"' in observed_chunk
            assert delivery_ms < 500.0

            release_snapshot.set()
            assert "event: stream.ready" in await anext(response.body_iterator)
            assert "event: stream.replay_complete" in await anext(response.body_iterator)
            print(f"round.observed reached SSE in {delivery_ms:.2f} ms before replay snapshot completed")
        finally:
            release_snapshot.set()
            await response.body_iterator.aclose()
            assert (await hub.metrics())["connected_clients"] == 0

    asyncio.run(scenario())


def test_sse_heartbeat_does_not_query_postgres(monkeypatch):
    async def scenario():
        class TimeoutQueue:
            async def get(self):
                await asyncio.Future()

            def get_nowait(self):
                raise asyncio.QueueEmpty

        class Hub:
            def __init__(self):
                self.subscriber = SimpleNamespace(queue=TimeoutQueue(), closed=False,
                                                  pending_event_ids=set())
                self.unsubscribed = False

            async def reserve_subscriber(self, _token):
                return self.subscriber

            async def prepare_replay(self, repository, _subscriber, _cursor, _stream_id):
                return repository.round_distribution_snapshot(), 0, 0

            async def unsubscribe(self, _subscriber):
                self.unsubscribed = True

        hub = Hub()

        class Repository:
            reads = 0
            database_url = "postgresql://isolated-test"

            def round_distribution_snapshot(self):
                self.reads += 1
                return {"stream_id": "heartbeat-test", "latest_sequence": 0,
                        "earliest_sequence": None, "retained_events": 0,
                        "continuity_gaps": 0, "continuity_unknown": 0, "latest": None}

            def count_round_distribution_after(self, *_args):
                return 0

        repository = Repository()
        app_state = SimpleNamespace(round_distribution=hub,
                                    wp=SimpleNamespace(repository=repository),
                                    round_observation_inbox=None,
                                    round_stream_id="heartbeat-test")
        disconnect_checks = 0

        async def is_disconnected():
            nonlocal disconnect_checks
            disconnect_checks += 1
            return disconnect_checks > 1

        fake_request = SimpleNamespace(app=SimpleNamespace(state=app_state), headers={},
                                       is_disconnected=is_disconnected)
        monkeypatch.setattr(round_api, "_authenticate_project", lambda _request: asyncio.sleep(
            0, result={"token_id": "heartbeat", "project_id": "p"}))
        monkeypatch.setattr(round_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.001)
        response = await round_api.subscribe_round_stream(fake_request)
        try:
            assert await anext(response.body_iterator) == ": connected\n\n"
            assert "event: stream.ready" in await anext(response.body_iterator)
            assert "event: stream.replay_complete" in await anext(response.body_iterator)
            heartbeat = await anext(response.body_iterator)
            assert "event: heartbeat" in heartbeat
            assert '"latest_sequence":0' in heartbeat
            # One snapshot is required to establish the replay boundary. No
            # follow-up PostgreSQL query may gate the keepalive.
            assert repository.reads == 1
        finally:
            await response.body_iterator.aclose()
            assert hub.unsubscribed is True

    asyncio.run(scenario())


def test_sse_emits_periodic_heartbeat_while_round_events_are_continuous(monkeypatch):
    async def scenario():
        class BusyQueue:
            def __init__(self):
                self.sequence = 0

            async def get(self):
                await asyncio.sleep(0.004)
                self.sequence += 1
                return {"event_type": "round.persisted", "stage": "PERSISTED",
                        "stream_id": "busy-heartbeat", "stream_sequence": self.sequence,
                        "committed_sequence": self.sequence,
                        "observation_id": f"busy-{self.sequence}",
                        "round_id": f"busy-{self.sequence}", "multiplier": 1.2,
                        "continuity_status": "VERIFIED", "durability": "COMMITTED"}

            def get_nowait(self):
                raise asyncio.QueueEmpty

        class Hub:
            def __init__(self):
                self.subscriber = SimpleNamespace(queue=BusyQueue(), closed=False,
                                                  pending_event_ids=set())

            async def reserve_subscriber(self, _token):
                return self.subscriber

            async def prepare_replay(self, repository, _subscriber, _cursor, _stream_id):
                return repository.round_distribution_snapshot(), 0, 0

            async def unsubscribe(self, _subscriber):
                return None

            def record_delivery(self, _event):
                return None

        class Repository:
            database_url = "postgresql://isolated-test"

            def round_distribution_snapshot(self):
                return {"stream_id": "busy-heartbeat", "latest_sequence": 0,
                        "earliest_sequence": None, "retained_events": 0,
                        "continuity_gaps": 0, "continuity_unknown": 0, "latest": None}

            def count_round_distribution_after(self, *_args):
                return 0

        hub = Hub()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                round_distribution=hub, wp=SimpleNamespace(repository=Repository()),
                round_observation_inbox=None, round_stream_id="busy-heartbeat")),
            headers={}, is_disconnected=lambda: asyncio.sleep(0, result=False))
        monkeypatch.setattr(round_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.015)
        monkeypatch.setattr(round_api, "_authenticate_project", lambda _request: asyncio.sleep(
            0, result={"token_id": "busy-heartbeat", "project_id": "p"}))
        response = await round_api.subscribe_round_stream(request)
        try:
            assert await anext(response.body_iterator) == ": connected\n\n"
            assert "event: stream.ready" in await anext(response.body_iterator)
            assert "event: stream.replay_complete" in await anext(response.body_iterator)
            chunks = [await asyncio.wait_for(anext(response.body_iterator), timeout=0.1)
                      for _ in range(8)]
            assert any("event: heartbeat" in chunk for chunk in chunks)
            assert sum("event: round.persisted" in chunk for chunk in chunks) >= 2
        finally:
            await response.body_iterator.aclose()

    asyncio.run(scenario())


def test_event_frame_ends_with_blank_line_and_pending_event_has_no_replay_cursor():
    event = round_api.observed_round_event(observation("round-x")[1], "stream-test")
    framed = round_api._sse(event)
    assert framed.endswith("\n\n")
    assert event["event_id"] == "aviator-round:round-x:observed"
    assert event["committed_sequence"] is None
    assert event["durability"] == "PENDING"
    assert event["platform_timestamp"] is None
    unknown_observation = observation("round-unknown-identity")[1]
    unknown_observation["round_identity_type"] = "UNKNOWN"
    unknown_observation["round_index_source"] = "UNKNOWN"
    assert round_api.observed_round_event(unknown_observation, "stream-test")["identity_source"] == "UNKNOWN"


def test_external_round_api_rejects_unauthenticated_requests_without_leaking_token():
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(round_stream_allowed_origins=[])))
        scope = {"type": "http", "method": "GET", "scheme": "http", "path": "/api/v1/rounds/clock",
                 "query_string": b"", "headers": [], "server": ("127.0.0.1", 8000),
                 "client": ("127.0.0.1", 12345), "app": app}
        request = Request(scope)
        with pytest.raises(HTTPException) as raised:
            await round_api._authenticate_project(request)
        assert raised.value.status_code == 401
        assert raised.value.detail["error"] == "project_token_required"
    asyncio.run(scenario())


def test_collector_status_payload_returns_the_full_status_document(tmp_path, monkeypatch):
    collector_status = {
        "collectorRunning": True,
        "loggedIn": True,
        "heartbeatAt": datetime.now(timezone.utc).isoformat(),
        "lastSuccessfulCollection": datetime.now(timezone.utc).isoformat(),
        "browserConnected": True,
        "pageConnected": True,
        "frameConnected": True,
        "lastError": None,
    }

    class FakePath:
        def __init__(self, path):
            self.path = Path(path)

        def resolve(self):
            return self

        @property
        def parents(self):
            return [FakePath(tmp_path)] * 4

        def __truediv__(self, value):
            return FakePath(self.path / value)

        def read_text(self, encoding=None):
            if self.path.name == "status.json":
                return __import__("json").dumps(collector_status)
            return "[]"

    class Inbox:
        def snapshot(self):
            return {"backlog_size": 2, "oldest_pending_observation_age_seconds": 3.0,
                    "oldest_pending_observation_received_at": "2026-10-09T00:00:00Z"}

    monkeypatch.setattr(round_api, "Path", FakePath)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        round_observation_inbox=Inbox())))
    stream = {"stream_id": "stream-test", "latest_sequence": 7,
              "earliest_sequence": 1, "retained_events": 7, "latest": None}

    payload = round_api._collector_status_payload(request, stream, {"clients": 1})

    assert payload["ok"] is True
    assert payload["collector_status"] == "HEALTHY"
    assert payload["stream"]["stream_id"] == "stream-test"
    assert payload["outbox"]["api_pending_observations"] == 2
