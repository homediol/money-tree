"""Contract tests use cheap estimators; no production artifacts/data are mutated."""
from dataclasses import replace
from threading import Barrier, Lock
import time

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import ModelTrainer, TARGET_COLUMN, evaluate_probabilities
from app.services.dataset_service import DatasetService
from test_models import trained


def frame(n=500):
    names = [item.name for item in DatasetService._build_metadata() if item.type != 'categorical_sequence']
    data = pd.DataFrame(np.zeros((n, len(names))), columns=names)
    data[names[0]] = np.arange(n, dtype=float)
    data['round_id'] = [str(i) for i in range(n)]
    data['round_index'] = np.arange(n)
    data['timestamp'] = pd.date_range('2026-01-01', periods=n, freq='s', tz='UTC').astype(str)
    data[TARGET_COLUMN] = np.arange(n) % 2
    return data


class SpyEstimator:
    def __init__(self, name, calls, tracker=None):
        self.name, self.calls, self.tracker = name, calls, tracker

    def fit(self, X, y):
        values = np.asarray(X)
        self.calls.append((self.name, 'fit', values[:, 0].copy(), np.asarray(y).copy()))
        self.feature_importances_ = np.zeros(values.shape[1])
        if self.tracker:
            with self.tracker['lock']:
                self.tracker['running'] += 1
                self.tracker['peak'] = max(self.tracker['peak'], self.tracker['running'])
            if len(values) == 350:
                self.tracker['barrier'].wait(timeout=10)
            time.sleep(.02)
            with self.tracker['lock']:
                self.tracker['running'] -= 1
        return self

    def predict_proba(self, X):
        indexes = np.asarray(X)[:, 0].astype(int)
        self.calls.append((self.name, 'predict', indexes.copy(), None))
        correct = indexes % 2
        p = np.where(correct, .9 if self.name == 'first' else .8, .1 if self.name == 'first' else .2)
        if self.name == 'first':
            p = np.where(indexes >= 425, 1 - p, p)
        return np.column_stack([1 - p, p])

    def get_params(self, deep=False):
        return {}


def cycle(monkeypatch, data=None, concurrency=1, tracker=None):
    calls = []
    trainer = ModelTrainer(model_concurrency=concurrency)
    monkeypatch.setattr(trainer, 'factories', lambda *_: {
        name: (lambda name=name: SpyEstimator(name, calls, tracker)) for name in ['first', 'second']})
    return trainer.train_validate(frame() if data is None else data), calls


def test_shared_splits_fit_only_past_and_each_test_is_opened_once(monkeypatch):
    result, calls = cycle(monkeypatch)
    assert result.ranking == ['first', 'second']
    assert len({row['model_id'] for row in result.models.values()}) == 2
    fit_ranges = {}
    for name in result.models:
        fits = [indexes for model, action, indexes, _ in calls if model == name and action == 'fit']
        assert len(fits) == 4
        assert all(indexes.max() < 350 for indexes in fits)
        fit_ranges[name] = [indexes.tolist() for indexes in fits]
        test_calls = [indexes for model, action, indexes, _ in calls
                      if model == name and action == 'predict' and indexes.min() >= 425]
        assert len(test_calls) == 1
        assert test_calls[0].tolist() == list(range(425, 500))
        row = result.models[name]
        assert row['training_time_seconds'] > 0
        assert row['test']['brier_score'] is not None
        assert len(row['walk_forward']) == 3
        for fold in row['walk_forward']:
            assert fold['train_end'] == fold['evaluation_start'] < fold['evaluation_end'] <= 350
    assert fit_ranges['first'] == fit_ranges['second']


def test_test_changes_cannot_reorder_or_choose_a_runner_up(monkeypatch):
    original, _ = cycle(monkeypatch)
    assert original.models['second']['deployable']
    assert not original.models['first']['deployable']
    assert original.algorithm == 'first' and not original.validated
    changed = frame()
    changed.loc[425:, TARGET_COLUMN] = 1 - changed.loc[425:, TARGET_COLUMN]
    after, _ = cycle(monkeypatch, changed)
    assert after.ranking == original.ranking and after.algorithm == original.algorithm
    assert after.validated  # Final test only vetoes, never changes rank.
    for name in after.ranking:
        assert after.models[name]['selection_score'] == original.models[name]['selection_score']
        assert after.models[name]['walk_forward'] == original.models[name]['walk_forward']


