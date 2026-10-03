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
    target_multiplier: float = Field(default=2.0, ge=1.0)
    min_sample_size: int = Field(default=30, ge=1)
    ml_max_history_age: int = Field(default=600, gt=0, description="Maximum history age in seconds for ML inference (ML_MAX_HISTORY_AGE)")
    readiness_required_rounds: int = Field(default=100, ge=1, description="Continuous rounds required before live inference is eligible")
    readiness_auto_train: bool = Field(default=True, description="Run one existing ML evaluation when a continuity window first becomes ready")
    ml_retrain_min_new_rounds: int = Field(default=250, ge=1)
    ml_retrain_cooldown_s: int = Field(default=3600, ge=0)
    ml_model_concurrency: int = Field(default=1, ge=1, le=4, description="Maximum concurrent candidate models per evaluation cycle")
    signal_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    strong_signal_threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    # Kept for one-time legacy import and isolated SQLite tests. In production
    # the canonical history is PostgreSQL's aviator_rounds table.
    data_path: Path = Path(__file__).resolve().parents[3] / "data" / "roundhistory.json"
    processed_data_dir: Path = Path(__file__).resolve().parents[3] / "data" / "processed"
    features_data_dir: Path = Path(__file__).resolve().parents[3] / "data" / "features"
    database_path: Path = Path(__file__).resolve().parents[2] / "winner_predict.sqlite3"
    database_url: Optional[str] = Field(default=None, description="Authoritative PostgreSQL DSN in production")
    require_postgres: bool = Field(default=True, description="Fail startup when authoritative PostgreSQL is unavailable")
    model_dir: Path = Path(__file__).resolve().parents[2] / "trained_models"
    decision_path: Path = Path(__file__).resolve().parents[3] / "data" / "decision.json"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    api_key: str | None = Field(default=None, min_length=16)
    public_read_only: bool = Field(default=False, description="Allow unauthenticated dashboard reads while protecting writes")
    backend_host: str = "0.0.0.0"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    enforce_single_instance: bool = True
    instance_lock_path: Path = Path(tempfile.gettempdir()) / "winner-predict-backend-8000.lock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
