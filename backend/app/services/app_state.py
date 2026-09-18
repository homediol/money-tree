from __future__ import annotations

from threading import RLock

from app.core.config import Settings
from app.database.repository import Repository
from app.ml.model_registry import ModelRegistry
from app.services.data_loader import RoundHistoryLoader
from app.services.pattern_engine import PatternEngine
from app.services.signal_engine import SignalEngine
from app.services.statistics_service import statistics
from app.services.dataset_service import DatasetService
from app.services.evidence_engine import EvidenceEngine


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.loader = RoundHistoryLoader(settings.data_path)
        self.dataset_service = DatasetService(
            settings.data_path, settings.processed_data_dir, settings.features_data_dir,
        )
        self.repository = Repository(settings.database_path)
        self._cache_lock = RLock()
        self._patterns_cache: list[dict] | None = None
        self._pattern_report_cache: dict | None = None
        self._analysis_cache: dict | None = None
        self._statistics_cache: dict | None = None
        self.rounds, self.quality = self.loader.load()
        self.dataset_service.build_training_dataset()
        self.repository.init()
        self.model_registry = ModelRegistry(settings.target_multiplier, settings.model_dir, self.repository)
        self.evidence_engine = EvidenceEngine(self.repository, self.model_registry,
                                              target=settings.target_multiplier,
                                              min_sample_size=settings.min_sample_size)
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
        self.dataset_service.process_incremental()
        with self._cache_lock:
            self.rounds, self.quality = rounds, quality
            self._patterns_cache = None
            self._pattern_report_cache = None
            self._analysis_cache = None
            self._statistics_cache = None
        self.sync_database()

    def patterns(self) -> list[dict]:
        with self._cache_lock:
            if self._patterns_cache is None:
                report = PatternEngine(self.settings.target_multiplier, self.settings.min_sample_size).report(self.rounds)
                items = report["patterns"]
                self.repository.save_patterns(items)
                self._patterns_cache = items
                self._pattern_report_cache = report
            return self._patterns_cache

    def pattern_report(self, target: float | None = None) -> dict:
        selected = float(target or self.settings.target_multiplier)
        if selected != float(self.settings.target_multiplier):
            return PatternEngine(selected, self.settings.min_sample_size).report(self.rounds)
        self.patterns()
        return self._pattern_report_cache or {}

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

    def build_evidence(self, prediction: dict | None) -> dict | None:
        return self.evidence_engine.build(prediction, self.dataset_service)
