# Aviator ML Enterprise Architecture

## Production Platform Shape

The platform is organized as independent services: data collection, validation, preprocessing, feature engineering, feature store, training, model registry, prediction service, ensemble engine, evaluation, drift detection, monitoring, dashboard API, and continuous learning. This improves the system because each module has one operational responsibility and can be tested, scaled, and replaced without rewriting the full application.

Implementation is centered around the FastAPI service container in `src/api/services.py`, which wires shared singletons for the trainer, model registry, prediction service, drift monitor, feature store, and dashboard routes. The expected impact is lower deployment risk and more predictable runtime state. The trade-off is more interfaces to maintain, so contracts such as model metadata and dashboard payloads must stay versioned.

## Machine Learning Pipeline

The feature pipeline builds rolling statistics, EMA features, volatility, variance, standard deviation, momentum, trend, lags, streaks, frequency signals, entropy, time features, and advanced statistical features. It improves prediction reliability by giving the models memory of recent crash behavior instead of only point-in-time values. The implementation fits imputation, constant-column filtering, and scaling only during training, then reuses those fitted transforms for inference to reduce leakage. The main limitation is that richer features can overfit small datasets, so cross-validation and drift monitoring are mandatory.

Training supports Random Forest, XGBoost, LightGBM, CatBoost, Logistic Regression, Neural Networks, LSTM, Transformer, and Ensemble models. Hyperparameter optimization is available through Optuna, class imbalance can be handled with class weights or SMOTE, and probabilities can be calibrated. This improves quality by comparing diverse learners and preventing overconfident raw scores. The trade-off is heavier dependency and compute cost, especially for deep learning and Bayesian search.

The ensemble path now trains base learners and wraps them in soft-voting or stacking logic. This improves robustness because a single weak model is less likely to dominate production decisions. The limitation is that ensemble inference is slower and member model failures must be monitored.

## Registry, Feature Store, and Versioning

The trainer now registers promoted models through the shared `ModelRegistry` instance used by prediction services. This improves reliability because a successful training run is immediately discoverable by inference and the dashboard. New active versions automatically demote older active versions for the same model. The limitation is that concurrent training jobs should later use file locking or an external registry database.

The feature store versions every engineered training matrix. It writes Parquet when available and falls back to pickle when Parquet engines are missing. This improves reproducibility while keeping local development usable. The trade-off is that pickle is Python-specific, so production should prefer Parquet with `pyarrow` or `fastparquet`.

## Evaluation and Monitoring

The evaluation layer computes accuracy, balanced accuracy, macro and weighted precision/recall/F1, MCC, Cohen's Kappa, ROC-AUC, PR-AUC, log loss, Brier score, Top-K accuracy, cross-validation summaries, and bootstrap confidence intervals. This improves model governance because model quality is judged from multiple perspectives instead of a single hit rate. The limitation is that some metrics require enough resolved predictions per class to be meaningful.

Prediction logs include timestamps, confidence, uncertainty, model version, probabilities, features used, risk level, recommendation, and resolution status. This improves auditability and post-hoc analysis. The trade-off is log growth; production should rotate or stream logs into a database.

Drift detection tracks feature drift and prediction drift. Continuous learning retrains only after drift and new-round thresholds justify it, then promotes only validated results. This improves long-term reliability under changing game behavior. The limitation is that drift thresholds need calibration against real operations.

## Dashboard API and UX

The dashboard exposes granular endpoints plus `/api/dashboard/enterprise-summary`, a single aggregate payload for finance-style dashboards. This improves UI performance and consistency by refreshing one contract containing live prediction, confidence, probability distribution, trends, confusion matrix, ROC, PR, calibration, feature importance, class distribution, drift, training history, model comparison, system health, dataset stats, and prediction logs.

The trade-off is that the summary endpoint does more work per request. In production it should be cached for a short interval or pushed over WebSocket for high-frequency updates.

## Operational Notes

Install dependencies from `aviator_enterprise/requirements.txt` before running the enterprise API. The local system Python currently does not include required packages such as `scikit-learn` and `aiofiles`.

Run:

```bash
python -m venv .venv-enterprise
. .venv-enterprise/bin/activate
pip install -r aviator_enterprise/requirements.txt
cd aviator_enterprise
uvicorn main:app --host 0.0.0.0 --port 8000
```

The codebase compiles successfully with `python3 -m compileall aviator_enterprise`.
