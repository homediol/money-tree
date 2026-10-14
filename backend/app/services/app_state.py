from __future__ import annotations

import pandas as pd

from app.core.config import Settings
from app.database.repository import Repository
from app.services.data_loader import RoundHistoryLoader


class AppState:
    """History-only application state; prediction features are retired."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = Repository(settings.database_path, settings.database_url,
                                     settings.require_postgres)
        self.repository.init()
        self.loader = RoundHistoryLoader(settings.data_path)
        if (self.repository.database_url
                and self.repository.load_application_state("legacy_rounds_imported") is None):
            legacy_rounds, _ = self.loader.load()
            if not legacy_rounds.empty:
                self.repository.import_legacy_rounds([
                    {"round_id": str(row.round_id), "round_index": int(row.round_index),
                     "multiplier": float(row.multiplier), "timestamp": row.timestamp}
                    for row in legacy_rounds.itertuples(index=False)
                ])
        self._load_history()
        previous_collector_state = self.repository.load_collector_state()
        self.collector_state = self.repository.rebuild_collector_state(
            required_rounds=settings.history_status_window_rounds,
            collector_session_id=(previous_collector_state or {}).get("collector_session_id"),
        )

    def _load_history(self) -> None:
        if self.repository.database_url:
            rows = self.repository.load_rounds()
            self.rounds = pd.DataFrame(rows)
            if not self.rounds.empty:
                self.rounds = self.rounds.sort_values("round_index").reset_index(drop=True)
            valid = len(self.rounds)
            stamps = self.rounds.get("timestamp") if valid else None
            self.quality = {
                "valid_records": valid,
                "invalid_records": 0,
                "first_timestamp": stamps.iloc[0] if stamps is not None and valid else None,
                "last_timestamp": stamps.iloc[-1] if stamps is not None and valid else None,
                "last_round_index": int(self.rounds.iloc[-1]["round_index"]) if valid else None,
                "source": "PostgreSQL:aviator_rounds",
            }
        else:
            self.rounds, self.quality = self.loader.load()
            if self.rounds is None:
                self.rounds = pd.DataFrame()
        self.rounds = self.rounds.copy()

    def reload(self) -> None:
        self._load_history()
        if not self.repository.database_url and not self.rounds.empty:
            self.sync_database()
        previous_collector_state = getattr(self, "collector_state", {}) or {}
        self.collector_state = self.repository.rebuild_collector_state(
            required_rounds=self.settings.history_status_window_rounds,
            collector_session_id=previous_collector_state.get("collector_session_id"),
        )

    def sync_database(self) -> None:
        if self.repository.database_url or self.rounds.empty:
            return
        self.repository.upsert_rounds([
            {
                "round_index": int(row.round_index),
                "round_id": str(getattr(row, "round_id", row.round_index)),
                "multiplier": float(row.multiplier),
                "timestamp": row.timestamp,
                "target": int(float(row.multiplier) >= 2.0),
            }
            for row in self.rounds.itertuples(index=False)
        ])
