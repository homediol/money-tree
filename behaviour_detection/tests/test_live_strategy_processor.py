import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json

from sqlalchemy.orm import Session
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from behaviour_detection.db import make_engine, migrate
from behaviour_detection.high_series import new_state, process_round
from behaviour_detection.live_strategy_processor import LiveStrategyProcessor
from behaviour_detection.models import (CanonicalRound, HighDominanceSeriesState,
    BehaviourApiRound, HybridCohort)
from behaviour_detection import api as behaviour_api
from behaviour_detection import settings as behaviour_settings
from behaviour_detection.api import create_app


def asgi_client_run(app, callback):
    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app,
                    client=("127.0.0.1", 50000)), base_url="http://testserver") as client:
                return await callback(client)
    return asyncio.run(run())


def invoke_api_route(app, path, method, *, body=None):
    route = next(item for item in app.routes
        if getattr(item, "path", None) == path and method in (item.methods or set()))
    scope = {"type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80), "app": app}
    request = Request(scope)
    return route.endpoint(request, body) if body is not None else route.endpoint(request)


def _canonical_round(session, index, now, multiplier="2.50"):
    round_id = f"r-{index}"
    value = Decimal(multiplier)
    session.add(CanonicalRound(identity_key=f"winner-predict:{round_id}", round_id=round_id,
        source_round_index=str(index), source_sequence=index, analysis_position=index,
        multiplier_raw=str(value), multiplier_value=value,
        outcome="H" if value > 2 else "L" if value < 2 else "E",
        event_timestamp=None, source_observed_at=now, ingestion_timestamp=now,
        source_name="winner_predict_database", timestamp_quality="observation_time_only",
        order_quality="winner_predict_canonical_order_verified", import_snapshot_id=None,
        inserted_at=now))


def _api_round(session, stream_id, index, now, multiplier="2.50", *,
               delivery_mode="LIVE", provenance="LIVE_VERIFIED",
               continuity="VERIFIED", source_metadata=None):
    round_id = f"r-{index}"
    payload = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": continuity, "delivery_mode": delivery_mode,
        "behaviour_provenance": provenance, "round_id": round_id,
        "round_uid": round_id, "round_index": index, "stream_id": stream_id,
        "multiplier": multiplier, "observed_at": now.isoformat(),
        "source_metadata": source_metadata or {}}
    session.add(BehaviourApiRound(identity_key=f"{stream_id}:{round_id}",
        stream_id=stream_id, round_id=round_id, round_index=index,
        multiplier_raw=str(multiplier), observed_at=now, platform_timestamp=None,
        received_at=now, payload=payload, status="PROMOTED"))


def test_verified_live_round_advances_strategy_once_without_analysis_worker(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live-strategy.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_live_round_index": 4, "last_processed_identity": "winner-predict:r-4",
            "last_processed_position": 4, "live_rolling_outcomes": ["H", "H", "H", "H"]})
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        if state_row:
            state_row.payload = machine
            state_row.updated_at = now
        else:
            session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id,
                payload=machine, updated_at=now))
        session.add(CanonicalRound(identity_key="winner-predict:r-5", round_id="r-5",
            source_round_index="5", source_sequence=5, analysis_position=5,
            multiplier_raw="2.50", multiplier_value=Decimal("2.50"), outcome="H",
            event_timestamp=None, source_observed_at=now, ingestion_timestamp=now,
            source_name="winner_predict_database", timestamp_quality="observation_time_only",
            order_quality="winner_predict_canonical_order_verified", import_snapshot_id=None,
            inserted_at=now))
        _api_round(session, "stream-1", 5, now)

    processor = LiveStrategyProcessor(engine)
    event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE", "round_id": "r-5",
        "behaviour_provenance": "LIVE_VERIFIED", "round_uid": "r-5",
        "stream_id": "stream-1", "round_index": 5, "multiplier": 2.5}
    recovery = processor.process({**event, "behaviour_provenance": "RECOVERY_REPLAY"})
    assert recovery["status"] == "REJECTED_PROVENANCE"
    result = processor.process(event)
    assert result["status"] == "PROCESSED"
    assert result["dominance_state"] == "HIGH_DOMINANT"
    assert result["decision"]["eligibility"] == "ELIGIBLE"
    assert result["decision"]["decision_round_identity"] == "winner-predict:r-5"
    duplicate = processor.process(event)
    assert duplicate["status"] == "DUPLICATE"
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        assert machine["state"] == "PART1_COLLECTING"
        assert machine["series"]["trigger_round_identity"] == "winner-predict:r-5"
        assert machine["last_live_round_index"] == 5
    engine.dispose()


