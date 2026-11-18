from behaviour_detection.collector import (
    _bootstrap_from_history, _find_stopped_watch, _match_delta, _persist_reading,
    _prepare_watch_switch_reanchor, _visible_dom, _watch_source_health, collect_once,
)
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session
import httpx
from behaviour_detection.collector import _archive_invalid
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import (
    ApiError, CanonicalRound, CollectorActivity, OrbitReading, OrbitRoundMapping,
    OrbitWatchState, SystemHealth,
)
from behaviour_detection.orbit_client import (
    MAX_RESPONSE_BYTES, PAYOUT_SELECTOR, OrbitContractError, OrbitDataWatchClient, parse_reading,
    select_watch, validate_latest_snapshot,
)


def _dom(values):
    return [{"item_index": i, "multiplier_raw": str(value),
             "multiplier_normalized": str(Decimal(str(value)).normalize()), "visible": True}
            for i, value in enumerate(values)]


def test_unique_overlap_proves_only_new_rounds():
    previous = [f"1.{i:02d}" for i in range(11, 31)]
    current = ["1.31", "1.32", *previous]
    delta = _match_delta(previous, _dom(current))
    assert delta["status"] == "VERIFIED_DELTA"
    assert delta["overlap"] == 20
    assert [item["multiplier_raw"] for item in delta["new_items"]] == ["1.32", "1.31"]


def test_repeated_values_do_not_invent_an_ambiguous_delta():
    delta = _match_delta(["1.20"] * 20, _dom(["1.21", "1.20"] * 10 + ["1.20"] * 20))
    assert delta["status"] == "CONTINUITY_UNVERIFIED"
    assert delta["new_items"] == []


def test_exact_duplicate_dom_halves_coalesce_with_both_source_indices_preserved():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    _historical_tail_fixture(engine)
    previous = [f"1.{position:02d}" for position in range(20, 0, -1)]
    one_visible_history = ["2.25", *previous[:19]]
    raw = _reading(42, one_visible_history + one_visible_history)
    raw["items"] = [{**item, "tagName": "div"} for item in raw["items"]]
    parsed = parse_reading(raw)
    current = _visible_dom(parsed)
    with Session(engine) as session, session.begin():
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-1", cursor="41",
            last_snapshot=_dom(previous + previous), source_position=20,
            continuity_status="cross_source_overlap_unverified", updated_at=datetime.now(timezone.utc)))
        session.add(OrbitReading(
            watch_id="watch-1", reading_id="42", checked_at=parsed.checked_at,
            received_at=datetime.now(timezone.utc), raw_payload=raw,
            status="quarantined_ambiguous_delta", validation_error="NO_UNIQUE_OVERLAP"))
    delta = _match_delta(previous, current)

    assert len(current) == 20
    assert delta["status"] == "VERIFIED_DELTA"
    assert delta["new_count"] == 1
    assert delta["current_duplicate_halves_coalesced"] is True
    assert delta["new_items"][0]["source_item_indices"] == [0, 20]
    assert len(raw["items"]) == 40  # raw provider provenance remains intact

    result = _persist_reading(engine, "watch-1", raw, parsed, current,
                              previous_snapshot=None)
    with Session(engine) as session:
        rounds = session.scalars(select(CanonicalRound).where(
            CanonicalRound.source_name == "orbit_data_watch")).all()
        mappings = session.scalars(select(OrbitRoundMapping).order_by(
            OrbitRoundMapping.item_index)).all()
        archived = session.scalar(select(OrbitReading).where(OrbitReading.reading_id == "42"))
        assert result["new_rounds"] == 1
        assert len(rounds) == 1 and rounds[0].multiplier_raw == "2.25"
        assert [(m.identity_key, m.item_index) for m in mappings] == [
            (rounds[0].identity_key, 0), (rounds[0].identity_key, 20)]
        assert len(archived.raw_payload["items"]) == 40
        assert archived.status == "VERIFIED_DELTA"
    engine.dispose()


