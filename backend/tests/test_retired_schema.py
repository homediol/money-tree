from app.database.repository import Repository


def test_active_schema_keeps_core_data_and_does_not_recreate_retired_ledgers(tmp_path):
    repository = Repository(tmp_path / "active-schema.sqlite3")
    repository.init()

    with repository.connect() as conn:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

    assert {"rounds", "collector_round_observation_events", "bet_executions",
            "execution_events", "balance_ledger", "reconciliation_records",
            "decision_audit", "application_state"} <= tables
    assert not ({"patterns", "signals", "model_versions", "ml_predictions", "decision_records",
                 "opportunity_v4_assessments", "prediction_evidence",
                 "prediction_evaluations", "readiness_state"} & tables)
