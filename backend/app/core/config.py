from functools import lru_cache
from pathlib import Path

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
    signal_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    strong_signal_threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    data_path: Path = Path(__file__).resolve().parents[2] / "data" / "roundhistory.json"
    database_path: Path = Path(__file__).resolve().parents[2] / "winner_predict.sqlite3"
    model_dir: Path = Path(__file__).resolve().parents[2] / "trained_models"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()

