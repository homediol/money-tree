from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
import pytest

from app.api import developer_console, signals


OWNER_KEY = "owner-secret-key-for-developer-console"


@pytest.fixture(autouse=True)
def inline_thread_helpers(monkeypatch):
    # Route contract tests use in-memory repositories; keep their database
    # shim synchronous and leave production threadpool behavior untouched.
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", run_inline)


class Limiter:
    def __init__(self):
        self.revoked = []

    def allow_request(self, _key):
        return True, 0

    async def revoke_token(self, token_id):
        self.revoked.append(token_id)


class FakeRepository:
    database_url = "postgresql://contract-test"

    def __init__(self):
        self.keys = []
        self.audit = []
        self.token_hash = None
        self.token = None
        self.signals = []

    def create_managed_api_key(self, **values):
        if values.get("rotate_token_id"):
            for prior in self.keys:
                if prior["token_id"] == values["rotate_token_id"] and prior["revoked_at"] is None:
                    prior["revoked_at"] = datetime.now(timezone.utc).isoformat()
        self.keys.append({**values, "created_at": datetime.now(timezone.utc).isoformat(),
                          "last_used_at": None, "revoked_at": None})
        self.audit.append({"action": "CREATED", "token_id": values["token_id"]})
        return {key: values[key] for key in (
            "token_id", "project_id", "label", "token_prefix", "key_type", "scopes", "expires_at")}

    def list_managed_api_keys(self):
        return [{key: value for key, value in row.items() if key != "token_hash"}
                for row in self.keys]

    def revoke_managed_api_key(self, token_id):
        for row in self.keys:
            if row["token_id"] == token_id and row["revoked_at"] is None:
                row["revoked_at"] = datetime.now(timezone.utc).isoformat()
                self.audit.append({"action": "REVOKED", "token_id": token_id})
                return {"token_id": token_id, "label": row["label"], "key_type": row["key_type"]}
        return None

    def external_project_token(self, token_hash):
        if token_hash != self.token_hash:
            return None
        return self.token

    def touch_external_project_token(self, token_id):
        if self.token:
            self.token["last_used_at"] = datetime.now(timezone.utc).isoformat()

    def record_api_key_audit(self, action, **values):
        self.audit.append({"action": action, **values})

    def count_bet_signals(self):
        return len(self.signals)

    def list_bet_signals(self, limit=100, *, session_id=None):
        values = [row for row in self.signals if session_id is None or row["session_id"] == session_id]
        return list(reversed(values[-limit:]))

    def bet_signal_by_id(self, signal_id):
        return next((row for row in self.signals if row["signal_id"] == signal_id), None)

    def connect(self):
        return FakeConnection()


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql):
        return self

    def fetchone(self):
        return (1,)


class FakeRequest:
    def __init__(self, app, body=None, token=OWNER_KEY, scheme="http"):
        self.app = app
        self.body = body
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = SimpleNamespace(host="testclient")
        self.url = SimpleNamespace(scheme=scheme, hostname="testserver")

    async def json(self):
        return self.body


def make_app():
    app = SimpleNamespace(state=SimpleNamespace())
    repo = FakeRepository()
    app.state.wp = SimpleNamespace(repository=repo)
    app.state.settings = SimpleNamespace(api_key=OWNER_KEY,
                                         rules_publisher_api_key="console-publisher-only-secret")
    app.state.round_distribution = Limiter()
    app.state.ready = True
    app.state.rules_engine = SimpleNamespace(status=lambda: {"active_session": {"status": "STOPPED"}})
    return app, repo


def test_generated_keys_are_returned_once_as_plaintext_and_hash_only_is_stored():
    app, repo = make_app()
    response = asyncio.run(developer_console.create_api_key(
        FakeRequest(app, {"name": "Reporting consumer", "project_id": "reporting",
                          "key_type": "consumer", "scopes": ["signals:read"]})))
    assert response["token"].startswith("wpc_")
    stored = repo.keys[0]
    assert stored["token_hash"] == hashlib.sha256(response["token"].encode()).hexdigest()
    assert stored["token_hash"] != response["token"]
    assert "token" not in repo.list_managed_api_keys()[0]
    assert repo.audit[0]["action"] == "CREATED"


