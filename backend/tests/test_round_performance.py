import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from app.api.rules_engine import performance
from app.round_distribution import RoundDistributionHub
from main import (
    _enqueue_round_events,
    _enqueue_rules_dashboard_notification,
    _process_round_event,
    _remember_collector_detection_timestamps,
)


def test_rules_dashboard_hints_are_bounded_and_latest_hint_survives_backpressure():
    queue = asyncio.Queue(maxsize=2)
    app = SimpleNamespace(state=SimpleNamespace(
        rules_dashboard_notification_queue=queue,
        rules_dashboard_notifications_coalesced=0,
    ))

    for round_index in range(1, 4):
        assert _enqueue_rules_dashboard_notification(app, {"round_index": round_index})

    assert queue.qsize() == 2
    assert queue.get_nowait() == {"round_index": 2}
    queue.task_done()
    assert queue.get_nowait() == {"round_index": 3}
    queue.task_done()
    assert app.state.rules_dashboard_notifications_coalesced == 1


def test_new_observations_keep_fifo_order_ahead_of_committed_settlement_work():
    async def scenario():
        class ActiveTask:
            @staticmethod
            def done():
                return False

        app = SimpleNamespace(state=SimpleNamespace(
            round_event_queue=asyncio.PriorityQueue(maxsize=8),
            round_event_queued_ids=set(), round_event_retry_ids=set(),
            round_event_processor_task=ActiveTask(), round_event_enqueue_sequence=0))
        committed = {"event_id": "c-10", "round_id": "round-10", "round_index": 10}
        observed = [
            {"event_id": "o-11", "round_id": "round-11", "round_index": 11},
            {"event_id": "o-12", "round_id": "round-12", "round_index": 12},
        ]
        assert _enqueue_round_events(app, "committed", [committed]) is True
        assert _enqueue_round_events(app, "observed", observed) is True

        queue = app.state.round_event_queue
        received = []
        for _ in range(3):
            _, _, stage, event, *_ = await queue.get()
            received.append((stage, event["round_id"]))
            queue.task_done()
        assert received == [("observed", "round-11"), ("observed", "round-12"),
                            ("committed", "round-10")]
    asyncio.run(scenario())


def test_round_queue_wait_stops_at_worker_dequeue_and_excludes_marker_lookup():
    class Metrics:
        def __init__(self):
            self.values = {}

        def record_priority_latency(self, name, _sample_id, value):
            self.values[name] = value

    class Engine:
        def has_round_event_consumers(self):
            return True

        def observe_round(self, *_args, **_kwargs):
            return {"event_timings_ms": {}, "sessions": []}

    class Repository:
        def latest_round_marker(self):
            return None

    async def scenario():
        metrics = Metrics()
        app = SimpleNamespace(state=SimpleNamespace(
            rules_engine=Engine(), wp=SimpleNamespace(repository=Repository()),
            round_distribution=metrics, shadow_signal_bridge=None,
            collector_detection_timestamps={},
        ))
        await _process_round_event(
            app, "observed", {"round_id": "round-queue", "event_type": "round.observed",
                               "durability": "PENDING", "continuity_status": "VERIFIED"},
            enqueued_ns=100, request_started_ns=None,
            validation_finished_ns=None, worker_started_ns=200)
        assert metrics.values["round_event_processor_queue_wait_ms"] == 0.0001

    asyncio.run(scenario())


def test_collector_detection_clock_is_retained_only_for_loopback_requests():
    app = SimpleNamespace(state=SimpleNamespace())
    event = {"round_id": "round-clock", "observed_at": "2026-11-02T23:40:00Z"}

    _remember_collector_detection_timestamps(app, [event], peer_is_loopback=False)
    assert not hasattr(app.state, "collector_detection_timestamps")

    _remember_collector_detection_timestamps(app, [event], peer_is_loopback=True)
    assert app.state.collector_detection_timestamps == {"round-clock": event["observed_at"]}


