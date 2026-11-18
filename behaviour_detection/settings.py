from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
PROJECT_ROOT = ROOT.parent


def _winner_predict_database_url() -> str:
    for name in ("BEHAVIOUR_SOURCE_DATABASE_URL", "WINNER_PREDICT_SOURCE_DATABASE_URL"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    # start-all.js exports backend/.env, while the standalone npm runtime does
    # not. This value is used only for the read-only canonical history sync.
    backend_env = PROJECT_ROOT / "backend" / ".env"
    if backend_env.is_file():
        value = str(dotenv_values(backend_env).get("DATABASE_URL") or "").strip()
        if value:
            return value
    return os.getenv("DATABASE_URL", "").strip()


def _live_processor_database_url() -> str:
    """Return the existing shared ingress journal URL for live decisions."""
    explicit = os.getenv("BEHAVIOUR_LIVE_PROCESSOR_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    spool_path = os.getenv("COLLECTOR_OBSERVATION_SPOOL_PATH", "").strip()
    backend_env = PROJECT_ROOT / "backend" / ".env"
    from_backend_env = False
    if not spool_path and backend_env.is_file():
        spool_path = str(dotenv_values(backend_env).get("COLLECTOR_OBSERVATION_SPOOL_PATH") or "").strip()
        from_backend_env = bool(spool_path)
    if spool_path:
        path = Path(spool_path).expanduser()
        if not path.is_absolute():
            base = backend_env.parent if from_backend_env else PROJECT_ROOT / "backend"
            path = base / path
    else:
        path = PROJECT_ROOT / "data" / "backend" / "round-observation-inbox.sqlite3"
    return URL.create("sqlite", database=str(path.resolve())).render_as_string(hide_password=False)


def _winner_predict_api_key() -> str:
    explicit = os.getenv("BEHAVIOUR_SHADOW_BACKEND_API_KEY", "").strip()
    if explicit:
        return explicit
    backend_env = PROJECT_ROOT / "backend" / ".env"
    if backend_env.is_file():
        return str(dotenv_values(backend_env).get("API_KEY") or "").strip()
    return ""


def env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def env_float(name: str, default: float, *, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _database_url() -> str:
    configured_url = os.getenv("BEHAVIOUR_DATABASE_URL", "").strip()
    # A local SQLite URL is an explicit no-container development choice and
    # takes precedence over the retained PostgreSQL component credentials.
    if configured_url.startswith("sqlite:///"):
        return configured_url
    # Component credentials are preferred so reserved password characters are
    # safely escaped by SQLAlchemy instead of hand-built into a URL.
    user = os.getenv("BEHAVIOUR_DB_USER", "").strip()
    password = os.getenv("BEHAVIOUR_DB_PASSWORD", "")
    database = os.getenv("BEHAVIOUR_DB_NAME", "").strip()
    if user and password and database:
        return URL.create(
            "postgresql+psycopg", username=user, password=password,
            host=os.getenv("BEHAVIOUR_DB_HOST", "127.0.0.1").strip(),
            port=env_int("BEHAVIOUR_DB_PORT", 5432, low=1, high=65535),
            database=database,
        ).render_as_string(hide_password=False)
    return configured_url


DATABASE_URL = _database_url()
LIVE_PROCESSOR_DATABASE_URL = _live_processor_database_url()
WINNER_PREDICT_DATABASE_URL = _winner_predict_database_url()
WINNER_PREDICT_SYNC_SECONDS = env_float(
    "BEHAVIOUR_WINNER_SYNC_SECONDS", 1.0, low=0.5, high=60.0
)
ORBIT_BASE_URL = (os.getenv("ORBIT_API_BASE_URL") or
                  os.getenv("BEHAVIOUR_ORBIT_API_BASE_URL", "")).strip()
ORBIT_API_KEY = (os.getenv("ORBIT_API_KEY") or
                 os.getenv("BEHAVIOUR_ORBIT_API_KEY", "")).strip()
ORBIT_WATCH_ID = os.getenv("BEHAVIOUR_ORBIT_WATCH_ID", "").strip() or None
ORBIT_POLL_SECONDS = env_float("BEHAVIOUR_ORBIT_POLL_SECONDS", 2.0, low=1.0, high=60.0)
ORBIT_PAGE_SIZE = env_int("BEHAVIOUR_ORBIT_PAGE_SIZE", 100, low=1, high=500)
ORBIT_CONNECT_TIMEOUT_SECONDS = env_float(
    "BEHAVIOUR_ORBIT_CONNECT_TIMEOUT_SECONDS", 5.0, low=0.1, high=60.0
)
ORBIT_REQUEST_TIMEOUT_SECONDS = env_float(
    "BEHAVIOUR_ORBIT_REQUEST_TIMEOUT_SECONDS", 15.0, low=0.1, high=180.0
)
API_HOST = os.getenv("BEHAVIOUR_API_HOST", "127.0.0.1")
API_PORT = env_int("BEHAVIOUR_API_PORT", 8100, low=1, high=65535)
SHADOW_BACKEND_URL = os.getenv("BEHAVIOUR_SHADOW_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
SHADOW_SHARED_SECRET = os.getenv("BEHAVIOUR_SHADOW_SHARED_SECRET", "")
SHADOW_BACKEND_API_KEY = _winner_predict_api_key()
TIME_GROUP_GAP_SECONDS = env_float(
    "BEHAVIOUR_TIME_GROUP_GAP_SECONDS", 180.0452, low=1.0, high=86400.0
)
STATE_CONFIRM_ROUNDS = env_int("BEHAVIOUR_STATE_CONFIRM_ROUNDS", 3, low=1, high=100)
MIN_STATE_ROUNDS = env_int("BEHAVIOUR_MIN_STATE_ROUNDS", 20, low=2, high=500)
IMPORT_BATCH_SIZE = env_int("BEHAVIOUR_IMPORT_BATCH_SIZE", 1000, low=100, high=10000)
LOG_LEVEL = os.getenv("BEHAVIOUR_LOG_LEVEL", "INFO").upper()


def validation_errors() -> list[str]:
    errors: list[str] = []
    if not DATABASE_URL:
        errors.append("BEHAVIOUR_DATABASE_URL is missing; no Winner Predict fallback is allowed")
    elif not (DATABASE_URL.startswith("postgresql+psycopg://") or DATABASE_URL.startswith("sqlite:///")):
        errors.append("BEHAVIOUR_DATABASE_URL must use PostgreSQL+psycopg or a local SQLite development file")
    if WINNER_PREDICT_DATABASE_URL:
        if not WINNER_PREDICT_DATABASE_URL.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
            errors.append("Winner Predict source database must use PostgreSQL")
    if bool(ORBIT_BASE_URL) != bool(ORBIT_API_KEY):
        errors.append("Set ORBIT_API_BASE_URL (or BEHAVIOUR_ORBIT_API_BASE_URL) and an independent Orbit API key")
    if ORBIT_BASE_URL and not ORBIT_BASE_URL.startswith((
            "https://", "http://localhost", "http://127.0.0.1", "http://[::1]",
            "http://host.containers.internal", "http://host.docker.internal")):
        errors.append("Orbit base URL must use HTTPS, except local development endpoints")
    return errors
