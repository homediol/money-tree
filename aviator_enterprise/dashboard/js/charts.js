/**
 * Aviator ML Enterprise — Chart Library v3
 * =========================================
 * Wraps Chart.js with a consistent dark-theme design system.
 * All charts are created via factory methods and cached by canvas ID
 * so they can be updated in-place without flicker.
 *
 * Design tokens mirror the CSS custom properties defined in style.css.
 */

const PALETTE = {
  cyan:    '#22d3ee',
  violet:  '#a78bfa',
  green:   '#34d399',
  pink:    '#f472b6',
  orange:  '#fb923c',
  yellow:  '#fbbf24',
  blue:    '#60a5fa',
  red:     '#f87171',
  grey:    '#64748b',
  teal:    '#2dd4bf',
  lime:    '#a3e635',
};

const COLORS = Object.values(PALETTE);

/** Hex colour with alpha suffix (0-1) */
function alpha(hex, a) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${a})`;
}

/** Shared axis config */
function axis(opts = {}) {
  return {
    ticks: { color: '#6e84a8', font: { size: 10, family: 'var(--mono, monospace)' }, maxTicksLimit: 8 },
    grid:  { color: 'rgba(255,255,255,0.04)', drawBorder: false },
    border: { display: false },
    ...opts,
  };
}

/** Shared tooltip config */
const TOOLTIP = {
  backgroundColor: 'rgba(6,11,20,0.96)',
  borderColor: 'rgba(255,255,255,0.10)',
  borderWidth: 1,
  titleColor: '#e8f0ff',
  bodyColor:  '#b4c5e0',
  padding: 11,
  cornerRadius: 8,
  displayColors: true,
  boxPadding: 4,
};

/** Shared legend config */
const LEGEND = {
  labels: {
    color: '#b4c5e0',
    font: { size: 11 },
    boxWidth: 12,
    boxHeight: 3,
    usePointStyle: true,
    pointStyle: 'line',
  },
};

/** Shared global options applied to every chart */
const GLOBAL_OPTS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 500, easing: 'easeOutQuart' },
  plugins: { legend: LEGEND, tooltip: TOOLTIP },
  scales: { x: axis(), y: axis() },
};

const Charts = {
  _cache: new Map(),

  /** Initialize global Chart.js defaults */
  init() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.color      = '#b4c5e0';
    Chart.defaults.font.size  = 11;
    Chart.defaults.borderColor = 'rgba(255,255,255,0.05)';
    Chart.defaults.elements.line.borderWidth  = 2;
    Chart.defaults.elements.point.radius      = 0;
    Chart.defaults.elements.point.hoverRadius = 4;
  },

  /** Create or replace a chart instance */
  _make(id, config) {
    if (this._cache.has(id)) {
      try { this._cache.get(id).destroy(); } catch {}
      this._cache.delete(id);
    }
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const chart = new Chart(canvas, config);
    this._cache.set(id, chart);
    return chart;
  },

  /** Deep-merge two plain objects */
  _merge(base, override) {
    const result = { ...base };
    for (const [k, v] of Object.entries(override || {})) {
      if (v && typeof v === 'object' && !Array.isArray(v) && typeof result[k] === 'object')
        result[k] = this._merge(result[k], v);
      else
        result[k] = v;
    }
    return result;
  },

  // ── Line chart ──────────────────────────────────────────────────
  line(id, labels, datasets, extraOpts = {}) {
    const cfg = {
      type: 'line',
      data: {
        labels,
        datasets: datasets.map((d, i) => ({
          tension: 0.35,
          fill: d.fill ?? false,
          pointRadius: d.pointRadius ?? 0,
          pointHoverRadius: 4,
          borderColor:     d.borderColor     || COLORS[i % COLORS.length],
          backgroundColor: d.backgroundColor || alpha(COLORS[i % COLORS.length], d.fill ? 0.12 : 0),
          borderDash: d.borderDash,
          spanGaps: d.spanGaps ?? true,
          ...d,
        })),
      },
      options: this._merge(GLOBAL_OPTS, extraOpts),
    };
    return this._make(id, cfg);
  },

  // ── Bar chart ───────────────────────────────────────────────────
  bar(id, labels, datasets, extraOpts = {}) {
    const cfg = {
      type: 'bar',
      data: {
        labels,
        datasets: datasets.map((d, i) => ({
          borderRadius: 4,
          borderSkipped: false,
          backgroundColor: d.backgroundColor || alpha(COLORS[i % COLORS.length], 0.75),
          borderColor:     d.borderColor     || COLORS[i % COLORS.length],
          borderWidth: 0,
          ...d,
        })),
      },
      options: this._merge(GLOBAL_OPTS, extraOpts),
    };
    return this._make(id, cfg);
  },

  // ── Horizontal bar ──────────────────────────────────────────────
  horizontalBar(id, labels, values, color) {
    return this.bar(id, labels, [{
      label: 'Importance',
      data: values,
      backgroundColor: color || alpha(PALETTE.cyan, 0.75),
      borderRadius: 4,
    }], {
      indexAxis: 'y',
      scales: {
        x: axis(),
        y: { ...axis(), ticks: { color: '#b4c5e0', font: { size: 10 } } },
      },
      plugins: { legend: { display: false } },
    });
  },

  // ── Doughnut ────────────────────────────────────────────────────
  doughnut(id, labels, values) {
    const cfg = {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: COLORS.slice(0, labels.length).map(c => alpha(c, 0.85)),
          borderColor: COLORS.slice(0, labels.length),
          borderWidth: 2,
          hoverOffset: 6,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '62%',
        animation: { duration: 500 },
        plugins: { legend: LEGEND, tooltip: TOOLTIP },
      },
    };
    return this._make(id, cfg);
  },

  // ── ROC Curve (multi-class OvR) ──────────────────────────────────
  multiRoc(id, data) {
    const datasets = [];
    if (data.classes) {
      data.classes.forEach((c, i) => {
        const auc = data.auc?.[i];
        datasets.push({
          label: `Class ${c}${auc != null ? ` (AUC=${auc.toFixed(3)})` : ''}`,
          data: (data.fpr[i] || []).map((f, j) => ({ x: f, y: (data.tpr[i] || [])[j] })),
          borderColor: COLORS[i % COLORS.length],
          fill: false, pointRadius: 0,
        });
      });
    } else if (data.fpr) {
      datasets.push({
        label: `ROC${data.auc != null ? ` (AUC=${(+data.auc).toFixed(3)})` : ''}`,
        data: data.fpr.map((f, j) => ({ x: f, y: data.tpr[j] })),
        borderColor: PALETTE.cyan, fill: false, pointRadius: 0,
      });
    }
    datasets.push({
      label: 'Random', data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
      borderColor: PALETTE.grey, borderDash: [5, 5], fill: false, pointRadius: 0,
    });
    return this._make(id, {
      type: 'scatter',
      data: { datasets },
      options: this._merge(GLOBAL_OPTS, {
        scales: {
          x: axis({ type: 'linear', min: 0, max: 1, title: { display: true, text: 'FPR', color: '#6e84a8' } }),
          y: axis({ min: 0, max: 1, title: { display: true, text: 'TPR', color: '#6e84a8' } }),
        },
        elements: { line: { tension: 0 } },
      }),
    });
  },

  // ── PR Curve ────────────────────────────────────────────────────
  multiPr(id, data) {
    const datasets = [];
    if (data.classes) {
      data.classes.forEach((c, i) => {
        const ap = data.ap?.[i];
        datasets.push({
          label: `Class ${c}${ap != null ? ` (AP=${ap.toFixed(3)})` : ''}`,
          data: (data.recall[i] || []).map((r, j) => ({ x: r, y: (data.precision[i] || [])[j] })),
          borderColor: COLORS[i % COLORS.length],
          fill: false, pointRadius: 0,
        });
      });
    } else if (data.recall) {
      datasets.push({
        label: 'PR',
        data: data.recall.map((r, j) => ({ x: r, y: data.precision[j] })),
        borderColor: PALETTE.violet, fill: false, pointRadius: 0,
      });
    }
    return this._make(id, {
      type: 'scatter',
      data: { datasets },
      options: this._merge(GLOBAL_OPTS, {
        scales: {
          x: axis({ type: 'linear', min: 0, max: 1, title: { display: true, text: 'Recall', color: '#6e84a8' } }),
          y: axis({ min: 0, max: 1, title: { display: true, text: 'Precision', color: '#6e84a8' } }),
        },
      }),
    });
  },

  // ── Calibration Curve ───────────────────────────────────────────
  calibration(id, data) {
    const datasets = [];
    if (data.classes) {
      data.classes.forEach((c, i) => {
        datasets.push({
          label: `Class ${c}`,
          data: (data.mean_predicted[i] || []).map((m, j) => ({ x: m, y: (data.fraction_positive[i] || [])[j] })),
          borderColor: COLORS[i % COLORS.length],
          fill: false, pointRadius: 4, pointHoverRadius: 6,
        });
      });
    } else if (data.mean_predicted) {
      datasets.push({
        label: 'Calibration',
        data: data.mean_predicted.map((m, j) => ({ x: m, y: data.fraction_positive[j] })),
        borderColor: PALETTE.green, fill: false, pointRadius: 4,
      });
    }
    datasets.push({
      label: 'Perfect', data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
      borderColor: PALETTE.grey, borderDash: [5, 5], fill: false, pointRadius: 0,
    });
    return this._make(id, {
      type: 'scatter',
      data: { datasets },
      options: this._merge(GLOBAL_OPTS, {
        scales: {
          x: axis({ type: 'linear', min: 0, max: 1, title: { display: true, text: 'Mean Predicted', color: '#6e84a8' } }),
          y: axis({ min: 0, max: 1, title: { display: true, text: 'Fraction Positive', color: '#6e84a8' } }),
        },
      }),
    });
  },

  // ── Confusion Matrix ─────────────────────────────────────────────
  confusionMatrix(id, cm) {
    const labels = cm.labels || [];
    const matrix = cm.normalized || cm.matrix || [];
    const datasets = matrix.map((row, i) => ({
      label: `Actual ${labels[i] || i}`,
      data: row.map(v => +(v * 100).toFixed(1)),
      backgroundColor: alpha(COLORS[i % COLORS.length], 0.72),
      borderWidth: 0,
      borderRadius: 3,
    }));
    return this._make(id, {
      type: 'bar',
      data: { labels: labels.map((l, i) => `Pred ${l || i}`), datasets },
      options: this._merge(GLOBAL_OPTS, {
        scales: {
          x: axis({ stacked: true }),
          y: axis({ stacked: true, ticks: { callback: v => v + '%' } }),
        },
      }),
    });
  },
};

window.Charts = Charts;
window.PALETTE = PALETTE;

// ── Additional enterprise chart helpers ─────────────────────────────
Charts.gauge = function(id, value, label = '') {
  /** Half-doughnut gauge 0-100 */
  const capped = Math.min(Math.max(value || 0, 0), 100);
  const color = capped >= 80 ? PALETTE.red : capped >= 60 ? PALETTE.orange : capped >= 40 ? PALETTE.yellow : PALETTE.green;
  return this._make(id, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: [capped, 100 - capped],
        backgroundColor: [alpha(color, 0.85), alpha(PALETTE.grey, 0.15)],
        borderColor: [color, 'transparent'],
        borderWidth: [2, 0],
        circumference: 180,
        rotation: 270,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '72%',
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
    },
  });
};

Charts.stackedArea = function(id, labels, datasets) {
  return this.line(id, labels, datasets.map((d, i) => ({
    ...d,
    fill: i === 0 ? 'origin' : `-1`,
    backgroundColor: alpha(COLORS[i % COLORS.length], 0.25),
    borderColor: COLORS[i % COLORS.length],
  })), { scales: { x: axis(), y: { ...axis(), stacked: true } } });
};

Charts.scatter = function(id, dataPoints, label = 'Data') {
  return this._make(id, {
    type: 'scatter',
    data: {
      datasets: [{
        label,
        data: dataPoints,
        backgroundColor: alpha(PALETTE.cyan, 0.65),
        borderColor: PALETTE.cyan,
        pointRadius: 4,
      }],
    },
    options: this._merge(GLOBAL_OPTS, {}),
  });
};

Charts.resourceTimeline = function(id, labels, cpu, mem, gpu) {
  const datasets = [
    { label: 'CPU %', data: cpu, borderColor: PALETTE.cyan,   fill: false },
    { label: 'MEM %', data: mem, borderColor: PALETTE.violet, fill: false },
  ];
  if (gpu && gpu.some(v => v != null)) {
    datasets.push({ label: 'GPU %', data: gpu, borderColor: PALETTE.green, fill: false });
  }
  return this.line(id, labels, datasets);
};

Charts.latencyHistogram = function(id, bins, counts) {
  return this.bar(id, bins, [{
    label: 'Requests',
    data: counts,
    backgroundColor: alpha(PALETTE.violet, 0.72),
    borderColor: PALETTE.violet,
  }], { plugins: { legend: { display: false } } });
};

Charts.featureBar = function(id, features, values) {
  const zipped = features.map((f, i) => ({ f, v: values[i] }))
    .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    .slice(0, 20);
  return this.horizontalBar(id, zipped.map(z => z.f), zipped.map(z => z.v));
};

Charts.shapWaterfall = function(id, features, shapValues) {
  const zipped = features.map((f, i) => ({ f, v: shapValues[i] }))
    .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    .slice(0, 15);
  const labels = zipped.map(z => z.f);
  const data   = zipped.map(z => z.v);
  const colors = data.map(v => v >= 0 ? alpha(PALETTE.green, 0.8) : alpha(PALETTE.red, 0.8));
  return this._make(id, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'SHAP value', data, backgroundColor: colors, borderWidth: 0, borderRadius: 3 }],
    },
    options: this._merge(GLOBAL_OPTS, {
      indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: {
        x: axis({ title: { display: true, text: 'SHAP value', color: '#6e84a8' } }),
        y: { ...axis(), ticks: { color: '#b4c5e0', font: { size: 10 } } },
      },
    }),
  });
};
