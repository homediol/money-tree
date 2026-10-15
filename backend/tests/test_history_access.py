from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

import main
from app.api import history
from app.history_access import is_public_history_request


def _request(path: str, method: str, app) -> Request:
    return Request({
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 123), "scheme": "http", "app": app,
    })


def test_history_api_key_exemption_is_narrow_and_method_specific():
    assert is_public_history_request("GET", "/api/history/admin-status")
    assert is_public_history_request("POST", "/api/history/exports")
    assert is_public_history_request("POST", "/api/history/clear")
    export_id = str(uuid4())
    assert is_public_history_request("GET", f"/api/history/exports/{export_id}")
    assert is_public_history_request("POST", f"/api/history/exports/{export_id}/verify")
    assert is_public_history_request("GET", f"/api/history/exports/{export_id}/download")
    assert not is_public_history_request("DELETE", "/api/history/clear")
    assert not is_public_history_request("POST", "/api/history/admin-status")
    assert not is_public_history_request("GET", "/api/history/exports/not-a-uuid")
    assert not is_public_history_request("POST", f"/api/history/exports/{export_id}/download")
    assert not is_public_history_request("GET", "/api/betting/status")
    assert not is_public_history_request("POST", "/api/betting/session")


def test_key_middleware_allows_history_without_opening_other_api_routes(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(
        api_key="configured-test-api-key", public_access=False, public_read_only=False,
    ))
    app = SimpleNamespace()
    history_request = _request("/api/history/clear", "POST", app)
    called = []

    async def next_handler(_request):
        called.append(True)
        return Response("reached", status_code=204)

    response = asyncio.run(main.require_api_key(history_request, next_handler))
    assert response.status_code == 204
    assert called == [True]

    private_request = _request("/api/betting/session", "POST", app)
    blocked = asyncio.run(main.require_api_key(private_request, next_handler))
    assert blocked.status_code == 401
    assert called == [True]


def test_history_routes_have_no_admin_key_gate_and_clear_still_requires_verified_export(
    tmp_path, monkeypatch,
):
    export_id = str(uuid4())
    artifact = tmp_path / "data" / "history_exports" / f"round-history-{export_id}.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"rounds":[]}', encoding="utf-8")
    manifest = {
        "export_id": export_id, "format": "json", "record_count": 0,
        "checksum_sha256": "a" * 64, "status": "COMPLETED",
    }

    app = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(data_path=tmp_path / "data" / "state.json"),
        wp=SimpleNamespace(repository=object()),
        betting_mode=SimpleNamespace(mode="SHADOW_REALISTIC"),
        live=SimpleNamespace(live_active=False),
    ))
    monkeypatch.setattr(history, "_collector_runtime_evidence", lambda _request: {})
    monkeypatch.setattr(history, "history_admin_status", lambda *_args, **_kwargs: {
        "total_records": 0, "history_epoch": 1,
    })
    monkeypatch.setattr(history, "export_round_history", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(history, "export_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(history, "verify_round_export", lambda _repo, _data_path, candidate: {
        "ok": candidate == export_id, "record_count": 0,
    })

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(history.asyncio, "to_thread", inline_to_thread)

    assert history.history_admin_dashboard_status(
        _request("/api/history/admin-status", "GET", app),
    )["total_records"] == 0
    assert history.create_history_export(
        _request("/api/history/exports", "POST", app), {"format": "json"},
    ) == manifest
    assert history.read_history_export_manifest(
        _request(f"/api/history/exports/{export_id}", "GET", app), export_id,
    ) == manifest
    assert history.verify_history_export(
        _request(f"/api/history/exports/{export_id}/verify", "POST", app), export_id,
    )["ok"] is True
    downloaded = history.download_history_export(
        _request(f"/api/history/exports/{export_id}/download", "GET", app), export_id,
    )
    assert downloaded.path == artifact

    # Invalid exports remain blocked by the existing safety gate. The test
    # never invokes the database clear operation.
    with pytest.raises(HTTPException) as clear:
        asyncio.run(history.clear_history(
            _request("/api/history/clear", "POST", app),
            {"export_id": "not-a-valid-export"},
        ))
    assert clear.value.status_code == 409
