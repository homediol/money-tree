from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api import live as live_api
from app.betting.browser_client import AviatorBrowserClient


def _request(app: FastAPI) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/live/cashout/verification",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })


class FakeSnapshot:
    def __init__(self, *, pending="CLEAR", autobet=False):
        self.ok = True
        self.page_role = "SHARED_AVIATOR"
        self.authentication_state = "AUTHENTICATED"
        self.browser_topology_verified = True
        self.balance_currency = "BIF"
        self.balance = 10000
        self.balance_observed_at = "2026-10-15T00:00:00+00:00"
        self.cashout_controls = [{
            "slot": slot, "displayedValue": "2.00",
            "autoCashoutEnabled": True, "autoTabSelected": True,
            "autoBetEnabled": autobet, "inputVisible": True,
        } for slot in (0, 1)]
        self.bet_panel_states = [{
            "slot": slot, "pending_bet_state": pending,
            "button_visible": True, "stake_input_visible": True,
            "button_text": "Bet 100 BIF", "currency_hint": "BIF",
        } for slot in (0, 1)]
        self.stake_inputs = [{"slot": slot, "visible": True, "value": "100"} for slot in (0, 1)]
        self.place_bet_buttons = [{"slot": slot, "visible": True} for slot in (0, 1)]
        self.betting_window = {"state": "BETTING_OPEN"}


class FakeBrowserClient:
    instance = None

    def __init__(self, _settings):
        self.calls = []
        self._snapshot = FakeSnapshot()
        FakeBrowserClient.instance = self

    async def snapshot(self):
        return self._snapshot

    _require_clear_two_panel_state = staticmethod(
        AviatorBrowserClient._require_clear_two_panel_state)
    _require_auto_bet_off_two_panel_state = staticmethod(
        AviatorBrowserClient._require_auto_bet_off_two_panel_state)

    async def set_auto_cashout(self, *, target, panel):
        pytest.fail("manual setup refresh must never mutate cashout controls")

    async def close(self):
        return None


def _app(execution_panel=0) -> FastAPI:
    app = FastAPI()
    app.state.live = SimpleNamespace(live_active=False, last_cashout_refresh=None)
    app.state.betting_mode = SimpleNamespace(mode="SHADOW_REALISTIC")
    app.state.betting = SimpleNamespace(settings=object())
    app.state.rules_engine = SimpleNamespace(ui_setup_configuration=lambda: {
        "status": "CONFIGURED" if execution_panel in (0, 1) else "BLOCKED",
        "execution_panel": execution_panel, "requested_stake": 100,
        "requested_stake_currency": "BIF",
    })
    return app


def test_refresh_reads_manual_configuration_without_financial_action(monkeypatch):
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", FakeBrowserClient)
    app = _app()

    result = asyncio.run(live_api.refresh_cashout(_request(app)))

    assert result["ok"] is True
    assert [panel["status"] for panel in result["cashout_verification"]["panels"]] == ["CASHOUT_VERIFIED", "CASHOUT_VERIFIED"]
    assert [panel["auto_tab_selected"] for panel in result["cashout_verification"]["panels"]] == [True, True]
    assert [panel["auto_bet"] for panel in result["cashout_verification"]["panels"]] == ["OFF", "OFF"]
    assert [panel["auto_cashout"] for panel in result["cashout_verification"]["panels"]] == ["ON", "ON"]
    assert [panel["multiplier"] for panel in result["cashout_verification"]["panels"]] == ["2.00", "2.00"]
    assert result["cashout_verification"]["platform_acknowledged"] is None
    assert result["cashout_verification"]["financial_action_submitted"] is False
    assert FakeBrowserClient.instance.calls == []
    assert app.state.live.last_cashout_refresh == result["cashout_verification"]


