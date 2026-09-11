from __future__ import annotations

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
        self.rounds, self.quality = self.loader.load()
        self.sync_database()

    def patterns(self) -> list[dict]:
        items = PatternEngine(self.settings.target_multiplier, self.settings.min_sample_size).discover(self.rounds)
        self.repository.save_patterns(items)
        return items

    def signal_engine(self) -> SignalEngine:
        return SignalEngine(
            self.settings.target_multiplier,
            self.settings.min_sample_size,
            self.settings.signal_threshold,
            self.settings.strong_signal_threshold,
            self.model_registry,
        )

    def current_analysis(self) -> dict:
        analysis = self.signal_engine().current_analysis(self.rounds)
        if self.rounds is not None and not self.rounds.empty:
            self.repository.save_signal(analysis)
        return analysis

    def statistics(self) -> dict:
        return {"data_quality": self.quality.model_dump(), "statistics": statistics(self.rounds, self.settings.target_multiplier)}

