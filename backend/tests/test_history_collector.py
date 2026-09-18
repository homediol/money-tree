from __future__ import annotations

import asyncio
import json

from app.history_collector import HistoryCollectorManager
from app.api.history import collector_status, latest, recent, stats


def test_history_loading_validation_dedup_order_stats_and_creation(tmp_path):
    created = HistoryCollectorManager(tmp_path / "created.json")
    assert created.data_path.exists()
    assert created.rows() == []

    path = tmp_path / "roundhistory.json"
    path.write_text(json.dumps([
        {"round_id": "2", "round_index": 2, "timestamp": "t2", "multiplier": 3},
        {"round_id": "1", "round_index": 1, "timestamp": "t1", "multiplier": "1.20x"},
        {"round_id": "1", "round_index": 3, "timestamp": "dup", "multiplier": 8},
        {"round_id": "zero", "round_index": 4, "multiplier": 0},
        {"round_id": "nan", "round_index": 5, "multiplier": "NaN"},
    ]), encoding="utf-8")
    manager = HistoryCollectorManager(path)
    assert [row["round_id"] for row in manager.rows()] == ["1", "2"]
    assert manager.status()["latest"]["round_id"] == "2"
    assert manager.status()["previous"]["round_id"] == "1"
    assert manager.stats() == {
        "total": 2, "min": 1.2, "max": 3.0, "mean": 2.1, "median": 2.1,
        "threshold_counts": {"below_1_5x": 1, "at_least_2x": 1, "at_least_5x": 0, "at_least_10x": 0},
    }


def test_history_events_and_lifecycle_are_independent_from_betting(tmp_path):
    events = []

    async def emit(payload):
        events.append(payload["type"])

    class Process:
        returncode = None
        def send_signal(self, _sig): pass

    async def run():
        manager = HistoryCollectorManager(tmp_path / "roundhistory.json", emit)
        manager.process = Process()
        await manager.pause()
        assert manager.status()["status"] == "WAITING"
        await manager.resume()
        assert manager.status()["running"] is True
        return manager

    manager = asyncio.run(run())
    assert events == ["history:waiting", "history:connecting"]
    assert not hasattr(manager, "betting")


def test_history_api_surface(tmp_path):
    path = tmp_path / "roundhistory.json"
    path.write_text(json.dumps([
        {"round_id": "r1", "round_index": 1, "timestamp": "2026-01-01T00:00:00Z", "multiplier": 2.5},
        {"round_id": "r2", "round_index": 2, "timestamp": "2026-01-01T00:01:00Z", "multiplier": 1.1},
    ]), encoding="utf-8")

    manager = HistoryCollectorManager(path)
    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"history_collector": manager})()})()})()
    assert collector_status(request)["count"] == 2
    assert latest(request)["latest"]["round_id"] == "r2"
    assert recent(request, limit=1)["rounds"][0]["round_id"] == "r2"
    assert stats(request)["total"] == 2
