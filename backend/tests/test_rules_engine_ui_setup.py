import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.api import rules_engine as api
from app.database.repository import Repository
from app.rules_engine import RulesEngine


BOUNDARY = {
    "event_type": "round.persisted", "durability": "COMMITTED",
    "continuity_status": "VERIFIED", "stream_id": "stream-1",
    "stream_sequence": 5, "round_id": "round-5", "round_index": 5,
    "multiplier": 2.3,
}


def configured_engine():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    return engine


def test_rules_engine_setup_is_operator_owned_and_not_required():
    result = configured_engine().ui_setup_configuration()
    assert result == {
        "status": "NOT_REQUIRED",
        "reason": "EXECUTOR_CONFIGURATION_IS_OPERATOR_OWNED",
        "session_id": None,
        "automatic_execution": "OFF",
    }


@pytest.mark.parametrize("options", [
    {"profit_target_rwf": 100},
    {"loss_limit_rwf": 100},
    {"maximum_exposure_percentage": 5},
    {"risk_profile": "PROFILE_A"},
    {"fixed_stake_rwf": 100},
    {"auto_stake": True},
    {"execution_panel": 0},
    {"cashout_target": 2.0},
])
def test_new_rule_sessions_reject_bankroll_and_platform_configuration(options):
    engine = configured_engine()
    with pytest.raises(ValueError, match="OPTIONAL_BANKROLL_MANAGEMENT_REMOVED|EXECUTOR_CONFIGURATION_MUST_NOT_BE_STORED_IN_RULE_SESSIONS"):
        engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
                             boundary=BOUNDARY, options=options)


def test_new_rule_session_persists_only_observation_configuration():
    engine = configured_engine()
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=BOUNDARY, options={"window_size": 25, "max_selections": 3},
    )
    config = session["configuration_snapshot"]
    assert config["window_size"] == 25
    assert config["max_selections"] == 3
    assert session["automatic_execution"] == "OFF"
    assert not ({"fixed_stake_rwf", "stake_rwf", "requested_stake", "execution_panel",
                 "cashout_target", "profit_target_rwf", "loss_limit_rwf"} & set(config))


def test_ui_setup_compatibility_endpoint_does_not_read_or_change_aviator():
    engine = configured_engine()
    app = FastAPI()
    app.state.rules_engine = engine
    request = Request({
        "type": "http", "method": "GET", "path": "/api/rules-engine/ui-setup",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })
    result = asyncio.run(api.ui_setup(request))
    assert result["status"] == "NOT_REQUIRED"
    assert result["automatic_execution"] == "OFF"
    assert engine.execution_authorized() is False


def test_historical_rule_session_api_hides_retired_bankroll_configuration():
    engine = configured_engine()
    engine.sessions["historic"] = {
        "session_id": "historic", "mode": "ALL_RULES_TOGETHER", "status": "COMPLETED",
        "configuration_snapshot": {"window_size": 50, "fixed_stake_rwf": 100,
                                    "profit_target_rwf": 400, "execution_panel": 0},
        "financial_stop": {"status": "STOPPED", "realized_betting_pl": 300,
                           "profit_target": 400},
        "selections": [{"selection_id": "selection-1", "execution_intent": {
            "intent_id": "intent-1", "requested_stake": 100,
            "cashout_target": 2.0, "selected_panel": 0,
            "target_round_index": 6,
        }}],
        "rule_selections": {},
    }
    app = FastAPI()
    app.state.rules_engine = engine
    request = Request({
        "type": "http", "method": "GET", "path": "/api/rules-engine/sessions/historic",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })

    result = api.session_detail(request, "historic")

    assert result["configuration_snapshot"] == {"window_size": 50}
    assert "financial_stop" not in result
    assert result["settlement_integrity"]["status"] == "STOPPED"
    intent = result["selections"][0]["execution_intent"]
    assert intent == {"intent_id": "intent-1", "target_round_index": 6}
    assert "profit_target" not in str(result)


def test_sqlite_intent_migration_preserves_old_rows_and_allows_no_cashout(tmp_path):
    database = tmp_path / "legacy-intents.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE rule_execution_intents (
            intent_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE,
            selection_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
            source_round_id TEXT, target_stream_id TEXT NOT NULL,
            target_round_id TEXT, target_round_index BIGINT NOT NULL,
            panel INTEGER NOT NULL DEFAULT 0, requested_stake NUMERIC,
            requested_stake_currency TEXT, cashout_target NUMERIC NOT NULL,
            execution_status TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT, payload TEXT NOT NULL,
            UNIQUE(target_stream_id,target_round_index,panel))""")
        connection.execute("""INSERT INTO rule_execution_intents VALUES(
            'old-intent','old-decision','old-selection','old-session','source','stream',
            'target',2,0,100,'BIF',2.0,'BLOCKED_READ_ONLY','2026-10-14T00:00:00Z',
            NULL,'{"legacy":true}')""")

    repository = Repository(database)
    repository.init()

    with repository.connect() as connection:
        columns = {row[1]: row for row in connection.execute(
            "PRAGMA table_info(rule_execution_intents)").fetchall()}
        preserved = connection.execute("SELECT payload FROM rule_execution_intents WHERE intent_id='old-intent'").fetchone()
        connection.execute("""INSERT INTO rule_execution_intents(
            intent_id,decision_id,selection_id,session_id,target_stream_id,target_round_index,
            panel,cashout_target,execution_status,created_at,payload)
            VALUES('new-intent','new-decision','new-selection','new-session','stream',3,-1,NULL,
                   'BLOCKED_READ_ONLY','2026-10-15T00:00:00Z','{}')""")
        new_cashout = connection.execute(
            "SELECT cashout_target FROM rule_execution_intents WHERE intent_id='new-intent'").fetchone()

    assert columns["cashout_target"][3] == 0
    assert preserved["payload"] == '{"legacy":true}'
    assert new_cashout["cashout_target"] is None
