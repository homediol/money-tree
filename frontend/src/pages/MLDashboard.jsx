import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle, CheckCircle, Info, TrendingUp,
  RefreshCw, Brain, Activity, Target, Zap, BarChart2,
  AlertCircle, Award, Wifi, WifiOff,
} from 'lucide-react';
import {
  LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';
import Sidebar from '../components/Sidebar.jsx';
import useSocket from '../lib/useSocket.js';
import { api, trainModel } from '../api/client.js';

const CAT_COLORS = {
  VERY_LOW: '#38bdf8', LOW: '#35d4ff', MEDIUM: '#9cff45',
  HIGH: '#f97316', VERY_HIGH: '#ff5277',
};

function scoreColor(v) {
  if (v >= 80) return { text:'text-emerald-400', bg:'bg-emerald-500/15', border:'border-emerald-500/30', label:'Excellent' };
  if (v >= 60) return { text:'text-amber-400',   bg:'bg-amber-400/15',   border:'border-amber-400/30',   label:'Average'   };
  return         { text:'text-rose-400',     bg:'bg-rose-500/15',    border:'border-rose-500/30',    label:'Poor'      };
}

function healthColor(status) {
  if (status === 'Excellent')         return { text:'text-emerald-400', bar:'#10b981' };
  if (status === 'Good')              return { text:'text-cyan-400',    bar:'#06b6d4' };
  if (status === 'Needs Improvement') return { text:'text-amber-400',   bar:'#f59e0b' };
  return                                     { text:'text-rose-400',    bar:'#f43f5e' };
}

function fmt(v) {
  if (v == null || Number.isNaN(+v)) return '—';
  return `${(+v).toFixed(1)}%`;
}

/* ── Reusable components ─────────────────────────────────────────────── */

function MetricCard({ label, value, tooltip, icon: Icon, raw }) {
  const col = scoreColor(raw ?? 0);
  const hasVal = value !== '—';
  return (
    <div className={`rounded-xl border ${col.border} ${col.bg} p-5 backdrop-blur transition-all duration-300 hover:-translate-y-1 hover:shadow-lg`}>
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold uppercase tracking-widest text-slate-400">{label}</span>
        {Icon && <Icon className={`h-4 w-4 ${col.text}`} />}
      </div>
      <div className={`text-4xl font-black tracking-tight ${hasVal ? col.text : 'text-slate-600'}`}>{value}</div>
      {tooltip && <p className="mt-2 text-xs leading-relaxed text-slate-500">{tooltip}</p>}
      {hasVal && (
        <div className={`mt-3 inline-block rounded-full border px-2 py-0.5 text-[10px] font-bold ${col.border} ${col.text}`}>
          {col.label}
        </div>
      )}
    </div>
  );
}

function CountCard({ label, value, color = 'text-white', sub }) {
  return (
    <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
      <div className="mb-2 text-xs font-semibold uppercase tracking-widest text-slate-400">{label}</div>
      <div className={`text-3xl font-black ${color}`}>{value ?? '—'}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

function RecommendationCard({ type, message }) {
  const cfg = {
    warning: { Icon: AlertTriangle, cls: 'text-amber-400 bg-amber-400/10 border-amber-400/30' },
    info:    { Icon: Info,          cls: 'text-cyan-400  bg-cyan-400/10  border-cyan-400/30'  },
    success: { Icon: CheckCircle,   cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' },
    error:   { Icon: AlertCircle,   cls: 'text-rose-400  bg-rose-500/10  border-rose-500/30'  },
  }[type] ?? { Icon: Info, cls: 'text-slate-400 bg-slate-800 border-line' };
  return (
    <div className={`flex items-start gap-3 rounded-xl border p-4 ${cfg.cls.split(' ').slice(1).join(' ')} border-${cfg.cls.split(' ')[2]}`}
         style={{ background: 'transparent' }}>
      <div className={`flex items-start gap-3 w-full`}>
        <cfg.Icon className={`h-4 w-4 mt-0.5 shrink-0 ${cfg.cls.split(' ')[0]}`} />
        <p className="text-sm text-slate-300">{message}</p>
      </div>
    </div>
  );
}

function HealthGauge({ score, status }) {
  const col  = healthColor(status);
  const pct  = Math.min(100, Math.max(0, score ?? 0));
  const circ = 2 * Math.PI * 54;
  const dash = (pct / 100) * circ;
  return (
    <div className="flex flex-col items-center gap-3">
      <div className="relative">
        <svg width="148" height="148" viewBox="0 0 148 148">
          <circle cx="74" cy="74" r="54" fill="none" stroke="#1e293b" strokeWidth="14"/>
          <circle cx="74" cy="74" r="54" fill="none" stroke={col.bar} strokeWidth="14"
            strokeDasharray={`${dash} ${circ}`} strokeLinecap="round"
            transform="rotate(-90 74 74)"
            style={{ transition: 'stroke-dasharray 1.2s cubic-bezier(.4,0,.2,1)' }}/>
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={`text-4xl font-black ${col.text}`}>{score ?? 0}</span>
          <span className="text-xs text-slate-500">/ 100</span>
        </div>
      </div>
      <span className={`rounded-full border px-3 py-1 text-xs font-bold ${col.text}`}
            style={{ borderColor: col.bar + '60' }}>{status}</span>
    </div>
  );
}

const ChartTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-line bg-panel/95 px-3 py-2 text-xs shadow-xl backdrop-blur">
      <p className="mb-1 text-slate-400">{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color }} className="font-semibold">
          {p.name}: {typeof p.value === 'number' ? `${p.value.toFixed(1)}%` : p.value}
        </p>
      ))}
    </div>
  );
};

