from datetime import datetime, timezone

from sqlalchemy.orm import Session

from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import CanonicalRound
from behaviour_detection.worker import _maybe_add_completed_block


def _round(position):
    outcome = "H" if position % 2 else "L"
    value = "2.10" if outcome == "H" else "1.90"
    return CanonicalRound(identity_key=f"fixture:{position}", round_id=None,
                          source_round_index=None, source_sequence=None,
                          analysis_position=position, multiplier_raw=value,
                          multiplier_value=value, outcome=outcome, event_timestamp=None,
                          ingestion_timestamp=datetime.now(timezone.utc), source_name="fixture",
                          timestamp_quality="unknown", order_quality="fixture_order",
                          import_snapshot_id=None, inserted_at=datetime.now(timezone.utc))


def test_live_reference_block_starts_after_frozen_snapshot_without_overlap():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    with Session(engine) as session, session.begin():
        session.add_all(_round(position) for position in range(50124, 50224))
    with Session(engine) as session, session.begin():
        assert _maybe_add_completed_block(session, 50200) is None
        block = _maybe_add_completed_block(session, 50223)
        assert block["first_position"] == 50124
        assert block["last_position"] == 50223
        assert block["high_count"] + block["low_count"] == 100
    engine.dispose()
