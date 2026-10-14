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


def test_backend_history_lifecycle_only_observes_separate_collector(tmp_path, monkeypatch):
    manager = HistoryCollectorManager(tmp_path / "roundhistory.json")
    calls = []
    monkeypatch.setattr(manager, "_node_status", lambda: {})

    async def forbidden_spawn(*_args, **_kwargs):
        calls.append("spawn")
        raise AssertionError("backend must not start a collector")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    status = asyncio.run(manager.start())
    assert manager.external is True
    assert manager.process is None
    assert status["running"] is None
    assert status["collector_process_status"] == "UNKNOWN"
    assert status["status"] == "UNKNOWN"
    asyncio.run(manager.stop())
    assert calls == []


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


def test_postgres_status_uses_compact_round_summary_without_loading_history(tmp_path):
    class SummaryRepository:
        database_url = "postgresql://test"

        def round_history_status(self):
            return {
                "count": 42,
                "latest": {"round_id": "r42", "round_index": 42, "timestamp": "t42", "multiplier": 2.5},
                "previous": {"round_id": "r41", "round_index": 41, "timestamp": "t41", "multiplier": 1.4},
            }

    def forbidden_round_load():
        raise AssertionError("status should not load every PostgreSQL round")

    manager = HistoryCollectorManager(tmp_path / "roundhistory.json", round_loader=forbidden_round_load)
    manager.repository = SummaryRepository()
    manager.health_status = lambda: {"status": "COLLECTING"}

    result = manager.status()

    assert result["count"] == 42
    assert result["latest"]["round_id"] == "r42"
    assert result["previous"]["round_id"] == "r41"
