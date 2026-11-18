from datetime import datetime, timezone
from decimal import Decimal
import math

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from behaviour_detection.analysis import build_recovery_reference, compare_historical_recovery
from behaviour_detection.api import create_app
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import (
    CanonicalRound, CumulativeStats, Next100Assessment, OrbitReading, OrbitWatchState,
    ResearchImport, SystemHealth,
)


def _normalized_item(index, multiplier, source_item_indices=None):
    return {"item_index": index, "multiplier_raw": multiplier,
            "multiplier_normalized": str(Decimal(multiplier).normalize()),
            "visible": True, "tag_name": "div",
            "source_item_indices": source_item_indices or [index]}


def test_historical_analogue_is_descriptive_and_never_claims_recovery_probability():
    context = ["H"] * 17 + ["L"] * 31 + ["E"] * 2
    phase1 = context + ["H"] * 50 + ["L"] * 50 + ["H"] * 25 + ["L"] * 25
    comparison = compare_historical_recovery(
        context, build_recovery_reference(phase1), phase1.count("H") / (phase1.count("H") + phase1.count("L")))

    assert comparison["counts"] == {"H": 17, "L": 31, "E": 2}
    assert comparison["matched_historical_windows"] >= 1
    assert comparison["future_50"]["matched_windows"] == comparison["matched_historical_windows"]
    assert comparison["future_100"]["matched_windows"] == comparison["matched_historical_windows"]
    assert comparison["recovery_likelihood"] is None
    assert comparison["predictive_adjustment_applied"] is False
    assert comparison["evidence_status"] == "DESCRIPTIVE_ANALOGUES_ONLY"


