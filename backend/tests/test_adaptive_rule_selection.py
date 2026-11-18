from copy import deepcopy

from app.rules_engine import RulesEngine, _expected, _identity, _reference


def _round(index, *, multiplier=1.5, observed=False):
    return {
        "stream_id": "adaptive-stream", "round_id": f"round-{index}",
        "observation_id": f"round-{index}", "round_index": index,
        "stream_sequence": index, "multiplier": multiplier,
        "event_type": "round.observed" if observed else "round.persisted",
        "durability": "PENDING" if observed else "COMMITTED",
        "continuity_status": "VERIFIED",
    }


def _start_adaptive(engine=None):
    engine = engine or RulesEngine()
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(10), options={"adaptive_rule_selection": True})
    return engine, session


def test_adaptive_starts_with_an_exploratory_candidate_and_no_fixed_participants():
    engine, session = _start_adaptive()
    assert session["adaptive_rule_selection"] is True
    assert session["automatic_rule_selection"] is False
    assert session["rule_ids"]
    assert session["active_rule_ids"] == []

    source = {**_identity(_round(11)), "outcome": "H"}
    choice = engine._adaptive_rule_choice(session, source, [_round(9), _round(10), _round(11)], "now")
    assert len(choice["rule_ids"]) == 1
    chosen = next(row for row in choice["candidates"] if row["rule_id"] == choice["rule_ids"][0])
    assert chosen["evidence_status"] == "EXPLORATORY"
    assert chosen["reason"] == "EXPLORATORY_BEST_AVAILABLE"
    assert choice["selection_version"] == 1
    assert choice["decision_sequence"] == 1


def test_adaptive_selects_a_variable_size_diverse_portfolio_and_records_switches():
    engine, session = _start_adaptive()
    session = engine.sessions[session["session_id"]]
    rule_ids = session["rule_ids"]
    assert len(rule_ids) >= 5
    duplicated = set(rule_ids[:2])

    def evidence(_session, rule_id, _source, context):
        signals = [4, 9] if rule_id in duplicated else [rule_ids.index(rule_id) + 20]
        return {"rolling_50": {"wins": 10, "losses": 2, "settled": 12, "net": 8},
            "baseline_win_rate": 0.4, "baseline_sample_count": 30,
            "score": 0.68, "confidence": 0.7, "stability": 0.9,
            "momentum": 0.1, "performance_feed_fresh": True,
            "context_evidence": {"context": context, "similar_samples": 5,
                                 "adjusted_win_rate": 0.65},
            "signal_round_set": signals}

    engine._adaptive_rule_evidence = evidence
    source = {**_identity(_round(11)), "outcome": "H"}
    choice = engine._adaptive_rule_choice(session, source, [_round(9), _round(10), _round(11)], "first")
    assert 3 <= len(choice["rule_ids"]) < len(rule_ids)
    excluded = next(row for row in choice["candidates"]
                    if row["reason"] == "EXCLUDED_HIGH_SIGNAL_OVERLAP")
    assert excluded["reason"] == "EXCLUDED_HIGH_SIGNAL_OVERLAP"
    assert excluded["overlaps_rule_id"] in rule_ids[:2]
    assert all(row["validated"] for row in choice["candidates"] if row["selected"])

    original_ids = list(choice["rule_ids"])
    removed_id = original_ids[0]
    old_version = choice["selection_version"]
    session_id = session["session_id"]

    def changed_evidence(_session, rule_id, _source, context):
        result = evidence(_session, rule_id, _source, context)
        if rule_id == removed_id:
            result.update({"score": 0.25, "stability": 0.1})
        return result

    engine._adaptive_rule_evidence = changed_evidence
    second = engine._adaptive_rule_choice(session,
        {**_identity(_round(12)), "outcome": "L"}, [_round(10), _round(11), _round(12)], "second")
    assert engine.sessions[session_id] is session
    assert second["selection_version"] == old_version + 1
    assert removed_id not in second["rule_ids"]
    assert any(item["rule_id"] == removed_id
               and item["reason"] == "REMOVED_AFTER_EDGE_DECAY_OR_UNSTABLE_EVIDENCE"
               for item in second["removed"])
    assert len(session["adaptive_decisions"]) == 2


def test_adaptive_separate_rule_predictions_settle_independently_but_count_one_shared_target():
    engine, session = _start_adaptive()
    session = engine.sessions[session["session_id"]]
    rule_ids = session["rule_ids"][:2]
    source = _identity(_round(11))
    proposals = []
    for rule_id in rule_ids:
        proposals.append({"rule_id": rule_id, "rule_version": engine._version(rule_id),
            "eligible": True, "reason": "test trigger",
            "proposal": {"sourceRoundRef": _reference(source),
                "expectedTargetRef": _expected(source),
                "relevantInputs": {"ruleCycle": {"action": "START", "maximum_targets": 3}}}})
    result = engine._separate(session, source, proposals, "selected")
    assert result["action"] == "SELECT"
    assert set(result["true_rule_ids"]) == set(rule_ids)
    assert session["selections"] == []
    assert all(len(session["rule_selections"][rule_id]) == 1 for rule_id in rule_ids)
    [signal_envelope] = session["adaptive_signal_selections"]
    assert result["selection_id"] == signal_envelope["selection_id"]
    assert signal_envelope["contributing_rule_ids"] == rule_ids
    assert len(result["selection_ids"]) == len(rule_ids)

    settled = engine._resolve(session, _identity(_round(12, multiplier=2.5)), "settled")
    assert len([item for item in settled if item.get("independent_rule_outcome")]) == 2
    assert all(session["rule_selections"][rule_id][0]["status"] == "WIN" for rule_id in rule_ids)
    assert signal_envelope["status"] == "WIN"
    summary = engine._session_summary(session)
    assert summary["results"] == {"WIN": 1, "LOSS": 0, "PENDING": 0}
    assert summary["selection_count"] == 1
    assert summary["adaptive_portfolio_performance"]["independent_rule_predictions"] == 2
    assert all(summary["adaptive_rule_performance"][rule_id]["wins"] == 1 for rule_id in rule_ids)


