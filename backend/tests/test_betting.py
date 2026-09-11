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

import httpx
import pytest

from app.betting.browser_adapters import PlacementUnavailable, SimulationBackend
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
                          profile=kw.pop("profile", "PROFILE_A"),
                          target_multiplier=target_multiplier, **kw)


def _run(coro):
    return asyncio.run(coro)


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
        # Target 1.05 < 1.21 → round-0 crash cashes out for +25 BIF.
        entry = await mgr.submit_decision(decision("d-win", 1.05))
        assert entry["status"] == OutcomeStatus.PLACED.value
        await _wait_task(session._task)
        st = session.status()
        assert st["state"] == SessionState.STOPPED.value
        assert st["stop_reason"] == "max_rounds"
        assert st["running_pnl_bif"] == 25.0
        assert st["resolved_decisions"] == 1
        (e,) = session.ledger_slice()
        assert e["pnl_bif"] == 25.0
        assert e["note"] == "won"
    _run(go())


def test_auto_stop_target_profit():
    async def go():
        mgr = BettingManager(fast_settings())
        # +25 profit crosses the +20 goal on the first resolution.
        await mgr.start_session(sim_request(target_profit_bif=20))
        session = mgr.session
        await mgr.submit_decision(decision("d-profit", 1.05))
        await _wait_task(session._task)
        assert session.state == SessionState.STOPPED
        assert session.stop_reason == "target_profit"
        assert session.status()["running_pnl_bif"] == 25.0
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

    async def __aenter__(self):
        self._had_previous = hasattr(app.state, "betting")
        self._previous = getattr(app.state, "betting", None)
        app.state.betting = self.manager
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.manager.shutdown()
        await self.client.aclose()
        if self._had_previous:
            app.state.betting = self._previous
        elif hasattr(app.state, "betting"):
            delattr(app.state, "betting")

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
    assert r.status_code == 200
    body = r.json()
    assert set(body["keys"]) == {"PROFILE_A", "PROFILE_B"}
    assert body["profiles"]["PROFILE_A"]["default_amount_bif"] == 500


def test_api_session_control_validation():
    async def go(client):
        first = await client.post("/api/betting/session", json={"action": "fly"})
        second = await client.post("/api/betting/session", json={})
        return first, second
    r, r2 = _run(_api_call(go))
    assert r.status_code == 422
    assert r2.status_code == 422


def test_api_decision_out_of_band_rejected_422():
    payload = {"decision_id": "d-422", "profile": "PROFILE_A",
               "target_multiplier": 2.0, "amount_bif": 50}
    async def go(client):
        return await client.post("/api/betting/decisions", json=payload)
    r = _run(_api_call(go))
    assert r.status_code == 422


def test_api_decision_while_idle_conflict_409():
    payload = {"decision_id": "d-idle", "profile": "PROFILE_A",
               "target_multiplier": 2.0}
    async def go(client):
        return await client.post("/api/betting/decisions", json=payload)
    r = _run(_api_call(go))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "session_not_running"


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


def test_api_sim_e2e_start_decide_resolve_stop():
    async def go(client):
        r = await client.post("/api/betting/session", json={
            "action": "start", "mode": "SIMULATION", "profile": "PROFILE_A",
            "max_rounds": 1})
        assert r.status_code == 200
        started = r.json()["status"]
        assert started["mode"] == "SIMULATION"
        assert started["simulated"] is True

        r = await client.post("/api/betting/decisions", json={
            "decision_id": "d-api-1", "profile": "PROFILE_A",
            "target_multiplier": 2.0})
        assert r.status_code == 200
        entry = r.json()["entry"]
        assert entry["status"] == "placed"
        assert entry["simulated"] is True

        # Poll until the scripted round resolves and the session auto-stops.
        for _ in range(400):
            entries = (await client.get("/api/betting/ledger")).json()["entries"]
            if entries and entries[0]["status"] == "resolved":
                break
            await asyncio.sleep(0.02)
        assert entries[0]["crash_point"] == MIN_CRASH_1
        assert entries[0]["pnl_bif"] == -500
        assert entries[0]["note"] == "lost"

        for _ in range(100):
            st = (await client.get("/api/betting/status")).json()["status"]
            if st["state"] in ("STOPPED", "ERROR"):
                break
            await asyncio.sleep(0.02)
        assert st["state"] == "STOPPED"
        assert st["stop_reason"] == "max_rounds"
        assert st["running_pnl_bif"] == -500
    _run(_api_call(go))


def test_api_duplicate_decision_conflict_409():
    async def go(client):
        await client.post("/api/betting/session", json={
            "action": "start", "mode": "SIMULATION", "profile": "PROFILE_A"})

        payload = {"decision_id": "d-api-dup", "profile": "PROFILE_A",
                   "target_multiplier": 2.0}
        assert (await client.post("/api/betting/decisions", json=payload)).status_code == 200
        r = await client.post("/api/betting/decisions", json=payload)
        assert r.status_code == 409
        assert r.json()["error"] == "duplicate_decision"

        r = await client.post("/api/betting/session", json={"action": "stop"})
        assert r.status_code == 200
        assert r.json()["status"]["state"] in ("STOPPING", "STOPPED")
        await _wait_task(client.manager.session._task)
    _run(_api_call(go))
