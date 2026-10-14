from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api import betting_mode as betting_mode_api
from app.database.repository import Repository
from app.services.betting_mode import BettingModeManager, SHADOW_REALISTIC
from app.live import LiveActivationManager
from app.services.betting_mode import LIVE_MODE_AUTHORIZATION


def _request(app: FastAPI, authorization: str | None = None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({
        "type": "http", "method": "POST", "path": "/api/betting-mode",
        "headers": headers, "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })


def test_public_dashboard_access_does_not_bypass_live_operator_auth(monkeypatch):
    class Modes:
        mode = SHADOW_REALISTIC

        async def switch_live(self, *_args, **_kwargs):
            raise AssertionError("unauthorized activation must not reach mode switching")

    app = FastAPI()
    app.state.betting_mode = Modes()
    monkeypatch.setattr(betting_mode_api, "get_settings", lambda: SimpleNamespace(
        api_key=None, public_access=True,
    ))

    with pytest.raises(HTTPException) as missing_key:
        asyncio.run(betting_mode_api.switch_mode(_request(app), {"mode": "LIVE_REAL"}))
    assert missing_key.value.status_code == 503

    monkeypatch.setattr(betting_mode_api, "get_settings", lambda: SimpleNamespace(
        api_key="a-configured-backend-key", public_access=True,
    ))
    with pytest.raises(HTTPException) as missing_bearer:
        asyncio.run(betting_mode_api.switch_mode(_request(app), {"mode": "LIVE_REAL"}))
    assert missing_bearer.value.status_code == 401

    identity = asyncio.run(betting_mode_api._require_live_credentials(
        _request(app, "Bearer a-configured-backend-key"),
    ))
    assert identity.startswith("api-key:")
    assert "a-configured-backend-key" not in identity


def test_live_manager_requires_authenticated_operator_identity(tmp_path):
    repository = Repository(tmp_path / "operator-auth.sqlite3")
    repository.init()
    app = SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(repository=repository)))
    modes = BettingModeManager(app)
    modes._reviews["single-use-review"] = {
        "expires_monotonic": time.monotonic() + 60,
    }

    result = asyncio.run(modes.switch_live({
        "review_id": "single-use-review",
        "confirmation": "ENABLE LIVE BETTING",
    }))

    assert result["ok"] is False
    assert result["error"] == "operator_authorization_required"
    assert modes.mode == SHADOW_REALISTIC
    assert modes.live_authorized is False


def test_live_controller_rejects_unidentified_direct_activation():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))
    result = asyncio.run(controller.start({
        "confirmation": "ENABLE LIVE BETTING",
    }, _mode_authorization=LIVE_MODE_AUTHORIZATION))

    assert result["ok"] is False
    assert result["error"] == "operator_authorization_required"
    assert controller.live_active is False
