import asyncio
import json

from main import ConnectionManager
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


def test_supervisor_collector_remains_owned_when_backend_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNER_COLLECTOR_EXTERNAL", "1")
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json")
    collector.external_pid_path = tmp_path / "collector-supervisor.json"
    collector.status_path = tmp_path / "status.json"
    collector.external_pid_path.write_text(json.dumps({
        "pid": __import__("os").getpid(), "running": True,
        "timestamp": "2026-09-25T10:00:00Z",
    }))
    collector.status_path.write_text(json.dumps({"health": "HEALTHY", "collectorRunning": True}))
    assert collector.health_status()["running"] is True
    asyncio.run(collector.stop())
    assert collector.health_status()["running"] is True


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

    monkeypatch.setenv("WINNER_COLLECTOR_EXTERNAL", "1")
    collector = HistoryCollectorManager(tmp_path / "roundhistory.json", repository=repository)
    collector.status_path = tmp_path / "collector-status.json"
    collector.external_pid_path = tmp_path / "collector-pid.json"
    collector.status_path.write_text(json.dumps({"health": "HEALTHY", "browserConnected": True}))
    collector.external_pid_path.write_text(json.dumps({
        "pid": __import__("os").getpid(), "running": True,
        "timestamp": "2026-09-25T10:00:00Z",
    }))
    snapshot = collector.health_status()
    assert snapshot["data_source"] == "collector_observation"
    assert repository.load_application_state("collector_status") == snapshot