def test_performance_dashboard_uses_measured_correlated_backend_spans():
    hub = RoundDistributionHub()
    hub.record_priority_latency(
        "backend_receive_to_decision_ready_ms",
        "round-42:backend_receive_to_decision_ready_ms",
        600.0,
    )
    hub.record_priority_latency(
        "api_durable_inbox_append_ms",
        "round-42:api_durable_inbox_append_ms",
        38.0,
    )
    queue = asyncio.Queue(maxsize=128)
    app = SimpleNamespace(state=SimpleNamespace(
        round_distribution=hub,
        round_event_queue=queue,
        round_event_processor_busy=True,
    ))

    payload = asyncio.run(performance(SimpleNamespace(app=app)))

    assert payload["status"] == "WARNING"
    assert payload["total_latest_ms"] == 600.0
    assert payload["total_latest_correlation_id"] == "round-42"
    assert payload["stages"]["Backend inbox append"]["p50_ms"] == 38.0
    assert payload["queue"] == {"depth": 0, "capacity": 128, "busy": True}
    assert payload["dashboard_notifications"]["coalesced"] == 0
    assert payload["measurement_availability"]["external_signal_consumer_ack"] == "UNAVAILABLE_NO_CLIENT_ACK_PROTOCOL"


def test_performance_dashboard_does_not_call_unknown_latency_green():
    app = SimpleNamespace(state=SimpleNamespace(round_distribution=RoundDistributionHub()))

    payload = asyncio.run(performance(SimpleNamespace(app=app)))

    assert payload["status"] == "UNKNOWN"
    assert payload["total_latest_ms"] is None
    assert payload["end_to_end_target_status"] == "UNKNOWN"
    assert payload["total_stage"] == "backend_receive_to_decision_ready_ms"


def test_performance_dashboard_marks_only_sub_2000ms_correlated_e2e_span_green():
    hub = RoundDistributionHub()
    hub.record_priority_latency("collector_detection_to_signal_api_acceptance_ms",
                                "round-e2e:collector_detection_to_signal_api_acceptance_ms",
                                1999.9)
    app = SimpleNamespace(state=SimpleNamespace(round_distribution=hub))

    passing = asyncio.run(performance(SimpleNamespace(app=app)))
    assert passing["end_to_end_target_status"] == "GREEN"
    assert passing["end_to_end_latest_ms"] == 1999.9
    assert passing["end_to_end_latest_correlation_id"] == "round-e2e"
    assert passing["stages"]["Collector detection to Signal API acceptance"]["p50_ms"] == 1999.9

    hub.record_priority_latency("collector_detection_to_signal_api_acceptance_ms",
                                "round-e2e-2000:collector_detection_to_signal_api_acceptance_ms",
                                2000.0)
    at_limit = asyncio.run(performance(SimpleNamespace(app=app)))
    assert at_limit["end_to_end_target_status"] == "CRITICAL"


def test_stale_collector_samples_are_reported_but_not_called_the_current_bottleneck(tmp_path):
    collector_status = tmp_path / "collector-status.json"
    collector_status.write_text(json.dumps({
        "collectorRunning": True,
        "state": "AUTH_REQUIRED",
        "lastSuccessfulCollection": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "storage": {"round_delivery": {"latency_ms": {
            "collector_http_roundtrip_to_api_ack_ms": {
                "samples": 10, "average_ms": 5000.0, "p50_ms": 5000.0,
                "p95_ms": 6000.0, "p99_ms": 6000.0, "max_ms": 6000.0,
            },
            "database_write_ms": {
                "samples": 10, "average_ms": 8000.0, "p50_ms": 8000.0,
                "p95_ms": 9000.0, "p99_ms": 9000.0, "max_ms": 9000.0,
            },
        }}},
    }))
    hub = RoundDistributionHub()
    hub.record_priority_latency("backend_event_loop_lag_ms", "loop-1", 3000.0)
    app = SimpleNamespace(state=SimpleNamespace(
        round_distribution=hub,
        collector_performance_status_path=collector_status,
    ))

    payload = asyncio.run(performance(SimpleNamespace(app=app)))

    assert payload["sample_freshness"]["collector_current"] is False
    assert payload["sample_freshness"]["collector_state"] == "AUTH_REQUIRED"
    assert payload["stages"]["Collector HTTP acknowledgment"]["p50_ms"] == 5000.0
    assert payload["stages"]["PostgreSQL round write"]["p50_ms"] == 8000.0
    assert payload["slowest_stage"] == "Backend event loop lag"
