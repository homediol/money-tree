from datetime import datetime, timezone
from types import SimpleNamespace
import asyncio
from pathlib import Path

from app.adie_engine import ADIEEngine, classify_multiplier
from app.api import adie as adie_api
from app.database.repository import Repository
from app.round_distribution import RoundDistributionHub
from app.round_observation_inbox import RoundObservationInbox
from app.rules_engine import RulesEngine
from main import collector_round_observed


def event(round_id, multiplier, *, continuity="VERIFIED", delivery="LIVE", sequence=None):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "round_id": round_id, "multiplier": multiplier,
        "observed_at": now, "api_received_at": now,
        "continuity_status": continuity, "delivery_mode": delivery,
        "identity_source": "LOCAL_SEQUENCE", "round_index": sequence,
        "stream_id": "test-stream", "stream_sequence": sequence,
    }


def engine(tmp_path):
    repository = Repository(tmp_path / "adie.sqlite3")
    repository.init()
    result = ADIEEngine(repository)
    # Unit tests do not need to repeatedly scan the 53k-row fixed reference.
    result.reference_rows = []
    result.reference_matrix = result.reference_matrix[:0]
    result.reference_positions = result.reference_positions[:0]
    return result


def test_classifier_keeps_exact_separate():
    assert classify_multiplier(2.01) == "HIGH"
    assert classify_multiplier(1.99) == "LOW"
    assert classify_multiplier(2.0) == "EXACT"


def test_fixed_horizon_controls_are_prospective_and_exact_is_separate(tmp_path):
    adie = engine(tmp_path)
    adie.process_observed(event("decision", 1.2, sequence=1))
    for i, multiplier in enumerate((2.0, 1.3, 2.5, 1.5, 1.4), start=2):
        adie.process_observed(event(f"target-{i}", multiplier, sequence=i))
    perf = adie.performance()["fixed_horizon_baselines"]
    high_five = next(row for row in perf["groups"] if row["direction"] == "HIGH" and row["horizon"] == 5)
    assert high_five["completed"] == 1
    assert high_five["wins"] == 2  # one HIGH and one EXACT
    assert high_five["losses"] == 3
    assert high_five["net"] == -1


def test_duplicate_and_noncontinuous_rounds_do_not_count_activity(tmp_path):
    adie = engine(tmp_path)
    first = event("r1", 1.2, sequence=1)
    assert adie.process_observed(first)["eligible"] is True
    assert adie.process_observed(first)["duplicate"] is True
    gap = adie.process_observed(event("r2", 3.5, continuity="GAP_BEFORE", sequence=3))
    assert gap["eligible"] is False
    assert adie.status()["eligible_since_selection"] == 0


def test_verified_flag_cannot_override_source_index_gap_or_inversion(tmp_path):
    adie = engine(tmp_path)
    assert adie.process_observed(event("idx-10", 1.4, sequence=10))["eligible"]
    gap = adie.process_observed(event("idx-12", 1.5, sequence=12))
    assert gap["eligible"] is False
    assert adie.decisions(limit=1)[0]["abstention_reason"] == "source_round_index_gap"
    inversion = adie.process_observed(event("idx-11", 2.5, sequence=11))
    assert inversion["eligible"] is False
    assert adie.decisions(limit=1)[0]["abstention_reason"] == "source_round_index_not_increasing"


def test_forced_selection_is_committed_by_round_100_and_exact_settles_high(tmp_path):
    adie = engine(tmp_path)
    selection = None
    for i in range(1, 101):
        result = adie.process_observed(event(f"r{i}", 1.5, sequence=i))
        if result.get("selected"):
            selection = result["selected"]
    assert selection is not None
    assert selection["selection_status"] == "FORCED_EXPLORATORY"
    assert selection["horizon"] == 5
    assert selection["target_start_position"] == 101
    assert selection["target_end_position"] == 105
    for i, multiplier in enumerate((2.0, 2.4, 1.2, 2.0, 1.5), start=101):
        adie.process_observed(event(f"r{i}", multiplier, sequence=i))
    forecast = adie.forecasts(limit=1)[0]
    assert forecast["status"] == "COMPLETED"
    assert forecast["target_start_round_id"] == "r101"
    assert forecast["target_end_round_id"] == "r105"
    assert (forecast["high_count"], forecast["low_count"], forecast["exact_count"]) == (1, 2, 2)
    # Forced first side is HIGH; EXACT counts as a HIGH accounting win.
    assert forecast["wins"] == 3
    assert forecast["losses"] == 2
    assert forecast["net_units"] == 1
    assert adie.performance()["totals"]["completed"] == 1