def test_fixed_rule_baseline_uses_unselected_forward_observer_ledgers():
    engine, created = _start_adaptive()
    session = engine.sessions[created["session_id"]]
    selected_rule, unselected_rule = session["rule_ids"][:2]
    target = {"streamId": "adaptive-stream", "roundIndex": 12}
    session["rule_selections"][selected_rule].append({
        "status": "LOSS", "actual_target": target,
    })

    class ObserverEngine:
        sessions = {"observer-session": {
            "mode": session["mode"],
            "performance_configuration_key": engine._performance_configuration_key(unselected_rule),
            "selections": [{"status": "WIN", "actual_target": target}],
        }}

    engine._performance_observers[unselected_rule] = {"engine": ObserverEngine()}
    comparison = engine._adaptive_baseline_comparison(session)
    assert comparison["fixed_rule"]["rule_id"] == unselected_rule
    assert comparison["fixed_rule"]["wins"] == 1
    assert comparison["fixed_rule"]["settled_targets"] == 1
    assert comparison["adaptive_portfolio"]["settled_targets"] == 1


def test_running_adaptive_session_reselects_after_settlement_without_restarting():
    engine, created = _start_adaptive()
    session = engine.sessions[created["session_id"]]
    rule_ids = session["rule_ids"]
    first_rule, second_rule, incoming_rule = rule_ids[:3]

    def evidence(current_session, rule_id, _source, context):
        settled = current_session.get("rule_selections", {}).get(first_rule) or []
        first_has_loss = any(item.get("status") == "LOSS" for item in settled)
        score = (0.25 if first_rule == rule_id and first_has_loss else
                 0.72 if rule_id in {second_rule, incoming_rule} or rule_id == first_rule else 0.4)
        sample = 12 if score >= 0.7 or score == 0.25 else 0
        return {"rolling_50": {"wins": 10 if score >= 0.7 else 3,
                "losses": 2 if sample else 0, "settled": sample, "net": 8 if sample else 3},
            "baseline_win_rate": 0.4, "score": score, "confidence": 0.7 if sample else 0,
            "stability": 0.9 if score >= 0.7 else 0.1 if score == 0.25 else 0.5,
            "performance_feed_fresh": True, "context_evidence": {"similar_samples": 0},
            "signal_round_set": [rule_ids.index(rule_id) + 30]}

    engine._adaptive_rule_evidence = evidence

    def proposal(current_session, rule_id, source, _history, at):
        before = deepcopy(current_session["engines"][rule_id])
        selected = rule_id in current_session.get("active_rule_ids", [])
        source_ref, target_ref = _reference(source), _expected(source)
        return {"rule_id": rule_id, "rule_version": engine._version(rule_id),
            "eligible": selected, "result": selected, "status": "TRUE" if selected else "FALSE",
            "reason": "test eligible rule" if selected else "test inactive rule",
            "state_snapshot": {"version": 1, "before": before, "after": before},
            "proposal": {"ruleId": rule_id, "ruleVersion": engine._version(rule_id),
                "experimentId": current_session["session_id"], "sourceRoundRef": source_ref,
                "expectedTargetRef": target_ref, "eligible": selected,
                "status": "TRUE" if selected else "FALSE", "reason": "test",
                "evaluatedAt": at, "relevantInputs": {"ruleCycle": {"action": "START",
                    "maximum_targets": 3}}, "privateState": {"before": before, "after": before}}}

    engine._proposal = proposal
    engine._advance_selected_state = lambda *_args, **_kwargs: None
    started_id = session["session_id"]
    engine.observe_round(_round(11, observed=True))
    first = engine.session(started_id)
    first_choice = first["adaptive_rule_choice"]
    assert set(first_choice["rule_ids"]) == {first_rule, second_rule, incoming_rule}
    assert len(first["rule_selections"][first_rule]) == 1

    engine.observe_round(_round(12, observed=True))
    current = engine.session(started_id)
    assert current["session_id"] == started_id
    assert current["rule_selections"][first_rule][0]["status"] == "LOSS"
    assert set(current["adaptive_rule_choice"]["rule_ids"]) == {second_rule, incoming_rule}
    assert current["adaptive_rule_choice"]["selection_version"] == first_choice["selection_version"] + 1


def test_adaptive_selection_decisions_and_mode_survive_restore_without_touching_automatic():
    engine, session = _start_adaptive()
    session = engine.sessions[session["session_id"]]
    source = {**_identity(_round(11)), "outcome": "L"}
    engine._adaptive_rule_choice(session, source, [_round(9), _round(10), _round(11)], "now")
    restored = RulesEngine()
    restored._restore(engine._snapshot())
    current = restored.session(session["session_id"])
    assert current["adaptive_rule_selection"] is True
    assert current["automatic_rule_selection"] is False
    assert current["adaptive_rule_choice"]["selection_version"] == 1
    assert len(current["adaptive_decisions"]) == 1
    assert current["adaptive_rule_choice"] == session["adaptive_rule_choice"]

