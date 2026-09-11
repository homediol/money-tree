"""
Centralised configuration for the Aviator ML Enterprise Platform.

This module loads, validates and exposes every runtime knob used across the
codebase.  All other modules MUST import settings from here — no module
should re-parse environment variables on its own.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Strongly-typed application configuration."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application --
    app_name: str = "aviator-ml-enterprise"
    app_version: str = "2.0.0"
    app_env: str = "production"
    debug: bool = False
    log_level: str = "INFO"
    secret_key: str = "change-me"

    # -- Server --
    host: str = "0.0.0.0"
    port: int = 8002  # 8000 now belongs to the Winner Predict backend
    workers: int = 4
    cors_origins: str = "*"

    # -- Paths --
    artifacts_dir: Path = ARTIFACTS_DIR
    data_dir: Path = DATA_DIR
    models_dir: Optional[Path] = None
    datasets_dir: Optional[Path] = None
    logs_dir: Optional[Path] = None
    reports_dir: Optional[Path] = None

    # -- Data source (always local roundhistory.json) --
    round_history_path: Path = PROJECT_ROOT.parent / "data" / "roundhistory.json"

    # -- Model configuration --
    sequence_length: int = 24
    forecast_horizon: int = 1
    n_classes: int = 5
    classification_mode: str = "multi_class"

    # -- Feature engineering --
    rolling_windows: str = "5,10,20,50,100"
    ema_spans: str = "5,10,20,50"
    lag_periods: str = "1,2,3,5,10"
    volatility_windows: str = "10,20,50"

    # -- Training --
    test_size: float = 0.2
    validation_size: float = 0.15
    cv_folds: int = 5
    n_bootstrap: int = 200
    optuna_trials: int = 50
    optuna_timeout: int = 1800
    early_stopping_patience: int = 10
    batch_size: int = 64
    max_epochs: int = 100

    # -- Class imbalance --
    imbalance_strategy: str = "class_weight"
    smote_ratio: str = "auto"

    # -- Calibration --
    calibration_method: str = "temperature"
    calibration_split: float = 0.2

    # -- Drift detection --
    drift_window: int = 200
    drift_threshold: float = 0.05
    drift_check_interval: int = 300
    retrain_min_new_rounds: int = 50
    retrain_min_improvement: float = 0.01

    # -- Uncertainty --
    uncertainty_threshold: float = 0.55
    min_confidence: float = 0.35
    max_uncertainty: float = 0.25

    # -- Ensemble --
    ensemble_method: str = "stacking"
    ensemble_weights: str = "auto"

    # -- Dashboard --
    dashboard_refresh_ms: int = 3000
    prediction_log_size: int = 500
    dashboard_history_days: int = 30

    # -- Categories (multi-class crash bucket boundaries) --
    category_boundaries: str = "1.50,2.00,5.00,15.00"
    category_names: str = "VERY_LOW,LOW,MEDIUM,HIGH,VERY_HIGH"

    # -- Risk thresholds --
    risk_bands: str = "LOW:0.0-0.3,MEDIUM:0.3-0.6,HIGH:0.6-0.8,EXTREME:0.8-1.0"

    # -- Recommendation thresholds --
    bet_confidence_strong: float = 0.70
    bet_confidence_moderate: float = 0.55
    bet_confidence_weak: float = 0.40

    # ------------------------------------------------------------------ utils
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return v

    # ----------------------------------------------------------------- helpers
    def cors_origins_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def rolling_window_list(self) -> List[int]:
        return [int(x) for x in self.rolling_windows.split(",") if x.strip()]

    def ema_span_list(self) -> List[int]:
        return [int(x) for x in self.ema_spans.split(",") if x.strip()]

    def lag_period_list(self) -> List[int]:
        return [int(x) for x in self.lag_periods.split(",") if x.strip()]

    def volatility_window_list(self) -> List[int]:
        return [int(x) for x in self.volatility_windows.split(",") if x.strip()]

    def category_boundary_list(self) -> List[float]:
        return [float(x) for x in self.category_boundaries.split(",") if x.strip()]

    def category_name_list(self) -> List[str]:
        return [x.strip() for x in self.category_names.split(",") if x.strip()]

    # ----------------------------------------------------- path bootstrap
    def bootstrap_paths(self) -> None:
        """Create artifact sub-directories on first import."""
        for sub in ("models", "datasets", "logs", "reports", "shap"):
            p = self.artifacts_dir / sub
            p.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.artifacts_dir / "models"
        self.datasets_dir = self.artifacts_dir / "datasets"
        self.logs_dir = self.artifacts_dir / "logs"
        self.reports_dir = self.artifacts_dir / "reports"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.bootstrap_paths()
    return s


settings = get_settings()


