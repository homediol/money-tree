/**
 * Aviator ML Enterprise — Dashboard Controller
 * ==============================================
 * All data originates from roundhistory.json via the backend.
 * No live game feed · No crash simulator · No auto-prediction WebSocket.
 *
 * WebSocket (/ws/metrics) receives lightweight metrics snapshots only —
 * it never auto-generates predictions.
 *
 * Predictions are generated only when the user clicks "Run Prediction".
 */

// ──────────────────────────────────────────────────────────── Toast
const Toast = {
  show(msg, type = 'info', duration = 4500) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    el.onclick = () => el.remove();
    document.getElementById('toast-stack').appendChild(el);
    setTimeout(() => el.remove(), duration);
  },
  success: (m) => Toast.show(m, 'success'),
  error:   (m) => Toast.show(m, 'error', 7000),
  warning: (m) => Toast.show(m, 'warning'),
  info:    (m) => Toast.show(m, 'info'),
};

// ──────────────────────────────────────────────────────────── Helpers
const fmt = {
  pct:  (v) => (v == null || isNaN(+v)) ? '--' : (+v * 100).toFixed(1) + '%',
  num:  (v, d=3) => (v == null || isNaN(+v)) ? '--' : (+v).toFixed(d),
  int:  (v) => v == null ? '--' : Math.round(+v).toLocaleString(),
  ms:   (v) => (v == null || isNaN(+v)) ? '--' : (+v).toFixed(1) + ' ms',
  time: (ts) => { try { return ts ? new Date(ts).toLocaleTimeString() : '--'; } catch { return ts||'--'; } },
  dt:   (ts) => { try { return ts ? new Date(ts).toLocaleString()     : '--'; } catch { return ts||'--'; } },
};

const el   = (id) => document.getElementById(id);
const set  = (id, v)   => { const e = el(id); if (e) e.textContent = v ?? '--'; };
const setH = (id, html) => { const e = el(id); if (e) e.innerHTML = html; };

function riskCls(r) {
  return { LOW:'risk-low', MEDIUM:'risk-medium', HIGH:'risk-high', EXTREME:'risk-extreme' }[(r||'').toUpperCase()] || '';
}
function recoCls(r) {
  return { BET:'reco-bet', WEAK_BET:'reco-weak', SKIP:'reco-skip' }[(r||'').toUpperCase()] || '';
}

// ──────────────────────────────────────────────────────────── Router
const Router = {
  sections: ['overview','prediction','performance','curves','models','training','features','drift','explainability','system'],
  titles: {
    overview:'Overview', prediction:'Prediction', performance:'Performance',
    curves:'Evaluation Curves', models:'Model Registry', training:'Training',
    features:'Features & Data', drift:'Drift Detection', explainability:'Explainability', system:'System',
  },
  current: 'overview',

  init() {
    document.querySelectorAll('.nav-item').forEach(a => {
      a.addEventListener('click', e => { e.preventDefault(); this.go(a.dataset.section); });
    });
  },

  go(section) {
    if (!this.sections.includes(section)) return;
    this.sections.forEach(s => el(`section-${s}`)?.classList.remove('active'));
    el(`section-${section}`)?.classList.add('active');
    document.querySelectorAll('.nav-item').forEach(a =>
      a.classList.toggle('active', a.dataset.section === section)
    );
    set('page-title', this.titles[section] || section);
    this.current = section;
    App.loadSection(section);
  },
};

// ──────────────────────────────────────────────────────────── Metrics-only WebSocket
// Receives health/counts pushes — does NOT auto-generate predictions.
const WS = {
  socket: null,
  _delay: 3000,
  _attempts: 0,

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    try {
      this.socket = new WebSocket(`${proto}://${location.host}/ws/metrics`);
      this.socket.onopen    = () => { this._attempts = 0; this._setStatus(true); };
      this.socket.onmessage = (e) => this._onMessage(e);
      this.socket.onclose   = () => { this._setStatus(false); this._reconnect(); };
      this.socket.onerror   = () => {};
    } catch { this._reconnect(); }
  },

  _setStatus(online) {
    const dot = el('ws-dot'), lbl = el('ws-label');
    if (dot) dot.className = `ws-dot ${online ? 'on' : 'off'}`;
    if (lbl) lbl.textContent = online ? 'Connected' : 'Offline';
  },

  _onMessage(e) {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'metrics') {
        // Update counts and health from push — no prediction triggered
        App._applyMetricsPush(d);
      }
    } catch {}
  },

  _reconnect() {
    this._attempts++;
    setTimeout(() => this.connect(), Math.min(this._delay * this._attempts, 30000));
  },
};