def test_test_baseline_is_not_evaluated_until_ranking_is_sealed(monkeypatch):
    import app.ml.trainer as module
    original_baseline, original_rank = module._baseline, ModelTrainer.rank_candidates
    sealed = {'value': False, 'test_evaluations': 0}
    target = frame()[TARGET_COLUMN].iloc[425:].to_numpy(int)
    def baseline(y_train, y_eval):
        if len(y_eval) == len(target) and np.array_equal(y_eval, target):
            assert sealed['value']
            sealed['test_evaluations'] += 1
        return original_baseline(y_train, y_eval)
    def rank(models):
        result = original_rank(models)
        sealed['value'] = True
        return result
    monkeypatch.setattr(module, '_baseline', baseline)
    monkeypatch.setattr(ModelTrainer, 'rank_candidates', staticmethod(rank))
    cycle(monkeypatch)
    assert sealed['test_evaluations'] == 1


@pytest.mark.parametrize('concurrency', [1, 2])
def test_configured_concurrency_bounds_model_work(monkeypatch, concurrency):
    tracker = {'lock': Lock(), 'running': 0, 'peak': 0, 'barrier': Barrier(concurrency)}
    result, _ = cycle(monkeypatch, concurrency=concurrency, tracker=tracker)
    assert tracker['peak'] == concurrency
    assert tracker['running'] == 0
    assert result.model_concurrency == concurrency
    assert len(result.models) == 2


def test_failures_are_visible_and_do_not_stop_other_models(monkeypatch):
    trainer = ModelTrainer()
    calls = []
    def missing():
        raise ImportError('dependency missing')
    monkeypatch.setattr(trainer, 'factories', lambda *_: {
        'missing': missing, 'second': lambda: SpyEstimator('second', calls)})
    result = trainer.train_validate(frame())
    assert result.ranking == ['second']
    assert result.models['missing']['rank'] is None
    assert result.models['missing']['status'] == 'UNAVAILABLE'
    assert result.models['missing']['rejection_reasons'] == ['ImportError: dependency missing']
    assert result.models['second']['test']


def test_every_model_failure_reports_not_deployable(monkeypatch):
    trainer = ModelTrainer()
    monkeypatch.setattr(trainer, 'factories', lambda *_: {'missing': lambda: trainer._unavailable('missing')})
    result = trainer.train_validate(frame())
    assert result.status == 'NOT_DEPLOYABLE' and not result.validated
    assert result.models['missing']['status'] == 'UNAVAILABLE'
    assert result.ranking == [] and result.cycle_id


def test_registry_persists_all_candidates_and_restores_cycle(trained):
    root, dataset, repository, _, result = trained
    saved = {row['model_version']: row for row in repository.list_model_candidates(100)}
    for name, row in result.models.items():
        assert row['model_id'] in saved
        if row.get('test'):
            assert (root / 'models' / row['model_id'] / 'model.joblib').exists()
            assert saved[row['model_id']]['test_metrics']['brier_score'] == row['test']['brier_score']
    restored = ModelRegistry(model_dir=root / 'models', repository=repository, max_feature_age_s=3600)
    assert restored.performance()['ranking'] == result.ranking
    assert restored.status(dataset)['deployable']


def test_invalid_champion_is_not_retained_and_lock_releases_on_failure(trained, tmp_path, monkeypatch):
    _, dataset, _, _, accepted = trained
    registry = ModelRegistry(model_dir=tmp_path / 'models', max_feature_age_s=3600)
    registry._model, registry._metadata = accepted._model, accepted.model_dump()
    registry._metadata['feature_schema_hash'] = 'incompatible'
    rejected = replace(accepted, validated=False, model_version='invalid-champion-attempt',
                       ensemble={'validated': False}, overfitting_checks={'deployable': False})
    rejected._model = accepted._model
    monkeypatch.setattr(registry.trainer, 'train_validate', lambda _: rejected)
    registry.train(dataset)
    assert registry.status(dataset)['status'] == 'NOT_DEPLOYABLE'
    assert registry.status(dataset)['active_model_version'] is None
    assert registry.performance()['deployment_outcome'] == 'NOT_DEPLOYABLE'
    def fail(_):
        raise RuntimeError('intentional training failure')
    monkeypatch.setattr(registry.trainer, 'train_validate', fail)
    with pytest.raises(RuntimeError, match='intentional'):
        registry.train(dataset)
    assert not registry._training
    assert registry._train_lock.acquire(blocking=False)
    registry._train_lock.release()


