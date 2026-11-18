from fastapi.testclient import TestClient

from behaviour_detection.api import create_app
from behaviour_detection.db import make_engine


def test_liveness_is_independent_and_missing_analysis_database_keeps_live_ready(monkeypatch, tmp_path):
    monkeypatch.setattr("behaviour_detection.api.settings.DATABASE_URL", "")
    monkeypatch.setattr("behaviour_detection.api.settings.LIVE_PROCESSOR_DATABASE_URL",
                        f"sqlite:///{tmp_path / 'live-only.sqlite3'}")
    with TestClient(create_app()) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["service"] == "advanced-crash-behaviour"
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"ok": True, "state": "READY_DATABASE_OFFLINE",
                                "database": "offline", "live_processor": "ready"}
        overview = client.get("/api/v1/behaviour/overview")
        assert overview.status_code == 503
        assert overview.json()["detail"]["code"] == "BEHAVIOUR_DATABASE_UNCONFIGURED"


def test_data_routes_fail_closed_when_schema_migration_failed(monkeypatch):
    engine = make_engine("sqlite:///:memory:")

    def fail_migration(_engine):
        raise RuntimeError("fixture migration failure")

    monkeypatch.setattr("behaviour_detection.api.migrate", fail_migration)
    with TestClient(create_app(engine)) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        overview = client.get("/api/v1/behaviour/overview")
        assert live.status_code == 200
        assert ready.status_code == 503
        assert ready.json()["detail"]["ok"] is False
        assert overview.status_code == 503
        assert overview.json()["detail"]["code"] == "BEHAVIOUR_SCHEMA_NOT_READY"
    engine.dispose()


def test_collector_status_discloses_missing_independent_orbit_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr("behaviour_detection.api.settings.WINNER_PREDICT_DATABASE_URL", "")
    monkeypatch.setattr("behaviour_detection.api.settings.ORBIT_BASE_URL", "")
    monkeypatch.setattr("behaviour_detection.api.settings.ORBIT_API_KEY", "")
    engine = make_engine(f"sqlite:///{tmp_path / 'api.sqlite3'}")
    with TestClient(create_app(engine)) as client:
        response = client.get("/api/v1/behaviour/collector")
        assert response.status_code == 200
        assert response.json()["health"]["orbit_collector"] == {
            "state": "NOT_CONFIGURED", "checked_at": None,
            "details": {"reason": "independent Behaviour Orbit credentials are not configured"},
        }
    engine.dispose()
