from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.database.repository import Repository
from app.shadow import ShadowManager


class Health:
    def can_bet_now(self, mode=None):
        return {"allowed": True, "reasons": []}


class LiveExecutor:
    def __init__(self): self.called = False
    async def submit_decision(self, *_args, **_kwargs): self.called = True


def test_shadow_pipeline_is_paper_only_and_reconciles(tmp_path):
    repo = Repository(tmp_path / "shadow.sqlite3"); repo.init()
    live = LiveExecutor()
    prediction = {"prediction_id": "p1", "source_round_id": "1", "probability_2x": .9,
                  "model_version": "m1", "feature_version": "f1", "fresh": True}
    evidence = {"evidence_id": "e1", "prediction_id": "p1", "model_version": "m1",
                "feature_version": "f1", "confidence": "HIGH", "evidence_strength": "STRONG",
                "pattern": {"sample_size": 30}, "data_quality": {"fresh": True}}
    decision = {"decision_id": "d1", "target_round_id": "2", "source_round_id": "1",
               "status": "READY_FOR_EXECUTION", "risk_evaluation": {"approved": True,
               "approved_bet": 100, "cashout_target": 2.0, "cashout": 2.0}}
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(model_registry=SimpleNamespace(predict_latest=lambda _: prediction,
          status=lambda _: {"status": "READY", "model_version": "m1", "feature_version": "f1", "deployable": True}),
          dataset_service=object(), build_evidence=lambda _: evidence),
        decision_engine=SimpleNamespace(evaluate=lambda *args, **kwargs: asyncio.sleep(0, result=decision)),
        risk=object()))

    async def run():
        shadow = ShadowManager(repository=repo)
        await shadow.start(health=Health(), starting_balance=5000, goal_balance=10000)
        app.state.shadow = shadow
        await shadow.process_round(app, {"round_id": "1", "multiplier": 1.2})
        assert len(repo.list_shadow_trades()) == 1
        await shadow.process_round(app, {"round_id": "2", "multiplier": 2.5})
        trade = repo.list_shadow_trades()[0]
        assert trade["status"] == "PAPER_RECONCILED"
        assert trade["result"] == "WIN"
        assert repo.list_executions() == []
        assert not live.called
        await shadow.stop()

    asyncio.run(run())


def test_shadow_start_is_blocked_when_health_is_unsafe(tmp_path):
    repo = Repository(tmp_path / "shadow.sqlite3"); repo.init()
    shadow = ShadowManager(repository=repo)
    try:
        asyncio.run(shadow.start(health=SimpleNamespace(can_bet_now=lambda mode=None: {"allowed": False, "reasons": ["history stale"]})))
    except ValueError as exc:
        assert "SHADOW START BLOCKED" in str(exc)
    else:
        raise AssertionError("unsafe shadow startup was accepted")

