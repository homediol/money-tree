"""Regression checks for the live History and read-only browser endpoints."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from app.api import betting as betting_api
from app.api.history import history
from app.database.repository import Repository
from app.round_observation_inbox import RoundObservationInbox
from app.rules_engine import RulesEngine
from app.services.app_state import AppState


def _request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_history_uses_current_canonical_repository_not_startup_dataframe():
    rounds = [{"round_id": "actual-22", "round_index": 22, "multiplier": 2.0,
               "timestamp": "2026-10-14T00:00:00Z"}]

    class CanonicalRepository:
        database_url = "postgresql://configured"

        def load_recent_rounds(self, limit):
            assert limit == 250
            return rounds

        def round_history_status(self):
            return {"count": 22}

    class Collector:
        def rows(self):
            raise AssertionError("PostgreSQL is authoritative")

    request = _request(wp=SimpleNamespace(repository=CanonicalRepository(), rounds=pd.DataFrame()),
                       history_collector=Collector())
    response = history(request)
    assert response == {"count": 22, "returned": 1, "rounds": rounds}


def test_repository_recent_rounds_are_bounded_and_ordered(tmp_path):
    repository = Repository(tmp_path / "rounds.sqlite3")
    repository.init()
    repository.upsert_rounds([
        {"round_id": "r1", "round_index": 1, "multiplier": 1.25,
         "timestamp": "2026-10-14T00:00:01Z", "target": 0},
        {"round_id": "r2", "round_index": 2, "multiplier": 2.0,
         "timestamp": "2026-10-14T00:00:02Z", "target": 1},
        {"round_id": "r3", "round_index": 3, "multiplier": 3.0,
         "timestamp": "2026-10-14T00:00:03Z", "target": 1},
    ])
    assert [row["round_id"] for row in repository.load_recent_rounds(2)] == ["r2", "r3"]


def test_legacy_sqlite_sync_uses_completed_outcome_threshold_without_retired_setting():
    captured = []
    state = AppState.__new__(AppState)
    state.repository = SimpleNamespace(database_url=None, upsert_rounds=captured.extend)
    state.settings = SimpleNamespace()
    state.rounds = pd.DataFrame([
        {"round_id": "r1", "round_index": 1, "multiplier": 1.99, "timestamp": "t1"},
        {"round_id": "r2", "round_index": 2, "multiplier": 2.0, "timestamp": "t2"},
    ])
    state.sync_database()
    assert [row["target"] for row in captured] == [0, 1]


def test_read_only_browser_probe_keeps_actual_readiness_and_timestamp(monkeypatch):
    class Client:
        def __init__(self, _settings):
            pass

        async def readiness(self):
            return {"connected": True, "stage": "ready", "page_found": True,
                    "game_found": True, "aviator_next_reachable": True,
                    "authentication_state": "AUTHENTICATED",
                    "browser_topology_verified": True}

        async def snapshot(self):
            return SimpleNamespace(as_dict=lambda: {"ok": True, "balance": 1200.0,
                "balance_text": "1,200 USD", "balance_currency": "USD",
                "balance_observed_at": "2026-10-14T00:00:00+00:00",
                "authentication_state": "AUTHENTICATED", "ui_ready": True})

        async def close(self):
            pass

    class Health:
        observation = None

        def record_browser_observation(self, value):
            self.observation = value

        def browser_observation(self):
            return self.observation

    monkeypatch.setattr(betting_api, "AviatorBrowserClient", Client)
    health = Health()
    request = _request(betting=SimpleNamespace(settings=object()), system_health=health)
    response = asyncio.run(betting_api.browser_readiness(request, include_snapshot=True))
    assert response["ok"] is True
    assert response["browser"]["connected"] is True
    assert response["platform_balance"]["verified"] is True
    assert datetime.fromisoformat(response["platform_balance"]["observed_at"])


def test_browser_snapshot_failure_does_not_claim_page_disconnected(monkeypatch):
    class Client:
        def __init__(self, _settings):
            pass

        async def readiness(self):
            return {"connected": True, "stage": "ready", "page_found": True,
                    "game_found": True, "aviator_next_reachable": True}

        async def snapshot(self):
            raise RuntimeError("snapshot unavailable")

        async def close(self):
            pass

    monkeypatch.setattr(betting_api, "AviatorBrowserClient", Client)
    request = _request(betting=SimpleNamespace(settings=object()))
    response = asyncio.run(betting_api.browser_readiness(request, include_snapshot=True))
    body = json.loads(response.body)
    assert body["ok"] is False
    assert body["browser"]["connected"] is True
    assert body["browser"]["page_found"] is True
    assert body["browser"]["error"] == "snapshot unavailable"


def test_empty_rules_engine_does_not_delay_collector_handoff():
    engine = RulesEngine()
    assert engine.needs_round_observation() is False
    engine.sessions["configured"] = {"status": "RUNNING"}
    assert engine.needs_round_observation() is True
    engine.sessions["configured"]["status"] = "MANUALLY_STOPPED"
    assert engine.needs_round_observation() is True
    engine.sessions["configured"].update(status="COMPLETED", auto_next_armed=True)
    assert engine.needs_round_observation() is True
    engine.sessions["configured"]["auto_next_armed"] = False
    assert engine.needs_round_observation() is False


def test_committed_handoff_uses_one_ordered_database_transaction():
    calls = []

    class Connection:
        def __enter__(self):
            calls.append("open")
            return self

        def __exit__(self, *_args):
            calls.append("commit")

        def execute(self, sql, params):
            assert "DELETE FROM round_distribution_events" in sql
            assert params == ("stream", "stream", 1000)
            calls.append("prune")

    class Store:
        def connect(self):
            return Connection()

        def persist_round_distribution_event(self, round_id, multiplier, observation,
                                             retention, *, connection, prune):
            assert prune is False and retention == 1000
            assert observation["round_id"] == round_id
            calls.append(round_id)
            return {"stream_id": "stream", "stream_sequence": len(calls),
                    "published_at": None, "event_id": round_id}

        def mark_round_distribution_event_published(self, stream_id, sequence, *, connection):
            assert stream_id == "stream"
            return {"stream_id": stream_id, "stream_sequence": sequence,
                    "published_at": "now", "event_id": calls[-1]}

    rows = [{"round_id": "r1", "multiplier": 2.0},
            {"round_id": "r2", "multiplier": 1.2}]
    events = Repository.persist_round_distribution_batch(Store(), rows, 1000)
    assert [event["event_id"] for event in events] == ["r1", "r2"]
    assert calls == ["open", "r1", "r2", "prune", "commit"]


def test_committed_handoff_marks_inbox_batch(tmp_path):
    inbox = RoundObservationInbox(tmp_path / "inbox.sqlite3")
    for ordinal in (1, 2):
        identity = f"r{ordinal}"
        inbox.append([{"round_id": identity, "observation_id": identity,
                       "multiplier": 2.0, "observed_at": "2026-10-14T00:00:00Z"}],
                     [{"round_id": identity, "observation_id": identity}],
                     received_at="2026-10-14T00:00:00Z")
    inbox.mark_committed_many([("r1", 10), ("r2", 11)])
    assert inbox.snapshot()["backlog_size"] == 0
