from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
from types import SimpleNamespace

import httpx

import main as main_module


OWNER_KEY = "owner-api-key-for-contract-tests"
PROJECT_TOKEN = "wpr_public-round-test-token-0123456789"


class Limiter:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.requests = []

    def allow_request(self, key):
        self.requests.append(key)
        return self.allowed, 23


class RoundRepository:
    database_url = "postgresql://public-api-test"

    def __init__(self):
        self.events = [
            {"stream_sequence": 1, "round_id": "round-a", "multiplier": 1.4,
             "event_type": "round.persisted"},
            {"stream_sequence": 2, "round_id": "round-b", "multiplier": 2.1,
             "event_type": "round.persisted"},
            {"stream_sequence": 3, "round_id": "round-c", "multiplier": 4.2,
             "event_type": "round.persisted"},
        ]
        self.signals = []
        self.token_hash = hashlib.sha256(PROJECT_TOKEN.encode()).hexdigest()

    def list_bet_signals(self, limit=100, *, session_id=None):
        return self.signals[:limit]

    def round_distribution_snapshot(self):
        return {"stream_id": "stream-test", "latest": self.events[-1],
                "earliest_sequence": 1, "latest_sequence": 3, "retained_events": 3}

    def round_distribution_events_page(self, _stream_id, *, through_sequence, limit,
                                       after_sequence=0, before_sequence=None, search=None,
                                       min_multiplier=None, max_multiplier=None, order="asc"):
        rows = [item for item in self.events if item["stream_sequence"] <= through_sequence]
        if order == "asc":
            rows = [item for item in rows if item["stream_sequence"] > after_sequence]
        elif before_sequence is not None:
            rows = [item for item in rows if item["stream_sequence"] < before_sequence]
        if search:
            rows = [item for item in rows if search.lower() in item["round_id"].lower()]
        if min_multiplier is not None:
            rows = [item for item in rows if item["multiplier"] >= min_multiplier]
        if max_multiplier is not None:
            rows = [item for item in rows if item["multiplier"] <= max_multiplier]
        rows.sort(key=lambda item: item["stream_sequence"], reverse=order == "desc")
        return rows[:limit]

    def external_project_token(self, token_hash):
        if token_hash != self.token_hash:
            return None
        return {"token_id": "consumer-test", "project_id": "analytics",
                "key_type": "consumer", "scopes": ["rounds:read"], "expires_at": None}

    def touch_external_project_token(self, _token_id):
        return None


def configure_main_app(monkeypatch, *, api_key=OWNER_KEY, public_flags=True, limiter=None):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    app = main_module.app
    settings = SimpleNamespace(
        api_key=api_key,
        public_access=public_flags,
        public_read_only=public_flags,
        cors_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                      "http://localhost:5174", "http://127.0.0.1:5174"],
        round_stream_allowed_origins=[],
    )
    repository = RoundRepository()
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(app.state, "settings", settings, raising=False)
    monkeypatch.setattr(app.state, "wp", SimpleNamespace(repository=repository), raising=False)
    monkeypatch.setattr(app.state, "round_distribution", limiter or Limiter(), raising=False)
    return app, repository


async def get(app, path, *, base_url="http://127.0.0.1", token=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=base_url) as client:
        return await client.get(path, headers=request_headers)


async def post(app, path, *, base_url="http://127.0.0.1", token=None, headers=None, body=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=base_url) as client:
        return await client.post(path, headers=request_headers, json=body)


def test_committed_round_endpoints_are_keyless_and_return_database_rows(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    latest = asyncio.run(get(app, "/api/v1/rounds/latest"))
    page = asyncio.run(get(app, "/api/v1/rounds?after_sequence=1&stream_id=stream-test&limit=1"))

    assert latest.status_code == 200
    assert latest.json()["round"]["round_id"] == "round-c"
    assert latest.json()["project_id"] is None
    assert page.status_code == 200
    assert page.json()["rounds"] == [{"stream_sequence": 2, "round_id": "round-b",
                                       "multiplier": 2.1, "event_type": "round.persisted"}]
    assert page.json()["next_cursor"] == 2
    assert page.json()["has_more"] is True


def test_latest_signal_action_is_anonymous_and_projects_canonical_signal(monkeypatch):
    app, repository = configure_main_app(monkeypatch)
    repository.signals = [{"action": "BET", "target_round_index": 20423,
                           "signal_id": "must-not-be-returned"}]

    response = asyncio.run(get(app, "/api/v1/signals/latest/action"))

    assert response.status_code == 200
    assert response.json() == {"action": "OK", "index": "#20423"}
    assert set(response.json()) == {"action", "index"}


def test_latest_signal_action_no_data_matches_latest_signal_contract(monkeypatch):
    app, _repository = configure_main_app(monkeypatch)

    response = asyncio.run(get(app, "/api/v1/signals/latest/action"))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "signal": None}


