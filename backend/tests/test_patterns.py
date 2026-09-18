from types import SimpleNamespace
import pandas as pd
import pytest

from app.api.patterns import baseline, patterns, report, sequences, streaks, top_patterns
from app.services.pattern_engine import EvidenceRules, PatternEngine, classify_stability, wilson_interval


def frame(values):
    return pd.DataFrame({"round_id": list(map(str, range(len(values)))), "round_index": range(len(values)),
        "multiplier": values, "timestamp": [f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z" for i in range(len(values))]})


def find(items, **definition):
    return next(item for item in items if all(str(item["definition"].get(k)) == str(v) for k, v in definition.items()))


def test_baseline_probability_and_multiple_targets():
    rounds = frame([1, 2, 3, 5, 10])
    engine = PatternEngine(2, 1)
    assert engine.baseline(rounds)["rate"] == .8
    assert engine.baseline(rounds, 5)["rate"] == .4
    assert PatternEngine(10, 1).baseline(rounds)["successes"] == 1


def test_wilson_interval():
    interval = wilson_interval(5, 10)
    assert interval["lower"] == pytest.approx(.236593, rel=1e-5)
    assert interval["upper"] == pytest.approx(.763407, rel=1e-5)
    assert wilson_interval(0, 0)["lower"] is None


def test_streak_lengths_thresholds_and_conditional_probability():
    rounds = frame([1.1, 2.2, 1.1, 1.2, 2.4, 1.1, 1.2, 1.3, 2.5] + [1.1] * 6 + [3])
    items = PatternEngine(2, 1).streak_patterns(rounds)
    one = find(items, threshold=1.5, streak_length=1)
    two = find(items, threshold=1.5, streak_length=2)
    six = find(items, threshold=1.5, streak_length="6+")
    assert one["sample_size"] > 0 and two["sample_size"] > 0 and six["sample_size"] == 1
    assert one["successes"] + one["failures"] == one["next_outcomes_observed"]
    assert one["success_rate"] == one["successes"] / one["sample_size"]
    assert {item["definition"]["threshold"] for item in items} == {1.2, 1.5, 2.0, 3.0}


def test_sequence_matching_uses_part4_buckets_and_lengths():
    items = PatternEngine(2, 1).sequence_patterns(frame([1.1, 1.2, 2.5, 5, 1.1, 1.2, 3, 6, 1.1, 1.2, 1.1]))
    low_low = next(item for item in items if item["definition"]["length"] == 2 and item["definition"]["sequence"] == ["LOW", "LOW"])
    assert low_low["sample_size"] == 3 and low_low["successes"] == 2
    assert low_low["definition"]["buckets"]["MEDIUM"] == "1.50-3.99"
    assert {item["definition"]["length"] for item in items} == {2, 3, 4, 5}


def test_small_sample_recent_windows_and_baseline_comparison():
    item = find(PatternEngine(2, 30).streak_patterns(frame([1.1, 1.2, 2.5, 4] * 80), (1.5,)), threshold=1.5, streak_length=2)
    assert item["evidence_state"] == "SUFFICIENT_DATA"
    assert item["baseline_rate"] == .5
    assert item["difference_from_baseline"] == item["success_rate"] - .5
    assert item["recent"]["250"]["sample_size"] <= item["sample_size"]
    tiny = PatternEngine(2, 30).sequence_patterns(frame([1.1, 1.2, 2.5, 3.0, 1.1]), (2,))[0]
    assert tiny["evidence_state"] == "INSUFFICIENT_DATA"


def test_chronological_oos_stability_and_evidence():
    item = find(PatternEngine(2, 10).streak_patterns(frame([1.1, 1.2, 2.5, 4] * 100), (1.5,)), threshold=1.5, streak_length=2)
    assert item["train"]["sample_size"] and item["test"]["sample_size"]
    assert item["out_of_sample_rate"] == item["test"]["success_rate"]
    assert item["stability"] == "STABLE"
    required = {"pattern_id", "pattern", "target", "sample_size", "successes", "failures", "success_rate",
        "baseline_rate", "difference_from_baseline", "confidence_interval", "recent_rate", "out_of_sample_rate",
        "stability", "data_period", "generated_at", "explanation"}
    assert required <= item.keys()
    assert "not a next-round prediction" in item["explanation"]


def test_stability_states_centralized():
    rules = EvidenceRules(min_sample_size=30)
    make = lambda n, rate, low, high, diff: {"sample_size": n, "success_rate": rate,
        "confidence_interval": {"lower": low, "upper": high}, "difference_from_baseline": diff}
    assert classify_stability(make(10,.5,.2,.8,0), make(10,.5,.2,.8,0), rules) == "INSUFFICIENT_DATA"
    assert classify_stability(make(50,.55,.4,.7,.05), make(40,.57,.4,.72,.07), rules) == "STABLE"
    assert classify_stability(make(50,.8,.7,.9,.3), make(40,.3,.2,.4,-.2), rules) == "VARIABLE"


def test_ranking_is_not_raw_rate_only():
    engine = PatternEngine(2, 30)
    low = {"sample_size": 2, "success_rate": 1., "confidence_interval": wilson_interval(2, 2)}
    large = {"sample_size": 100, "success_rate": .6, "confidence_interval": wilson_interval(60, 100)}
    low_score, _ = engine._rank_score(low, .5, {"success_rate": .2}, {"success_rate": 0}, "INSUFFICIENT_DATA")
    large_score, parts = engine._rank_score(large, .1, {"success_rate": .6}, {"success_rate": .59}, "STABLE")
    assert large_score > low_score
    assert len(parts) == 6


def test_no_future_leakage_for_final_target():
    original = [1.1, 1.2, 2.5, 1.1, 1.2, 3., 1.1, 1.2, 1.]
    changed = original[:-1] + [99.]
    first = {item["pattern_id"]: item for item in PatternEngine(2, 1).discover(frame(original))}
    second = {item["pattern_id"]: item for item in PatternEngine(2, 1).discover(frame(changed))}
    assert first.keys() == second.keys()
    assert all(first[key]["sample_size"] == second[key]["sample_size"] for key in first)
    item = find(first.values(), threshold=1.5, streak_length=2)
    assert second[item["pattern_id"]]["successes"] == item["successes"] + 1


def test_pattern_api_shapes_and_filters():
    payload = PatternEngine(2, 1).report(frame([1.1, 1.2, 2.5, 4] * 20))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(pattern_report=lambda target=None: payload))))
    assert baseline(request)["sample_size"] == 80
    assert report(request)["baseline"]["rate"] == .5
    assert len(top_patterns(request, limit=3)["patterns"]) == 3
    assert all(item["kind"] == "streak" for item in streaks(request, threshold=1.5)["patterns"])
    assert all(item["definition"]["length"] == 2 for item in sequences(request, length=2)["patterns"])
    assert len(patterns(request, kind="sequence", limit=5)["patterns"]) == 5
