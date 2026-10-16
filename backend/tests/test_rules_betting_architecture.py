import asyncio
from types import SimpleNamespace

import pytest

from app.api.rules_engine import dashboard
from app.betting.cashout import MockCashoutReadAdapter, inspect_cashout_configuration
from app.betting.browser_adapters import RealBrowserBackend
from app.betting.platform_state import (
    PlatformBetState,
    classify_betting_window,
    classify_round_phase,
    classify_platform_bet_result,
    classify_platform_bet_state,
    detect_insufficient_balance,
    observe_betting_deadline,
)
from app.betting.selectors import GameSnapshot, parse_balance_display
from app.database.repository import Repository
from app.rules_engine import RulesEngine


def test_fixed_cashout_requires_enabled_setting_and_platform_acknowledgement():
    displayed_but_unacknowledged = inspect_cashout_configuration({
        "displayedValue": "2.00", "autoCashoutEnabled": True,
        "autoTabSelected": True,
        "platformAcknowledged": None,
    })
    accepted = inspect_cashout_configuration({
        "displayedValue": "2.00", "autoCashoutEnabled": True,
        "autoTabSelected": True,
        "platformAcknowledged": True,
    })
    rejected = inspect_cashout_configuration({
        "displayedValue": "2.01", "autoCashoutEnabled": False,
        "autoTabSelected": False,
        "platformAcknowledged": False,
    })

    assert displayed_but_unacknowledged["configuration_valid"] is True
    assert displayed_but_unacknowledged["verified"] is False
    assert "PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED" in displayed_but_unacknowledged["failures"]
    assert accepted["target"] == 2.0 and accepted["verified"] is True
    assert rejected["verified"] is False
    assert "CASHOUT_VALUE_MUST_EQUAL_2_00" in rejected["failures"]
    assert "PLATFORM_REJECTED_CASHOUT_SETTING" in rejected["failures"]


def test_mock_cashout_adapter_is_read_only_and_reports_missing_panel():
    adapter = MockCashoutReadAdapter({
        "displayedValue": "2.00", "autoCashoutEnabled": True,
        "autoTabSelected": True,
        "platformAcknowledged": True,
    })
    panel_zero = asyncio.run(adapter.read_cashout_configuration(0))
    missing_panel = asyncio.run(adapter.read_cashout_configuration(1))

    assert panel_zero["verified"] is True
    assert missing_panel["verified"] is False
    assert missing_panel["failures"] == ["CASHOUT_CONTROL_NOT_FOUND"]


def test_real_browser_backend_delegates_practice_reads_and_writes_to_guarded_client():
    class MockUiClient:
        async def set_auto_cashout(self, *, target, panel):
            pytest.fail("read-only adapter must not call the browser setter")

        async def read_auto_cashout(self, *, panel):
            return {"verified": False, "panel": panel}

        async def set_stake_input(self, *, amount, panel):
            pytest.fail("read-only adapter must not call the browser setter")

        async def read_stake_input(self, *, panel):
            return {"found": panel == 0, "input": {"value": "100"}}

        async def detect_bet_button(self, *, panel):
            return {"detected": panel == 0, "interaction_performed": False}

        async def detect_betting_window(self, *, panel):
            return {"state": "UNKNOWN", "panel": panel}

        async def detect_insufficient_balance(self):
            return detect_insufficient_balance([])

        async def read_verified_balance(self):
            return {"verified": False, "amount": 28, "currency": "BIF"}

    async def go():
        adapter = RealBrowserBackend(MockUiClient(), allow_real_placement=False)
        capabilities = adapter.verified_capabilities()
        assert capabilities["bet_submission"] is False
        assert capabilities["acceptance_receipt"] is False
        assert capabilities["settlement_receipt"] is False
        cashout_write = await adapter.set_auto_cashout(2.0)
        assert cashout_write["status"] == "CASHOUT_UNAVAILABLE"
        assert cashout_write["mutation_performed"] is False
        assert (await adapter.read_auto_cashout())["verified"] is False
        stake_write = await adapter.set_stake_input(100)
        assert stake_write["mutation_performed"] is False
        assert (await adapter.read_stake_input())["input"]["value"] == "100"
        assert (await adapter.detect_bet_button())["interaction_performed"] is False
        assert (await adapter.detect_betting_window())["state"] == "UNKNOWN"
        assert (await adapter.detect_insufficient_balance())["detected"] is False
        balance = await adapter.read_verified_balance()
        assert balance["verified"] is False and balance["currency"] == "BIF"

    asyncio.run(go())


