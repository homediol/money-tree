from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.database.repository import Repository
from app.decision.engine import DecisionEngine
from app.decision.schemas import DecisionConfig, DecisionRecord


class RiskResult:
    def __init__(self, approved=True, reason="approved", decision_id=""):
        self.approved, self.reason, self.decision_id = approved, reason, decision_id

    def public(self):
        return {"approved": self.approved, "reason": self.reason,
                "decision_id": self.decision_id, "approved_bet": 250 if self.approved else 0}


class FakeRisk:
    selected_profile = "PROFILE_A"
    emergency_latched = False

    def __init__(self, approved=True, reason="approved"):
        self.approved, self.reason, self.calls = approved, reason, []

    async def evaluate(self, intent, status):
        self.calls.append((intent, status))
        return RiskResult(self.approved, self.reason, intent.decision_id)


def prediction(**updates):
    row = {"prediction_id": "p1", "source_round_id": "100", "target": "next_round_ge_2x",
           "probability_2x": .72, "created_at": datetime.now(timezone.utc).isoformat(),
           "model_version": "m1", "feature_version": "f1", "fresh": True}
    row.update(updates)
    return row


def evidence(**updates):
    row = {"evidence_id": "e1", "prediction_id": "p1", "confidence": "HIGH",
           "evidence_strength": "STRONG", "calculated_at": datetime.now(timezone.utc).isoformat(),
           "model_version": "m1", "feature_version": "f1", "pattern": {"sample_size": 80},
           "data_quality": {"fresh": True}}
    row.update(updates)
    return row


def model(**updates):
    row = {"status": "READY", "deployable": True, "model_version": "m1", "feature_version": "f1"}
    row.update(updates)
    return row


def betting(**updates):
    row = {"automatic_enabled": True, "state": "SIMULATION", "mode": "SIMULATION",
           "profile": "PROFILE_A", "current_balance": 10000, "starting_balance": 10000,
           "goal_balance": 20000, "current_bet": None, "consecutive_losses": 0,
           "stop_reason": None, "emergency_stop": False}
    row.update(updates)
    return row


@pytest.fixture
def setup(tmp_path):
    repo = Repository(tmp_path / "db.sqlite3"); repo.init()
    engine = DecisionEngine(tmp_path / "decision.json", repo,
                            config=DecisionConfig(automatic_mode=True))
    return engine, repo


def run(coro):
    return asyncio.run(coro)


def evaluate(engine, risk=None, *, pred=None, ev=None, mod=None, bet=None, target=None):
    risk = risk or FakeRisk()
    result = run(engine.evaluate(pred or prediction(), ev or evidence(),
                                 model_status=mod or model(), betting_status=bet or betting(),
                                 risk_manager=risk, target_round_id=target))
    return result, risk


def test_qualifying_decision_requires_risk_and_never_calls_executor(setup):
    result, risk = evaluate(setup[0])
    assert result["status"] == "READY_FOR_EXECUTION"
    assert result["risk_status"] == "APPROVED"
    assert result["execution_status"] == "READY"
    assert result["target_round_id"] == "101"
    assert len(risk.calls) == 1
    assert risk.calls[0][0].amount_bif is None  # risk owns sizing


@pytest.mark.parametrize(("pred","ev","mod","reason"), [
    (prediction(probability_2x=.54), evidence(), model(), "probability_below_threshold"),
    (prediction(), evidence(confidence="LOW"), model(), "confidence_below_threshold"),
    (prediction(), evidence(evidence_strength="WEAK"), model(), "evidence_strength_below_threshold"),
    (prediction(), evidence(data_quality={"fresh": False}), model(), "prediction_or_data_stale"),
    (prediction(), evidence(model_version="other"), model(), "model_or_feature_version_mismatch"),
    (prediction(), evidence(), model(status="STALE"), "model_unhealthy"),
])
def test_threshold_stale_health_and_version_blocks(setup, pred, ev, mod, reason):
    result, risk = evaluate(setup[0], pred=pred, ev=ev, mod=mod)
    assert result["status"] == "BLOCKED"
    assert reason in result["block_reasons"]
    assert not risk.calls


