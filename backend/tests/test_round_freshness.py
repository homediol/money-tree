from __future__ import annotations

import asyncio

from app.api.history import _round_stream_state
from app.round_distribution import RoundDistributionHub


def _event(round_id: str, observed_at: str, *, delivery="LIVE", continuity="VERIFIED"):
    return {
        "event_type": "round.observed",
        "event_id": round_id,
        "observation_id": round_id,
        "round_id": round_id,
        "round_uid": round_id,
        "observed_at": observed_at,
        "continuity_status": continuity,
        "delivery_mode": delivery,
    }


def test_hub_uses_only_verified_live_rounds_for_empirical_freshness_cadence():
    async def scenario():
        hub = RoundDistributionHub()
        await hub.publish(_event("recovered", "2026-11-18T08:00:00Z", delivery="RECOVERED",
                                 continuity="GAP_BEFORE"))
        await hub.publish(_event("live-1", "2026-11-18T08:10:00Z"))
        await hub.publish(_event("live-2", "2026-11-18T08:10:20Z"))
        await hub.publish(_event("live-3", "2026-11-18T08:10:45Z"))
        cadence = (await hub.metrics())["verified_live_round_interval_ms"]
        assert cadence["samples"] == 2
        assert cadence["p50_ms"] == 20000.0
        assert cadence["p95_ms"] == 25000.0
        assert cadence["max_ms"] == 25000.0

    asyncio.run(scenario())


def test_round_stream_freshness_waits_for_empirical_cadence_and_then_goes_stale():
    live = _event("live", "2026-11-18T08:10:00Z")
    assert _round_stream_state(observed=live, verified_live=live, age_seconds=4,
                               freshness_limit_ms=None) == "WAITING_FOR_CADENCE"
    assert _round_stream_state(observed=live, verified_live=live, age_seconds=20,
                               freshness_limit_ms=25000) == "LIVE"
    assert _round_stream_state(observed=live, verified_live=live, age_seconds=26,
                               freshness_limit_ms=25000) == "STALE"
    recovered = _event("gap", "2026-11-18T08:10:00Z", delivery="RECOVERED",
                       continuity="GAP_BEFORE")
    assert _round_stream_state(observed=recovered, verified_live=None, age_seconds=0,
                               freshness_limit_ms=25000) == "RECONCILING"
    assert _round_stream_state(observed=None, verified_live=None, age_seconds=None,
                               freshness_limit_ms=None) == "WAITING_FOR_LIVE"