@pytest.mark.parametrize('value', [0, 5])
def test_invalid_concurrency_is_rejected(value):
    with pytest.raises(ValueError):
        ModelTrainer(model_concurrency=value)
    with pytest.raises(ValueError):
        Settings(ml_model_concurrency=value)


def test_unknown_features_and_duplicate_rounds_fail_closed(monkeypatch):
    trainer = ModelTrainer()
    assert trainer.train_validate(frame().assign(future_result=1)).status == 'INCOMPATIBLE'
    data = frame()
    data.loc[1, 'round_id'] = data.loc[0, 'round_id']
    assert trainer.train_validate(data).status == 'ERROR'


@pytest.mark.parametrize('veto', ['selection', 'folds', 'test', 'frozen_bootstrap', 'best_bootstrap'])
def test_existing_deployment_gates_each_veto_independently(monkeypatch, veto):
    data = frame()
    trainer = ModelTrainer()
    calls = []
    model = SpyEstimator('second', calls).fit(data.iloc[:350, :124], data[TARGET_COLUMN].iloc[:350])
    row = {'validation': {'brier_score': .04, 'accuracy': .8}, 'train': {'brier_score': .04},
           'selection_brier_advantage': .0009 if veto == 'selection' else .21,
           'folds_beating_baseline': 1 if veto == 'folds' else 3,
           'walk_forward': [{'metrics': {'brier_score': .04}}] * 3,
           'passes_selection_gate': veto not in {'selection', 'folds'}}
    def bootstrap(*_, baseline_name='frozen_training_frequency', **__):
        lower = -.01 if ((veto == 'frozen_bootstrap' and baseline_name == 'frozen_training_frequency')
                         or (veto == 'best_bootstrap' and baseline_name == 'base_rate_probability')) else .01
        return {'point': .21, 'ci95': [lower, .3]}
    monkeypatch.setattr('app.ml.trainer.block_bootstrap_brier_advantage', bootstrap)
    values = data.iloc[:, :124].to_numpy(float)
    y = data[TARGET_COLUMN].to_numpy(int).copy()
    if veto == 'test':
        y[425:] = 1 - y[425:]
    _, report = trainer._verify_candidate(model, row, values[350:400], y[350:400], values[400:425],
        y[400:425], values[425:], y[425:], data.iloc[425:], y[:350], y, 425,
        {'base_rate_probability': {'brier_score': .25}})
    assert report['deployable'] is False
    assert report['rejection_reasons']


def test_walk_forward_class_weights_only_see_fold_training_labels():
    data = frame(350)
    data[TARGET_COLUMN] = np.where(np.arange(350) < 193, np.arange(350) % 10 == 0, 1).astype(int)
    weights = []
    class Weighted(SpyEstimator):
        def get_params(self, deep=False):
            return {'class_weight': None}
        def set_params(self, **params):
            weights.append(params['class_weight'])
            return self
    factories = {'second': lambda: Weighted('second', [])}
    names = [item.name for item in DatasetService._build_metadata() if item.type != 'categorical_sequence']
    folds = ModelTrainer()._walk_forward(data, names, 'second', factories)
    assert len(folds) == 3
    assert weights[0] == 'balanced'  # Full training window is not imbalanced.
    assert weights[1:] == [None, None]


@pytest.mark.parametrize('value', [np.nan, np.inf, -.1, 1.1])
def test_invalid_model_probabilities_cannot_pass_gates(value):
    with pytest.raises(ValueError, match='probabilities'):
        evaluate_probabilities(np.array([0, 1]), np.array([.1, value]))


def test_no_hidden_random_validation_split_in_gradient_boosting():
    assert ModelTrainer.factories()['gradient_boosting']().get_params()['early_stopping'] is False