@pytest.mark.parametrize(("config","bet","reason"), [
    (DecisionConfig(automatic_mode=False), betting(), "automatic_mode_off"),
    (DecisionConfig(automatic_mode=True), betting(stop_reason="goal_reached"), "goal_reached"),
    (DecisionConfig(automatic_mode=True), betting(emergency_stop=True), "emergency_stop"),
    (DecisionConfig(automatic_mode=True), betting(mode="REAL", browser_status="DISCONNECTED", last_ui_ready=False), "browser_unavailable"),
])
def test_system_gates(tmp_path, config, bet, reason):
    repo = Repository(tmp_path / f"{reason}.sqlite3"); repo.init()
    engine = DecisionEngine(tmp_path / f"{reason}.json", repo, config=config)
    result, risk = evaluate(engine, bet=bet)
    assert reason in result["block_reasons"]
    assert not risk.calls


def test_missing_inputs_wait_without_persisting(setup):
    result = run(setup[0].evaluate(None, None, model_status=model(), betting_status=betting(), risk_manager=FakeRisk()))
    assert result["status"] == "WAITING"
    assert setup[1].latest_decision() is None


def test_round_mismatch_is_blocked_before_risk(setup):
    result, risk = evaluate(setup[0], target="999")
    assert "target_round_mismatch" in result["block_reasons"]
    assert not risk.calls


def test_source_must_be_latest_history_round(setup):
    result, risk = evaluate(setup[0], bet=betting(latest_history_round_id="101"))
    assert "target_round_stale" in result["block_reasons"]
    assert result["status"] == "EXPIRED"
    assert result["execution_status"] == "EXPIRED"
    assert not risk.calls


def test_risk_rejection_cannot_become_executable(setup):
    result, risk = evaluate(setup[0], FakeRisk(False, "session loss limit"))
    assert result["status"] == "RISK_REJECTED"
    assert result["execution_status"] == "BLOCKED"
    assert result["risk_status"] == "REJECTED"


def test_idempotency_and_concurrency_create_one_decision(setup):
    engine, repo = setup
    risk = FakeRisk()
    async def concurrent():
        return await asyncio.gather(*[
            engine.evaluate(prediction(), evidence(), model_status=model(), betting_status=betting(), risk_manager=risk)
            for _ in range(8)
        ])
    rows = run(concurrent())
    assert len({row["decision_id"] for row in rows}) == 1
    assert len(repo.list_decisions()) == 1
    assert len(risk.calls) == 1


def test_ttl_expiration_and_atomic_validated_contract(tmp_path):
    repo = Repository(tmp_path / "db.sqlite3"); repo.init()
    path = tmp_path / "decision.json"
    engine = DecisionEngine(path, repo, config=DecisionConfig(automatic_mode=True, decision_ttl_seconds=1))
    result, _ = evaluate(engine)
    payload = json.loads(path.read_text())
    payload["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(payload))
    expired = engine.current()
    assert expired["status"] == "EXPIRED"
    assert expired["execution_status"] == "EXPIRED"
    DecisionRecord.model_validate_json(path.read_text())
    assert not path.with_suffix(".json.tmp").exists()


def test_audit_history_contains_risk_handoff_and_ready(setup):
    result, _ = evaluate(setup[0])
    audit = setup[1].decision_audit(result["decision_id"])
    assert [row["status"] for row in audit] == ["PENDING_RISK", "READY_FOR_EXECUTION"]
    transitions = [row["status"] for row in result["transitions"]]
    assert transitions == ["CREATED", "VALIDATING", "CANDIDATE", "PENDING_RISK", "RISK_APPROVED", "READY_FOR_EXECUTION"]


def test_api_has_no_direct_create_or_execute_bypass():
    from main import app
    decision_routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes if route.path.startswith("/api/decisions")}
    assert not any(path.endswith("/create") or path.endswith("/execute") for path, _ in decision_routes)
    evaluate_routes = [methods for path, methods in decision_routes if path == "/api/decisions/evaluate"]
    assert evaluate_routes == [("POST",)]


def test_schema_rejects_invalid_state_transition(setup):
    result, _ = evaluate(setup[0])
    result["transitions"][2]["status"] = "READY_FOR_EXECUTION"
    with pytest.raises(ValidationError, match="invalid decision transition"):
        DecisionRecord.model_validate(result)


def test_configuration_persists_across_restart(tmp_path):
    repo = Repository(tmp_path / "db.sqlite3"); repo.init()
    path = tmp_path / "decision.json"
    first = DecisionEngine(path, repo)
    first.update_config({"automatic_mode": True, "minimum_probability": .61})
    second = DecisionEngine(path, repo)
    assert second.config.automatic_mode is True
    assert second.config.minimum_probability == .61