def test_delayed_targets_wait_then_activate_and_lock_exact_identities(tmp_path):
    adie = engine(tmp_path)
    adie._choose_normal = lambda _features, _summary: (
        {"direction": "LOW", "horizon": 5, "offset": 2, "evidence": {"test": True}},
        [], "test_candidate")
    first = adie.process_observed(event("decision", 3.0, sequence=1))
    selected = first["selected"]
    assert selected["status"] == "WAITING"
    assert selected["target_start_position"] == 4
    # A process restart reloads the immutable target contract from the same DB.
    adie = engine(tmp_path)
    adie.process_observed(event("p2", 3, sequence=2))
    adie.process_observed(event("p3", 1.5, sequence=3))
    waiting = adie.forecasts(limit=1)[0]
    assert waiting["status"] == "WAITING"
    adie.process_observed(event("target-start", 2.0, sequence=4))
    active = adie.forecasts(limit=1)[0]
    assert active["status"] == "ACTIVE"
    assert active["target_start_round_id"] == "target-start"


def test_gap_marks_active_window_incomplete_without_settlement(tmp_path):
    adie = engine(tmp_path)
    adie._choose_normal = lambda _features, _summary: (
        {"direction": "HIGH", "horizon": 5, "offset": 0, "evidence": {}}, [], "test")
    adie.process_observed(event("decision", 1.2, sequence=1))
    adie.process_observed(event("gap", 1.4, continuity="GAP_BEFORE", sequence=3))
    forecast = adie.forecasts(limit=1)[0]
    assert forecast["status"] == "INCOMPLETE"
    assert forecast["invalidation_reason"] == "continuity_gap_before"
    assert forecast["wins"] == 0


def test_collector_observed_ingestion_commits_before_next_target_is_processed(tmp_path):
    adie = engine(tmp_path)
    repository = adie.repository
    rules = RulesEngine()
    inbox = RoundObservationInbox(tmp_path / "observed-inbox.sqlite3")
    app = SimpleNamespace(state=SimpleNamespace(
        rules_engine=rules, wp=SimpleNamespace(repository=repository), adie_engine=adie,
        adie_executor=None, round_processing_executor=None,
        round_observation_inbox=inbox, round_stream_id="test-stream",
        round_distribution=RoundDistributionHub(),
    ))
    for i in range(1, 100):
        adie.process_observed(event(f"prior-{i}", 1.4, sequence=i))

    async def send(round_id, multiplier, index):
        observed_at = datetime.now(timezone.utc).isoformat()
        body = {"source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [{
            "round_id": round_id, "multiplier": multiplier,
            "observed_at": observed_at, "round_index": index,
            "round_index_source": "LOCAL_SEQUENCE", "continuity_verified": True,
            "gap_before": False, "delivery_mode": "LIVE",
        }]}
        async def json_body(): return body
        request = SimpleNamespace(
            app=app, json=json_body,
            client=SimpleNamespace(host="127.0.0.1"),
            url=SimpleNamespace(hostname="127.0.0.1"),
        )
        return await collector_round_observed(request)

    async def replay():
        try:
            result = await send("live-100", 3.2, 100)
            assert result["status"] == "ROUND_OBSERVED_RULES_DECISION_READY"
            locked = adie.forecasts(limit=1)[0]
            assert locked["decision_round_id"] == "live-100"
            assert locked["status"] == "LOCKED"
            assert locked["target_start_position"] == 101
            await send("live-101", 2.0, 101)
            settled = adie.forecasts(limit=1)[0]
            assert settled["target_start_round_id"] == "live-101"
            assert settled["exact_count"] == 1
            assert settled["wins"] == 1
        finally:
            task = getattr(app.state, "round_event_processor_task", None)
            if task is not None:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            inbox.close()

    asyncio.run(replay())


def test_read_only_api_contract_and_migration_cover_adie_ledgers(tmp_path):
    adie = engine(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        adie_engine=adie, history_collector=None)))
    status = asyncio.run(adie_api.status(request))
    forecasts = asyncio.run(adie_api.forecasts(request, limit=10, status=None))
    decisions = asyncio.run(adie_api.decisions(request, limit=10))
    performance = asyncio.run(adie_api.performance(request))
    assert status["policy_version"] == "adie-shadow-exploratory-v1"
    assert status["collector"]["status"] == "UNKNOWN"
    assert forecasts == {"items": []}
    assert decisions == {"items": []}
    assert performance["accounting"] == "SIMULATED_SHADOW_FIXED_STAKE_2X_NOT_REALIZED_PROFIT"
    migration = Path(__file__).parents[1] / "migrations" / "20261116_adie_shadow_intelligence.sql"
    sql = migration.read_text(encoding="utf-8")
    for table in ("adie_state", "adie_round_events", "adie_decisions", "adie_forecasts",
                  "adie_forecast_rounds", "adie_baseline_windows", "adie_baseline_window_rounds"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

