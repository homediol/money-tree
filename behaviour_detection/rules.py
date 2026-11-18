from __future__ import annotations

from collections.abc import Iterable, Mapping

RULE_VERSION = "1.0.0"

RULES = [
    {
        "rule_id": "DATA_TIME_QUALITY", "version": RULE_VERSION,
        "name": "Timestamp and ordering quality", "category": "data_quality",
        "description": "Reports known event-time, timestamp tie, and sequence-order limitations.",
        "formula": "Count timestamp ties, source-order inversions, and records without per-round event time; never impute event time from ingestion time.",
        "input_features": ["event_timestamp", "ingestion_timestamp", "timestamp_quality", "order_quality"],
        "thresholds": {"timestamp_group_gap_seconds": 180.0452},
        "minimum_evidence": {"rounds": 1}, "evaluation_frequency": "each_analysis_update",
        "validation_status": "descriptive_audit_rule", "enabled": True,
    },
    {
        "rule_id": "DISTRIBUTION_ROLLING", "version": RULE_VERSION,
        "name": "Multiscale HIGH/LOW/E distribution", "category": "high_low_behaviour",
        "description": "Reports H/L/E shares, transitions, run lengths, entropy, multiplier quantiles, and volatility for 10-500 round windows.",
        "formula": "For each complete rolling window, preserve E separately; H=multiplier>2.00, L<2.00, E=2.00.",
        "input_features": ["outcome", "multiplier_raw", "analysis_position"],
        "thresholds": {"windows": [10, 20, 50, 100, 200, 500]},
        "minimum_evidence": {"window_rounds": "window_width"}, "evaluation_frequency": "each_round",
        "validation_status": "descriptive_rule", "enabled": True,
    },
    {
        "rule_id": "SEQUENCE_RUN_ALTERNATION", "version": RULE_VERSION,
        "name": "Sequence, streak, and alternation monitor", "category": "sequence_behaviour",
        "description": "Describes current streaks, runs, H/L alternation, and transition entropy. EXACT remains a separate symbol and breaks H/L adjacency.",
        "formula": "Count consecutive symbols over observed order; compute alternation only for adjacent pairs with neither symbol E.",
        "input_features": ["outcome", "analysis_position", "order_quality"],
        "thresholds": {"motif_max_length": 6},
        "minimum_evidence": {"rounds": 2}, "evaluation_frequency": "each_round",
        "validation_status": "descriptive_rule", "enabled": True,
    },
    {
        "rule_id": "RESEARCH_MOTIF_MONITOR", "version": RULE_VERSION,
        "name": "Phase 1 motif recurrence monitor", "category": "sequence_behaviour",
        "description": "Tracks research-discovered motifs and their rolling counts. Recurrence is descriptive and does not imply prediction.",
        "formula": "Count exact contiguous motif matches in the latest 500 available ordered outcomes; compare with the imported Phase 1 frequency and BH q-value.",
        "input_features": ["outcome", "research_motif", "BH_q_value"],
        "thresholds": {"rolling_horizon": 500, "maximum_motif_length": 8,
                       "predictive_adjustment": False},
        "minimum_evidence": {"motif_occurrences": 1}, "evaluation_frequency": "each_round",
        "validation_status": "validated_as_retrospective_structure_only", "enabled": True,
    },
    {
        "rule_id": "TIME_PROXIMITY_GROUP", "version": RULE_VERSION,
        "name": "Empirical timestamp-proximity group", "category": "time_behaviour",
        "description": "Builds descriptive historical groups at the Phase 1 positive-gap p99; live rounds without event timestamps are not assigned event-time sessions.",
        "formula": "Start a new group when consecutive recorded event timestamps differ by more than 180.0452 seconds; flag ties and unknown times.",
        "input_features": ["event_timestamp", "analysis_position", "timestamp_quality"],
        "thresholds": {"gap_seconds": 180.0452},
        "minimum_evidence": {"rounds": 1}, "evaluation_frequency": "historical_import_and_event_time_update",
        "validation_status": "descriptive_method", "enabled": True,
    },
    {
        "rule_id": "BEHAVIOUR_STATE_DESCRIPTION", "version": RULE_VERSION,
        "name": "Transparent rolling behavioural state", "category": "behavioural_state",
        "description": "Assigns a descriptive state from the 50-round window using Phase 1 group labels and rolling-50 p90 run cutoffs; confirmation hysteresis is operational.",
        "formula": "With at least 20 non-EXACT outcomes: H share >=.60 HIGH_DOMINANT; <=.40 LOW_DOMINANT; alternation >=.65 ALTERNATING; H/L max run >= rolling-50 p90 (7/8) HIGH/LOW_CLUSTERING; within .50±.08 BALANCED; otherwise TRANSITION. Require three successive candidate evaluations to confirm a state change.",
        "input_features": ["rolling_50", "state_candidate", "timestamp_quality"],
        "thresholds": {"minimum_nonexact": 20, "high_dominant": 0.60, "low_dominant": 0.40,
                       "balanced_half_width": 0.08, "alternation": 0.65,
                       "high_run_p90": 7, "low_run_p90": 8, "confirmation_rounds": 3},
        "minimum_evidence": {"nonexact_rounds": 20}, "evaluation_frequency": "each_round",
        "validation_status": "descriptive_heuristic_not_predictive", "enabled": True,
    },
    {
        "rule_id": "NEXT100_EXPANDING_REFERENCE", "version": RULE_VERSION,
        "name": "Next-100 expanding historical reference", "category": "next100_assessment",
        "description": "Reports a next-100 statistical reference from completed disjoint future-100 blocks; no contextual adjustment is active.",
        "formula": "Starting with origin position 199, make non-overlapping 100-round target blocks. Expand the mean of prior block H/(H+L); separately average prior E/100. Derive expected counts as in the Phase 1 code.",
        "input_features": ["completed_future100_blocks", "H_count", "L_count", "E_count"],
        "thresholds": {"horizon": 100, "high_dominance": [0.55, 0.60, 0.65],
                       "low_dominance": [0.45, 0.40, 0.35], "balanced_bands": [[0.45, 0.55], [0.40, 0.60]],
                       "context_adjustment_enabled": False},
        "minimum_evidence": {"complete_prior_blocks": 20}, "evaluation_frequency": "each_round",
        "validation_status": "validated_reference_baseline_not_forecaster_of_exact_outcomes", "enabled": True,
    },
]


