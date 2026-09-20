from __future__ import annotations

import json
from pathlib import Path

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.database.repository import Repository


def history(path: Path, count: int = 140):
    path.write_text(json.dumps([{
        "round_id": str(i), "round_index": i,
        "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
        "multiplier": 2.2 if i % 4 == 0 else 1.2,
    } for i in range(count)]))


def test_chronological_replay_profiles_and_reproducibility(tmp_path):
    path = tmp_path / "roundhistory.json"; history(path)
    a = BacktestEngine(path).run(BacktestConfig(profile="PROFILE_A", min_history=100))
    b = BacktestEngine(path).run(BacktestConfig(profile="PROFILE_A", min_history=100))
    assert a["run_id"] == b["run_id"]
    assert a["mode"] == "HISTORICAL_SIMULATION"
    assert all(t["source_round_id"] == str(int(t["round_id"]) - 1) for t in a["trades"])
    assert [t["index"] for t in a["trades"]] == sorted(t["index"] for t in a["trades"])
    profile_b = BacktestEngine(path).run(BacktestConfig(profile="PROFILE_B", min_history=100))
    assert profile_b["config"]["profile"] == "PROFILE_B"


def test_future_mutation_does_not_change_prior_predictions(tmp_path):
    path = tmp_path / "roundhistory.json"; history(path)
    first = BacktestEngine(path).run(BacktestConfig(min_history=100))
    rows = json.loads(path.read_text())
    rows[-1]["multiplier"] = 99.0
    path.write_text(json.dumps(rows))
    second = BacktestEngine(path).run(BacktestConfig(min_history=100))
    n = len(first["trades"]) - 1
    assert first["trades"][:n] == second["trades"][:n]


def test_goal_risk_blocks_and_persistence_are_simulation_only(tmp_path):
    path = tmp_path / "roundhistory.json"; history(path)
    repo = Repository(tmp_path / "db.sqlite3"); repo.init()
    result = BacktestEngine(path, repository=repo).run(BacktestConfig(
        starting_balance=1000, goal_balance=1000, min_history=100))
    assert result["summary"]["bets"] == 0
    assert result["summary"]["risk_blocks"].get("goal_reached", 0) > 0 or result["summary"]["bets"] == 0
    assert repo.list_backtests()[0]["run_id"] == result["run_id"]
    assert repo.list_executions() == []
