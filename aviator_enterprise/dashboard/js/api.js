/**
 * Aviator ML Enterprise — API Client
 * ====================================
 * All predictions come from roundhistory.json via the backend.
 * No live game feed, no crash simulator, no auto-polling WebSocket.
 *
 * WebSocket is used only to push lightweight metrics snapshots
 * (counts, rolling accuracy, health) — it never auto-generates predictions.
 */
class ApiClient {
  constructor(base = '') {
    this.base = base || window.location.origin;
  }

  async _fetch(path, options = {}) {
    const url = `${this.base}${path}`;
    const opts = {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    };
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
    }
    try {
      const res = await fetch(url, opts);
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 300)}`);
      }
      const ct = res.headers.get('content-type') || '';
      if (ct.includes('application/json')) return res.json();
      return res.blob();
    } catch (e) {
      console.error('[API]', path, e.message);
      throw e;
    }
  }

  // ── Dashboard overview ─────────────────────────────────────────
  overview()            { return this._fetch('/api/dashboard/overview'); }
  confidence()          { return this._fetch('/api/dashboard/confidence'); }
  probDistribution()    { return this._fetch('/api/dashboard/probability-distribution'); }
  accuracyTrend()       { return this._fetch('/api/dashboard/accuracy-trend'); }
  metricsTrend()        { return this._fetch('/api/dashboard/metrics-trend'); }
  confusionMatrix()     { return this._fetch('/api/dashboard/confusion-matrix'); }
  rocCurve()            { return this._fetch('/api/dashboard/roc-curve'); }
  prCurve()             { return this._fetch('/api/dashboard/pr-curve'); }
  calibrationCurve()    { return this._fetch('/api/dashboard/calibration-curve'); }
  featureImportance()   { return this._fetch('/api/dashboard/feature-importance'); }
  classDistribution()   { return this._fetch('/api/dashboard/class-distribution'); }
  drift()               { return this._fetch('/api/dashboard/drift'); }
  driftHistory()        { return this._fetch('/api/dashboard/drift-history'); }
  trainingHistory()     { return this._fetch('/api/dashboard/training-history'); }
  learningCurve()       { return this._fetch('/api/dashboard/learning-curve'); }
  lossCurve()           { return this._fetch('/api/dashboard/loss-curve'); }
  validationCurve()     { return this._fetch('/api/dashboard/validation-curve'); }
  modelComparison()     { return this._fetch('/api/dashboard/model-comparison'); }
  confidenceHistogram() { return this._fetch('/api/dashboard/confidence-histogram'); }
  rollingMetrics(w=100) { return this._fetch(`/api/dashboard/rolling-metrics?window=${w}`); }
  predictionLog(n=50)   { return this._fetch(`/api/dashboard/prediction-log?n=${n}`); }
  datasetStats()        { return this._fetch('/api/dashboard/dataset-stats'); }
  systemStats()         { return this._fetch('/api/dashboard/system-stats'); }
  health()              { return this._fetch('/api/dashboard/health'); }
  retrainingInfo()      { return this._fetch('/api/dashboard/retraining-info'); }

  // ── Prediction (from roundhistory only) ───────────────────────
  /**
   * Ask the model for the next-round prediction.
   * Reads roundhistory.json, engineers features on the full series,
   * and returns a predicted class + probabilities.
   */
  predict(modelName = 'xgboost', explain = true) {
    return this._fetch(
      `/api/predictions/predict`,
      { method: 'POST', body: { model_name: modelName, explain } }
    );
  }

  /**
   * Provide the actual outcome so accuracy tracking can be updated.
   * Also appends the resolved round to the in-memory history.
   */
  resolvePrediction(predictionId, actualMultiplier) {
    return this._fetch('/api/predictions/resolve', {
      method: 'POST',
      body: { prediction_id: predictionId, actual_multiplier: actualMultiplier },
    });
  }

  recentPredictions(n = 50)  { return this._fetch(`/api/predictions/recent?n=${n}`); }
  predictionCounts()          { return this._fetch('/api/predictions/counts'); }

  // ── Models ─────────────────────────────────────────────────────
  trainModel(payload)   { return this._fetch('/api/models/train', { method: 'POST', body: payload }); }
  trainAll(payload)     { return this._fetch('/api/models/train-all', { method: 'POST', body: payload }); }
  modelRegistry()       { return this._fetch('/api/models/registry'); }
  activateVersion(modelName, version) {
    return this._fetch(`/api/models/registry/${modelName}/activate/${version}`, { method: 'POST' });
  }
  bestModel(metric = 'f1_macro') { return this._fetch(`/api/models/best?metric=${metric}`); }

  // ── Monitoring / continuous learning ──────────────────────────
  clStatus() { return this._fetch('/api/monitoring/continuous-learning'); }
  clStart(cfg) {
    return this._fetch('/api/monitoring/continuous-learning/start', { method: 'POST', body: cfg });
  }
  clStop() {
    return this._fetch('/api/monitoring/continuous-learning/stop', { method: 'POST' });
  }

  // ── Exports ────────────────────────────────────────────────────
  exportCsv(n=200)   { return this._fetch(`/api/dashboard/export/csv?n=${n}`); }
  exportExcel(n=200) { return this._fetch(`/api/dashboard/export/excel?n=${n}`); }
  exportJson(n=200)  { return this._fetch(`/api/dashboard/export/json?n=${n}`); }
  exportPdf(n=200)   { return this._fetch(`/api/dashboard/export/pdf?n=${n}`); }
}

window.api = new ApiClient();
