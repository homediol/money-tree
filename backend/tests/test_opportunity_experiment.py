from app.database.repository import Repository
from app.ml.opportunity_v4 import MODEL_VERSION, OpportunityStabilityV4


def _repo(tmp_path):
    repository = Repository(tmp_path / "experiment.sqlite")
    repository.init()
    return repository


def _experiment_setup(repository, *, rounds_observed=0):
    config = {
        "version": MODEL_VERSION, "status": "FROZEN", "configuration_hash": "frozen-v3-hash",
        "coverage_reference": {"cutoffs": {"top_5%": 0.75}},
    }
    repository.save_application_state("opportunity_v3_frozen_config", config, "2026-10-04T00:00:00+00:00")
    gate = {
        "gate_hash": "development-only-gate", "model_version": MODEL_VERSION,
        "model_hash": "frozen-v3-hash", "status": "FROZEN_RESEARCH_ONLY",
        "created_at": "2026-10-04T00:00:00+00:00",
        "policy": {"score_threshold": 0.75, "max_selections_per_100": 4,
                   "window_rounds": 100, "research_only": True},
    }
    repository.save_opportunity_research_gate_once(gate)
    return repository.create_opportunity_experiment({
        "experiment_id": "experiment-1", "model_version": MODEL_VERSION,
        "model_hash": "frozen-v3-hash", "gate_hash": gate["gate_hash"],
        "status": "ACTIVE", "started_at": "2026-10-04T00:00:00+00:00",
        "start_round_index": 1000, "last_round_index": 1000,
        "rounds_observed": rounds_observed, "updated_at": "2026-10-04T00:00:00+00:00",
    })


def _assessment(index, score=0.9):
    return {
        "assessment_id": f"assessment-{index}", "model_version": MODEL_VERSION,
        "round_id": f"source-{index}", "round_index": index,
        "target_round_index": index + 1, "observed_at": f"2026-10-04T00:00:{index % 60:02d}+00:00",
        "created_at": f"2026-10-04T00:00:{index % 60:02d}+00:00",
        "scorable": True, "assessment_immutable": True, "opportunity_score": score,
        "model_configuration_hash": "frozen-v3-hash", "selection_state": "NO_SIGNAL",
        "selected": False, "selection_reason": "V3 diagnostic only",
        "feature_snapshot": {"past_only_feature": 0.2},
        "round_order_proof": {
            "assessment_source_round_index": index, "assessment_source_round_id": f"source-{index}",
            "latest_round_at_assessment_commit_index": index,
            "latest_round_at_assessment_commit_id": f"source-{index}",
            "target_outcome_round_index": index + 1,
            "source_was_latest_at_assessment_commit": True,
            "target_absent_at_assessment_commit": True,
            "proof_method": "postgres_advisory_transaction_lock",
        },
    }


def test_frozen_development_cutoff_creates_separate_research_gate_without_changing_v3(tmp_path):
    repository = _repo(tmp_path)
    config = {
        "version": MODEL_VERSION, "status": "FROZEN", "configuration_hash": "frozen-v3-hash",
        "coverage_reference": {"cutoffs": {"top_5%": 0.75}},
    }
    repository.save_application_state("opportunity_v3_frozen_config", config, "2026-10-04T00:00:00+00:00")

    gate = OpportunityStabilityV4(repository).freeze_research_gate()

    assert gate["status"] == "FROZEN_RESEARCH_ONLY"
    assert gate["policy"]["score_threshold"] == 0.75
    assert gate["policy"]["prospective_outcomes_used"] is False
    assert repository.load_application_state("opportunity_v3_frozen_config") == config


