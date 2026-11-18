from datetime import datetime, timezone
from types import SimpleNamespace

from behaviour_detection.high_series import (DEFAULTS, new_state, prepare_strategy_eligibility,
    process_round, validate_config)
from behaviour_detection.api import create_app
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.live_strategy_processor import LiveStrategyProcessor
from sqlalchemy.orm import Session
from behaviour_detection.models import (BehaviourStrategySettlement, HighDominance2xBatch,
    HighDominanceSeriesState, HybridCohort, SchemaMigration)
from behaviour_detection.worker import (_expire_high_series_time_limits,
    quarantine_unverifiable_high_series)


def invoke_config_endpoint(engine, payload, *, origin=None):
    app = create_app(engine)
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/api/v1/behaviour/hybrid/high-series/config")
    request = SimpleNamespace(headers={"origin": origin} if origin else {},
                              client=SimpleNamespace(host="127.0.0.1"))
    with Session(engine, expire_on_commit=False) as session:
        try:
            return 200, route.endpoint(request, payload, session)
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                return exc.status_code, exc.detail
            raise


def step(state, position, dominance, *, previous=None, emergence=False, multiplier=None,
         signal_executed=None, settlement_result=None):
    # Dominance and a single round's classification are separate facts. The
    # default fixture follows dominance for older lifecycle cases; Part 2 tests
    # pass explicit multipliers when exercising result-based gating.
    if multiplier is None:
        multiplier = ("2.10" if dominance == "HIGH_DOMINANT" else
                      "1.50" if dominance == "LOW_DOMINANT" else "1.80")
    row = {"identity_key": f"id-{position}", "round_id": str(position),
        "position": position, "multiplier": multiplier,
        "outcome": "E" if multiplier == "2.00" else "H" if float(multiplier) > 2 else "L"}
    if signal_executed is None:
        signal_executed = bool(state and state.get("state") in {
            "PART1_COLLECTING", "PART2_ACTIVE", "HIGH_DOMINANCE_ONLY_ACTIVE"})
    settlement = ({"signal_id": f"signal-{position}", "signal_lock_status": "DURABLY_LOCKED",
        "settlement_status": "COMMITTED", "target_round_identity": row["identity_key"],
        "result": settlement_result or ("WIN" if float(multiplier) > 2 else "LOSS")}
        if signal_executed else None)
    return process_round(state, row=row,
        previous_state=previous or "BALANCED", current_state=dominance,
        emergence=emergence, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        signal_executed=signal_executed, signal_settlement=settlement)


def started():
    legacy_config = new_state()["config"]
    legacy_config["part1"]["net_win_limit_enabled"] = False
    legacy_config["part2"]["net_win_limit_enabled"] = False
    return step(new_state(legacy_config), 1, "HIGH_DOMINANT", emergence=True)


def test_selected_defaults_and_validation_constraints():
    config = new_state()["config"]
    assert config["strategy_mode"] == "PART1_AND_PART2"
    assert config["part1"] == {"enabled": True, "max_counted_rounds": 5,
        "net_win_limit_enabled": True, "net_win_target": 2}
    assert config["part2"]["max_counted_rounds_per_batch"] == 5
    assert config["part2"]["net_win_target"] == 3
    assert config["part2"]["pause_limit_verified_rounds"] == 10
    assert config["cooldown_verified_rounds"] == 10
    assert config["on_win_limit_reached"] == "WAIT_FOR_NEXT_FRESH_TRIGGER"
    assert config["shadow_only"] is True
    assert validate_config({"part1": {"net_win_target": 5}})["part1"]["net_win_target"] == 5
    for invalid in (
        {"part1": {"net_win_target": 0}},
        {"part1": {"max_counted_rounds": 2, "net_win_target": 3}},
        {"part2": {"max_counted_rounds_per_batch": 2, "net_win_target": 3}},
        {"strategy_mode": "INVALID"}, {"shadow_only": False},
    ):
        try:
            validate_config(invalid)
        except ValueError:
            continue
        raise AssertionError(f"invalid strategy config was accepted: {invalid}")