def test_cashout_checklist_verifies_both_panels_without_panel_selection(monkeypatch):
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", FakeBrowserClient)

    result = asyncio.run(live_api.refresh_cashout(_request(_app(execution_panel=None))))

    verification = result["cashout_verification"]
    checklist = {row["item"]: row["status"] for row in result["manual_setup"]["checklist"]}
    assert verification["selected_panel"] is None
    assert verification["status"] == "CASHOUT_VERIFIED"
    assert checklist["Execution panel selected"] == "REQUIRED"
    assert checklist["Auto Cashout manually enabled"] == "VERIFIED"
    assert checklist["Cashout multiplier 2.00x"] == "VERIFIED"
    assert checklist["Auto Bet OFF"] == "VERIFIED"


def test_stake_mismatch_is_read_even_when_auto_tab_is_selected():
    from app.api.live import _manual_setup_observation

    snapshot = FakeSnapshot()
    snapshot.bet_panel_states[0]["bet_tab_active"] = False
    snapshot.bet_panel_states[0]["button_text"] = "Bet 200 BIF"
    # The real normalized detector nests visibility under `evidence`; do not
    # rely on a flattened field that production snapshots do not expose.
    snapshot.bet_panel_states[0].pop("button_visible")
    snapshot.bet_panel_states[0]["evidence"] = {
        "button_visible": True, "panel_visible": True,
    }
    snapshot.stake_inputs[0]["value"] = "200"
    observation, _ = _manual_setup_observation(snapshot, {
        "status": "CONFIGURED", "execution_panel": 0,
        "requested_stake": 100, "requested_stake_currency": "BIF",
    })

    checklist = {row["item"]: row["status"] for row in observation["checklist"]}
    assert checklist["Stake manually entered"] == "STAKE_MISMATCH"
    assert observation["expected_stake"] == 100
    assert observation["observed_stake"] == 200
    assert observation["button_text"] == "Bet 200 BIF"
    assert observation["financial_action_submitted"] is False


@pytest.mark.parametrize("pending,autobet", [("PENDING", False), ("UNKNOWN", False), ("CLEAR", True)])
def test_refresh_refuses_unclear_bet_or_auto_bet_state(monkeypatch, pending, autobet):
    class UnsafeBrowser(FakeBrowserClient):
        def __init__(self, settings):
            super().__init__(settings)
            self._snapshot = FakeSnapshot(pending=pending, autobet=autobet)

    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", UnsafeBrowser)

    result = asyncio.run(live_api.refresh_cashout(_request(_app())))
    panels = result["cashout_verification"]["panels"]
    if autobet:
        assert any(panel["status"] != "CASHOUT_VERIFIED" for panel in panels)
    else:
        # A read-only settings readback remains valid during a pending/unknown
        # bet; readiness is blocked separately by the panel state checklist.
        assert all(panel["status"] == "CASHOUT_VERIFIED" for panel in panels)
        assert all(panel["platform_acknowledged"] is None for panel in panels)
        pending_check = next(row for row in result["manual_setup"]["checklist"]
                             if row["item"] == "No active or pending bet")
        assert pending_check["status"] == ("BLOCKED" if pending == "PENDING" else "UNAVAILABLE")
    assert UnsafeBrowser.instance.calls == []


def test_read_only_refresh_remains_available_while_live_mode_is_active(monkeypatch):
    FakeBrowserClient.instance = None
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", FakeBrowserClient)
    app = _app()
    app.state.betting_mode.mode = "LIVE_REAL"

    result = asyncio.run(live_api.refresh_cashout(_request(app)))
    assert result["ok"] is True
    assert FakeBrowserClient.instance.calls == []


