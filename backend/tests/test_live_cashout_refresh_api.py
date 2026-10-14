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
        "type": "http", "method": "POST", "path": "/api/live/cashout/refresh",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })


class FakeSnapshot:
    def __init__(self, *, pending="CLEAR", autobet=False):
        self.ok = True
        self.page_role = "SHARED_AVIATOR"
        self.authentication_state = "AUTHENTICATED"
        self.browser_topology_verified = True
        self.cashout_controls = [{
            "slot": slot, "displayedValue": "2.00",
            "autoCashoutEnabled": True, "autoTabSelected": True,
            "autoBetEnabled": autobet,
        } for slot in (0, 1)]
        self.bet_panel_states = [{
            "slot": slot, "pending_bet_state": pending,
            "button_visible": True, "stake_input_visible": True,
        } for slot in (0, 1)]
        self.stake_inputs = [{"slot": slot, "visible": True} for slot in (0, 1)]
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
        self.calls.append((target, panel))
        return {"ok": True}

    async def close(self):
        return None


def _app() -> FastAPI:
    app = FastAPI()
    app.state.live = SimpleNamespace(live_active=False, last_cashout_refresh=None)
    app.state.betting_mode = SimpleNamespace(mode="SHADOW_REALISTIC")
    app.state.betting = SimpleNamespace(settings=object())
    return app


def test_refresh_configures_and_reads_both_panels_without_financial_action(monkeypatch):
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", FakeBrowserClient)
    app = _app()

    result = asyncio.run(live_api.refresh_cashout(_request(app)))

    assert result["ok"] is True
    assert [panel["status"] for panel in result["cashout_refresh"]["panels"]] == ["READY", "READY"]
    assert [panel["auto_tab_selected"] for panel in result["cashout_refresh"]["panels"]] == [True, True]
    assert [panel["auto_bet"] for panel in result["cashout_refresh"]["panels"]] == ["OFF", "OFF"]
    assert [panel["auto_cashout"] for panel in result["cashout_refresh"]["panels"]] == ["ON", "ON"]
    assert [panel["multiplier"] for panel in result["cashout_refresh"]["panels"]] == ["2.00", "2.00"]
    assert result["cashout_refresh"]["platform_acknowledged"] is None
    assert result["cashout_refresh"]["financial_action_submitted"] is False
    assert FakeBrowserClient.instance.calls == [(2.0, 0), (2.0, 1)]
    assert app.state.live.last_cashout_refresh == result["cashout_refresh"]


@pytest.mark.parametrize("pending,autobet", [("PENDING", False), ("UNKNOWN", False), ("CLEAR", True)])
def test_refresh_refuses_unclear_bet_or_auto_bet_state(monkeypatch, pending, autobet):
    class UnsafeBrowser(FakeBrowserClient):
        def __init__(self, settings):
            super().__init__(settings)
            self._snapshot = FakeSnapshot(pending=pending, autobet=autobet)

    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", UnsafeBrowser)

    with pytest.raises(HTTPException) as failure:
        asyncio.run(live_api.refresh_cashout(_request(_app())))

    assert failure.value.status_code == 409
    assert UnsafeBrowser.instance.calls == []


def test_refresh_is_disabled_while_live_mode_is_active(monkeypatch):
    FakeBrowserClient.instance = None
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", FakeBrowserClient)
    app = _app()
    app.state.betting_mode.mode = "LIVE_REAL"

    with pytest.raises(HTTPException) as failure:
        asyncio.run(live_api.refresh_cashout(_request(app)))

    assert failure.value.status_code == 409
    assert FakeBrowserClient.instance is None


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
    panels = [{"panel": index + 1, "status": "READY", "auto_bet": "OFF",
               "auto_cashout": "ON", "multiplier": "2.00", "auto_tab_selected": True,
               "failures": []} for index in (0, 1)]
    live = SimpleNamespace(last_cashout_refresh={
        "source": "SHARED_AVIATOR_FRAME_READBACK", "page_role": "SHARED_AVIATOR",
        "browser_topology_verified": True, "observed_at": stamp,
        "status": "READY", "auto_bet": "OFF", "panels": panels,
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
