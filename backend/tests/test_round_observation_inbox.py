from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.requests import ClientDisconnect

import main as main_app
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


def test_idle_inbox_connection_keeps_wal_and_pending_events_across_reopen(tmp_path):
    path = tmp_path / "anchor.sqlite3"
    inbox = RoundObservationInbox(path)
    event, row = observation("anchor-round")
    inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())
    assert Path(f"{path}-wal").exists()
    inbox.close()
    restarted = RoundObservationInbox(path)
    try:
        assert [item["round_id"] for item in restarted.pending_page(after_sequence=0)["events"]] == ["anchor-round"]
    finally:
        restarted.close()


def test_latest_verified_round_is_available_after_database_commit(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "latest-round.sqlite3")
    event, row = observation("latest-verified-round", 2.8)
    event.update({"continuity_status": "VERIFIED", "round_index": 41})
    row.update({"continuity_verified": True, "round_index": 41})
    inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())
    assert inbox.latest_pending_verified_round()["round_id"] == "latest-verified-round"
    persisted = {**event, "event_type": "round.persisted", "stage": "PERSISTED",
                 "durability": "COMMITTED", "stored_at": datetime.now(timezone.utc).isoformat()}
    inbox.mark_committed_many([("latest-verified-round", 91, persisted)])
    assert inbox.latest_pending_verified_round() is None
    latest = inbox.latest_verified_round_event()
    assert latest["event_type"] == "round.persisted"
    assert latest["durability"] == "COMMITTED"
    assert latest["round_id"] == "latest-verified-round"


def test_verified_observed_rule_work_replays_in_order_until_durably_processed(tmp_path):
    path = tmp_path / "rules-replay.sqlite3"
    inbox = RoundObservationInbox(path)
    pairs = [observation(f"verified-{index}") for index in range(3)]
    unverified_event, unverified_row = observation("unverified")
    for event, row in pairs:
        event["continuity_status"] = "VERIFIED"
        row["continuity_verified"] = True
    inbox.append([event for event, _ in [*pairs, (unverified_event, unverified_row)]],
                 [row for _, row in [*pairs, (unverified_event, unverified_row)]],
                 received_at=datetime.now(timezone.utc).isoformat())

    restarted = RoundObservationInbox(path)
    page = restarted.pending_rule_events_page(limit=2)
    assert [event["round_id"] for event in page["events"]] == ["verified-0", "verified-1"]
    assert page["has_more"] is True
    next_page = restarted.pending_rule_events_page(after_sequence=page["next_cursor"], limit=2)
    assert [event["round_id"] for event in next_page["events"]] == ["verified-2"]
    assert next_page["has_more"] is False
    assert restarted.mark_rules_processed("verified-0") is True
    assert restarted.mark_rules_processed("verified-0") is False
    remaining = restarted.pending_rule_events_page(limit=10)
    assert [event["round_id"] for event in remaining["events"]] == ["verified-1", "verified-2"]


def test_gap_before_observation_is_replayed_for_session_boundary_sync(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "gap-boundary-replay.sqlite3")
    event, row = observation("gap-boundary")
    event["continuity_status"] = "GAP_BEFORE"
    row.update({"continuity_verified": True, "gap_before": True})
    inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())

    restarted = RoundObservationInbox(tmp_path / "gap-boundary-replay.sqlite3")
    assert restarted.pending_rule_events_page()["events"] == [event]


def test_pending_duplicate_continuity_change_requeues_rule_state_sync(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "changed-continuity.sqlite3")
    event, row = observation("changed-continuity")
    event["continuity_status"] = "VERIFIED"
    row["continuity_verified"] = True
    received_at = datetime.now(timezone.utc).isoformat()
    inbox.append([event], [row], received_at=received_at)
    assert inbox.mark_rules_processed("changed-continuity") is True

    boundary_event = {**event, "continuity_status": "GAP_BEFORE"}
    boundary_row = {**row, "gap_before": True}
    inbox.append([boundary_event], [boundary_row],
                 received_at=datetime.now(timezone.utc).isoformat())

    assert inbox.pending_rule_events_page()["events"] == [boundary_event]


