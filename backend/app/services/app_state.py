from __future__ import annotations

from threading import RLock

from app.core.config import Settings
from app.database.repository import Repository
from app.ml.model_registry import ModelRegistry
from app.services.data_loader import RoundHistoryLoader
from app.services.pattern_engine import PatternEngine
from app.services.signal_engine import SignalEngine
from app.services.statistics_service import statistics


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.loader = RoundHistoryLoader(settings.data_path)
        self.repository = Repository(settings.database_path)
        self.model_registry = ModelRegistry(settings.target_multiplier)
        self._cache_lock = RLock()
        self._patterns_cache: list[dict] | None = None
        self._analysis_cache: dict | None = None
        self._statistics_cache: dict | None = None
        self.rounds, self.quality = self.loader.load()
        self.repository.init()
        self.sync_database()

    def sync_database(self) -> None:
        if self.rounds.empty:
            return
        rows = [
            {
                "round_index": int(r.round_index),
                "multiplier": float(r.multiplier),
                "timestamp": r.timestamp,
                "target": int(float(r.multiplier) >= self.settings.target_multiplier),
            }
            for r in self.rounds.itertuples()
        ]
        self.repository.upsert_rounds(rows)

    def reload(self) -> None:
        rounds, quality = self.loader.load()
        with self._cache_lock:
            self.rounds, self.quality = rounds, quality
            self._patterns_cache = None
            self._analysis_cache = None
            self._statistics_cache = None
        self.sync_database()

    def patterns(self) -> list[dict]:
        with self._cache_lock:
            if self._patterns_cache is None:
                items = PatternEngine(self.settings.target_multiplier, self.settings.min_sample_size).discover(self.rounds)
                self.repository.save_patterns(items)
                self._patterns_cache = items
            return self._patterns_cache

    def signal_engine(self) -> SignalEngine:
        return SignalEngine(
            self.settings.target_multiplier,
            self.settings.min_sample_size,
            self.settings.signal_threshold,
            self.settings.strong_signal_threshold,
            self.model_registry,
        )

    def current_analysis(self) -> dict:
        with self._cache_lock:
            if self._analysis_cache is None:
                analysis = self.signal_engine().current_analysis(self.rounds)
                if self.rounds is not None and not self.rounds.empty:
                    self.repository.save_signal(analysis)
                self._analysis_cache = analysis
            return self._analysis_cache

    def statistics(self) -> dict:
        with self._cache_lock:
            if self._statistics_cache is None:
                self._statistics_cache = {
                    "data_quality": self.quality.model_dump(),
                    "statistics": statistics(self.rounds, self.settings.target_multiplier),
                }
            return self._statistics_cache
