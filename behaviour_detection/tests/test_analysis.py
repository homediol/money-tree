from decimal import Decimal

from behaviour_detection.analysis import (
    baseline_assessment, classify_multiplier, disjoint_reference_blocks,
    sequence_features,
)
from behaviour_detection.importer import _validate_rule_storage_lengths


def test_decimal_outcome_classification_preserves_exact():
    assert classify_multiplier(Decimal("2.0000000001")) == "H"
    assert classify_multiplier(Decimal("1.9999999999")) == "L"
    assert classify_multiplier(Decimal("2.00")) == "E"


def test_sequence_counts_keep_exact_separate_and_report_runs():
    features = sequence_features(list("HHLLEHL"))
    assert features["counts"] == {"H": 3, "L": 3, "E": 1}
    assert features["run_max"] == {"H": 2, "L": 2, "E": 1}
    assert features["current_streak"] == {"symbol": "L", "length": 1}


def test_phase1_disjoint_windows_use_correct_one_based_positions():
    symbols = ["H", "L"] * 150
    blocks = disjoint_reference_blocks(symbols)
    assert len(blocks) == 1
    assert (blocks[0]["first_position"], blocks[0]["last_position"]) == (201, 300)
    assert blocks[0]["high_count"] == blocks[0]["low_count"] == 50
    forecast = baseline_assessment(blocks, latest_position=300,
                                   latest_identity_key="fixture", state="BALANCED")
    assert forecast["expected_high_count"] == 50
    assert forecast["expected_low_count"] == 50
    assert forecast["expected_exact_count"] == 0
    assert forecast["forecast_method"] == "phase1_expanding_mean_of_disjoint_future100_blocks"


def test_rule_definitions_fit_database_string_columns():
    _validate_rule_storage_lengths()