def test_contract_keeps_checked_at_as_reading_time_and_requires_selector():
    watch = {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running"}
    reading = {"id": "1", "checkedAt": "2026-11-13T08:30:00Z", "count": 1,
               "items": [{"index": 0, "value": "2.00x", "visible": True}]}
    assert parse_reading(reading).items[0].multiplier_raw == "2.00"
    assert validate_latest_snapshot({"watch": watch, "latest": reading, "cursor": "1"}, watch)[0] == "1"
    assert select_watch({"watches": [watch]}) == watch
    try:
        validate_latest_snapshot({"watch": None, "latest": reading, "cursor": "1"}, watch)
    except OrbitContractError as exc:
        assert exc.code == "INVALID_LATEST_WATCH"
    else:
        raise AssertionError("invalid watch shape was accepted")


def test_watch_selection_prefers_running_configured_watch_then_first_running_fallback():
    stopped = {"id": "stopped-watch", "selector": PAYOUT_SELECTOR, "status": "stopped"}
    first_running = {"id": "first-running", "selector": PAYOUT_SELECTOR, "status": "running"}
    second_running = {"id": "second-running", "selector": PAYOUT_SELECTOR, "status": "running"}
    response = {"watches": [stopped, first_running, second_running]}

    assert select_watch(response, "stopped-watch") == first_running
    assert select_watch(response) == first_running
    assert select_watch(response, "second-running") == second_running
    try:
        select_watch(response, "stopped-watch", allow_fallback=False)
    except OrbitContractError as exc:
        assert exc.code == "WATCH_NOT_RUNNING_OR_INVALID"
    else:
        raise AssertionError("a durable Watch cursor was allowed to switch to another source")


def test_watch_selection_does_not_fall_back_from_a_mismatched_configured_selector():
    response = {"watches": [
        {"id": "configured", "selector": ".other", "status": "stopped"},
        {"id": "running", "selector": PAYOUT_SELECTOR, "status": "running"},
    ]}
    try:
        select_watch(response, "configured")
    except OrbitContractError as exc:
        assert exc.code == "SAVED_WATCH_SELECTOR_MISMATCH"
    else:
        raise AssertionError("selector mismatch was silently redirected to another watch")


def test_watch_switch_archives_old_cursor_before_reanchor():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    saved = OrbitWatchState(
        singleton_id=1, watch_id="saved-watch", cursor="6924", last_snapshot=_dom(["1.50"] * 20),
        source_position=50123, continuity_status="cross_source_overlap_unverified",
        updated_at=datetime.now(timezone.utc))
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.add(saved)

    target = {"id": "live-watch", "selector": PAYOUT_SELECTOR, "status": "running"}
    switched = _prepare_watch_switch_reanchor(
        engine, saved, target,
        {"upstream_collector_status": "waiting_for_connection"},
        {"upstream_collector_status": "running"})

    with Session(engine) as session:
        assert switched is True
        assert session.get(OrbitWatchState, 1) is None
        activity = session.scalar(select(CollectorActivity))
        assert activity.event_type == "orbit_watch_switch_reanchor_started"
        assert activity.details["previous_watch_id"] == "saved-watch"
        assert activity.details["previous_cursor"] == "6924"
        assert len(activity.details["previous_last_snapshot"]) == 20
        assert activity.details["intervening_rounds_status"] == "UNKNOWN_AND_NOT_INFERRED"
        health = session.get(SystemHealth, "orbit_collector")
        assert health.state == "DEGRADED"
        assert health.details["watch_id"] == "live-watch"
    engine.dispose()


def test_host_gateway_aliases_are_allowed_for_container_local_http_only():
    for host in ("host.containers.internal", "host.docker.internal"):
        client = OrbitDataWatchClient(f"http://{host}:4000", "test-key", attempts=1)
        client.close()
    for address in ("http://example.com:4000", "http://192.168.1.8:4000"):
        try:
            OrbitDataWatchClient(address, "test-key", attempts=1)
        except OrbitContractError as exc:
            assert exc.code == "INVALID_BASE_URL"
        else:
            raise AssertionError("non-local plain HTTP Orbit endpoint was accepted")


def test_api_reachability_does_not_hide_offline_orbit_browser_collector():
    state, details = _watch_source_health({
        "status": "running", "collectorStatus": "waiting_for_connection",
        "lastError": "This Chrome connection is offline. Reconnect the extension and try again."})
    assert state == "DEGRADED"
    assert details == {"upstream_collector_status": "waiting_for_connection",
                       "upstream_error_code": "CHROME_CONNECTION_OFFLINE"}


def test_stopped_saved_watch_is_reported_only_for_the_exact_aviator_watch():
    stopped = {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "stopped",
               "collectorStatus": "stopped"}
    assert _find_stopped_watch({"watches": [stopped]}, "watch-1") == stopped
    assert _find_stopped_watch({"watches": [stopped]}, "other-watch") is None
    assert _find_stopped_watch({"watches": [{**stopped, "status": "running"}]},
                               "watch-1") is None
    assert _find_stopped_watch({"watches": [{**stopped, "selector": ".other"}]},
                               "watch-1") is None


def test_collector_health_is_degraded_when_orbit_browser_is_offline():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    raw = _reading(42, ["1.50"] * 20)
    parsed = parse_reading(raw)
    received = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-1", cursor="42", last_snapshot=_dom(["1.50"] * 20),
            source_position=0, continuity_status="cross_source_overlap_unverified",
            updated_at=received))
        session.add(OrbitReading(
            watch_id="watch-1", reading_id="42", checked_at=parsed.checked_at,
            received_at=received, raw_payload=raw, status="VERIFIED_NO_CHANGE"))

    class _OfflineClient:
        def latest(self, watch_id):
            return {"watch": {"id": watch_id, "selector": PAYOUT_SELECTOR},
                    "latest": raw, "cursor": "42"}

    watch = {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running",
             "collectorStatus": "waiting_for_connection",
             "lastError": "This Chrome connection is offline."}
    result = collect_once(engine, _OfflineClient(), watch)
    with Session(engine) as session:
        health = session.get(SystemHealth, "orbit_collector")
        assert result["status"] == "connected_no_new_reading"
        assert health.state == "DEGRADED"
        assert health.details["status"] == "SOURCE_WAITING_FOR_CONNECTION"
        assert health.details["upstream_error_code"] == "CHROME_CONNECTION_OFFLINE"
    engine.dispose()


