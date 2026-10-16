from datetime import datetime, timezone

import pytest

from app.round_buffer import InMemoryRoundBuffer


def round_event(round_id, index, *, session="collector-one", continuity=True):
    return {
        "round_id": round_id,
        "round_index": index,
        "multiplier": 1.5 + index / 10,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "collector_session_id": session,
        "continuity_verified": continuity,
    }


def test_buffer_deduplicates_orders_chronologically_and_resets_on_collector_session_change():
    buffer = InMemoryRoundBuffer(capacity=100)
    later = round_event("r2", 2)
    earlier = round_event("r1", 1)
    buffer.append(later)
    buffer.append(earlier)
    duplicate = buffer.append(dict(earlier))

    snapshot = buffer.snapshot()
    assert duplicate["inserted"] is False
    assert [row["round_id"] for row in snapshot["rounds"]] == ["r1", "r2"]
    assert snapshot["count"] == 2
    assert snapshot["continuity_after_restart"] is False

    new_session = buffer.append(round_event("new-session-r1", 1, session="collector-two"))
    assert new_session["reset"] is True
    assert new_session["snapshot"]["count"] == 1
    assert new_session["snapshot"]["reset_count"] == 1
    assert new_session["snapshot"]["continuity_after_restart"] is False


def test_buffer_evicts_oldest_round_at_bounded_capacity_and_rejects_conflicting_duplicate():
    buffer = InMemoryRoundBuffer(capacity=100)
    for index in range(1, 102):
        buffer.append(round_event(f"r{index}", index))

    snapshot = buffer.snapshot()
    assert snapshot["capacity"] == 100
    assert snapshot["count"] == 100
    assert snapshot["rounds"][0]["round_index"] == 2
    assert snapshot["rounds"][-1]["round_index"] == 101
    with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
        buffer.append({**round_event("r101", 101), "multiplier": 9.0})


def test_process_reset_clears_rounds_and_changes_buffer_epoch():
    buffer = InMemoryRoundBuffer()
    buffer.append(round_event("r1", 1))
    before = buffer.snapshot()["epoch_id"]

    buffer.reset()
    after = buffer.snapshot()

    assert after["epoch_id"] != before
    assert after["rounds"] == []
    assert after["count"] == 0
    assert after["continuity_after_restart"] is False
