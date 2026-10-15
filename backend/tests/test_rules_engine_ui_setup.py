import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api import rules_engine as api
from app.database.repository import Repository
from app.rules_engine import RulesEngine


def active_engine(currency="RWF", panel=1):
    engine = RulesEngine()
    engine.sessions["session-1"] = {
        "session_id": "session-1", "status": "RUNNING",
        "configuration_snapshot": {
            "fixed_stake_rwf": 100, "stake_currency": currency,
            "execution_panel": panel,
        },
    }
    return engine


def test_ui_setup_returns_single_panel_configuration_without_any_true_rule():
    result = active_engine().ui_setup_configuration()
    assert result["status"] == "CONFIGURED"
    assert result["execution_panel"] == 1
    assert result["requested_stake"] == 100
    assert result["requested_stake_currency"] == "RWF"
    assert result["automatic_execution"] == "OFF"


def test_ui_setup_blocks_invalid_selected_panel():
    result = active_engine(panel=2).ui_setup_configuration()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "SESSION_EXECUTION_PANEL_INVALID"


def test_ui_setup_blocks_legacy_session_without_an_explicit_panel():
    result = active_engine(panel=None).ui_setup_configuration()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "SESSION_EXECUTION_PANEL_NOT_SELECTED"


def test_legacy_rwf_named_stake_keeps_its_currency_evidence():
    engine = active_engine(currency=None, panel=None)
    result = engine.ui_setup_configuration()
    assert result["status"] == "BLOCKED"
    assert result["requested_stake"] == 100
    assert result["requested_stake_currency"] == "RWF"
    assert result["reason"] == "SESSION_EXECUTION_PANEL_NOT_SELECTED"


def test_session_start_preserves_selected_single_panel():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary={"event_type": "round.persisted", "durability": "COMMITTED",
                  "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                  "round_id": "r-5", "round_index": 5, "multiplier": 2.3},
        options={"fixed_stake_rwf": 100, "stake_currency": "RWF", "execution_panel": 1},
    )
    assert session["configuration_snapshot"]["execution_panel"] == 1


def test_session_start_persists_a_fixed_bif_ui_stake_separately_from_shadow_rwf():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary={"event_type": "round.persisted", "durability": "COMMITTED",
                  "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                  "round_id": "r-5", "round_index": 5, "multiplier": 2.3},
        options={"fixed_stake_rwf": 100, "stake_currency": "RWF", "execution_panel": 1,
                 "ui_fixed_stake_bif": 200, "ui_stake_currency": "BIF"},
    )
    config = session["configuration_snapshot"]
    assert config["fixed_stake_rwf"] == 100
    assert config["stake_currency"] == "RWF"
    assert config["ui_fixed_stake_bif"] == 200
    assert config["ui_stake_currency"] == "BIF"
    assert engine.ui_setup_configuration()["requested_stake"] == 200
    assert engine.ui_setup_configuration()["requested_stake_currency"] == "BIF"
    assert engine.ui_setup_configuration()["execution_panel"] == 1


def test_fixed_bif_stake_and_selected_panel_survive_rules_engine_restart(tmp_path):
    repository = Repository(tmp_path / "ui-fixed-stake.sqlite3")
    repository.init()
    engine = RulesEngine(persistence=repository)
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary={"event_type": "round.persisted", "durability": "COMMITTED",
                  "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                  "round_id": "r-5", "round_index": 5, "multiplier": 2.3},
        options={"fixed_stake_rwf": 100, "stake_currency": "RWF", "execution_panel": 0,
                 "ui_fixed_stake_bif": 350, "ui_stake_currency": "BIF"},
    )

    restored = RulesEngine(persistence=repository)
    setup = restored.ui_setup_configuration()
    assert setup["status"] == "CONFIGURED"
    assert setup["execution_panel"] == 0
    assert setup["requested_stake"] == 350
    assert setup["requested_stake_currency"] == "BIF"


