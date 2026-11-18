from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database.repository import Repository
from app.shadow import ShadowManager


class Health:
    def can_shadow(self):
        return {"allowed": True, "reasons": []}


def ready_decision(decision_id="decision-1", target="round-2", **changes):
    row = {"decision_id": decision_id, "target_round_id": target,
           "status": "READY_FOR_EXECUTION",
           "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
           "cashout_target": 2.0}
    row.update(changes)
    return row


def test_shadow_start_is_blocked_when_health_is_unsafe(tmp_path):
    repo = Repository(tmp_path / "shadow.sqlite3")
    repo.init()
    shadow = ShadowManager(repository=repo)
    try:
        asyncio.run(shadow.start(health=SimpleNamespace(
            can_shadow=lambda: {"allowed": False, "reasons": ["history stale"]})))
    except ValueError as exc:
        assert "SHADOW START BLOCKED" in str(exc)
    else:
        raise AssertionError("unsafe shadow startup was accepted")


def test_shadow_uses_one_fixed_stake_and_cashout_and_rejects_duplicate(tmp_path):
    repo = Repository(tmp_path / "fixed-shadow.sqlite3")
    repo.init()
    shadow = ShadowManager(repository=repo)

    async def run():
        await shadow.start(starting_balance=5000, goal_balance=6000, health=Health(),
                           configuration={"mode": "MANUAL", "execution_panel": 0,
                                           "fixed_stake_rwf": 125, "stake_currency": "RWF"})
        decision = ready_decision()
        placed = await shadow.submit_decision(decision)
        assert placed["accepted"] is True
        assert len(placed["trades"]) == 1
        assert placed["trades"][0]["bet_amount"] == 125
        assert placed["trades"][0]["cashout_target"] == 2.0
        assert (await shadow.submit_decision(decision))["reason"] == "duplicate_shadow_decision"
        assert (await shadow.submit_decision(ready_decision(
            "decision-2", "round-3", requested_stake=250))) ["reason"] == "fixed_stake_configuration_mismatch"
        await shadow.reconcile_round({"round_id": "round-2", "multiplier": 1.75})
        trades = repo.list_shadow_trades()
        assert len(trades) == 1
        assert trades[0]["result"] == "LOSS"
        assert trades[0]["pnl"] == -125

    asyncio.run(run())


def test_shadow_rejects_expired_decision(tmp_path):
    repo = Repository(tmp_path / "expired-shadow.sqlite3")
    repo.init()
    shadow = ShadowManager(repository=repo)

    async def run():
        await shadow.start(starting_balance=5000, health=Health(),
                           configuration={"mode": "MANUAL", "execution_panel": 0,
                                           "fixed_stake_rwf": 100, "stake_currency": "RWF"})
        result = await shadow.submit_decision(ready_decision(
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        assert result == {"accepted": False, "reason": "decision_expired"}

    asyncio.run(run())


def test_restart_recovers_pending_shadow_result_and_requires_exact_round(tmp_path):
    repo = Repository(tmp_path / "shadow-recovery.sqlite3")
    repo.init()
    session = {"session_id": "SH-restart", "started_at": "now",
        "starting_balance": 5000, "current_balance": 5000, "goal_balance": 6000,
        "bets": 0, "wins": 0, "losses": 0, "unknown": 0, "staked": 0,
        "payout": 0,
        "panel_summary": {"1": ShadowManager._empty_panel_summary(),
                          "2": ShadowManager._empty_panel_summary()},
        "config": ShadowManager._validate_configuration(
            {"mode": "MANUAL", "execution_panel": 0, "fixed_stake_rwf": 100,
             "stake_currency": "RWF"}, 5000, 6000)}
    repo.save_shadow_session(session)
    repo.save_shadow_trade({"execution_id": "P-pending", "session_id": "SH-restart",
        "decision_id": "d-restart", "target_round_id": "round-9", "panel": 1,
        "bet_amount": 100, "cashout_target": 2.0, "balance_before": 5000,
        "status": "PAPER_AUTHORIZED"})

    async def run():
        recovered = ShadowManager(repository=repo)
        assert recovered.mode == "PAUSED"
        assert recovered.status()["pending_rounds"] == ["round-9"]
        status = recovered.status()
        assert status["production_isolated"] is True
        # SHADOW has no live betting balance contract; this paper-only status
        # deliberately does not expose a fabricated verified real balance.
        assert "verified_real_balance" not in status
        trade = repo.list_shadow_trades(session_id="SH-restart")[0]
        assert trade["status"] == "UNKNOWN"
        try:
            await recovered.start(health=Health(), starting_balance=5000,
                                 goal_balance=6000)
        except ValueError as exc:
            assert "pending panel results" in str(exc)
        else:
            raise AssertionError("restart resumed while a result was unresolved")
        await recovered.reconcile_round({"round_id": "other-round", "multiplier": 2.2})
        assert repo.list_shadow_trades(session_id="SH-restart")[0]["status"] == "UNKNOWN"
        await recovered.reconcile_round({"round_id": "round-9", "multiplier": 2.2})

    asyncio.run(run())
    assert repo.list_shadow_trades(session_id="SH-restart")[0]["status"] == "PAPER_RECONCILED"
