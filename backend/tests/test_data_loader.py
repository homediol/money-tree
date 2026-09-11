from pathlib import Path

from app.services.data_loader import RoundHistoryLoader


def test_loader_reads_real_roundhistory():
    path = Path(__file__).resolve().parents[1] / "data" / "roundhistory.json"
    frame, report = RoundHistoryLoader(path).load()
    assert report.detected_format == "array"
    assert report.valid_records > 0
    assert report.invalid_records == 0
    assert {"round_index", "multiplier", "timestamp", "target"}.issubset(frame.columns)
    assert frame["multiplier"].min() >= 1