def test_first_live_round_with_persistent_high_is_not_a_new_emergence(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live-baseline.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_live_round_index": 5, "live_rolling_outcomes": ["H"] * 5,
            "last_dominance_state": "HIGH_DOMINANT"})
        state_row.payload = machine
        session.add(CanonicalRound(identity_key="winner-predict:r-6", round_id="r-6",
            source_round_index="6", source_sequence=6, analysis_position=6,
            multiplier_raw="2.50", multiplier_value=Decimal("2.50"), outcome="H",
            event_timestamp=None, source_observed_at=now, ingestion_timestamp=now,
            source_name="winner_predict_database", timestamp_quality="observation_time_only",
            order_quality="winner_predict_canonical_order_verified", import_snapshot_id=None,
            inserted_at=now))
        _api_round(session, "stream-1", 6, now)
    processor = LiveStrategyProcessor(engine)
    event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "LIVE_VERIFIED", "round_id": "r-6", "round_uid": "r-6",
        "stream_id": "stream-1", "round_index": 6, "multiplier": 2.5}
    result = processor.process(event)
    assert result["dominance_state"] == "HIGH_DOMINANT"
    assert result["decision"]["eligibility"] == "INELIGIBLE"
    processor.record_stage_latency("rules_engine_acceptance_to_durable_lock_ms", 2.25)
    stage = processor.metrics()["stages"]["rules_engine_acceptance_to_durable_lock_ms"]
    assert stage == {"samples": 1, "p50_ms": 2.25, "p95_ms": 2.25,
                     "p99_ms": 2.25, "max_ms": 2.25}
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        assert machine["series"] is None
        assert machine["state"] == "ARMED_FOR_HIGH"
    engine.dispose()


def test_verified_round_api_runs_strategy_before_database_analysis_worker(tmp_path, monkeypatch):
    from behaviour_detection import worker
    engine = make_engine(f"sqlite:///{tmp_path / 'direct-live-api.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for index in range(1, 5):
            _api_round(session, "stream-api", index, now)
            session.add(CanonicalRound(identity_key=f"winner-predict:r-{index}",
                round_id=f"r-{index}", source_round_index=str(index), source_sequence=index,
                analysis_position=index, multiplier_raw="2.50", multiplier_value=Decimal("2.50"),
                outcome="H", event_timestamp=None, source_observed_at=now,
                ingestion_timestamp=now, source_name="winner_predict_database",
                timestamp_quality="observation_time_only",
                order_quality="winner_predict_canonical_order_verified",
                import_snapshot_id=None, inserted_at=now))
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_live_round_index": 4, "live_rolling_outcomes": ["H"] * 4,
            "last_processed_identity": "winner-predict:r-4", "last_processed_position": 4})
        state_row.payload = machine
    monkeypatch.setattr(worker, "_publish_behaviour_eligibility", lambda engine: None)
    body = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "LIVE_VERIFIED", "round_id": "r-5", "round_uid": "r-5",
        "round_index": 5, "canonical_sequence": 5, "stream_id": "stream-api",
        "multiplier": 2.5, "observed_at": now.isoformat()}
    app = create_app(engine)
    async def exercise_route_handlers():
        async with app.router.lifespan_context(app):
            payload = invoke_api_route(app, "/internal/round-observed", "POST", body=body)
            assert payload["inbox_status"] == "PROMOTED"
            assert payload["decision"]["status"] == "PROCESSED"
            assert payload["decision"]["decision"]["eligibility"] == "ELIGIBLE"
            status = invoke_api_route(app, "/api/v1/behaviour/live-status", "GET")
            assert status["processor"]["samples"] == 1
            assert status["processor"]["max_ms"] > 0
    asyncio.run(exercise_route_handlers())
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        assert machine["series"]["trigger_round_id"] == "r-5"
        assert machine["last_live_round_id"] == "r-5"
    engine.dispose()


