from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.database.repository import Repository
from app.risk.engine import RiskManager
from app.services.rules_shadow_executor import execute_rules_selection_in_shadow
from app.shadow import ShadowManager, shadow_target_id


class Healthy:
    def can_bet_now(self, mode=None):
        return {"allowed": True, "reasons": []}


class Snapshot:
    def __init__(self, stake=100):
        stamp = datetime.now(timezone.utc).isoformat()
        self.ok = True
        self.page_role = "SHARED_AVIATOR"
        self.authentication_state = "AUTHENTICATED"
        self.browser_topology_verified = True
        self.balance_currency = "BIF"
        self.balance = 10000
        self.balance_observed_at = stamp
        self.cashout_controls = [{
            "slot": slot, "displayedValue": "2.00", "autoCashoutEnabled": True,
            "autoTabSelected": True, "autoBetEnabled": False, "inputVisible": True,
        } for slot in (0, 1)]
        self.bet_panel_states = [{
            "slot": slot, "pending_bet_state": "CLEAR", "button_visible": True,
            "button_text": f"Bet {stake} BIF", "currency_hint": "BIF",
            "bet_tab_active": False, "frame_identity": "shared-game-frame",
        } for slot in (0, 1)]
        self.stake_inputs = [{"slot": slot, "visible": True, "value": str(stake)}
                             for slot in (0, 1)]
        self.place_bet_buttons = [{"slot": slot, "visible": True} for slot in (0, 1)]
        self.betting_window = {"state": "UNKNOWN"}


class BrowserClient:
    stake = 100

    def __init__(self, _settings):
        self._snapshot = Snapshot(self.stake)

    async def snapshot(self):
        return self._snapshot

    async def close(self):
        return None


def _runtime(tmp_path, monkeypatch, *, observed_stake=100):
    repo = Repository(tmp_path / f"rules-shadow-{observed_stake}.sqlite3")
    repo.init()
    shadow = ShadowManager(repository=repo)
    config = {"mode": "MANUAL", "panels": [
        {"enabled": True, "stake": 100, "cashout": 2.0},
        {"enabled": False, "stake": None, "cashout": 2.0},
    ]}
    asyncio.run(shadow.start(profile="PROFILE_A", starting_balance=10000,
                             health=Healthy(), configuration=config))
    risk = RiskManager()
    shadow.status()
    decision_id = f"selection-{observed_stake}"
    session_id = "rules-session"
    selection = {
        "selection_id": decision_id,
        "source_round": {"streamId": "stream-1", "roundId": "source-10", "roundIndex": 10},
        "status": "PENDING",
    }
    rules_session = {"session_id": session_id, "status": "RUNNING",
                     "selection_status": "ACTIVE", "selections": [selection]}
    class Rules:
        def session(self, _session_id):
            return rules_session if _session_id == session_id else None
    mode = SimpleNamespace(mode="SHADOW_REALISTIC", live_authorized=False,
        shadow_ready=True, betting_status=shadow.betting_status)
    live = SimpleNamespace(manual_setup_readbacks=None, live_active=False)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repo), rules_engine=Rules(), betting_mode=mode,
        shadow=shadow, live=live, risk=risk, betting=SimpleNamespace(settings=object()),
    ))
    target_ref = {"streamId": "stream-1", "roundId": None, "roundIndex": 11}
    intent = {
        "decision_id": decision_id, "selection_id": decision_id, "session_id": session_id,
        "selected_panel": 0, "requested_stake": 100,
        "requested_stake_currency": "BIF", "cashout_target": 2.0,
        "source_round_id": "source-10", "target_stream_id": "stream-1",
        "target_round_index": 11, "target_round_ref": target_ref,
        "execution_status": "BLOCKED_READ_ONLY",
    }
    with repo.connect() as conn:
        conn.execute("""INSERT INTO rule_execution_intents(
            intent_id,decision_id,selection_id,session_id,source_round_id,target_stream_id,
            target_round_id,target_round_index,panel,requested_stake,requested_stake_currency,
            cashout_target,execution_status,created_at,expires_at,payload)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"intent-{observed_stake}", decision_id, decision_id, session_id, "source-10",
             "stream-1", None, 11, 0, 100, "BIF", 2.0, "BLOCKED_READ_ONLY",
             datetime.now(timezone.utc).isoformat(), None,
             json.dumps(intent, sort_keys=True, separators=(",", ":"))))
    BrowserClient.stake = observed_stake
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", BrowserClient)
    return app, shadow, repo, decision_id, intent


def test_prospective_rules_select_reaches_shadow_executor_and_reconciles_by_stream_index(
    tmp_path, monkeypatch,
):
    app, shadow, repo, decision_id, intent = _runtime(tmp_path, monkeypatch)

    async def run():
        result = await execute_rules_selection_in_shadow(app, decision_id, intent)
        assert result["status"] == "PAPER_AUTHORIZED"
        assert result["target_round_id"] == shadow_target_id("stream-1", 11)
        assert result["real_money_action_submitted"] is False
        assert (await execute_rules_selection_in_shadow(app, decision_id, intent))["already_recorded"] is True
        await shadow.reconcile_round({"stream_id": "stream-1", "round_index": 11,
                                      "round_id": "provider-round-11", "multiplier": 2.2})

    asyncio.run(run())
    trade = repo.list_shadow_trades()[0]
    saved = repo.rule_execution_intent(decision_id)["shadow_execution"]
    assert trade["target_identity_status"] == "CANONICAL_STREAM_INDEX"
    assert trade["status"] == "PAPER_RECONCILED"
    assert saved["status"] == "PAPER_RECONCILED"
    assert saved["settlement"] == "SHADOW_SIMULATED"
    assert repo.list_executions() == []


def test_shadow_executor_requires_operator_to_correct_live_stake_mismatch(tmp_path, monkeypatch):
    app, shadow, repo, decision_id, intent = _runtime(tmp_path, monkeypatch, observed_stake=200)

    result = asyncio.run(execute_rules_selection_in_shadow(app, decision_id, intent))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "STAKE_MISMATCH"
    assert result["setup"]["expected_stake"] == 100
    assert result["setup"]["observed_stake"] == 200
    assert not repo.list_shadow_trades()
