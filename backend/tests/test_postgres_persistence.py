"""Run with TEST_DATABASE_URL pointing to a disposable PostgreSQL database."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database.repository import Repository
from scripts.migrate_sqlite_to_postgres import migrate


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")
def test_postgres_restarts_preserve_history_risk_and_execution_state(tmp_path):
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
                assert repository.import_legacy_rounds(rounds) == 150
                repository.save_application_state("risk_runtime", {
                    "selected_profile": "PROFILE_B", "emergency_latched": True,
                }, datetime.now(timezone.utc).isoformat())
                repository.save_betting_mode("SHADOW_REALISTIC", {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                repository.create_execution({
                    "execution_id": "exec-1", "decision_id": "decision-1",
                    "target_round_id": "151", "profile": "PROFILE_B",
                    "bet_amount": 100, "cashout_target": 2.0,
                    "status": "PLACED", "simulated": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "balance_before": 5000,
                })
            assert repository.import_legacy_rounds(rounds) == 0
            assert len(repository.load_rounds()) == 150
            state = repository.rebuild_collector_state()
            # Legacy import preserves rows and IDs but does not manufacture
            # prospective continuity proof for historical data.
            assert state["contiguous_rounds"] == 0
            assert state["warmup_status"] == "WARMING_UP"
            assert state["total_history"] == 150
            assert repository.load_application_state("risk_runtime")["emergency_latched"] is True
            assert repository.load_betting_mode()["mode"] == "SHADOW_REALISTIC"
            execution = repository.execution_for("decision-1", "151")
            assert execution["execution_id"] == "exec-1"
            assert execution["status"] in {"PLACED", "UNKNOWN"}
    finally:
        with psycopg.connect(root_dsn) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")
def test_sqlite_migration_keeps_stable_round_ids_risk_and_mode(tmp_path):
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
        source.save_application_state("risk_runtime", {"selected_profile": "PROFILE_B"},
                                      datetime.now(timezone.utc).isoformat())
        source.save_betting_mode("SHADOW_REALISTIC", {"updated_at": datetime.now(timezone.utc).isoformat()})

        migrate(source_path, raw, dsn)
        migrate(source_path, raw, dsn)
        destination = Repository(tmp_path / "unused.sqlite3", dsn, require_postgres=True)
        assert len(destination.load_rounds()) == 150
        assert destination.load_rounds()[-1]["round_id"] == "platform-150"
        assert destination.rebuild_collector_state()["contiguous_rounds"] == 0
        assert destination.load_application_state("risk_runtime")["selected_profile"] == "PROFILE_B"
        assert destination.load_betting_mode()["mode"] == "SHADOW_REALISTIC"
    finally:
        with psycopg.connect(root_dsn) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
