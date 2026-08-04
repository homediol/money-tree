/**
 * LiveTimeline.jsx — Real-time round timeline.
 *
 * Shows last N rounds with:
 *   - Round number
 *   - Actual multiplier
 *   - Prediction (if any)
 *   - Result: WIN | LOSS | WAITING | (none)
 */

const CAT_COLOR = {
  VERY_LOW:  '#38bdf8',
  LOW:       '#35d4ff',
  MEDIUM:    '#9cff45',
  HIGH:      '#f97316',
  VERY_HIGH: '#ff5277',
  UNKNOWN:   '#64748b',
};

const RESULT_STYLE = {
  WIN:     { bg: 'bg-lime-500/15 border-lime-500/30',   text: 'text-lime-400',   label: 'WIN'  },
  LOSS:    { bg: 'bg-rose-500/15 border-rose-500/30',   text: 'text-rose-400',   label: 'LOSS' },
  WAITING: { bg: 'bg-amber-500/10 border-amber-500/30', text: 'text-amber-400',  label: '⏳'   },
};

function multColor(v) {
  if (!v) return '#64748b';
  if (v >= 15) return '#ff5277';
  if (v >= 5)  return '#f97316';
  if (v >= 2)  return '#9cff45';
  return '#35d4ff';
}

function fmtTime(ts) {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return ''; }
}

export default function LiveTimeline({ timeline = [], currentRoundId }) {
  // Show last 20, newest first
  const entries = [...timeline].reverse().slice(0, 20);

  if (!entries.length) {
    return (
      <div className="rounded-xl border border-line bg-panel/80 p-5">
        <h2 className="text-lg font-bold text-white mb-1">Live Timeline</h2>
        <p className="text-xs text-slate-500">Waiting for rounds…</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-line bg-panel/80 p-5 shadow-lg backdrop-blur">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-white">Live Timeline</h2>
          <p className="text-xs text-slate-500">Real-time round history with predictions</p>
        </div>
        {currentRoundId && (
          <div className="rounded-full bg-cyan/10 border border-cyan/30 px-3 py-1 text-xs font-bold text-cyan">
            Round #{currentRoundId}
          </div>
        )}
      </div>

      <div className="space-y-1.5 max-h-[480px] overflow-y-auto pr-1">
        {entries.map((entry, i) => {
          const isLatest  = i === 0;
          const isWaiting = entry.result === 'WAITING';
          const rs        = RESULT_STYLE[entry.result] ?? null;
          const catColor  = CAT_COLOR[entry.category] ?? CAT_COLOR.UNKNOWN;

          return (
            <div
              key={entry.round_id}
              className={`flex items-center gap-3 rounded-lg border px-3 py-2 transition-all ${
                isLatest
                  ? 'border-cyan/30 bg-cyan/5 ring-1 ring-cyan/10'
                  : isWaiting
                  ? 'border-amber-500/20 bg-amber-500/5'
                  : 'border-line/50 bg-ink/40'
              }`}
            >
              {/* Round number */}
              <div className="w-16 shrink-0">
                <div className={`text-xs font-bold ${isLatest ? 'text-cyan' : 'text-slate-400'}`}>
                  #{entry.round_id}
                </div>
                <div className="text-[10px] text-slate-600">{fmtTime(entry.timestamp)}</div>
              </div>

              {/* Multiplier */}
              <div className="w-16 shrink-0 text-center">
                {entry.multiplier != null && entry.multiplier > 0 ? (
                  <span className="text-sm font-black" style={{ color: multColor(entry.multiplier) }}>
                    {Number(entry.multiplier).toFixed(2)}×
                  </span>
                ) : (
                  <span className="text-xs text-slate-600">—</span>
                )}
              </div>

              {/* Category dot */}
              <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: catColor }} />

              {/* Prediction */}
              <div className="flex-1 min-w-0">
                {entry.prediction ? (
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-slate-400">Pred:</span>
                    <span className="text-xs font-bold text-white">{entry.prediction}</span>
                    {entry.confidence != null && (
                      <span className="text-[10px] text-slate-500">{Math.round(entry.confidence)}%</span>
                    )}
                  </div>
                ) : (
                  <span className="text-xs text-slate-600">No prediction</span>
                )}
              </div>

              {/* Result badge */}
              <div className="shrink-0">
                {rs ? (
                  <span className={`rounded-full border px-2.5 py-0.5 text-[10px] font-black ${rs.bg} ${rs.text}`}>
                    {rs.label}
                  </span>
                ) : (
                  <span className="text-[10px] text-slate-700">—</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