def test_public_round_search_filters_and_descending_cursor_work(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    filtered = asyncio.run(get(app, "/api/v1/rounds?search=round-b&min_multiplier=2"))
    descending = asyncio.run(get(app, "/api/v1/rounds?order=desc&limit=2"))
    resumed = asyncio.run(get(app, "/api/v1/rounds?order=desc&before_sequence=2&stream_id=stream-test"))

    assert filtered.status_code == 200
    assert [row["round_id"] for row in filtered.json()["rounds"]] == ["round-b"]
    assert descending.status_code == 200
    assert [row["round_id"] for row in descending.json()["rounds"]] == ["round-c", "round-b"]
    assert descending.json()["cursor_direction"] == "before"
    assert resumed.status_code == 200
    assert [row["round_id"] for row in resumed.json()["rounds"]] == ["round-a"]


def test_legacy_public_flags_do_not_open_private_routes_or_writes(monkeypatch):
    app, _repo = configure_main_app(monkeypatch, public_flags=True)

    private_read = asyncio.run(get(app, "/api/v1/console/overview"))
    private_write = asyncio.run(post(app, "/api/v1/rounds", body={"round_id": "external"}))

    assert private_read.status_code == 401
    assert private_read.json()["error"] == "authentication_required"
    assert private_write.status_code == 401


def test_local_dashboard_bootstrap_uses_an_httponly_session_without_returning_owner_key(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            bootstrap = await client.post("/api/auth/local-browser-session",
                                          headers={"Origin": "http://localhost:5174"})
            access = await client.get("/api/v1/console/access",
                                      headers={"Origin": "http://localhost:5174"})
            return bootstrap, access

    bootstrap, access = asyncio.run(scenario())
    cookie_header = bootstrap.headers.get("set-cookie", "").lower()
    assert bootstrap.status_code == 200
    assert bootstrap.json() == {
        "ok": True, "session": "LOCAL_BROWSER",
        "expires_in_seconds": main_module.LOCAL_BROWSER_SESSION_TTL_SECONDS,
    }
    assert "httponly" in cookie_header
    assert "samesite=strict" in cookie_header
    assert OWNER_KEY not in bootstrap.text
    assert access.status_code == 200
    assert access.json() == {"ok": True, "role": "owner", "api_key_management": True}


def test_local_dashboard_session_cannot_bootstrap_or_authorize_from_remote_host(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as local_client:
            issued = await local_client.post("/api/auth/local-browser-session",
                headers={"Origin": "http://localhost:5174"})
        cookie = issued.cookies.get(main_module.LOCAL_BROWSER_SESSION_COOKIE)
        transport = httpx.ASGITransport(app=app, client=("198.51.100.25", 44321))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="https://api.example.test") as remote_client:
            bootstrap = await remote_client.post("/api/auth/local-browser-session",
                headers={"Origin": "http://localhost:5174"})
            private = await remote_client.get("/api/v1/console/overview", headers={
                "Origin": "http://localhost:5174",
                "Cookie": f"{main_module.LOCAL_BROWSER_SESSION_COOKIE}={cookie}",
            })
        return bootstrap, private

    bootstrap, private = asyncio.run(scenario())
    assert bootstrap.status_code == 403
    assert bootstrap.json()["error"] == "local_browser_session_not_allowed"
    assert private.status_code == 401
    assert private.json()["error"] == "authentication_required"


def test_local_dashboard_session_is_bound_to_the_bootstrap_origin(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            issued = await client.post("/api/auth/local-browser-session",
                                       headers={"Origin": "http://localhost:5174"})
            same_origin = await client.get("/api/v1/console/access",
                                           headers={"Origin": "http://localhost:5174"})
            other_local_app = await client.get("/api/v1/console/access",
                                               headers={"Origin": "http://localhost:5173"})
            return issued, same_origin, other_local_app

    issued, same_origin, other_local_app = asyncio.run(scenario())
    assert issued.status_code == 200
    assert same_origin.status_code == 200
    assert other_local_app.status_code == 401


def test_scoped_round_api_uses_its_token_without_owner_key(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    response = asyncio.run(get(app, "/api/v1/rounds/clock", token=PROJECT_TOKEN))

    assert response.status_code == 200
    assert response.json()["clock_reference"] == "API_SYSTEM_UTC"


def test_remote_private_api_fails_closed_when_owner_key_is_unconfigured(monkeypatch):
    app, _repo = configure_main_app(monkeypatch, api_key=None, public_flags=True)

    response = asyncio.run(get(app, "/api/live/status", base_url="https://api.example.test"))

    assert response.status_code == 503
    assert response.json()["error"] == "api_key_not_configured"


def test_public_reads_require_https_remotely_and_reject_unlisted_origins(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    insecure = asyncio.run(get(app, "/api/v1/rounds/latest", base_url="http://api.example.test"))
    origin = asyncio.run(get(app, "/api/v1/rounds/latest", base_url="https://api.example.test",
                            headers={"Origin": "https://unlisted.example"}))

    assert insecure.status_code == 426
    assert origin.status_code == 403
    assert origin.json()["error"] == "origin_not_allowed"


def test_public_reads_allow_configured_browser_origin(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)

    response = asyncio.run(get(app, "/api/v1/rounds/latest",
                               headers={"Origin": "http://localhost:5173"}))

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_public_route_validation_and_rate_limit_use_json_errors(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)
    invalid = asyncio.run(get(app, "/api/v1/rounds?limit=0"))
    limited_app, _repo = configure_main_app(monkeypatch, limiter=Limiter(allowed=False))
    limited = asyncio.run(get(limited_app, "/api/v1/rounds/latest"))

    assert invalid.status_code == 422
    assert invalid.json()["ok"] is False
    assert invalid.json()["error"] == "validation_error"
    assert limited.status_code == 429
    assert limited.json()["error"] == "public_rate_limit_exceeded"
    assert limited.headers["Retry-After"] == "23"


def test_openapi_marks_explicit_public_reads_as_anonymous(monkeypatch):
    app, _repo = configure_main_app(monkeypatch)
    response = asyncio.run(get(app, "/openapi.json"))
    schema = response.json()

    assert response.status_code == 200
    assert schema["paths"]["/api/v1/rounds"]["get"]["security"] == []
    assert schema["paths"]["/api/v1/rounds/latest"]["get"]["x-api-key-required"] is False
    assert schema["paths"]["/api/v1/signals/latest/action"]["get"]["security"] == []
    assert schema["paths"]["/api/v1/signals/latest/action"]["get"]["x-public-access"] is True
    assert schema["paths"]["/api/v1/signals/latest/action"]["get"]["x-api-key-required"] is False
    assert schema["paths"]["/api/live/status"]["get"]["security"] == [{"BearerAuth": []}]
    assert schema["paths"]["/api/v1/rounds/stream"]["get"]["x-api-key-required"] is False


def test_bot_control_endpoints_remain_private(monkeypatch):
    # Avoid creating or modifying the service's runtime log while importing it.
    original_file_handler = logging.FileHandler
    monkeypatch.setattr(logging, "FileHandler", lambda *_args, **_kwargs: logging.NullHandler())
    bot_api = importlib.import_module("bot_api")
    monkeypatch.setattr(logging, "FileHandler", original_file_handler)
    monkeypatch.setattr(bot_api, "bot_settings", SimpleNamespace(
        api_key=OWNER_KEY,
        cors_origins=["http://localhost:5173"],
        round_stream_allowed_origins=[],
    ))
    monkeypatch.setattr(bot_api.bot_runner, "get_status", lambda: {"status": "STOPPED"})

    async def call(path, *, base_url="http://127.0.0.1", token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot_api.app),
                                     base_url=base_url) as client:
            return await client.get(path, headers=headers)

    async def oversized_start():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot_api.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.post("/bot/start",
                                     headers={"Authorization": f"Bearer {OWNER_KEY}",
                                              "Content-Length": "65537"},
                                     content="{}")

    anonymous = asyncio.run(call("/bot/status"))
    root = asyncio.run(call("/"))
    remote_http = asyncio.run(call("/bot/status", base_url="http://api.example.test"))
    oversized = asyncio.run(oversized_start())

    assert anonymous.status_code == 401
    assert root.status_code == 200
    assert remote_http.status_code == 426
    assert oversized.status_code == 413
