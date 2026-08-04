import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, TrendingUp, Wifi, WifiOff } from 'lucide-react';
import useLiveSync from '../lib/useLiveSync.js';

// API
import {
  fetchAccuracy,
  fetchSkipQuality,
  fetchVhQuality,
  trainModel,
  runBackfill,
  api,
} from '../api/client.js';

// Format
import { formatMultiplier, formatPercent, predictionColor, riskColor, predictionLabel } from '../lib/format.js';

// Live components
import LiveStatusBar  from '../components/LiveStatusBar.jsx';
import LiveTimeline   from '../components/LiveTimeline.jsx';

// Prediction components
import ConfidenceMeter      from '../components/ConfidenceMeter.jsx';
import PerformanceChart     from '../components/PerformanceChart.jsx';
import PredictionLog        from '../components/PredictionLog.jsx';
import MetricCard           from '../components/MetricCard.jsx';

// Risk components
import RiskGauge            from '../components/RiskGauge.jsx';
import StatsGrid            from '../components/StatsGrid.jsx';
import StreakTracker        from '../components/StreakTracker.jsx';
import FactorBreakdown      from '../components/FactorBreakdown.jsx';
import MACrossoverChart     from '../components/MACrossoverChart.jsx';
import CategoryDistribution from '../components/CategoryDistribution.jsx';
import RiskHistoryChart     from '../components/RiskHistoryChart.jsx';
import MultiplierChart      from '../components/MultiplierChart.jsx';
import DistributionChart    from '../components/DistributionChart.jsx';
import Sidebar              from '../components/Sidebar.jsx';
import SkipQualityPanel     from '../components/SkipQualityPanel.jsx';
import CalibrationPanel     from '../components/CalibrationPanel.jsx';
import MomentumPanel        from '../components/MomentumPanel.jsx';
import RiskTierPanel        from '../components/RiskTierPanel.jsx';

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState('overview');

  // ── Single source of truth ────────────────────────────────────────────────
  const {
    connected,
    currentRound,
    prediction,       // always for NEXT round
    timeline,
    stats,
    collectorStatus,
    decisions,
    history,
    riskOverview,
    riskHistory,
    lastUpdated,
    requestSync,
  } = useLiveSync();

  // ── Supplemental state (not in live sync) ─────────────────────────────────
  const [accuracy,    setAccuracy]    = useState(null);
  const [skipQuality, setSkipQuality] = useState(null);
  const [vhQuality,   setVhQuality]   = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [training,    setTraining]    = useState(false);
  const [error,       setError]       = useState('');
  const [modelReady,  setModelReady]  = useState(false);

  // ── Model readiness probe ─────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function waitForReady() {
      while (!cancelled) {
        try {
          const { data } = await api.get('/ready');
          if (data?.ready) {
            if (!cancelled) setModelReady(true);
            return;
          }
        } catch {}
        await new Promise(r => setTimeout(r, 2000));
      }
    }
    waitForReady();
    return () => { cancelled = true; };
  }, []);

  // ── Load supplemental data once on mount ─────────────────────────────────
  useEffect(() => {
    fetchAccuracy().then(setAccuracy).catch(() => {});
    fetchSkipQuality().then(setSkipQuality).catch(() => {});
    fetchVhQuality().then(setVhQuality).catch(() => {});
  }, []);

  // ── Refresh accuracy after each resolved prediction ───────────────────────
  useEffect(() => {
    if (!decisions.length) return;
    const resolved = decisions.filter(d => d.actual_multiplier != null);
    if (resolved.length > 0) {
      fetchAccuracy().then(setAccuracy).catch(() => {});
    }
  }, [decisions]);

  // ── Manual refresh ────────────────────────────────────────────────────────
  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      requestSync();
      const [accData, sqData, vhData] = await Promise.all([
        fetchAccuracy(),
        fetchSkipQuality().catch(() => null),
        fetchVhQuality().catch(() => null),
      ]);
      setAccuracy(accData);
      if (sqData) setSkipQuality(sqData);
      if (vhData) setVhQuality(vhData);
      runBackfill().catch(() => {});
    } catch (err) {
      setError(err.message || 'Refresh failed.');
    } finally {
      setLoading(false);
    }
  }, [requestSync]);

  const train = async () => {
    setTraining(true);
    setError('');
    try { await trainModel(30); await refresh(); }
    catch (err) { setError(err.message || 'Training failed.'); }
    finally { setTraining(false); }
  };

  // ── Derived ───────────────────────────────────────────────────────────────
  const risk       = riskOverview?.risk    || {};
  const summary    = riskOverview?.summary || {};
  const riskLevel  = risk?.risk_level      || 'LOW';
  const riskScore  = risk?.risk_score      || 0;
  const streakData = risk?.streaks;
  const probabilityRows = Object.entries(prediction?.probabilities || {});

  // ── Tab renderers ─────────────────────────────────────────────────────────

  const renderOverview = () => (
    <>
      {/* Live status bar — always visible on overview */}
      <LiveStatusBar
        currentRound={currentRound}
        prediction={prediction}
        stats={stats}
        collectorStatus={collectorStatus}
        connected={connected}
      />

      <div className="grid gap-5 lg:grid-cols-[1fr_1.4fr]">
        <LiveTimeline timeline={timeline} currentRoundId={currentRound?.round_id} />
        <div className="space-y-5">
          <StatsGrid summary={summary} risk={risk} />
          <div className="rounded-xl border border-line bg-panel/80 p-5 shadow-lg backdrop-blur">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="text-lg font-bold text-white">Risk Index</h2>
                <p className="text-xs text-slate-500">Composite risk assessment</p>
              </div>
              <div className={`rounded-full px-3 py-1 text-xs font-bold ${
                riskLevel === 'HIGH'   ? 'bg-danger/10 text-danger' :
                riskLevel === 'MEDIUM' ? 'bg-amber-300/10 text-amber-300' : 'bg-cyan/10 text-cyan'
              }`}>{riskLevel}</div>
            </div>
            <RiskGauge score={riskScore} level={riskLevel} size={200} />
          </div>
        </div>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <PerformanceChart rounds={history} />
        <RiskHistoryChart rounds={riskHistory} />
      </div>
    </>
  );

  const renderPrediction = () => (
    <>
      {/* Next round prediction — always for round N+1 */}
      <div className="mb-4 rounded-xl border border-cyan/30 bg-cyan/5 px-5 py-4">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="text-xs font-semibold uppercase tracking-widest text-cyan">
            Next Round Prediction
          </div>
          {prediction?.for_round && (
            <div className="rounded-full bg-cyan/10 border border-cyan/30 px-3 py-0.5 text-xs font-bold text-cyan">
              Round #{prediction.for_round}
            </div>
          )}
          {currentRound?.round_id && (
            <div className="text-xs text-slate-500">
              Current: Round #{currentRound.round_id}
            </div>
          )}
        </div>
      </div>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label={`Prediction (Round #${prediction?.for_round ?? '?'})`} value={predictionLabel(prediction?.prediction)}>
          <div className={`mt-1 h-2 rounded-full bg-gradient-to-r ${predictionColor(prediction?.prediction)}`} />
        </MetricCard>
        <MetricCard label="Confidence" value={formatPercent(prediction?.confidence)} accent="text-cyan">
          <ConfidenceMeter value={prediction?.confidence || 0} />
        </MetricCard>
        <MetricCard label="Recommended Cashout" value={formatMultiplier(prediction?.recommended_cashout)} accent="text-acid">
          <div className="mt-1 flex items-center justify-between text-xs">
            <span className="text-slate-400">Cash out before crash</span>
            <span className={`font-bold ${riskColor(prediction?.risk_level)}`}>
              {prediction?.risk_level ?? '—'} RISK
            </span>
          </div>
        </MetricCard>
        <MetricCard label="Model Accuracy" value={formatPercent(accuracy?.validation_accuracy)} accent="text-amber-300">
          <div className="mt-1 flex items-center justify-between text-xs text-slate-400">
            <span>Engine: {accuracy?.model?.engine ?? 'fallback'}</span>
            <span>{accuracy?.model?.samples ?? 0} samples</span>
          </div>
          {accuracy?.hit_rate_pct != null && (
            <div className="mt-2 flex items-center justify-between text-xs">
              <span className="text-slate-400">Hit rate ({accuracy.resolved_count} resolved)</span>
              <span className="font-bold text-acid">{formatPercent(accuracy.hit_rate_pct)}</span>
            </div>
          )}
        </MetricCard>
      </section>

      {/* Override notices */}
      {prediction?.skip_override && (
        <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/8 px-4 py-3 text-xs text-amber-400">
          ⚡ Skip override: {prediction.skip_override}
        </div>
      )}
      {prediction?.vh_downgrade_reason && (
        <div className="mt-2 rounded-lg border border-orange-500/30 bg-orange-500/8 px-4 py-3 text-xs text-orange-400">
          ↓ VH downgrade: {prediction.vh_downgrade_reason}
        </div>
      )}
      {prediction?.recalibration_active && (
        <div className="mt-2 rounded-lg border border-amber-400/40 bg-amber-400/10 px-4 py-3 text-xs text-amber-300">
          🔄 {prediction.recalibration_reason}
        </div>
      )}

      <section className="mt-5 grid gap-5 lg:grid-cols-2">
        <div className="rounded-xl border border-line bg-panel/80 p-5 shadow-lg backdrop-blur">
          <h2 className="text-lg font-bold text-white">Cashout Strategy</h2>
          <p className="mb-4 text-xs text-slate-500">Conservative targets per prediction category</p>
          <div className="space-y-3">
            {[
              { cat: 'VERY_LOW',  range: '1.0–1.5×', target: '1.10–1.30×', color: 'bg-sky-500' },
              { cat: 'LOW',       range: '1.5–2.0×', target: '1.30–1.70×', color: 'bg-cyan'     },
              { cat: 'MEDIUM',    range: '2.0–5.0×', target: '1.70–2.80×', color: 'bg-acid'     },
              { cat: 'HIGH',      range: '5.0–15×',  target: '2.80–5.50×', color: 'bg-orange-400'},
              { cat: 'VERY_HIGH', range: '15×+',     target: '5.50–12×',   color: 'bg-danger'   },
            ].map(({ cat, range, target, color }) => {
              const isActive = prediction?.prediction === cat;
              return (
                <div key={cat} className={`flex items-center gap-3 rounded-lg px-3 py-2.5 transition ${isActive ? 'bg-slate-700/60 ring-1 ring-white/10' : 'opacity-60'}`}>
                  <div className={`h-2.5 w-2.5 rounded-full ${color} shrink-0`} />
                  <span className="w-20 text-xs font-bold text-white">{cat}</span>
                  <span className="flex-1 text-xs text-slate-400">Crash: {range}</span>
                  <span className={`text-xs font-black ${isActive ? 'text-acid' : 'text-slate-300'}`}>
                    {isActive ? '→ ' : ''}{target}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
        <div className="rounded-xl border border-line bg-panel/80 p-5 shadow-lg backdrop-blur">
          <h2 className="text-lg font-bold text-white">Probability Split</h2>
          <p className="mb-4 text-xs text-slate-500">
            Bayesian-blended probabilities
            {prediction?.bayes_weight != null && (
              <span className="ml-1 text-violet-400">({Math.round(prediction.bayes_weight * 100)}% Bayes weight)</span>
            )}
          </p>
          <div className="space-y-3">
            {probabilityRows.map(([label, value]) => {
              const barColor =
                label === 'VERY_LOW' ? 'from-sky-500 to-sky-400' :
                label === 'LOW'      ? 'from-cyan to-sky-300' :
                label === 'MEDIUM'   ? 'from-acid to-emerald-300' :
                label === 'HIGH'     ? 'from-orange-400 to-amber-300' : 'from-danger to-orange-300';
              return (
                <div key={label}>
                  <div className="mb-1.5 flex justify-between text-xs">
                    <span className="font-semibold text-white">{label}</span>
                    <span className="text-slate-300">{formatPercent(value)}</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-slate-800">
                    <div className={`h-full rounded-full bg-gradient-to-r ${barColor} transition-all duration-500`} style={{ width: `${value}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section className="mt-5 grid gap-5 lg:grid-cols-2">
        <PerformanceChart rounds={history} />
        <PredictionLog decisions={decisions} />
      </section>
    </>
  );

  const renderDistribution = () => (
    <div className="grid gap-5 lg:grid-cols-2">
      <CategoryDistribution counts={summary?.category_counts} />
      <DistributionChart probabilities={prediction?.probabilities} />
    </div>
  );

  const renderMultipliers = () => (
    <div className="grid gap-5 lg:grid-cols-2">
      <MultiplierChart rounds={history} />
      <MACrossoverChart rounds={history} />
    </div>
  );

  const renderLogs = () => (
    <>
      <div className="grid gap-5 lg:grid-cols-2">
        <StreakTracker streaks={streakData} />
        <RiskHistoryChart rounds={riskHistory} />
      </div>
      <div className="mt-5">
        <PredictionLog decisions={decisions} />
      </div>
    </>
  );

  const renderIntelligence = () => (
    <div className="space-y-5">
      {prediction && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[
            { label: 'Action',         value: prediction.action ?? '—',
              color: prediction.action === 'BET' ? 'text-lime-400' : 'text-amber-400',
              bg: prediction.action === 'BET' ? 'bg-lime-500/10 border-lime-500/20' : 'bg-amber-500/10 border-amber-500/20' },
            { label: 'Effective Trend',value: (prediction.streak?.trend || prediction.trend || '—').toUpperCase(),
              color: 'text-violet-400', bg: 'bg-violet-500/10 border-violet-500/20' },
            { label: 'Momentum Score', value: prediction.streak?.momentum_score != null
                ? `${Math.round(prediction.streak.momentum_score * 100)}` : '—',
              color: 'text-cyan-400', bg: 'bg-cyan-500/10 border-cyan-500/20' },
            { label: 'Conf Correction',value: prediction.conf_correction_factor != null
                ? `×${Number(prediction.conf_correction_factor).toFixed(3)}` : '—',
              color: prediction.conf_inverted ? 'text-rose-400' : 'text-slate-300',
              bg: prediction.conf_inverted ? 'bg-rose-500/10 border-rose-500/20' : 'bg-slate-800/40 border-line' },
          ].map(({ label, value, color, bg }) => (
            <div key={label} className={`rounded-xl border px-4 py-3 text-center ${bg}`}>
              <div className={`text-xl font-black ${color}`}>{value}</div>
              <div className="text-[10px] uppercase tracking-wide text-slate-500 mt-0.5">{label}</div>
            </div>
          ))}
        </div>
      )}
      <SkipQualityPanel skipQuality={skipQuality} vhQuality={vhQuality} />
      <RiskTierPanel prediction={prediction} />
      <MomentumPanel prediction={prediction} />
      <CalibrationPanel />
    </div>
  );

  const tabContent = {
    overview:     renderOverview,
    performance:  renderPrediction,
    distribution: renderDistribution,
    multipliers:  renderMultipliers,
    logs:         renderLogs,
    intelligence: renderIntelligence,
  };

  const tabTitles = {
    overview:     'Risk Overview',
    performance:  'Prediction Console',
    distribution: 'Crash Distribution',
    multipliers:  'Multiplier Analysis',
    logs:         'History & Logs',
    intelligence: 'Intelligence Engine',
  };

  return (
    <div className="flex min-h-screen bg-ink text-white">
      <Sidebar activeTab={activeTab} onTabChange={setActiveTab} onRefresh={refresh} loading={loading} onTrain={train} training={training} />

      <div className="fixed bottom-0 left-0 right-0 z-50 flex border-t border-line bg-panel/95 backdrop-blur lg:hidden">
        {['overview', 'performance', 'logs', 'intelligence'].map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)}
            className={`flex-1 py-3 text-center text-[10px] font-bold uppercase tracking-[0.1em] transition ${
              activeTab === tab
                ? tab === 'intelligence' ? 'text-violet-400 border-t-2 border-violet-400' : 'text-cyan border-t-2 border-cyan'
                : 'text-slate-500'
            }`}>{tab}</button>
        ))}
      </div>

      <main className="flex-1 overflow-auto pb-20 lg:pb-0">
        <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
          <header className="mb-6 flex flex-col gap-4 border-b border-line pb-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.22em]">
                {riskLevel === 'HIGH'
                  ? <AlertTriangle className="h-3.5 w-3.5 text-danger" />
                  : <TrendingUp className={`h-3.5 w-3.5 ${riskLevel === 'MEDIUM' ? 'text-amber-300' : 'text-cyan'}`} />}
                <span className={riskLevel === 'HIGH' ? 'text-danger' : riskLevel === 'MEDIUM' ? 'text-amber-300' : 'text-cyan'}>
                  Aviator Risk Management
                </span>
              </div>
              <h1 className="mt-2 text-3xl font-black sm:text-4xl">
                {tabTitles[activeTab] ?? 'Dashboard'}
              </h1>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <span className={`flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-semibold ${
                connected ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                          : 'border-amber-500/30 bg-amber-500/10 text-amber-400'
              }`}>
                {connected ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
                {connected ? 'Live' : 'Polling'}
              </span>
              <span className="rounded-md border border-line px-3 py-2 text-sm text-slate-300">
                {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Waiting for data'}
              </span>
              <button onClick={refresh} disabled={loading}
                className="rounded-md border border-cyan px-4 py-2 text-sm font-bold text-cyan transition hover:bg-cyan hover:text-ink disabled:opacity-50">
                {loading ? 'Refreshing' : 'Refresh'}
              </button>
              <button onClick={train} disabled={training}
                className="rounded-md bg-acid px-4 py-2 text-sm font-black text-ink transition hover:brightness-110 disabled:opacity-50">
                {training ? 'Training' : 'Train Model'}
              </button>
            </div>
          </header>

          {!modelReady && (
            <div className="mb-5 flex items-center gap-3 rounded-lg border border-cyan/30 bg-cyan/5 p-4 text-sm text-cyan">
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-cyan border-t-transparent" />
              <span>Loading TensorFlow model — first load takes ~60s. Predictions will appear automatically when ready.</span>
            </div>
          )}
          {error && (
            <div className="mb-5 rounded-lg border border-danger/60 bg-danger/10 p-4 text-sm text-rose-100">{error}</div>
          )}

          {(tabContent[activeTab] || tabContent.overview)()}
        </div>
      </main>
    </div>
  );
}