def test_round_worker_keeps_observed_event_pending_until_commit_checkpoint(tmp_path, monkeypatch):
    async def scenario():
        inbox = RoundObservationInbox(tmp_path / "worker-replay.sqlite3")
        event, row = observation("retry-me")
        event["continuity_status"] = "VERIFIED"
        row["continuity_verified"] = True
        inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())

        async def inline_inbox(_app, function, *args, **kwargs):
            kwargs.pop("priority", None)
            return function(*args, **kwargs)

        failed_once = asyncio.Event()
        attempts = 0

        async def process(_app, _stage, _event, *_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                failed_once.set()
                raise RuntimeError("simulated interrupted decision")
            return True, {"session-1"}, None

        class Engine:
            def persist_current_snapshot(self, *, session_ids=None):
                assert session_ids == {"session-1"}
                return {"snapshot_serialization_ms": 1.0, "database_persistence_ms": 2.0}

        monkeypatch.setattr(main_app, "run_round_observation_inbox", inline_inbox)
        monkeypatch.setattr(main_app, "_process_round_event", process)
        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=inbox, rules_engine=Engine(), round_distribution=None,
            round_event_retry_attempts={}, round_event_queued_ids=set()))
        assert main_app._enqueue_round_events(app, "observed", [event]) is True
        queue = app.state.round_event_queue
        worker = app.state.round_event_processor_task
        try:
            await asyncio.wait_for(failed_once.wait(), timeout=1)
            assert [item["round_id"] for item in
                    inbox.pending_rule_events_page()["events"]] == ["retry-me"]
            await asyncio.wait_for(queue.join(), timeout=2)
            assert attempts == 2
            assert [item["round_id"] for item in
                    inbox.pending_rule_events_page()["events"]] == ["retry-me"]
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


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


def test_commit_and_rules_acknowledgements_batch_atomically_and_recover_by_stage(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "batched-rule-ack.sqlite3")
    first_event, first_row = observation("batch-ack-1")
    second_event, second_row = observation("batch-ack-2")
    for event, row in ((first_event, first_row), (second_event, second_row)):
        event["round_index"] = row["round_index"]
        event["continuity_status"] = "VERIFIED"
        row["continuity_verified"] = True
    inbox.append([first_event, second_event], [first_row, second_row],
                 received_at=datetime.now(timezone.utc).isoformat())
    committed = {**first_event, "event_type": "round.persisted", "durability": "COMMITTED",
                 "stream_sequence": int(first_event["round_index"])}
    inbox.mark_committed_many([("batch-ack-1", first_event["round_index"], committed)])

    pending = inbox.pending_rule_events_page()["events"]
    assert [event["round_id"] for event in pending] == ["batch-ack-1", "batch-ack-2"]
    assert all(event["continuity_status"] == "VERIFIED" for event in pending)
    committed_pending = inbox.pending_committed_rule_events_page()["events"]
    assert [event["round_id"] for event in committed_pending] == ["batch-ack-1"]
    assert committed_pending[0]["delivery_mode"] == "RECOVERED"
    assert inbox.mark_rules_processed_many([
        ("observed", "batch-ack-2"), ("committed", "batch-ack-1")]) == 2

    restarted = RoundObservationInbox(inbox.path)
    assert restarted.pending_rule_events_page()["events"] == []
    assert restarted.pending_committed_rule_events_page()["events"] == []


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


