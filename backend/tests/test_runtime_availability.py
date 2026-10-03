from __future__ import annotations

import asyncio
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
    first = InstanceLock(tmp_path / "backend.lock")
    second = InstanceLock(tmp_path / "backend.lock")
    first.acquire()
    try:
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
