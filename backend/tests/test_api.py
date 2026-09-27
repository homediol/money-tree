import asyncio

import httpx

from app.betting.config import get_betting_settings
from app.betting.session import BettingManager
from app.core.config import get_settings
from app.services.app_state import AppState
from main import app
from main import settings as app_settings


def test_status_endpoint():
    async def go():
        app.state.wp = AppState(get_settings())
        app.state.betting = BettingManager(get_betting_settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/status")
        await app.state.betting.shutdown()
        return response

    response = asyncio.run(go())
    assert response.status_code == 200
    assert response.json()["label"] == "STATISTICAL PATTERN ANALYSIS"


def test_api_key_authentication_when_configured():
    async def go():
        original = app_settings.api_key
        app_settings.api_key = "test-key-with-16-characters"
        try:
            app.state.wp = AppState(get_settings())
            app.state.betting = BettingManager(get_betting_settings())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                health = await client.get("/health")
                denied = await client.get("/api/status")
                allowed = await client.get(
                    "/api/status",
                    headers={"Authorization": "Bearer test-key-with-16-characters"},
                )
            await app.state.betting.shutdown()
            return health, denied, allowed
        finally:
            app_settings.api_key = original

    health, denied, allowed = asyncio.run(go())
    assert health.json()["auth_required"] is True
    assert denied.status_code == 401
    assert allowed.status_code == 200
