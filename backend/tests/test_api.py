import asyncio

import httpx

from app.betting.config import get_betting_settings
from app.betting.session import BettingManager
from app.core.config import get_settings
from app.services.app_state import AppState
from main import app


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