def test_overview_uses_orbit_seed_for_live_rolling50_and_keeps_session_history_separate(tmp_path, monkeypatch):
    monkeypatch.setattr("behaviour_detection.api.settings.WINNER_PREDICT_DATABASE_URL", "")
    engine = make_engine(f"sqlite:///{tmp_path / 'live-context.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    phase1 = ["H"] * 17 + ["L"] * 31 + ["E"] * 2 + ["H"] * 50 + ["L"] * 50 + ["H"] * 25 + ["L"] * 25
    high, low, exact = (phase1.count(symbol) for symbol in ("H", "L", "E"))
    values = ["3.00" if symbol == "H" else "1.00" if symbol == "L" else "2.00" for symbol in phase1]
    multiplier_sum = sum((Decimal(value) for value in values), Decimal("0"))
    log_values = [math.log(float(value)) for value in values]
    context50 = phase1[:50]
    chronological = ["L"] * 10 + context50
    newest_first = list(reversed(chronological))
    duplicated_newest_first = newest_first + newest_first

    with Session(engine) as session, session.begin():
        session.add(ResearchImport(
            snapshot_id="live-context-fixture", snapshot_utc=now,
            source_sha256="a" * 64, input_files_sha256="b" * 64,
            source_records=len(phase1), canonical_rounds=len(phase1), high_count=high,
            low_count=low, exact_count=exact, imported_at=now))
        session.add_all(CanonicalRound(
            identity_key=f"phase1:{position}", round_id=str(position), source_round_index=None,
            source_sequence=position, analysis_position=position,
            multiplier_raw=value, multiplier_value=Decimal(value), outcome=symbol,
            event_timestamp=None, ingestion_timestamp=None, source_name="phase1",
            timestamp_quality="missing", order_quality="fixture_order",
            import_snapshot_id="live-context-fixture", inserted_at=now)
            for position, (symbol, value) in enumerate(zip(phase1, values), start=1))
        session.add(CumulativeStats(
            scope="global", round_count=len(phase1), high_count=high, low_count=low, exact_count=exact,
            event_timestamp_count=0, event_timestamp_missing_count=len(phase1), timestamp_tie_count=0,
            order_uncertain_count=0, cross_source_overlap_unknown_count=0,
            minimum_multiplier=Decimal("1.00"), maximum_multiplier=Decimal("3.00"),
            multiplier_sum=multiplier_sum, log_multiplier_sum=sum(log_values),
            log_multiplier_square_sum=sum(value * value for value in log_values),
            tail_counts={"ge_5": 0, "ge_10": 0, "ge_100": 0, "ge_1000": 0},
            updated_at=now))
        session.add(Next100Assessment(
            assessed_at=now, latest_identity_key="phase1:200", latest_position=len(phase1),
            training_blocks=5, expected_high_rate_nonexact=.49, expected_exact_rate=.01,
            expected_high_count=48.51, expected_low_count=50.49, expected_exact_count=1,
            historical_count_distribution={"high_count": {"p05": 42, "p50": 49, "p95": 58}},
            threshold_probabilities={}, data_sufficiency="SUPPORTED_HISTORICAL_BASELINE",
            state="UNCERTAIN", active_rules=[], explanation={"baseline_evidence": "Fixture baseline."}))
        raw_reading = {"id": "42", "checkedAt": now.isoformat().replace("+00:00", "Z"),
                       "count": len(duplicated_newest_first),
                       "items": [{"index": index,
                                  "value": f"{'3.00' if symbol == 'H' else '1.00' if symbol == 'L' else '2.00'}x",
                                  "visible": True, "tagName": "div"}
                                 for index, symbol in enumerate(duplicated_newest_first)]}
        session.add(OrbitReading(watch_id="watch-fixture", reading_id="42", checked_at=now,
                                 received_at=now, raw_payload=raw_reading, status="VERIFIED_NO_CHANGE"))
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-fixture", cursor="42",
            last_snapshot=[_normalized_item(index,
                                            "3.00" if symbol == "H" else "1.00" if symbol == "L" else "2.00",
                                            [index, index + len(newest_first)])
                           for index, symbol in enumerate(newest_first)],
            source_position=len(phase1), continuity_status="cross_source_overlap_unverified",
            updated_at=now))
        session.add(SystemHealth(component="orbit_collector", state="DEGRADED", checked_at=now,
                                 details={"error_code": "CHROME_CONNECTION_OFFLINE",
                                          "latest_source_cursor": "43",
                                          "latest_source_checked_at": "2026-11-13T21:07:00+00:00"}))

    with TestClient(create_app(engine)) as client:
        response = client.get("/api/v1/behaviour/overview")
        state_response = client.get("/api/v1/behaviour/state")
        sequence_response = client.get("/api/v1/behaviour/sequences")
        next100_response = client.get("/api/v1/behaviour/next100")
    assert response.status_code == 200
    payload = response.json()
    live = payload["live_context"]
    assert live["context_status"] == "VALID"
    assert live["rolling_window_rounds"] == 50
    assert live["rolling50"]["counts"] == {"H": 17, "L": 31, "E": 2}
    assert live["validation"]["duplicates"]["provider_visible_items"] == 120
    assert live["validation"]["duplicates"]["coalesced_duplicate_items"] == 60
    assert live["validation"]["duplicates"]["exact_duplicate_halves_coalesced"] is True
    assert live["latest_round"]["outcome"] == "E"
    assert live["latest_round"]["event_timestamp"] is None
    assert live["source_reading_id"] == "42"
    assert live["latest_source_reading_id"] == "43"
    assert live["source_checked_at"] == "2026-11-13T21:07:00+00:00"
    assert datetime.fromisoformat(live["context_source_checked_at"]).replace(
        tzinfo=timezone.utc) == now
    assert live["validation"]["per_round_timestamp_gaps"].startswith("unavailable")
    assert live["session_history"]["round_count"] == 0
    assert live["session_history"]["seed_context_merged"] is False
    assert payload["next100"]["expected_high_rate_nonexact"] == .49
    recovery = payload["next100"]["historical_recovery_comparison"]
    assert recovery["recovery_likelihood"] is None
    assert recovery["matched_historical_windows"] >= 1
    assert payload["health"]["orbit_collector"]["state"] == "DEGRADED"
    assert state_response.json()["rolling_windows"]["50"]["counts"] == {"H": 17, "L": 31, "E": 2}
    assert sequence_response.json()["recent_sequence_hle"] == live["recent_sequence_hle"]
    assert next100_response.json()["historical_recovery_comparison"]["recovery_likelihood"] is None
    engine.dispose()
