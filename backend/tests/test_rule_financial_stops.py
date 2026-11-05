from __future__ import annotations

import pytest

from app.rules_engine import RulesEngine


def boundary():
    return {"stream_id": "stream-1", "round_id": "round-1", "observation_id": "round-1",
            "round_index": 1, "stream_sequence": 1, "multiplier": 2.0,
            "event_type": "round.persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED"}


def test_removed_loss_limit_settings_are_ignored_and_never_persisted():
    engine = RulesEngine()
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary(),
        options={"fixed_stake_rwf": 175, "loss_limit_rwf": 25,
                 "reconciled_loss_limit_enabled": True,
                 "reconciled_loss_limit_rwf": 50},
    )
    config = session["configuration_snapshot"]
    assert config["fixed_stake_rwf"] == 175
    assert config["loss_limit_enabled"] is False
    assert config["loss_limit_count"] is None
    assert "reconciled_loss_limit_enabled" not in config
    assert session.get("financial_stop") is None


def test_rule_configuration_rejects_removed_loss_controls():
    engine = RulesEngine()
    with pytest.raises(ValueError, match="UNSUPPORTED_RULE_PARAMETER"):
        engine.configure("USER_PLAYING_POLICY_START_CHECK", enabled=True,
                         parameters={"loss_limit_enabled": True})


def test_reconciled_results_are_observations_and_do_not_change_rule_state():
    engine = RulesEngine()
    session = {"status": "RUNNING", "selection_status": "ACTIVE",
               "configuration_snapshot": {}, "selections": [{
                   "selection_id": "selection-1", "contributing_rule_ids": ["RULE_A"],
               }]}
    engine._apply_reconciled_financials(session,
        {"execution_id": "execution-1", "decision_id": "selection-1",
         "result": "LOSS", "profit_loss": -100},
        {"realized_betting_pl": -100, "currency": "RWF", "win_count": 0,
         "loss_count": 1, "reconciled_bets": 1}, "2026-10-16T00:00:00+00:00")

    assert session["status"] == "RUNNING"
    assert session["selection_status"] == "ACTIVE"
    assert session.get("financial_stop") is None
    assert session["reconciled_financials"]["loss_count"] == 1
    assert session["selections"][0]["financial_result"]["profit_loss"] == -100


def test_unresolved_execution_keeps_manual_reconciliation_pause():
    class Persistence:
        open = True

        def load_application_state(self, _key):
            return None

        def mark_rule_execution_pending(self, _execution, _reason):
            return {"session_id": "session-1"}

        def has_open_rule_session_executions(self, _session_id):
            return self.open

        def save_rules_engine_snapshot(self, *_args):
            pass

    repository = Persistence()
    engine = RulesEngine(repository)
    engine.sessions["session-1"] = {"session_id": "session-1", "mode": "ALL_RULES_TOGETHER",
                                    "status": "RUNNING",
                                    "selection_status": "ACTIVE", "selections": []}
    result = engine.block_unknown_execution_reconciliation(
        {"execution_id": "execution-1", "decision_id": "decision-1"},
        "settlement_receipt_missing")
    assert result["ok"] is True
    assert engine.session("session-1")["status"] == "PAUSED"
    with pytest.raises(ValueError, match="UNRECONCILED_EXECUTION_REMAINS"):
        engine.session_action("session-1", "resume")

    repository.open = False
    resumed = engine.session_action("session-1", "resume")
    assert resumed["status"] == "RUNNING"
    assert resumed["selection_status"] == "ACTIVE"