def test_consumer_cannot_receive_publisher_write_scope():
    app, _repo = make_app()
    with pytest.raises(HTTPException) as error:
        asyncio.run(developer_console.create_api_key(FakeRequest(app, {
            "name": "bad key", "key_type": "consumer", "scopes": ["signals:write"],
        })))
    assert error.value.status_code == 422
    assert error.value.detail["error"] == "consumer_keys_are_read_only"


def test_publisher_key_is_write_only_and_not_consumer_readable():
    app, repo = make_app()
    result = asyncio.run(developer_console.create_api_key(FakeRequest(app, {
        "name": "rules publisher", "key_type": "publisher", "scopes": ["signals:write"],
    })))
    assert result["scopes"] == ["signals:write"]
    repo.token_hash = hashlib.sha256(result["token"].encode()).hexdigest()
    repo.token = {"token_id": result["token_id"], "key_type": "publisher",
                  "scopes": ["signals:write"], "expires_at": None}
    with pytest.raises(HTTPException) as error:
        signals._authenticate_consumer(FakeRequest(app, token=result["token"]))
    assert error.value.status_code == 401


def test_publisher_credential_can_authenticate_write_endpoint_but_consumer_cannot():
    app, repo = make_app()
    publisher = asyncio.run(developer_console.create_api_key(FakeRequest(app, {
        "name": "rules publisher", "key_type": "publisher", "scopes": ["signals:write"],
    })))
    repo.token_hash = hashlib.sha256(publisher["token"].encode()).hexdigest()
    repo.token = {"token_id": publisher["token_id"], "key_type": "publisher",
                  "scopes": ["signals:write"], "expires_at": None}
    assert signals._require_engine_auth(FakeRequest(app, token=publisher["token"])) is None
    assert repo.token.get("last_used_at")
    repo.token["key_type"] = "consumer"
    with pytest.raises(HTTPException) as error:
        signals._require_engine_auth(FakeRequest(app, token=publisher["token"]))
    assert error.value.status_code == 401


def test_read_scope_is_enforced_and_invalid_key_is_rejected():
    app, repo = make_app()
    consumer_token = "scoped-consumer-token-0123456789ABCDEF"
    repo.token_hash = hashlib.sha256(consumer_token.encode()).hexdigest()
    repo.token = {"token_id": "consumer-1", "project_id": "test-project",
                  "key_type": "consumer", "scopes": ["signals:read"], "expires_at": None}
    request = FakeRequest(app, token=consumer_token)
    assert signals._authenticate_consumer(request, required_scope="signals:read")["token_id"] == "consumer-1"
    with pytest.raises(HTTPException) as error:
        signals._authenticate_consumer(request, required_scope="rules:read")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as invalid:
        signals._authenticate_consumer(FakeRequest(app, token="invalid-read-token-0123456789"))
    assert invalid.value.status_code == 401


def test_expired_consumer_key_is_rejected():
    app, repo = make_app()
    token = "expired-consumer-token-0123456789ABCDEF"
    repo.token_hash = hashlib.sha256(token.encode()).hexdigest()
    repo.token = {"token_id": "expired", "key_type": "consumer", "scopes": ["signals:read"],
                  "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    with pytest.raises(HTTPException) as error:
        signals._authenticate_consumer(FakeRequest(app, token=token))
    assert error.value.status_code == 401


def test_revocation_and_rotation_revoke_old_credential():
    app, repo = make_app()
    created = asyncio.run(developer_console.create_api_key(FakeRequest(app, {
        "name": "external reader", "project_id": "reader", "scopes": ["signals:read"],
    })))
    revoked = asyncio.run(developer_console.revoke_api_key(created["token_id"], FakeRequest(app)))
    assert revoked["revoked"] is True
    assert repo.keys[0]["revoked_at"]
    rotated_old = asyncio.run(developer_console.create_api_key(FakeRequest(app, {
        "name": "rotate me", "project_id": "rotate-me", "scopes": ["signals:read"],
    })))
    result = asyncio.run(developer_console.rotate_api_key(rotated_old["token_id"], FakeRequest(app, {})))
    assert result["token"] != rotated_old["token"]
    assert rotated_old["token_id"] in app.state.round_distribution.revoked
    assert any(row["token_id"] == rotated_old["token_id"] and row["revoked_at"] for row in repo.keys)


def test_console_overview_reports_backend_states_and_no_signal_without_fake_wait():
    app, repo = make_app()
    repo.signals = []
    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))
    assert payload["api_server"]["status"] == "ONLINE"
    assert payload["postgresql"]["status"] == "CONNECTED"
    assert payload["node_publisher"]["status"] == "DISCONNECTED"
    assert payload["has_last_published_signal"] is False
    assert payload["total_bet_signals"] == 0
    assert payload["signal_action"]["action"] is None
    assert payload["signal_action"]["reason"] == "NO_SIGNAL"
    assert payload["signal_action"]["wait_reason"] is None