def active_rule_registry(definitions: Iterable | None = None) -> dict[str, dict]:
    """Return enabled highest-version definitions, honoring a newer disablement."""
    if definitions is None:
        definitions = RULES
    latest: dict[str, dict] = {}
    for item in definitions:
        if isinstance(item, Mapping):
            definition = dict(item)
        else:
            definition = {
                "rule_id": item.rule_id, "version": item.version, "name": item.name,
                "category": item.category, "description": item.description,
                "formula": item.formula, "input_features": item.input_features,
                "thresholds": item.thresholds, "minimum_evidence": item.minimum_evidence,
                "evaluation_frequency": item.evaluation_frequency,
                "validation_status": item.validation_status, "enabled": item.enabled,
            }
        rule_id = definition["rule_id"]
        version = tuple(int(part) if part.isdigit() else 0
                        for part in str(definition.get("version", "0")).split("."))
        prior = latest.get(rule_id)
        prior_version = tuple(int(part) if part.isdigit() else 0
                              for part in str(prior.get("version", "0")).split(".")) if prior else ()
        if prior is None or version > prior_version:
            latest[rule_id] = definition
    return {rule_id: definition for rule_id, definition in latest.items()
            if definition.get("enabled", False)}


def evaluate_descriptive_rules(features: dict, *, latest_identity_key: str | None,
                               registry: Mapping[str, Mapping] | None = None) -> list[dict]:
    registry = active_rule_registry() if registry is None else registry
    windows = features.get("rolling_windows", {})
    seq = features.get("sequence", {})
    state = features.get("state", {})
    quality = features.get("source_quality", {})
    evaluations = [
        ("DATA_TIME_QUALITY", "OBSERVED", "descriptive", {
            "event_timestamp_count": quality.get("event_timestamp_count"),
            "event_timestamp_missing_count": quality.get("event_timestamp_missing_count"),
            "timestamp_tie_count": quality.get("timestamp_tie_count"),
            "order_uncertain_count": quality.get("order_uncertain_count"),
            "ingestion_time_is_not_event_time": True}),
        ("DISTRIBUTION_ROLLING", "OBSERVED", "descriptive", windows),
        ("SEQUENCE_RUN_ALTERNATION", "OBSERVED", "descriptive", seq),
        ("RESEARCH_MOTIF_MONITOR", "OBSERVED", "descriptive_only", features.get("motifs", {})),
        ("TIME_PROXIMITY_GROUP", "OBSERVED", "descriptive", features.get("current_time_group", {})),
        ("BEHAVIOUR_STATE_DESCRIPTION", state.get("candidate", "UNCERTAIN"), "descriptive_heuristic", state),
        ("NEXT100_EXPANDING_REFERENCE", features.get("next100", {}).get("data_sufficiency", "INSUFFICIENT_HISTORY"),
         "baseline_reference", features.get("next100", {})),
    ]
    output = []
    for rule_id, result, evidence, explanation in evaluations:
        definition = registry.get(rule_id)
        if definition is None or not definition.get("enabled", False):
            continue
        output.append({"rule_id": rule_id, "rule_version": definition.get("version", RULE_VERSION),
                       "result": result, "evidence_class": evidence,
                       "latest_identity_key": latest_identity_key,
                       "explanation": {"observation": explanation,
                                       "formula": definition.get("formula"),
                                       "thresholds": definition.get("thresholds", {}),
                                       "minimum_evidence": definition.get("minimum_evidence", {}),
                                       "validation_status": definition.get("validation_status")}})
    return output