def test_analysis_database_outage_does_not_block_local_live_decisions(tmp_path, monkeypatch):
    from behaviour_detection import settings, worker

    journal_path = tmp_path / "shared-ingress.sqlite3"
    seed_engine = make_engine(f"sqlite:///{journal_path}")
    migrate(seed_engine)
    now = datetime.now(timezone.utc)
    with Session(seed_engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for index in range(1, 5):
            _canonical_round(session, index, now)
            _api_round(session, "offline-stream", index, now)
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_live_round_index": 4, "live_rolling_outcomes": ["H"] * 4,
            "last_processed_round_index": 4, "last_processed_position": 4,
            "last_processed_round_identity": "winner-predict:r-4"})
        state_row.payload = machine
    seed_engine.dispose()

    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "LIVE_PROCESSOR_DATABASE_URL",
                        f"sqlite:///{journal_path}")
    monkeypatch.setattr(worker, "_publish_behaviour_eligibility", lambda _engine: None)
    event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "LIVE_VERIFIED", "round_id": "r-5", "round_uid": "r-5",
        "round_index": 5, "canonical_sequence": 5, "stream_id": "offline-stream",
        "multiplier": 2.5, "observed_at": now.isoformat()}

    app = create_app()
    async def exercise_offline_live_path():
        async with app.router.lifespan_context(app):
            assert app.state.engine is None
            assert app.state.database_error == "BEHAVIOUR_DATABASE_URL_MISSING"
            ready = invoke_api_route(app, "/health/ready", "GET")
            assert ready["state"] == "READY_DATABASE_OFFLINE"
            response = invoke_api_route(app, "/internal/round-observed", "POST", body=event)
            assert response["decision"]["status"] == "PROCESSED"
            assert response["decision"]["decision"]["eligibility"] == "ELIGIBLE"
            status = invoke_api_route(app, "/api/v1/behaviour/live-status", "GET")
            assert status["processor"]["latest_live_round_uid"] == "r-5"
            assert status["database_status"] == "OFFLINE"
    asyncio.run(exercise_offline_live_path())


def test_recovery_replay_advances_cursor_without_creating_live_series(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'recovery-live.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for index in range(1, 7):
            _canonical_round(session, index, now)
        _api_round(session, "recovery-stream", 5, now,
            provenance="RECOVERY_REPLAY", delivery_mode="RECOVERED")
        _api_round(session, "recovery-stream", 6, now)
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_live_round_index": 4, "live_rolling_outcomes": ["H"] * 4,
            "last_processed_round_index": 4, "last_processed_position": 4,
            "last_processed_round_identity": "winner-predict:r-4"})
        state_row.payload = machine

    processor = LiveStrategyProcessor(engine)
    replay = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "RECOVERY_REPLAY", "round_id": "r-5", "round_uid": "r-5",
        "round_index": 5, "stream_id": "recovery-stream", "multiplier": 2.5}
    result = processor.recover(replay)
    assert result["status"] == "WAITING_FOR_LIVE"
    assert result["eligibility_created"] is False

    live = {**replay, "behaviour_provenance": "LIVE_VERIFIED",
        "round_id": "r-6", "round_uid": "r-6", "round_index": 6}
    result = processor.process(live)
    assert result["status"] == "PROCESSED"
    assert result["decision"]["eligibility"] == "INELIGIBLE"
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        assert machine["series"] is None
        assert machine["last_live_round_id"] == "r-6"
        assert machine["provenance"] == "LIVE_VERIFIED"
    engine.dispose()