def test_balance_parser_does_not_invent_amount_or_currency():
    observed = parse_balance_display("1,234.50 BIF")
    amount_without_currency = parse_balance_display("1,234.50")
    invalid = parse_balance_display("Balance unavailable")
    conflicting = parse_balance_display("100 BIF / 80 USD")

    assert observed == {"amount": 1234.5, "currency": "BIF"}
    assert amount_without_currency == {"amount": 1234.5, "currency": None}
    assert invalid == {"amount": None, "currency": None}
    assert conflicting == {"amount": 100.0, "currency": None}


def test_snapshot_retains_account_identity_and_cashout_observation_without_claiming_ack():
    snapshot = GameSnapshot.from_value({
        "ok": True, "payouts": ["2.00x"], "bets": 0, "betPanel": True,
        "stakeInputs": [{"slot": 0, "visible": True}],
        "placeBetButtons": [{"slot": 0, "visible": True, "disabled": False}],
        "cashoutControls": [{"slot": 0, "displayedValue": "2.00",
            "autoTabSelected": True,
            "autoCashoutEnabled": True, "platformAcknowledged": None}],
        "bettingWindow": {"state": "BETTING_OPEN", "evidence": {
            "source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM", "panel_index": 0,
            "phase_indicator_visible": True, "explicit_window_state": "BETTING_OPEN",
            "text": "Place your bets",
        }},
    })

    assert snapshot.cashout_controls[0]["platformAcknowledged"] is None
    assert snapshot.as_dict()["authentication_state"] == "UNKNOWN"
    assert snapshot.as_dict()["balance_currency"] is None
    assert snapshot.betting_window["state"] == "BETTING_OPEN"


def test_betting_window_requires_explicit_visible_phase_not_just_an_enabled_bet_button():
    base = {"source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM", "panel_index": 0,
            "phase_indicator_visible": True, "explicit_window_state": "BETTING_OPEN",
            "text": "Place your bets"}
    assert classify_betting_window(base) == "BETTING_OPEN"
    assert classify_betting_window({**base, "explicit_window_state": "BETTING_CLOSED"}) == "BETTING_CLOSED"
    assert classify_betting_window({**base, "phase_indicator_visible": False}) == "UNKNOWN"
    assert classify_betting_window({**base, "source": "arbitrary-dom"}) == "UNKNOWN"
    button_only = {"source": "AVIATOR_BETTING_PANEL_READ_ONLY", "panel_index": 0,
                   "button_visible": True, "button_enabled": True,
                   "stake_input_visible": True, "bet_tab_active": True}
    assert classify_betting_window(button_only) == "UNKNOWN"
    untrusted_snapshot = GameSnapshot.from_value({"ok": True, "bettingWindow": {
        "state": "BETTING_OPEN", "evidence": {**base, "source": "arbitrary-dom"},
    }})
    assert untrusted_snapshot.betting_window["state"] == "UNKNOWN"


def test_round_phase_and_deadline_require_explicit_visible_frame_evidence():
    assert classify_round_phase() == "UNKNOWN"
    assert classify_round_phase(phase_hints=[{"txt": "Waiting for next round", "visible": True}]) == \
        "INTERMISSION"
    assert classify_round_phase(status_messages=["Round in progress"]) == "ROUND_RUNNING"
    assert classify_round_phase(phase_hints=[{"cls": "stage-board", "txt": "Flew Away",
                                             "visible": True}]) == "ROUND_ENDED"
    assert classify_round_phase(phase_hints=[{"cls": "stage-board", "txt": "UFC / Aviator transition",
                                             "visible": True}]) == "INTERMISSION"
    assert classify_round_phase(phase_hints=[{"cls": "stage-board", "txt": "6.25x",
                                             "visible": True}]) == "ROUND_RUNNING"
    assert classify_round_phase(betting_window={"state": "BETTING_OPEN"}) == "BETTING_OPEN"
    assert classify_round_phase(betting_window={"state": "BETTING_CLOSED"}) == "BETTING_CLOSED"
    assert classify_round_phase(phase_hints=[{"txt": "Round completed", "visible": False}]) == "UNKNOWN"
    deadline = observe_betting_deadline(
        status_messages=[{"text": "Betting closes in 4.5 seconds", "visible": True}],
        observed_at="2026-10-14T20:00:00+00:00",
    )
    assert deadline["status"] == "VERIFIED"
    assert deadline["deadline_at"] == "2026-10-14T20:00:04.500000+00:00"
    reached = observe_betting_deadline(
        status_messages=[{"text": "Betting closes in 0 seconds", "visible": True}],
        observed_at="2026-10-14T20:00:00+00:00",
    )
    assert reached["status"] == "VERIFIED"
    assert reached["deadline_at"] == "2026-10-14T20:00:00+00:00"
    assert observe_betting_deadline(status_messages=["Betting closes in 500 seconds"])["status"] == "UNKNOWN"


