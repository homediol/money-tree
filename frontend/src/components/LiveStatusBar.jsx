/**
 * LiveStatusBar.jsx — Real-time status cards.
 * Shows: current round, next prediction, collector status, win/loss stats.
 */

function StatusDot({ ok, pulse = false }) {
  return (
    <span className={`inline-block h-2 w-2 rounded-full shrink-0 ${
      ok
        ? pulse ? 'bg-lime-400 animate-pulse' : 'bg-lime-400'
        : 'bg-rose-500'
    }`} />
  );
}

function Card({ label, value, sub, accent = 'text-white', border = 'border-line', children }) {
  return (
    <div className={`rounded-xl border ${border} bg-panel/80 px-4 py-3 backdrop-blur`}>
      <div className="text-[10px] uppercase tracking-[0.15em] text-slate-500 mb-1">{label}</div>
      <div className={`text-xl font-black ${accent} leading-none`}>{value ?? '—'}</div>
      {sub && <div className="text-[10px] text-slate-500 mt-1">{sub}</div>}
      {children}
    </div>
  );
}

const CAT_ACCENT = {
  VERY_LOW:  'text-sky-400',
  LOW:       'text-cyan-400',
  MEDIUM:    'text-lime-400',
  HIGH:      'text-orange-400',
  VERY_HIGH: 'text-rose-400',
};

function timeSince(ts) {
  if (!ts) return null;
  const secs = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (secs < 60)  return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

export default function LiveStatusBar({ currentRound, prediction, stats, collectorStatus, connected }) {
  const predCat    = prediction?.prediction;
  const predConf   = prediction?.confidence;
  const forRound   = prediction?.for_round;
  const accuracy   = stats?.accuracy_pct;
  const wins       = stats?.wins ?? 0;
  const losses     = stats?.losses ?? 0;
  const streak     = stats?.current_streak ?? 0;
  const streakType = stats?.streak_type;
  const lastRoundTime = stats?.last_round_time;

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 mb-5">

      {/* Current Round */}
      <Card
        label="Current Round"
        value={currentRound?.round_id != null ? `#${currentRound.round_id}` : '—'}
        accent="text-cyan"
        border="border-cyan/20"
        sub={timeSince(lastRoundTime) ?? 'Waiting…'}
      >
        {currentRound?.multiplier != null && (
          <div className="mt-1 text-xs font-bold" style={{
            color: currentRound.multiplier >= 15 ? '#ff5277'
                 : currentRound.multiplier >= 5  ? '#f97316'
                 : currentRound.multiplier >= 2  ? '#9cff45' : '#35d4ff'
          }}>
            {Number(currentRound.multiplier).toFixed(2)}×
          </div>
        )}
      </Card>

      {/* Next Prediction */}
      <Card
        label={forRound ? `Next Prediction (Round #${forRound})` : 'Next Prediction'}
        value={predCat ?? 'Predicting…'}
        accent={CAT_ACCENT[predCat] ?? 'text-slate-400'}
        border={predCat ? 'border-cyan/20' : 'border-line'}
      >
        {predConf != null && (
          <div className="mt-1.5">
            <div className="flex items-center justify-between text-[10px] mb-0.5">
              <span className="text-slate-500">Confidence</span>
              <span className="text-white font-bold">{Math.round(predConf)}%</span>
            </div>
            <div className="h-1 rounded-full bg-slate-800 overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-cyan to-lime-400 transition-all duration-500"
                style={{ width: `${Math.min(predConf, 100)}%` }}
              />
            </div>
          </div>
        )}
      </Card>

      {/* Win / Loss */}
      <Card
        label="Win / Loss"
        value={`${wins}W / ${losses}L`}
        accent={wins >= losses ? 'text-lime-400' : 'text-rose-400'}
        sub={accuracy != null ? `${accuracy}% accuracy` : 'No resolved predictions'}
      >
        {streak > 0 && streakType && (
          <div className={`mt-1 text-[10px] font-bold ${streakType === 'WIN' ? 'text-lime-400' : 'text-rose-400'}`}>
            {streakType} streak ×{streak}
          </div>
        )}
      </Card>

      {/* Collector Status */}
      <Card
        label="Collector"
        value={collectorStatus?.running ? 'Running' : 'Offline'}
        accent={collectorStatus?.running ? 'text-lime-400' : 'text-rose-400'}
        border={collectorStatus?.running ? 'border-lime-500/20' : 'border-rose-500/20'}
      >
        <div className="mt-2 space-y-1">
          {[
            { label: 'Browser', ok: collectorStatus?.browser },
            { label: 'Frame',   ok: collectorStatus?.frame   },
            { label: 'Login',   ok: collectorStatus?.logged_in },
          ].map(({ label, ok }) => (
            <div key={label} className="flex items-center gap-1.5 text-[10px]">
              <StatusDot ok={ok} pulse={ok} />
              <span className={ok ? 'text-slate-300' : 'text-slate-600'}>{label}</span>
            </div>
          ))}
        </div>
      </Card>

      {/* Socket Status */}
      <Card
        label="Socket"
        value={connected ? 'Live' : 'Polling'}
        accent={connected ? 'text-lime-400' : 'text-amber-400'}
        border={connected ? 'border-lime-500/20' : 'border-amber-500/20'}
      >
        <div className="mt-2 flex items-center gap-1.5 text-[10px]">
          <StatusDot ok={connected} pulse={connected} />
          <span className="text-slate-400">{connected ? 'WebSocket connected' : 'Fallback polling'}</span>
        </div>
        {stats?.prediction_latency_ms != null && (
          <div className="mt-1 text-[10px] text-slate-500">
            Pred latency: <span className="text-slate-300">{stats.prediction_latency_ms}ms</span>
          </div>
        )}
      </Card>

    </div>
  );
}
