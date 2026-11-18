from behaviour_detection.rules import active_rule_registry, evaluate_descriptive_rules


def _rule(version, *, enabled=True, thresholds=None):
    return {"rule_id": "SEQUENCE_RUN_ALTERNATION", "version": version,
            "enabled": enabled, "name": "sequence", "category": "sequence",
            "description": "fixture", "formula": "count sequence",
            "input_features": ["sequence"], "thresholds": thresholds or {"motif_max_length": 6},
            "minimum_evidence": {"rounds": 2}, "evaluation_frequency": "each_round",
            "validation_status": "descriptive_rule"}


def test_rule_registry_uses_latest_enabled_version_and_evaluation_explains_config():
    registry = active_rule_registry([_rule("1.0.0"), _rule("1.0.1", thresholds={"motif_max_length": 8})])
    assert registry["SEQUENCE_RUN_ALTERNATION"]["version"] == "1.0.1"
    result = evaluate_descriptive_rules({"sequence": {"rounds": 20}},
                                        latest_identity_key="round-1", registry=registry)
    assert len(result) == 1
    assert result[0]["rule_version"] == "1.0.1"
    assert result[0]["explanation"]["thresholds"] == {"motif_max_length": 8}


def test_disabled_rule_is_not_evaluated_even_if_older_version_was_enabled():
    registry = active_rule_registry([_rule("1.0.0"), _rule("1.0.1", enabled=False)])
    assert "SEQUENCE_RUN_ALTERNATION" not in registry
    result = evaluate_descriptive_rules({"sequence": {"rounds": 20}},
                                        latest_identity_key="round-1", registry=registry)
    assert result == []
