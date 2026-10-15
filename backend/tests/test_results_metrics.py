import asyncio
import json

from fastapi import FastAPI
from starlette.requests import Request

from app.api.results import router, session_metrics


def test_optional_bankroll_session_analytics_endpoint_is_retired():
    app = FastAPI()
    app.include_router(router)
    request = Request({
        "type": "http", "method": "GET", "path": "/api/results/session-metrics",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })

    response = asyncio.run(session_metrics(request))

    assert response.status_code == 410
    body = json.loads(response.body)
    assert body["error"] == "OPTIONAL_BANKROLL_ANALYTICS_REMOVED"
