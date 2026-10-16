import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.betting.platform_state import (
    classify_bet_panel_observation,
    classify_betting_window,
    panel_observation_transition,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "aviator_panel_state_screenshots.json"


def _fixtures():
    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    observed_at = datetime.now(timezone.utc).isoformat()
    return [{**row, "source": "AVIATOR_BETTING_PANEL_READ_ONLY",
             "frame_identity": "https://aviator-next.spribegaming.com/",
             "observed_at": observed_at} for row in rows]


def test_screenshot_state_fixtures_report_text_amount_currency_and_confidence():
    available, queued, active = [classify_bet_panel_observation(row) for row in _fixtures()]
    assert (available["state"], available["displayed_stake"], available["currency"]) == (
        "BET_AVAILABLE", 200.0, "BIF")
    assert (queued["state"], queued["pending_bet_state"]) == ("BET_QUEUED", "PENDING")
    assert (active["state"], active["cashout_amount"], active["currency"]) == (
        "BET_ACTIVE", 121.0, "BIF")
    assert active["evidence"]["recognized_color"] == "AMBER"
    assert all(0.8 <= row["confidence"] <= 1 for row in (available, queued, active))


def test_panels_are_classified_independently_and_missing_controls_stay_unknown():
    rows = _fixtures()
    second_panel = {**rows[0], "panel_id": "PANEL_2", "button_text": "Bet 50 BIF",
                    "displayed_stake_text": "Bet 50 BIF"}
    assert classify_bet_panel_observation(rows[2])["state"] == "BET_ACTIVE"
    assert classify_bet_panel_observation(second_panel)["state"] == "BET_AVAILABLE"
    missing = classify_bet_panel_observation({
        "panel_id": "PANEL_2", "panel_visible": True, "button_visible": False,
        "button_enabled": False, "source": "AVIATOR_BETTING_PANEL_READ_ONLY",
        "frame_identity": "https://aviator-next.spribegaming.com/",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    assert missing["state"] == "UNKNOWN"


def test_queued_panel_preserves_readable_stake_and_account_currency():
    queued = {**_fixtures()[1], "displayed_stake_text": "200", "currency_hint": "BIF"}
    result = classify_bet_panel_observation(queued)
    assert result["state"] == "BET_QUEUED"
    assert (result["displayed_stake"], result["currency"]) == (200.0, "BIF")


def test_text_is_primary_but_conflicting_recognized_color_fails_closed():
    rows = _fixtures()
    neutral = {**rows[0], "button_color": "rgb(90, 90, 90)"}
    assert classify_bet_panel_observation(neutral)["state"] == "BET_AVAILABLE"
    red_text_with_green_color = {**rows[1], "button_color": "rgb(40, 167, 69)"}
    assert classify_bet_panel_observation(red_text_with_green_color)["state"] == "UNKNOWN"
    green_text_with_amber_color = {**rows[0], "button_color": "rgb(255, 193, 7)"}
    assert classify_bet_panel_observation(green_text_with_amber_color)["state"] == "UNKNOWN"
    standard_orange_cashout = {**rows[2], "button_color": "rgb(255, 165, 0)"}
    assert classify_bet_panel_observation(standard_orange_cashout)["state"] == "UNKNOWN"
    transparent_color = {**rows[0], "button_color": "rgba(0, 0, 0, 0)"}
    assert classify_bet_panel_observation(transparent_color)["state"] == "BET_AVAILABLE"
    assert classify_bet_panel_observation(transparent_color)["evidence"]["recognized_color"] is None
    uppercase = {**rows[0], "main_label": "BET", "button_text": "BET 200 BIF",
                 "displayed_stake_text": "BET 200 BIF"}
    assert classify_bet_panel_observation(uppercase)["currency"] == "BIF"


def test_disabled_bet_is_unknown_unless_the_panel_explicitly_reports_reset():
    row = {**_fixtures()[0], "button_enabled": False}
    assert classify_bet_panel_observation(row)["state"] == "UNKNOWN"
    explicit_reset = {**row, "button_visible": False, "data_state": "reset"}
    assert classify_bet_panel_observation(explicit_reset)["state"] == "BET_RESET"


def test_stale_observations_and_unknown_panel_identity_are_not_reused():
    rows = _fixtures()
    stale = {**rows[0], "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()}
    assert classify_bet_panel_observation(stale)["state"] == "UNKNOWN"
    unknown_identity = {**rows[0], "frame_identity": None}
    assert classify_bet_panel_observation(unknown_identity)["state"] == "UNKNOWN"


def test_transitions_deduplicate_events_reject_stale_rows_and_handle_frame_reload():
    available, queued, active = [classify_bet_panel_observation(row) for row in _fixtures()]
    repeated = {**available, "observed_at": (datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat()}
    later_previous = {**queued, "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()}
    earlier_current = {**available, "observed_at": datetime.now(timezone.utc).isoformat()}
    assert panel_observation_transition(available, repeated)["duplicate"] is True
    assert panel_observation_transition(later_previous, earlier_current)["stale"] is True
    queued_transition = panel_observation_transition(available, queued)
    active_transition = panel_observation_transition(queued, active)
    returned_to_bet = panel_observation_transition(active, available)
    assert (queued_transition["from_state"], queued_transition["to_state"]) == ("BET_AVAILABLE", "BET_QUEUED")
    assert (active_transition["from_state"], active_transition["to_state"]) == ("BET_QUEUED", "BET_ACTIVE")
    assert (returned_to_bet["from_state"], returned_to_bet["to_state"], returned_to_bet["current_state"]) == (
        "BET_ACTIVE", "BET_RESET", "BET_AVAILABLE")
    reloaded = {**available, "frame_identity": "https://aviator-next.spribegaming.com/reloaded"}
    reload_transition = panel_observation_transition(available, reloaded)
    assert reload_transition["frame_reloaded"] is True
    assert reload_transition["from_state"] == "UNKNOWN"


def test_green_bet_action_does_not_establish_a_betting_window():
    button_only = {
        "source": "AVIATOR_BETTING_PANEL_READ_ONLY", "panel_index": 0,
        "button_visible": True, "button_enabled": True,
        "stake_input_visible": True, "bet_tab_active": True,
    }
    explicit = {
        "source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM", "panel_index": 0,
        "phase_indicator_visible": True, "explicit_window_state": "BETTING_OPEN",
    }
    assert classify_betting_window(button_only, panel=0) == "UNKNOWN"
    assert classify_betting_window(explicit, panel=0) == "BETTING_OPEN"