def test_malformed_watch_selector_is_reported_as_contract_error():
    response = {"watches": [{"id": "watch-null-selector", "selector": None,
                             "status": "running"}]}
    try:
        select_watch(response)
    except OrbitContractError as exc:
        assert exc.code == "AVIATOR_PAYOUT_WATCH_UNAVAILABLE"
    else:
        raise AssertionError("malformed selector was accepted")


def test_orbit_multiplier_parser_preserves_all_provider_decimal_digits_for_classification():
    values = ["1.9999999999", "2.0000000001", "2.000000000000000000000001"]
    parsed = [parse_reading(_reading(index + 1, [value])).items[0]
              for index, value in enumerate(values)]
    assert [item.multiplier_raw for item in parsed] == values
    assert [item.multiplier for item in parsed] == [Decimal(value) for value in values]
    assert ["L" if item.multiplier < 2 else "H" for item in parsed] == ["L", "H", "H"]


def test_orbit_response_size_limit_is_enforced_while_streaming():
    client = OrbitDataWatchClient("http://127.0.0.1", "test-key", attempts=1)
    client._client.close()
    body = b'{"value":"' + b"x" * MAX_RESPONSE_BYTES + b'"}'
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body)))
    try:
        client._get("/bounded")
    except OrbitContractError as exc:
        assert exc.code == "RESPONSE_TOO_LARGE"
    else:
        raise AssertionError("oversized Orbit payload was accepted")
    finally:
        client.close()


def test_invalid_provider_payloads_are_quarantined_without_identity_collision():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    _archive_invalid(engine, "watch-1", {"bad": "first"}, "", "INVALID_HISTORY_PAGE")
    _archive_invalid(engine, "watch-1", {"bad": "second"}, "", "INVALID_HISTORY_PAGE")
    _archive_invalid(engine, "watch-1", {"bad": "first"}, "", "INVALID_HISTORY_PAGE")
    with Session(engine) as session:
        rows = session.scalars(select(OrbitReading)).all()
        assert len(rows) == 2
        assert all(row.status == "quarantined_invalid" for row in rows)
        assert {row.raw_payload["bad"] for row in rows} == {"first", "second"}
    engine.dispose()


