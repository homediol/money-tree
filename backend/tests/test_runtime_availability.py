from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core.runtime import InstanceLock, SingleInstanceError
from app.database.repository import Repository
from main import app


def run(coro):
    return asyncio.run(coro)


def prepare_runtime(tmp_path):
    repository = Repository(tmp_path / "availability.sqlite3")
    repository.init()
    app.state.wp = SimpleNamespace(repository=repository)
    app.state.ready = True
    app.state.instance_id = "test-instance"
    app.state.started_at = "2026-09-24T00:00:00+00:00"
    app.state.started_monotonic = time.monotonic()
    return repository


def test_health_reports_backend_and_database(tmp_path):
    prepare_runtime(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.get("/health")

    response = run(scenario())
    payload = response.json()
    assert response.status_code == 200
    assert payload["service"] == "winner-predict-backend"
    assert payload["backend"]["status"] == "ok"
    assert payload["database"]["status"] == "ok"
    assert payload["ok"] is True


def test_health_reports_postgres_without_exposing_dsn_or_sqlite_path(tmp_path):
    repository = prepare_runtime(tmp_path)
    # Use a local probe connection while presenting PostgreSQL configuration.
    repository.database_url = "postgresql://user:secret@host/database"
    sqlite_probe = Repository(tmp_path / "health-probe.sqlite3")
    sqlite_probe.init()
    repository.connect = sqlite_probe.connect

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.get("/health")

    response = run(scenario())
    assert response.status_code == 200
    assert response.json()["database"] == {"status": "ok", "backend": "postgresql"}
    assert "secret" not in response.text


def test_unhandled_request_returns_500_and_server_stays_healthy(tmp_path):
    repository = prepare_runtime(tmp_path)

    class CrashingState:
        def __init__(self):
            self.repository = repository

        @property
        def quality(self):
            raise RuntimeError("request-level failure")

    app.state.wp = CrashingState()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            failed = await client.get("/api/system/status")
            healthy = await client.get("/health")
            return failed, healthy

    failed, healthy = run(scenario())
    assert failed.status_code == 500
    assert failed.json()["error"] == "internal_server_error"
    assert failed.json()["request_id"]
    assert healthy.status_code == 200
    assert healthy.json()["database"]["status"] == "ok"


def test_health_exposes_database_failure_without_crashing_backend():
    class BrokenRepository:
        database_path = "broken.sqlite3"

        @staticmethod
        def connect():
            raise OSError("database unavailable")

    app.state.wp = SimpleNamespace(repository=BrokenRepository())
    app.state.ready = True
    app.state.instance_id = "test-instance"
    app.state.started_monotonic = time.monotonic()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.get("/health")

    response = run(scenario())
    assert response.status_code == 503
    assert response.json()["backend"]["status"] == "ok"
    assert response.json()["database"]["status"] == "error"


def test_instance_lock_prevents_duplicate_backend(tmp_path):
    path = tmp_path / "backend.lock"
    first = InstanceLock(path, port=8000, instance_id="owner-instance")
    second = InstanceLock(path, port=8000, instance_id="competitor-instance")
    first.acquire()
    try:
        metadata = json.loads(path.read_text())
        assert metadata["pid"] > 0
        assert metadata["created_at"]
        assert metadata["instance_id"] == "owner-instance"
        assert metadata["port"] == 8000
        assert "process_start_time" in metadata
        assert metadata["backend_port"] == 8000
        assert metadata["hostname"] == socket.gethostname()
        assert metadata["owner_type"] == InstanceLock.OWNER_TYPE
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_instance_lock_replaces_stale_metadata_only_after_kernel_lock_is_free(tmp_path):
    path = tmp_path / "backend.lock"
    path.write_text(json.dumps({"pid": 2_147_483_647, "started_at": "old-owner"}))
    lock = InstanceLock(path, port=8000, instance_id="recovered-owner")
    lock.acquire()
    try:
        metadata = json.loads(path.read_text())
        assert metadata["pid"] != 2_147_483_647
        assert metadata["instance_id"] == "recovered-owner"
        assert metadata["created_at"] != "old-owner"
    finally:
        lock.release()


def test_instance_lock_detects_pid_reuse_from_process_start_time(tmp_path, monkeypatch):
    path = tmp_path / "backend.lock"
    path.write_text(json.dumps({
        "pid": os.getpid(), "process_start_time": "different-boot:1",
        "hostname": socket.gethostname(), "owner_type": InstanceLock.OWNER_TYPE,
    }))
    monkeypatch.setattr(InstanceLock, "_looks_like_backend_process", staticmethod(lambda _snapshot: True))
    lock = InstanceLock(path, port=8000, instance_id="pid-reuse-recovered")
    lock.acquire()
    try:
        metadata = json.loads(path.read_text())
        assert metadata["instance_id"] == "pid-reuse-recovered"
        assert metadata["process_start_time"] == InstanceLock._process_start_time(os.getpid())
    finally:
        lock.release()


def test_instance_lock_refuses_live_backend_metadata_without_kernel_lock(tmp_path, monkeypatch):
    path = tmp_path / "backend.lock"
    prior = {
        "pid": os.getpid(),
        "process_start_time": InstanceLock._process_start_time(os.getpid()),
        "hostname": socket.gethostname(), "owner_type": InstanceLock.OWNER_TYPE,
        "instance_id": "still-live-owner", "backend_port": 8000,
    }
    path.write_text(json.dumps(prior))
    monkeypatch.setattr(InstanceLock, "_looks_like_backend_process", staticmethod(lambda _snapshot: True))
    lock = InstanceLock(path, port=8000, instance_id="must-not-take-over")
    with pytest.raises(SingleInstanceError, match="live_backend_without_kernel_lock"):
        lock.acquire()
    assert json.loads(path.read_text()) == prior