def test_console_overview_does_not_report_unknown_action_diagnostics_as_negative(monkeypatch):
    app, _repo = make_app()

    async def unavailable(_app):
        raise RuntimeError("private storage details must not be exposed")

    monkeypatch.setattr(developer_console, "evaluate_latest_signal_action", unavailable)
    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))

    diagnostics = payload["signal_action"]
    assert diagnostics["diagnostics_available"] is False
    assert diagnostics["action"] == "UNAVAILABLE"
    assert diagnostics["reason"] == "ACTION_DIAGNOSTICS_UNAVAILABLE"
    assert diagnostics["signal_persisted"] is None
    assert diagnostics["signal_accepted"] is None
    assert diagnostics["validation_passed"] is None
    assert "private storage details" not in str(diagnostics)


def test_console_session_coverage_counts_only_canonically_accepted_signals():
    app, repo = make_app()
    session_id = "active-session"
    decision_id = "decision-1"
    app.state.rules_engine = SimpleNamespace(
        status=lambda: {"active_session": {
            "session_id": session_id, "status": "RUNNING", "selection_status": "ACTIVE",
        }},
        session=lambda _session_id: {"selections": [{
            "decision_id": decision_id, "status": "PENDING",
        }]},
    )
    repo.signals = [{
        "session_id": session_id, "decision_id": decision_id,
        "signal_id": "legacy-shadow-signal",
    }]

    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))

    assert payload["reporting_session_signal_coverage"]["accepted_signal_count"] == 1
    repo.signals[0]["distribution_status"] = "NOT_ACCEPTED"

    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))

    assert payload["reporting_session_signal_coverage"]["accepted_signal_count"] == 0
    repo.signals[0]["distribution_status"] = "ACCEPTED"

    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))

    assert payload["reporting_session_signal_coverage"]["accepted_signal_count"] == 1


def test_publisher_heartbeat_reports_identity_shadow_state_and_queue():
    app, _repo = make_app()
    response = asyncio.run(signals.publisher_heartbeat(FakeRequest(app, {
        "publisher_id": "winner-predict-node-rules",
        "shadow_status": "ACTIVE", "queue_depth": 2,
        "queue_status": "RETRYING", "delivery_failures": 1,
    }, token="console-publisher-only-secret")))
    status = developer_console._publisher_status(FakeRequest(app))
    assert response["publisher"] == "CONNECTED"
    assert response["heartbeat_interval_seconds"] == 15
    assert status["status"] == "CONNECTED"
    assert status["publisher_id"] == "winner-predict-node-rules"
    assert status["shadow_status"] == "ACTIVE"
    assert status["queue_depth"] == 2
    assert status["queue_status"] == "RETRYING"
    assert status["delivery_failures"] == 1