def test_same_orbit_identity_replay_is_deduplicated_and_mutation_is_quarantined():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    previous = [f"1.{i:02d}" for i in range(11, 31)]

    def payload(newest):
        values = [newest, "1.32", *previous]
        return {"id": "42", "checkedAt": "2026-11-13T08:30:00Z", "count": len(values),
                "items": [{"index": i, "value": f"{value}x", "visible": True}
                          for i, value in enumerate(values)]}

    original = payload("1.31")
    parsed = parse_reading(original)
    dom = [{"item_index": item.item_index, "multiplier_raw": item.multiplier_raw,
            "multiplier_normalized": str(item.multiplier.normalize()), "visible": True}
           for item in parsed.items]
    first = _persist_reading(engine, "watch-1", original, parsed, dom,
                             previous_snapshot=previous, initial_alignment=True)
    replay = _persist_reading(engine, "watch-1", original, parsed, dom,
                              previous_snapshot=previous, initial_alignment=True)
    altered = payload("1.33")
    parsed_altered = parse_reading(altered)
    altered_dom = [{"item_index": item.item_index, "multiplier_raw": item.multiplier_raw,
                    "multiplier_normalized": str(item.multiplier.normalize()), "visible": True}
                   for item in parsed_altered.items]
    conflict = _persist_reading(engine, "watch-1", altered, parsed_altered, altered_dom,
                                previous_snapshot=previous, initial_alignment=True)
    with Session(engine) as session:
        assert first["new_rounds"] == 2
        assert replay["duplicate_reading"] is True
        assert conflict["status"] == "quarantined_identity_conflict"
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 2
        assert session.scalar(select(func.count()).select_from(OrbitReading)) == 2
    engine.dispose()


class _HistoryClient:
    def __init__(self, readings):
        self.readings = readings

    def history(self, watch_id, after, limit):
        candidates = [reading for reading in self.readings if int(reading["id"]) > int(after)]
        items = candidates[:limit]
        next_cursor = items[-1]["id"] if items else str(after)
        return {"watchId": watch_id, "selector": PAYOUT_SELECTOR, "readings": items,
                "nextCursor": next_cursor, "hasMore": len(candidates) > len(items)}


def _reading(reading_id, newest_first, checked_at="2026-11-13T08:30:00Z"):
    return {"id": str(reading_id), "checkedAt": checked_at, "count": len(newest_first),
            "items": [{"index": index, "value": f"{value}x", "visible": True,
                       "tagName": "div"}
                      for index, value in enumerate(newest_first)]}


def _historical_tail_fixture(engine):
    with Session(engine) as session, session.begin():
        for position in range(1, 21):
            value = f"1.{position:02d}"
            session.add(CanonicalRound(
                identity_key=f"phase1:{position}", round_id=str(position),
                source_round_index=None, source_sequence=None, analysis_position=position,
                multiplier_raw=value, multiplier_value=Decimal(value),
                outcome="H" if Decimal(value) > 2 else "L", event_timestamp=None,
                ingestion_timestamp=None, source_name="phase1", timestamp_quality="missing",
                order_quality="phase1_test_order", import_snapshot_id="test", inserted_at=datetime.now(timezone.utc)))


def test_first_start_pages_history_and_anchors_before_ingesting_live_delta():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    _historical_tail_fixture(engine)
    phase1_newest_first = [f"1.{position:02d}" for position in range(20, 0, -1)]
    readings = [_reading(1, ["1.50"] * 20),
                _reading(2, phase1_newest_first),
                _reading(3, ["1.22", "1.21", *phase1_newest_first])]
    latest = readings[-1]
    result = _bootstrap_from_history(engine, _HistoryClient(readings), "watch-1", "3",
                                     latest, parse_reading(latest), _dom(["1.22", "1.21", *phase1_newest_first]),
                                     page_size=2)
    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        live_rounds = session.scalars(select(CanonicalRound).where(
            CanonicalRound.source_name == "orbit_data_watch"
        ).order_by(CanonicalRound.analysis_position)).all()
        archived = session.scalars(select(OrbitReading).order_by(OrbitReading.reading_id)).all()
        assert result["status"] == "initial_history_backfilled"
        assert result["pages"] == 2
        assert result["new_rounds"] == 2
        assert state.cursor == "3"
        assert state.continuity_status == "cross_source_overlap_verified"
        assert [row.multiplier_raw for row in live_rounds] == ["1.21", "1.22"]
        assert [row.status for row in archived] == ["quarantined_pre_anchor", "VERIFIED_NO_CHANGE", "VERIFIED_DELTA"]
    engine.dispose()


def test_first_start_without_overlap_stores_explicit_unknown_boundary():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    _historical_tail_fixture(engine)
    latest = _reading(7, ["1.90"] * 20)
    result = _bootstrap_from_history(engine, _HistoryClient([latest]), "watch-1", "7",
                                     latest, parse_reading(latest), _dom(["1.90"] * 20), page_size=10)
    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        assert result["status"] == "cross_source_overlap_unverified"
        assert state.cursor == "7"
        assert state.continuity_status == "cross_source_overlap_unverified"
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 20
        latest_row = session.scalar(select(OrbitReading).where(OrbitReading.reading_id == "7"))
        assert latest_row.status == "quarantined_continuity"
    engine.dispose()