def test_part1_win_target_stops_series_before_five_and_never_enters_part2():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    state = step(state, 2, "LOW_DOMINANT", multiplier="2.50", settlement_result="WIN")
    state = step(state, 3, "LOW_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    state = step(state, 4, "HIGH_DOMINANT", multiplier="2.10", settlement_result="WIN")
    series = state["series"]
    assert state["state"] == "COOLDOWN"
    assert series["stop_reason"] == "PART1_WIN_TARGET_REACHED"
    assert series["part2"] is None
    assert series["part1"]["confirmed_win_count"] == 2
    assert series["part1"]["loss_count"] == 1
    assert series["part1"]["counted_rounds"] == 3
    assert series["part1"]["remaining_win_target"] == 0
    assert [event["type"] for event in state["events"]].count("PART1_WIN_TARGET_REACHED") == 1


def test_unlocked_observation_and_unverified_settlement_do_not_count():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    row = {"identity_key": "id-2", "round_id": "2", "position": 2,
           "multiplier": "2.50", "outcome": "H"}
    state = process_round(state, row=row, previous_state="HIGH_DOMINANT",
        current_state="HIGH_DOMINANT", emergence=False, signal_executed=True,
        signal_settlement={"signal_id": "unlocked", "result": "WIN",
                           "settlement_status": "PENDING"})
    assert state["series"]["part1"]["counted_rounds"] == 0
    assert state["series"]["part1"]["wins"] == 0


def test_duplicate_settlement_receipt_cannot_inflate_win_count_or_change_identity():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    settlement = {"signal_id": "one-signal", "signal_lock_status": "DURABLY_LOCKED",
        "settlement_status": "COMMITTED", "target_round_identity": "id-2",
        "result": "WIN", "net_profit": 125.5}
    state = process_round(state, row={"identity_key": "id-2", "round_id": "2",
        "position": 2, "multiplier": "2.50", "outcome": "H"},
        previous_state="HIGH_DOMINANT", current_state="HIGH_DOMINANT",
        emergence=False, signal_executed=True, signal_settlement=settlement)
    assert state["series"]["part1"]["confirmed_win_count"] == 1
    assert state["series"]["part1"]["net_profit"] == 125.5
    duplicate = process_round(state, row={"identity_key": "id-3", "round_id": "3",
        "position": 3, "multiplier": "2.50", "outcome": "H"},
        previous_state="HIGH_DOMINANT", current_state="HIGH_DOMINANT",
        emergence=False, signal_executed=True,
        signal_settlement={**settlement, "target_round_identity": "id-3"})
    assert duplicate["series"]["part1"]["confirmed_win_count"] == 1
    assert duplicate["state"] == "UNSAFE_STATE"
    assert duplicate["recovery_quarantine_reason"] == "SHADOW_SETTLEMENT_IDENTITY_CONFLICT"


def test_pending_shadow_settlement_blocks_next_eligibility_and_counts_when_reconciled():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    row = {"identity_key": "id-2", "round_id": "2", "position": 2,
           "multiplier": "2.50", "outcome": "H"}
    state = process_round(state, row=row, previous_state="HIGH_DOMINANT",
        current_state="HIGH_DOMINANT", emergence=False, settlement_pending=True,
        pending_signal={"signal_id": "pending-2"})
    assert state["state"] == "WAITING_FOR_SHADOW_SETTLEMENT"
    deferred = process_round(state, row={"identity_key": "id-3", "round_id": "3",
        "position": 3, "multiplier": "1.50", "outcome": "L"},
        previous_state="HIGH_DOMINANT", current_state="LOW_DOMINANT", emergence=False)
    assert deferred["state"] == "WAITING_FOR_SHADOW_SETTLEMENT"
    assert len(deferred["deferred_verified_rounds"]) == 1
    state["state"] = state.pop("settlement_resume_state")
    state["last_processed_identity"] = state["settlement_wait"]["previous_identity"]
    state["last_processed_position"] = state["settlement_wait"]["previous_position"]
    settled = process_round(state, row=row, previous_state="HIGH_DOMINANT",
        current_state="HIGH_DOMINANT", emergence=False, signal_executed=True,
        signal_settlement={"signal_id": "pending-2", "signal_lock_status": "DURABLY_LOCKED",
            "settlement_status": "COMMITTED", "target_round_identity": "id-2", "result": "WIN"})
    assert settled["series"]["part1"]["wins"] == 1
    assert settled["settlement_receipts"]["pending-2"]["target_round_identity"] == "id-2"


def test_shadow_settlement_endpoint_reconciles_durable_lock_once(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'shadow-settlement-api.sqlite3'}")
    migrate(engine)
    machine = started()
    series = machine["series"]
    stream_id = "settlement-api-stream"
    signal_id = "settlement-api-signal"
    lock = {"signal_id": signal_id, "session_id": "shadow-session-api",
        "series_id": series["series_id"], "part": "PART1", "batch_id": "PART1-001",
        "configuration_version": series["configuration_version"],
        "target_round_id": f"shadow-index:{stream_id}:2", "target_stream_id": stream_id,
        "target_round_index": 2, "lock_status": "DURABLY_LOCKED"}
    machine.setdefault("locked_signals", {})[signal_id] = lock
    machine = process_round(machine, row={"identity_key": "winner-predict:r-api-2",
        "round_id": "r-api-2", "position": 2, "multiplier": "2.50", "outcome": "H"},
        previous_state="HIGH_DOMINANT", current_state="HIGH_DOMINANT", emergence=False,
        settlement_pending=True, pending_signal=lock)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        state_row.payload = machine

    app = create_app(engine)
    app.state.live_strategy_processor = LiveStrategyProcessor(engine)
    route = next(route for route in app.routes
        if getattr(route, "path", None) == "/api/v1/behaviour/internal/shadow-settlement")
    request = SimpleNamespace(app=app, client=SimpleNamespace(host="127.0.0.1"), headers={})
    payload = {**lock, "signal_id": signal_id, "session_id": lock["session_id"],
        "series_id": series["series_id"], "part": "PART1", "batch_id": "PART1-001",
        "configuration_version": series["configuration_version"],
        "signal_target_round_id": lock["target_round_id"],
        "signal_lock_status": "DURABLY_LOCKED",
        "settlement_status": "COMMITTED", "target_round_id": "r-api-2",
        "target_round_identity": "winner-predict:r-api-2", "target_stream_id": stream_id,
        "target_round_index": 2, "actual_multiplier": 2.5, "result": "WIN",
        "net_profit": 100, "evidence": {"event_type": "round.persisted",
            "durability": "COMMITTED", "continuity_status": "VERIFIED",
            "target_round_id": "r-api-2", "target_stream_id": stream_id,
            "target_round_index": 2}}
    with Session(engine, expire_on_commit=False) as session:
        first = route.endpoint(request, payload, session)
    assert first["status"] == "RECORDED"
    with Session(engine) as session:
        saved = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
        receipt = session.get(BehaviourStrategySettlement, signal_id)
    assert receipt is not None
    assert saved["series"]["part1"]["confirmed_win_count"] == 1
    assert saved["series"]["part1"]["net_profit"] == 100
    with Session(engine, expire_on_commit=False) as session:
        duplicate = route.endpoint(request, payload, session)
    assert duplicate["status"] == "ALREADY_RECORDED"
    with Session(engine) as session:
        saved = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload
    assert saved["series"]["part1"]["confirmed_win_count"] == 1
    engine.dispose()


def test_part1_r5_without_target_uses_r5_dominance_to_enter_part2():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    for position in range(2, 6):
        state = step(state, position, "HIGH_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    state = step(state, 6, "HIGH_DOMINANT", multiplier="2.10", settlement_result="WIN")
    assert state["series"]["part1"]["counted_rounds"] == 5
    assert state["series"]["part1"]["confirmed_win_count"] == 1
    assert state["state"] == "PART2_ACTIVE"
    assert state["series"]["part2"]["current_batch"]["counted_rounds"] == 0


def test_part1_r5_non_high_terminates_when_win_target_not_reached():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    for position in range(2, 6):
        state = step(state, position, "HIGH_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    state = step(state, 6, "BALANCED", multiplier="2.10", settlement_result="WIN")
    assert state["series"]["part1"]["counted_rounds"] == 5
    assert state["state"] == "COOLDOWN"
    assert state["series"]["stop_reason"] == "STOPPED_PART1_NO_HIGH"


def test_part2_win_target_stops_before_batch_end():
    state = step(new_state(), 1, "HIGH_DOMINANT", emergence=True)
    for position in range(2, 6):
        state = step(state, position, "HIGH_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    state = step(state, 6, "HIGH_DOMINANT", multiplier="2.10", settlement_result="WIN")
    assert state["state"] == "PART2_ACTIVE"
    for position in range(7, 10):
        state = step(state, position, "HIGH_DOMINANT", multiplier="2.50", settlement_result="WIN")
    batch = state["series"]["part2"]["current_batch"]
    assert state["state"] == "COOLDOWN"
    assert state["series"]["stop_reason"] == "PART2_WIN_TARGET_REACHED"
    assert batch["confirmed_win_count"] == 3
    assert batch["counted_rounds"] == 3
    assert batch["remaining_win_target"] == 0


def test_part1_only_and_high_only_modes():
    part1_config = new_state()["config"]
    part1_config["strategy_mode"] = "PART1_ONLY"
    part1_config["part1"]["net_win_limit_enabled"] = False
    state = step(new_state(part1_config), 1, "HIGH_DOMINANT", emergence=True)
    for position in range(2, 7):
        state = step(state, position, "HIGH_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    assert state["state"] == "COOLDOWN"
    assert state["series"]["stop_reason"] == "STOPPED_PART1_COMPLETED"
    assert state["series"]["part2"] is None

    high_config = new_state()["config"]
    high_config["strategy_mode"] = "HIGH_DOMINANCE_ONLY"
    high = step(new_state(high_config), 1, "HIGH_DOMINANT", emergence=True)
    assert high["state"] == "HIGH_DOMINANCE_ONLY_ACTIVE"
    assert prepare_strategy_eligibility(high,
        {"identity_key": "id-1", "round_id": "1", "position": 1})["eligibility"] == "ELIGIBLE"
    high = step(high, 2, "LOW_DOMINANT", multiplier="1.50", settlement_result="LOSS")
    assert high["state"] == "COOLDOWN"
    assert high["series"]["stop_reason"] == "STOPPED_HIGH_DOMINANCE_EXIT"


def test_configuration_edits_are_pending_until_the_next_series():
    state = started()
    active_version = state["series"]["configuration_version"]
    pending = new_state()["config"]
    pending["configuration_version"] = active_version + 1
    pending["part1"]["net_win_target"] = 1
    state["pending_config"] = pending
    state["pending_configuration_version"] = pending["configuration_version"]
    for position in range(2, 7):
        state = step(state, position, "LOW_DOMINANT")
    for position in range(7, 17):
        state = step(state, position, "LOW_DOMINANT")
    assert state["config"]["configuration_version"] == pending["configuration_version"]
    assert state["pending_config"] is None
    state = step(state, 17, "HIGH_DOMINANT", emergence=True)
    assert state["series"]["configuration_version"] == pending["configuration_version"]
    assert state["series"]["configuration_snapshot"]["part1"]["net_win_target"] == 1


def test_part1_at_three_of_five_does_not_evaluate_continuation():
    state = started()
    for pos in range(2, 5):
        state = step(state, pos, "LOW_DOMINANT")
    assert state["state"] == "PART1_COLLECTING"
    assert len(state["series"]["part1"]["rounds"]) == 3
    assert state["series"]["part1"].get("state_after_r5") is None


def test_part1_exposes_one_idempotent_future_only_strategy_eligibility():
    state = started()
    row = {"identity_key": "trigger-id", "round_id": "trigger-round", "position": 1}
    first = prepare_strategy_eligibility(state, row,
        decision_timestamp="2026-01-01T00:00:01+00:00")
    second = prepare_strategy_eligibility(state, row,
        decision_timestamp="2026-01-01T00:00:02+00:00")
    assert first == second
    assert first["part"] == "PART1"
    assert first["direction"] == "HIGH"
    assert first["source_round_id"] == "trigger-round"
    assert first["target_sequence_boundary"]["cohort_position"] == 2
    assert first["eligibility_id"].startswith("behaviour-eligibility-")
    assert first["eligibility"] == "ELIGIBLE"
    assert first["current_dominance_state"] == "HIGH_DOMINANT"
    assert state["series"].get("eligibility_events") is None


def test_unresolved_rolling_horizon_gap_blocks_shadow_intent():
    state = started()
    state["data_health"] = "DATA_STALE"
    snapshot = prepare_strategy_eligibility(state,
        {"identity_key": "gap-round", "round_id": "gap-round", "position": 2})
    assert snapshot["eligibility"] == "INELIGIBLE"
    assert snapshot["eligibility_reason"] == "BEHAVIOUR_DATA_STALE"
    assert state["series"].get("eligibility_events") is None


def test_idle_strategy_exposes_noneligible_state_without_signal_intent():
    state = new_state()
    snapshot = prepare_strategy_eligibility(state,
        {"identity_key": "idle-round", "round_id": "idle-round", "position": 2})
    assert snapshot["eligibility"] == "INELIGIBLE"
    assert snapshot["eligibility_reason"] == "NO_ACTIVE_SERIES"
    assert state["latest_strategy_eligibility"] == snapshot
    assert state.get("series") is None


def test_part1_five_high_opens_part2_after_r5_and_does_not_count_r5_twice():
    state = started()
    for pos in range(2, 7):
        state = step(state, pos, "HIGH_DOMINANT")
    assert state["state"] == "PART2_ACTIVE"
    assert len(state["series"]["part1"]["rounds"]) == 5
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 0
    state = step(state, 7, "HIGH_DOMINANT")
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 1


def test_part1_r5_high_dominance_enters_part2_paused_when_r5_result_is_low():
    state = started()
    for pos in range(2, 6):
        state = step(state, pos, "HIGH_DOMINANT")
    state = step(state, 6, "HIGH_DOMINANT", multiplier="1.50")
    assert len(state["series"]["part1"]["rounds"]) == 5
    assert state["series"]["part1"]["state_after_r5"] == "HIGH_DOMINANT"
    assert state["state"] == "PART2_PAUSED_NON_HIGH"
    assert state["series"]["part2"]["current_batch"]["rounds"] == []
    assert state["series"]["part2"]["pause_started_round_identity"] == "id-6"
    decision = prepare_strategy_eligibility(state,
        {"identity_key": "id-6", "round_id": "6", "position": 6})
    assert decision["eligibility"] == "INELIGIBLE"
    assert decision["eligibility_reason"] == "PART2_PAUSED_NON_HIGH"

    state = step(state, 7, "LOW_DOMINANT", multiplier="2.10")
    assert state["state"] == "PART2_ACTIVE"
    assert state["series"]["part2"]["current_batch"]["rounds"] == []
    resumed = prepare_strategy_eligibility(state,
        {"identity_key": "id-7", "round_id": "7", "position": 7})
    assert resumed["eligibility"] == "ELIGIBLE"


def test_part1_finishing_non_high_stops_and_starts_empty_cooldown():
    state = started()
    for pos in range(2, 7):
        state = step(state, pos, "LOW_DOMINANT")
    assert state["state"] == "COOLDOWN"
    assert state["series"]["stop_reason"] == "STOPPED_PART1_NO_HIGH"
    assert state["cooldown_progress"] == 0


def test_recovery_counts_only_the_one_settled_locked_signal():
    state = started()
    for pos in range(2, 7):
        state = process_round(state,
            row={"identity_key": f"replay-{pos}", "round_id": str(pos),
                 "position": pos, "multiplier": "1.50", "outcome": "L"},
            previous_state="HIGH_DOMINANT", current_state="LOW_DOMINANT",
            emergence=False, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            signal_executed=(pos == 2),
            signal_settlement=({"signal_id": "signal-recovery-2",
                "signal_lock_status": "DURABLY_LOCKED", "settlement_status": "COMMITTED",
                "target_round_identity": "replay-2", "result": "LOSS"} if pos == 2 else None),
            allow_trigger=False)
    part1 = state["series"]["part1"]
    assert state["state"] == "PART1_COLLECTING"
    assert len(part1["rounds"]) == 1
    assert part1["rounds"][0]["execution_status"] == "LOCKED_SIGNAL_SETTLED"
    assert part1["rounds"][0]["result"] == "LOSS"
    assert part1["wins"] == 0
    assert part1["losses"] == 1


def test_recovery_does_not_open_a_new_series_from_high_context():
    state = process_round(new_state(), row={"identity_key": "replay-high",
        "round_id": "replay-high", "position": 5, "multiplier": "2.50", "outcome": "H"},
        previous_state="BALANCED", current_state="HIGH_DOMINANT", emergence=True,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc), allow_trigger=False)
    assert state["state"] == "IDLE"
    assert state["series"] is None


def active_part2():
    state = started()
    for pos in range(2, 7):
        state = step(state, pos, "HIGH_DOMINANT")
    for pos in (7, 8):
        state = step(state, pos, "HIGH_DOMINANT")
    return state


def test_part2_settles_transition_round_then_pauses_same_batch():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT", multiplier="1.50")
    assert state["state"] == "PART2_PAUSED_NON_HIGH"
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 3
    assert state["series"]["part2"]["current_batch"]["rounds"][-1]["result"] == "LOSS"
    assert state["series"]["part2"]["pause_count"] == 0


def test_part2_resume_same_batch_and_resume_round_is_not_counted():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    state = step(state, 10, "LOW_DOMINANT")
    state = step(state, 11, "BALANCED", multiplier="2.10")
    assert state["state"] == "PART2_ACTIVE"
    assert state["series"]["part2"]["resume_count"] == 1
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 3
    state = step(state, 12, "LOW_DOMINANT", multiplier="2.10")
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 4


def test_pause_limit_expires_on_tenth_observation_and_starts_cooldown():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    for pos in range(10, 20):
        state = step(state, pos, "BALANCED", multiplier="1.80")
    assert state["state"] == "COOLDOWN"
    assert state["series"]["stop_reason"] == "STOPPED_PAUSE_LIMIT"
    assert state["cooldown_progress"] == 0
    assert state["series"]["part2"]["pause_count"] == 10


def test_high_return_on_exact_pause_limit_precedes_expiration():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    for pos in range(10, 19):
        state = step(state, pos, "BALANCED", multiplier="1.80")
    state = step(state, 19, "LOW_DOMINANT", multiplier="2.10")
    assert state["state"] == "PART2_ACTIVE"
    assert state["series"]["part2"]["resume_count"] == 1
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 3


def test_persistent_high_does_not_retrigger_after_cooldown_without_exit_and_reentry():
    state = started()
    for pos in range(2, 7):
        state = step(state, pos, "LOW_DOMINANT")
    for pos in range(7, 16):
        state = step(state, pos, "HIGH_DOMINANT", emergence=True)
    assert state["state"] == "COOLDOWN"
    state = step(state, 16, "HIGH_DOMINANT", emergence=True)
    assert state["state"] == "WAITING_FOR_FRESH_TRIGGER"
    assert state["series"] is None
    assert state["completed_series"][-1]["stop_reason"] == "STOPPED_PART1_NO_HIGH"
    assert len(state["completed_series"][-1]["part1"]["rounds"]) == 5
    state = step(state, 17, "HIGH_DOMINANT", emergence=False)
    assert state["state"] == "WAITING_FOR_FRESH_TRIGGER"
    state = step(state, 18, "LOW_DOMINANT", emergence=False)
    assert state["state"] == "WAITING_FOR_FRESH_TRIGGER"
    state = step(state, 19, "HIGH_DOMINANT", emergence=True)
    assert state["state"] == "PART1_COLLECTING"
    assert state["series"]["trigger_position"] == 19
    assert state["series"]["part1"]["rounds"] == []
    state = step(state, 20, "HIGH_DOMINANT", emergence=False)
    assert len(state["series"]["part1"]["rounds"]) == 1


def test_completed_cooldown_does_not_rearm_if_current_horizon_is_not_high():
    state = started()
    for pos in range(2, 7):
        state = step(state, pos, "LOW_DOMINANT")
    for pos in range(7, 17):
        state = step(state, pos, "LOW_DOMINANT")
    assert state["state"] == "WAITING_FOR_FRESH_TRIGGER"
    state = step(state, 17, "LOW_DOMINANT", emergence=False)
    assert state["state"] == "WAITING_FOR_FRESH_TRIGGER"
    assert state["series"] is None


def test_restart_snapshot_and_duplicate_replay_are_idempotent():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    resumed = process_round(state, row={"identity_key": "id-9", "round_id": "9",
        "position": 9, "multiplier": "2.10", "outcome": "H"},
        previous_state="LOW_DOMINANT", current_state="LOW_DOMINANT", emergence=False)
    assert resumed == state
    assert len(resumed["series"]["part2"]["current_batch"]["rounds"]) == 3


def test_no_lookahead_and_batch_rollover_are_exactly_once():
    state = active_part2()
    for pos in (9, 10):
        state = step(state, pos, "HIGH_DOMINANT")
    # Third outcome turns the horizon LOW; it is settled from the already locked active eligibility.
    state = step(state, 11, "LOW_DOMINANT", multiplier="1.50")
    assert len(state["series"]["part2"]["batches"]) == 1
    assert len(state["series"]["part2"]["batches"][0]["rounds"]) == 5
    assert state["series"]["part2"]["batches"][0]["rounds"][-1]["result"] == "LOSS"
    assert state["state"] == "PART2_PAUSED_NON_HIGH"
    assert state["series"]["part2"]["current_batch"] is None
    assert state["series"]["part2"]["batch_number"] == 2


def test_completed_batch_that_turns_low_is_closed_and_next_opens_on_resume():
    state = active_part2()
    for pos in (9, 10):
        state = step(state, pos, "HIGH_DOMINANT")
    state = step(state, 11, "LOW_DOMINANT", multiplier="1.50")
    part2 = state["series"]["part2"]
    assert state["state"] == "PART2_PAUSED_NON_HIGH"
    assert len(part2["batches"]) == 1
    assert len(part2["batches"][0]["rounds"]) == 5
    assert part2["current_batch"] is None
    assert part2["batch_number"] == 2
    state = step(state, 12, "HIGH_DOMINANT")
    assert state["state"] == "PART2_ACTIVE"
    assert state["series"]["part2"]["batch_number"] == 2
    assert state["series"]["part2"]["current_batch"]["rounds"] == []
    state = step(state, 13, "HIGH_DOMINANT")
    assert len(state["series"]["part2"]["current_batch"]["rounds"]) == 1


def test_replay_repairs_completed_batch_duplicated_as_current():
    state = active_part2()
    for pos in (9, 10):
        state = step(state, pos, "HIGH_DOMINANT")
    state = step(state, 11, "LOW_DOMINANT", multiplier="1.50")
    part2 = state["series"]["part2"]
    part2["current_batch"] = {**part2["batches"][-1], "rounds": list(part2["batches"][-1]["rounds"])}
    replay = step(state, 11, "LOW_DOMINANT")
    assert replay["series"]["part2"]["current_batch"] is None
    assert len(replay["series"]["part2"]["batches"]) == 1


def test_data_gap_does_not_create_pause_observations():
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    count = state["series"]["part2"]["pause_count"]
    state = process_round(state, row={"identity_key": "id-10", "position": 10,
        "multiplier": "1.10", "outcome": "L"}, previous_state="LOW_DOMINANT",
        current_state="LOW_DOMINANT", emergence=False, data_fresh=False)
    assert state["state"] == "PART2_PAUSED_NON_HIGH"
    assert state["series"]["part2"]["pause_count"] == count
    assert state["data_health"] == "DATA_STALE"
    assert state["last_processed_position"] == 9


def test_high_active_batch_completes_once_and_opens_next_batch():
    state = active_part2()
    for pos in range(9, 13):
        state = step(state, pos, "HIGH_DOMINANT")
    state = step(state, 13, "HIGH_DOMINANT")
    part2 = state["series"]["part2"]
    assert len(part2["batches"]) == 1
    assert len(part2["batches"][0]["rounds"]) == 5
    assert part2["batch_number"] == 2
    assert len(part2["current_batch"]["rounds"]) == 2


def test_pause_limit_configuration_is_validated_and_persisted(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-config.sqlite3'}")
    migrate(engine)
    config = new_state()["config"]
    assert config["strategy_mode"] == "PART1_AND_PART2"
    assert config["part1"]["net_win_target"] == 2
    assert config["part2"]["net_win_target"] == 3
    status, saved = invoke_config_endpoint(engine, {
        "strategy_mode": "PART1_AND_PART2", "part1": {"net_win_target": 1},
        "part2": {"net_win_target": 4, "pause_limit_verified_rounds": 20},
        "cooldown_verified_rounds": 12})
    assert status == 200
    assert saved["config"]["part1"]["net_win_target"] == 1
    assert saved["config"]["part2"]["net_win_target"] == 4
    assert saved["config"]["part2"]["pause_limit_verified_rounds"] == 20
    assert saved["config"]["cooldown_verified_rounds"] == 12
    invalid_status, _ = invoke_config_endpoint(engine, {"part2": {"net_win_target": 6}})
    assert invalid_status == 422
    forbidden_status, _ = invoke_config_endpoint(engine, {"part1": {"net_win_target": 2}},
        origin="https://attacker.example")
    assert forbidden_status == 403
    with Session(engine) as session:
        saved_config = session.get(HighDominanceSeriesState,
            session.get(HybridCohort, 1).cohort_id).payload["config"]
    assert saved_config["part1"]["net_win_target"] == 1
    assert saved_config["part2"]["net_win_target"] == 4
    assert saved_config["cooldown_verified_rounds"] == 12
    engine.dispose()


def test_active_series_keeps_its_snapshot_and_stores_user_edit_as_pending(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-pending-config.sqlite3'}")
    migrate(engine)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        machine = started()
        row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        row.payload = machine
        row.updated_at = datetime.now(timezone.utc)
    status, result = invoke_config_endpoint(engine,
        {"part1": {"net_win_target": 1}, "cooldown_verified_rounds": 12})
    assert status == 200
    assert result["configuration_status"] == "PENDING_NEXT_SERIES"
    assert result["active_config"]["part1"]["net_win_limit_enabled"] is False
    assert result["pending_config"]["part1"]["net_win_target"] == 1
    with Session(engine) as session:
        cohort = session.get(HybridCohort, 1)
        machine = session.get(HighDominanceSeriesState, cohort.cohort_id).payload
    assert machine["series"]["configuration_version"] == 1
    assert machine["pending_config"]["configuration_version"] == 2
    engine.dispose()


def test_persisted_restart_recovers_part1_pause_and_cooldown_progress(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-restart.sqlite3'}")
    migrate(engine)
    part1 = started()
    for pos in range(2, 5):
        part1 = step(part1, pos, "LOW_DOMINANT")
    paused = active_part2()
    paused = step(paused, 9, "LOW_DOMINANT")
    stopped = started()
    for pos in range(2, 7):
        stopped = step(stopped, pos, "LOW_DOMINANT")
    for pos in range(7, 13):
        stopped = step(stopped, pos, "BALANCED")
    assert stopped["cooldown_progress"] == 6
    with Session(engine) as session, session.begin():
        for key, payload in (("part1", part1), ("paused", paused), ("cooldown", stopped)):
            session.add(HighDominanceSeriesState(cohort_id=key, payload=payload,
                updated_at=datetime.now(timezone.utc)))
    with Session(engine) as session:
        recovered_part1 = session.get(HighDominanceSeriesState, "part1").payload
        recovered_paused = session.get(HighDominanceSeriesState, "paused").payload
        recovered_cooldown = session.get(HighDominanceSeriesState, "cooldown").payload
    assert recovered_part1["state"] == "PART1_COLLECTING"
    assert len(recovered_part1["series"]["part1"]["rounds"]) == 3
    assert recovered_paused["state"] == "PART2_PAUSED_NON_HIGH"
    assert len(recovered_paused["series"]["part2"]["current_batch"]["rounds"]) == 3
    assert recovered_cooldown["cooldown_progress"] == 6
    assert recovered_cooldown["state"] == "COOLDOWN"
    engine.dispose()


def test_migration_recovers_an_existing_live_part1_without_recounting(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-legacy.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort_id = cohort.cohort_id
        cohort.last_cohort_position = 8
        cohort.last_identity_key = "legacy-r8"
        session.add(HighDominance2xBatch(
            batch_id="legacy-live-part1", cohort_id=cohort_id,
            series_anchor_position=5, batch_number=1, decision_position=5,
            decision_identity_key="legacy-r5", decision_round_id="5",
            trigger_horizon="5", phase="INITIAL", start_position=6,
            state_at_decision="HIGH_DOMINANT", state_at_end="LOW_DOMINANT",
            lifecycle_status="COLLECTING", outcomes=[
                {"cohort_position": pos, "identity_key": f"legacy-r{pos}",
                 "round_id": str(pos), "multiplier": "2.10", "classification": "H",
                 "result_at_2x": "WIN"} for pos in (6, 7, 8)],
            wins=3, losses=0, opened_at=now, updated_at=now,
        ))
        session.execute(SchemaMigration.__table__.delete().where(SchemaMigration.version == 14))
        session.connection().exec_driver_sql("DROP TABLE high_dominance_series_state")
    migrate(engine)
    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState, cohort_id).payload
    assert machine["state"] == "PART1_COLLECTING"
    assert machine["last_processed_position"] == 8
    assert [item["identity_key"] for item in machine["series"]["part1"]["rounds"]] == [
        "legacy-r6", "legacy-r7", "legacy-r8"]
    assert machine["series"]["part1"]["wins"] == 3
    engine.dispose()


def test_optional_wall_clock_timeout_stops_without_synthesizing_a_round(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-timeout.sqlite3'}")
    migrate(engine)
    state = active_part2()
    state = step(state, 9, "LOW_DOMINANT")
    state["config"]["pause_timeout_seconds"] = 1
    state["series"]["part2"]["pause_started_at"] = "2020-01-01T00:00:00+00:00"
    counted_before = len(state["series"]["part2"]["current_batch"]["rounds"])
    with Session(engine) as session, session.begin():
        session.add(HighDominanceSeriesState(cohort_id="timeout", payload=state,
            updated_at=datetime.now(timezone.utc)))
    _expire_high_series_time_limits(engine)
    with Session(engine) as session:
        expired = session.get(HighDominanceSeriesState, "timeout").payload
    assert expired["state"] == "COOLDOWN"
    assert expired["series"]["stop_reason"] == "STOPPED_TIME_LIMIT"
    assert len(expired["series"]["part2"]["current_batch"]["rounds"]) == counted_before
    assert expired["series"]["part2"]["pause_intervals"][-1]["terminal_reason"] == "STOPPED_TIME_LIMIT"
    engine.dispose()


def test_unverifiable_restored_active_series_is_quarantined_and_preserved(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-quarantine.sqlite3'}")
    migrate(engine)
    state = active_part2()
    original = state["series"].copy()
    with Session(engine) as session, session.begin():
        session.add(HighDominanceSeriesState(cohort_id="legacy", payload=state,
            updated_at=datetime.now(timezone.utc)))
    assert quarantine_unverifiable_high_series(engine) == 1
    with Session(engine) as session:
        quarantined = session.get(HighDominanceSeriesState, "legacy").payload
    assert quarantined["state"] == "UNSAFE_STATE"
    assert quarantined["provenance"] == "UNVERIFIABLE"
    assert quarantined["quarantined_series"] == original
    assert quarantined["quarantine_reason"] == "ACTIVE_SERIES_LACKS_VERIFIABLE_LIVE_PROVENANCE"
    engine.dispose()