def test_rules_priority_decision_is_acknowledged_after_durable_inbox_and_fanout(tmp_path, monkeypatch):
    async def scenario():
        order = []

        class DurableInbox:
            def append(self, events, _observations, *, received_at):
                assert received_at
                order.append("durable_inbox")
                return {"events": events, "duplicates": 0, "backlog_size": len(events)}

        published = []

        async def publish_event(item):
            published.append(item)
            order.append("other_fanout")

        class RulesEngine:
            def observe_round(self, *_args, **_kwargs):
                order.append("rules_engine")
                return {"decision": "WAIT", "reason": "TEST", "true_rule_ids": [], "sessions": []}

        class Hub:
            record_priority_latency = lambda *_args: None

            async def publish(self, item):
                await publish_event(item)

        event, row = observation("fast-observed", 1.9)
        async def request_json():
            row.update({"continuity_verified": True,
                        "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                        "round_index_source": "LOCAL_SEQUENCE"})
            return {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [row]}

        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        async def inline_inbox(_app, function, *args, priority=False, **kwargs):
            return function(*args, **kwargs)

        event_queue = asyncio.PriorityQueue(maxsize=512)
        queued_ids = set()
        monkeypatch.setattr(main_app, "_round_event_queue",
                            lambda _app: (event_queue, queued_ids))
        monkeypatch.setattr(main_app.asyncio, "to_thread", inline_to_thread)
        monkeypatch.setattr(main_app, "run_round_observation_inbox", inline_inbox)
        def completed_decision_waiter(_app, stage, item):
            future = asyncio.get_running_loop().create_future()
            future.set_result({"decision_ready": True, "stage": stage})
            return main_app._round_event_key(stage, item), future

        monkeypatch.setattr(main_app, "_round_event_completion_future", completed_decision_waiter)
        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=DurableInbox(),
            round_stream_id="stream-test",
            round_distribution=Hub(),
            rules_engine=RulesEngine(),
            round_event_enqueue_sequence=0,
            round_event_retry_ids=set(),
        ))
        request = SimpleNamespace(app=app, json=request_json)
        response = await collector_round_observed(request)
        assert response["ok"] is True
        assert order == ["durable_inbox", "other_fanout"]
        assert len(published) == 1
        assert response["rule_evaluation_stage"] == "DECISION_READY_BEFORE_COLLECTOR_ACK"
        assert response["rules_engine_evaluations"] == [
            {"round_id": "fast-observed", "status": "DECISION_READY"}]
        assert event_queue.qsize() == 1

    asyncio.run(scenario())


def test_gap_boundary_observation_is_queued_before_collector_ack(tmp_path, monkeypatch):
    async def scenario():
        accepted_events = []

        class DurableInbox:
            def append(self, events, _observations, *, received_at):
                accepted_events.extend(events)
                return {"events": events, "duplicates": 0, "backlog_size": len(events)}

        class Hub:
            def record_priority_latency(self, *_args):
                return None

            async def publish(self, _event):
                return None

        event, row = observation("orbit-gap-boundary", 1.81)
        row.update({"continuity_verified": True, "gap_before": True,
                    "delivery_mode": "RECOVERED",
                    "identity_confidence": "OVERLAP_VERIFIED_ORDER_ONLY",
                    "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                    "round_index_source": "LOCAL_SEQUENCE"})

        async def request_json():
            return {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [row]}

        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        async def inline_inbox(_app, function, *args, **kwargs):
            kwargs.pop("priority", None)
            return function(*args, **kwargs)

        event_queue = asyncio.PriorityQueue(maxsize=512)
        queued_ids = set()
        monkeypatch.setattr(main_app, "_round_event_queue",
                            lambda _app: (event_queue, queued_ids))
        def completed_boundary_waiter(_app, stage, item):
            assert stage == "observed"
            assert item["continuity_status"] == "GAP_BEFORE"
            future = asyncio.get_running_loop().create_future()
            future.set_result({"decision_ready": True, "stage": stage})
            return main_app._round_event_key(stage, item), future

        monkeypatch.setattr(main_app.asyncio, "to_thread", inline_to_thread)
        monkeypatch.setattr(main_app, "run_round_observation_inbox", inline_inbox)
        monkeypatch.setattr(main_app, "_round_event_completion_future", completed_boundary_waiter)
        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=DurableInbox(), round_stream_id="stream-test",
            round_distribution=Hub(), rules_engine=SimpleNamespace(),
            round_event_enqueue_sequence=0, round_event_retry_ids=set()))

        response = await collector_round_observed(SimpleNamespace(app=app, json=request_json))

        assert response["ok"] is True
        assert accepted_events[0]["delivery_mode"] == "RECOVERED"
        assert accepted_events[0]["continuity_status"] == "GAP_BEFORE"
        assert response["rules_engine_evaluations"] == [
            {"round_id": "orbit-gap-boundary", "status": "BOUNDARY_STATE_ADVANCED"}]
        assert event_queue.qsize() == 1

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


