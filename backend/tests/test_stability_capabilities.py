import asyncio

from fastapi import FastAPI
from starlette.requests import Request

from app.api.stability import capabilities, router


def test_backend_exposes_current_manual_executor_and_integrity_capabilities():
    app = FastAPI()
    app.include_router(router)

    result = asyncio.run(capabilities(Request({
        "type": "http", "method": "GET", "path": "/api/backend/capabilities",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })))

    assert result["capabilities"]["manual_platform_stake_readback_v2"] is True
    assert result["capabilities"]["manual_executor_v2"] is True
    assert result["capabilities"]["collector_panel_configuration_readback_v1"] is True
    assert result["capabilities"]["real_money_execution_disabled"] is True
    assert "live_activation_balance_exposure_gate_v1" not in result["capabilities"]