// ──────────────────────────────────────────────────────────── Prediction UI
const PredictionUI = {
  lastId: null,
  lastPrediction: null,

  render(p) {
    if (!p) return;
    this.lastId = p.prediction_id;
    this.lastPrediction = p;

    const cls = p.predicted_class || p.prediction_class || '--';
    set('pred-class', cls);
    set('pred-ts', fmt.time(p.timestamp));

    const conf = p.confidence || 0;
    set('pred-conf', fmt.pct(conf));
    const circ = 2 * Math.PI * 58;
    const ring = el('ring-fill');
    if (ring) ring.style.strokeDashoffset = (circ * (1 - conf)).toFixed(2);

    const riskEl = el('pred-risk');
    if (riskEl) { riskEl.className = `risk-tag ${riskCls(p.risk_level)}`; riskEl.textContent = p.risk_level || '--'; }
    const recoEl = el('pred-reco');
    if (recoEl) { recoEl.className = `reco-tag ${recoCls(p.recommendation)}`; recoEl.textContent = p.recommendation || '--'; }

    set('pred-unc',     fmt.pct(p.uncertainty));
    set('pred-model',   (p.model_name||'') + ' ' + (p.model_version||''));
    set('pred-latency', fmt.ms(p.latency_ms));
    set('ss-latency',   fmt.ms(p.latency_ms));

    const barsEl = el('prob-bars');
    if (barsEl && p.probabilities) {
      barsEl.innerHTML = Object.entries(p.probabilities)
        .sort((a,b) => b[1]-a[1])
        .map(([c, prob]) => `
          <div class="pbar">
            <span class="pbar-lbl">${c}</span>
            <div class="pbar-track"><div class="pbar-fill" style="width:${(prob*100).toFixed(1)}%"></div></div>
            <span class="pbar-val">${(prob*100).toFixed(1)}%</span>
          </div>`).join('');
    }

    ExplainUI.render(p);
    LogUI.prepend(p);
  },
};

