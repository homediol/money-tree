from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.database.repository import Repository
from app.shadow import ShadowManager


class Health:
    def can_bet_now(self, mode=None):
        return {"allowed": True, "reasons": []}


def test_shadow_start_is_blocked_when_health_is_unsafe(tmp_path):
    repo = Repository(tmp_path / "shadow.sqlite3"); repo.init()
    shadow = ShadowManager(repository=repo)
    try:
        asyncio.run(shadow.start(health=SimpleNamespace(can_bet_now=lambda mode=None: {"allowed": False, "reasons": ["history stale"]})))
    except ValueError as exc:
        assert "SHADOW START BLOCKED" in str(exc)
    else:
        raise AssertionError("unsafe shadow startup was accepted")


def test_two_panel_manual_uses_combined_risk_budget_and_settles_independently(tmp_path):
    repo = Repository(tmp_path / "two-panel-shadow.sqlite3"); repo.init()
    shadow = ShadowManager(repository=repo)
    config = {"mode": "MANUAL", "maximum_combined_exposure": 1000,
              "panels": [{"enabled": True, "stake": 100, "cashout": 2.0},
                         {"enabled": True, "stake": 100, "cashout": 1.5}]}
    decision = {"decision_id": "manual-d1", "target_round_id": "round-2",
                "profile": "PROFILE_A", "status": "READY_FOR_EXECUTION",
                "risk_evaluation": {"approved": True, "status": "APPROVED",
                    "profile": "PROFILE_A", "approved_bet": 200, "cashout": 2.0}}

    async def run():
        await shadow.start(profile="PROFILE_A", starting_balance=5000, goal_balance=6000,
                           health=Health(), configuration=config)
        placed = await shadow.submit_decision(decision)
        assert placed["accepted"] is True
        assert [trade["panel"] for trade in placed["trades"]] == [1, 2]
        assert [trade["bet_amount"] for trade in placed["trades"]] == [100, 100]
        assert (await shadow.submit_decision(decision))["reason"] == "duplicate_shadow_decision"
        await shadow.reconcile_round({"round_id": "round-2", "multiplier": 1.75})
        trades = sorted(repo.list_shadow_trades(), key=lambda row: row["panel"])
        assert [trade["result"] for trade in trades] == ["LOSS", "WIN"]
        assert [trade["pnl"] for trade in trades] == [-100, 50]
        assert shadow.status()["profit"] == -50
        assert shadow.status()["panel_summary"]["1"]["pnl"] == -100
        assert shadow.status()["panel_summary"]["2"]["pnl"] == 50
        assert repo.list_executions() == []

    asyncio.run(run())


def test_manual_shadow_rejects_exposure_above_decision_approval(tmp_path):
    repo = Repository(tmp_path / "panel-risk.sqlite3"); repo.init()
    shadow = ShadowManager(repository=repo)
    config = {"mode": "MANUAL", "maximum_combined_exposure": 1000,
              "panels": [{"enabled": True, "stake": 300, "cashout": 2.0},
                         {"enabled": True, "stake": 300, "cashout": 2.0}]}
    decision = {"decision_id": "manual-d2", "target_round_id": "round-2",
                "profile": "PROFILE_A", "status": "READY_FOR_EXECUTION",
                "risk_evaluation": {"approved": True, "status": "APPROVED",
                    "profile": "PROFILE_A", "approved_bet": 500, "cashout": 2.0}}

    async def run():
        await shadow.start(starting_balance=20000, goal_balance=30000,
                           health=Health(), configuration=config)
        result = await shadow.submit_decision(decision)
        assert result["accepted"] is False
        assert result["reason"] == "combined_panel_exposure_exceeds_risk_approval"
        assert not repo.list_shadow_trades()

    asyncio.run(run())


def test_restart_recovers_pending_panel_as_unknown_and_requires_exact_round(tmp_path):
    repo = Repository(tmp_path / "shadow-recovery.sqlite3"); repo.init()
    session = {"session_id": "SH-restart", "started_at": "now", "profile": "PROFILE_A",
        "starting_balance": 5000, "current_balance": 5000, "goal_balance": 6000,
        "bets": 0, "wins": 0, "losses": 0, "unknown": 0, "staked": 0,
        "payout": 0, "peak": 5000, "drawdown": 0, "losing_streak": 0,
        "longest_losing_streak": 0, "risk_blocks": 0, "total_exposure": 100,
        "panel_summary": {"1": ShadowManager._empty_panel_summary(), "2": ShadowManager._empty_panel_summary()},
        "config": ShadowManager._validate_configuration({"mode": "MANUAL", "panels": [
            {"enabled": True, "stake": 100, "cashout": 2.0},
            {"enabled": False, "stake": None, "cashout": 2.0}],
        }, "PROFILE_A", 5000, 6000)}
    repo.save_shadow_session(session)
    repo.save_shadow_trade({"execution_id": "P-pending", "session_id": "SH-restart",
        "decision_id": "d-restart", "target_round_id": "round-9", "panel": 1,
        "bet_amount": 100, "cashout_target": 2.0, "balance_before": 5000,
        "status": "PAPER_AUTHORIZED"})

    async def run():
        recovered = ShadowManager(repository=repo)
        assert recovered.mode == "PAUSED"
        assert recovered.status()["pending_rounds"] == ["round-9"]
        assert recovered.status()["verified_real_balance"] is None
        assert recovered.betting_status()["current_verified_real_balance"] is None
        trade = repo.list_shadow_trades(session_id="SH-restart")[0]
        assert trade["status"] == "UNKNOWN"
        try:
            await recovered.start(health=Health(), starting_balance=5000, goal_balance=6000)
        except ValueError as exc:
            assert "pending panel results" in str(exc)
        else:
            raise AssertionError("restart resumed while a panel result was unresolved")
        await recovered.reconcile_round({"round_id": "round-9", "multiplier": 2.2})

    asyncio.run(run())
    assert repo.list_shadow_trades(session_id="SH-restart")[0]["status"] == "PAPER_RECONCILED"
