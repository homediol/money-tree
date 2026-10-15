import asyncio

from fastapi import FastAPI
from starlette.requests import Request

from app.api.stability import capabilities, router


def test_backend_exposes_manual_setup_readback_compatibility_capabilities():
    app = FastAPI()
    app.include_router(router)

    result = asyncio.run(capabilities(Request({
        "type": "http", "method": "GET", "path": "/api/backend/capabilities",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })))

    assert result["capabilities"]["manual_platform_stake_readback_v2"] is True
    assert result["capabilities"]["manual_platform_setup_readback_v3"] is True