def test_publisher_status_distinguishes_stale_disconnected_and_auth_failure():
    app, _repo = make_app()
    request = FakeRequest(app, token=None)
    assert developer_console._publisher_status(request)["status"] == "DISCONNECTED"
    app.state.rules_publisher_heartbeat = {
        "publisher_id": "node", "shadow_status": "STOPPED",
        "received_at": (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(),
    }
    assert developer_console._publisher_status(request)["status"] == "STALE"
    del app.state.rules_publisher_heartbeat
    app.state.rules_publisher_last_auth_failure_at = datetime.now(timezone.utc).isoformat()
    assert developer_console._publisher_status(request)["status"] == "AUTHENTICATION_FAILED"


def test_admin_owner_key_is_not_accepted_for_publisher_writes():
    app, _repo = make_app()
    with pytest.raises(HTTPException) as error:
        signals._require_engine_auth(FakeRequest(app, token=OWNER_KEY))
    assert error.value.status_code == 401


def test_unconfigured_publisher_rejects_invalid_credential_as_auth_failure():
    app, _repo = make_app()
    app.state.settings.rules_publisher_api_key = None
    with pytest.raises(HTTPException) as error:
        signals._require_engine_auth(FakeRequest(
            app, token="invalid-publisher-credential-with-enough-length"))
    assert error.value.status_code == 401
    assert developer_console._publisher_status(FakeRequest(app, token=None))["status"] == "AUTHENTICATION_FAILED"


def test_console_health_and_diagnostics_do_not_require_owner_credential():
    app, _repo = make_app()
    request = FakeRequest(app, token=None)
    access = asyncio.run(developer_console.console_access(request))
    overview = asyncio.run(developer_console.console_overview(request))
    database = asyncio.run(developer_console.database_diagnostics(request))
    publisher = asyncio.run(developer_console.publisher_diagnostics(request))
    assert access == {"ok": True, "role": "viewer", "api_key_management": False}
    assert overview["api_server"]["status"] == "ONLINE"
    assert "last_published_signal" not in overview
    assert database["database"] == "CONNECTED"
    assert publisher["publisher"]["status"] == "DISCONNECTED"


def test_console_reports_database_unavailable_without_leaking_connection_details():
    app, repo = make_app()
    repo.connect = lambda: (_ for _ in ()).throw(OSError("password=must-not-leak"))
    payload = asyncio.run(developer_console.console_overview(FakeRequest(app)))
    assert payload["api_server"]["status"] == "DEGRADED"
    assert payload["postgresql"]["status"] == "DISCONNECTED"
    assert "must-not-leak" not in repr(payload)


def test_key_management_still_requires_owner_authentication():
    app, _repo = make_app()
    with pytest.raises(HTTPException) as error:
        asyncio.run(developer_console.list_api_keys(FakeRequest(app, token="consumer-key")))
    assert error.value.status_code == 401


def test_developer_console_routes_include_health_metrics_and_key_lifecycle():
    routes = {route.path for route in developer_console.router.routes}
    assert {
        "/api/v1/console/access", "/api/v1/console/overview", "/api/v1/console/signals",
        "/api/v1/diagnostics/database", "/api/v1/diagnostics/publisher", "/api/v1/metrics",
        "/api/v1/decisions/latest", "/api/v1/keys", "/api/v1/keys/{token_id}",
        "/api/v1/keys/{token_id}/rotate",
    }.issubset(routes)


def test_scoped_consumer_key_passes_application_middleware_and_uses_route_scopes(monkeypatch):
    import main as main_module

    app = main_module.app
    repo = FakeRepository()
    read_token = "scoped-consumer-token-0123456789ABCDEF"
    repo.token_hash = hashlib.sha256(read_token.encode()).hexdigest()
    repo.token = {"token_id": "http-consumer", "project_id": "test-project",
                  "key_type": "consumer", "scopes": ["signals:read"], "expires_at": None}
    settings = SimpleNamespace(api_key=OWNER_KEY, rules_publisher_api_key="engine-only-publisher-test-key",
                               public_access=False, public_read_only=False,
                               cors_origins=[], round_stream_allowed_origins=[])
    monkeypatch.setattr(main_module, "settings", settings)
    for name in ("settings", "wp", "round_distribution", "bet_signal_hub", "ready"):
        monkeypatch.setattr(app.state, name, getattr(app.state, name, None), raising=False)
    app.state.settings = settings
    app.state.wp = SimpleNamespace(repository=repo)
    app.state.round_distribution = Limiter()
    app.state.bet_signal_hub = signals.BetSignalHub()
    app.state.ready = True

    async def call(path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get(path, headers=headers)
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            return response, live, ready

    response, live, ready = asyncio.run(call("/api/v1/signals/latest", read_token))
    denied, _, _ = asyncio.run(call("/api/v1/rules/selected", read_token))
    invalid, _, _ = asyncio.run(call("/api/v1/signals/latest", "wrong-consumer-token-0123456789"))
    console, _, _ = asyncio.run(call("/api/v1/console/access"))
    console_overview, _, _ = asyncio.run(call("/api/v1/console/overview"))
    console_feed, _, _ = asyncio.run(call("/api/v1/console/signals", read_token))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "signal": None}
    assert denied.status_code == 403
    assert invalid.status_code == 401
    # The overview contains operational state, so only the owner API_KEY can
    # reach it. Scoped consumers retain access to the signal feed below.
    assert console.status_code == 401
    assert console_overview.status_code == 401
    assert console_feed.status_code == 200
    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["database"]["status"] == "CONNECTED"