/* ── Main page ──────────────────────────────────────────────────────── */

export default function MLDashboard() {
  const navigate = useNavigate();
  const { connected: wsConnected } = useSocket();
  const [metrics, setMetrics]       = useState(null);
  const [loading, setLoading]       = useState(false);
  const [training, setTraining]     = useState(false);
  const [error, setError]           = useState('');
  const [lastUpdated, setLastUpdated] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const { data } = await api.get('/model-metrics');
      setMetrics(data);
      setLastUpdated(new Date());
    } catch (e) {
      setError(e?.response?.data?.error || e.message || 'Failed to load metrics.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  const train = async () => {
    setTraining(true); setError('');
    try { await trainModel(30); await refresh(); }
    catch (e) { setError(e?.response?.data?.error || e.message || 'Training failed.'); }
    finally { setTraining(false); }
  };

  /* derived — all from live.* (same dataset, verified) */
  const live     = metrics?.live          ?? {};
  const health   = metrics?.health        ?? {};
  const recs     = metrics?.recommendations ?? [];
  const auditData = metrics?.audit        ?? {};
  const trainLog = metrics?.training_log  ?? {};

  const accuracy  = live.accuracy           ?? 0;
  const precision = live.precision          ?? 0;   // macro, same dataset
  const recall    = live.recall             ?? 0;   // macro, same dataset
  const f1        = live.f1                 ?? 0;   // macro, same dataset
  const wPrec     = live.weighted_precision ?? 0;
  const wF1       = live.weighted_f1        ?? 0;

  /* pie data */
  const pieData = [
    { name: 'Correct',   value: live.correct   ?? 0, fill: '#10b981' },
    { name: 'Incorrect', value: live.incorrect ?? 0, fill: '#f43f5e' },
  ];

  /* bar data — prediction categories */
  const barData = Object.entries(live.prediction_dist ?? {}).map(([cat, n]) => ({
    name: cat.replace('_', ' '), count: n, fill: CAT_COLORS[cat] ?? '#64748b',
  }));

  /* per-class from live data (same dataset) */
  const perClassBar = Object.entries(live.per_class ?? {}).map(([cat, m]) => ({
    name: cat.replace('_', ' '),
    Precision: m.precision, Recall: m.recall, F1: m.f1,
  }));

  /* accuracy history */
  const accHistory = (live.accuracy_history ?? []).map(p => ({
    name: `#${p.index}`, accuracy: p.accuracy,
  }));

  /* calibration curve */
  const calBins = (live.calibration_bins ?? []).map(b => ({
    name: b.range, actual: b.accuracy, expected: b.expected, n: b.n,
  }));

  /* confidence distribution */
  const confBands = live.confidence_dist?.bands ?? {};
  const confBandData = Object.entries(confBands).map(([band, n]) => ({
    name: band, count: n,
    fill: band === 'STRONG_BET' ? '#10b981' : band === 'BET' ? '#06b6d4'
        : band === 'WEAK_BET'  ? '#f59e0b' : '#ef4444',
  }));

  const prfTrend = perClassBar;

  return (
    <div className="flex min-h-screen bg-ink text-white">
      <Sidebar activeTab="ml-dashboard" onTabChange={() => {}} onRefresh={refresh}
               loading={loading} onTrain={train} training={training} />

      {/* mobile bottom bar */}
      <div className="fixed bottom-0 left-0 right-0 z-50 flex border-t border-line bg-panel/95 backdrop-blur lg:hidden">
        <button onClick={() => navigate('/')}
          className="flex-1 py-3 text-center text-[11px] font-bold uppercase tracking-wide text-slate-500">
          Dashboard
        </button>
        <button className="flex-1 border-t-2 border-violet-400 py-3 text-center text-[11px] font-bold uppercase tracking-wide text-violet-400">
          ML Performance
        </button>
      </div>

      <main className="flex-1 overflow-auto pb-20 lg:pb-0">
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">

          {/* Header */}
          <header className="mb-6 flex flex-col gap-4 border-b border-line pb-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-violet-400">
                <Brain className="h-3.5 w-3.5" />
                Aviator ML System
              </div>
              <h1 className="mt-2 text-3xl font-black sm:text-4xl">ML Performance Dashboard</h1>
              <p className="mt-1 text-sm text-slate-400">
                Real-time model evaluation — how well the AI is predicting crash outcomes
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <span className={`flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-semibold ${
                wsConnected ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                            : 'border-amber-500/30 bg-amber-500/10 text-amber-400'}`}>
                {wsConnected ? <Wifi className="h-3.5 w-3.5"/> : <WifiOff className="h-3.5 w-3.5"/>}
                {wsConnected ? 'Live' : 'Polling'}
              </span>
              {lastUpdated && (
                <span className="rounded-md border border-line px-3 py-2 text-xs text-slate-400">
                  Updated {lastUpdated.toLocaleTimeString()}
                </span>
              )}
              <button onClick={refresh} disabled={loading}
                className="flex items-center gap-2 rounded-md border border-violet-500/40 px-4 py-2 text-sm font-bold text-violet-400 transition hover:bg-violet-500/10 disabled:opacity-50">
                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`}/>
                {loading ? 'Refreshing' : 'Refresh'}
              </button>
              <button onClick={train} disabled={training}
                className="flex items-center gap-2 rounded-md bg-violet-500 px-4 py-2 text-sm font-black text-white transition hover:bg-violet-600 disabled:opacity-50">
                <Brain className="h-4 w-4"/>
                {training ? 'Training…' : 'Retrain Model'}
              </button>
            </div>
          </header>

          {error && (
            <div className="mb-5 rounded-lg border border-rose-500/50 bg-rose-500/10 p-4 text-sm text-rose-200">{error}</div>
          )}

          {/* ── Section 1: Metric Cards ───────────────────────────── */}
          <section className="mb-8">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-sm font-bold uppercase tracking-widest text-slate-400">Performance Metrics</h2>
              {/* Audit badge */}
              <div className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-bold ${
                auditData.consistency_ok
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                  : 'border-rose-500/30 bg-rose-500/10 text-rose-400'
              }`}>
                {auditData.consistency_ok ? <CheckCircle className="h-3.5 w-3.5"/> : <AlertCircle className="h-3.5 w-3.5"/>}
                {auditData.consistency_ok ? 'Metrics verified' : 'Inconsistency detected'}
                <span className="ml-1 opacity-70">({auditData.confidence_level_pct ?? 0}% confidence)</span>
              </div>
            </div>
            <p className="mb-4 text-xs text-slate-500">
              All metrics computed from the same {live.resolved ?? 0} resolved live predictions — mathematically consistent.
            </p>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <MetricCard label="Accuracy" value={fmt(accuracy)} raw={accuracy} icon={Target}
                tooltip="Correct predictions ÷ total resolved. Ground truth verified." />
              <MetricCard label="Macro Precision" value={fmt(precision)} raw={precision} icon={Zap}
                tooltip="Average precision across all 5 classes. Same dataset as Accuracy." />
              <MetricCard label="Macro Recall" value={fmt(recall)} raw={recall} icon={Activity}
                tooltip="Average recall across all 5 classes. Same dataset as Accuracy." />
              <MetricCard label="Weighted F1" value={fmt(wF1)} raw={wF1} icon={Award}
                tooltip="F1 weighted by class support — more reliable than macro when classes are imbalanced." />
            </div>
          </section>

          {/* ── Section 2: Count Cards ────────────────────────────── */}
          <section className="mb-8">
            <h2 className="mb-4 text-sm font-bold uppercase tracking-widest text-slate-400">Prediction Counts</h2>
            <div className="grid gap-4 sm:grid-cols-3">
              <CountCard label="Total Predictions" value={live.total_predictions?.toLocaleString()}
                color="text-white" sub="all decisions stored" />
              <CountCard label="Correct Predictions" value={live.correct?.toLocaleString()}
                color="text-emerald-400" sub={`${fmt(accuracy)} hit rate`} />
              <CountCard label="Incorrect Predictions" value={live.incorrect?.toLocaleString()}
                color="text-rose-400" sub="resolved & wrong" />
            </div>
          </section>

          {/* ── Section 3: Charts ─────────────────────────────────── */}
          <section className="mb-8">
            <h2 className="mb-4 text-sm font-bold uppercase tracking-widest text-slate-400">Visual Analytics</h2>
            <div className="grid gap-5 lg:grid-cols-2">

              {/* Accuracy over time */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Accuracy Over Time</h3>
                <p className="mb-4 text-xs text-slate-500">How the model's hit rate evolved across prediction batches</p>
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={accHistory} margin={{ top:4, right:8, left:-20, bottom:0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false}/>
                    <XAxis dataKey="name" tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <YAxis domain={[0,100]} tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <Tooltip content={<ChartTip/>}/>
                    <Line type="monotone" dataKey="accuracy" name="Accuracy" stroke="#a78bfa"
                      strokeWidth={2} dot={{ r:3, fill:'#a78bfa' }} activeDot={{ r:5 }}/>
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* Per-class Precision / Recall / F1 */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Precision, Recall & F1 by Category</h3>
                <p className="mb-4 text-xs text-slate-500">Training metrics broken down by crash category</p>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={prfTrend} margin={{ top:4, right:8, left:-20, bottom:0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false}/>
                    <XAxis dataKey="name" tick={{ fill:'#64748b', fontSize:9 }} axisLine={false} tickLine={false}/>
                    <YAxis domain={[0,100]} tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <Tooltip content={<ChartTip/>}/>
                    <Legend wrapperStyle={{ fontSize:10, color:'#94a3b8' }}/>
                    <Bar dataKey="Precision" fill="#a78bfa" radius={[3,3,0,0]} maxBarSize={14}/>
                    <Bar dataKey="Recall"    fill="#38bdf8" radius={[3,3,0,0]} maxBarSize={14}/>
                    <Bar dataKey="F1"        fill="#9cff45" radius={[3,3,0,0]} maxBarSize={14}/>
                  </BarChart>
                </ResponsiveContainer>
              </div>

              {/* Correct vs Incorrect pie */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Correct vs Incorrect</h3>
                <p className="mb-4 text-xs text-slate-500">Overall prediction outcome distribution</p>
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie data={pieData} cx="50%" cy="50%" innerRadius={55} outerRadius={90}
                         paddingAngle={3} dataKey="value" nameKey="name">
                      {pieData.map((e, i) => <Cell key={i} fill={e.fill}/>)}
                    </Pie>
                    <Tooltip content={<ChartTip/>}/>
                    <Legend wrapperStyle={{ fontSize:11, color:'#94a3b8' }}/>
                  </PieChart>
                </ResponsiveContainer>
              </div>

              {/* Prediction category bar */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Prediction Category Distribution</h3>
                <p className="mb-4 text-xs text-slate-500">How often each crash category was predicted</p>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={barData} margin={{ top:4, right:8, left:-20, bottom:0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false}/>
                    <XAxis dataKey="name" tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <YAxis tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <Tooltip content={<ChartTip/>}/>
                    <Bar dataKey="count" name="Predictions" radius={[4,4,0,0]} maxBarSize={40}>
                      {barData.map((e, i) => <Cell key={i} fill={e.fill}/>)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </section>

          {/* ── Section 3b: Calibration & Confidence ─────────────── */}
          <section className="mb-8">
            <h2 className="mb-4 text-sm font-bold uppercase tracking-widest text-slate-400">Calibration & Confidence</h2>
            <div className="grid gap-5 lg:grid-cols-2">
              {/* Calibration curve */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Calibration Curve</h3>
                <p className="mb-4 text-xs text-slate-500">
                  Actual accuracy vs expected confidence per bin. Diagonal = perfect calibration.
                </p>
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={calBins} margin={{ top:4, right:8, left:-20, bottom:0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false}/>
                    <XAxis dataKey="name" tick={{ fill:'#64748b', fontSize:9 }} axisLine={false} tickLine={false}/>
                    <YAxis domain={[0,100]} tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                    <Tooltip content={<ChartTip/>}/>
                    <Legend wrapperStyle={{ fontSize:10, color:'#94a3b8' }}/>
                    <Line type="monotone" dataKey="actual"   name="Actual Acc" stroke="#9cff45" strokeWidth={2} dot={{ r:4 }}/>
                    <Line type="monotone" dataKey="expected" name="Expected"   stroke="#64748b" strokeWidth={1} strokeDasharray="4 4" dot={false}/>
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {/* Confidence band distribution */}
              <div className="rounded-xl border border-line bg-panel/80 p-5 backdrop-blur">
                <h3 className="mb-1 font-bold text-white">Confidence Band Distribution</h3>
                <p className="mb-4 text-xs text-slate-500">How many predictions fell into each confidence band</p>
                {confBandData.length > 0 ? (
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart data={confBandData} margin={{ top:4, right:8, left:-20, bottom:0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false}/>
                      <XAxis dataKey="name" tick={{ fill:'#64748b', fontSize:9 }} axisLine={false} tickLine={false}/>
                      <YAxis tick={{ fill:'#64748b', fontSize:10 }} axisLine={false} tickLine={false}/>
                      <Tooltip content={<ChartTip/>}/>
                      <Bar dataKey="count" name="Predictions" radius={[4,4,0,0]} maxBarSize={60}>
                        {confBandData.map((e, i) => <Cell key={i} fill={e.fill}/>)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-32 items-center justify-center text-xs text-slate-500 italic">No data yet</div>
                )}
                {live.confidence_dist && (
                  <div className="mt-3 flex gap-4 border-t border-line pt-3 text-xs text-slate-500">
                    <span>Mean: <span className="text-slate-300">{live.confidence_dist.mean}%</span></span>
                    <span>Min: <span className="text-slate-300">{live.confidence_dist.min}%</span></span>
                    <span>Max: <span className="text-slate-300">{live.confidence_dist.max}%</span></span>
                  </div>
                )}
              </div>
            </div>
          </section>

          {/* ── Section 4: Model Health ───────────────────────────── */}
          <section className="mb-8">
            <h2 className="mb-4 text-sm font-bold uppercase tracking-widest text-slate-400">Model Health</h2>
            <div className="grid gap-5 lg:grid-cols-[auto_1fr]">
              <div className="flex items-center justify-center rounded-xl border border-line bg-panel/80 px-8 py-6 backdrop-blur">
                <HealthGauge score={health.score ?? 0} status={health.status ?? 'Critical'}/>
              </div>
              <div className="rounded-xl border border-line bg-panel/80 p-6 backdrop-blur">
                <h3 className="mb-4 text-lg font-bold text-white">What Do These Numbers Mean?</h3>
                <div className="space-y-3">
                  {[
                    { icon: Target,   color:'text-violet-400', title:'Accuracy',
                      desc:'Measures how often predictions are correct. Higher is better.' },
                    { icon: Zap,      color:'text-cyan-400',   title:'Precision',
                      desc:'Measures how reliable positive predictions are — fewer false alarms.' },
                    { icon: Activity, color:'text-emerald-400', title:'Recall',
                      desc:'Measures how many real opportunities were detected by the model.' },
                    { icon: Award,    color:'text-amber-400',  title:'F1 Score',
                      desc:'A single balanced score combining Precision and Recall.' },
                  ].map(({ icon: Icon, color, title, desc }) => (
                    <div key={title} className="flex items-start gap-3">
                      <Icon className={`h-4 w-4 mt-0.5 shrink-0 ${color}`}/>
                      <div>
                        <span className="text-sm font-bold text-white">{title}</span>
                        <span className="ml-2 text-sm text-slate-400">{desc}</span>
                      </div>
                    </div>
                  ))}
                </div>
                {/* Engine info */}
                <div className="mt-5 flex flex-wrap gap-3 border-t border-line pt-4 text-xs text-slate-500">
                  {metrics?.training?.rf?.engine && (
                    <span>RF engine: <span className="text-slate-300">{metrics.training.rf.engine}</span></span>
                  )}
                  {metrics?.training?.lstm?.engine && (
                    <span>LSTM engine: <span className="text-slate-300">{metrics.training.lstm.engine}</span></span>
                  )}
                  {metrics?.training?.rf?.samples && (
                    <span>Trained on <span className="text-slate-300">{metrics.training.rf.samples?.toLocaleString()}</span> samples</span>
                  )}
                </div>
              </div>
            </div>
          </section>

          {/* ── Section 5: Recommendations ───────────────────────── */}
          <section className="mb-8">
            <h2 className="mb-4 text-sm font-bold uppercase tracking-widest text-slate-400">Recommendations</h2>
            <div className="space-y-3">
              {recs.length === 0
                ? <p className="text-sm text-slate-500 italic">No recommendations — waiting for data.</p>
                : recs.map((r, i) => <RecommendationCard key={i} type={r.type} message={r.message}/>)
              }
            </div>
          </section>

        </div>
      </main>
    </div>
  );
}
