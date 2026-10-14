import json
from datetime import datetime, timedelta, timezone

from app.history_collector import HistoryCollectorManager


def test_collector_api_status_exposes_each_cashout_panel(tmp_path, monkeypatch):
    manager = HistoryCollectorManager(tmp_path / "rounds.json")
    manager.status_path = tmp_path / "status.json"
    heartbeat_at = datetime.now(timezone.utc).isoformat()
    value = {
        "target": "2.00", "auto_bet": "OFF", "status": "READY",
        "panel_1_cashout": "READY", "panel_2_cashout": "READY",
        "panels": [
            {"slot": 0, "status": "READY", "auto_bet": "OFF", "auto_cashout": "ON",
             "multiplier": "2.00", "observed_at": heartbeat_at},
            {"slot": 1, "status": "READY", "auto_bet": "OFF", "auto_cashout": "ON",
             "multiplier": "2.00", "observed_at": heartbeat_at},
        ],
        "updated_at": heartbeat_at, "error_reason": None,
    }
    manager.status_path.write_text(json.dumps({
        "collectorRunning": True, "pid": 12345,
        "heartbeatAt": heartbeat_at,
        "cashout_initialization": value,
    }), encoding="utf-8")
    monkeypatch.setattr(manager, "_external_process", lambda: (True, None))

    status = manager.health_status(persist=False)
    assert status["cashout_initialization"] == value
    assert status["cashout_initialization"]["panel_1_cashout"] == "READY"
    assert status["cashout_initialization"]["panel_2_cashout"] == "READY"


def test_missing_collector_cashout_status_is_unknown(tmp_path, monkeypatch):
    manager = HistoryCollectorManager(tmp_path / "rounds.json")
    manager.status_path = tmp_path / "status.json"
    manager.status_path.write_text(json.dumps({"collectorRunning": True, "pid": 12345}), encoding="utf-8")
    monkeypatch.setattr(manager, "_external_process", lambda: (True, None))

    status = manager.health_status(persist=False)
    assert status["cashout_initialization"]["status"] == "UNKNOWN"
    assert status["cashout_initialization"]["panel_1_cashout"] == "UNKNOWN"
    assert status["cashout_initialization"]["panel_2_cashout"] == "UNKNOWN"


def test_stopped_or_stale_collector_cannot_publish_old_browser_readiness(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    cases = [
        (False, False, now.isoformat()),
        (True, True, (now - timedelta(hours=1)).isoformat()),
    ]
    for index, (process_evidence, collector_running, heartbeat) in enumerate(cases):
        manager = HistoryCollectorManager(tmp_path / f"rounds-{index}.json")
        manager.status_path = tmp_path / f"status-{index}.json"
        manager.status_path.write_text(json.dumps({
            "collectorRunning": collector_running, "pid": 12345,
            "heartbeatAt": heartbeat, "browserConnected": True,
            "frameConnected": True, "loggedIn": True,
            "authenticationState": "AUTHENTICATED",
            "browser_pages": {
                "browser_count": 1, "context_count": 1, "page_count": 1,
                "aviator_page_count": 1, "aviator_target_id": "old-page",
                "aviator_page_state": "OPEN", "game_frame_count": 1,
                "game_frame_state": "READY", "page_role": "SHARED_AVIATOR",
            },
            "cashout_initialization": {
                "target": "2.00", "auto_bet": "OFF", "status": "READY",
                "panel_1_cashout": "READY", "panel_2_cashout": "READY",
            },
        }), encoding="utf-8")
        monkeypatch.setattr(manager, "_external_process",
                            lambda: (process_evidence, heartbeat))

        status = manager.health_status(persist=False)

        assert status["browser_pages"]["page_count"] is None
        assert status["browser_pages"]["aviator_target_id"] is None
        assert status["browser_pages"]["game_frame_state"] == "UNKNOWN"
        assert status["browser_pages"]["topology_verified"] is False
        assert status["browser_connected"] is False
        assert status["frame_connected"] is False
        assert status["logged_in"] is None
        assert status["authentication_state"] == "UNKNOWN"
        assert status["cashout_initialization"]["status"] == "UNKNOWN"
        assert status["cashout_initialization"]["panel_1_cashout"] == "UNKNOWN"
        assert status["betting_ui_configuration"]["status"] == "UNKNOWN"
