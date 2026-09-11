import pandas as pd

from app.services.pattern_engine import PatternEngine


def test_low_streak_pattern_counts_following_rounds():
    rounds = pd.DataFrame(
        {
            "round_index": range(1, 9),
            "multiplier": [1.1, 1.2, 2.4, 1.3, 1.4, 1.5, 2.1, 1.1],
            "timestamp": [None] * 8,
        }
    )
    engine = PatternEngine(target=2.0, min_sample_size=1)
    patterns = {p["pattern_id"]: p for p in engine.discover(rounds)}
    assert patterns["low_streak_2"]["occurrences"] >= 2
    assert patterns["low_streak_3"]["success_count"] == 1