def test_recovery_rebuilds_proven_live_part1_then_cooldown_without_new_intents(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'reconcile-active-series.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    stream = "verified-recovery-stream"
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for index in range(1, 21):
            _canonical_round(session, index, now,
                multiplier="2.50" if index <= 5 else "1.50")
            payload = {"event_type": "round.observed", "durability": "PENDING",
                "continuity_status": "VERIFIED",
                "delivery_mode": "LIVE" if index == 5 else "RECOVERED",
                "behaviour_provenance": "LIVE_VERIFIED" if index == 5 else "RECOVERY_REPLAY",
                "round_id": f"r-{index}", "round_uid": f"r-{index}",
                "round_index": index, "stream_id": stream,
                "multiplier": 2.5 if index <= 5 else 1.5,
                "observed_at": now.isoformat(),
                "source_metadata": ({"source": "ORBIT_DATA_WATCH_API", "reading_id": "read-5",
                    "reading_checked_at": now.isoformat(), "collector_received_at": now.isoformat()}
                    if index == 5 else {})}
            session.add(BehaviourApiRound(identity_key=f"{stream}:r-{index}",
                stream_id=stream, round_id=f"r-{index}", round_index=index,
                multiplier_raw=payload["multiplier"].__str__(), observed_at=now,
                platform_timestamp=None, received_at=now, payload=payload,
                status="PROMOTED"))
        machine = new_state()
        for index in range(1, 6):
            machine = process_round(machine,
                row={"identity_key": f"winner-predict:r-{index}", "round_id": f"r-{index}",
                     "position": index, "multiplier": "2.50", "outcome": "H"},
                previous_state="HIGH_DOMINANT" if index > 1 else "BALANCED",
                current_state="HIGH_DOMINANT", emergence=index == 5,
                now=now)
        machine.update({"state": "UNSAFE_STATE", "provenance": "UNVERIFIABLE",
            "recovery_quarantine_reason": "MISSED_LIVE_ROUNDS_DURING_RECOVERY",
            "last_live_round_id": "r-5", "last_live_round_index": 5,
            "last_live_round_position": 5, "last_live_stream_id": stream,
            "last_processed_round_id": "r-9", "last_processed_round_identity": "winner-predict:r-9",
            "last_processed_round_index": 9, "last_processed_stream_id": stream,
            "last_processed_position": 9, "live_rolling_outcomes": ["L"] * 5})
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        state_row.payload = machine
        state_row.updated_at = now

    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE fast_signal_outbox (
            signal_id TEXT, session_id TEXT, target_id TEXT, signal_json TEXT,
            persisted INTEGER NOT NULL DEFAULT 0)""")
        signal = {"signal_id": "signal-for-r6", "session_id": "session-1",
            "target_round_id": f"shadow-index:{stream}:6", "mode": "SHADOW",
            "source_id": "BEHAVIOUR_DETECTION", "distribution_status": "ACCEPTED",
            "settlement_status": "COMMITTED", "settlement": {
                "status": "COMMITTED", "result": "WIN", "target_round_id": "r-6",
                "target_round_index": 6, "profit_loss_rwf": 1,
                "evidence": {"event_type": "round.persisted", "durability": "COMMITTED",
                    "continuity_status": "VERIFIED", "target_stream_id": stream}}}
        connection.exec_driver_sql("INSERT INTO fast_signal_outbox VALUES(?,?,?,?,1)",
            (signal["signal_id"], signal["session_id"], signal["target_round_id"],
             json.dumps(signal)))

    processor = LiveStrategyProcessor(engine)
    event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "RECOVERED",
        "behaviour_provenance": "RECOVERY_REPLAY", "round_id": "r-10", "round_uid": "r-10",
        "round_index": 10, "stream_id": stream, "multiplier": 1.5,
        "observed_at": now.isoformat()}
    restored = processor.recover(event)
    assert restored["status"] == "RESTORED_FROM_RECOVERY"
    assert restored["series_reconciled"] is True
    assert restored["eligibility_created"] is False
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
    assert machine["state"] == "PART1_COLLECTING"
    assert machine["provenance"] == "RECOVERY_REPLAY"
    assert machine["recovery_reconciliation"]["status"] == "VALID_RESTORED"
    part1 = machine["series"]["part1"]
    assert len(part1["rounds"]) == 1
    assert part1["confirmed_win_count"] == 1
    assert part1["counted_rounds"] == 1
    assert part1["rounds"][0]["execution_status"] == "LOCKED_SIGNAL_SETTLED"
    assert part1["rounds"][0]["result"] == "WIN"
    assert not machine.get("pending_strategy_eligibilities")

    for index in range(11, 21):
        next_event = {**event, "round_id": f"r-{index}", "round_uid": f"r-{index}",
            "round_index": index}
        result = processor.recover(next_event)
        assert result["eligibility_created"] is False
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
    assert machine["state"] == "PART1_COLLECTING"
    assert machine["series"]["part1"]["counted_rounds"] == 1
    assert machine["cooldown_progress"] == 0
    assert machine["provenance"] == "RECOVERY_REPLAY"
    assert machine.get("latest_strategy_eligibility") is None
    engine.dispose()


def test_gap_recovery_resets_stale_dominance_baseline_before_live_decisions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'gap-recovery.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        _canonical_round(session, 8, now, multiplier="1.50")
        _canonical_round(session, 9, now, multiplier="2.50")
        _api_round(session, "gap-stream", 8, now, multiplier="1.50",
            provenance="RECOVERY_REPLAY", delivery_mode="RECOVERED", continuity="GAP_BEFORE")
        _api_round(session, "gap-stream", 9, now, multiplier="2.50")
        _api_round(session, "gap-stream", 6, now, multiplier="1.50",
            provenance="RECOVERY_REPLAY", delivery_mode="RECOVERED", continuity="GAP_BEFORE")
        _api_round(session, "gap-stream", 7, now, multiplier="2.50")
        machine = new_state()
        machine.update({"state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
            "last_processed_round_id": "r-5", "last_processed_round_identity": "winner-predict:r-5",
            "last_processed_round_index": 5, "last_processed_stream_id": "gap-stream",
            "last_processed_position": 5, "live_rolling_outcomes": ["H"] * 5,
            "last_dominance_state": "HIGH_DOMINANT"})
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        if state_row:
            state_row.payload = machine
            state_row.updated_at = now
        else:
            session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id,
                payload=machine, updated_at=now))

    processor = LiveStrategyProcessor(engine)
    gap = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "GAP_BEFORE", "delivery_mode": "RECOVERED",
        "behaviour_provenance": "RECOVERY_REPLAY", "round_id": "r-8", "round_uid": "r-8",
        "round_index": 8, "stream_id": "gap-stream", "multiplier": 1.5}
    replayed = processor.recover(gap)
    assert replayed["status"] == "WAITING_FOR_LIVE"
    assert replayed["gap_boundary"] is True
    assert replayed["eligibility_created"] is False

    live = {**gap, "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "LIVE_VERIFIED", "round_id": "r-9", "round_uid": "r-9",
        "round_index": 9, "multiplier": 2.5}
    decision = processor.process(live)
    assert decision["status"] == "PROCESSED"
    assert decision["decision"]["eligibility"] == "INELIGIBLE"
    assert decision["decision"]["current_dominance_state"] == "INSUFFICIENT_DATA"
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        assert machine["live_rolling_outcomes"] == ["L", "H"]
        assert machine["state"] == "ARMED_FOR_HIGH"
    engine.dispose()


def test_live_status_separates_orbit_connection_from_verified_round(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'live-status.sqlite3'}")
    migrate(engine)
    monkeypatch.setattr(behaviour_api, "_read_orbit_live_status", lambda: {
        "source_status": "CONNECTED", "stream_status": "CONNECTED",
        "collector_state": "RECOVERING", "coverage": "AMBIGUOUS_SNAPSHOT",
        "latest_browser_observed_multiplier": 1.35,
        "last_browser_observation_at": "2026-11-18T06:59:51.635Z",
        "latest_verified_round_uid": None, "latest_verified_at": None,
        "latest_persisted_round_uid": "old-round", "latest_persisted_at": "2026-11-17T23:59:44Z",
        "integrity_reason": "AMBIGUOUS_HISTORY_CONTAINER", "ingress_backlog": 0,
        "round_status": "WAITING_FOR_VERIFIED_ROUND",
    })
    app = create_app(engine=engine)
    async def read_status():
        async with app.router.lifespan_context(app):
            return invoke_api_route(app, "/api/v1/behaviour/live-status", "GET")
    payload = asyncio.run(read_status())
    assert payload["orbit_source"] == "CONNECTED"
    assert payload["orbit"]["latest_browser_observed_multiplier"] == 1.35
    assert payload["orbit"]["latest_verified_round_uid"] is None
    assert payload["orbit"]["latest_persisted_round_uid"] == "old-round"
    assert payload["orbit"]["round_status"] == "WAITING_FOR_VERIFIED_ROUND"
    engine.dispose()


def test_collector_status_reader_keeps_recovered_rounds_out_of_live_label(tmp_path, monkeypatch):
    monkeypatch.setattr(behaviour_settings, "PROJECT_ROOT", tmp_path)
    status_path = tmp_path / "data" / "bot" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text('{"state":"RECOVERING","orbitApi":{"status":"CONNECTED",'
        '"streamStatus":"CONNECTED"},"roundHistoryHealth":{"lastVerifiedAt":"2026-11-18T06:59:51Z",'
        '"latestVerifiedRoundId":"old-round","latestVerifiedDeliveryMode":"RECOVERED",'
        '"latestPersistedRoundId":"old-round","coverage":"GAP_SUSPECTED"}}', encoding="utf-8")

    payload = behaviour_api._read_orbit_live_status()

    assert payload["stream_status"] == "CONNECTED"
    assert payload["latest_verified_round_uid"] == "old-round"
    assert payload["latest_verified_delivery_mode"] == "RECOVERED"
    assert payload["round_status"] == "RECOVERY_CANDIDATE"
