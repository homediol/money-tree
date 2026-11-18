import json

from backend import bot_runner


def test_legacy_start_route_cannot_launch_or_persist_browser_credentials(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(bot_runner, "STATUS_PATH", status_path)

    result = bot_runner.start("+250700000000", "sensitive-test-password", headless=False)

    assert result["success"] is False
    assert result["error"] == "round_history_uses_orbit_data_watch_api"
    assert not status_path.exists()
    assert not (tmp_path / "config.json").exists()


def test_legacy_stop_route_never_stops_the_orbit_collector(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "collectorRunning": True,
        "state": "COLLECTING",
        "dataSource": "ORBIT_DATA_WATCH_API",
    }), encoding="utf-8")
    monkeypatch.setattr(bot_runner, "STATUS_PATH", status_path)

    result = bot_runner.stop()

    assert result["success"] is False
    assert result["error"] == "collector_is_supervisor_managed"
    assert json.loads(status_path.read_text(encoding="utf-8"))["collectorRunning"] is True
