import asyncio
import json

from main import ConnectionManager
from app.api.stability import _node_collector_snapshot
from app.database.repository import Repository
from app.history_collector import HistoryCollectorManager
from app.services.process_lifecycle import ProcessLifecycle


def test_broadcast_tolerates_connections_changing_while_send_yields():
    manager = ConnectionManager()

    class Socket:
        async def send_json(self, payload):
            manager.active.remove(self)
            manager.active.add(Socket())
            await asyncio.sleep(0)

    manager.active.add(Socket())
    asyncio.run(manager.broadcast({"type": "backend:stability"}))
    assert len(manager.active) == 1


def test_external_collector_heartbeat_remains_owned_when_backend_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTOR_HEARTBEAT_STALE_SECONDS", "60")
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.status_path = tmp_path / "status.json"
    heartbeat = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    collector.status_path.write_text(json.dumps({
        "pid": __import__("os").getpid(), "collectorRunning": True,
        "heartbeatAt": heartbeat, "state": "COLLECTING", "loggedIn": True,
    }))
    collector._external_process = lambda: (True, heartbeat)
    assert collector.health_status()["running"] is True
    assert collector.health_status()["status"] == "COLLECTING"
    asyncio.run(collector.stop())
    assert collector.health_status()["running"] is True


def test_expired_collector_heartbeat_is_reported_stale(tmp_path):
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.status_path = tmp_path / "stale-status.json"
    old = "2026-10-01T10:00:00+00:00"
    collector.status_path.write_text(json.dumps({
        "pid": 99999999, "collectorRunning": True, "heartbeatAt": old,
        "state": "COLLECTING",
    }))
    collector._external_process = lambda: (False, old)
    status = collector.health_status()
    assert status["running"] is False
    assert status["status"] == "STALE"
    assert status["heartbeat_state"] == "STALE"
    assert status["collector_process_status"] == "STOPPED"


def test_node_collector_status_does_not_call_a_fresh_heartbeat_a_fresh_feed():
    now = __import__("datetime").datetime(2026, 10, 8, 11, 10, tzinfo=__import__("datetime").timezone.utc)
    status = _node_collector_snapshot({
        "collectorRunning": True, "health": "STALE", "state": "STALE",
        "heartbeatAt": "2026-10-08T11:09:50Z",
        "lastSuccessfulCollection": "2026-10-08T06:10:38Z",
        "loggedIn": None, "authRequired": False,
        "browserConnected": True, "pageConnected": True, "frameConnected": True,
        "lastRoundId": "verified-42", "lastObservedRoundId": "verified-42",
    }, {"process_verified": True, "collector_process_status": "RUNNING",
        "logged_in": False, "network": {"status": "ONLINE"}}, now=now)
    assert status["collector_process_status"] == "RUNNING"
    assert status["heartbeat_state"] == "FRESH"
    assert status["collection_fresh"] is False
    assert status["runtime_status"] == "STALE"
    assert status["authentication_status"] == "UNVERIFIED"
    assert status["network_status"] == "ONLINE"


def test_unverifiable_collector_process_is_not_reported_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTOR_HEARTBEAT_STALE_SECONDS", "60")
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.status_path = tmp_path / "unverified-status.json"
    heartbeat = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    collector.status_path.write_text(json.dumps({
        "pid": 99999999, "collectorRunning": True, "heartbeatAt": heartbeat,
        "state": "COLLECTING",
    }))
    collector._external_process = lambda: (None, heartbeat)
    status = collector.health_status()
    assert status["running"] is None
    assert status["collector_process_status"] == "UNKNOWN"
    assert status["status"] == "COLLECTING"
    assert "could not be verified" in status["process_reason"]


def test_external_process_permission_error_is_unknown_not_stopped(tmp_path, monkeypatch):
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.status_path = tmp_path / "permission-status.json"
    heartbeat = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    collector.status_path.write_text(json.dumps({
        "pid": __import__("os").getpid(), "collectorRunning": True,
        "heartbeatAt": heartbeat,
    }))

    def deny_process_probe(_pid, _signal):
        raise PermissionError("process visibility denied")

    monkeypatch.setattr("app.history_collector.os.kill", deny_process_probe)
    assert collector._external_process() == (None, heartbeat)


def test_fresh_heartbeat_with_pid_outside_backend_namespace_stays_collecting(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTOR_HEARTBEAT_STALE_SECONDS", "60")
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.status_path = tmp_path / "namespace-status.json"
    heartbeat = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    collector.status_path.write_text(json.dumps({
        "pid": 99999999, "collectorRunning": True, "heartbeatAt": heartbeat,
        "state": "COLLECTING", "lastRoundId": "aviator-round-real-42",
        "lastRoundTime": heartbeat,
    }))

    def pid_is_outside_namespace(_pid, _signal):
        raise ProcessLookupError("PID is not visible in this namespace")

    monkeypatch.setattr("app.history_collector.os.kill", pid_is_outside_namespace)
    status = collector.health_status()
    assert status["status"] == "COLLECTING"
    assert status["running"] is None
    assert status["collector_process_status"] == "UNKNOWN"
    assert status["last_persisted_round"] == "aviator-round-real-42"
    assert "heartbeat is fresh" in status["process_reason"]


def test_rejected_duplicate_does_not_inflate_authoritative_restart_count(tmp_path):
    lifecycle = ProcessLifecycle(tmp_path / "lifecycle.jsonl")
    lifecycle.record("backend_start", pid=101, ppid=10, startup_reason="supervisor_start")
    lifecycle.record("backend_exception", pid=202, ppid=20,
                     error_type="SingleInstanceError", reason="lock held")
    lifecycle.record("supervisor_exit", pid=202, supervisor_pid=20,
                     unexpected=True, reason="code=3 signal=none")
    lifecycle.record("backend_start", pid=303, ppid=30, startup_reason="supervisor_start")
    status = lifecycle.status()
    assert status["restart_count"] == 1
    assert status["unexpected_restart_count"] == 0
    assert status["rejected_duplicate_launch_count"] == 1
    assert status["last_shutdown_reason"] is None
    lifecycle.record("supervisor_exit", pid=303, supervisor_pid=30,
                     unexpected=True, reason="code=1 signal=none")
    assert lifecycle.status()["unexpected_restart_count"] == 1


def test_lifecycle_and_collector_observations_are_persisted(tmp_path, monkeypatch):
    repository = Repository(tmp_path / "state.sqlite3")
    repository.init()
    lifecycle_path = tmp_path / "lifecycle.jsonl"
    lifecycle = ProcessLifecycle(lifecycle_path)
    lifecycle.record("backend_start", pid=10, startup_reason="test")
    lifecycle.bind_repository(repository)
    lifecycle.record("backend_signal", signal="SIGTERM")
    restored = ProcessLifecycle(tmp_path / "empty.jsonl")
    restored.bind_repository(repository)
    assert [event["event"] for event in restored.events()] == ["backend_start", "backend_signal"]

    collector = HistoryCollectorManager(tmp_path / "roundhistory.json", repository=repository)
    collector.status_path = tmp_path / "collector-status.json"
    heartbeat = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    collector.status_path.write_text(json.dumps({"state": "COLLECTING", "collectorRunning": True,
                                                  "heartbeatAt": heartbeat, "pid": __import__("os").getpid()}))
    collector._external_process = lambda: (True, heartbeat)
    snapshot = collector.health_status()
    assert snapshot["data_source"] == "collector_observation"
    assert repository.load_application_state("collector_status") == snapshot