def test_verified_pending_round_is_queued_only_after_durable_inbox_append(tmp_path, monkeypatch):
    async def scenario():
        order = []

        class FastDurableInbox:
            def __init__(self):
                self.count = 0

            def append(self, events, _observations, *, received_at):
                self.count += len(events)
                order.append("inbox_durable")
                return {"events": events, "duplicates": 0, "backlog_size": self.count}

        class Fanout:
            def record_priority_latency(self, *_args):
                return None

            async def publish(self, event):
                order.append(f"fanout:{event['round_id']}")

        inbox = FastDurableInbox()
        first_event, first_row = observation("priority-round-1", 1.91)
        second_event, second_row = observation("priority-round-2", 2.04)
        for event, row, round_index in ((first_event, first_row, 10), (second_event, second_row, 11)):
            row.update({"continuity_verified": True, "round_index": round_index,
                        "round_index_source": "LOCAL_SEQUENCE",
                        "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                        "identity_confidence": "OVERLAP_VERIFIED_ORDER_ONLY"})

        async def request_json():
            return {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [first_row, second_row]}

        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        async def inline_inbox(_app, function, *args, priority=False, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(main_app.asyncio, "to_thread", inline_to_thread)
        monkeypatch.setattr(main_app, "run_round_observation_inbox", inline_inbox)
        def completed_decision_waiter(_app, stage, item):
            future = asyncio.get_running_loop().create_future()
            future.set_result({"decision_ready": True, "stage": stage})
            return main_app._round_event_key(stage, item), future

        monkeypatch.setattr(main_app, "_round_event_completion_future", completed_decision_waiter)

        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=inbox,
            round_stream_id="stream-test",
            round_distribution=Fanout(),
            rules_engine=SimpleNamespace(),
            # Keep this test focused on the enqueue boundary; the real async
            # worker would consume the queue while the assertions run.
            round_event_processor_task=asyncio.get_running_loop().create_future(),
            shadow=SimpleNamespace(reconcile_round=lambda *_args: pytest.fail("SHADOW must not block priority rules")),
            history_collector=SimpleNamespace(rows=lambda: pytest.fail("history refresh must not block priority rules")),
        ))
        response = await collector_round_observed(SimpleNamespace(app=app, json=request_json))
        assert response["ok"] is True
        assert response["rule_evaluation_stage"] == "DECISION_READY_BEFORE_COLLECTOR_ACK"
        assert [item["round_id"] for item in response["rules_engine_evaluations"]] == [
            "priority-round-1", "priority-round-2"]
        assert [item["status"] for item in response["rules_engine_evaluations"]] == [
            "DECISION_READY", "DECISION_READY"]
        assert [item["round_id"] for item in response["priority_timings_ms"]] == [
            "priority-round-1", "priority-round-2"]
        assert order == ["inbox_durable", "fanout:priority-round-1", "fanout:priority-round-2"]
        assert app.state.round_event_queue.qsize() == 2

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


def test_rules_priority_latency_metrics_deduplicate_event_samples_and_report_p99():
    async def scenario():
        hub = RoundDistributionHub()
        hub.record_priority_latency("rules_engine_evaluation_ms", "round-1", 2.0)
        hub.record_priority_latency("rules_engine_evaluation_ms", "round-1", 200.0)
        hub.record_priority_latency("rules_engine_evaluation_ms", "round-2", 3.0)
        summary = (await hub.metrics())["latency_ms"]["rules_engine_evaluation_ms"]
        assert summary == {"samples": 2, "average_ms": 2.5, "p50_ms": 2.0, "p95_ms": 3.0,
                           "p99_ms": 3.0, "max_ms": 3.0}

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

            async def next_stream_event(event_name):
                for _ in range(20):
                    chunk = await anext(response.body_iterator)
                    if f"event: {event_name}" in chunk:
                        return chunk
                    # The protocol permits heartbeat frames while the replay
                    # boundary is still being prepared.
                    assert "event: heartbeat" in chunk
                raise AssertionError(f"event: {event_name} was not emitted")

            await next_stream_event("stream.ready")
            await next_stream_event("stream.replay_complete")
            heartbeat = await next_stream_event("heartbeat")
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


def test_verified_round_envelope_exposes_shared_identity_and_sequence():
    event, row = observation("canonical-round", 2.0)
    row.update({"continuity_verified": True, "round_index": 41,
                "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                "round_index_source": "LOCAL_SEQUENCE",
                "source_metadata": {"source": "ORBIT_DATA_WATCH_API"}})
    event = round_api.observed_round_event(row, "stream-test")
    assert event["round_uid"] == event["round_id"] == "canonical-round"
    assert event["canonical_sequence"] == event["round_index"] == 41
    assert event["provider_round_id"] is None
    assert event["provider_sequence"] is None
    assert event["source"] == "ORBIT_DATA_WATCH_API"
    assert event["classification"] == "EXACT"
    assert event["verification_status"] == "VERIFIED"
    assert event["verified_at"]


def test_behaviour_handoff_checkpoint_replays_and_deduplicates(tmp_path):
    path = tmp_path / "behaviour-consumer.sqlite3"
    inbox = RoundObservationInbox(path)
    event, row = observation("behaviour-round", 2.4)
    row.update({"continuity_verified": True, "round_index": 11,
                "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                "round_index_source": "LOCAL_SEQUENCE"})
    event = round_api.observed_round_event(row, "stream-test")
    received_at = datetime.now(timezone.utc).isoformat()
    inbox.append([event], [row], received_at=received_at)
    pending = inbox.pending_behaviour_events()[0]
    assert pending[0] == "behaviour-round"
    assert {key: value for key, value in pending[1].items()
            if key not in {"journal_received_at", "latest_verified_round_index"}} == event
    assert pending[1]["journal_received_at"] == received_at
    assert pending[1]["latest_verified_round_index"] == 11
    inbox.close()

    recovered = RoundObservationInbox(path)
    assert recovered.pending_behaviour_events()[0][0] == "behaviour-round"
    assert recovered.mark_behaviour_processed("behaviour-round") is True
    assert recovered.pending_behaviour_events() == []
    recovered.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())
    assert recovered.pending_behaviour_events() == []
    health = recovered.snapshot()
    assert health["behaviour_pending"] == 0
    assert health["behaviour_checkpoint"] > 0
    assert health["checkpoint_namespace"] == "LOCAL_SQLITE_INBOX_SEQUENCE_NOT_CANONICAL_ROUND_INDEX"


def test_behaviour_consumer_receives_gap_boundaries_without_live_signal_provenance(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "behaviour-gap-boundary.sqlite3")
    event, row = observation("gap-boundary-round", 1.6)
    row.update({"continuity_verified": True, "gap_before": True, "round_index": 12,
                "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                "round_index_source": "LOCAL_SEQUENCE", "delivery_mode": "RECOVERED"})
    event = round_api.observed_round_event(row, "stream-gap")
    event["continuity_status"] = "GAP_BEFORE"
    event["delivery_mode"] = "RECOVERED"
    inbox.append([event], [row], received_at=datetime.now(timezone.utc).isoformat())

    pending = inbox.pending_behaviour_events()
    assert len(pending) == 1
    assert pending[0][1]["continuity_status"] == "GAP_BEFORE"
    assert pending[0][1]["delivery_mode"] == "RECOVERED"
    assert inbox.snapshot()["behaviour_pending"] == 1
    inbox.close()


def test_behaviour_provenance_blocks_signal_creation_while_verified_backlog_exists(monkeypatch):
    monkeypatch.setattr(main_app, "_trusted_live_round_after_boundary",
                        lambda _event, _boundary: True)
    event = {"continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "round_index": 11, "latest_verified_round_index": 12}
    assert main_app._behaviour_event_provenance(event, "boundary") == (
        "RECOVERY_REPLAY", "BACKLOG_AHEAD_OF_DECISION")
    latest = {**event, "latest_verified_round_index": 11}
    assert main_app._behaviour_event_provenance(latest, "boundary") == (
        "LIVE_VERIFIED", None)


def test_behaviour_inbox_orders_by_canonical_round_index_and_marks_backlog_replay(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "behaviour-canonical-order.sqlite3")
    rows, events = [], []
    for index in (12, 11):
        event, row = observation(f"ordered-{index}", 2.1)
        row.update({"continuity_verified": True, "round_index": index,
                    "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                    "round_index_source": "LOCAL_SEQUENCE"})
        event = round_api.observed_round_event(row, "stream-order")
        events.append(event)
        rows.append(row)
    inbox.append(events, rows, received_at=datetime.now(timezone.utc).isoformat())

    first = inbox.pending_behaviour_events(stream_id="stream-order")[0]
    assert first[1]["round_index"] == 11
    assert first[1]["latest_verified_round_index"] == 12
    assert inbox.mark_behaviour_processed(first[0]) is True
    second = inbox.pending_behaviour_events(stream_id="stream-order")[0]
    assert second[1]["round_index"] == 12

    inbox.defer_behaviour_event(second[0], "test_backoff")
    assert inbox.pending_behaviour_events(stream_id="stream-order", now=0) == []
    inbox.close()


def test_behaviour_reconcile_skips_only_rounds_covered_by_its_checkpoint(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "behaviour-reconcile.sqlite3")
    rows, events = [], []
    now = datetime.now(timezone.utc).isoformat()
    for index in range(1, 4):
        event, row = observation(f"reconcile-{index}", 1.5 + index)
        row.update({"continuity_verified": True, "round_index": index,
                    "round_identity_type": "COLLECTOR_OBSERVATION_HASH",
                    "round_index_source": "LOCAL_SEQUENCE"})
        row["source_metadata"] = {"source": "ORBIT_DATA_WATCH_API"}
        rows.append(row)
        events.append(round_api.observed_round_event(row, "stream-reconcile"))
    inbox.append(events, rows, received_at=now)

    assert inbox.reconcile_behaviour_checkpoint(stream_id="stream-reconcile",
        round_id="reconcile-2", round_index=2) == 2
    assert [event["round_uid"] for _, event in inbox.pending_behaviour_events()] == ["reconcile-3"]
    inbox.close()


def test_behaviour_round_consumer_runs_without_rules_or_postgres(monkeypatch):
    async def scenario():
        delivered = asyncio.Event()
        received = []

        class Inbox:
            pending = [("obs-1", {"event_type": "round.observed", "round_id": "round-1",
                "round_uid": "round-1", "durability": "PENDING", "continuity_status": "VERIFIED",
                "delivery_mode": "LIVE"})]

            def pending_behaviour_events(self, **_kwargs):
                return list(self.pending)

            def mark_behaviour_processed(self, observation_id):
                self.pending = []
                return observation_id == "obs-1"

            def defer_behaviour_event(self, *_args):
                raise AssertionError("unexpected retry")

        async def forward(_app, event):
            received.append(event["round_uid"])
            delivered.set()
            return True

        monkeypatch.setattr(main_app, "_forward_live_round_to_behaviour", forward)
        app = SimpleNamespace(state=SimpleNamespace(
            round_observation_inbox=Inbox(), behaviour_round_wakeup=asyncio.Event(),
            behaviour_round_last_delivered_id=None, behaviour_round_last_delivered_at=None,
            behaviour_round_last_delivery_ms=None, behaviour_round_last_error=None))
        task = asyncio.create_task(main_app._run_behaviour_round_consumer(app))
        try:
            await asyncio.wait_for(delivered.wait(), timeout=1)
            for _ in range(20):
                if app.state.behaviour_round_last_delivered_id:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert received == ["round-1"]
        assert app.state.behaviour_round_last_delivered_id == "round-1"

    asyncio.run(scenario())


def test_behaviour_live_boundary_rejects_recently_forwarded_old_orbit_reading():
    boundary = "2026-11-18T07:00:00+00:00"
    base = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "journal_received_at": "2026-11-18T07:01:00+00:00",
        "source_metadata": {"source": "ORBIT_DATA_WATCH_API",
            "collector_received_at": "2026-11-18T06:59:00+00:00",
            "reading_checked_at": "2026-11-18T06:58:00+00:00"}}
    assert main_app._trusted_live_round_after_boundary(base, boundary) is False

    fresh = {**base, "source_metadata": {**base["source_metadata"],
        "collector_received_at": "2026-11-18T07:01:00+00:00",
        "reading_checked_at": "2026-11-18T07:00:30+00:00"}}
    assert main_app._trusted_live_round_after_boundary(fresh, boundary) is True
    assert main_app._trusted_live_round_after_boundary(fresh, None) is False


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
        "roundSourceStatus": "AVAILABLE",
        "roundCollectionStatus": "HEALTHY_CAUGHT_UP",
        "loggedIn": True,
        "heartbeatAt": datetime.now(timezone.utc).isoformat(),
        "lastSuccessfulCollection": datetime.now(timezone.utc).isoformat(),
        "lastObservedRoundId": "observed-round-3",
        "lastObservedRoundIndex": 3,
        "lastObservedAt": datetime.now(timezone.utc).isoformat(),
        "lastDetectedRoundId": "observed-round-3",
        "lastDetectedAt": datetime.now(timezone.utc).isoformat(),
        "lastDetectedStatus": "VALIDATED",
        "lastDetectedReason": "ROUND_OVERLAP_VALIDATED",
        "roundCommitSamples": [
            {"round_id": "round-1", "latency_ms": 10.0},
            {"round_id": "round-2", "latency_ms": 20.0},
            {"round_id": "round-3", "latency_ms": 100.0},
        ],
        "storage": {"round_delivery": {"last_round_timing": {
            "round_history_api_visibility": "NOT_MEASURED",
        }}},
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
    assert payload["metrics"]["latency_ms"]["observation_to_commit_ms"]["p99_ms"] == 100.0
    assert payload["collector"]["lastObservedRoundId"] == "observed-round-3"
    assert payload["collector"]["roundSourceStatus"] == "AVAILABLE"
    assert payload["collector"]["roundCollectionStatus"] == "HEALTHY_CAUGHT_UP"
    assert payload["collector"]["lastDetectedStatus"] == "VALIDATED"
    assert payload["collector"]["last_round_timing"]["round_history_api_visibility"] == "NOT_MEASURED"


def test_orbit_watch_health_uses_api_and_source_status_without_legacy_browser_flags():
    checked_at = datetime.now(timezone.utc).isoformat()
    collector = {
        "collectorRunning": True,
        "dataSource": "ORBIT_DATA_WATCH_API",
        "heartbeatAt": checked_at,
        "lastSuccessfulCollection": None,
        "authRequired": False,
        "loggedIn": None,
        "browserConnected": False,
        "pageConnected": False,
        "frameConnected": False,
        "orbitApi": {
            "status": "CONNECTED",
            "streamStatus": "CONNECTED",
            "sourceStatus": "HEALTHY",
            "lastSourceCheckAt": checked_at,
            "lastError": None,
        },
    }

    assert round_api._collector_health_from_payload(collector) == "HEALTHY"
    collector["orbitApi"]["streamStatus"] = "DISCONNECTED"
    assert round_api._collector_health_from_payload(collector) == "STALE"


def test_fast_signal_settlement_mirror_is_idempotent_and_restart_durable(tmp_path):
    inbox_path = tmp_path / "settlement-mirror.sqlite"
    inbox = RoundObservationInbox(inbox_path)
    signal = {"signal_id": "behaviour-shadow-1", "session_id": "shadow-1",
        "target_round_id": "shadow-index:stream-1:12", "mode": "SHADOW",
        "source_id": "BEHAVIOUR_DETECTION", "distribution_status": "ACCEPTED"}
    inserted, _stored = inbox.enqueue_fast_signal(signal)
    assert inserted is True
    settlement = {"status": "COMMITTED", "result": "WIN", "target_round_id": "round-12",
        "target_round_index": 12, "target_stream_id": "stream-1"}
    assert inbox.commit_fast_signal_settlement(signal["signal_id"], settlement) is True
    assert inbox.commit_fast_signal_settlement(signal["signal_id"], settlement) is True
    [updated] = inbox.pending_fast_signals()
    assert updated["settlement_status"] == "COMMITTED"
    assert updated["settlement"] == settlement
    with pytest.raises(ObservationIdentityConflict):
        inbox.commit_fast_signal_settlement(signal["signal_id"],
            {**settlement, "result": "LOSS"})
    inbox.close()

    reopened = RoundObservationInbox(inbox_path)
    [restored] = reopened.pending_fast_signals()
    assert restored["signal_id"] == signal["signal_id"]
    assert restored["settlement"]["result"] == "WIN"
    reopened.close()
