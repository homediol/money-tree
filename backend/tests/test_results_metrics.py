from app.api.results import verified_session_metrics


def test_unverified_balance_snapshot_is_not_exposed_as_session_results():
    fake_sample = {
        "session_id": "S-4", "mode": "REAL", "simulated": False,
        "starting_balance": 500, "current_balance": 800,
        "goal_balance": 1000, "profit": 300, "total_bets": 0,
    }

    assert verified_session_metrics([fake_sample], []) == []


def test_real_session_totals_require_reconciled_verified_execution():
    metrics = [
        {"session_id": "real-1", "mode": "REAL", "simulated": False,
         "starting_balance": 500, "current_balance": 600, "profit": 100},
        {"session_id": "sim-1", "mode": "SIMULATION", "simulated": True,
         "starting_balance": 500, "current_balance": 600, "profit": 100},
    ]
    executions = [
        {"session_id": "real-1", "status": "RECONCILED", "balance_status": "VERIFIED",
         "profit_loss": 100, "simulated": False},
        {"session_id": "sim-1", "status": "RECONCILED", "balance_status": "VERIFIED",
         "profit_loss": 100, "simulated": True},
    ]

    assert verified_session_metrics(metrics, executions) == [metrics[0]]


def test_unsettled_or_unverified_execution_does_not_authorize_session_totals():
    metrics = [{"session_id": "real-1", "mode": "REAL", "simulated": False,
                "starting_balance": 500, "current_balance": 800, "profit": 300}]
    executions = [{"session_id": "real-1", "status": "UNKNOWN", "balance_status": "MISMATCH",
                   "profit_loss": None, "simulated": False}]

    assert verified_session_metrics(metrics, executions) == []