// ──────────────────────────────────────────────────────────── Log UI
const LogUI = {
  _log: [], _max: 300,

  prepend(p) {
    if (!p) return;
    this._log.unshift(p);
    if (this._log.length > this._max) this._log.length = this._max;
    this._renderRow(p, true);
    set('log-badge', this._log.length);
  },

  renderAll(preds) {
    if (!preds) return;
    const tbody = el('pred-log-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    [...preds].forEach(p => this._renderRow(p, false));
    set('log-badge', preds.length);
  },

  _renderRow(p, prepend) {
    const tbody = el('pred-log-body');
    if (!tbody) return;
    const correct = p.correct === true  ? '<span class="text-success">✓</span>'
                  : p.correct === false ? '<span class="text-danger">✗</span>' : '--';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${fmt.time(p.timestamp)}</td>
      <td class="num" title="${p.prediction_id||''}">${(p.prediction_id||'').slice(0,8)}</td>
      <td>${p.predicted_class_name||p.predicted_class||'--'}</td>
      <td class="num">${fmt.pct(p.confidence)}</td>
      <td><span class="risk-tag ${riskCls(p.risk_level)}">${p.risk_level||'--'}</span></td>
      <td><span class="reco-tag ${recoCls(p.recommendation)}">${p.recommendation||'--'}</span></td>
      <td>${p.model_name||'--'}</td>
      <td class="num">${p.model_version||'--'}</td>
      <td>${p.actual_class_name||'--'}</td>
      <td>${correct}</td>`;
    prepend && tbody.firstChild ? tbody.insertBefore(tr, tbody.firstChild) : tbody.appendChild(tr);
  },
};

// ──────────────────────────────────────────────────────────── Metrics UI
const MetricsUI = {
  DEFS: [
    { key:'accuracy',          label:'Accuracy',           fmt:'pct' },
    { key:'balanced_accuracy', label:'Balanced Accuracy',  fmt:'pct' },
    { key:'precision_macro',   label:'Precision (Macro)',  fmt:'pct' },
    { key:'recall_macro',      label:'Recall (Macro)',     fmt:'pct' },
    { key:'f1_macro',          label:'F1 (Macro)',         fmt:'pct' },
    { key:'precision_weighted',label:'Precision (Wtd)',    fmt:'pct' },
    { key:'recall_weighted',   label:'Recall (Wtd)',       fmt:'pct' },
    { key:'f1_weighted',       label:'F1 (Weighted)',      fmt:'pct' },
    { key:'mcc',               label:'MCC',                fmt:'num3' },
    { key:'cohen_kappa',       label:"Cohen's Kappa",      fmt:'num3' },
    { key:'roc_auc',           label:'ROC-AUC',            fmt:'num3' },
    { key:'pr_auc',            label:'PR-AUC',             fmt:'num3' },
    { key:'log_loss',          label:'Log Loss',           fmt:'num3', invert:true },
    { key:'brier_score',       label:'Brier Score',        fmt:'num3', invert:true },
    { key:'top_2_accuracy',    label:'Top-2 Accuracy',     fmt:'pct' },
    { key:'top_3_accuracy',    label:'Top-3 Accuracy',     fmt:'pct' },
  ],

  render(metrics, containerId='eval-metrics-full') {
    const c = el(containerId);
    if (!c || !metrics) return;
    c.innerHTML = this.DEFS.map(d => {
      const v = metrics[d.key];
      const display = (v == null || isNaN(+v)) ? '--'
        : d.fmt === 'pct' ? fmt.pct(+v) : fmt.num(+v, 4);
      let cls = '';
      if (v != null && !isNaN(+v)) {
        const thr = d.fmt === 'pct' ? 0.6 : 0.5;
        cls = (d.invert ? +v < thr : +v >= thr) ? 'good' : 'warn';
      }
      return `<div class="m-card">
        <div class="m-card-label">${d.label}</div>
        <div class="m-card-value ${cls}">${display}</div></div>`;
    }).join('');
  },
};

// ──────────────────────────────────────────────────────────── Model UI
const ModelUI = {
  render(rows) {
    const tbody = el('model-table-body');
    if (!tbody || !rows) return;
    tbody.innerHTML = rows.map(r => {
      const m = r.metrics || r;
      return `<tr class="${r.is_active ? 'active-row' : ''}">
        <td>${r.model_name}</td>
        <td class="num">${r.version||'--'}</td>
        <td>${r.is_active ? '<span class="badge ok">ACTIVE</span>' : ''}</td>
        <td class="num">${fmt.pct(m.accuracy)}</td>
        <td class="num">${fmt.pct(m.balanced_accuracy)}</td>
        <td class="num">${fmt.pct(m.f1_macro)}</td>
        <td class="num">${fmt.num(m.mcc)}</td>
        <td class="num">${fmt.num(m.cohen_kappa)}</td>
        <td class="num">${fmt.num(m.log_loss)}</td>
        <td class="num">${fmt.num(m.roc_auc)}</td>
        <td class="num">${fmt.num(m.pr_auc)}</td>
        <td class="num">${fmt.num(m.brier_score)}</td>
        <td>${fmt.dt(r.created_at)}</td>
        <td>${!r.is_active
          ? `<button class="btn btn-ghost btn-sm" onclick="App.activateModel('${r.model_name}','${r.version}')">Set Active</button>`
          : '--'}</td></tr>`;
    }).join('');
  },
};

// ──────────────────────────────────────────────────────────── Explain UI
const ExplainUI = {
  render(p) {
    const body  = el('explain-body');
    const sf    = el('shap-features-body');
    const rSum  = el('reasoning-summary');
    if (rSum && p.reasoning) rSum.textContent = p.reasoning;
    if (!p.top_features?.length) {
      const ph = '<div class="explain-placeholder">No SHAP data for this prediction</div>';
      if (body) body.innerHTML = ph;
      if (sf)   sf.innerHTML   = ph;
      return;
    }
    if (body) body.innerHTML = `
      <div class="explain-summary">
        Prediction: <strong>${p.predicted_class||'--'}</strong>
        · Confidence: <strong>${fmt.pct(p.confidence)}</strong>
        · Risk: <span class="risk-tag ${riskCls(p.risk_level)}">${p.risk_level||'--'}</span>
        · Signal: <span class="reco-tag ${recoCls(p.recommendation)}">${p.recommendation||'--'}</span>
      </div>`;
    if (sf) sf.innerHTML = p.top_features.slice(0,12).map(f => {
      const cls = f.impact === 'positive' ? 'pos' : 'neg';
      return `<div class="shap-feature ${cls}">
        <span class="shap-name">${f.feature}</span>
        <span class="text-muted" style="font-size:10px">${fmt.num(f.value,3)}</span>
        <span class="shap-val ${cls}">${f.shap_value>=0?'+':''}${fmt.num(f.shap_value,4)}</span></div>`;
    }).join('');
  },
};

// ──────────────────────────────────────────────────────────── Drift UI
const DriftUI = {
  renderTable(snap) {
    const tbody = el('drift-feat-body');
    if (!tbody || !snap) return;
    const feats = Object.entries(snap.feature_drift||{}).sort((a,b)=>b[1]-a[1]).slice(0,25);
    tbody.innerHTML = feats.map(([name, score]) => {
      const status = score>0.2
        ? '<span class="badge bad">HIGH</span>'
        : score>0.1 ? '<span class="badge warn">MEDIUM</span>'
        : '<span class="badge ok">LOW</span>';
      return `<tr><td class="num">${name}</td><td class="num">${fmt.num(score,4)}</td><td>${status}</td></tr>`;
    }).join('');
    const overall = snap.overall_score || snap.overall_drift || 0;
    const badge = el('drift-status-badge');
    if (badge) {
      badge.className = `badge ${overall>0.2?'bad':overall>0.1?'warn':'ok'}`;
      badge.textContent = overall>0.2?'HIGH DRIFT':overall>0.1?'MEDIUM':'STABLE';
    }
  },
};

// ──────────────────────────────────────────────────────────── System UI
const SystemUI = {
  render(h) {
    if (!h) return;
    set('sys-cpu',     fmt.num(h.cpu_percent,1));
    set('sys-mem',     fmt.num(h.memory_percent,1));
    set('sys-disk',    fmt.num(h.disk_percent,1));
    set('sys-gpu',     h.gpu_percent != null ? fmt.num(h.gpu_percent,1) : '--');
    set('sys-uptime',  fmt.int(h.uptime_seconds));
    set('sys-err',     fmt.num((h.error_rate||0)*100,1));
    set('sys-latency', fmt.ms(h.inference_latency_ms));
    const score = h.health_score;
    set('health-score', score != null ? fmt.num(score,1) : '--');
    const dot = el('health-dot');
    if (dot && score != null) {
      dot.className = `health-dot ${score>=70?'ok':score>=40?'warn':'bad'}`;
    }
    set('ss-latency', fmt.ms(h.inference_latency_ms));
  },
};

// ──────────────────────────────────────────────────────────── Main App
const App = {
  _timer: null,
  _interval: 8000,          // poll interval for the active section
  _lastPrediction: null,

  async init() {
    if (typeof Chart !== 'undefined') Charts.init();
    Router.init();
    this._bindEvents();
    await this.loadSection('overview');
    this._startAutoRefresh();
    WS.connect();
  },

  _bindEvents() {
    // Run Prediction (reads from roundhistory — user-initiated only)
    el('btn-predict')?.addEventListener('click', () => this.runPrediction());

    // Refresh current section
    el('btn-refresh')?.addEventListener('click', () => this.loadSection(Router.current, true));

    // Resolve actual outcome
    el('btn-resolve')?.addEventListener('click', () => this.resolve());

    // Quick-train sidebar
    el('btn-quick-train')?.addEventListener('click', () => {
      this.trainModel(el('quick-model')?.value || 'xgboost');
    });

    // Full train form
    el('btn-train-submit')?.addEventListener('click', () => {
      this.trainModel(el('train-model-name')?.value || 'xgboost', {
        optimize:        el('train-optimize')?.checked || false,
        cross_validate:  el('train-cv')?.checked !== false,
        calibrate:       el('train-calibrate')?.checked !== false,
        class_imbalance: el('train-imbalance')?.value || 'class_weight',
      });
    });

    // Model registry refresh
    el('btn-refresh-registry')?.addEventListener('click', () => this.loadSection('models', true));

    // Continuous learning
    el('btn-cl-start')?.addEventListener('click', () => this.clStart());
    el('btn-cl-stop')?.addEventListener('click',  () => this.clStop());

    // Export buttons
    document.querySelectorAll('[data-export]').forEach(btn =>
      btn.addEventListener('click', () => this.doExport(btn.dataset.export))
    );

    // Clear prediction log
    el('btn-clear-log')?.addEventListener('click', () => {
      if (el('pred-log-body')) el('pred-log-body').innerHTML = '';
      LogUI._log = [];
      set('log-badge', '0');
    });

    // Mobile sidebar
    el('menu-toggle')?.addEventListener('click', () => el('sidebar')?.classList.toggle('open'));

    // Modal close
    el('modal-close')?.addEventListener('click', () => { if (el('modal-backdrop')) el('modal-backdrop').hidden = true; });
  },

  _startAutoRefresh() {
    clearInterval(this._timer);
    this._timer = setInterval(() => this.loadSection(Router.current), this._interval);
  },

  // Called by the WS metrics push — updates counts/health without running a prediction
  _applyMetricsPush(d) {
    if (d.counts) {
      set('v-total',    fmt.int(d.counts.total));
      set('v-pending',  `${fmt.int(d.counts.pending)} pending`);
      set('v-resolved', fmt.int(d.counts.resolved));
    }
    if (d.health) SystemUI.render(d.health);
  },

  // ── User actions ─────────────────────────────────────────────────────────
  async runPrediction() {
    const btn = el('btn-predict');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Running…'; }
    try {
      const model = el('quick-model')?.value || 'xgboost';
      const p = await window.api.predict(model, true);
      PredictionUI.render(p);
      this._lastPrediction = p;
      Router.go('prediction');
      Toast.success('Prediction generated from round history');
    } catch (e) {
      Toast.error('Prediction failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '▶ Run Prediction'; }
    }
  },

  async resolve() {
    const mult = parseFloat(el('resolve-mult')?.value);
    const id   = PredictionUI.lastId;
    const msg  = el('resolve-msg');
    if (!id) { if (msg) { msg.className='resolve-msg err'; msg.textContent='No prediction to resolve'; } return; }
    if (!mult || mult < 1) { if (msg) { msg.className='resolve-msg err'; msg.textContent='Enter a multiplier ≥ 1.0'; } return; }
    try {
      await window.api.resolvePrediction(id, mult);
      if (msg) { msg.className='resolve-msg ok'; msg.textContent=`✓ Resolved ×${mult}`; }
      if (el('resolve-mult')) el('resolve-mult').value = '';
      Toast.success(`Round resolved — ×${mult}`);
    } catch (e) {
      if (msg) { msg.className='resolve-msg err'; msg.textContent='Failed: '+e.message; }
    }
  },

  async trainModel(name, opts = {}) {
    const prog   = el('train-progress') || el('train-progress-full');
    const bar    = el('train-bar')      || el('train-bar-full');
    const status = el('train-status')   || el('train-status-full');
    if (prog)   prog.style.display = 'block';
    if (bar)    bar.classList.add('animate');
    if (status) status.textContent = `Training ${name}…`;
    try {
      const r = await window.api.trainModel({ model_name: name, ...opts });
      if (status) status.textContent = `✓ ${name} v${r.model_version} — F1: ${fmt.pct(r.metrics?.f1_macro)}`;
      const res = el('train-result');
      if (res) res.innerHTML = `<pre>${JSON.stringify({version:r.model_version,metrics:r.metrics},null,2)}</pre>`;
      Toast.success(`${name} trained successfully`);
    } catch (e) {
      if (status) status.textContent = `✗ ${e.message}`;
      Toast.error('Training failed: ' + e.message);
    } finally {
      if (bar) bar.classList.remove('animate');
    }
  },

  async activateModel(name, version) {
    try {
      await window.api.activateVersion(name, version);
      Toast.success(`Activated ${name} ${version}`);
      this.loadSection('models', true);
    } catch (e) { Toast.error('Activation failed: ' + e.message); }
  },

  async clStart() {
    const interval = parseInt(el('cl-interval')?.value) || 3600;
    try {
      await window.api.clStart({ model_name: 'xgboost', interval_seconds: interval });
      set('cl-badge', 'running');
      if (el('cl-badge')) el('cl-badge').className = 'badge ok';
      Toast.success('Continuous learning started');
    } catch (e) { Toast.error('CL start failed: ' + e.message); }
  },

  async clStop() {
    try {
      await window.api.clStop();
      set('cl-badge', 'stopped');
      if (el('cl-badge')) el('cl-badge').className = 'badge';
      Toast.warning('Continuous learning stopped');
    } catch (e) { Toast.error(e.message); }
  },

  async doExport(kind) {
    try {
      const blob = await window.api[`export${kind[0].toUpperCase()+kind.slice(1)}`]();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href = url; a.download = `aviator-export.${kind}`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { Toast.error('Export failed: ' + e.message); }
  },

  // ── Section loaders ──────────────────────────────────────────────────────
  async loadSection(section, force=false) {
    try {
      switch(section) {
        case 'overview':       await this._loadOverview();       break;
        case 'prediction':     await this._loadPredictionLog();  break;
        case 'performance':    await this._loadPerformance();    break;
        case 'curves':         await this._loadCurves();         break;
        case 'models':         await this._loadModels();         break;
        case 'training':       await this._loadTraining();       break;
        case 'features':       await this._loadFeatures();       break;
        case 'drift':          await this._loadDrift();          break;
        case 'explainability': /* rendered via runPrediction */  break;
        case 'system':         await this._loadSystem();         break;
      }
    } catch (e) { console.warn('Section load error:', section, e); }
  },

  async _loadOverview() {
    const [ov, trend, rolling, confHist, probDist, retrain] = await Promise.allSettled([
      window.api.overview(),
      window.api.accuracyTrend(),
      window.api.rollingMetrics(100),
      window.api.confidenceHistogram(),
      window.api.probDistribution(),
      window.api.retrainingInfo(),
    ]);

    if (ov.status==='fulfilled') {
      const d = ov.value;
      const active = d.active_model, best = d.best_model;
      const counts = d.counts||{}, health = d.health||{}, drift = d.drift||{};
      set('v-model',     active?.model_name||'--');
      set('v-model-ver', active?.version||'--');
      set('v-best',      best?.model_name||'--');
      set('v-best-f1',   best ? `F1 ${fmt.pct(best.metrics?.f1_macro)}` : '--');
      set('v-total',     fmt.int(counts.total));
      set('v-pending',   `${fmt.int(counts.pending)} pending`);
      set('v-resolved',  fmt.int(counts.resolved));
      set('v-accuracy',  counts.correct&&counts.resolved ? fmt.pct(counts.correct/counts.resolved)+' acc' : '-- acc');
      set('v-health',    fmt.num(health.health_score,1));
      set('v-health-sub',health.health_score>=70?'Good':health.health_score>=40?'Fair':'Poor');
      set('v-drift',     fmt.num(drift.overall_score,4));
      set('v-drift-sub', drift.drift_detected?'Drift detected':'Stable');
      set('ss-model',    active?.model_name||'--');
      set('ss-version',  active?.version||'--');
      set('ss-trained',  fmt.dt(active?.created_at));
      SystemUI.render(health);
      MetricsUI.render(d.rolling||{}, 'metrics-grid-ov');
    }

    if (retrain.status==='fulfilled') {
      const r = retrain.value;
      set('ss-model-ver',   r.model_version);
      set('ss-ds-ver',      r.dataset_version);
      set('ss-trained',     fmt.dt(r.last_training_time));
      set('ss-next',        r.cl_running ? fmt.dt(r.next_scheduled_retrain) : 'Manual');
    }

    if (trend.status==='fulfilled' && trend.value.trend?.length) {
      const t = trend.value.trend;
      const labels = t.map(d => fmt.time(d.timestamp));
      Charts.line('chart-accuracy-trend', labels,
        [{ label:'Accuracy', data:t.map(d=>(d.accuracy*100).toFixed(2)) }]);
      Charts.line('chart-metrics-trend', labels, [
        { label:'Precision', data:t.map(d=>(d.precision*100).toFixed(2)) },
        { label:'Recall',    data:t.map(d=>(d.recall*100).toFixed(2)) },
        { label:'F1',        data:t.map(d=>(d.f1_macro*100).toFixed(2)) },
      ]);
    }

    if (confHist.status==='fulfilled') {
      const h = confHist.value;
      const bins = (h.bins||[]).map(b=>`${(b[0]*100).toFixed(0)}-${(b[1]*100).toFixed(0)}%`);
      Charts.bar('chart-confidence-hist', bins, [{ label:'Count', data:h.counts||[] }]);
    }

    if (probDist.status==='fulfilled' && probDist.value.data?.length) {
      const pd = probDist.value;
      Charts.bar('chart-prob-dist', pd.classes, pd.data.map(d=>({
        label:d.class, data:d.values.map(v=>(v*100).toFixed(2))
      })));
    }

    if (rolling.status==='fulfilled') {
      const m = rolling.value.metrics||{};
      MetricsUI.render(m, 'metrics-grid-ov');
      const keys=['accuracy','f1_macro','precision_macro','recall_macro'];
      Charts.bar('chart-rolling-perf', keys.map(k=>k.replace(/_/g,' ').toUpperCase()),
        [{ label:'Score', data:keys.map(k=>((m[k]||0)*100).toFixed(2)) }]);
    }
  },

  async _loadPredictionLog() {
    const log = await window.api.predictionLog(100).catch(()=>({predictions:[]}));
    LogUI.renderAll(log.predictions||[]);
  },

  async _loadPerformance() {
    const [rolling, cm, perClass] = await Promise.allSettled([
      window.api.rollingMetrics(500),
      window.api.confusionMatrix(),
      window.api.rollingMetrics(500),
    ]);
    if (rolling.status==='fulfilled') MetricsUI.render(rolling.value.metrics||rolling.value, 'eval-metrics-full');
    if (cm.status==='fulfilled' && cm.value.matrix?.length) Charts.confusionMatrix('chart-confusion', cm.value);
    // Per-class bar chart
    if (rolling.status==='fulfilled') {
      const m = rolling.value.metrics||{};
      const keys=['precision_macro','recall_macro','f1_macro','mcc'];
      Charts.bar('chart-per-class', keys.map(k=>k.replace(/_/g,' ').toUpperCase()),
        [{ label:'Score', data:keys.map(k=>(+(m[k]||0)*100).toFixed(2)) }]);
    }
    // Rolling metrics line
    const rm = await window.api.rollingMetrics(200).catch(()=>({metrics:{}}));
    const metrics = rm.metrics||{};
    const idx = Array.from({length:1},(_,i)=>i+1);
    Charts.bar('chart-rolling-metrics',
      ['Accuracy','F1 Macro','Precision','Recall','MCC','Kappa'],
      [{ label:'Score', data:[metrics.accuracy,metrics.f1_macro,metrics.precision_macro,
         metrics.recall_macro,metrics.mcc,metrics.cohen_kappa].map(v=>(+(v||0)*100).toFixed(2)) }]
    );
  },

  async _loadCurves() {
    const [roc, pr, cal] = await Promise.allSettled([
      window.api.rocCurve(), window.api.prCurve(), window.api.calibrationCurve()
    ]);
    if (roc.status==='fulfilled' && Object.keys(roc.value).length) Charts.multiRoc('chart-roc', roc.value);
    if (pr.status==='fulfilled'  && Object.keys(pr.value).length)  Charts.multiPr('chart-pr', pr.value);
    if (cal.status==='fulfilled' && Object.keys(cal.value).length) Charts.calibration('chart-calibration', cal.value);
    const conf = await window.api.confidence().catch(()=>null);
    if (conf?.bins) {
      const labels = conf.bins.map(b=>`${(b[0]*100).toFixed(0)}%`);
      Charts.line('chart-reliability', labels, [
        { label:'Fraction Positive', data:(conf.accuracies||[]).map(v=>v!=null?(v*100).toFixed(1):null), spanGaps:false },
        { label:'Perfect',           data:conf.bins.map(b=>((b[0]+b[1])/2*100).toFixed(1)), borderDash:[5,5] },
      ]);
    }
  },

  async _loadModels() {
    const [comp, registry] = await Promise.allSettled([
      window.api.modelComparison(), window.api.modelRegistry()
    ]);
    if (comp.status==='fulfilled') ModelUI.render(comp.value.rows||[]);
    if (registry.status==='fulfilled' && registry.value.versions?.length) {
      const vers = registry.value.versions.slice(0,10);
      Charts.bar('chart-model-compare',
        vers.map(v=>`${v.model_name} ${v.version}`), [
          { label:'Accuracy', data:vers.map(v=>((v.metrics?.accuracy||0)*100).toFixed(2)) },
          { label:'F1 Macro', data:vers.map(v=>((v.metrics?.f1_macro||0)*100).toFixed(2)) },
          { label:'ROC-AUC',  data:vers.map(v=>((v.metrics?.roc_auc||0)*100).toFixed(2)) },
        ]);
    }
  },
