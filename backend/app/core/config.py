from functools import lru_cache
from pathlib import Path
from typing import Optional
import tempfile

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration.

    Values are read from environment variables and from a ``.env`` file in the
    backend/ working directory (see .env.example). Defaults mirror the paths
    that live inside the backend/ directory.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Winner Predict"
    history_status_window_rounds: int = Field(default=100, ge=1)
    # Kept for one-time legacy import and isolated SQLite tests. In production
    # the canonical history is PostgreSQL's aviator_rounds table.
    data_path: Path = Path(__file__).resolve().parents[3] / "data" / "roundhistory.json"
    database_path: Path = Path(__file__).resolve().parents[2] / "winner_predict.sqlite3"
    database_url: Optional[str] = Field(default=None, description="Authoritative PostgreSQL DSN in production")
    require_postgres: bool = Field(default=True, description="Fail startup when authoritative PostgreSQL is unavailable")
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    round_stream_allowed_origins: list[str] = Field(default_factory=list)
    api_key: str | None = Field(default=None, min_length=16)
    public_access: bool = Field(default=False, description="Allow unauthenticated access to dashboard reads and actions")
    public_read_only: bool = Field(default=False, description="Allow unauthenticated dashboard reads while protecting writes")
    backend_host: str = "0.0.0.0"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    enforce_single_instance: bool = True
    instance_lock_path: Path = Path(tempfile.gettempdir()) / "winner-predict-backend-8000.lock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