def test_new_ui_panel_selection_does_not_fall_back_to_simulated_rwf_stake():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary={"event_type": "round.persisted", "durability": "COMMITTED",
                  "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                  "round_id": "r-5", "round_index": 5, "multiplier": 2.3},
        options={"fixed_stake_rwf": 100, "stake_currency": "RWF", "execution_panel": 1},
    )
    result = engine.ui_setup_configuration()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "FIXED_UI_STAKE_BIF_NOT_CONFIGURED"
    assert result["requested_stake"] is None


def test_ui_fixed_stake_rejects_rwf_currency_and_missing_panel():
    boundary = {"event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                "round_id": "r-5", "round_index": 5, "multiplier": 2.3}
    for options, expected in [
        ({"fixed_stake_rwf": 100, "stake_currency": "RWF", "execution_panel": 1,
          "ui_fixed_stake_bif": 200, "ui_stake_currency": "RWF"}, "UI_FIXED_STAKE_CURRENCY_MUST_BE_BIF"),
        ({"fixed_stake_rwf": 100, "stake_currency": "RWF", "ui_fixed_stake_bif": 200},
         "UI_FIXED_STAKE_REQUIRES_SELECTED_PANEL"),
    ]:
        engine = RulesEngine()
        engine.configure("RECENT_5_H_GE_4", enabled=True)
        try:
            engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
                                 boundary=boundary, options=options)
        except ValueError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"expected {expected}")


def test_shadow_session_can_start_without_an_execution_panel():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary={"event_type": "round.persisted", "durability": "COMMITTED",
                  "continuity_status": "VERIFIED", "stream_id": "s", "stream_sequence": 5,
                  "round_id": "r-5", "round_index": 5, "multiplier": 2.3},
        options={"fixed_stake_rwf": 100, "stake_currency": "RWF"},
    )
    assert session["status"] == "RUNNING"
    assert session["configuration_snapshot"]["execution_panel"] is None
    assert session["automatic_execution"] == "OFF"


def test_shadow_boundary_freshness_rejects_stale_feed_and_accepts_current_commit():
    now = datetime.now(timezone.utc)
    fresh = {"observed_at": (now - timedelta(seconds=2)).isoformat()}
    stale = {"observed_at": (now - timedelta(seconds=121)).isoformat()}
    api._require_fresh_shadow_boundary(fresh, now=now)
    try:
        api._require_fresh_shadow_boundary(stale, now=now)
    except ValueError as exc:
        assert str(exc) == "SHADOW_ROUND_FEED_STALE"
    else:
        raise AssertionError("stale committed history must not start a prospective session")


def test_api_ui_setup_blocks_rwf_stake_on_bif_platform(monkeypatch):
    class FakeSnapshot:
        ok = True
        balance_currency = "BIF"
        bet_panel_states = [
            {"slot": 0, "panel_id": "PANEL_1", "state": "BET_AVAILABLE", "pending_bet_state": "CLEAR"},
            {"slot": 1, "panel_id": "PANEL_2", "state": "BET_AVAILABLE", "pending_bet_state": "CLEAR"},
        ]

    class FakeClient:
        def __init__(self, settings):
            pass

        async def readiness(self):
            return {"authentication_state": "AUTHENTICATED", "page_role": "SHARED_AVIATOR",
                    "browser_topology_verified": True}

        async def snapshot(self):
            return FakeSnapshot()

        async def close(self):
            pass

    monkeypatch.setattr(api, "AviatorBrowserClient", FakeClient, raising=False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        rules_engine=active_engine(), betting=SimpleNamespace(settings=object()),
    )))
    result = asyncio.run(api.ui_setup(request))
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "STAKE_CURRENCY_MISMATCH"
    assert result["platform_currency"] == "BIF"
    assert result["requested_stake_currency"] == "RWF"
    assert result["automatic_execution"] == "OFF"
