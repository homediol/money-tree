"""Tests for the betting automation module (Part 1).

Covers the deterministic SimulationBackend, the BettingSession state machine
(sim resolution, auto-stop goals, duplicate/after-stop handling), REAL-mode
read-only gating (defer/reject — never a fake placement), the BettingManager,
and the /api/betting REST surface through TestClient.

No test opens a CDP socket or places a real bet.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.betting.browser_adapters import (
    PlacementUnavailable,
    RealBrowserBackend,
    SimulationBackend,
)
from app.betting.browser_client import AviatorBrowserClient, CdpSession, BrowserUiError, BrowserUnreachable, Target
from app.betting.selectors import GAME_DOCUMENT_PROBE, READ_BALANCE, GameSnapshot, SNAPSHOT, parse_amount
from app.betting.config import BettingSettings
from app.betting.events import betting_event, ledger_event, notice_event, state_event
from app.betting.profiles import PROFILES, profile_keys
from app.betting.schemas import DecisionIntent, SessionStartRequest
from app.betting.session import (
    BettingManager,
    BettingSession,
    DuplicateDecision,
    SessionAlreadyActive,
    SessionNotRunning,
)
from app.betting.states import (
    DecisionRejectReason,
    Mode,
    OutcomeStatus,
    SessionState,
)
from main import app

MIN_CRASH_1 = 1.21  # SIM_CRASH_SEQUENCE[0] — crashes below a 2.0 target


def fast_settings(**kw) -> BettingSettings:
    """Tight cadences + hard-coded safety so tests never touch a live browser."""
    base = dict(
        cdp_http="http://127.0.0.1:1",  # deliberately dead; never contacted
        allow_real_placement=False,
        poll_interval_s=0.02,
        browser_recheck_s=0.05,
        sim_round_every_s=0.12,
    )
    base.update(kw)
    return BettingSettings(**base)


def sim_request(**kw) -> SessionStartRequest:
    return SessionStartRequest(action="start", mode="SIMULATION",
                               profile="PROFILE_A", **kw)


def real_request(**kw) -> SessionStartRequest:
    return SessionStartRequest(action="start", mode="REAL",
                               profile="PROFILE_A", **kw)


def decision(decision_id: str, target_multiplier: float = 2.0,
             **kw) -> DecisionIntent:
    return DecisionIntent(decision_id=decision_id,
                          round_id=kw.pop("round_id", f"round-{decision_id}"),
                          execute=kw.pop("execute", True),
                          profile=kw.pop("profile", "PROFILE_A"),
                          target_multiplier=target_multiplier, **kw)


def _run(coro):
    return asyncio.run(coro)


def test_cdp_evaluate_invokes_function_and_awaits_result():
    async def go():
        session = CdpSession("ws://unused", fast_settings())
        captured = {}

        async def fake_call(method, params):
            captured.update({"method": method, "params": params})
            return {"result": {"type": "object", "value": {"ok": True}}}

        session.call = fake_call
        value = await session.evaluate("() => ({ ok: true })")
        return value, captured

    value, captured = _run(go())
    assert value == {"ok": True}
    assert captured["method"] == "Runtime.evaluate"
    assert captured["params"]["expression"] == "(() => ({ ok: true }))()"
    assert captured["params"]["awaitPromise"] is True


def test_cdp_evaluate_targets_an_explicit_frame_context():
    async def go():
        session = CdpSession("ws://unused", fast_settings())
        captured = {}

        async def fake_call(method, params):
            captured.update({"method": method, "params": params})
            return {"result": {"type": "boolean", "value": True}}

        session.call = fake_call
        assert await session.evaluate("() => true", context_id=42) is True
        return captured

    captured = _run(go())
    assert captured["params"]["contextId"] == 42


def test_snapshot_queries_directly_inside_aviator_next_context():
    assert "const d = document" in SNAPSHOT
    assert "d.querySelectorAll('.payouts-block .payout')" in SNAPSHOT
    assert "d.querySelectorAll('app-bet-control')" in SNAPSHOT
    assert "panel.querySelector('.bet-block')" in SNAPSHOT
    assert r"const actionPattern = /^(bet|cancel|cash\s*out)\b/i" in SNAPSHOT
    assert "visible_action_labels: actionLabels" in SNAPSHOT
    assert "pending_indicator:" in SNAPSHOT
    assert "panel_id: `PANEL_${i + 1}`" in SNAPSHOT
    assert "[role=\"alert\"]" in SNAPSHOT
    from app.betting import selectors
    assert not hasattr(selectors, "SET_AUTO_CASHOUT")
    assert not hasattr(selectors, "SET_STAKE_INPUT")


def test_provider_session_auth_is_hashed_and_login_prompt_takes_precedence():
    frame = Target("frame", "iframe",
        "https://aviaport.spribegaming.com/aviator?token=private-token&user=private-user", "ws://unused")
    state, identity = AviatorBrowserClient._provider_authentication(
        frame, {"authenticationState": "UNKNOWN"}, {"loginPromptVisible": False},
    )
    assert state == "AUTHENTICATED"
    assert identity and "private-token" not in identity and "private-user" not in identity
    assert AviatorBrowserClient._provider_authentication(
        frame, {"authenticationState": "UNKNOWN"}, {"loginPromptVisible": True},
    ) == ("AUTH_REQUIRED", None)


def test_mock_ui_practice_configures_cashout_and_stake_without_bet_submission():
    async def go():
        state = {
            "cashout_value": "1.10", "cashout_enabled": False,
            "auto_selected": False, "stake": "200", "alerts": [],
        }

        class MockPageSession:
            calls = []

            async def evaluate(self, expression, **kwargs):
                self.calls.append(expression)
                assert kwargs.get("context_id") == 77
                assert ".btn-success.bet" not in expression
                if ".cash-out-switcher .input-switch" in expression:
                    state.update(cashout_value="2.00", cashout_enabled=True,
                                 auto_selected=True)
                    return {"ok": True, "autoTabClicked": True, "toggleClicked": True,
                            "displayedValue": "2.00", "platformAcknowledged": None}
                if ".bet-block input[type=\"text\"]" in expression:
                    state["stake"] = "100"
                    return {"ok": True, "displayedValue": "100"}
                raise AssertionError("unexpected UI interaction")

        client = AviatorBrowserClient(fast_settings())
        page = Target("bet-page", "page",
                      "https://winner.rw/en/virtual/crash-games/aviator", "ws://bet-page")
        frame = Target("game-frame", "iframe",
                       "https://aviaport.spribegaming.com/aviator", "ws://game-frame", "bet-page")
        targets = [page, frame]
        client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
        client._browser_page_roles = lambda: {
            "browser_count": 1, "context_count": 1, "page_count": 1,
            "aviator_page_count": 1, "aviator_target_id": "bet-page",
            "aviator_page_state": "OPEN", "game_frame_state": "READY",
        }
        client.readiness = lambda: _ready_betting()
        session = MockPageSession()
        client._connect_session = lambda: _session(session)
        client._game_context = lambda _session: _context(77)

        async def snapshot():
            value = GameSnapshot.from_value({
                "ok": True, "observedAt": datetime.now(timezone.utc).isoformat(),
                "betPanel": True, "betTabActive": True,
                "stakeInputs": [{"slot": 0, "placeholder": "100", "value": state["stake"],
                                 "visible": True, "minAttribute": None},
                                {"slot": 1, "placeholder": "100", "value": "200",
                                 "visible": True, "minAttribute": None}],
                "placeBetButtons": [{"slot": 0, "associatedPanel": 0,
                                     "selector": ".btn-success.bet", "text": "Bet 200 BIF",
                                     "visible": True, "disabled": False},
                                    {"slot": 1, "associatedPanel": 1,
                                     "selector": ".btn-success.bet", "text": "Bet 200 BIF",
                                     "visible": True, "disabled": False}],
                "cashoutControls": [{"slot": 0, "displayedValue": state["cashout_value"],
                                     "autoCashoutEnabled": state["cashout_enabled"],
                                     "autoBetEnabled": False,
                                     "autoTabSelected": state["auto_selected"],
                                     "inputVisible": state["auto_selected"],
                                     "autoTabVisible": True, "platformAcknowledged": None},
                                    {"slot": 1, "displayedValue": "1.10",
                                     "autoCashoutEnabled": False, "autoBetEnabled": False,
                                     "autoTabSelected": False, "inputVisible": False,
                                     "autoTabVisible": True, "platformAcknowledged": None}],
                "bettingWindow": {"evidence": {
                    "source": "AVIATOR_BETTING_PANEL_READ_ONLY",
                    "selector": ".btn-success.bet", "panel_index": 0,
                    "button_visible": True,
                    "button_enabled": True, "stake_input_visible": True, "bet_tab_active": True,
                }},
                "betPanelStates": [{"slot": 0, "panel_id": "PANEL_1", "panel_visible": True,
                    "source": "AVIATOR_BETTING_PANEL_READ_ONLY",
                    "button_visible": True, "button_enabled": True,
                    "main_label": "Bet", "button_text": "Bet 200 BIF",
                    "displayed_stake_text": "Bet 200 BIF", "button_color": "rgb(40, 167, 69)",
                    "visible_action_labels": ["Bet 200 BIF"],
                    "frame_identity": "https://aviator-next.spribegaming.com/",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "stake_input_visible": True, "bet_tab_active": True},
                    {"slot": 1, "panel_id": "PANEL_2", "panel_visible": True,
                     "source": "AVIATOR_BETTING_PANEL_READ_ONLY",
                     "button_visible": True, "button_enabled": True,
                     "main_label": "Bet", "button_text": "Bet 200 BIF",
                     "displayed_stake_text": "Bet 200 BIF", "button_color": "rgb(40, 167, 69)",
                     "visible_action_labels": ["Bet 200 BIF"],
                     "frame_identity": "https://aviator-next.spribegaming.com/",
                     "observed_at": datetime.now(timezone.utc).isoformat(),
                     "stake_input_visible": True, "bet_tab_active": True}],
                "statusMessages": state["alerts"],
                "balanceText": "28 BIF",
            })
            value.authentication_state = "AUTHENTICATED"
            value.page_role = "SHARED_AVIATOR"
            value.browser_topology_verified = True
            value.provider_identity = "winner.rw"
            value.session_identity = "mock-session"
            value.balance_observed_at = "2026-10-14T00:00:00+00:00"
            return value

        client.snapshot = snapshot
        async def _ready_betting():
            return {"connected": True, "stage": "ready",
                    "authentication_state": "AUTHENTICATED",
                    "page_role": "SHARED_AVIATOR", "browser_topology_verified": True}
        async def _session(value): return value
        async def _context(value): return value

        with pytest.raises(BrowserUiError, match="MANUAL_OPERATOR_CONFIGURATION_REQUIRED"):
            await client.set_auto_cashout(2.00, panel=0)
        with pytest.raises(BrowserUiError, match="MANUAL_OPERATOR_CONFIGURATION_REQUIRED"):
            await client.set_stake_input(100, panel=0)
        button = await client.detect_bet_button(panel=0)
        window = await client.detect_betting_window(panel=0)
        state["alerts"] = [{"text": "Insufficient balance. Please enter a lower stake."}]
        insufficient = await client.detect_insufficient_balance()
        balance = await client.read_verified_balance()
        stake_details = await client.read_stake_input(panel=0)

        assert stake_details["minimum_stake_evidence"]["platform_enforcement_verified"] is False
        assert button["detected"] is True and button["interaction_performed"] is False
        # An enabled Bet button is panel affordance, not direct phase evidence.
        assert window["state"] == "UNKNOWN"
        assert insufficient["detected"] is True
        assert insufficient["source"] == "OBSERVED_ALERT_TEXT"
        assert balance["verified"] is True
        assert session.calls == []
        return session.calls

    calls = _run(go())
    assert calls == []


def test_ui_practice_mutations_block_on_unverified_history_page():
    async def go():
        client = AviatorBrowserClient(fast_settings())
        client.snapshot = lambda: _snapshot_with_controls()
        client.readiness = lambda: _unknown_history_readiness()
        client._connect_session = lambda: pytest.fail("unverified page must not be mutated")

        with pytest.raises(BrowserUiError, match="MANUAL_OPERATOR_CONFIGURATION_REQUIRED"):
            await client.set_auto_cashout(2.00)
        with pytest.raises(BrowserUiError, match="MANUAL_OPERATOR_CONFIGURATION_REQUIRED"):
            await client.set_stake_input(100)
        with pytest.raises(BrowserUiError, match="MANUAL_OPERATOR_CONFIGURATION_REQUIRED"):
            await client.set_auto_cashout(1.50)

    async def _snapshot_with_controls():
        return GameSnapshot.from_value({"ok": True, "cashoutControls": [
            {"slot": 0, "autoTabVisible": True, "displayedValue": "1.10"}],
            "stakeInputs": [{"slot": 0, "visible": True, "value": "200"}]})
    async def _unknown_history_readiness():
        return {"connected": True, "stage": "ready", "authentication_state": "UNKNOWN",
                "page_role": "SHARED_AVIATOR", "browser_topology_verified": False}

    _run(go())


def test_browser_reconnect_uses_new_collector_owned_frame(monkeypatch):
    class FakeCdpSession:
        def __init__(self, ws_url, settings):
            self.ws_url = ws_url
            self.settings = settings
            self.connected = False
            self.closed = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True
            self.connected = False

    monkeypatch.setattr("app.betting.browser_client.CdpSession", FakeCdpSession)

    async def go():
        client = AviatorBrowserClient(fast_settings())
        aviator = Target("aviator", "page", "https://winner.rw/en/virtual/crash-games/aviator", "ws://aviator")
        new_frame = Target("new-frame", "iframe", "https://aviaport.spribegaming.com/aviator",
                           "ws://new-frame", "aviator")
        targets = [aviator, new_frame]
        client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
        client._browser_page_roles = lambda: {
            "browser_count": 1, "context_count": 1, "page_count": 1,
            "aviator_page_count": 1, "aviator_target_id": "aviator",
            "aviator_page_state": "OPEN", "game_frame_state": "READY",
        }
        old = FakeCdpSession("ws://old-frame", fast_settings())
        old.connected = True
        client._session = old
        client._frame_target_id = "old-frame"

        session = await client._connect_session()

        assert old.closed is True
        assert session.ws_url == "ws://new-frame"
        assert session.connected is True
        assert client._frame_target_id == "new-frame"

    _run(go())


def test_nested_aviator_frame_is_found_in_cdp_frame_tree():
    tree = {"frame": {"id": "outer", "url": "https://aviaport.example"},
            "childFrames": [{"frame": {"id": "game", "url":
                              "https://aviator-next.spribegaming.com/"}}]}
    assert AviatorBrowserClient._find_frame(tree, "aviator-next")["id"] == "game"


def test_nested_aviator_frame_attach_retries_transient_empty_frame_tree():
    class DelayedFrameSession:
        def __init__(self):
            self.frame_tree_reads = 0

        async def call(self, method, params=None):
            if method == "Page.enable":
                return {}
            if method == "Page.getFrameTree":
                self.frame_tree_reads += 1
                child_frames = []
                if self.frame_tree_reads >= 3:
                    child_frames = [{"frame": {
                        "id": "nested-game", "url": "https://aviator-next.spribegaming.com/",
                    }}]
                return {"frameTree": {"frame": {"id": "outer", "url": "https://aviaport.spribegaming.com"},
                                       "childFrames": child_frames}}
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 77}
            raise AssertionError(f"unexpected CDP method: {method}")

    async def go():
        client = AviatorBrowserClient(fast_settings())
        session = DelayedFrameSession()
        context_id = await client._game_context(session)
        assert context_id == 77
        assert session.frame_tree_reads == 3

    _run(go())


def test_browser_target_resolution_uses_only_authoritative_shared_aviator_page():
    client = AviatorBrowserClient(BettingSettings())
    targets = [
        Target("page-a", "page", "https://winner.rw/aviator", "ws://page-a"),
        Target("frame-b", "iframe", "https://aviaport.spribegaming.com/aviator",
               "ws://frame-b", "page-b"),
        Target("page-b", "page", "https://winner.rw/aviator", "ws://page-b"),
        Target("frame-a", "iframe", "https://aviaport.spribegaming.com/aviator",
               "ws://frame-a", "page-a"),
    ]
    client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
    client._browser_page_roles = lambda: {}
    assert client.resolve_targets() == (None, None)
    client._browser_page_roles = lambda: {
        "browser_count": 1, "context_count": 1, "page_count": 1,
        "aviator_page_count": 1, "aviator_target_id": "page-a",
        "aviator_page_state": "OPEN", "game_frame_state": "READY",
    }
    page, frame = client.resolve_targets()
    assert page.id == "page-a"
    assert frame.id == "frame-a"
    assert client._selected_page_role == "SHARED_AVIATOR"
    assert client._topology_verified() is False, "two game targets must block shared topology"


def test_browser_target_resolution_requires_shared_page_identity_and_does_not_guess():
    client = AviatorBrowserClient(BettingSettings())
    page = Target("history-page", "page",
                  "https://winner.rw/en/virtual/crash-games/aviator", "ws://history-page")
    frame = Target("game-frame", "iframe",
                   "https://aviaport.spribegaming.com/aviator", "ws://game-frame",
                   "history-page")
    targets = [page, frame]
    client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
    client._browser_page_roles = lambda: {
        "browser_count": 1, "context_count": 1, "page_count": 1,
        "aviator_page_count": 1, "aviator_target_id": "history-page",
        "aviator_page_state": "OPEN", "game_frame_state": "READY",
    }

    selected, selected_frame = client.resolve_targets()

    assert selected is page
    assert selected_frame is frame
    assert client._selected_page_role == "SHARED_AVIATOR"
    assert client._topology_verified() is True


def test_browser_target_resolution_does_not_guess_when_no_recorded_page_is_open():
    client = AviatorBrowserClient(BettingSettings())
    targets = [Target("unassigned", "page", "https://winner.rw/aviator", "ws://unassigned")]
    client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
    client._browser_page_roles = lambda: {
        "browser_count": 1, "context_count": 1, "page_count": 1,
        "aviator_page_count": 0, "aviator_target_id": "closed-aviator",
        "aviator_page_state": "CLOSED", "game_frame_state": "NOT_READY",
    }

    assert client.resolve_targets() == (None, None)


def test_browser_readiness_reports_shared_aviator_page_topology():
    page = Target("history-page", "page",
                  "https://winner.rw/en/virtual/crash-games/aviator", "ws://history-page")
    frame = Target("game-frame", "iframe",
                   "https://aviaport.spribegaming.com/aviator", "ws://game-frame",
                   "history-page")
    targets = [page, frame]

    class ReadOnlySession:
        async def evaluate(self, expression, **kwargs):
            if expression == READ_BALANCE:
                return {"ok": True, "text": "1,234 USD", "authenticationState": "AUTHENTICATED"}
            if expression == GAME_DOCUMENT_PROBE:
                return {"ok": True, "url": "https://aviator-next.spribegaming.com/"}
            raise AssertionError("unexpected browser expression")

    client = AviatorBrowserClient(BettingSettings())
    client._discover = lambda: (setattr(client, "_target_cache", targets) or targets)
    client._browser_page_roles = lambda: {
        "browser_count": 1, "context_count": 1, "page_count": 1,
        "aviator_page_count": 1, "aviator_target_id": "history-page",
        "aviator_page_state": "OPEN", "game_frame_state": "READY",
    }

    async def connect_read_only():
        return ReadOnlySession()

    async def game_context(_session):
        return 17

    client._connect_session = connect_read_only
    client._game_context = game_context
    result = _run(client.readiness())

    assert result["connected"] is True
    assert result["stage"] == "ready"
    assert result["page_found"] is True
    assert result["game_found"] is True
    assert result["page_role"] == "SHARED_AVIATOR"
    assert result["browser_topology_verified"] is True


def test_low_level_browser_bet_action_is_disabled_until_separate_authorization():
    client = AviatorBrowserClient(BettingSettings())
    client._connect_session = lambda: pytest.fail("disabled method must not open the browser")
    with pytest.raises(BrowserUiError, match="REAL_MONEY_EXECUTION_DISABLED"):
        asyncio.run(client.place_bet(amount_bif=100, cashout=2.0, slot=0))


@pytest.mark.parametrize(("text", "expected"), [
    ("1,000BIF", 1000),
    ("1,000 BIF", 1000),
    ("12.3K", 12_300),
    ("12.3KBIF", 12_300),
    ("2 M BIF", 2_000_000),
])
def test_balance_parser_distinguishes_currency_from_scale_suffix(text, expected):
    assert parse_amount(text) == expected


async def _wait_task(task: asyncio.Task, timeout: float = 6.0) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"session task did not finish within {timeout}s")


# ── SimulationBackend (deterministic game loop) ─────────────────────────────

def test_sim_backend_first_crash_is_scripted_loss():
    async def go():
        sim = SimulationBackend(round_every_s=0.05)
        await sim.place(amount_bif=500, target_multiplier=2.0,
                        bet_slot=0, decision_id="d-1")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            resolved = await sim.collect_resolved()
            if resolved:
                return resolved
            await asyncio.sleep(0.02)
        pytest.fail("round never closed")
    (resolved,) = _run(go())
    assert resolved["crash_point"] == MIN_CRASH_1
    assert resolved["won"] is False
    assert resolved["pnl_bif"] == -500
    assert resolved["round_label"] == "SIM-R0"


def test_sim_backend_win_resolution_math_and_balance():
    async def go():
        sim = SimulationBackend(round_every_s=0.05, initial_balance=2000.0,
                                crash_sequence=[3.10])
        await sim.place(amount_bif=500, target_multiplier=2.0,
                        bet_slot=0, decision_id="d-win")
        # Balance untouched until the round closes.
        assert sim.current_balance() == 2000.0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            resolved = await sim.collect_resolved()
            if resolved:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("round never closed")
        (item,) = resolved
        assert item["crash_point"] == 3.10
        assert item["won"] is True
        assert item["pnl_bif"] == 500          # 500 * (2.0 - 1)
        assert sim.current_balance() == 2500.0
    _run(go())


def test_sim_backend_place_between_rounds_joins_next_round():
    async def go():
        sim = SimulationBackend(round_every_s=0.05, crash_sequence=[1.21, 3.10])
        sim._open_next_round()          # index 0 (crash 1.21)
        sim._close_current()            # empty round closes
        await sim.place(amount_bif=200, target_multiplier=1.5,
                        bet_slot=1, decision_id="d-next")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            resolved = await sim.collect_resolved()
            if resolved:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("round never closed")
        (item,) = resolved
        assert item["round_label"] == "SIM-R1"
        assert item["crash_point"] == 3.10
        assert item["won"] is True
        assert item["pnl_bif"] == 100          # 200 * (1.5 - 1)
    _run(go())


def test_sim_backend_rejects_stake_above_balance():
    async def go():
        sim = SimulationBackend(round_every_s=0.05, initial_balance=100.0)
        with pytest.raises(PlacementUnavailable) as exc:
            await sim.place(amount_bif=500, target_multiplier=2.0,
                            bet_slot=0, decision_id="d-poor")
        assert exc.value.reason == "insufficient_balance"
        assert sim.pending_count() == 0
    _run(go())


def test_sim_backend_payouts_newest_first_and_describe():
    async def go():
        sim = SimulationBackend(round_every_s=0.05)
        # Force three closed rounds to build a payout history.
        for _ in range(3):
            sim._open_next_round()
            sim._close_current()
        payouts = sim.crash_payouts()
        # Index 0/1/2 crashes: 1.21, 3.10, 5.75 — newest first.
        assert payouts == [5.75, 3.10, 1.21]
        assert payouts[0] == 5.75
        desc = sim.describe()
        assert desc["backend"] == "simulation"
        assert desc["simulated"] is True
        assert desc["round_every_s"] == 0.05
    _run(go())


# ── BettingSession — simulation lifecycle ───────────────────────────────────

def test_idle_manager_reports_explicit_off_mode():
    status = BettingManager(fast_settings()).status()
    assert status["state"] == "IDLE"
    assert status["mode"] == "OFF"
    assert status["automatic_enabled"] is False


def test_sim_session_places_resolves_and_autostops_on_max_rounds():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request(max_rounds=1))
        session = mgr.session
        entry = await mgr.submit_decision(decision("d-max-r", 2.0))
        assert entry["status"] == OutcomeStatus.PLACED.value
        assert entry["simulated"] is True
        assert entry["mode"] == "SIMULATION"
        assert entry["amount_bif"] == 500       # profile default applied
        await _wait_task(session._task)
        assert session.state == SessionState.STOPPED
        assert session.stop_reason == "max_rounds"
        st = session.status()
        assert st["resolved_decisions"] == 1
        assert st["running_pnl_bif"] == -500
        assert st["simulated"] is True
        (entry2,) = session.ledger_slice()
        assert entry2["status"] == OutcomeStatus.RESOLVED.value
        assert entry2["crash_point"] == MIN_CRASH_1
        assert entry2["pnl_bif"] == -500
        assert entry2["note"] == "lost"
        assert entry2["simulated"] is True
    _run(go())


def test_sim_session_win_accumulates_pnl():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request(max_rounds=1))
        session = mgr.session
        session.backend.crash_sequence = [3.10]
        # Profile A requires 2.00x; a 3.10x crash yields +500.
        entry = await mgr.submit_decision(decision("d-win", 2.0))
        assert entry["status"] == OutcomeStatus.PLACED.value
        await _wait_task(session._task)
        st = session.status()
        assert st["state"] == SessionState.STOPPED.value
        assert st["stop_reason"] == "max_rounds"
        assert st["running_pnl_bif"] == 500.0
        assert st["resolved_decisions"] == 1
        (e,) = session.ledger_slice()
        assert e["pnl_bif"] == 500.0
        assert e["note"] == "won"
    _run(go())


def test_auto_stop_target_profit():
    async def go():
        mgr = BettingManager(fast_settings())
        # +500 profit crosses the +20 goal on the first resolution.
        await mgr.start_session(sim_request(target_profit_bif=20))
        session = mgr.session
        session.backend.crash_sequence = [3.10]
        await mgr.submit_decision(decision("d-profit", 2.0))
        await _wait_task(session._task)
        assert session.state == SessionState.STOPPED
        assert session.stop_reason == "target_profit"
        assert session.status()["running_pnl_bif"] == 500.0
    _run(go())


def test_auto_stop_max_loss():
    async def go():
        mgr = BettingManager(fast_settings())
        # -500 loss exceeds the -400 floor.
        await mgr.start_session(sim_request(max_loss_bif=400))
        session = mgr.session
        await mgr.submit_decision(decision("d-loss", 2.0))
        await _wait_task(session._task)
        assert session.state == SessionState.STOPPED
        assert session.stop_reason == "max_loss"
        assert session.status()["running_pnl_bif"] == -500
    _run(go())


def test_duplicate_decision_raises_and_ledger_keeps_one_entry():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request(max_rounds=2))
        session = mgr.session
        await mgr.submit_decision(decision("d-dup", 2.0))
        with pytest.raises(DuplicateDecision) as exc:
            await mgr.submit_decision(decision("d-dup", 2.0))
        assert exc.value.code == "duplicate_decision"
        assert len(session.ledger) == 1
        await mgr.stop_session()
        await _wait_task(session._task)
    _run(go())


def test_submit_and_stop_after_session_ended_raise_session_not_running():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request(max_rounds=1))
        session = mgr.session
        await mgr.submit_decision(decision("d-then-stop", 2.0))
        await _wait_task(session._task)
        assert session.state == SessionState.STOPPED
        with pytest.raises(SessionNotRunning) as exc:
            await mgr.submit_decision(decision("d-late", 2.0))
        assert exc.value.code == "session_not_running"
        with pytest.raises(SessionNotRunning):
            await mgr.stop_session()
    _run(go())


def test_manager_rejects_second_session_while_active_then_allows_restart():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request(max_rounds=1))
        session = mgr.session
        with pytest.raises(SessionAlreadyActive) as exc:
            await mgr.start_session(sim_request(max_rounds=1))
        assert exc.value.code == "session_already_active"
        await mgr.stop_session()
        await _wait_task(session._task)
        # A fresh session may start after the previous one stopped.
        await mgr.start_session(sim_request(max_rounds=1))
        assert mgr.session.session_id == "S-2"
        await mgr.stop_session()
        await _wait_task(mgr.session._task)
    _run(go())


def test_manager_idle_status_and_submit_fail_cleanly():
    async def go():
        mgr = BettingManager(fast_settings())
        st = mgr.status()
        assert st["state"] == SessionState.IDLE.value
        assert st["session_id"] is None
        assert mgr.ledger() == []
        with pytest.raises(SessionNotRunning) as exc:
            await mgr.submit_decision(decision("d-idle", 2.0))
        assert exc.value.code == "session_not_running"
    _run(go())


# ── REAL-mode read-only gating (no browser, no fake placements) ─────────────

def _real_session(settings: BettingSettings) -> BettingSession:
    session = BettingSession(settings=settings, request=real_request(),
                             session_id="S-REAL")
    session.start()                      # builds RealBrowserBackend; no I/O
    assert session.state == SessionState.STARTING
    return session


def test_real_gating_unknown_balance_is_rejected():
    async def go():
        session = _real_session(fast_settings())
        # last_balance starts None → funds can't be verified → reject.
        entry = await session.submit_decision(decision("r-1", 2.0))
        assert entry["status"] == OutcomeStatus.REJECTED.value
        assert entry["reason"] == DecisionRejectReason.UI_NOT_READY.value
        assert entry["simulated"] is False
        assert "balance unknown" in (entry["note"] or "")
    _run(go())


def test_real_gating_insufficient_balance_is_rejected():
    async def go():
        session = _real_session(fast_settings())
        session.last_balance = 100.0     # < 500 default stake
        session.last_ui_ready = True
        entry = await session.submit_decision(decision("r-2", 2.0))
        assert entry["status"] == OutcomeStatus.REJECTED.value
        assert entry["reason"] == DecisionRejectReason.INSUFFICIENT_BALANCE.value
        assert session.rejected_count == 1
        assert session.placed_count == 0
    _run(go())


def test_real_gating_ui_not_ready_is_rejected():
    async def go():
        session = _real_session(fast_settings())
        session.last_balance = 5000.0
        session.last_ui_ready = False    # betting panel not observable
        entry = await session.submit_decision(decision("r-3", 2.0))
        assert entry["status"] == OutcomeStatus.REJECTED.value
        assert entry["reason"] == DecisionRejectReason.UI_NOT_READY.value
    _run(go())


def test_real_gating_funded_and_ready_defers_not_places():
    async def go():
        session = _real_session(fast_settings())
        session.last_balance = 5000.0
        session.last_ui_ready = True
        entry = await session.submit_decision(decision("r-4", 2.0))
        assert entry["status"] == OutcomeStatus.DEFERRED.value
        assert entry["reason"] == DecisionRejectReason.PLACEMENT_DISABLED.value
        assert entry["simulated"] is False
        assert entry["mode"] == "REAL"
        assert session.deferred_count == 1
        assert session.placed_count == 0
        # Real backend never resolves anything in Part 1.
        assert await session.backend.collect_resolved() == []
    _run(go())


def test_real_gating_master_switch_on_still_defers_driver_not_implemented():
    async def go():
        session = _real_session(fast_settings(allow_real_placement=True))
        session.last_balance = 5000.0
        session.last_ui_ready = True
        assert session.backend.allow_real_placement is True
        entry = await session.submit_decision(decision("r-5", 2.0))
        assert entry["status"] == OutcomeStatus.DEFERRED.value
        assert (entry["reason"]
                == DecisionRejectReason.PLACEMENT_DRIVER_UNAVAILABLE.value)
        assert "Part 2" in (entry["note"] or "")
        assert session.placed_count == 0
    _run(go())


def test_real_backend_never_clicks_without_platform_receipt_verification():
    class ReadyBrowser:
        settings = SimpleNamespace(cdp_http="http://127.0.0.1:9222")

        def __init__(self):
            self.place_calls = 0

        async def readiness(self):
            return {"connected": True, "stage": "ready"}

        async def place_bet(self, **_kwargs):
            self.place_calls += 1

    async def go():
        browser = ReadyBrowser()
        backend = RealBrowserBackend(browser, allow_real_placement=True,
                                     execution_gate=lambda _intent: {"allowed": True})
        with pytest.raises(PlacementUnavailable) as exc:
            await backend.place(amount_bif=500, target_multiplier=2.0,
                                bet_slot=0, decision_id="no-click",
                                intent=SimpleNamespace())
        assert exc.value.reason == "platform_verification_incomplete"
        assert browser.place_calls == 0
        assert await backend.collect_resolved() == []

    _run(go())


def test_real_backend_passes_server_execution_id_to_final_gate():
    class ReadyBrowser:
        settings = SimpleNamespace(cdp_http="http://127.0.0.1:9222")

        async def readiness(self):
            return {"connected": True, "stage": "ready"}

    seen = {}

    async def gate(_intent, *, execution_id=None):
        seen["execution_id"] = execution_id
        return {"allowed": False, "reasons": ["test_block"]}

    async def go():
        backend = RealBrowserBackend(ReadyBrowser(), allow_real_placement=True,
                                     execution_gate=gate)
        with pytest.raises(PlacementUnavailable) as exc:
            await backend.place(amount_bif=500, target_multiplier=2.0,
                                bet_slot=0, decision_id="d-current",
                                intent=SimpleNamespace(), execution_id="server-execution")
        assert exc.value.reason == "final_safety_gate_blocked"
        assert seen["execution_id"] == "server-execution"

    _run(go())


def test_snapshot_missing_balance_is_unknown_not_zero():
    snap = GameSnapshot.from_value({"ok": True, "balanceText": ""})
    assert snap.balance is None
    assert snap.as_dict()["balance"] is None


# ── Schemas / events / profiles contracts ───────────────────────────────────

def test_decision_intent_schema_rejects_out_of_band_values():
    with pytest.raises(Exception):
        DecisionIntent(decision_id="d", profile="PROFILE_A",
                       target_multiplier=2.0, amount_bif=50)   # < min 100
    with pytest.raises(Exception):
        DecisionIntent(decision_id="d", profile="PROFILE_A",
                       target_multiplier=0.5)                  # < 1.01
    with pytest.raises(Exception):
        SessionStartRequest(action="start", mode="SIMULATION",
                            max_rounds=0)                      # < 1


def test_decision_effective_amount_uses_profile_default():
    d = decision("d-amt")                       # no amount given
    assert d.effective_amount() == PROFILES["PROFILE_A"].default_amount_bif
    d2 = DecisionIntent(decision_id="d2", profile="PROFILE_B",
                        target_multiplier=1.5, amount_bif=300)
    assert d2.effective_amount() == 300


def test_event_helpers_use_betting_prefix():
    assert betting_event("state", state="IDLE")["type"] == "betting:state"
    assert state_event("SIMULATION", "SIMULATION")["state"] == "SIMULATION"
    ev = ledger_event({"entry_id": "e1"}, action="resolved")
    assert ev["type"] == "betting:ledger"
    assert ev["action"] == "resolved"
    n = notice_event("stopped", "bye", level="warn")
    assert n["type"] == "betting:notice"
    assert n["level"] == "warn"


def test_profiles_contract():
    keys = profile_keys()
    assert keys == sorted(PROFILES)
    for key in keys:
        p = PROFILES[key]
        assert p.min_amount_bif <= p.default_amount_bif <= p.max_amount_bif
        assert p.max_loss_bif >= p.max_amount_bif


# ── REST surface (real FastAPI router, in-process async client) ─────────────

class ApiHarness:
    def __init__(self):
        self.manager = BettingManager(fast_settings())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._previous = None
        self._had_previous = False
        self._previous_decision_engine = None
        self._had_previous_decision_engine = False

    async def __aenter__(self):
        self._had_previous = hasattr(app.state, "betting")
        self._previous = getattr(app.state, "betting", None)
        app.state.betting = self.manager
        self._had_previous_decision_engine = hasattr(app.state, "decision_engine")
        self._previous_decision_engine = getattr(app.state, "decision_engine", None)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.manager.shutdown()
        await self.client.aclose()
        if self._had_previous:
            app.state.betting = self._previous
        elif hasattr(app.state, "betting"):
            delattr(app.state, "betting")
        if self._had_previous_decision_engine:
            app.state.decision_engine = self._previous_decision_engine
        elif hasattr(app.state, "decision_engine"):
            delattr(app.state, "decision_engine")

    def authorize(self, decision_id, round_id, profile="PROFILE_A", amount=500):
        record = {"status": "READY_FOR_EXECUTION", "decision_id": decision_id,
                  "target_round_id": round_id, "profile": profile,
                  "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                  "risk_evaluation": {"approved": True, "approved_bet": amount}}
        app.state.decision_engine = SimpleNamespace(current=lambda: record)

    async def get(self, path: str, **kw):
        return await self.client.get(path, **kw)

    async def post(self, path: str, **kw):
        return await self.client.post(path, **kw)


async def _api_call(fn):
    async with ApiHarness() as client:
        return await fn(client)


def test_api_status_idle_shape():
    async def go(client):
        return await client.get("/api/betting/status")
    r = _run(_api_call(go))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"]["state"] == "IDLE"
    assert body["status"]["session_id"] is None


def test_api_profiles_choices():
    async def go(client):
        return await client.get("/api/betting/profiles")
    r = _run(_api_call(go))
    assert r.status_code == 410
    assert r.json()["error"] == "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED"


def test_api_session_control_validation():
    async def go(client):
        first = await client.post("/api/betting/session", json={"action": "fly"})
        second = await client.post("/api/betting/session", json={})
        return first, second
    r, r2 = _run(_api_call(go))
    assert r.status_code == 422
    assert r2.status_code == 422


def test_api_session_control_rejects_direct_real_start():
    async def go(client):
        return await client.post("/api/betting/session", json={
            "action": "start", "mode": "REAL", "profile": "PROFILE_A",
        })
    r = _run(_api_call(go))
    assert r.status_code == 409
    assert r.json()["error"] == "FINANCIAL_EXECUTOR_RETIRED"
    assert "Real-money execution remains disabled" in r.json()["message"]


def test_api_decision_out_of_band_rejected_422():
    payload = {"decision_id": "d-422", "profile": "PROFILE_A",
               "target_multiplier": 2.0, "amount_bif": 50}
    async def go(client):
        return await client.post("/api/betting/decisions", json=payload)
    r = _run(_api_call(go))
    assert r.status_code == 409
    assert r.json()["decision"] == "NO_ACTION"
    assert r.json()["automatic_execution"] == "OFF"


def test_api_decision_while_idle_conflict_409():
    payload = {"decision_id": "d-idle", "round_id": "round-idle", "profile": "PROFILE_A",
               "target_multiplier": 2.0}
    async def go(client):
        client.authorize("d-idle", "round-idle")
        return await client.post("/api/betting/decisions", json=payload)
    r = _run(_api_call(go))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "rules_engine_not_configured"
    assert body["decision"] == "NO_ACTION"
    assert body["automatic_execution"] == "OFF"


def test_api_ledger_empty_when_no_session():
    async def go(client):
        return await client.get("/api/betting/ledger")
    r = _run(_api_call(go))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "entries": []}


def test_api_stop_without_session_conflict_409():
    async def go(client):
        return await client.post("/api/betting/session", json={"action": "stop"})
    r = _run(_api_call(go))
    assert r.status_code == 409
    assert r.json()["error"] == "session_not_running"


def test_api_session_start_is_retired_for_simulation_and_real_modes():
    async def go(client):
        r = await client.post("/api/betting/session", json={
            "action": "start", "mode": "SIMULATION",
        })
        assert r.status_code == 409
        assert r.json()["error"] == "FINANCIAL_EXECUTOR_RETIRED"

        r = await client.post("/api/betting/decisions", json={
            "decision_id": "d-api-1", "round_id": "round-api-1", "profile": "PROFILE_A",
            "target_multiplier": 2.0})
        assert r.status_code == 409
        assert r.json()["error"] == "rules_engine_not_configured"
        assert r.json()["decision"] == "NO_ACTION"
        assert r.json()["automatic_execution"] == "OFF"
        assert (await client.get("/api/betting/ledger")).json()["entries"] == []

        stopped = await client.post("/api/betting/session", json={"action": "stop"})
        assert stopped.status_code == 409
    _run(_api_call(go))


def test_api_repeated_decision_attempts_remain_blocked_without_rules():
    async def go(client):
        payload = {"decision_id": "d-api-dup", "round_id": "round-api-dup", "profile": "PROFILE_A",
                   "target_multiplier": 2.0}
        first = await client.post("/api/betting/decisions", json=payload)
        second = await client.post("/api/betting/decisions", json=payload)
        assert first.status_code == 409
        assert second.status_code == 409
        assert first.json()["error"] == second.json()["error"] == "rules_engine_not_configured"
        assert (await client.get("/api/betting/ledger")).json()["entries"] == []

        r = await client.post("/api/betting/session", json={"action": "stop"})
        assert r.status_code == 409
        assert r.json()["error"] == "session_not_running"
    _run(_api_call(go))


# ─── Authorized-decision and automatic-mode contract ─────────────────

def test_expired_decision_is_rejected_without_placement():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request())
        entry = await mgr.submit_decision(decision(
            "expired", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        assert entry["status"] == "rejected"
        assert entry["reason"] == "expired_decision"
        assert mgr.session.placed_count == 0
        await mgr.stop_session()
        await _wait_task(mgr.session._task)
    _run(go())


def test_execute_false_and_missing_round_are_rejected():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request())
        denied = await mgr.submit_decision(decision("no-exec", execute=False))
        missing = DecisionIntent(decision_id="no-round", profile="PROFILE_A",
                                 cashout=2.0)
        missing_entry = await mgr.submit_decision(missing)
        assert denied["reason"] == "execution_not_authorized"
        assert missing_entry["reason"] == "invalid_round"
        await mgr.stop_session()
        await _wait_task(mgr.session._task)
    _run(go())


def test_profile_cashout_must_match_active_profile():
    async def go():
        mgr = BettingManager(fast_settings())
        await mgr.start_session(sim_request())
        mismatch = await mgr.submit_decision(decision("bad-cashout", 1.5))
        assert mismatch["reason"] == "cashout_mismatch"
        assert mgr.session.placed_count == 0
        await mgr.stop_session()
        await _wait_task(mgr.session._task)
    _run(go())


def test_goal_balance_reached_stops_and_emits_event():
    async def go():
        events = []
        async def broadcast(event):
            events.append(event)
        mgr = BettingManager(fast_settings(), broadcaster=broadcast)
        req = sim_request(starting_balance=1000, goal_balance=1400)
        await mgr.start_session(req)
        session = mgr.session
        session.backend.crash_sequence = [3.10]
        await mgr.submit_decision(decision("goal", 2.0, amount_bif=500))
        await _wait_task(session._task)
        await asyncio.sleep(0)
        assert session.stop_reason == "goal_reached"
        assert session.wins == 1 and session.losses == 0
        assert any(e["type"] == "betting:goal_reached" for e in events)
    _run(go())


def test_emergency_stop_emits_event_and_stops():
    async def go():
        events = []
        async def broadcast(event):
            events.append(event)
        mgr = BettingManager(fast_settings(), broadcaster=broadcast)
        await mgr.start_session(sim_request())
        session = mgr.session
        await mgr.stop_session(emergency=True)
        await _wait_task(session._task)
        await asyncio.sleep(0)
        assert session.stop_reason == "emergency"
        assert any(e["type"] == "betting:emergency_stop" for e in events)
    _run(go())


def test_api_automatic_start_requires_explicit_live_activation():
    async def go(client):
        return await client.post("/api/betting/start", json={
            "starting_balance": 5000, "goal_balance": 10000,
            "profile": "profile_a",
        })
    response = _run(_api_call(go))
    assert response.status_code == 409
    assert response.json()["error"] == "FINANCIAL_EXECUTOR_RETIRED"


def test_named_realtime_events_cover_frontend_contract():
    async def go():
        events = []
        async def broadcast(event):
            events.append(event)
        mgr = BettingManager(fast_settings(), broadcaster=broadcast)
        await mgr.start_session(sim_request())
        await mgr.submit_decision(decision("events"))
        await asyncio.sleep(0)
        await mgr.stop_session()
        await _wait_task(mgr.session._task)
        await asyncio.sleep(0)
        names = {event["type"] for event in events}
        assert {"betting:started", "betting:waiting",
                "betting:decision_received", "betting:bet_placed",
                "betting:stopped"}.issubset(names)
    _run(go())


def test_browser_disconnect_stops_connected_real_session():
    class DisconnectedBackend:
        async def readiness(self):
            raise BrowserUnreachable("connection lost")

    async def go():
        session = _real_session(fast_settings())
        session.state = SessionState.CONNECTED
        session.backend = DisconnectedBackend()
        await session._real_observe_cycle()
        assert session._stop_requested is True
        assert session.stop_reason == "browser_disconnected"
        assert session.state == SessionState.STOPPING
    _run(go())


def test_unreachable_status_stops_connected_real_session():
    class DisconnectedBackend:
        async def readiness(self):
            return {"connected": False, "stage": "unreachable",
                    "error": "connection lost"}

    async def go():
        session = _real_session(fast_settings())
        session.state = SessionState.CONNECTED
        session.backend = DisconnectedBackend()
        await session._real_observe_cycle()
        assert session._stop_requested is True
        assert session.stop_reason == "browser_disconnected"

    _run(go())


def test_unknown_observed_balance_stops_real_session():
    class UnknownBalanceBackend:
        async def readiness(self):
            return {"connected": True, "stage": "ready"}

        async def snapshot(self):
            return {"ok": True, "balance": None, "balance_text": "",
                    "ui_ready": True, "payouts_head": []}

    async def go():
        session = _real_session(fast_settings())
        session.backend = UnknownBalanceBackend()
        await session._real_observe_cycle()
        assert session._stop_requested is True
        assert session.stop_reason == "balance_unverified"
        assert session.last_balance is None

    _run(go())
