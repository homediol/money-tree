from app.ml.rare_opportunity_policy import RareOpportunitySelectionPolicyV1


def _row(index: int, *, score: float, hit: bool, verified: bool = True):
    return {"source_round_index": index - 1, "target_round_index": index,
            "score": score, "is_true": hit, "pre_outcome_verified": verified}


def test_policy_development_excludes_rows_at_or_after_cutoff():
    rows = [_row(19928, score=.49, hit=True),
            _row(19929, score=.48, hit=False),
            _row(19930, score=.99, hit=True),
            _row(19931, score=1.0, hit=True)]
    report = RareOpportunitySelectionPolicyV1.develop(
        rows, model_version="V3_FROZEN_2026-10-03", model_hash="frozen",
        development_cutoff_round=19929)

    assert report["development_sample_count"] == 2
    assert report["development_target_end"] == 19929
    assert report["active_experiment_rounds_used"] is False
    assert report["status"] == "NO_RELIABLE_POLICY"
    assert report["selection_rules"] is None


def test_unverified_rows_are_excluded_and_empty_development_fails_closed():
    report = RareOpportunitySelectionPolicyV1.develop(
        [_row(10, score=.99, hit=True, verified=False)],
        model_version="V3_FROZEN_2026-10-03", model_hash="frozen")

    assert report["development_sample_count"] == 0
    assert report["candidate_frequencies"] == []
    assert report["status"] == "NO_RELIABLE_POLICY"


def test_frozen_policy_decision_states_are_separate_from_scoring():
    policy = {"status": "FROZEN", "selection_rules": {
        "selection_threshold": .8, "watch_threshold": .7}}

    assert RareOpportunitySelectionPolicyV1.decide(.9, policy) == (
        "SELECTED", "FROZEN_DEVELOPMENT_RARE_TAIL_CUTOFF")
    assert RareOpportunitySelectionPolicyV1.decide(.75, policy) == (
        "WATCH", "FROZEN_DEVELOPMENT_WATCH_BAND")
    assert RareOpportunitySelectionPolicyV1.decide(.6, policy) == (
        "NO_SIGNAL", "BELOW_FROZEN_DEVELOPMENT_WATCH_BAND")
    assert RareOpportunitySelectionPolicyV1.decide(.9, {"status": "NO_RELIABLE_POLICY"}) == (
        "NO_SIGNAL", "NO_RELIABLE_POLICY")