def test_legacy_top5_research_gate_does_not_select_live_predictions(tmp_path):
    repository = _repo(tmp_path)
    _experiment_setup(repository)

    for index in range(101, 106):
        assert repository.save_v4_assessment(_assessment(index)) is not None

    predictions = repository.opportunity_experiment_predictions("experiment-1")
    targets = repository.opportunity_experiment_targets("experiment-1")
    assert predictions == []
    assert targets == []
    # The legacy top-5% diagnostic cutoff is not a live rare-selection policy.
    for index in range(101, 106):
        assessment = repository.get_v4_assessment(f"source-{index}")
        assert assessment["selection_state"] == "NO_SIGNAL"
        assert assessment["selected"] is False
        assert assessment["opportunity_score"] == 0.9


def test_frozen_rare_policy_is_capped_at_four_for_the_whole_experiment(tmp_path):
    repository = _repo(tmp_path)
    experiment = _experiment_setup(repository)
    policy = {
        "policy_id": "policy-v1", "policy_version": "RareOpportunitySelectionPolicyV1",
        "policy_hash": "frozen-policy-hash", "model_version": MODEL_VERSION,
        "model_hash": "frozen-v3-hash", "status": "FROZEN",
        "created_at": "2026-10-04T00:00:00+00:00", "development_cutoff_round": 19929,
        "selection_rules": {"selection_threshold": 0.8, "watch_threshold": 0.7,
                             "max_selections_per_experiment": 4},
    }
    with repository.connect() as conn:
        conn.execute("""INSERT INTO opportunity_selection_policies
            (policy_hash,policy_id,model_version,model_hash,status,development_cutoff_round,created_at,payload)
            VALUES(?,?,?,?,?,?,?,?)""",
            (policy["policy_hash"], policy["policy_id"], policy["model_version"], policy["model_hash"],
             policy["status"], policy["development_cutoff_round"], policy["created_at"], __import__("json").dumps(policy)))
    experiment.update({"selection_policy_status": "FROZEN", "selection_policy_hash": policy["policy_hash"],
                       "policy_activation_target_index": 102, "max_selections": 4,
                       "target_threshold": 2.0})
    repository.update_opportunity_experiment(experiment)

    for index in range(101, 106):
        assert repository.save_v4_assessment(_assessment(index)) is not None

    predictions = repository.opportunity_experiment_predictions("experiment-1")
    targets = repository.opportunity_experiment_targets("experiment-1")
    assert len(predictions) == 4
    assert all(row["status"] == "PENDING" for row in predictions)
    assert [row["selection_state"] for row in targets] == [
        "SELECTED", "SELECTED", "SELECTED", "SELECTED", "NO_SIGNAL",
    ]
    assert all(row["target_was_absent_at_selection"] is True for row in targets)
    for index in range(101, 106):
        assessment = repository.get_v4_assessment(f"source-{index}")
        assert assessment["selection_state"] == "NO_SIGNAL"
        assert assessment["selected"] is False

    experiment = repository.active_opportunity_experiment()
    experiment.update({"rounds_observed": 100, "last_round_index": 1200,
                       "updated_at": "2026-10-04T00:02:00+00:00"})
    repository.update_opportunity_experiment(experiment)
    for index in range(201, 206):
        assert repository.save_v4_assessment(_assessment(index)) is not None
    assert len(repository.opportunity_experiment_predictions("experiment-1")) == 4


def test_experiment_progress_survives_repository_restart_without_legacy_continuity_reset(tmp_path):
    first = _repo(tmp_path)
    experiment = _experiment_setup(first, rounds_observed=10)
    experiment.update({"rounds_scored": 8, "rounds_missed": 1, "rounds_gaps": 1,
                       "last_round_index": 1011, "updated_at": "2026-10-04T00:01:00+00:00"})
    first.update_opportunity_experiment(experiment)

    restarted = _repo(tmp_path)
    restored = restarted.active_opportunity_experiment()

    assert restored["experiment_id"] == "experiment-1"
    assert restored["rounds_observed"] == 10
    assert restored["rounds_scored"] == 8
    assert restored["rounds_missed"] == 1
    assert restored["rounds_gaps"] == 1
