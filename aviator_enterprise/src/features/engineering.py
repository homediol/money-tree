"""
Feature Engineering Pipeline (Enterprise v3)
=============================================

Orchestrates all feature generators:
  - Statistical (rolling, EMA, volatility, momentum, streak, entropy, z-score)
  - Time-based (cyclic encoding, inter-arrival, session features)
  - Advanced (higher-order moments, autocorrelation, spectral, cross-features)

Pipeline steps:
  1. Build raw features
  2. Handle missing values via forward-fill + median imputation
  3. Drop near-zero-variance columns
  4. Apply RobustScaler
  5. Return feature matrix + fitted transformers for inference

All transformers are fitted only during `fit_transform` and applied
(not refitted) during `transform`, preventing data leakage.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import RobustScaler

from config.settings import settings
from src.core.logger import get_logger
from src.features.statistical import StatisticalFeatures
from src.features.time_based import TimeBasedFeatures
from src.features.advanced import AdvancedFeatures

logger = get_logger(__name__)
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)


@dataclass
class FeaturePipelineResult:
    features: pd.DataFrame
    feature_names: List[str]
    target: Optional[pd.Series]
    scaler: Optional[RobustScaler]
    imputer: Optional[SimpleImputer]
    dropped_constant: List[str]
    metadata: Dict[str, object] = field(default_factory=dict)


class FeatureEngineeringPipeline:
    """
    End-to-end feature engineering from a raw DataFrame to a
    model-ready scaled matrix.

    Guarantees:
      - No information leakage between fit and transform phases.
      - Reproducible transforms stored as instance state.
      - All NaN/Inf values handled before reaching models.
    """

    # Columns never used as features — only as labels / metadata
    _EXCLUDE = frozenset([
        "round_id", "ts_utc", "timestamp", "ingested_at", "source",
        "category", "category_name", "multiplier", "log_multiplier",
        "interarrival_s",
    ])

    def __init__(self) -> None:
        self.statistical = StatisticalFeatures()
        self.time_based = TimeBasedFeatures()
        self.advanced = AdvancedFeatures()
        self.scaler: Optional[RobustScaler] = None
        self.imputer: Optional[SimpleImputer] = None
        self.dropped_constant: List[str] = []
        self.feature_names_: List[str] = []
        self._fitted = False
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------------- public
    def fit_transform(
        self,
        df: pd.DataFrame,
        *,
        target_col: str = "category",
        dropna: bool = True,
    ) -> FeaturePipelineResult:
        """Fit pipeline on `df` and return transformed features."""
        self._fitted = False
        engineered = self._build_all(df)
        engineered = self._fill_time_cols(engineered)
        if dropna:
            engineered = engineered.dropna(
                subset=[c for c in engineered.columns if c not in self._EXCLUDE],
                how="all",
            ).reset_index(drop=True)

        target = engineered[target_col] if target_col in engineered.columns else None
        feat_cols = [c for c in engineered.columns if c not in self._EXCLUDE]
        X = engineered[feat_cols].copy()

        X = self._drop_constants_fit(X)
        X = self._impute_fit(X)
        X = self._scale_fit(X)
        X = self._sanitize(X)

        self.feature_names_ = list(X.columns)
        self._fitted = True
        self.logger.info(
            "Features fitted",
            extra={"n_features": len(self.feature_names_), "n_samples": len(X)},
        )
        return FeaturePipelineResult(
            features=X,
            feature_names=self.feature_names_,
            target=target,
            scaler=self.scaler,
            imputer=self.imputer,
            dropped_constant=self.dropped_constant,
            metadata={"n_samples": len(X), "n_features": len(self.feature_names_)},
        )

    def transform(
        self,
        df: pd.DataFrame,
        *,
        target_col: str = "category",
    ) -> FeaturePipelineResult:
        """Apply already-fitted transforms to new data."""
        if not self._fitted:
            # Auto-fit if not yet fitted (supports lazy initialization)
            return self.fit_transform(df, target_col=target_col)

        engineered = self._build_all(df)
        engineered = self._fill_time_cols(engineered)
        target = engineered[target_col] if target_col in engineered.columns else None
        feat_cols = [c for c in engineered.columns if c not in self._EXCLUDE]
        X = engineered[feat_cols].copy()

        # Align to fitted feature names — fill missing columns with 0
        X = X.reindex(columns=self.feature_names_ + self.dropped_constant, fill_value=0.0)
        X = X.drop(columns=self.dropped_constant, errors="ignore")
        X = X.reindex(columns=self.feature_names_, fill_value=0.0)

        if self.imputer is not None:
            X = pd.DataFrame(
                self.imputer.transform(X), columns=X.columns, index=X.index
            )
        if self.scaler is not None:
            X = pd.DataFrame(
                self.scaler.transform(X), columns=X.columns, index=X.index
            )
        X = self._sanitize(X)
        return FeaturePipelineResult(
            features=X,
            feature_names=list(X.columns),
            target=target,
            scaler=self.scaler,
            imputer=self.imputer,
            dropped_constant=self.dropped_constant,
        )

    def export_metadata(self) -> dict:
        return {
            "n_features": len(self.feature_names_),
            "feature_names": self.feature_names_,
            "dropped_constant": self.dropped_constant,
            "scaler": type(self.scaler).__name__ if self.scaler else None,
            "imputer": type(self.imputer).__name__ if self.imputer else None,
            "fitted": self._fitted,
        }

    # ---------------------------------------------------------------- private
    def _build_all(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self.statistical.build(df)
        out = self.time_based.build(out)
        out = self.advanced.build(out)
        return out

    @staticmethod
    def _fill_time_cols(df: pd.DataFrame) -> pd.DataFrame:
        for col in ("ts_utc", "timestamp"):
            if col in df.columns:
                df[col] = df[col].ffill().bfill()
        return df

    def _drop_constants_fit(self, X: pd.DataFrame) -> pd.DataFrame:
        variances = X.var(numeric_only=True)
        const_cols = list(variances[variances < 1e-8].index)
        self.dropped_constant = const_cols
        if const_cols:
            self.logger.info(
                "Dropping constant features", extra={"count": len(const_cols)}
            )
        return X.drop(columns=const_cols, errors="ignore")

    def _impute_fit(self, X: pd.DataFrame) -> pd.DataFrame:
        self.imputer = SimpleImputer(strategy="median")
        arr = self.imputer.fit_transform(X)
        return pd.DataFrame(arr, columns=X.columns, index=X.index)

    def _scale_fit(self, X: pd.DataFrame) -> pd.DataFrame:
        self.scaler = RobustScaler()
        arr = self.scaler.fit_transform(X)
        return pd.DataFrame(arr, columns=X.columns, index=X.index)

    @staticmethod
    def _sanitize(X: pd.DataFrame) -> pd.DataFrame:
        """Replace any remaining NaN/Inf introduced by scaling with 0."""
        return X.replace([np.inf, -np.inf], np.nan).fillna(0.0)


__all__ = ["FeatureEngineeringPipeline", "FeaturePipelineResult"]