def test_unverified_new_source_page_holds_cursor_and_reports_degraded_once():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    old_values = [f"1.{position:02d}" for position in range(60)]
    latest = _reading(43, ["1.70"] * 41)
    with Session(engine) as session, session.begin():
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-1", cursor="42", last_snapshot=_dom(old_values),
            source_position=100, continuity_status="cross_source_overlap_unverified",
            updated_at=datetime.now(timezone.utc)))

    class ChangedPageClient(_HistoryClient):
        def latest(self, _watch_id):
            return {"watch": {"id": "watch-1", "selector": PAYOUT_SELECTOR},
                    "latest": latest, "cursor": "43"}

    watch = {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running",
             "collectorStatus": "waiting_for_connection",
             "lastError": "This Chrome connection is offline."}
    client = ChangedPageClient([latest])
    assert collect_once(engine, client, watch)["status"] == "quarantined_ambiguous_delta"
    assert collect_once(engine, client, watch)["status"] == "quarantined_ambiguous_delta"
    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        health = session.get(SystemHealth, "orbit_collector")
        assert state.cursor == "42"
        assert health.state == "DEGRADED"
        assert health.details["last_verified_context_rounds"] == 60
        assert health.details["latest_source_context_rounds"] == 41
        assert session.scalar(select(func.count()).select_from(ApiError)) == 1
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 0
    engine.dispose()


def test_healthy_live_watch_quarantines_overlap_gap_and_reanchors_from_matching_latest_snapshot():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    old_values = [f"1.{position:02d}" for position in range(60)]
    current_values = [f"2.{position + 10:02d}" for position in range(42)]
    readings = [
        _reading(43, current_values, "2026-11-13T20:00:00Z"),
        _reading(44, ["2.52", *current_values[:41]], "2026-11-13T20:01:00Z"),
        _reading(45, ["2.53", *current_values[:41]], "2026-11-13T20:02:00Z"),
    ]
    latest = readings[-1]
    with Session(engine) as session, session.begin():
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-1", cursor="42", last_snapshot=_dom(old_values),
            source_position=100, continuity_status="cross_source_overlap_unverified",
            updated_at=datetime.now(timezone.utc)))

    class RunningClient(_HistoryClient):
        def latest(self, watch_id):
            return {"watch": {"id": watch_id, "selector": PAYOUT_SELECTOR},
                    "latest": latest, "cursor": latest["id"]}

    watch = {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running",
             "collectorStatus": "running"}
    result = collect_once(engine, RunningClient(readings), watch, page_size=10)
    assert result["status"] == "reanchored_after_unverified_gap"
    assert result["cursor"] == "45"
    assert result["quarantined_readings"] == 2

    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        health = session.get(SystemHealth, "orbit_collector")
        archived = session.scalars(select(OrbitReading).order_by(OrbitReading.reading_id)).all()
        assert state.cursor == "45"
        assert state.continuity_status == "within_watch_reanchored_after_unverified_gap"
        assert state.last_snapshot == _visible_dom(parse_reading(latest))
        assert [(row.reading_id, row.status) for row in archived] == [
            ("43", "quarantined_reanchor_gap"),
            ("44", "quarantined_reanchor_gap"),
            ("45", "REANCHORED_BASELINE"),
        ]
        assert health.state == "HEALTHY"
        assert health.details["quarantined_reading_count"] == 2
        assert health.details["latest_context_rounds"] == 42
        assert health.details["latest_source_checked_at"] == "2026-11-13T20:02:00+00:00"
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 0
        assert session.scalar(select(func.count()).select_from(ApiError).where(
            ApiError.error_code == "ROUND_GAP_QUARANTINED_REANCHOR")) == 1

    # The re-anchored baseline remains healthy on the next poll; it is not
    # reclassified as a failed overlap while no new source reading exists.
    assert collect_once(engine, RunningClient(readings), watch)["status"] == "connected_no_new_reading"
    with Session(engine) as session:
        assert session.get(SystemHealth, "orbit_collector").state == "HEALTHY"
        assert session.get(OrbitWatchState, 1).cursor == "45"
    engine.dispose()