def test_insufficient_balance_requires_explicit_observed_response_text():
    low_balance = detect_insufficient_balance([])
    mock_response = detect_insufficient_balance([
        {"text": "Insufficient balance. Please enter a lower stake."},
    ])
    unrelated = detect_insufficient_balance(["Betting window is closed"])

    assert low_balance == {
        "detected": False, "observed": False, "matched_messages": [], "source": None,
    }
    assert mock_response["detected"] is True
    assert mock_response["source"] == "OBSERVED_ALERT_TEXT"
    assert unrelated["detected"] is False
    assert unrelated["observed"] is True


def test_rules_dashboard_api_never_reports_ready_without_live_evidence(tmp_path):
    repository = Repository(tmp_path / "rules-dashboard.sqlite3")
    repository.init()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        rules_engine=RulesEngine(persistence=repository),
        history_collector=SimpleNamespace(status=lambda: {
            "status": "HEALTHY", "count": 50, "latest": {"round_id": "round-50"},
        }),
        betting_mode=SimpleNamespace(status=lambda: {"live_authorized": False}),
        live=None,
        wp=SimpleNamespace(repository=repository),
    )))

    result = dashboard(request)

    assert result["real_money_execution"] == "DISABLED"
    assert result["execution_readiness"]["ready"] is False
    assert "REAL_MONEY_EXECUTION_DISABLED_PENDING_LIVE_QUALIFICATION" \
        in result["execution_readiness"]["blockers"]
    assert len(result["execution_readiness"]["blocker_matrix"]) == 17
    assert result["execution_readiness"]["ready"] is False
    assert result["shared_selection"] is None
    assert "balance" not in result


def test_platform_state_needs_receipt_and_a_round_multiplier_does_not_settle_bet():
    click_only = classify_platform_bet_state({"click_performed": True, "button_enabled": True})
    accepted = classify_platform_bet_state({
        "platform_status": "ACCEPTED", "platform_evidence": True,
        "platform_receipt_id": "receipt-1", "target_round_id": "round-2",
        "accepted_stake": 100, "accepted_currency": "BIF",
        "session_identity": "session-1", "platform_observed_at": "2026-10-14T00:00:00Z",
        "decision_id": "decision-1",
    })
    rejected = classify_platform_bet_state({
        "platform_status": "REJECTED", "platform_evidence": True,
    })

    assert click_only == PlatformBetState.UNKNOWN
    assert accepted == PlatformBetState.ACCEPTED
    assert rejected == PlatformBetState.UNKNOWN
    assert classify_platform_bet_state({
        "platform_status": "REJECTED", "platform_evidence": True,
        "platform_receipt_id": "reject-1", "target_round_id": "round-2",
        "session_identity": "session-1", "platform_observed_at": "2026-10-14T00:00:00Z",
    }) == PlatformBetState.REJECTED
    assert classify_platform_bet_result({"round_multiplier": 4.0}) == "UNKNOWN"
    assert classify_platform_bet_result({
        "platform_evidence": True, "platform_receipt_id": "receipt-1",
        "settlement_receipt_id": "settlement-1", "target_round_id": "round-2",
        "session_identity": "session-1", "settled_at": "2026-10-14T00:00:00Z",
        "bet_result": "WIN",
    }) == "WIN"
