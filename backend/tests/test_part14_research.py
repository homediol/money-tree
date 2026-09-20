from __future__ import annotations

import json
from pathlib import Path

from app.database.repository import Repository
from app.research.runner import ResearchConfig, StrategyExperimentRunner


def make_history(path: Path, n=180):
    path.write_text(json.dumps([{"round_id": str(i), "round_index": i,
        "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
        "multiplier": 2.2 if i % 3 == 0 else 1.2} for i in range(n)]))


def test_research_validation_test_robustness_and_reproducibility(tmp_path):
    path = tmp_path / "history.json"; make_history(path)
    cfg = ResearchConfig(profile="", min_history=30, thresholds=(.5, .6), max_candidates=4)
    first = StrategyExperimentRunner(path).run(cfg)
    second = StrategyExperimentRunner(path).run(cfg)
    assert first["research_id"] == second["research_id"]
    assert first["mode"] == "SIMULATION"
    assert first["partitions"]["untouched_test"] > 0
    assert first["final_untouched_test"]
    assert "stable_range" in first["robustness"]
    assert first["production_isolation"] == {"live_execution": False, "browser_access": False, "configuration_mutation": False}


def test_research_rejects_live_mode_and_persists_without_executions(tmp_path):
    path = tmp_path / "history.json"; make_history(path)
    try:
        StrategyExperimentRunner(path).run(ResearchConfig(mode="LIVE", min_history=30))
    except ValueError as exc:
        assert "SIMULATION" in str(exc)
    else:
        raise AssertionError("live research mode must be rejected")
    repo = Repository(tmp_path / "db.sqlite3"); repo.init()
    result = StrategyExperimentRunner(path, repository=repo).run(ResearchConfig(
        profile="PROFILE_A", thresholds=(.55,), max_candidates=1, min_history=30))
    assert repo.research_by_id(result["research_id"])["research_id"] == result["research_id"]
    assert repo.list_executions() == []