def test_changed_archived_reading_holds_cursor_instead_of_skipping_to_later_overlap():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    previous = [f"1.{position:02d}" for position in range(11, 31)]
    original_conflict = _reading(43, ["1.90"] * 20)
    mutated_conflict = _reading(43, ["2.90"] * 20)
    later_overlap = _reading(44, ["1.31", "1.32", *previous])
    with Session(engine) as session, session.begin():
        session.add(OrbitWatchState(
            singleton_id=1, watch_id="watch-1", cursor="42", last_snapshot=_dom(previous),
            source_position=100, continuity_status="cross_source_overlap_unverified",
            updated_at=datetime.now(timezone.utc)))
        parsed_original = parse_reading(original_conflict)
        session.add(OrbitReading(
            watch_id="watch-1", reading_id="43", checked_at=parsed_original.checked_at,
            received_at=datetime.now(timezone.utc), raw_payload=original_conflict,
            status="VERIFIED_DELTA"))

    class ConflictedReplayClient(_HistoryClient):
        def latest(self, watch_id):
            return {"watch": {"id": watch_id, "selector": PAYOUT_SELECTOR},
                    "latest": later_overlap, "cursor": "44"}

    client = ConflictedReplayClient([mutated_conflict, later_overlap])
    result = collect_once(engine, client, {
        "id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running",
        "collectorStatus": "running",
    })
    assert result["status"] == "quarantined_identity_conflict"
    assert result["cursor_held"] == "42"
    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        health = session.get(SystemHealth, "orbit_collector")
        assert state.cursor == "42"
        assert health.state == "DEGRADED"
        assert health.details["conflicted_reading_id"] == "43"
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 0
        assert session.scalar(select(func.count()).select_from(OrbitReading).where(
            OrbitReading.reading_id == "44")) == 0
    engine.dispose()


def test_same_cursor_payload_mutation_is_quarantined_instead_of_reported_healthy():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    previous = [f"1.{i:02d}" for i in range(11, 31)]
    original = _reading(42, ["1.31", "1.32", *previous])
    original_parsed = parse_reading(original)
    original_dom = [{"item_index": item.item_index, "multiplier_raw": item.multiplier_raw,
                     "multiplier_normalized": str(item.multiplier.normalize()), "visible": True}
                    for item in original_parsed.items]
    _persist_reading(engine, "watch-1", original, original_parsed, original_dom,
                     previous_snapshot=previous, initial_alignment=True)
    changed = _reading(42, ["1.33", "1.32", *previous])

    class _SameCursorClient:
        def latest(self, watch_id):
            return {"watch": {"id": watch_id, "selector": PAYOUT_SELECTOR},
                    "latest": changed, "cursor": "42"}

    result = collect_once(engine, _SameCursorClient(),
                          {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running"})
    with Session(engine) as session:
        rows = session.scalars(select(OrbitReading).order_by(OrbitReading.id)).all()
        health = session.get(SystemHealth, "orbit_collector")
        assert result["status"] == "quarantined_identity_conflict"
        assert len(rows) == 2
        assert rows[-1].status == "quarantined_identity_conflict"
        assert health.state == "DEGRADED"
    engine.dispose()


def test_history_items_newer_than_latest_cursor_wait_for_next_poll():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    _historical_tail_fixture(engine)
    phase1_tail = [f"1.{position:02d}" for position in range(20, 0, -1)]
    first = _reading(1, ["1.80"] * 20)
    anchor = _reading(2, phase1_tail)
    _bootstrap_from_history(engine, _HistoryClient([first, anchor]), "watch-1", "2",
                            anchor, parse_reading(anchor), _dom(phase1_tail), page_size=10)
    after_latest = _reading(3, ["1.22", "1.21", *phase1_tail])
    raced_future = _reading(4, ["1.23", "1.22", "1.21", *phase1_tail])

    class RacingHistoryClient:
        def latest(self, _watch_id):
            return {"watch": {"id": "watch-1", "selector": PAYOUT_SELECTOR},
                    "latest": after_latest, "cursor": "3"}

        def history(self, _watch_id, after, _limit):
            items = [row for row in (after_latest, raced_future) if int(row["id"]) > int(after)]
            return {"watchId": "watch-1", "selector": PAYOUT_SELECTOR,
                    "readings": items, "nextCursor": "4", "hasMore": False}

    result = collect_once(engine, RacingHistoryClient(),
                          {"id": "watch-1", "selector": PAYOUT_SELECTOR, "status": "running"})
    with Session(engine) as session:
        state = session.get(OrbitWatchState, 1)
        assert result["status"] == "synced"
        assert state.cursor == "3"
        assert session.scalar(select(OrbitReading.id).where(OrbitReading.reading_id == "4")) is None
    engine.dispose()
