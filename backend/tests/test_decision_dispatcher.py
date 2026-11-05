from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.decision_dispatcher import DecisionDispatcher


def _selection(*, currency="RWF"):
    return {
        "selection_id": "selection-1",
        "execution_intent": {
            "decision_id": "selection-1",
            "source_round_id": "source-10",
            "target_round_id": "target-11",
            "provider_round_id": "provider-11",
            "target_stream_id": "stream-1",
            "target_round_index": 11,
            "requested_stake": 200,
            "requested_stake_currency": currency,
            "cashout_target": 2.0,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            "panel": 0,
        },
    }


def _app(selection, *, mode="SIMULATION"):
    submitted = []

    class Rules:
        def execution_selection(self, decision_id):
            return selection if decision_id == "selection-1" else None

    class Manager:
        session = SimpleNamespace(mode=mode)

        async def submit_decision(self, intent):
            submitted.append(intent)
            return {"status": "accepted", "simulated": True}

    state = SimpleNamespace(
        rules_engine=Rules(), betting=Manager(),
        emergency_stop=SimpleNamespace(latched=False),
        latest_provider_phase_observation={},
    )
    app = SimpleNamespace(state=state)
    state.decision_dispatcher = DecisionDispatcher(app)
    return app, submitted


def test_dispatch_evaluation_forwards_only_live_committed_selection_to_executor():
    selection = _selection()
    app, submitted = _app(selection)
    dispatcher = app.state.decision_dispatcher
    evaluation = {"sessions": [{"decision": {
        "action": "SELECT", "selection_id": "selection-1",
    }}]}

    result = asyncio.run(dispatcher.dispatch_evaluation(evaluation))

    assert result["status"] == "DISPATCHED"
    assert len(submitted) == 1
    assert submitted[0].decision_id == "selection-1"
    assert submitted[0].round_id == "target-11"
    assert submitted[0].effective_amount() == 200
    assert submitted[0].amount_currency == "RWF"
    assert submitted[0].cashout == 2.0


def test_dispatcher_skips_recovered_or_already_completed_events():
    app, submitted = _app(_selection())
    dispatcher = app.state.decision_dispatcher
    evaluation = {"sessions": [{"decision": {
        "action": "SELECT", "selection_id": "selection-1",
    }}]}

    recovered = asyncio.run(dispatcher.dispatch_evaluation(evaluation, delivery_mode="RECOVERED"))
    completed = asyncio.run(dispatcher.dispatch_evaluation(
        evaluation, target_already_completed=True))

    assert recovered["status"] == "SKIPPED"
    assert completed["status"] == "SKIPPED"
    assert submitted == []


def test_dispatcher_uses_durable_ledger_to_skip_collector_replay():
    app, submitted = _app(_selection())
    entries = {}

    class Repository:
        def __init__(self):
            self.entries = entries

        def execution_for(self, decision_id, target_round_id):
            return entries.get((decision_id, target_round_id))

    manager = app.state.betting
    manager.repository = Repository()
    submit = manager.submit_decision

    async def submit_and_record(intent):
        result = await submit(intent)
        manager.repository.entries[(intent.decision_id, intent.round_id)] = {
            "decision_id": intent.decision_id,
            "target_round_id": intent.round_id,
            "status": "QUEUED",
        }
        return result

    manager.submit_decision = submit_and_record
    evaluation = {"sessions": [{"decision": {
        "action": "SELECT", "selection_id": "selection-1",
    }}]}
    dispatcher = app.state.decision_dispatcher

    first = asyncio.run(dispatcher.dispatch_evaluation(evaluation))
    second = asyncio.run(dispatcher.dispatch_evaluation(evaluation))

    assert first["status"] == "DISPATCHED"
    assert second["dispatched"][0]["status"] == "ALREADY_DISPATCHED"
    assert len(submitted) == 1


def test_dispatcher_rejects_currency_mismatch_before_executor_submission():
    selection = _selection(currency="BIF")
    app, submitted = _app(selection)
    dispatcher = app.state.decision_dispatcher
    from app.betting.schemas import DecisionIntent

    intent = DecisionIntent(
        decision_id="selection-1", round_id="target-11", cashout=2.0,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=20),
        bet_amount=200, amount_currency="RWF", source_round_id="source-10",
        target_stream_id="stream-1", target_round_index=11,
        provider_round_id="provider-11",
    )
    result = asyncio.run(dispatcher.dispatch_intent(intent))

    assert result["http_status"] == 409
    assert "stake_currency_does_not_match_rules_engine_configuration" in result["reasons"]
    assert submitted == []


def test_real_executor_contract_rejects_rwf_stake_on_bif_account():
    from app.betting.schemas import DecisionIntent
    from app.betting.session import BettingSession
    from app.betting.states import Mode

    session = object.__new__(BettingSession)
    session.mode = Mode.REAL
    session.last_snapshot = {"balance_currency": "BIF"}
    session.current_bet = None
    intent = DecisionIntent(
        decision_id="currency-check", round_id="target-11", cashout=2.0,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=20),
        bet_amount=200, amount_currency="RWF",
    )

    rejection = session._validate_contract(intent)

    assert rejection is not None
    assert rejection[0] == "stake_currency_mismatch"
