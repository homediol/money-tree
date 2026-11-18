import asyncio
import json
from types import SimpleNamespace

from starlette.requests import Request

from main import health


class NoIoRepository:
    database_url = "postgresql://configured"

    def connect(self):
        raise AssertionError("liveness must not acquire a database connection")


class CachedHealth:
    def snapshot(self):
        return {"components": {"database": {
            "state": "HEALTHY", "age_s": 0.25, "last_success": 123.0,
        }}}


class NoHistoryLifecycle:
    def status(self):
        raise AssertionError("liveness must not load lifecycle history")


def test_health_endpoint_uses_cached_dependency_and_compact_lifecycle():
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=NoIoRepository()),
        system_health=CachedHealth(),
        lifecycle=NoHistoryLifecycle(),
        instance_lock=SimpleNamespace(status=lambda: {"owned": True}),
        ready=True,
        started_at="2026-11-16T00:00:00+00:00",
        started_monotonic=0.0,
        instance_id="health-test",
    )
    app = SimpleNamespace(state=state)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/health", "raw_path": b"/health",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        "app": app,
    }

    response = asyncio.run(health(Request(scope)))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "healthy"
    assert payload["backend"]["status"] == "ok"
    assert payload["database"]["status"] == "ok"
    assert payload["stability"]["pid"] > 0
    assert "events" not in payload["stability"]


def test_health_degrades_when_cached_database_heartbeat_is_stale():
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=NoIoRepository()),
        system_health=SimpleNamespace(snapshot=lambda: {"components": {"database": {
            "state": "HEALTHY", "age_s": 61, "last_success": 123.0,
        }}}),
        lifecycle=None,
        instance_lock=None,
        ready=True,
        started_at=None,
        started_monotonic=0.0,
        instance_id="health-test",
    )
    app = SimpleNamespace(state=state)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/health", "raw_path": b"/health",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        "app": app,
    }

    response = asyncio.run(health(Request(scope)))
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["backend"]["status"] == "ok"
    assert payload["database"]["status"] == "stale"
