import pandas as pd

from app.services.similarity_engine import SimilarityEngine


def test_similarity_returns_historical_cases():
    rounds = pd.DataFrame(
        {
            "round_index": range(1, 12),
            "multiplier": [1.1, 1.4, 1.2, 2.2, 1.1, 1.4, 1.2, 1.3, 3.0, 1.1, 1.4],
            "timestamp": [None] * 11,
        }
    )
    result = SimilarityEngine(target=2.0, min_sample_size=1).find_similar(rounds, sequence_length=3, limit=3)
    assert result["similar_cases"] >= 1
    assert result["probability"] is not None
    assert result["cases"]

