from pathlib import Path

from app.core.config import Settings
from app.services.app_state import AppState


def test_signal_is_labeled_statistical_analysis(tmp_path):
    settings = Settings(
        data_path=Path(__file__).resolve().parents[1] / "data" / "roundhistory.json",
        database_path=tmp_path / "test.sqlite3",
        model_dir=tmp_path,
    )
    state = AppState(settings)
    analysis = state.current_analysis()
    assert analysis["label"] == "STATISTICAL PATTERN ANALYSIS"
    assert "guarantee" in " ".join(analysis["why"]).lower()
    assert analysis["data_source"] == "roundhistory.json"

