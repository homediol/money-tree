"""Run with TEST_DATABASE_URL pointing to a disposable PostgreSQL database."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database.repository import Repository
from scripts.migrate_sqlite_to_postgres import migrate


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")
def test_three_postgres_restarts_preserve_collector_and_model_state(tmp_path):
    import psycopg
    from psycopg.conninfo import make_conninfo

    root_dsn = os.environ["TEST_DATABASE_URL"]
    schema = "winner_test_" + uuid.uuid4().hex
    with psycopg.connect(root_dsn) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    dsn = make_conninfo(root_dsn, options=f"-csearch_path={schema}")
    try:
        start = datetime.now(timezone.utc) - timedelta(seconds=150)
        rounds = [{"round_id": str(i), "round_index": i,
                   "timestamp": (start + timedelta(seconds=i)).isoformat(),
                   "multiplier": 2.0 if i % 2 else 1.2} for i in range(1, 151)]
        for restart in range(4):
            repository = Repository(tmp_path / "unused.sqlite3", dsn, require_postgres=True)
            repository.init()
            if restart == 0:
                # The collector may write the first new round before the
                # backend gets a chance to import its older JSON history.
                with repository.connect() as conn:
                    conn.execute("""INSERT INTO aviator_rounds(round_id,round_index,multiplier,timestamp)
                                    VALUES(?,?,?,?)""",
                                 (rounds[-1]["round_id"], rounds[-1]["round_index"],
                                  rounds[-1]["multiplier"], rounds[-1]["timestamp"]))
                assert repository.import_legacy_rounds(rounds) == 150
                repository.save_model_candidate({"model_version": "candidate-1", "algorithm": "extra_trees",
                                                 "validation_folds": [{"fold": 1}],
                                                 "metrics": {"brier_score": .25},
                                                 "deployment_status": "NOT_DEPLOYABLE"})
                repository.save_application_state("ml_registry", {"candidate": {"model_version": "candidate-1"}},
                                                  datetime.now(timezone.utc).isoformat())
            assert repository.import_legacy_rounds(rounds) == 0
            assert len(repository.load_rounds()) == 150
            state = repository.rebuild_collector_state()
            assert state["contiguous_rounds"] == 150
            assert state["total_history"] == 150
            assert repository.list_model_candidates(1)[0]["metrics"]["brier_score"] == .25
            assert repository.load_application_state("ml_registry")["candidate"]["model_version"] == "candidate-1"
    finally:
        with psycopg.connect(root_dsn) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")
def test_sqlite_migration_keeps_stable_ids_model_and_mode(tmp_path):
    import json
    import psycopg
    from psycopg.conninfo import make_conninfo

    root_dsn = os.environ["TEST_DATABASE_URL"]
    schema = "winner_migration_" + uuid.uuid4().hex
    with psycopg.connect(root_dsn) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    dsn = make_conninfo(root_dsn, options=f"-csearch_path={schema}")
    try:
        start = datetime.now(timezone.utc) - timedelta(seconds=150)
        rounds = [{"round_id": f"platform-{i}", "round_index": i,
                   "timestamp": (start + timedelta(seconds=i)).isoformat(),
                   "multiplier": 2.0 if i % 2 else 1.2} for i in range(1, 151)]
        raw = tmp_path / "roundhistory.json"
        raw.write_text(json.dumps(rounds))
        source_path = tmp_path / "old.sqlite3"
        source = Repository(source_path)
        source.init()
        source.upsert_rounds([{**row, "target": int(row["multiplier"] >= 2)} for row in rounds])
        source.save_application_state("ml_registry", {"candidate": {"model_version": "candidate-1"}},
                                      datetime.now(timezone.utc).isoformat())
        source.save_model_candidate({"model_version": "candidate-1", "algorithm": "extra_trees",
                                     "metrics": {"brier_score": .25}, "deployment_status": "NOT_DEPLOYABLE"})
        source.save_betting_mode("SHADOW_REALISTIC", {"updated_at": datetime.now(timezone.utc).isoformat()})

        migrate(source_path, raw, dsn)
        migrate(source_path, raw, dsn)
        destination = Repository(tmp_path / "unused.sqlite3", dsn, require_postgres=True)
        assert len(destination.load_rounds()) == 150
        assert destination.load_rounds()[-1]["round_id"] == "platform-150"
        assert destination.rebuild_collector_state()["contiguous_rounds"] == 150
        assert destination.load_application_state("ml_registry")["candidate"]["model_version"] == "candidate-1"
        assert destination.list_model_candidates(1)[0]["metrics"]["brier_score"] == .25
        assert destination.load_betting_mode()["mode"] == "SHADOW_REALISTIC"
    finally:
        with psycopg.connect(root_dsn) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