def test_manual_setup_combines_separate_bet_and_auto_tab_readbacks_without_mutation():
    from app.api.live import _manual_setup_observation

    configuration = {"execution_panel": 0, "requested_stake": 100,
                     "requested_stake_currency": "BIF"}
    bet_view = FakeSnapshot()
    bet_view.cashout_controls = [{**row, "autoTabSelected": False, "inputVisible": False}
                                 for row in bet_view.cashout_controls]
    bet_view.bet_panel_states = [{**row, "bet_tab_active": True,
                                  "frame_identity": "frame-session-1"}
                                 for row in bet_view.bet_panel_states]
    first, readbacks = _manual_setup_observation(bet_view, configuration)
    assert first["checklist"][3]["status"] == "STAKE_VERIFIED"
    assert all(row["status"] == "CASHOUT_UNAVAILABLE" for row in first["cashout_panels"])

    auto_view = FakeSnapshot()
    auto_view.bet_panel_states = [{**row, "bet_tab_active": False,
                                   "frame_identity": "frame-session-1"}
                                  for row in auto_view.bet_panel_states]
    auto_view.stake_inputs = [{**row, "visible": False} for row in auto_view.stake_inputs]
    second, _ = _manual_setup_observation(auto_view, configuration, readbacks)
    assert second["checklist"][3]["status"] == "STAKE_VERIFIED"
    assert second["stake_readback_source"] == "CACHED_READ_ONLY_PLATFORM_READBACK"
    assert all(row["status"] == "CASHOUT_VERIFIED" for row in second["cashout_panels"])

    reloaded_frame = FakeSnapshot()
    reloaded_frame.bet_panel_states = [{**row, "bet_tab_active": False,
                                        "frame_identity": "frame-session-2"}
                                       for row in reloaded_frame.bet_panel_states]
    reloaded_frame.stake_inputs = [{**row, "visible": False} for row in reloaded_frame.stake_inputs]
    reloaded, _ = _manual_setup_observation(reloaded_frame, configuration, readbacks)
    assert reloaded["checklist"][3]["status"] == "UNAVAILABLE"

    stale_readbacks = {**readbacks, "stake": {
        **readbacks["stake"], "observed_at": "2020-01-01T00:00:00+00:00"}}
    stale, _ = _manual_setup_observation(auto_view, configuration, stale_readbacks)
    assert stale["checklist"][3]["status"] == "UNAVAILABLE"


def test_new_shared_frame_readback_overrides_older_cashout_unknown():
    from datetime import datetime, timezone

    from app.api.rules_engine import _shared_aviator_readiness

    stamp = datetime.now(timezone.utc).isoformat()
    pages = {
        "browser_count": 1, "context_count": 1, "page_count": 1,
        "aviator_page_count": 1, "aviator_target_id": "shared-page",
        "aviator_page_state": "OPEN", "game_frame_count": 1,
        "game_frame_state": "READY", "history_target_id": "shared-page",
        "history_page_state": "OPEN", "betting_target_id": "shared-page",
        "betting_page_state": "OPEN",
    }
    health = SimpleNamespace(health_status=lambda persist=False: {
        "collector_process_status": "RUNNING", "heartbeat_state": "FRESH",
        "last_observed_round": "round-1", "last_observed_at": stamp,
        "browser_connected": True, "browser_pages": pages,
        "betting_ui_configuration": {"status": "READY", "updated_at": stamp},
        "cashout_initialization": {"status": "UNKNOWN", "updated_at": "2020-01-01T00:00:00+00:00", "panels": []},
    })
    panels = [{"panel": index + 1, "status": "CASHOUT_VERIFIED", "auto_bet": "OFF",
               "auto_cashout": "ON", "multiplier": "2.00", "auto_tab_selected": True,
               "failures": []} for index in (0, 1)]
    live = SimpleNamespace(last_cashout_refresh={
        "source": "SHARED_AVIATOR_FRAME_READBACK", "page_role": "SHARED_AVIATOR",
        "browser_topology_verified": True, "observed_at": stamp,
        "status": "CASHOUT_VERIFIED", "auto_bet": "OFF", "panels": panels,
        "platform_acknowledged": None,
        "betting_controls": {"status": "READY", "panels": [], "stake_inputs": [], "bet_buttons": []},
    })
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        history_collector=health, live=live,
    )))

    result = _shared_aviator_readiness(request)

    assert result["cashout_configured"] == "READY"
    assert result["cashout_initialization"]["source"] == "SHARED_AVIATOR_FRAME_READBACK"
    assert result["cashout_readback_source"] == "FRESH_SHARED_AVIATOR_FRAME"
    assert result["platform_cashout_acknowledged"] is None
