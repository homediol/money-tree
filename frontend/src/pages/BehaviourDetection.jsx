import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { behaviourApiPrefix, behaviourDelete, behaviourGet, behaviourPut } from '../services/behaviourApi.js';
import { api } from '../services/api.js';
import { subscribeLiveEvents } from '../services/liveEvents.js';
import {
  enableNotificationAudio, enableNotificationVoice, getNotificationHistory, getNotificationSettings, listVoices, NOTIFICATION_EVENT_DEFINITIONS,
  getNotificationAudioStatus, playConfiguredSound, previewSound, publishNotification, recordAudibilityResult,
  saveNotificationSettings, SOUND_PROFILES, stopSound, stopVoice, subscribeNotifications,
  speakText, soundVerificationKey, voiceVerificationKey,
} from '../services/notifications.js';

const links = [
  ['overview', 'Overview'], ['live', 'Live rounds'], ['state', 'Behaviour state'],
  ['next100', 'Next 100'], ['sequences', 'Sequence monitor'], ['outlook', 'Sequence outlook'],
  ['time', 'Time behaviour'], ['groups', 'Historical groups'], ['distribution', 'H/L distribution'],
  ['motifs', 'Motif explorer'], ['rules', 'Rules and evidence'], ['evaluation', 'Forecast history'],
  ['health', 'Collector and health'],
];

const panel = 'rounded-xl border border-zinc-800 bg-zinc-900/70 p-4';
const STRATEGY_CONFIGURATION_DEFAULTS = {
  strategy_mode: 'PART1_AND_PART2',
  part1: { enabled: true, max_counted_rounds: 5, net_win_limit_enabled: true, net_win_target: 2 },
  part2: { enabled: true, max_counted_rounds_per_batch: 5, net_win_limit_enabled: true,
    net_win_target: 3, pause_limit_verified_rounds: 10 },
  on_win_limit_reached: 'WAIT_FOR_NEXT_FRESH_TRIGGER', cooldown_verified_rounds: 10,
  pause_timeout_seconds: null, shadow_only: true, configuration_version: 1,
};
function normalizeStrategyConfiguration(value = {}) {
  return { ...STRATEGY_CONFIGURATION_DEFAULTS, ...value,
    part1: { ...STRATEGY_CONFIGURATION_DEFAULTS.part1, ...(value.part1 || {}) },
    part2: { ...STRATEGY_CONFIGURATION_DEFAULTS.part2, ...(value.part2 || {}) } };
}
function validateStrategyDraft(draft) {
  const errors = [];
  if (draft.strategy_mode !== 'HIGH_DOMINANCE_ONLY' && !draft.part1.enabled)
    errors.push('Part 1 must be enabled for Part 1 modes.');
  for (const [part, maximumKey, label] of [
    ['part1', 'max_counted_rounds', 'Part 1'],
    ['part2', 'max_counted_rounds_per_batch', 'Part 2'],
  ]) {
    const maximum = Number(draft[part][maximumKey]);
    const target = Number(draft[part].net_win_target);
    if (!Number.isInteger(maximum) || maximum < 1 || maximum > 1000)
      errors.push(`${label} maximum counted rounds must be between 1 and 1000.`);
    if (!Number.isInteger(target) || target < 1 || target > maximum)
      errors.push(`${label} NET WIN target must be a positive integer no greater than its maximum rounds.`);
  }
  if (!Number.isInteger(Number(draft.part2.pause_limit_verified_rounds))
      || Number(draft.part2.pause_limit_verified_rounds) < 1
      || Number(draft.part2.pause_limit_verified_rounds) > 10000)
    errors.push('Part 2 pause limit must be between 1 and 10000 verified rounds.');
  if (!Number.isInteger(Number(draft.cooldown_verified_rounds))
      || Number(draft.cooldown_verified_rounds) < 1
      || Number(draft.cooldown_verified_rounds) > 10000)
    errors.push('Cooldown must be between 1 and 10000 verified rounds.');
  if (draft.pause_timeout_seconds !== null && draft.pause_timeout_seconds !== ''
      && (!Number.isInteger(Number(draft.pause_timeout_seconds))
        || Number(draft.pause_timeout_seconds) < 1 || Number(draft.pause_timeout_seconds) > 31536000))
    errors.push('Wall-clock pause timeout must be a positive integer or disabled.');
  return errors;
}
const number = (value, digits = 2) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
const percent = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '—';
const timestamp = value => value ? new Date(value).toLocaleString() : 'Unknown';
const shortTime = value => value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
const elapsedMs = (later, earlier) => {
  const end = Date.parse(later || '');
  const start = Date.parse(earlier || '');
  return Number.isFinite(end) && Number.isFinite(start) ? end - start : null;
};
const lagLabel = value => value == null ? 'UNKNOWN' : value < -500 ? 'CLOCK SKEW' : `${Math.max(0, Math.round(value))} ms`;

function useBehaviourData(path, refreshMs = 0, enabled = true) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState(null);
  const inFlight = useRef(null);
  const queuedRefresh = useRef(null);
  const [liveConnection, setLiveConnection] = useState('connecting');
  const [latestVerifiedEvent, setLatestVerifiedEvent] = useState(null);
  const [frontendReceivedAt, setFrontendReceivedAt] = useState(null);
  const [frontendRenderedAt, setFrontendRenderedAt] = useState(null);
  const refresh = useCallback(async (isCurrent = () => true) => {
    if (inFlight.current) {
      // Coalesce timer, focus, and event-triggered refreshes behind the
      // current request. Slow analysis responses must not fan out into a
      // queue of identical database reads.
      queuedRefresh.current = { path, isCurrent, run: () => refresh(isCurrent) };
      return false;
    }
    const pending = behaviourGet(path)
      .then(next => {
        // StrictMode can clean up the first effect while its request is still
        // running. Its replacement is already subscribed to the same path, so
        // publish the response instead of leaving the page loading until a
        // second slow analysis request finishes.
        if (isCurrent() || (queuedRefresh.current?.path === path && queuedRefresh.current.isCurrent())) {
          setData(next); setError(''); setUpdatedAt(new Date().toISOString());
        }
        return true;
      })
      .catch(reason => {
        if (isCurrent() || (queuedRefresh.current?.path === path && queuedRefresh.current.isCurrent()))
          setError(reason?.message || 'Behaviour Detection backend is unavailable');
        return false;
      })
      .finally(() => {
        if (inFlight.current === pending) inFlight.current = null;
        const queued = queuedRefresh.current;
        queuedRefresh.current = null;
        if (queued?.isCurrent()) {
          queueMicrotask(() => { if (queued.isCurrent()) void queued.run(); });
        }
      });
    inFlight.current = pending;
    return pending;
  }, [path]);
  useEffect(() => {
    let alive = true;
    if (!enabled) return () => { alive = false; };
    const run = () => { if (alive) refresh(() => alive); };
    run();
    if (!refreshMs) return () => { alive = false; };
    const timer = window.setInterval(run, refreshMs);
    const onFocus = () => { if (document.visibilityState === 'visible') run(); };
    document.addEventListener('visibilitychange', onFocus);
    return () => { alive = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', onFocus); };
  }, [enabled, refresh, refreshMs]);
  useEffect(() => {
    if (!enabled || !['/overview', '/hybrid'].includes(path)) return undefined;
    let alive = true;
    const timers = new Set();
    const unsubscribe = subscribeLiveEvents(frame => {
      if (frame?.__transport) {
        setLiveConnection(frame.status);
        if (frame.status === 'restored') void refresh(() => alive);
        return;
      }
      if (frame?.type !== 'history:verified-rounds') return;
      const latest = frame.rounds?.at(-1);
      if (!latest || latest.event_type !== 'round.persisted' || latest.durability !== 'COMMITTED') return;
      setLatestVerifiedEvent(latest);
      setFrontendReceivedAt(new Date().toISOString());
      // The independent analysis transaction may finish just after the
      // committed Winner event. Retry briefly, then retain normal polling.
      for (const delay of [500, 1500]) {
        const timer = window.setTimeout(() => {
          timers.delete(timer);
          if (!alive) return;
          if (inFlight.current) queuedRefresh.current = { path, isCurrent: () => alive, run: () => refresh(() => alive) };
          else void refresh(() => alive);
        }, delay);
        timers.add(timer);
      }
    });
    return () => { alive = false; unsubscribe(); timers.forEach(window.clearTimeout); };
  }, [enabled, path, refresh]);
  useEffect(() => {
    if (!data) return undefined;
    const frame = window.requestAnimationFrame(() => setFrontendRenderedAt(new Date().toISOString()));
    return () => window.cancelAnimationFrame(frame);
  }, [data]);
  return { data, error, updatedAt, refresh, liveConnection, latestVerifiedEvent, frontendReceivedAt, frontendRenderedAt };
}

function ShellPage({ title, subtitle, path, refreshMs = 0, children, render }) {
  const { data, error, updatedAt, refresh, liveConnection, latestVerifiedEvent, frontendReceivedAt } = useBehaviourData(path, refreshMs);
  return <div className="space-y-5">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-300">Behaviour Detection</p>
        <h1 className="mt-1 text-2xl font-bold text-zinc-100">{title}</h1>
        <p className="mt-1 text-sm text-zinc-400">{subtitle}</p></div>
      <div className="flex items-center gap-3 text-xs text-zinc-500">
        <span>Checked {timestamp(updatedAt)}</span>
        <button onClick={() => refresh()} className="rounded border border-zinc-700 px-3 py-1.5 text-zinc-200 hover:bg-zinc-800">Refresh</button>
      </div>
    </div>
    {error && <div role="alert" className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-4 text-sm text-amber-200">
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="font-semibold">Behaviour data request failed</div>
        <button type="button" onClick={() => refresh()} className="rounded border border-amber-700/70 px-3 py-1.5 text-xs font-semibold hover:bg-amber-900/40">Retry this page</button></div>
      <div className="mt-1">GET {behaviourApiPrefix}{path} · {error}</div>
    </div>}
    {data ? render?.(data, { liveConnection, latestVerifiedEvent, frontendReceivedAt, updatedAt }) : !error ? <div className={panel}>Loading independent Behaviour Detection data…</div> : null}
    {children}
  </div>;
}

function Metric({ label, value, detail, valueClass = 'text-zinc-100' }) {
  return <div className={`${panel} relative overflow-hidden`}><div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-emerald-400/50 via-sky-400/20 to-transparent" />
    <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</p>
    <p className={`mt-2 break-words text-2xl font-semibold tabular-nums ${valueClass}`}>{value}</p>{detail && <p className="mt-1 text-xs text-zinc-500">{detail}</p>}</div>;
}

function HighMetric({ label, value, detail, tone = 'emerald', valueClass = '', compact = false }) {
  const tones = {
    emerald: { card: 'border-emerald-500/25 from-emerald-950/60 via-zinc-900/90 to-zinc-950/90', dot: 'bg-emerald-300', value: 'text-emerald-100' },
    rose: { card: 'border-rose-500/25 from-rose-950/50 via-zinc-900/90 to-zinc-950/90', dot: 'bg-rose-300', value: 'text-rose-100' },
    sky: { card: 'border-sky-500/25 from-sky-950/50 via-zinc-900/90 to-zinc-950/90', dot: 'bg-sky-300', value: 'text-sky-100' },
    violet: { card: 'border-violet-500/25 from-violet-950/50 via-zinc-900/90 to-zinc-950/90', dot: 'bg-violet-300', value: 'text-violet-100' },
    amber: { card: 'border-amber-500/25 from-amber-950/50 via-zinc-900/90 to-zinc-950/90', dot: 'bg-amber-300', value: 'text-amber-100' },
  };
  const style = tones[tone] || tones.emerald;
  return <div className={`group relative min-w-0 ${compact ? 'min-h-[82px] rounded-lg p-2.5' : 'min-h-[112px] rounded-xl p-3.5'} overflow-hidden border bg-gradient-to-br shadow-lg shadow-black/10 transition duration-200 hover:-translate-y-0.5 hover:shadow-black/25 ${style.card}`}>
    <div className={`absolute inset-x-3 top-0 h-px ${style.dot} opacity-70`} />
    <div className="flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} /><p className="text-[10px] font-bold uppercase tracking-[0.14em] text-zinc-400">{label}</p></div>
    <p className={`${compact ? 'mt-1.5 text-lg' : 'mt-2 text-2xl'} break-words font-semibold tabular-nums ${valueClass || style.value}`}>{value}</p>
    {detail && <p className={`mt-1 ${compact ? 'line-clamp-2 text-[10px] leading-3.5' : 'text-[11px] leading-4'} text-zinc-400`}>{detail}</p>}
  </div>;
}

function StatusBadge({ value, className = '' }) {
  const label = String(value || 'UNKNOWN');
  const normalized = label.toLowerCase();
  const outcomeTone = /^(h|high)$/.test(normalized) ? 'border-emerald-500/30 bg-emerald-400/10 text-emerald-300'
    : /^(l|low)$/.test(normalized) ? 'border-rose-500/30 bg-rose-400/10 text-rose-300'
      : /^(e|exact)$/.test(normalized) ? 'border-amber-500/30 bg-amber-400/10 text-amber-200'
        : /high_dominant/.test(normalized) ? 'border-emerald-500/30 bg-emerald-400/10 text-emerald-300'
          : /low_dominant/.test(normalized) ? 'border-rose-500/30 bg-rose-400/10 text-rose-300' : '';
  const tone = outcomeTone || (/selected|active in shadow/.test(normalized)
    ? 'border-violet-400/30 bg-violet-400/10 text-violet-200'
    : /data_stale|unverifiable|unsafe_state|error|fail|stop|reject|invalid|unavailable/.test(normalized)
      ? 'border-rose-500/30 bg-rose-400/10 text-rose-300'
      : /healthy|running|valid|passed|connected|accepted|synced|live_ready|part[12]_active|ok/.test(normalized)
    ? 'border-emerald-500/30 bg-emerald-400/10 text-emerald-300'
    : /degrad|warn|syncing|pending|unknown|partial|insufficient|stale|quarantin|paused|waiting|armed/.test(normalized)
      ? 'border-amber-500/30 bg-amber-400/10 text-amber-200'
      : 'border-zinc-700 bg-zinc-800/70 text-zinc-300');
  return <span className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide ${tone} ${className}`}>{label}</span>;
}

function DistributionBar({ label, items, detail }) {
  const normalized = items.map(item => ({ ...item, value: Math.max(0, Number(item.value) || 0) }));
  const total = normalized.reduce((sum, item) => sum + item.value, 0);
  const description = normalized.map(item => `${item.label} ${item.value}`).join(', ');
  return <section className="rounded-xl border border-white/[0.07] bg-zinc-950/45 p-3.5">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h5 className="text-[11px] font-bold uppercase tracking-[0.13em] text-zinc-300">{label}</h5>
      <span className="text-[11px] tabular-nums text-zinc-500">{total.toLocaleString()} {detail || 'rounds'}</span>
    </div>
    <div role="img" aria-label={`${label}: ${description || 'no data'}`}
      className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
      {normalized.map(item => <span key={item.label} title={`${item.label}: ${item.value}`}
        className={`${item.color} transition-[width] duration-500`} style={{ width: `${total ? item.value / total * 100 : 0}%` }} />)}
    </div>
    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
      {normalized.map(item => <div key={item.label} className="min-w-0 rounded-lg bg-white/[0.025] px-2.5 py-2">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${item.color}`} />{item.label}
        </div>
        <p className="mt-1 text-sm font-semibold tabular-nums text-zinc-100">{item.value.toLocaleString()}
          <span className="ml-1 text-[10px] font-medium text-zinc-500">{total ? `${(item.value / total * 100).toFixed(0)}%` : '—'}</span>
        </p>
      </div>)}
    </div>
  </section>;
}

function RoundWindowProgress({ label, rounds = [], required = 5, tone = 'sky', detail,
  completed, showRoundTiles = true }) {
  const safeRequired = Math.max(1, Number(required) || 5);
  const count = Math.min(safeRequired, Math.max(0, Number(completed ?? rounds.length) || 0));
  const percentComplete = count / safeRequired * 100;
  const barTone = {
    sky: 'from-sky-400 to-cyan-300',
    violet: 'from-violet-400 to-fuchsia-300',
    amber: 'from-amber-400 to-yellow-200',
  }[tone] || 'from-sky-400 to-cyan-300';
  const outcomeTone = item => item?.result === 'WIN' ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-100'
    : item?.result === 'LOSS' ? 'border-rose-400/25 bg-rose-400/10 text-rose-100'
      : item?.outcome === 'H' ? 'border-emerald-400/20 bg-emerald-400/[0.06] text-emerald-100'
        : item?.outcome === 'L' ? 'border-rose-400/20 bg-rose-400/[0.06] text-rose-100'
          : item?.outcome === 'E' ? 'border-amber-400/25 bg-amber-400/[0.07] text-amber-100'
            : 'border-zinc-800 bg-zinc-950/60 text-zinc-500';
  return <section className="rounded-xl border border-white/[0.07] bg-zinc-950/45 p-3.5">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div><h5 className="text-[11px] font-bold uppercase tracking-[0.13em] text-zinc-300">{label}</h5>
        {detail && <p className="mt-1 text-[11px] leading-4 text-zinc-500">{detail}</p>}</div>
      <span className="rounded-md border border-white/[0.06] bg-black/20 px-2 py-1 text-xs font-bold tabular-nums text-zinc-100">
        {count}/{safeRequired}</span>
    </div>
    <div className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-label={label}
      aria-valuemin={0} aria-valuemax={safeRequired} aria-valuenow={count}
      aria-valuetext={`${count} of ${safeRequired} verified rounds`}>
      <div className={`h-full rounded-full bg-gradient-to-r ${barTone} transition-[width] duration-500`}
        style={{ width: `${percentComplete}%` }} />
    </div>
    {showRoundTiles && <details className="mt-2">
      <summary className="cursor-pointer text-[10px] font-medium text-zinc-500">Show verified round details</summary>
      <div className="mt-2 grid grid-cols-5 gap-1.5">
      {Array.from({ length: safeRequired }, (_, index) => {
        const item = rounds[index];
        return <div key={index} title={item ? `Round ${index + 1} · ${item.multiplier ?? '—'}x · ${item.result || item.outcome || 'verified'}` : `Round ${index + 1} waiting`}
          className={`min-w-0 rounded-lg border px-1 py-1.5 text-center ${outcomeTone(item)}`}>
          <span className="block text-[9px] font-bold uppercase tracking-wide opacity-60">R{index + 1}</span>
          <span className="mt-0.5 block truncate text-[10px] font-semibold tabular-nums">{item ? `${number(item.multiplier)}x` : '—'}</span>
          <span className="mt-0.5 block truncate text-[8px] font-semibold uppercase tracking-wide opacity-70">{item?.result || item?.outcome || 'WAIT'}</span>
        </div>;
      })}
      </div>
    </details>}
  </section>;
}

function ShareBar({ items, label = 'Distribution' }) {
  const total = items.reduce((sum, item) => sum + Math.max(0, Number(item.value) || 0), 0);
  return <div>
    <div role="img" aria-label={`${label}: ${items.map(item => `${item.label} ${number(item.value, 0)}`).join(', ')}`}
      className="flex h-3 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
      {items.map(item => <div key={item.label} title={`${item.label}: ${total ? percent(item.value / total) : '—'}`}
        className={`${item.color} transition-[width] duration-500`} style={{ width: `${total ? (Math.max(0, Number(item.value) || 0) / total) * 100 : 0}%` }} />)}
    </div>
    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
      {items.map(item => <div key={item.label} className="rounded-lg bg-zinc-950/50 px-3 py-2">
        <div className="flex items-center justify-between gap-2 text-xs"><span className="flex items-center gap-2 text-zinc-400"><span className={`h-2 w-2 rounded-full ${item.color}`} />{item.label}</span>
          <b className="tabular-nums text-zinc-100">{total ? percent(item.value / total) : '—'}</b></div>
        <p className="mt-1 text-sm font-semibold tabular-nums text-zinc-200">{number(item.value, 0)}</p>
      </div>)}
    </div>
  </div>;
}

function RateMeter({ label, value, detail }) {
  const rate = Number(value);
  const bounded = Number.isFinite(rate) ? Math.max(0, Math.min(1, rate)) : 0;
  return <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
    <div className="flex items-center justify-between gap-3"><span className="text-sm text-zinc-300">{label}</span><b className="text-sm tabular-nums text-emerald-300">{percent(value)}</b></div>
    <div className="mt-2 h-2 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-emerald-400" style={{ width: `${bounded * 100}%` }} /></div>
    {detail && <p className="mt-2 text-xs text-zinc-500">{detail}</p>}
  </div>;
}

function ProgressBar({ label, completed = 0, total = 0, helper }) {
  const safeTotal = Math.max(0, Number(total) || 0);
  const safeCompleted = Math.max(0, Math.min(safeTotal, Number(completed) || 0));
  const remaining = Math.max(0, safeTotal - safeCompleted);
  const progress = safeTotal ? (safeCompleted / safeTotal) * 100 : 0;
  const wholePercent = Math.round(progress);
  return <div className="rounded-xl border border-zinc-800 bg-zinc-950/40 p-4">
    <div className="flex items-start justify-between gap-3">
      <div><h3 className="font-semibold text-zinc-100">{label}</h3>
        {helper && <p className="mt-1 text-xs text-zinc-500">{helper}</p>}</div>
      <span className="rounded-lg bg-emerald-400/10 px-2.5 py-1 text-sm font-bold tabular-nums text-emerald-300">{wholePercent}%</span>
    </div>
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={wholePercent}
      className="mt-4 h-3 overflow-hidden rounded-full bg-zinc-800">
      <div className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-lime-300 transition-[width] duration-500"
        style={{ width: `${progress}%` }} />
    </div>
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
      <span className="inline-flex items-center gap-2 text-zinc-300"><span className="h-2 w-2 rounded-full bg-emerald-400" />Completed <b className="tabular-nums text-white">{safeCompleted.toLocaleString()}</b></span>
      <span className="inline-flex items-center gap-2 text-zinc-500"><span className="h-2 w-2 rounded-full bg-zinc-600" />Remaining <b className="tabular-nums text-zinc-300">{remaining.toLocaleString()}</b></span>
    </div>
  </div>;
}

function OutcomeBar({ counts }) {
  const total = Math.max(0, Number(counts.H || 0) + Number(counts.L || 0) + Number(counts.E || 0));
  const outcomes = [
    { key: 'H', label: 'HIGH', count: Number(counts.H || 0), color: 'bg-emerald-400', text: 'text-emerald-300' },
    { key: 'L', label: 'LOW', count: Number(counts.L || 0), color: 'bg-rose-400', text: 'text-rose-300' },
    { key: 'E', label: 'EXACT', count: Number(counts.E || 0), color: 'bg-amber-300', text: 'text-amber-200' },
  ];
  return <section className={panel}>
    <div className="flex flex-wrap items-end justify-between gap-2">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-300">Last 50 rounds</p>
        <h2 className="mt-1 text-lg font-semibold text-zinc-100">Outcome mix</h2></div>
      <span className="text-xs tabular-nums text-zinc-500">{total} / 50 rounds</span>
    </div>
    <div role="img" aria-label={`Outcome mix: ${outcomes.map(({ label, count }) => `${label} ${count}`).join(', ')}`}
      className="mt-4 flex h-5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
      {outcomes.map(item => <div key={item.key} className={`${item.color} transition-[width] duration-500`}
        style={{ width: `${Math.min(50, item.count) / 50 * 100}%` }} />)}
    </div>
    <div className="mt-4 grid grid-cols-3 gap-2">
      {outcomes.map(item => <div key={item.key} className="rounded-lg bg-zinc-950/50 px-3 py-2.5">
        <p className={`flex items-center gap-2 text-[11px] font-semibold tracking-wide ${item.text}`}>
          <span className={`h-2 w-2 rounded-full ${item.color}`} />{item.label}
        </p>
        <p className="mt-1 text-xl font-bold tabular-nums text-zinc-100">{item.count}</p>
        <p className="text-[11px] text-zinc-500">{total ? percent(item.count / total) : '—'} share</p>
      </div>)}
    </div>
  </section>;
}

function SequenceStrip({ sequence }) {
  const tones = { H: 'bg-emerald-400', L: 'bg-rose-400', E: 'bg-amber-300' };
  const symbols = String(sequence || '').slice(-50).split('');
  return <section className={panel}>
    <div className="flex items-end justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-sky-300">Round order</p>
        <h2 className="mt-1 text-lg font-semibold text-zinc-100">Recent sequence</h2></div>
      <span className="text-xs text-zinc-500">Newest →</span>
    </div>
    {symbols.length ? <div role="img" aria-label={`Recent outcomes: ${symbols.join(' ')}`}
      className="mt-4 flex flex-wrap gap-1.5">
      {symbols.map((symbol, index) => <span key={`${index}-${symbol}`} title={`${index + 1}: ${symbol === 'H' ? 'High' : symbol === 'L' ? 'Low' : 'Exact'}`}
        className={`h-5 w-5 rounded-md ${tones[symbol] || 'bg-zinc-600'} ring-1 ring-white/10`} />)}
    </div> : <p className="mt-4 text-sm text-zinc-500">Waiting for rounds.</p>}
    <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2 text-xs text-zinc-400">
      <span className="inline-flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-emerald-400" />HIGH</span>
      <span className="inline-flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-rose-400" />LOW</span>
      <span className="inline-flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-amber-300" />EXACT</span>
    </div>
  </section>;
}

function Table({ columns, rows, rowKey }) {
  return <div className={`${panel} overflow-x-auto p-0 shadow-inner shadow-black/10`}><table className="w-full min-w-[680px] text-left text-sm">
    <thead className="sticky top-0 bg-zinc-950/90 text-[11px] uppercase tracking-wide text-zinc-500 backdrop-blur"><tr>{columns.map(c => <th key={c.label} className="whitespace-nowrap border-b border-zinc-800 px-3 py-3">{c.label}</th>)}</tr></thead>
    <tbody className="divide-y divide-zinc-800/80">{rows.map((row, index) => <tr key={rowKey ? rowKey(row) : index} className="text-zinc-300 transition-colors odd:bg-white/[0.015] hover:bg-zinc-800/50">
      {columns.map(c => <td key={c.label} className="px-3 py-2.5 align-top">{c.render ? c.render(row) : (row[c.key] ?? '—')}</td>)}
    </tr>)}{!rows.length && <tr><td className="px-3 py-6 text-zinc-500" colSpan={columns.length}>No records available.</td></tr>}</tbody>
  </table></div>;
}

function HighSeriesMachinePanel({ machine = {}, roundSync = {}, refresh, selectedForNextSession = false }) {
  const savedConfig = normalizeStrategyConfiguration(machine.pending_config || machine.config || {});
  const activeConfig = normalizeStrategyConfiguration(
    machine.series?.configuration_snapshot || machine.config || {});
  const [draftConfig, setDraftConfig] = useState(savedConfig);
  const [saveError, setSaveError] = useState('');
  const [saveNotice, setSaveNotice] = useState('');
  const [savingConfig, setSavingConfig] = useState(false);
  const [rulesStatus, setRulesStatus] = useState(null);
  useEffect(() => setDraftConfig(savedConfig), [JSON.stringify(savedConfig)]);
  useEffect(() => {
    let cancelled = false;
    const readStatus = () => api.get('/api/rules-engine/status')
      .then(({ data }) => { if (!cancelled) setRulesStatus(data); })
      .catch(() => { if (!cancelled) setRulesStatus(null); });
    void readStatus();
    const timer = window.setInterval(readStatus, 15000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);
  const configDirty = JSON.stringify(draftConfig) !== JSON.stringify(savedConfig);
  const configErrors = validateStrategyDraft(draftConfig);
  const series = machine.series || {};
  const part1 = series.part1 || { rounds: [], wins: 0, losses: 0 };
  const part2 = series.part2 || {};
  const storedBatch = part2.current_batch;
  const currentBatchClosed = Boolean(storedBatch?.completed_at);
  const batch = currentBatchClosed ? { rounds: [], wins: 0, losses: 0 }
    : (storedBatch || { rounds: [], wins: 0, losses: 0 });
  const hasLatestCompletedBatch = Array.isArray(part2.batches) && part2.batches.length > 0;
  const displayBatchIsCompleted = currentBatchClosed
    || (!storedBatch && hasLatestCompletedBatch && machine.state !== 'PART2_ACTIVE');
  const displayBatch = displayBatchIsCompleted
    ? (part2.batches?.at(-1) || storedBatch || { rounds: [], wins: 0, losses: 0 }) : batch;
  const displayBatchRounds = displayBatch.rounds || [];
  const displayBatchNumber = displayBatchIsCompleted
    ? part2.batches?.length || part2.batch_number || 1 : part2.batch_number || 1;
  const totals = [...(part1.rounds || []), ...(part2.batches || []).flatMap(item => item.rounds || []), ...(batch.rounds || [])];
  const sourceMix = totals.reduce((counts, item) => {
    const category = item.outcome === 'H' || item.outcome === 'L' || item.outcome === 'E' ? item.outcome : 'UNKNOWN';
    counts[category] += 1;
    return counts;
  }, { H: 0, L: 0, E: 0, UNKNOWN: 0 });
  const resultMix = totals.reduce((counts, item) => {
    const result = item.result === 'WIN' ? 'WIN' : item.result === 'LOSS' ? 'LOSS' : 'UNSCORED';
    counts[result] += 1;
    return counts;
  }, { WIN: 0, LOSS: 0, UNSCORED: 0 });
  const net = list => (list?.wins || 0) - (list?.losses || 0);
  const save = async () => {
    setSaveError('');
    setSaveNotice('');
    setSavingConfig(true);
    try {
      const result = await behaviourPut('/hybrid/high-series/config', {
        ...draftConfig,
        pause_limit_verified_rounds: Number(draftConfig.part2.pause_limit_verified_rounds),
        part2_pause_limit_verified_rounds: Number(draftConfig.part2.pause_limit_verified_rounds),
      });
      setDraftConfig(normalizeStrategyConfiguration(result.pending_config || result.config));
      setSaveNotice(result.configuration_status === 'PENDING_NEXT_SERIES'
        ? 'Saved for the next series. The active series keeps its original snapshot.'
        : 'Strategy settings saved.');
      await refresh?.();
    }
    catch (error) { setSaveError(error.message || 'Could not save pause limit.'); }
    finally { setSavingConfig(false); }
  };
  const status = machine.state || 'ARMED_FOR_HIGH';
  const cooldown = status === 'COOLDOWN';
  const paused = status.startsWith('PART2_PAUSED');
  const archivedSeries = machine.completed_series || [];
  const syncIsCurrent = roundSync.status === 'SYNCED';
  const activeRulesSession = rulesStatus?.active_session || null;
  const shadowSession = activeRulesSession?.strategy_id === 'BEHAVIOUR_ROLLING_HIGH'
    ? activeRulesSession : null;
  const lastBehaviourSelection = shadowSession?.last_behaviour_selection;
  const lastSettlement = shadowSession?.last_settled_target;
  const integrationStatus = shadowSession ? 'ACTIVE IN SHADOW'
    : selectedForNextSession ? 'SELECTED FOR NEXT SESSION'
      : rulesStatus ? 'NOT SELECTED' : 'RULES ENGINE UNAVAILABLE';
  const integrationDetail = shadowSession
    ? `Session ${shadowSession.session_id || 'active'} · ${shadowSession.status || 'RUNNING'}`
    : activeRulesSession && selectedForNextSession
      ? `Current session uses ${activeRulesSession.strategy_id || activeRulesSession.mode || 'another source'}; this selection applies after it ends.`
      : selectedForNextSession
        ? 'Start SHADOW session below to activate Behaviour Detection · Rolling HIGH.'
        : 'Behaviour analysis continues independently until this source is selected.';
  const sessionResults = shadowSession?.results || {};
  const acceptedSelectionCount = Math.max(0, Number(shadowSession?.selection_count) || 0);
  const shadowResultMix = [
    { label: 'WIN', value: Number(sessionResults.WIN) || 0, color: 'bg-emerald-400' },
    { label: 'LOSS', value: Number(sessionResults.LOSS) || 0, color: 'bg-rose-400' },
    { label: 'PENDING', value: Number(sessionResults.PENDING) || 0, color: 'bg-amber-300' },
    { label: 'UNCLASSIFIED', value: Math.max(0, acceptedSelectionCount
      - (Number(sessionResults.WIN) || 0) - (Number(sessionResults.LOSS) || 0)
      - (Number(sessionResults.PENDING) || 0)), color: 'bg-zinc-500' },
  ];
  const pauseLimitValue = Math.max(1, Number(activeConfig.part2?.pause_limit_verified_rounds) || 10);
  const sourceIdentityMatches = Boolean(roundSync.latest_verified_identity
    && roundSync.latest_verified_identity === roundSync.last_processed_identity);
  const updatePart = (part, key, value) => setDraftConfig(current => ({
    ...current, [part]: { ...current[part], [key]: value },
  }));
  const updateRoot = (key, value) => setDraftConfig(current => ({ ...current, [key]: value }));
  const numericValue = event => event.target.value === '' ? '' : Number(event.target.value);
  const inputClass = 'mt-1 block h-9 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2.5 text-xs text-zinc-100 outline-none focus:border-emerald-400';
  return <section className="mt-4 space-y-3 rounded-2xl border border-emerald-400/20 bg-zinc-950/45 p-3.5 shadow-inner shadow-black/20 sm:p-4" aria-label="HIGH series lifecycle">
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-white/[0.06] bg-gradient-to-r from-emerald-950/35 via-zinc-950/60 to-sky-950/25 p-3.5">
      <div className="min-w-0">
        <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-emerald-300">Verified series · SHADOW-only accounting</p>
        <h4 className="mt-1 text-base font-semibold text-zinc-50">Rolling HIGH lifecycle</h4>
        <p className="mt-1 text-xs leading-5 text-zinc-400">Eligibility locks before each counted round. Only committed, verified Winner Prediction rounds advance this series.</p>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <StatusBadge value={machine.data_health === 'DATA_STALE' ? 'DATA_STALE' : status} />
        <StatusBadge value={roundSync.status || 'SYNC_UNKNOWN'} />
        {roundSync.live_status && <StatusBadge value={roundSync.live_status} />}
      </div>
    </div>
    <details className="rounded-xl border border-sky-300/20 bg-sky-950/15 p-3">
      <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-xs font-semibold text-sky-100">
        <span>Strategy settings · {activeConfig.strategy_mode} · Part 1 {activeConfig.part1?.max_counted_rounds ?? 5} / Part 2 {activeConfig.part2?.max_counted_rounds_per_batch ?? 5}</span>
        <StatusBadge value={machine.configuration_status || (machine.pending_config ? 'PENDING_NEXT_SERIES' : machine.series ? 'ACTIVE' : 'SAVED')} />
      </summary>
    <section className="mt-3 rounded-xl border border-sky-300/20 bg-sky-950/15 p-3" aria-label="Strategy Management and Win Limits">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><p className="text-[10px] font-bold uppercase tracking-[0.15em] text-sky-300">Strategy Management &amp; Win Limits</p>
          <p className="mt-1 text-[11px] leading-4 text-zinc-400">NET WIN counts confirmed, durably locked SHADOW signal wins. It is separate from monetary profit.</p></div>
        <StatusBadge value={machine.configuration_status || (machine.pending_config ? 'PENDING_NEXT_SERIES' : machine.series ? 'ACTIVE' : 'SAVED')} />
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        <label className="text-[10px] font-medium text-zinc-400">Strategy mode<select className={inputClass} value={draftConfig.strategy_mode}
          onChange={event => updateRoot('strategy_mode', event.target.value)}>
          <option value="HIGH_DOMINANCE_ONLY">HIGH Dominance Only</option><option value="PART1_ONLY">Part 1 Only</option>
          <option value="PART1_AND_PART2">Part 1 + Part 2</option></select></label>
        <fieldset className="rounded-lg border border-white/[0.06] p-2.5"><legend className="px-1 text-[10px] font-semibold text-sky-200">Part 1</legend>
          <label className="flex items-center gap-2 text-[11px] text-zinc-300"><input type="checkbox" checked={draftConfig.part1.enabled} onChange={event => updatePart('part1', 'enabled', event.target.checked)} />Enable Part 1</label>
          <label className="mt-2 block text-[10px] text-zinc-400">Maximum counted rounds<input className={inputClass} type="number" min="1" max="1000" value={draftConfig.part1.max_counted_rounds} onChange={event => updatePart('part1', 'max_counted_rounds', numericValue(event))} /></label>
          <label className="mt-2 flex items-center gap-2 text-[11px] text-zinc-300"><input type="checkbox" checked={draftConfig.part1.net_win_limit_enabled} onChange={event => updatePart('part1', 'net_win_limit_enabled', event.target.checked)} />Enable NET WIN limit</label>
          <label className="mt-2 block text-[10px] text-zinc-400">NET WIN target<input className={inputClass} type="number" min="1" max="1000" value={draftConfig.part1.net_win_target} onChange={event => updatePart('part1', 'net_win_target', numericValue(event))} /></label>
        </fieldset>
        <fieldset className="rounded-lg border border-white/[0.06] p-2.5"><legend className="px-1 text-[10px] font-semibold text-violet-200">Part 2</legend>
          <label className="flex items-center gap-2 text-[11px] text-zinc-300"><input type="checkbox" checked={draftConfig.part2.enabled} onChange={event => updatePart('part2', 'enabled', event.target.checked)} />Enable Part 2</label>
          <label className="mt-2 block text-[10px] text-zinc-400">Maximum counted rounds per batch<input className={inputClass} type="number" min="1" max="1000" value={draftConfig.part2.max_counted_rounds_per_batch} onChange={event => updatePart('part2', 'max_counted_rounds_per_batch', numericValue(event))} /></label>
          <label className="mt-2 flex items-center gap-2 text-[11px] text-zinc-300"><input type="checkbox" checked={draftConfig.part2.net_win_limit_enabled} onChange={event => updatePart('part2', 'net_win_limit_enabled', event.target.checked)} />Enable NET WIN limit</label>
          <label className="mt-2 block text-[10px] text-zinc-400">NET WIN target<input className={inputClass} type="number" min="1" max="1000" value={draftConfig.part2.net_win_target} onChange={event => updatePart('part2', 'net_win_target', numericValue(event))} /></label>
        </fieldset>
        <label className="text-[10px] font-medium text-zinc-400">Part 2 pause limit (verified rounds)<input className={inputClass} type="number" min="1" max="10000" value={draftConfig.part2.pause_limit_verified_rounds} onChange={event => updatePart('part2', 'pause_limit_verified_rounds', numericValue(event))} /></label>
        <label className="text-[10px] font-medium text-zinc-400">Cooldown (verified live rounds)<input className={inputClass} type="number" min="1" max="10000" value={draftConfig.cooldown_verified_rounds} onChange={event => updateRoot('cooldown_verified_rounds', numericValue(event))} /></label>
        <label className="text-[10px] font-medium text-zinc-400">On win limit reached<select className={inputClass} value={draftConfig.on_win_limit_reached} onChange={event => updateRoot('on_win_limit_reached', event.target.value)}><option value="WAIT_FOR_NEXT_FRESH_TRIGGER">Wait for next fresh trigger</option></select></label>
        <label className="text-[10px] font-medium text-zinc-400">Optional pause safety timeout (seconds)<input className={inputClass} type="number" min="1" max="31536000" placeholder="Disabled" value={draftConfig.pause_timeout_seconds ?? ''} onChange={event => updateRoot('pause_timeout_seconds', event.target.value === '' ? null : numericValue(event))} /></label>
        <div className="flex items-center rounded-lg border border-emerald-400/15 bg-emerald-950/15 px-3 py-2 text-[11px] text-emerald-200">SHADOW simulation only · real-money execution disabled</div>
      </div>
      {configErrors.length > 0 && <ul className="mt-3 space-y-1 rounded-lg border border-rose-400/20 bg-rose-950/20 p-2.5 text-[11px] text-rose-200" role="alert">{configErrors.map(error => <li key={error}>{error}</li>)}</ul>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => void save()} disabled={savingConfig || !configDirty || configErrors.length > 0} className="rounded-lg border border-sky-300/25 bg-sky-300/[0.08] px-3 py-2 text-xs font-semibold text-sky-100 disabled:cursor-not-allowed disabled:opacity-40">{savingConfig ? 'Saving…' : 'Save settings'}</button>
        <button type="button" onClick={() => { setDraftConfig(normalizeStrategyConfiguration(machine.pending_config || machine.config || {})); setSaveError(''); setSaveNotice(''); }} disabled={!configDirty || savingConfig} className="rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 disabled:opacity-40">Reset unsaved edits</button>
        {saveError && <span role="alert" className="text-[11px] text-rose-300">{saveError}</span>}
        {saveNotice && !configDirty && <span role="status" className="text-[11px] text-emerald-300">{saveNotice}</span>}
        {configDirty && <span className="text-[11px] text-amber-300">Unsaved draft changes</span>}
      </div>
      <div className="mt-3 grid gap-2 text-[10px] sm:grid-cols-2">
        <p className="rounded-lg border border-white/[0.05] bg-black/20 p-2 text-zinc-400"><b className="text-zinc-200">Active configuration v{activeConfig.configuration_version ?? '—'}:</b> {activeConfig.strategy_mode} · Part 1 {activeConfig.part1?.max_counted_rounds ?? 5} rounds / target {activeConfig.part1?.net_win_target ?? 2} · Part 2 {activeConfig.part2?.max_counted_rounds_per_batch ?? 5} / target {activeConfig.part2?.net_win_target ?? 3}</p>
        <p className="rounded-lg border border-white/[0.05] bg-black/20 p-2 text-zinc-400"><b className="text-zinc-200">Pending next series:</b> {machine.pending_config ? `v${machine.pending_config.configuration_version} · ${machine.pending_config.strategy_mode}` : 'None'}{machine.series ? ` · current series holds v${series.configuration_version ?? '—'}` : ''}</p>
      </div>
    </section>
    </details>
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      <HighMetric compact tone="violet" label="Active series" value={series.series_id || 'Waiting for HIGH'}
        detail={series.trigger_round_id ? `Triggered by ${series.trigger_round_id} · position ${series.trigger_position ?? '—'}` : 'A fresh verified HIGH emergence opens a series.'} valueClass="break-all text-base text-violet-100" />
      <HighMetric compact tone={syncIsCurrent ? 'emerald' : 'amber'} label="Trigger horizon"
        value={`${machine.config?.trigger_horizon ?? 5} rounds`}
        detail={`${syncIsCurrent ? 'Synced state' : 'Last processed state'} · ${machine.last_dominance_state || 'UNKNOWN'}`} />
      <HighMetric compact tone="sky" label="Part 1" value={`${part1.counted_rounds ?? part1.rounds?.length ?? 0}/${activeConfig.part1.max_counted_rounds} settled`}
        detail={`WIN ${part1.confirmed_win_count ?? part1.wins ?? 0} · LOSS ${part1.loss_count ?? part1.losses ?? 0} · remaining wins ${part1.remaining_win_target ?? Math.max(0, activeConfig.part1.net_win_target - (part1.wins || 0))}`} />
      <HighMetric compact tone={paused ? 'amber' : 'emerald'} label="Part 2 current batch"
        value={`${displayBatch.counted_rounds ?? displayBatchRounds.length}/${activeConfig.part2.max_counted_rounds_per_batch} settled`}
        detail={`WIN ${displayBatch.confirmed_win_count ?? displayBatch.wins ?? 0} · LOSS ${displayBatch.loss_count ?? displayBatch.losses ?? 0} · remaining wins ${displayBatch.remaining_win_target ?? Math.max(0, activeConfig.part2.net_win_target - (displayBatch.wins || 0))} · ${displayBatchIsCompleted ? 'latest completed batch' : paused ? `paused · ${part2.pause_reason || 'waiting for HIGH'}` : status === 'PART2_ACTIVE' ? 'collecting' : 'not started'} · batch ${displayBatchNumber}`} />
    </div>
    <div className="grid gap-2 lg:grid-cols-2">
      <RoundWindowProgress label={`Part 1 · up to ${activeConfig.part1.max_counted_rounds} settled signals`} rounds={part1.rounds || []} tone="sky"
        detail="Only a durably locked signal with a committed SHADOW settlement counts." />
      <RoundWindowProgress label={`Part 2 · ${displayBatchIsCompleted ? 'latest completed' : 'batch'} ${displayBatchNumber}`}
        rounds={displayBatchRounds} tone="violet"
        detail={paused ? `Paused: ${part2.pause_reason || 'waiting for HIGH to return'}.` : `${activeConfig.part2.max_counted_rounds_per_batch} settled signals maximum per batch.`} />
    </div>
    {paused && <RoundWindowProgress label="Pause allowance" completed={part2.pause_count || 0}
      showRoundTiles={false} required={pauseLimitValue} tone="amber"
      detail="HIGH is checked for a same-horizon resume before the configured pause limit expires." />}
    <details className="rounded-xl border border-white/[0.07] bg-black/10 p-2.5">
      <summary className="cursor-pointer text-[11px] font-semibold text-zinc-300">Series breakdown and verified rounds · {totals.length} counted</summary>
    <div className="mt-2 grid gap-2 lg:grid-cols-2">
      <DistributionBar label="Verified source classifications" detail="verified rounds" items={[
        { label: 'HIGH', value: sourceMix.H, color: 'bg-emerald-400' },
        { label: 'LOW', value: sourceMix.L, color: 'bg-rose-400' },
        { label: 'EXACT', value: sourceMix.E, color: 'bg-amber-300' },
        { label: 'UNKNOWN', value: sourceMix.UNKNOWN, color: 'bg-zinc-500' },
      ]} />
      <DistributionBar label="Series SHADOW results" detail="counted rounds" items={[
        { label: 'WIN', value: resultMix.WIN, color: 'bg-emerald-400' },
        { label: 'LOSS', value: resultMix.LOSS, color: 'bg-rose-400' },
        { label: 'UNSCORED', value: resultMix.UNSCORED, color: 'bg-zinc-500' },
      ]} />
    </div>
    {shadowSession && <div className="mt-2"><DistributionBar label="Active session result states" detail="accepted SHADOW selections" items={shadowResultMix} /></div>}
    {series.trigger_round_identity && <div className="mt-2 grid gap-2 sm:grid-cols-2">
      <div className="rounded border border-zinc-800 p-2"><b className="text-xs text-sky-200">Part 1 · up to {activeConfig.part1.max_counted_rounds} settled signals</b><div className="mt-1 grid gap-1" style={{ gridTemplateColumns: `repeat(${Math.min(10, activeConfig.part1.max_counted_rounds)}, minmax(0, 1fr))` }}>{Array.from({ length: activeConfig.part1.max_counted_rounds }, (_, i) => { const item = part1.rounds?.[i]; return <div key={i} className="rounded bg-zinc-900 p-1 text-center text-[10px] text-zinc-300">R{i + 1}<br />{item ? `${number(item.multiplier)}x` : '—'}</div>; })}</div><p className="mt-1 text-[10px] text-zinc-400">{part1.wins || 0} WIN · {part1.losses || 0} LOSS · {part1.rounds?.filter(item => item.outcome === 'E').length || 0} EXACT · {net(part1)}u</p></div>
      <div className="rounded border border-zinc-800 p-2"><b className="text-xs text-emerald-200">Part 2 · {displayBatchIsCompleted ? 'latest completed outcomes' : 'current counted outcomes'}</b><div className="mt-1 text-[10px] text-zinc-300">{displayBatchRounds.map((item, i) => `R${i + 1} ${number(item.multiplier)}x`).join(' · ') || 'No counted rounds in this batch'}</div><p className="mt-1 text-[10px] text-zinc-400">Part 2 total {(part2.batches || []).reduce((n, item) => n + item.rounds.length, 0) + (batch.rounds?.length || 0)} · WIN {[(part2.batches || []).flatMap(item => item.rounds), batch.rounds || []].flat().filter(item => item.result === 'WIN').length} · LOSS {[(part2.batches || []).flatMap(item => item.rounds), batch.rounds || []].flat().filter(item => item.result === 'LOSS').length} · EXACT {[(part2.batches || []).flatMap(item => item.rounds), batch.rounds || []].flat().filter(item => item.outcome === 'E').length} · net {net({ wins: [(part2.batches || []).flatMap(item => item.rounds), batch.rounds || []].flat().filter(item => item.result === 'WIN').length, losses: [(part2.batches || []).flatMap(item => item.rounds), batch.rounds || []].flat().filter(item => item.result === 'LOSS').length })}u · pauses {part2.pause_intervals?.length || (part2.pause_started_at ? 1 : 0)}</p></div>
    </div>}
    </details>
    <section className="rounded-xl border border-violet-400/15 bg-gradient-to-br from-violet-950/25 via-zinc-950/55 to-sky-950/20 p-3.5" aria-label="Rules Engine strategy integration">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><p className="text-[10px] font-bold uppercase tracking-[0.14em] text-violet-300">Rules Engine handoff</p>
          <h5 className="mt-1 text-sm font-semibold text-violet-100">Behaviour Detection · Rolling HIGH</h5>
          <p className="mt-1 text-[11px] leading-4 text-zinc-400">{integrationDetail}</p>
        </div>
        <StatusBadge value={integrationStatus} />
      </div>
      <details className="mt-2">
      <summary className="cursor-pointer text-[10px] font-semibold text-zinc-400">SHADOW delivery and settlement detail</summary>
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        <div className="min-w-0 rounded-lg border border-white/[0.05] bg-black/20 p-2.5">
          <p className="text-[9px] font-bold uppercase tracking-wide text-zinc-500">Accepted selections</p>
          <p className="mt-1 text-sm font-semibold tabular-nums text-violet-100">{shadowSession?.selection_count ?? 0}</p>
          <p className="mt-1 break-all text-[10px] leading-4 text-zinc-500">{lastBehaviourSelection ? `${lastBehaviourSelection.part || 'PART'} · ${lastBehaviourSelection.batch_id || 'batch —'}` : 'No accepted source selection yet.'}</p>
        </div>
        <div className="min-w-0 rounded-lg border border-white/[0.05] bg-black/20 p-2.5">
          <p className="text-[9px] font-bold uppercase tracking-wide text-zinc-500">Latest source round</p>
          <p className="mt-1 break-all text-xs font-semibold text-sky-100">{lastBehaviourSelection?.source_round_id || roundSync.latest_verified_round_id || 'Waiting for a verified round'}</p>
          <p className="mt-1 text-[10px] text-zinc-500">Sequence {roundSync.latest_source_sequence ?? '—'} · {sourceIdentityMatches ? 'reducer caught up' : 'sync pending'}</p>
        </div>
        <div className="min-w-0 rounded-lg border border-white/[0.05] bg-black/20 p-2.5">
          <p className="text-[9px] font-bold uppercase tracking-wide text-zinc-500">Latest settlement</p>
          <p className="mt-1 text-xs font-semibold text-zinc-100">{lastSettlement ? `Round ${lastSettlement.round_id || '—'}` : 'No settlement yet'}</p>
          <p className="mt-1 text-[10px] text-zinc-500">{lastSettlement ? (lastSettlement.results || []).map(item => item.result).join(' · ') || 'Committed' : 'Pending targets stay visible until verified.'}</p>
        </div>
      </div>
      </details>
      <p className="mt-3 rounded-lg border border-white/[0.05] bg-black/15 px-3 py-2 text-[11px] text-zinc-300">
        Eligibility: <b className={shadowSession?.behaviour_strategy?.eligible ? 'text-emerald-200' : 'text-amber-200'}>
          {shadowSession?.behaviour_strategy ? `${shadowSession.behaviour_strategy.current_dominance_state || 'UNKNOWN'} · ${shadowSession.behaviour_strategy.eligible ? 'ELIGIBLE' : shadowSession.behaviour_strategy.reason || 'NOT ELIGIBLE'}`
            : selectedForNextSession ? 'Waiting for a verified eligibility update in this session.'
              : status.startsWith('PART2_PAUSED') ? 'Part 2 is paused while it waits for HIGH.'
                : machine.data_health === 'DATA_STALE' ? 'Not eligible · source data is stale.' : 'No current eligible HIGH signal.'}</b>
      </p>
    </section>
    {status === 'PART1_COLLECTING' && <p className="mt-2 rounded bg-sky-950/40 p-2 text-xs text-sky-200">Part 1 is collecting. Part 2 eligibility will be evaluated after R5.</p>}
    {status === 'PART1_COMPLETED' && !part1.completion_pending && <p className="mt-2 text-xs text-emerald-200">Part 1 completed; Part 2 opens on the next eligible verified round.</p>}
    {status === 'PART1_COMPLETED' && part1.completion_pending && <p className="mt-2 text-xs text-amber-200">Part 1 is data-incomplete: the rolling horizon at R5 was unavailable, so continuation is not evaluated from a later state.</p>}
    {series.stop_reason && <p className="mt-2 text-xs text-rose-200">Stopped: {series.stop_reason}{series.last_pause_reason ? ` · last pause reason: ${series.last_pause_reason}` : ''}</p>}
    {paused && <p className="mt-2 text-xs text-amber-200">Pause progress {part2.pause_count || 0}/{activeConfig.part2.pause_limit_verified_rounds ?? 10} verified rounds · HIGH return is checked before expiration on the limit observation.</p>}
    {cooldown && <p className="mt-2 text-xs text-amber-200">Cooldown: {machine.cooldown_progress || 0}/{activeConfig.cooldown_verified_rounds ?? 10} verified live rounds · remaining {Math.max(0, (activeConfig.cooldown_verified_rounds ?? 10) - (machine.cooldown_progress || 0))} · waiting for a fresh HIGH exit/re-entry transition.</p>}
    <details className="mt-2"><summary className="cursor-pointer text-[10px] text-zinc-400">Series event timeline ({machine.events?.length || 0})</summary><ol className="mt-1 max-h-40 space-y-1 overflow-auto">{(machine.events || []).slice(-30).reverse().map((event, i) => <li key={`${event.type}:${event.round_identity}:${i}`} className="text-[10px] text-zinc-400">{event.timestamp} · <b className="text-zinc-200">{event.type}</b> · round {event.round_id || event.round_identity || '—'}</li>)}</ol></details>
    {archivedSeries.length > 0 && <details className="mt-2 rounded border border-zinc-800 bg-black/20 p-2"><summary className="cursor-pointer text-[10px] text-zinc-300">Archived verified series ({archivedSeries.length})</summary><div className="mt-2 max-h-80 space-y-2 overflow-auto">{[...archivedSeries].reverse().map(item => {
      const part1Rounds = item.part1?.rounds || [];
      const part2Batches = item.part2?.batches || [];
      const currentPart2Batch = item.part2?.current_batch && !item.part2.current_batch.completed_at
        ? item.part2.current_batch : null;
      const part2Rounds = [...part2Batches.flatMap(batchItem => batchItem.rounds || []),
        ...(currentPart2Batch?.rounds || [])];
      const allRounds = [...part1Rounds, ...part2Rounds];
      const wins = allRounds.filter(round => round.result === 'WIN').length;
      const losses = allRounds.filter(round => round.result === 'LOSS').length;
      return <article key={item.series_id} className="rounded border border-zinc-800 p-2 text-[10px] text-zinc-400">
        <div className="flex flex-wrap justify-between gap-x-3 gap-y-1"><b className="text-zinc-200">{item.series_id}</b><span>{item.stop_reason || item.terminal_state || 'COMPLETED'} · {timestamp(item.archived_at)}</span></div>
        <p className="mt-1">Trigger round {item.trigger_round_id || item.trigger_round_identity} · anchor #{item.trigger_position} · Part 1 {part1Rounds.length}/5 · Part 2 {part2Batches.length} completed batches / {part2Rounds.length} counted rounds{currentPart2Batch ? ` · partial batch ${currentPart2Batch.rounds?.length || 0}/5` : ''}</p>
        <p className="mt-1">Part 1: {part1Rounds.map((round, i) => `R${i + 1} ${round.multiplier}x ${round.result}`).join(' · ') || 'No results'} · {item.part1?.wins || 0} WIN / {item.part1?.losses || 0} LOSS</p>
        <div className="mt-1 space-y-1">{part2Batches.map((batchItem, index) => <p key={`${item.series_id}:${index}`}>Part 2 batch {index + 1}: {(batchItem.rounds || []).map((round, i) => `R${i + 1} ${round.multiplier}x ${round.result}`).join(' · ')} · {batchItem.wins || 0} WIN / {batchItem.losses || 0} LOSS · state {batchItem.state_after_r5 || '—'}</p>)}
          {currentPart2Batch && <p>Part 2 batch {item.part2?.batch_number || part2Batches.length + 1} · incomplete {currentPart2Batch.rounds?.length || 0}/5: {(currentPart2Batch.rounds || []).map((round, i) => `R${i + 1} ${round.multiplier}x ${round.result}`).join(' · ')} · {currentPart2Batch.wins || 0} WIN / {currentPart2Batch.losses || 0} LOSS · stopped {item.stop_reason || item.terminal_state || '—'}</p>}
        </div>
        <p className="mt-1 text-zinc-300">Series total: {wins} WIN / {losses} LOSS · {wins - losses > 0 ? '+' : ''}{number(wins - losses)}u net · SHADOW-only</p>
      </article>;
    })}</div></details>}
  </section>;
}

export function BehaviourDetectionSelectionCard({ selectedForNextSession = true }) {
  const [liveStatus, setLiveStatus] = useState(null);
  const [seriesData, setSeriesData] = useState(null);
  const [error, setError] = useState('');
  const [seriesError, setSeriesError] = useState('');
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);
  const requestInFlight = useRef(false);
  const refresh = useCallback(async () => {
    if (requestInFlight.current) return false;
    requestInFlight.current = true;
    try {
      const next = await behaviourGet('/live-status');
      setLiveStatus(next);
      setError('');
      setUpdatedAt(new Date().toISOString());
      return true;
    } catch (reason) {
      setError(reason?.message || 'Behaviour Detection is temporarily unavailable.');
      return false;
    } finally {
      requestInFlight.current = false;
    }
  }, []);

  const refreshSeriesDetails = useCallback(async () => {
    setSeriesLoading(true);
    setSeriesError('');
    try {
      const next = await behaviourGet('/hybrid');
      setSeriesData(next);
      return true;
    } catch (reason) {
      setSeriesError(reason?.message || 'Series details are temporarily unavailable.');
      return false;
    } finally {
      setSeriesLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    let timer = null;
    const poll = async () => {
      await refresh();
      if (active) timer = window.setTimeout(poll, 5000);
    };
    void poll();
    const refreshVisible = () => {
      if (document.visibilityState === 'visible' && !requestInFlight.current) {
        window.clearTimeout(timer);
        void poll();
      }
    };
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [refresh]);

  const audit = seriesData?.high_dominance_2x || {};
  const machine = audit.series_machine || {};
  const roundSync = audit.round_sync || {};
  const processor = liveStatus?.processor || {};
  const eligibility = processor.latest_eligibility || {};
  const activeHorizon = machine.config?.trigger_horizon ?? eligibility.trigger_horizon ?? audit.trigger_horizon ?? 5;
  const verifiedHorizonState = roundSync.status === 'SYNCED'
    ? audit.current_horizon_states?.[activeHorizon] : null;
  const processedRoundId = processor.latest_live_round_uid || eligibility.source_round_id;
  const observedRoundId = liveStatus?.orbit?.latest_verified_round_uid;
  const sourceRoundId = observedRoundId || processedRoundId || roundSync.latest_verified_round_id;
  const sourceIdentity = processor.latest_live_round_identity || eligibility.decision_round_identity
    || roundSync.latest_verified_identity;
  const dataHealth = eligibility.verified_data_freshness?.data_health || machine.data_health;
  const state = dataHealth === 'DATA_STALE' ? 'DATA_STALE'
    : machine.state || liveStatus?.strategy_state || processor.series_state || 'CONNECTING';
  const dominance = verifiedHorizonState || machine.last_dominance_state
    || eligibility.current_dominance_state || audit.current_confirmed_state || 'WAITING';
  const processorMatchesEligibility = !eligibility.decision_round_identity
    || eligibility.decision_round_identity === processor.latest_live_round_identity;
  const processorMatchesObservation = !observedRoundId || observedRoundId === processor.latest_live_round_uid;
  const sourceIsCurrent = liveStatus
    ? processor.status === 'LIVE_READY' && processor.provenance === 'LIVE_VERIFIED'
      && Boolean(processor.latest_live_round_identity) && processorMatchesEligibility && processorMatchesObservation
      && dataHealth !== 'DATA_STALE'
    : roundSync.status === 'SYNCED' && roundSync.live_status === 'LIVE_READY'
      && dataHealth !== 'DATA_STALE';
  const sourceStatus = liveStatus?.processor_state || roundSync.status || 'SYNC_CHECKING';
  const latestSourceSequence = roundSync.latest_verified_round_id === sourceRoundId
    ? roundSync.latest_source_sequence ?? eligibility.source_sequence : null;
  const dominanceTone = dominance === 'HIGH_DOMINANT' ? 'emerald'
    : dominance === 'LOW_DOMINANT' ? 'rose' : 'sky';
  const sourceTone = sourceIsCurrent ? 'emerald'
    : dataHealth === 'DATA_STALE' || sourceStatus === 'DATA_STALE' ? 'rose' : 'amber';

  return <section className="selected-shadow-source mt-4 rounded-xl border border-violet-400/25 p-3 sm:p-4"
    aria-label="Behaviour Detection SHADOW source">
    <div className="flex flex-wrap items-start justify-between gap-2 border-b border-violet-300/15 pb-3">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-violet-300/20 bg-violet-300/10 text-base font-black text-violet-200">H</span>
        <div className="min-w-0">
          <p className="text-[10px] font-bold uppercase tracking-[0.17em] text-violet-300">Selected SHADOW source</p>
          <h3 className="mt-1 text-base font-semibold text-violet-50">Behaviour Detection · Rolling HIGH</h3>
          <p className="mt-1 max-w-3xl text-[11px] leading-4 text-zinc-400">
            Verified HIGH eligibility feeds future-only SHADOW signals. Rules Engine owns signal acceptance; SHADOW owns settlement and results.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge value={selectedForNextSession ? 'SELECTED FOR NEXT SESSION' : 'NOT SELECTED'} />
        <StatusBadge value={state} />
        <StatusBadge value={sourceStatus} />
      </div>
    </div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      <HighMetric compact tone={dominanceTone} label={verifiedHorizonState ? 'Current verified rolling state' : sourceIsCurrent || dominance === 'WAITING' ? 'Rolling state' : 'Last processed state'} value={dominance}
        detail={`Horizon ${activeHorizon} verified rounds${verifiedHorizonState ? ' · synced to latest source' : ''}`} />
      <HighMetric compact tone={sourceTone} label={sourceIsCurrent ? 'Verified source' : 'Latest verified source'} value={latestSourceSequence == null
        ? (sourceRoundId || 'Waiting') : `#${latestSourceSequence}`}
        detail={`${sourceRoundId || 'No verified round yet'} · ${sourceIsCurrent ? 'live and processed' : `${liveStatus?.orbit?.latest_verified_delivery_mode || processor.provenance || 'not live'} · processor ${processedRoundId || 'waiting'} · ${sourceStatus}`} `} valueClass="text-base text-emerald-100" />
      <HighMetric compact tone={state === 'PART2_ACTIVE' ? 'emerald'
        : state.startsWith('PART2_PAUSED') ? 'amber' : 'violet'} label="HIGH series lifecycle"
        value={state}
        detail={machine.series?.series_id || eligibility.series_id || 'Waiting for a fresh qualifying HIGH emergence'} valueClass="text-base text-violet-100" />
      <HighMetric compact tone={error ? 'amber' : 'sky'} label="Source timing"
        value={(liveStatus?.orbit?.last_browser_observation_at || roundSync.latest_observed_at
          || processor.latest_live_processed_at) ? shortTime(liveStatus?.orbit?.last_browser_observation_at
            || roundSync.latest_observed_at || processor.latest_live_processed_at) : 'Checking source'}
        detail={`${error ? `API check failed ${updatedAt ? shortTime(updatedAt) : ''} · ${error}` : `Source observed · API checked ${updatedAt ? shortTime(updatedAt) : '—'} · refreshes every 5s`}`} valueClass="text-base text-sky-100" />
    </div>
    <details className="mt-2 rounded-lg border border-white/[0.06] bg-black/20 px-3 py-2 text-[10px]">
      <summary className="cursor-pointer font-medium text-zinc-400">Latest verified round identity and feed detail</summary>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <span className="min-w-0 break-all text-zinc-400">Observed verified round <b className="text-zinc-200">{sourceRoundId || 'waiting'}</b> · processed identity <b className="text-zinc-200">{sourceIdentity || 'waiting'}</b></span>
      <span className={sourceIsCurrent ? 'font-semibold text-emerald-300' : 'font-semibold text-amber-200'}>
        {sourceIsCurrent ? 'Latest round is processed' : `Feed ${roundSync.live_status || liveStatus?.processor_state || 'not confirmed'} · ${sourceStatus}`}
      </span>
      </div>
    </details>
    {error && <p role="status" className="mt-3 rounded-lg border border-amber-400/25 bg-amber-950/25 px-3 py-2.5 text-xs leading-5 text-amber-100">{error} · Showing the last available verified snapshot; API check time is separate from source observation time.</p>}
    <details className="mt-3" onToggle={event => { if (event.currentTarget.open && !seriesData && !seriesLoading) void refreshSeriesDetails(); }}>
      <summary className="cursor-pointer rounded-lg border border-emerald-400/15 bg-emerald-950/10 px-3 py-2.5 text-xs font-semibold text-emerald-100">Series configuration, progress, results and history</summary>
      {seriesLoading && <p role="status" className="mt-2 text-xs text-zinc-400">Loading detailed series history…</p>}
      {seriesError && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-amber-200" role="status">
        <span>{seriesError}</span><button type="button" onClick={() => void refreshSeriesDetails()} className="underline underline-offset-2">Retry</button>
      </div>}
      {machine.state && <HighSeriesMachinePanel machine={machine} roundSync={roundSync}
        refresh={refreshSeriesDetails} selectedForNextSession={selectedForNextSession} />}
    </details>
    <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-sky-300/15 bg-gradient-to-r from-sky-950/20 to-violet-950/20 p-3.5">
      <p className="max-w-3xl text-xs leading-5 text-zinc-300">{selectedForNextSession
        ? 'This source is selected for the next SHADOW session. Start it below; signals still require a fresh verified decision and future target round.'
        : 'Behaviour analysis remains live independently; select this source to use its HIGH eligibility in SHADOW.'}</p>
      <Link to="/behaviour" className="shrink-0 rounded-lg border border-sky-300/20 bg-sky-300/[0.06] px-3 py-2 text-xs font-semibold text-sky-200 transition hover:border-sky-200/40 hover:bg-sky-300/10 hover:text-sky-50">Open Behaviour Detection <span aria-hidden="true">→</span></Link>
    </div>
  </section>;
}

export function BehaviourStrategySessionSummary() {
  const [machine, setMachine] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    const load = () => behaviourGet('/hybrid').then(payload => {
      if (!active) return;
      setMachine(payload?.high_dominance_2x?.series_machine || null);
      setError('');
    }).catch(reason => { if (active) setError(reason?.message || 'Behaviour strategy state is unavailable.'); });
    void load();
    const timer = window.setInterval(load, 5000);
    const refreshVisible = () => { if (document.visibilityState === 'visible') void load(); };
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener('focus', refreshVisible); document.removeEventListener('visibilitychange', refreshVisible); };
  }, []);
  const config = normalizeStrategyConfiguration(machine?.series?.configuration_snapshot || machine?.config || {});
  const series = machine?.series || {};
  const part = series.current_part === 'PART2' ? 'part2' : 'part1';
  const counter = part === 'part2' ? series.part2?.current_batch : series.part1;
  const settledWins = Number(counter?.confirmed_win_count ?? counter?.wins) || 0;
  const settledLosses = Number(counter?.loss_count ?? counter?.losses) || 0;
  const counted = Number(counter?.counted_rounds ?? counter?.rounds?.length) || 0;
  const target = Number(config[part]?.net_win_target) || 0;
  const limitEnabled = Boolean(config[part]?.net_win_limit_enabled);
  const remaining = Math.max(0, target - settledWins);
  const cooldown = Number(config.cooldown_verified_rounds) || 10;
  return <section aria-label="Behaviour strategy configuration for SHADOW session" className="mt-4 rounded-xl border border-fuchsia-400/20 bg-fuchsia-950/10 p-3.5">
    <div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="text-sm font-semibold text-fuchsia-100">Behaviour strategy in this SHADOW session</h3><p className="mt-1 text-[11px] text-zinc-400">Shared Behaviour reducer state · refreshed from the Behaviour backend</p></div>
      <span className="rounded border border-emerald-400/20 bg-emerald-950/20 px-2 py-1 text-[10px] font-semibold text-emerald-200">SHADOW simulation only</span></div>
    {error && <p role="status" className="mt-2 text-[11px] text-amber-200">{error}</p>}
    {!machine && !error && <p className="mt-2 text-[11px] text-zinc-400">Loading shared strategy state…</p>}
    {machine && <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      <HighMetric tone="violet" label="Strategy mode" value={config.strategy_mode} detail={`Configuration v${series.configuration_version ?? config.configuration_version ?? '—'}`} valueClass="text-sm text-violet-100" />
      <HighMetric tone="sky" label="Active series / part" value={series.series_id ? `${series.current_part || '—'}` : machine.state || 'WAITING'} detail={series.series_id || 'No active series'} valueClass="break-all text-sm text-sky-100" />
      <HighMetric tone="emerald" label="NET WIN target" value={limitEnabled ? target : 'OFF'} detail={`${settledWins} confirmed wins · ${remaining} remaining · ${settledLosses} losses`} />
      <HighMetric tone="sky" label="Counted rounds" value={counted} detail={`Maximum ${part === 'part2' ? config.part2.max_counted_rounds_per_batch : config.part1.max_counted_rounds}`} />
      <HighMetric tone="amber" label="Stop reason" value={series.stop_reason || 'NONE'} detail={machine.state === 'COOLDOWN' ? `${Math.max(0, cooldown - (machine.cooldown_progress || 0))} verified cooldown rounds remain` : `Cooldown ${machine.cooldown_progress || 0}/${cooldown}`} valueClass="text-sm text-amber-100" />
      <HighMetric tone={machine.state === 'WAITING_FOR_FRESH_TRIGGER' ? 'amber' : 'violet'} label="Fresh trigger" value={machine.state === 'WAITING_FOR_FRESH_TRIGGER' ? 'WAITING' : machine.state === 'COOLDOWN' ? 'COOLDOWN' : 'NOT WAITING'} detail={machine.state === 'WAITING_FOR_FRESH_TRIGGER' ? 'Requires a later verified HIGH exit and re-entry.' : `State ${machine.state || '—'}`} valueClass="text-sm" />
      <HighMetric tone="sky" label="Configuration changes" value={machine.pending_config ? `PENDING v${machine.pending_config.configuration_version}` : machine.series ? 'ACTIVE SNAPSHOT' : 'SAVED'} detail={machine.pending_config ? 'Applies to the next series.' : 'No pending next-series settings.'} valueClass="text-sm" />
    </div>}
  </section>;
}

function HighDominance2xCard({ audit = {}, onDelete }) {
  const [deletingBatchId, setDeletingBatchId] = useState('');
  const [bulkDeletingPhase, setBulkDeletingPhase] = useState('');
  const [selectedBatchIds, setSelectedBatchIds] = useState({ INITIAL: [], CONTINUATION: [] });
  const [deleteMessage, setDeleteMessage] = useState('');
  const [expandedParts, setExpandedParts] = useState({});
  const [historyPages, setHistoryPages] = useState({});
  const [historyLoading, setHistoryLoading] = useState({});
  const [historyErrors, setHistoryErrors] = useState({});
  const [resettingPerformanceHistory, setResettingPerformanceHistory] = useState(false);
  const active = audit.active_batch;
  const batches = audit.batches || [];
  const summary = audit.summary || {};
  const machine = audit.series_machine || {};
  const machineIsAuthoritative = machine.policy_version === 'high-series-v1';
  const machineSeries = machine.series;
  const machinePart1 = machineSeries?.part1;
  const machinePart2 = machineSeries?.part2;
  const projectRounds = (rounds = []) => rounds.map(item => ({
    cohort_position: item.position, identity_key: item.identity_key,
    round_id: item.round_id, multiplier: item.multiplier,
    classification: item.outcome, result_at_2x: item.result,
    observed_at: item.timestamp,
  }));
  // Persisted reducer state drives the live panels. Legacy audit rows remain
  // available for history but may lag a migrated live series.
  const series = machineSeries ? {
    series_anchor_position: machineSeries.trigger_position,
    trigger_horizon: String(machineSeries.trigger_horizon || machine.config?.trigger_horizon || 5),
    continuation_batch_count: machinePart2?.batches?.length || 0,
    initial_batch: {
      batch_id: `${machineSeries.series_id}:INITIAL`,
      series_anchor_position: machineSeries.trigger_position,
      batch_number: 1, decision_position: machineSeries.trigger_position,
      decision_round_id: machineSeries.trigger_round_id,
      trigger_horizon: String(machineSeries.trigger_horizon || 5), phase: 'INITIAL',
      status: machinePart1?.rounds?.length === 5 ? 'COMPLETED' : 'COLLECTING',
      round_progress: { observed: machinePart1?.rounds?.length || 0, required: 5 },
      outcomes: projectRounds(machinePart1?.rounds), wins: machinePart1?.wins || 0,
      losses: machinePart1?.losses || 0,
      net_units: (machinePart1?.wins || 0) - (machinePart1?.losses || 0),
      state_at_end: machinePart1?.state_after_r5 || 'COLLECTING',
    },
    continuation_batches: (machinePart2?.batches || []).map((item, index) => {
      const preceding = index ? machinePart2.batches[index - 1] : null;
      const decisionRound = preceding?.rounds?.at(-1) || machinePart1?.rounds?.[4];
      return {
        batch_id: `${machineSeries.series_id}:CONTINUATION:${index + 1}`,
        series_anchor_position: machineSeries.trigger_position, batch_number: index + 1,
        decision_position: decisionRound?.position,
        decision_round_id: decisionRound?.round_id,
        trigger_horizon: String(machineSeries.trigger_horizon || 5), phase: 'CONTINUATION',
        status: 'COMPLETED', round_progress: { observed: item.rounds?.length || 0, required: 5 },
        outcomes: projectRounds(item.rounds), wins: item.wins || 0,
        losses: item.losses || 0,
        net_units: (item.wins || 0) - (item.losses || 0),
        state_at_end: item.state_after_r5 || '—',
      };
    }),
  } : (machine.state ? {} : (audit.current_series || {}));
  const initial = series.initial_batch;
  const initialComplete = Number(initial?.round_progress?.observed) >= 5
    && Boolean(initial?.state_at_end) && initial.state_at_end !== 'INSUFFICIENT_DATA';
  const continuations = series.continuation_batches || [];
  const machineBatch = machinePart2?.current_batch;
  const machineBatchClosed = Boolean(machineBatch?.completed_at);
  const machineStatus = machine.state;
  const machineCurrentContinuation = machineSeries && machinePart2 ? {
    batch_id: `${machineSeries.series_id}:CONTINUATION:${machinePart2.batch_number}`,
    series_anchor_position: machineSeries.trigger_position,
    batch_number: machinePart2.batch_number,
    decision_position: machinePart2.batches?.at(-1)?.rounds?.at(-1)?.position || machinePart1?.rounds?.[4]?.position,
    decision_round_id: machinePart2.batches?.at(-1)?.rounds?.at(-1)?.round_id || machinePart1?.rounds?.[4]?.round_id,
    trigger_horizon: String(machineSeries.trigger_horizon || 5), phase: 'CONTINUATION',
    status: machineStatus === 'PART2_ACTIVE' ? 'COLLECTING'
      : machineStatus === 'PART2_PAUSED_LOW_DOMINANT' ? 'STOPPED_LOW_DOMINANT'
        : machineStatus === 'PART2_PAUSED_NON_HIGH' ? 'STOPPED_STATE_EXIT'
          : machineStatus === 'COOLDOWN' ? machineSeries.stop_reason : 'COLLECTING',
    round_progress: { observed: machineBatchClosed ? 0 : (machineBatch?.rounds?.length || 0), required: 5 },
    outcomes: projectRounds(machineBatchClosed ? [] : machineBatch?.rounds),
    wins: machineBatchClosed ? 0 : (machineBatch?.wins || 0),
    losses: machineBatchClosed ? 0 : (machineBatch?.losses || 0),
    net_units: machineBatchClosed ? 0 : (machineBatch?.wins || 0) - (machineBatch?.losses || 0),
    state_at_end: machine.last_dominance_state || 'UNKNOWN',
  } : null;
  // Legacy audit rows may remain COLLECTING after the reducer has stopped the
  // series. They are history only once the persisted reducer policy is active.
  const continuation = machineCurrentContinuation || (!machineIsAuthoritative
    ? (active?.phase === 'CONTINUATION' ? active : continuations[0])
    : null);
  const triggerHorizon = machine.config?.trigger_horizon || active?.trigger_horizon || series.trigger_horizon || audit.trigger_horizon;
  const roundSync = audit.round_sync || {};
  const roundSyncCurrent = roundSync.status === 'SYNCED';
  const currentVerifiedWindow = audit.current_verified_window || [];
  const currentVerifiedWins = currentVerifiedWindow.filter(item => item.result_at_2x === 'WIN').length;
  const currentVerifiedLosses = currentVerifiedWindow.filter(item => item.result_at_2x === 'LOSS').length;
  const triggerState = roundSyncCurrent
    ? audit.current_horizon_states?.[triggerHorizon] || audit.trigger_state || machine.last_dominance_state
    : null;
  const archivedAnchors = new Set((machine.completed_series || []).map(item => item.trigger_position));
  const previousBatches = batches.filter(batch =>
    batch.series_anchor_position !== (machineSeries?.trigger_position ?? series.series_anchor_position)
      && !archivedAnchors.has(batch.series_anchor_position));
  const newestFirst = items => [...items].sort((a, b) =>
    Number(b.series_anchor_position) - Number(a.series_anchor_position)
    || Number(b.batch_number) - Number(a.batch_number));
  const previousInitialBatches = newestFirst(previousBatches.filter(batch => batch.phase === 'INITIAL'));
  const previousContinuationBatches = newestFirst(previousBatches.filter(batch => batch.phase === 'CONTINUATION'));
  const netUnits = Number(summary.net_units) || 0;
  const netText = netUnits > 0 ? `+${number(netUnits)} units profit`
    : netUnits < 0 ? `−${number(Math.abs(netUnits))} units loss` : '0.00 units · break even';
  const netTone = netUnits > 0 ? 'text-emerald-300' : netUnits < 0 ? 'text-rose-300' : 'text-zinc-300';
  const machineState = machine.data_health === 'DATA_STALE' ? 'DATA_STALE'
    : !roundSyncCurrent && roundSync.status ? roundSync.status : (machine.state || audit.status || 'IDLE');
  const livePart2 = machineSeries?.part2;
  const liveMachineBatch = livePart2?.current_batch;
  const currentWindowProgress = liveMachineBatch?.completed_at ? 0 : (liveMachineBatch?.rounds?.length || 0);
  const currentWindowDetail = machineState === 'PART1_COLLECTING'
    ? `Part 1 ${machineSeries?.part1?.rounds?.length || 0}/5 · Part 2 eligibility after R5`
    : machineState.startsWith('PART2_PAUSED')
      ? `Batch ${livePart2?.batch_number || '—'} paused at ${currentWindowProgress}/5 · ${livePart2?.pause_reason || 'non-HIGH'}`
      : machineState === 'PART2_ACTIVE'
        ? `Batch ${livePart2?.batch_number || '—'} active · ${currentWindowProgress}/5 counted rounds`
        : machineState === 'COOLDOWN'
          ? `Cooldown ${machine.cooldown_progress || 0}/${machine.config?.new_series_cooldown_rounds || 10} · waiting for fresh HIGH after cooldown`
          : machineState === 'PART1_COMPLETED'
            ? 'Part 1 complete · evaluating continuation from R5 state'
          : machineState === 'ARMED_FOR_HIGH'
              ? triggerState === 'HIGH_DOMINANT' ? 'Armed · HIGH is active; next verified round locks the decision' : 'Armed · waiting for HIGH_DOMINANT'
              : 'No Part 2 window is currently open';

  useEffect(() => {
    setExpandedParts({});
    setHistoryPages({});
    setHistoryErrors({});
    setSelectedBatchIds({ INITIAL: [], CONTINUATION: [] });
  }, [series.series_anchor_position]);

  const deleteBatch = async batch => {
    const phaseLabel = batch.phase === 'INITIAL' ? 'Part 1 initial batch' : 'Part 2 continuation batch';
    if (!window.confirm(`Delete ${phaseLabel} for decision round #${batch.decision_round_id || batch.decision_position}? This removes the saved audit entry and changes the displayed net totals. Verified rounds remain unchanged.`)) return;
    setDeletingBatchId(batch.batch_id);
    setDeleteMessage('');
    try {
      await behaviourDelete(`/hybrid/high-dominance-2x/${encodeURIComponent(batch.batch_id)}`);
      const totalsRefreshed = await onDelete?.();
      setHistoryPages({});
      setDeleteMessage(totalsRefreshed === false || !onDelete
        ? 'Batch deleted. Net totals will update after the next successful refresh.'
        : 'Saved batch deleted. Net totals refreshed.');
    } catch (reason) {
      setDeleteMessage(reason?.message || 'Could not delete this batch.');
    } finally {
      setDeletingBatchId('');
    }
  };

  const setBatchSelected = (phase, batchId, selected) => {
    setSelectedBatchIds(current => {
      const ids = current[phase] || [];
      return { ...current, [phase]: selected
        ? (ids.includes(batchId) ? ids : [...ids, batchId])
        : ids.filter(id => id !== batchId) };
    });
  };

  const deleteBatches = async (phase, { deleteAll = false, batchIds = [] } = {}) => {
    const partName = phase === 'INITIAL' ? 'Part 1 initial' : 'Part 2 continuation';
    const partItems = phase === 'INITIAL' ? previousInitialBatches : previousContinuationBatches;
    const count = deleteAll ? (audit.phase_summaries?.[phase]?.batch_count ?? partItems.length) : batchIds.length;
    if (!count) return;
    const actionText = deleteAll ? `all ${count} historical ${partName} batches` : `${count} selected ${partName} batch${count === 1 ? '' : 'es'}`;
    if (!window.confirm(`Delete ${actionText}? This removes saved audit entries only. Verified rounds and the current live series remain unchanged.`)) return;
    setBulkDeletingPhase(phase);
    setDeleteMessage('');
    try {
      const result = await behaviourDelete('/hybrid/high-dominance-2x/bulk-delete',
        deleteAll ? { phase, delete_all: true } : { phase, batch_ids: batchIds });
      const totalsRefreshed = await onDelete?.();
      setSelectedBatchIds(current => ({ ...current, [phase]: deleteAll ? []
        : (current[phase] || []).filter(id => !batchIds.includes(id)) }));
      setHistoryPages({});
      setExpandedParts({});
      setDeleteMessage(`${result.deleted_count} ${partName} batch${result.deleted_count === 1 ? '' : 'es'} deleted.${totalsRefreshed === false || !onDelete ? ' Totals will refresh after the next successful update.' : ' Totals refreshed.'}`);
    } catch (reason) {
      setDeleteMessage(reason?.message || `Could not delete ${partName} batches.`);
    } finally {
      setBulkDeletingPhase('');
    }
  };

  const resetPerformanceHistory = async () => {
    if (!window.confirm('Reset archived Part 1 and Part 2 SHADOW performance history? Any active series, pending eligibility, accepted SHADOW signals, verified Winner Prediction rounds, and source cursors will be preserved. Quarantined or completed stale results will be cleared; DATA_STALE remains if the live processor is still behind.')) return;
    setResettingPerformanceHistory(true);
    setDeleteMessage('');
    try {
      const result = await behaviourDelete('/hybrid/high-series/performance-history', {
        confirm: 'RESET_HIGH_SERIES_PERFORMANCE_HISTORY',
      });
      const totalsRefreshed = await onDelete?.();
      setHistoryPages({});
      setExpandedParts({});
      setSelectedBatchIds({ INITIAL: [], CONTINUATION: [] });
      const activeMessage = result.active_series_preserved
        ? ` Active series remains in progress (${result.active_rounds_preserved} counted rounds and ${result.pending_eligibilities_preserved} pending eligibilities preserved).`
        : result.baseline_status === 'DATA_STALE_PRESERVED'
          ? ' The stale quarantine remains until a fresh verified live round arrives.'
          : ' No active series remains; new results will start on a fresh HIGH transition.';
      const netMessage = result.active_series_preserved
        ? ` Current net reflects the preserved active series (${result.summary?.net_units ?? 0} units).`
        : ' Hypothetical net is now zero.';
      setDeleteMessage(`SHADOW history reset (${result.legacy_batches_cleared} saved batches, ${result.archived_series_cleared} archived series, and ${result.series_rounds_cleared || 0} archived or quarantined rounds cleared). Verified source rounds and accepted SHADOW signals were preserved.${activeMessage}${netMessage}${totalsRefreshed === false || !onDelete ? ' Totals will refresh after the next successful update.' : ''}`);
    } catch (reason) {
      setDeleteMessage(reason?.message || 'Could not reset SHADOW performance history.');
    } finally {
      setResettingPerformanceHistory(false);
    }
  };

  const loadHistory = async (phase, reset = false) => {
    const loaded = reset ? [] : (historyPages[phase]?.items || []);
    setHistoryLoading(current => ({ ...current, [phase]: true }));
    setHistoryErrors(current => ({ ...current, [phase]: '' }));
    try {
      const page = await behaviourGet(`/hybrid/high-dominance-2x/history?phase=${phase}&offset=${loaded.length}&limit=50`);
      setHistoryPages(current => ({ ...current, [phase]: {
        items: [...loaded, ...(page.items || [])], total: page.total || 0,
      } }));
    } catch (reason) {
      setHistoryErrors(current => ({ ...current, [phase]: reason?.message || 'Could not load batch history.' }));
    } finally {
      setHistoryLoading(current => ({ ...current, [phase]: false }));
    }
  };

  const renderBatchPanel = ({ title, subtitle, batch, accent, emptyText, trailing }) => {
    const outcomes = batch?.outcomes || [];
    const progress = batch?.round_progress?.observed ?? outcomes.length;
    const required = audit.batch_size || 5;
    const isContinuation = batch?.phase === 'CONTINUATION';
    return <div className={`rounded-xl border ${accent.border} ${accent.bg} p-3.5`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><b className="text-sm text-zinc-100">{title}</b><p className="mt-0.5 text-[11px] leading-4 text-zinc-400">{subtitle}</p></div>
        {batch && <StatusBadge value={batch.status} />}
      </div>
      {batch ? <>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="text-zinc-300">Series anchor #{batch.series_anchor_position} · Batch #{batch.batch_number} · trigger horizon <b className={accent.text}>{batch.trigger_horizon || triggerHorizon || '—'} rounds</b></span>
          <span className="text-zinc-500">Decision #{batch.decision_round_id || batch.decision_position}</span>
        </div>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-black/40"><div className={`h-full rounded-full ${accent.bar} transition-[width]`} style={{ width: `${Math.min(100, (progress / required) * 100)}%` }} /></div>
        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-400"><span>{progress}/{required} verified rounds</span><span><b className="text-emerald-300">{batch.wins ?? 0} WIN</b> · <b className="text-rose-300">{batch.losses ?? 0} LOSS</b> · <b className={Number(batch.net_units) > 0 ? 'text-emerald-300' : Number(batch.net_units) < 0 ? 'text-rose-300' : 'text-zinc-300'}>{Number(batch.net_units) > 0 ? `+${number(batch.net_units)}u net profit` : Number(batch.net_units) < 0 ? `−${number(Math.abs(Number(batch.net_units)))}u net loss` : '0.00u break even'}</b></span></div>
        <div className="mt-2.5 grid grid-cols-5 gap-1.5">
          {Array.from({ length: required }, (_, index) => {
            const outcome = outcomes[index];
            return <div key={outcome?.identity_key || index} title={outcome ? `Round ${outcome.round_id || outcome.cohort_position}: ${outcome.multiplier}x ${outcome.result_at_2x}` : 'Awaiting verified round'}
              className={`flex min-h-[48px] flex-col items-center justify-center rounded-lg border text-center ${outcome?.result_at_2x === 'WIN' ? 'border-emerald-500/30 bg-emerald-400/10' : outcome?.result_at_2x === 'LOSS' ? 'border-rose-500/30 bg-rose-400/10' : 'border-zinc-800 bg-black/20'}`}>
              <span className="text-[10px] text-zinc-500">R{index + 1}</span>
              <span className={`text-[11px] font-bold ${outcome?.result_at_2x === 'WIN' ? 'text-emerald-300' : outcome?.result_at_2x === 'LOSS' ? 'text-rose-300' : 'text-zinc-600'}`}>{outcome ? `${outcome.multiplier}x` : '—'}</span>
            </div>;
          })}
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] text-zinc-500"><span>{isContinuation ? `Rolling state: ${active === batch ? triggerState || 'UPDATING' : batch.state_at_end || '—'}` : `State after initial check: ${batch.state_at_end || 'COLLECTING'}`}</span>{trailing}</div>
        {batch.coverage_issue && <p className="mt-2 rounded-md bg-amber-400/10 px-2 py-1.5 text-[11px] text-amber-200">Stopped: {batch.coverage_issue.replaceAll('_', ' ')}</p>}
      </> : <p className="mt-3 text-xs leading-5 text-zinc-400">{emptyText}</p>}
    </div>;
  };

  const renderPreviousBatch = batch => {
    const loadedHistory = Object.values(historyPages).flatMap(page => page?.items || []);
    const hasContinuation = batch.phase === 'INITIAL' && [...previousBatches, ...loadedHistory].some(other =>
      other.series_anchor_position === batch.series_anchor_position && other.phase === 'CONTINUATION');
    const protectedBatch = batch.status === 'COLLECTING' || hasContinuation;
    const selected = (selectedBatchIds[batch.phase] || []).includes(batch.batch_id);
    return <div key={batch.batch_id} className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2"><label className="flex items-start gap-2 text-xs font-semibold text-zinc-200"><input type="checkbox" className="mt-0.5 accent-emerald-400" aria-label={`Select ${batch.phase === 'INITIAL' ? 'Part 1' : 'Part 2'} batch ${batch.batch_number} at anchor ${batch.series_anchor_position}`} checked={selected} disabled={protectedBatch || Boolean(bulkDeletingPhase)} onChange={event => setBatchSelected(batch.phase, batch.batch_id, event.target.checked)} /><span>Series anchor #{batch.series_anchor_position} · Batch #{batch.batch_number} · Decision #{batch.decision_round_id || batch.decision_position} · {batch.trigger_horizon || '—'}-round source</span></label><StatusBadge value={batch.status} /></div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-zinc-400"><span>{batch.round_progress?.observed || 0}/5 rounds</span><span className="text-emerald-300">{batch.wins} WIN</span><span className="text-rose-300">{batch.losses} LOSS</span><span className={Number(batch.net_units) > 0 ? 'text-emerald-300' : Number(batch.net_units) < 0 ? 'text-rose-300' : 'text-zinc-300'}>{Number(batch.net_units) > 0 ? `+${number(batch.net_units)}u net profit` : Number(batch.net_units) < 0 ? `−${number(Math.abs(Number(batch.net_units)))}u net loss` : '0.00u break even'}</span></div>
      <button type="button" title={hasContinuation ? 'Delete Part 2 continuation batches first.' : undefined} onClick={() => void deleteBatch(batch)} disabled={deletingBatchId === batch.batch_id || protectedBatch} className="mt-2 rounded border border-rose-800/70 px-2.5 py-1 text-[11px] font-semibold text-rose-200 hover:bg-rose-950/50 disabled:cursor-not-allowed disabled:opacity-40">{deletingBatchId === batch.batch_id ? 'Deleting…' : batch.status === 'COLLECTING' ? 'Live batch' : hasContinuation ? 'Delete Part 2 first' : 'Delete batch'}</button>
    </div>;
  };

  const renderPreviousPart = (title, items, phase, tone) => {
    const aggregate = audit.phase_summaries?.[phase] || {};
    const count = aggregate.batch_count ?? items.length;
    const page = historyPages[phase];
    const expanded = Boolean(expandedParts[phase]);
    const shown = expanded ? (page?.items || items) : items.slice(0, 5);
    const selectedIds = selectedBatchIds[phase] || [];
    const continuationCount = Number(audit.phase_summaries?.CONTINUATION?.batch_count) || 0;
    const initialDeleteBlocked = phase === 'INITIAL' && continuationCount > 0;
    const wins = aggregate.wins ?? items.reduce((sum, batch) => sum + Number(batch.wins || 0), 0);
    const losses = aggregate.losses ?? items.reduce((sum, batch) => sum + Number(batch.losses || 0), 0);
    const net = Number(aggregate.net_units ?? (wins - losses));
    const netLabel = net > 0 ? `+${number(net)}u net profit` : net < 0 ? `−${number(Math.abs(net))}u net loss` : '0.00u break even';
    return <section className={`rounded-lg border ${tone} bg-zinc-900/50 p-3`}>
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2"><div>
        <h4 className="text-xs font-bold uppercase tracking-wide text-zinc-200">{title} ({count})</h4>
        <p className="mt-1 text-[11px] text-zinc-400"><b className="text-emerald-300">{wins} WIN</b> · <b className="text-rose-300">{losses} LOSS</b> · <b className={net > 0 ? 'text-emerald-300' : net < 0 ? 'text-rose-300' : 'text-zinc-300'}>{netLabel}</b></p>
      </div><div className="flex flex-wrap items-center gap-1.5"><span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">{count} batches</span>
        <button type="button" onClick={() => shown.forEach(batch => {
          const loadedHistory = Object.values(historyPages).flatMap(value => value?.items || []);
          const hasPart2 = phase === 'INITIAL' && [...previousBatches, ...loadedHistory].some(other => other.series_anchor_position === batch.series_anchor_position && other.phase === 'CONTINUATION');
          if (batch.status !== 'COLLECTING' && !hasPart2) setBatchSelected(phase, batch.batch_id, true);
        })} disabled={!shown.some(batch => batch.status !== 'COLLECTING') || Boolean(bulkDeletingPhase)} className="rounded border border-zinc-700 px-2 py-1 text-[10px] font-semibold text-zinc-200 hover:bg-zinc-800 disabled:opacity-40">Select shown</button>
        <button type="button" onClick={() => setSelectedBatchIds(current => ({ ...current, [phase]: [] }))} disabled={!selectedIds.length || Boolean(bulkDeletingPhase)} className="rounded border border-zinc-700 px-2 py-1 text-[10px] font-semibold text-zinc-300 hover:bg-zinc-800 disabled:opacity-40">Clear</button>
        <button type="button" onClick={() => void deleteBatches(phase, { batchIds: selectedIds })} disabled={!selectedIds.length || Boolean(bulkDeletingPhase)} className="rounded border border-rose-800/70 px-2 py-1 text-[10px] font-semibold text-rose-200 hover:bg-rose-950/50 disabled:opacity-40">{bulkDeletingPhase === phase ? 'Deleting…' : `Delete selected (${selectedIds.length})`}</button>
        <button type="button" title={initialDeleteBlocked ? 'Delete historical Part 2 batches first.' : undefined} onClick={() => void deleteBatches(phase, { deleteAll: true })} disabled={!count || Boolean(bulkDeletingPhase) || initialDeleteBlocked} className="rounded border border-rose-800/70 px-2 py-1 text-[10px] font-semibold text-rose-200 hover:bg-rose-950/50 disabled:cursor-not-allowed disabled:opacity-40">Delete all</button>
      </div></div>
      {initialDeleteBlocked && <p className="mb-2 text-[10px] text-amber-300">Delete Part 2 batches first; Part 1 records are linked to those continuation records.</p>}
      {shown.length ? <div className="space-y-2">{shown.map(renderPreviousBatch)}</div> : <p className="text-[11px] text-zinc-500">No batches in this part yet.</p>}
      {count > 0 && (count > 5 || count > items.length) && <div className="mt-2 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => {
          if (expanded) setExpandedParts(current => ({ ...current, [phase]: false }));
          else { setExpandedParts(current => ({ ...current, [phase]: true })); if (!page) void loadHistory(phase, true); }
        }} className="rounded border border-zinc-700 px-2.5 py-1 text-[11px] font-semibold text-sky-200 hover:bg-zinc-800">{expanded ? 'Show last 5' : `View all ${count}`}</button>
        {expanded && page && page.items.length < page.total && <button type="button" disabled={historyLoading[phase]} onClick={() => void loadHistory(phase)} className="rounded border border-zinc-700 px-2.5 py-1 text-[11px] text-zinc-200 disabled:opacity-50">{historyLoading[phase] ? 'Loading…' : `Load more (${page.total - page.items.length})`}</button>}
        {historyLoading[phase] && !page && <span className="text-[11px] text-zinc-400">Loading saved batches…</span>}
        {historyErrors[phase] && <span role="alert" className="text-[11px] text-rose-300">{historyErrors[phase]}</span>}
      </div>}
    </section>;
  };

  return <section className="rounded-2xl border border-emerald-500/20 bg-gradient-to-br from-[#071512] via-zinc-900 to-[#090d12] p-4 shadow-xl shadow-black/20 ring-1 ring-white/[0.025]">
    <HighSeriesMachinePanel machine={machine} roundSync={roundSync} refresh={onDelete} />
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-300">HIGH DOMINANT only</p>
        <h3 className="mt-1 text-lg font-semibold text-zinc-100">Rolling HIGH · verified results</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-zinc-400">Historical results come from saved batches. The latest verified rolling sample is shown separately, even while a quarantined strategy machine blocks new eligibility.</p></div>
      <div className="flex items-center gap-2"><StatusBadge value={machineState} />
        <button type="button" onClick={() => void resetPerformanceHistory()} disabled={resettingPerformanceHistory} title="Clear completed and quarantined history. An active series, pending eligibility, accepted signals, verified rounds, and source cursors stay unchanged." className="rounded-lg border border-rose-800/70 px-2.5 py-1.5 text-[10px] font-semibold text-rose-200 hover:bg-rose-950/50 disabled:cursor-not-allowed disabled:opacity-40">{resettingPerformanceHistory ? 'Resetting…' : 'Reset performance history'}</button>
      </div>
    </div>
    <div className="mt-3 grid gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <HighMetric label="Source horizon" value={triggerHorizon ? `${triggerHorizon} rounds` : '—'} detail={`${roundSyncCurrent ? 'Latest rolling state' : 'Waiting for source sync'} · ${triggerState || roundSync.status || 'UNKNOWN'}${roundSync.latest_verified_round_id ? ` · as of #${roundSync.latest_source_sequence ?? '—'} ${roundSync.latest_verified_round_id}` : ''}`} tone={triggerState === 'HIGH_DOMINANT' ? 'emerald' : triggerState === 'LOW_DOMINANT' ? 'rose' : 'sky'} />
      <HighMetric label="Latest verified rolling sample" value={`${currentVerifiedWins} WIN / ${currentVerifiedLosses} LOSS`} detail={`${currentVerifiedWindow.length}/5 committed rounds · separate from saved batches and SHADOW settlements`} tone="sky" />
      <HighMetric label="Recorded 2x WIN" value={summary.wins ?? 0} detail="Historical verified rounds · multiplier ≥ 2.00x" tone="emerald" />
      <HighMetric label="Recorded LOSS" value={summary.losses ?? 0} detail="Historical verified rounds · multiplier < 2.00x" tone="rose" />
      <HighMetric label="Historical win rate" value={percent(summary.win_rate)} detail={`${summary.observed_rounds ?? 0} unique verified rounds · all saved batches`} tone="sky" />
      <HighMetric label="Five-counted-round batches" value={summary.batch_count ?? 0} detail={`${summary.part2_window_count ?? 0} Part 2 windows · ${currentWindowDetail}`} tone="violet" />
      <HighMetric label="Hypothetical net" value={netText} detail="Deduplicated rounds · fixed 2.00x · 1 unit per round" tone={netUnits < 0 ? 'rose' : 'emerald'} valueClass={netTone} />
    </div>
    <section className="rounded-xl border border-sky-400/20 bg-sky-950/10 p-3" aria-label="Latest verified rolling rounds">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <b className="text-xs text-sky-100">Latest 5 verified rounds used by the rolling sample</b>
        <span className="text-[10px] text-zinc-400">Source: {audit.current_verified_window_source || 'ANALYSIS_COHORT'} · latest: {currentVerifiedWindow.at(-1)?.round_id || 'waiting'}</span>
      </div>
      {audit.current_verified_window_source === 'SYNCED_ANALYSIS_COHORT' && <p className="mt-2 text-[10px] text-zinc-400">Analysis cohort is synced to the latest live verified identity.</p>}
      {audit.current_verified_window_source === 'WAITING_FOR_LIVE_SYNC' && <p className="mt-2 text-[10px] text-amber-200">Analysis has not caught up to the latest live identity; stale rounds are hidden until sync completes.</p>}
      <div className="mt-2 grid gap-1.5 sm:grid-cols-5">{currentVerifiedWindow.map((item, index) => <div key={item.identity_key || item.round_id} className="min-w-0 rounded-lg border border-zinc-800 bg-black/20 px-2.5 py-2 text-[10px]">
        <div className="flex items-center justify-between gap-2"><span className="text-zinc-500">R{index + 1} · #{item.cohort_position}</span><b className={item.result_at_2x === 'WIN' ? 'text-emerald-300' : 'text-rose-300'}>{item.result_at_2x}</b></div>
        <div className="mt-1 truncate font-mono text-zinc-200" title={item.round_id}>{item.round_id || 'ID unavailable'}</div>
        <div className="mt-1 text-zinc-400">{number(item.multiplier, 2)}x · {item.outcome}</div>
      </div>)}</div>
    </section>
    <div className="mt-3 grid gap-3 xl:grid-cols-2">
      {renderBatchPanel({ title: 'Part 1 · Initial five rounds', subtitle: 'A fresh qualifying HIGH_DOMINANT emergence locks the decision. Part 1 excludes that decision round and counts exactly the next five verified rounds.', batch: initial,
        accent: { border: 'border-sky-500/25', bg: 'bg-sky-950/20', text: 'text-sky-300', bar: 'bg-sky-400' },
        emptyText: triggerState === 'HIGH_DOMINANT' && machineState === 'ARMED_FOR_HIGH'
          ? 'HIGH_DOMINANT is already active. Wait for a fresh qualifying HIGH emergence; no Part 1 round is counted yet.'
          : 'When the rolling trigger horizon is HIGH_DOMINANT and the series is armed, this section counts the next five verified rounds.' })}
      {renderBatchPanel({ title: 'Part 2 · Continue while HIGH', subtitle: `Each Part 2 window collects five rounds. If its source horizon turns LOW first, that window pauses and resumes from its current count when the same horizon returns HIGH. After a window completes, Part 2 opens another five-round window while the horizon remains HIGH.`, batch: continuation,
        accent: { border: 'border-emerald-500/25', bg: 'bg-emerald-950/20', text: 'text-emerald-300', bar: 'bg-emerald-400' },
        emptyText: initialComplete ? (initial.state_at_end === 'HIGH_DOMINANT'
          ? 'The initial five are complete; waiting for the next verified round to open the continuation.'
          : `Continuation did not start because the ${initial.trigger_horizon || triggerHorizon || 'source'}-round horizon was ${initial.state_at_end || 'not HIGH DOMINANT'} after the initial five.`)
          : initial && Number(initial.round_progress?.observed || 0) < 5
            ? 'Part 1 is collecting. Part 2 eligibility will be evaluated after R5.'
            : 'Continuation begins only after Part 1 completes while its source horizon remains HIGH DOMINANT.',
        trailing: audit.continuation_waiting_for_resume
          ? continuation?.status === 'COMPLETED'
            ? `Last Part 2 window completed ${audit.continuation_rounds ?? 5}/${audit.continuation_window_rounds ?? 5}; waiting for the same horizon to return HIGH for the next window`
            : `Waiting for the ${triggerHorizon || continuation?.trigger_horizon}-round horizon to return HIGH · current window ${audit.continuation_rounds ?? 0}/${audit.continuation_window_rounds ?? 5} rounds`
          : continuation?.status === 'STOPPED_LOW_DOMINANT' ? 'Paused on LOW DOMINANT'
            : continuation?.status === 'STOPPED_STATE_EXIT' ? 'Stopped when HIGH ended' : null })}
    </div>
    {deleteMessage && <p role="status" className="mt-2 text-xs text-zinc-300">{deleteMessage}</p>}
    {(audit.history_count > 0 || previousBatches.length > 0) && <details open className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-xs font-semibold text-zinc-300">Previous five-round batches ({audit.history_count ?? previousBatches.length}) · newest first</summary>
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        {renderPreviousPart('Part 1 · Initial five-round batches', previousInitialBatches, 'INITIAL', 'border-sky-500/25')}
        {renderPreviousPart('Part 2 · Continuation batches', previousContinuationBatches, 'CONTINUATION', 'border-emerald-500/25')}
      </div>
      <p className="mt-2 text-[10px] text-zinc-500">Choose individual saved batches or use Delete all within each Part. Delete Part 2 first when clearing both parts. Current/live series and verified rounds are protected.</p>
    </details>}
    <p className="mt-3 text-[11px] text-zinc-500">Exactly 2.00x counts as WIN. Net is hypothetical: one unit staked at a fixed 2.00x target gives +1 unit for WIN and −1 for LOSS; it is not real money or a bet. This prospective audit does not change SHADOW or ADIE decisions.</p>
  </section>;
}

function HighDominanceStateTestCard({ audit = {}, rolling = {}, onDelete }) {
  const [deleting, setDeleting] = useState('');
  const [deleteMessage, setDeleteMessage] = useState('');
  const active = audit.active_episode;
  const episodes = audit.episodes || [];
  const earlierCount = Math.max(0, Number(audit.summary?.episode_count ?? episodes.length) - 1);
  const deleteEpisodes = async (item = null) => {
    const label = item ? `HIGH episode for decision round #${item.decision_round_id || item.decision_position}`
      : `all ${earlierCount} earlier HIGH episodes`;
    if (!window.confirm(`Delete ${label}? Saved episode outcomes and audit totals will be removed. Verified game rounds remain unchanged.`)) return;
    setDeleting(item?.episode_id || 'all');
    setDeleteMessage('');
    try {
      const result = await behaviourDelete('/hybrid/high-dominance-state-test/earlier',
        item ? { episode_ids: [item.episode_id] } : { delete_all: true });
      const refreshed = await onDelete?.();
      setDeleteMessage(refreshed === false || !onDelete
        ? `${result.deleted_count} episode(s) deleted. Totals will update after the next successful refresh.`
        : `${result.deleted_count} episode(s) deleted. Totals refreshed.`);
    } catch (reason) {
      setDeleteMessage(reason?.message || 'Could not delete the HIGH episode.');
    } finally {
      setDeleting('');
    }
  };
  const latest = episodes[0];
  const episode = active || latest;
  const summary = audit.summary || {};
  const checkpoints = episode?.checkpoints || {};
  const progress = episode?.round_progress?.observed || 0;
  const totalNet = Number(summary.net_units) || 0;
  const netTone = totalNet > 0 ? 'text-emerald-300' : totalNet < 0 ? 'text-rose-300' : 'text-zinc-300';
  return <section className="rounded-xl border border-violet-500/25 bg-gradient-to-br from-violet-950/20 to-zinc-900/80 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-300">Confirmed-state test · HIGH only</p>
        <h3 className="mt-1 text-lg font-semibold text-zinc-100">HIGH DOMINANT · 5 / 10 / 30 / 50 verified rounds</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-zinc-400">An episode opens on a confirmed HIGH DOMINANT transition. Its decision round is an anchor; scoring starts on the next verified round and ends when HIGH exits or the 50-round checkpoint completes.</p></div>
      <div className="flex flex-wrap items-center gap-2"><StatusBadge value={rolling.confirmed_state || audit.current_confirmed_state || 'INSUFFICIENT_DATA'} />
        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[10px] text-zinc-300">{rolling.confirmed_momentum || '—'} · {rolling.strength || '—'}</span>
        <StatusBadge value={audit.status || 'WAITING_FOR_CONFIRMED_HIGH'} />
      </div>
    </div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
      <Metric label="Episodes" value={summary.episode_count ?? 0} detail={active ? 'One episode collecting' : 'Waiting for confirmed HIGH'} />
      <Metric label="2x WIN" value={summary.wins ?? 0} detail="Multiplier ≥ 2.00x" />
      <Metric label="2x LOSS" value={summary.losses ?? 0} detail="Multiplier < 2.00x" />
      <Metric label="EXACT outcomes" value={summary.exact_count ?? 0} detail={`${summary.observed_rounds ?? 0} scored rounds`} />
      <Metric label="Hypothetical net" value={`${totalNet > 0 ? '+' : ''}${number(totalNet)}u`} detail="1 unit per round at fixed 2x" valueClass={netTone} />
    </div>
    <div className="mt-3 rounded-xl border border-violet-500/20 bg-black/20 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><b className="text-sm text-zinc-100">{active ? 'Active HIGH episode' : episode ? 'Latest HIGH episode' : 'No confirmed HIGH episode yet'}</b>
        {episode && <p className="mt-0.5 text-[11px] text-zinc-400">Decision round #{episode.decision_round_id || episode.decision_position} · {progress}/50 scored rounds · opened {timestamp(episode.opened_at)}</p>}</div>
        {episode && <StatusBadge value={episode.status} />}
      </div>
      {episode && <>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-black/40"><div className="h-full rounded-full bg-violet-400 transition-[width]" style={{ width: `${Math.min(100, progress / 50 * 100)}%` }} /></div>
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
          {[5, 10, 30, 50].map(size => {
            const checkpoint = checkpoints[String(size)] || { status: 'COLLECTING', remaining: Math.max(0, size - progress) };
            const reached = checkpoint.status === 'REACHED';
            const net = Number(checkpoint.net_units) || 0;
            return <div key={size} className={`rounded-lg border p-2.5 ${reached ? 'border-violet-400/30 bg-violet-400/[0.07]' : 'border-zinc-800 bg-zinc-950/50'}`}>
              <div className="flex items-center justify-between gap-2"><b className="text-xs text-zinc-200">{size} rounds</b><span className="text-[10px] text-zinc-400">{reached ? 'REACHED' : checkpoint.status === 'NOT_REACHED' ? 'NOT REACHED' : `${checkpoint.remaining ?? 0} left`}</span></div>
              {reached ? <p className="mt-1.5 text-[11px]"><span className="text-emerald-300">{checkpoint.wins} WIN</span> · <span className="text-rose-300">{checkpoint.losses} LOSS</span><br /><b className={net > 0 ? 'text-emerald-300' : net < 0 ? 'text-rose-300' : 'text-zinc-300'}>{net > 0 ? '+' : ''}{number(net)}u net</b> · {percent(checkpoint.win_rate)}</p>
                : <p className="mt-1.5 text-[10px] leading-4 text-zinc-500">{checkpoint.reason === 'STOPPED_STATE_EXIT' ? `Stopped when state exited to ${episode.exit_state || 'another state'}.` : active ? 'Collecting while confirmed HIGH persists.' : 'Episode ended before this checkpoint.'}</p>}
            </div>;
          })}
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-400"><span><b className="text-emerald-300">{episode.wins ?? 0} WIN</b> · <b className="text-rose-300">{episode.losses ?? 0} LOSS</b> · {episode.exact_count ?? 0} EXACT</span><span className={Number(episode.net_units) > 0 ? 'text-emerald-300' : Number(episode.net_units) < 0 ? 'text-rose-300' : 'text-zinc-300'}>{Number(episode.net_units) > 0 ? '+' : ''}{number(episode.net_units)}u net</span></div>
      </>}
    </div>
    {episodes.length > 1 && <details className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-xs font-semibold text-zinc-300">Earlier HIGH episodes ({earlierCount}) · newest first</summary>
      <div className="mt-3 flex justify-end"><button type="button" onClick={() => void deleteEpisodes()} disabled={Boolean(deleting)} className="rounded border border-rose-800/70 px-3 py-1.5 text-xs font-semibold text-rose-200 hover:bg-rose-950/50 disabled:opacity-40">{deleting === 'all' ? 'Deleting…' : `Delete all earlier (${earlierCount})`}</button></div>
      <div className="mt-2 space-y-2">{episodes.slice(1).map(item => <div key={item.episode_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-black/20 p-2.5 text-[11px]">
        <span>Decision #{item.decision_round_id || item.decision_position} · {item.round_progress?.observed || 0}/50 · {item.wins} WIN / {item.losses} LOSS</span><span className="flex items-center gap-2"><b className={Number(item.net_units) > 0 ? 'text-emerald-300' : Number(item.net_units) < 0 ? 'text-rose-300' : 'text-zinc-300'}>{Number(item.net_units) > 0 ? '+' : ''}{number(item.net_units)}u</b><StatusBadge value={item.status} /><button type="button" onClick={() => void deleteEpisodes(item)} disabled={Boolean(deleting) || item.status === 'COLLECTING'} className="rounded border border-rose-800/70 px-2.5 py-1 text-[11px] font-semibold text-rose-200 hover:bg-rose-950/50 disabled:opacity-40">{deleting === item.episode_id ? 'Deleting…' : 'Delete'}</button></span>
      </div>)}</div>
    </details>}
    {deleteMessage && <p role="status" className="mt-2 text-xs text-amber-200">{deleteMessage}</p>}
    <p className="mt-2 text-[10px] leading-4 text-zinc-500">Descriptive prospective test, not a signal. For this 2.00x audit target, exactly 2.00x counts as WIN; dominance classification itself treats HIGH as strictly above 2.00x. Net units are hypothetical.</p>
  </section>;
}

function HybridOverview({ compact = false }) {
  const { data, error, refresh, liveConnection, latestVerifiedEvent, frontendReceivedAt, frontendRenderedAt, updatedAt } = useBehaviourData('/hybrid', 15000, !compact);
  const [liveStatus, setLiveStatus] = useState(null);
  const [liveStatusError, setLiveStatusError] = useState('');
  const [settings, setSettings] = useState(() => getNotificationSettings());
  const [notificationHistory, setNotificationHistory] = useState(() => getNotificationHistory().slice(0, 6));
  const [notice, setNotice] = useState('');
  const [audioStatus, setAudioStatus] = useState(() => getNotificationAudioStatus() === 'running'
    ? 'Audio engine is running in this tab.' : 'Audio needs a user gesture in this tab.');
  const [profiles, setProfiles] = useState(() => Object.fromEntries(
    Object.keys(NOTIFICATION_EVENT_DEFINITIONS).filter(key => key.startsWith('behaviour.'))
      .map(key => [key, getNotificationSettings().events[key]?.sound || 'discovery'])));
  const [eventVolumes, setEventVolumes] = useState(() => Object.fromEntries(
    Object.keys(NOTIFICATION_EVENT_DEFINITIONS).filter(key => key.startsWith('behaviour.'))
      .map(key => [key, getNotificationSettings().events[key]?.volume ?? getNotificationSettings().volume])));
  const [profileStatus, setProfileStatus] = useState({});
  const [verifiedProfiles, setVerifiedProfiles] = useState(() => {
    try {
      const latest = new Map();
      for (const item of JSON.parse(localStorage.getItem('winner_predict_audio_audibility_tests') || '[]'))
        if (item.kind === 'sound' && item.verificationKey && !latest.has(item.verificationKey)) latest.set(item.verificationKey, item.result);
      return new Set([...latest].filter(([, result]) => result === 'YES').map(([key]) => key));
    }
    catch { return new Set(); }
  });
  const [voices, setVoices] = useState(() => listVoices());
  const [voiceStatus, setVoiceStatus] = useState('');
  const [voiceSample, setVoiceSample] = useState('High dominance rising.');
  const [activeVoiceVerificationKey, setActiveVoiceVerificationKey] = useState('');
  const [testEvent, setTestEvent] = useState('behaviour.high_rising');
  const [testMessage, setTestMessage] = useState('High dominance rising.');
  const [testSound, setTestSound] = useState(true);
  const [testVoice, setTestVoice] = useState(false);
  const [testVolume, setTestVolume] = useState(() => getNotificationSettings().volume);
  const [testStatus, setTestStatus] = useState('');
  const [repeatStopped, setRepeatStopped] = useState(false);
  const previous = useRef(null);
  const lastLiveEventId = useRef(null);
  const lastSeriesEventKey = useRef(null);
  const completionTimers = useRef(null);

  useEffect(() => {
    if (compact) return undefined;
    let alive = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await behaviourGet('/live-status');
        if (alive) { setLiveStatus(value); setLiveStatusError(''); }
      } catch (reason) {
        if (alive) setLiveStatusError(reason?.message || 'Live status request failed');
      } finally {
        inFlight = false;
      }
    };
    load();
    const timer = window.setInterval(load, 15000);
    const onFocus = () => { if (document.visibilityState === 'visible') load(); };
    document.addEventListener('visibilitychange', onFocus);
    return () => { alive = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', onFocus); };
  }, [compact]);

  const persist = useCallback((next, success = 'Notification preferences saved.') => {
    try {
      const saved = saveNotificationSettings(next);
      setSettings(saved); setNotice(success);
      if (saved.enabled && !saved.muted && saved.voiceEnabled && saved.mode !== 'sound-only') enableNotificationVoice();
      if (saved.enabled && !saved.muted && saved.mode !== 'voice-only') {
        void enableNotificationAudio().then(result => setAudioStatus(result.message));
      }
      return true;
    }
    catch (reason) { setNotice(reason?.message || 'Could not save notification preferences.'); return false; }
  }, []);

  useEffect(() => subscribeNotifications((item) => {
    if (item.kind === 'settings') setSettings(item.settings);
    if (item.kind === 'audio-state') setAudioStatus(item.message || `Audio engine ${item.status}.`);
    if (item.kind === 'audio-blocked') setAudioStatus(item.result?.message || 'Browser audio is blocked.');
    if (item.kind === 'playback-result' && item.result?.status === 'blocked') setAudioStatus(item.result.message || 'Browser audio is blocked.');
    if (item.kind === 'voice') setVoiceStatus(item.status === 'started' ? 'Speaking…' : item.status === 'ended' ? 'Voice preview ended.' : item.message || 'Voice playback failed.');
    if (item.kind === 'event' || item.kind === 'playback-result') setNotificationHistory(getNotificationHistory().slice(0, 6));
  }), []);

  useEffect(() => {
    const updateVoices = () => setVoices(listVoices());
    updateVoices();
    window.speechSynthesis?.addEventListener?.('voiceschanged', updateVoices);
    if (window.speechSynthesis && !window.speechSynthesis.addEventListener) window.speechSynthesis.onvoiceschanged = updateVoices;
    return () => {
      window.speechSynthesis?.removeEventListener?.('voiceschanged', updateVoices);
      if (window.speechSynthesis?.onvoiceschanged === updateVoices) window.speechSynthesis.onvoiceschanged = null;
    };
  }, []);

  const cancelCompletion = useCallback(() => {
    if (!completionTimers.current) return;
    window.clearInterval(completionTimers.current.interval);
    window.clearTimeout(completionTimers.current.timeout);
    completionTimers.current = null;
  }, []);

  useEffect(() => {
    if (!data?.available) return;
    const machineEvents = data.high_dominance_2x?.series_machine?.events || [];
    if (lastSeriesEventKey.current === null) {
      const latest = machineEvents.at(-1);
      lastSeriesEventKey.current = latest ? `${latest.type}:${latest.round_identity}:${latest.timestamp}` : '';
    } else {
      const eventTypes = {
        HIGH_EMERGENCE_DETECTED: 'behaviour.high_emergence', NEW_HIGH_EMERGENCE_DETECTED: 'behaviour.high_emergence',
        HIGH_SIGNAL_GENERATED: 'behaviour.high_signal_generated',
        SHADOW_SIGNAL_ACCEPTED: 'behaviour.shadow_signal_accepted',
        SHADOW_SIGNAL_REJECTED: 'behaviour.shadow_signal_rejected',
        SHADOW_SIGNAL_SETTLED_WIN: 'behaviour.signal_settled_win',
        SHADOW_SIGNAL_SETTLED_LOSS: 'behaviour.signal_settled_loss',
        PART1_STARTED: 'behaviour.part1_started', PART1_COMPLETED: 'behaviour.part1_completed',
        PART2_STARTED: 'behaviour.part2_started', PART2_PAUSED_LOW_DOMINANT: 'behaviour.part2_paused',
        PART2_PAUSED_NON_HIGH: 'behaviour.part2_paused', PART2_RESUMED_HIGH: 'behaviour.part2_resumed',
        STOPPED_PART1_NO_HIGH: 'behaviour.part2_stopped', STOPPED_PAUSE_LIMIT: 'behaviour.part2_stopped',
        STOPPED_TIME_LIMIT: 'behaviour.part2_stopped', COOLDOWN_COMPLETED: 'behaviour.cooldown_completed',
      };
      const previousIndex = machineEvents.findIndex(item =>
        `${item.type}:${item.round_identity}:${item.timestamp}` === lastSeriesEventKey.current);
      const startAt = previousIndex >= 0 ? previousIndex + 1 : machineEvents.length;
      for (const item of machineEvents.slice(startAt)) {
        const key = `${item.type}:${item.round_identity}:${item.timestamp}`;
        const eventType = eventTypes[item.type];
        if (eventType) void publishNotification({ eventType,
          id: `high-series:${data.cohort?.cohort_id}:${key}`,
          deduplicationKey: `high-series:${data.cohort?.cohort_id}:${key}`,
          roundId: item.round_id, message: `${item.type.replaceAll('_', ' ')} at verified round ${item.round_id || item.round_identity}.` });
      }
      if (machineEvents.length) {
        const latest = machineEvents.at(-1);
        lastSeriesEventKey.current = `${latest.type}:${latest.round_identity}:${latest.timestamp}`;
      }
    }
    const coverageStatus = data.coverage?.status || 'COVERAGE_OBSERVED';
    const current = { coverageStatus };
    const prior = previous.current;
    if (prior) {
      const pos = data.cohort?.last_cohort_position || 0;
      if (coverageStatus !== prior.coverageStatus && ['POSSIBLE_MISSING_ROUNDS', 'TIME_ESTIMATED_100_ELAPSED', 'CONFIRMED_SOURCE_GAP'].includes(coverageStatus)) {
        const message = coverageStatus === 'CONFIRMED_SOURCE_GAP'
          ? 'A source round sequence gap is confirmed by authoritative order metadata.'
          : 'Round coverage is uncertain; elapsed time suggests rounds may be missing.';
        void publishNotification({ eventType: 'behaviour.possible_missing', id: `hybrid:coverage:${data.cohort?.cohort_id}:${pos}:${coverageStatus}`, deduplicationKey: `hybrid:coverage:${data.cohort?.cohort_id}:${pos}:${coverageStatus}`, severity: 'warning', message, soundEnabled: false, voiceEnabled: false });
      }
    }
    previous.current = current;
    const liveEvents = data.live_notification_events || [];
    if (lastLiveEventId.current === null) {
      lastLiveEventId.current = Number(liveEvents.at(-1)?.id) || 0;
    } else for (const item of liveEvents) {
      const eventId = Number(item.id) || 0;
      if (eventId <= lastLiveEventId.current) continue;
      lastLiveEventId.current = eventId;
      const event = publishNotification({ eventType: item.event_type,
        id: item.event_id, deduplicationKey: item.event_id,
        roundId: item.round_id,
        direction: item.event_type.includes('high') ? 'HIGH' : item.event_type.includes('low') ? 'LOW' : null,
        message: item.event_type === 'behaviour.window_completed'
          ? `Observed 100-round window ${item.window_number} completed.` : undefined });
      setRepeatStopped(false);
      if (item.event_type === 'behaviour.window_completed') {
        void event.then((result) => {
          const seconds = Math.min(30, Math.max(0, Number(settings.behaviourAlerts?.completionDurationSeconds) || 0));
          if (seconds > 1 && result.event?.playback === 'ended' && settings.enabled && !settings.muted && data.coverage?.data_fresh) {
            cancelCompletion();
            const interval = window.setInterval(() => { void playConfiguredSound('behaviour.window_completed'); }, 1000);
            const timeout = window.setTimeout(() => { cancelCompletion(); }, seconds * 1000);
            completionTimers.current = { interval, timeout };
          }
        });
      }
    }
  }, [data, settings, cancelCompletion]);

  const activeState = data?.rolling?.confirmed_state;
  const activeEvent = activeState === 'HIGH_DOMINANT' ? 'behaviour.high'
    : activeState === 'LOW_DOMINANT' ? 'behaviour.low' : null;
  const feedUsable = Boolean(data?.coverage?.data_fresh)
    && !['POSSIBLE_MISSING_ROUNDS', 'TIME_ESTIMATED_100_ELAPSED', 'CONFIRMED_SOURCE_GAP', 'KNOWN_COLLECTOR_OUTAGE'].includes(data?.coverage?.status)
    && ['HEALTHY', 'CONNECTED', 'COLLECTING'].includes(String(data?.coverage?.collector_state || '').toUpperCase());
  useEffect(() => {
    const repeat = settings.behaviourAlerts || {};
    if (!activeEvent || data?.rolling?.state !== activeState || !feedUsable || !repeat.repeatEnabled
        || !settings.enabled || repeatStopped || settings.muted
        || settings.mode === 'voice-only' || document.visibilityState !== 'visible') return undefined;
    const intervalSeconds = Math.max(10, Math.min(300, Number(repeat.intervalSeconds) || 60));
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void playConfiguredSound(activeEvent);
    }, intervalSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [activeEvent, activeState, data?.rolling?.state, feedUsable, settings, repeatStopped]);
  useEffect(() => {
    if (!settings.enabled || settings.muted || !feedUsable) cancelCompletion();
  }, [settings.enabled, settings.muted, feedUsable, cancelCompletion]);
  useEffect(() => () => { cancelCompletion(); }, [cancelCompletion]);

  const eventTypes = Object.entries(NOTIFICATION_EVENT_DEFINITIONS)
    .filter(([key]) => key.startsWith('behaviour.'));
  const updateProfile = (eventType, profile) => {
    setProfiles(current => ({ ...current, [eventType]: profile }));
    setProfileStatus(current => ({ ...current, [eventType]: null }));
  };
  const previewProfile = async (eventType) => {
    const profile = profiles[eventType];
    const result = await previewSound(profile, Number(eventVolumes[eventType] ?? settings.events[eventType]?.volume ?? settings.volume));
    setProfileStatus(current => ({ ...current, [eventType]: result }));
  };
  const confirmSound = (eventType, heard) => {
    const profile = profiles[eventType];
    const volume = Number(eventVolumes[eventType] ?? settings.events[eventType]?.volume ?? settings.volume);
    const verificationKey = soundVerificationKey(profile, volume);
    recordAudibilityResult('sound', profile, heard ? 'YES' : 'NO', '', volume);
    setVerifiedProfiles(current => {
      const updated = new Set(current);
      if (heard) updated.add(verificationKey); else updated.delete(verificationKey);
      return updated;
    });
    setProfileStatus(current => ({ ...current, [eventType]: { ...current[eventType], confirmation: heard ? 'You confirmed audibility.' : 'You reported that it was not audible.' } }));
  };
  const useSound = (eventType) => {
    const profile = profiles[eventType];
    const current = getNotificationSettings();
    const events = { ...current.events, [eventType]: { ...current.events[eventType], sound: profile, volume: Number(eventVolumes[eventType] ?? current.volume), enabled: true } };
    persist({ ...current, events }, `${SOUND_PROFILES.find(item => item.id === profile)?.name || profile} saved for ${eventType}.`);
  };
  const runSyntheticTest = async () => {
    const selected = profiles[testEvent] || settings.events[testEvent]?.sound || 'discovery';
    if (testSound && settings.mode !== 'voice-only') await enableNotificationAudio();
    if (testVoice) setActiveVoiceVerificationKey(voiceVerificationKey(settings));
    const result = await publishNotification({ eventType: testEvent, id: `test:hybrid:${Date.now()}`,
      deduplicationKey: `test:hybrid:${Date.now()}`, message: testMessage,
      voiceAnnouncement: testMessage, soundProfile: selected, test: true, synthetic: true,
      soundEnabled: testSound, voiceEnabled: testVoice, volume: Number(testVolume),
      voiceMessage: testMessage });
    setTestStatus(`Synthetic only · ${result.event?.playback || result.status}${result.event?.playbackMessage ? ` — ${result.event.playbackMessage}` : ''}`);
  };
  const previewVoice = () => {
    if (!voices.length) { setVoiceStatus('SpeechSynthesis has no available browser voices.'); return; }
    const voice = voices.find(item => item.voiceURI === settings.voiceURI) || voices[0];
    const options = { voiceURI: voice.voiceURI, language: voice.lang,
      rate: settings.rate, pitch: settings.pitch, volume: settings.voiceVolume };
    setActiveVoiceVerificationKey(voiceVerificationKey(options));
    const result = speakText(voiceSample, options);
    setVoiceStatus(result.message || (result.status === 'started' ? 'Speaking…' : result.status));
  };
  const confirmVoice = (heard) => {
    const voice = voices.find(item => item.voiceURI === settings.voiceURI) || voices[0];
    if (!voice) return;
    recordAudibilityResult('voice', 'speech-synthesis', heard ? 'YES' : 'NO', activeVoiceVerificationKey);
    if (heard) persist({ ...settings, voiceURI: voice.voiceURI,
      language: voice.lang, voiceEnabled: true }, `Voice ${voice.name || voice.lang} verified and enabled.`);
    else setNotice('Voice was not confirmed audible. Voice announcements remain unchanged.');
  };
  const coverage = data?.coverage || {};
  const sourceGaps = coverage.confirmed_source_gaps || [];
  const possibleRange = coverage.possible_missing_round_range;
  const staleMinutes = Number.isFinite(Number(coverage.last_observation_age_seconds))
    ? Math.floor(Number(coverage.last_observation_age_seconds) / 60) : null;
  const windowNow = data?.current_window || {};
  const roundsProgress = windowNow.round_progress || { completed: 0, required: 100, fraction: 0 };
  const timeProgress = windowNow.time_progress || {};
  const sourceDetails = data?.collector?.details || {};
  const behaviourRound = coverage.latest_canonical_round || {};
  const winnerIndex = Math.max(Number(sourceDetails.source_latest_round_index) || 0, Number(latestVerifiedEvent?.round_index) || 0);
  const cursorIndex = sourceDetails.sync_cursor_round_index == null ? NaN : Number(sourceDetails.sync_cursor_round_index);
  const workerPosition = data?.analysis_worker?.details?.last_position == null ? NaN : Number(data.analysis_worker.details.last_position);
  const canonicalPosition = behaviourRound.analysis_position == null ? NaN : Number(behaviourRound.analysis_position);
  const sourceBacklog = Number.isFinite(cursorIndex) ? Math.max(0, winnerIndex - cursorIndex) : null;
  const analysisBacklog = Number.isFinite(workerPosition) && Number.isFinite(canonicalPosition)
    ? Math.max(0, canonicalPosition - workerPosition) : null;
  const winnerRoundId = winnerIndex === Number(latestVerifiedEvent?.round_index)
    ? latestVerifiedEvent?.round_id : sourceDetails.source_latest_round_id;
  const matchingRenderedRound = latestVerifiedEvent?.round_id
    && data?.cohort?.last_identity_key === `winner-predict:${latestVerifiedEvent.round_id}`;

  if (compact) return null;

  return <section className={`${panel} space-y-4 border-sky-500/20 bg-gradient-to-br from-sky-950/15 to-zinc-900/80`}>
    <section className="rounded-xl border border-emerald-500/20 bg-black/20 p-3 text-xs text-zinc-300" aria-label="Live decision processor status">
      {liveStatusError && <div role="alert" className="mb-3 rounded border border-amber-700/60 bg-amber-950/30 p-2 text-amber-200">Live status request failed: {liveStatusError}</div>}
      <div className="flex flex-wrap items-center justify-between gap-2"><b className="text-sm text-white">Live decision path</b>
        <div className="flex flex-wrap gap-2"><StatusBadge value={liveStatus?.processor_state || 'LOADING'} />
          <StatusBadge value={`DATABASE ${liveStatus?.database_status || 'CHECKING'}`} /></div></div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div>Latest live verified round<br /><b className="break-all text-emerald-200">{liveStatus?.processor?.latest_live_round_uid || 'WAITING_FOR_LIVE'}</b>
          <div className="mt-1 text-zinc-500">{timestamp(liveStatus?.processor?.latest_live_processed_at)}</div></div>
        <div>Strategy state<br /><b className="text-zinc-100">{liveStatus?.strategy_state || 'UNKNOWN'}</b>
          <div className="mt-1 text-zinc-500">Provenance: {liveStatus?.strategy_provenance || 'UNKNOWN'}</div></div>
        <div>SQLite ingress journal<br /><b className="text-zinc-100">{liveStatus?.sqlite_journal?.state || 'UNKNOWN'}</b>
          <div className="mt-1 text-zinc-500">Backlog: {liveStatus?.sqlite_journal?.pending_records ?? 'UNKNOWN'}</div></div>
        <div>Next signal eligibility<br /><b className="text-zinc-100">{liveStatus?.signal_eligibility || 'WAITING_FOR_LIVE'}</b>
          <div className="mt-1 text-zinc-500">Decision p95: {liveStatus?.processor?.p95_ms == null ? 'UNKNOWN' : `${number(liveStatus.processor.p95_ms, 3)} ms`} · {liveStatus?.processor?.samples ?? 0} samples</div></div>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-zinc-500">
        <span>Latest SHADOW lock: {liveStatus?.latest_signal_lock?.signal_status || liveStatus?.latest_signal_lock?.status || 'NO_SIGNAL_ACCEPTED'}</span>
        <span>Signal ID: {liveStatus?.latest_signal_lock?.signal_id || '—'}</span>
        <span>Lock p95: {liveStatus?.processor?.stages?.rules_engine_acceptance_to_durable_lock_ms?.p95_ms == null
          ? 'UNKNOWN' : `${number(liveStatus.processor.stages.rules_engine_acceptance_to_durable_lock_ms.p95_ms, 3)} ms`}</span>
        <span>Lock time: {timestamp(liveStatus?.latest_signal_lock?.lock_acknowledged_at)}</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-zinc-500"><span>Orbit SSE: {liveStatus?.orbit?.stream_status || liveStatus?.orbit_source || 'UNKNOWN'}</span>
        <span>Collector: {liveStatus?.orbit?.collector_state || 'UNKNOWN'}</span>
        <span>Coverage: {liveStatus?.orbit?.coverage || 'UNKNOWN'}</span>
        <span>Pre-start lock timing: {liveStatus?.pre_start_timing || 'UNVERIFIED'}</span></div>
      <div className="grid gap-2 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-[11px] text-zinc-400 sm:grid-cols-2 xl:grid-cols-5">
        <div>Last Orbit stream event<br /><b className="text-zinc-200">{timestamp(liveStatus?.orbit?.last_stream_event_at)}</b></div>
        <div>Latest Browser observation<br /><b className="text-zinc-200">{liveStatus?.orbit?.latest_browser_observed_multiplier == null ? 'UNKNOWN' : `${number(liveStatus.orbit.latest_browser_observed_multiplier)}x`}</b><br />{timestamp(liveStatus?.orbit?.last_browser_observation_at)}</div>
        <div>Latest collector verified candidate<br /><b className="break-all text-zinc-200">{liveStatus?.orbit?.latest_verified_round_uid || (liveStatus?.orbit?.latest_verified_at ? 'ID_UNAVAILABLE' : 'NONE')}</b><br />{timestamp(liveStatus?.orbit?.latest_verified_at)} · {liveStatus?.orbit?.latest_verified_delivery_mode || (liveStatus?.orbit?.latest_verified_at ? 'UNKNOWN' : 'NO_EVENT')}</div>
        <div>Latest PostgreSQL round<br /><b className="break-all text-zinc-200">{liveStatus?.orbit?.latest_persisted_round_uid || 'UNKNOWN'}</b><br />{timestamp(liveStatus?.orbit?.latest_persisted_at)}</div>
        <div>Integrity<br /><b className="text-amber-200">{liveStatus?.orbit?.integrity_reason || liveStatus?.orbit?.round_status || 'UNKNOWN'}</b><br />Collector backlog: {liveStatus?.orbit?.ingress_backlog ?? 'UNKNOWN'}</div>
      </div>
    </section>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-sky-300">Hybrid dominance intelligence</p>
        <h2 className="mt-1 text-lg font-semibold text-zinc-100">Prospective live windows</h2>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-zinc-400">Versioned descriptive analysis on verified outcomes after cohort activation. Rolling behaviour is not a forecast; elapsed time never completes an observed window.</p></div>
      <div className="flex flex-wrap items-center gap-2"><StatusBadge value={coverage.status || (data ? 'COVERAGE_OBSERVED' : 'LOADING')} />
        <span className="text-[11px] text-zinc-500">{data?.policy_version || 'hybrid-dominance-v1'}</span></div>
    </div>
    {error && <p role="alert" className="rounded-lg border border-amber-700/50 bg-amber-950/30 p-3 text-sm text-amber-200">Hybrid analysis unavailable: {error}</p>}
    {!data?.available ? <p className="text-sm text-zinc-400">Waiting for the prospective cohort migration and first eligible verified round.</p> : <>
      <section className="rounded-xl border border-sky-500/25 bg-sky-950/15 p-3 text-xs text-zinc-300" aria-label="Shared verified round synchronization">
        <div className="flex flex-wrap items-center justify-between gap-2"><b className="text-sm text-white">Shared verified round sync</b><StatusBadge value={liveConnection === 'connected' || liveConnection === 'restored' ? 'CONNECTED' : liveConnection.toUpperCase()} /></div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div>Winner round<br /><b className="break-all text-sky-200">{winnerRoundId || 'UNKNOWN'}</b></div>
          <div>Behaviour round<br /><b className="break-all text-emerald-200">{data.cohort?.last_identity_key || 'WAITING'}</b></div>
          <div>Source backlog<br /><b>{sourceBacklog == null ? 'UNKNOWN' : sourceBacklog}</b> rounds</div>
          <div>Analysis backlog<br /><b>{analysisBacklog == null ? 'UNKNOWN' : analysisBacklog}</b> rounds</div>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-zinc-400">
          <span>Verified delivery lag: {lagLabel(elapsedMs(sourceDetails.last_sync_at, sourceDetails.source_latest_stored_at))}</span>
          <span>Behaviour processing lag: {lagLabel(elapsedMs(data.analysis_worker?.checked_at, sourceDetails.last_sync_at))}</span>
          <span>Frontend update lag: {matchingRenderedRound ? lagLabel(elapsedMs(frontendRenderedAt, frontendReceivedAt)) : 'UNKNOWN'}</span>
          <span>Updated {timestamp(updatedAt)}</span>
        </div>
        <p className="mt-2 text-[11px] text-zinc-500">Lag across machines may include clock skew. UNKNOWN means the matching timestamps were not observed.</p>
      </section>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Metric label="Verified round progress" value={`${roundsProgress.completed}/${roundsProgress.required}`} detail={`Window ${windowNow.window_number || '—'} · ${windowNow.lifecycle_status || 'OPEN'}`} />
        <Metric label="Estimated time progress" value={timeProgress.estimated_rounds == null ? '—' : `${timeProgress.estimated_rounds}/100 est.`} detail={`${timeProgress.status || 'TIME_ESTIMATE_ONLY'} · never confirms rounds`} />
        <Metric label="Dominance state" value={(data.rolling?.confirmed_state || 'INSUFFICIENT_DATA').replaceAll('_', ' ')} detail={`${data.rolling?.confirmed_momentum || 'INSUFFICIENT_DATA'} · ${data.rolling?.strength || '—'}`} />
        <Metric label="Feed freshness" value={coverage.data_fresh ? 'FRESH' : 'UNCERTAIN'} detail={`${number(coverage.last_observation_age_seconds, 0)}s since latest verified observation`} />
        <Metric label="Sound alert" value={settings.muted ? 'MUTED' : activeEvent && settings.behaviourAlerts?.repeatEnabled && feedUsable ? `REPEATING ${activeState === 'HIGH_DOMINANT' ? 'HIGH' : 'LOW'}` : 'READY / IDLE'} detail={settings.enabled ? (feedUsable ? 'Shared notification audio' : 'Stopped until feed is fresh') : 'Master notifications disabled'} />
      </div>
      {coverage.status !== 'COVERAGE_OBSERVED' && <div role="status" className="rounded-lg border border-amber-600/40 bg-amber-950/30 px-3 py-3 text-sm text-amber-100">
        <b>Coverage: {coverage.status.replaceAll('_', ' ')}.</b>
        {staleMinutes != null && <p className="mt-1">The latest verified observation is about {staleMinutes} minutes old.</p>}
        {possibleRange && <p className="mt-1">Possible unseen rounds during the stale period: {possibleRange.lower}–{possibleRange.upper} (low-confidence estimate from past timing; these are not known round IDs or confirmed outcomes).</p>}
        {sourceGaps.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5">{sourceGaps.map((gap, index) => <li key={`${gap.after_identity_key}:${gap.before_identity_key}:${index}`}>
          Source sequence {gap.after_source_sequence ?? '?'} → {gap.before_source_sequence ?? '?'} has a continuity gap.
          {gap.observed_but_excluded_rounds?.length > 0 && <> Stored round{gap.observed_but_excluded_rounds.length > 1 ? 's' : ''} {gap.observed_but_excluded_rounds.map(round => `#${round.source_sequence} (${round.multiplier}x, ${round.round_id})`).join(', ')} {gap.observed_but_excluded_rounds.length > 1 ? 'are' : 'is'} marked order-uncertain and excluded from the rolling analysis.</>}
          {gap.unrepresented_analyzed_sequence_count > 0 && <> {gap.unrepresented_analyzed_sequence_count} sequence position{gap.unrepresented_analyzed_sequence_count > 1 ? 's are' : ' is'} not represented by an observed record in Behaviour Detection.</>}
        </li>)}</ul>}
        <p className="mt-2 text-xs text-amber-200/80">No missing round IDs or outcomes are invented. Time estimates never complete a verified window.</p>
      </div>}
      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
          <div className="mb-3 flex items-center justify-between gap-2"><h3 className="font-semibold text-zinc-100">Rolling horizons</h3><span className="text-[11px] text-zinc-500">HIGH/LOW percentages exclude EXACT</span></div>
          <div className="overflow-x-auto"><table className="w-full min-w-[820px] text-left text-xs"><thead className="text-[10px] uppercase tracking-wide text-zinc-500"><tr>{['Rounds','H / L / E','HIGH share','LOW share','95% HIGH interval','State','Momentum','Max run H/L','Clusters H/L'].map(item => <th key={item} className="border-b border-zinc-800 px-2 py-2">{item}</th>)}</tr></thead>
            <tbody className="divide-y divide-zinc-800/70">{Object.entries(data.rolling?.horizons || {}).map(([horizon, item]) => { const isAuditSource = horizon === data.high_dominance_2x?.trigger_horizon && item.state === 'HIGH_DOMINANT'; return <tr key={horizon} className={isAuditSource ? 'bg-emerald-400/[0.08]' : ''}><td className="px-2 py-2 font-bold text-zinc-200">{horizon}{isAuditSource && <span className="ml-1.5 rounded bg-emerald-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-emerald-300">2x source</span>}</td><td className="px-2 py-2 tabular-nums">{item.counts?.H ?? 0} / {item.counts?.L ?? 0} / {item.counts?.E ?? 0}</td><td className="px-2 py-2 text-emerald-300">{percent(item.high_share_nonexact)}</td><td className="px-2 py-2 text-rose-300">{percent(item.low_share_nonexact)}</td><td className="px-2 py-2 tabular-nums">{item.high_share_wilson_95 ? `${percent(item.high_share_wilson_95[0])}–${percent(item.high_share_wilson_95[1])}` : '—'}</td><td className="px-2 py-2"><StatusBadge value={item.state} /></td><td className="px-2 py-2 text-zinc-300">{item.momentum_vs_next_horizon || '—'}</td><td className="px-2 py-2 tabular-nums">{item.high_runs || 0} / {item.low_runs || 0}</td><td className="px-2 py-2 tabular-nums">{item.high_clusters?.cluster_count || 0} / {item.low_clusters?.cluster_count || 0}</td></tr>;})}</tbody>
          </table></div>
          <p className="mt-2 text-[11px] text-zinc-500">Observed HIGH share baseline: {percent(data.research_reference?.high_share_nonexact)} from {number(data.research_reference?.rounds, 0)} historical rounds. Wilson intervals and deviation are descriptive; research did not validate a predictive edge.</p>
        </section>
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
          <div className="flex items-center justify-between gap-2"><h3 className="font-semibold text-zinc-100">Current 100-round window</h3><StatusBadge value={windowNow.lifecycle_status} /></div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-sky-400 transition-[width]" style={{ width: `${Math.min(100, Number(roundsProgress.fraction || 0) * 100)}%` }} /></div>
          <div className="mt-2 flex justify-between text-xs text-zinc-400"><span>{roundsProgress.completed || 0} verified</span><span>{Math.max(0, 100 - (roundsProgress.completed || 0))} observed rounds remaining</span></div>
          {timeProgress.remaining_seconds && <p className="mt-2 text-[11px] text-zinc-500">Estimated remaining time low / median / high: {number(timeProgress.remaining_seconds.low_seconds, 0)}s / {number(timeProgress.remaining_seconds.median_seconds, 0)}s / {number(timeProgress.remaining_seconds.high_seconds, 0)}s.</p>}
          <div className="mt-3 grid grid-cols-3 gap-2 text-xs"><div className="rounded bg-zinc-900 p-2">HIGH <b className="text-emerald-300">{windowNow.summary?.counts?.H ?? 0}</b></div><div className="rounded bg-zinc-900 p-2">LOW <b className="text-rose-300">{windowNow.summary?.counts?.L ?? 0}</b></div><div className="rounded bg-zinc-900 p-2">EXACT <b className="text-amber-200">{windowNow.summary?.counts?.E ?? 0}</b></div></div>
          <div className="mt-3 space-y-1 text-xs text-zinc-400"><p>Opening: <b className="text-zinc-200">{windowNow.summary?.opening_state || 'INSUFFICIENT DATA'}</b> · Closing: <b className="text-zinc-200">{windowNow.summary?.closing_state || 'COLLECTING'}</b></p>
            <p>Relationship: <b className="text-zinc-200">{windowNow.summary?.relationship_to_previous?.classification || 'FIRST WINDOW'}</b></p>
            <p>Time cadence: <b className="text-zinc-200">{number(coverage.selected_cadence?.median_seconds, 1)}s median</b> · estimate ± broad empirical quantiles</p></div>
        </section>
      </div>
      <details className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-sm font-semibold text-zinc-100"><span>View Consecutive window relationships ({data.windows?.length || 0})</span><span className="text-[11px] font-normal text-zinc-500">Observed transitions · not independent forecasts <span aria-hidden="true" className="ml-1 text-sky-300">＋</span></span></summary>
        <div className="mt-3">
        {data.windows?.length ? <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">{[...data.windows].reverse().map(window => <div key={window.window_id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <div className="flex justify-between gap-2"><b className="text-sm text-zinc-100">Window {window.window_number}</b><StatusBadge value={window.lifecycle_status} /></div>
          <p className="mt-2 text-xs text-zinc-400">{window.summary?.observed_window_transition || 'Opening and closing behaviour are collecting'}</p>
          <p className="mt-1 text-[11px] text-zinc-500">From previous: {window.summary?.relationship_to_previous?.classification || 'FIRST WINDOW'} · {window.summary?.counts?.H ?? 0} H · {window.summary?.counts?.L ?? 0} L · {window.summary?.counts?.E ?? 0} E · {window.summary?.completeness || 'COLLECTING'}</p>
          {window.summary?.cluster_change_from_previous && <p className="mt-1 text-[11px] text-zinc-500">Cluster count delta H/L: {window.summary.cluster_change_from_previous.high_cluster_count_delta} / {window.summary.cluster_change_from_previous.low_cluster_count_delta}</p>}
        </div>)}</div> : <p className="text-sm text-zinc-500">The first cohort window is waiting for verified rounds.</p>}
        </div>
      </details>

      <HighDominance2xCard audit={data.high_dominance_2x}
        onDelete={() => refresh(() => true)} />
      <HighDominanceStateTestCard audit={data.high_dominance_state_test} rolling={data.rolling}
        onDelete={() => refresh(() => true)} />

      <details className="rounded-xl border border-zinc-800 bg-zinc-950/40 p-3">
        <summary className="cursor-pointer list-none text-sm font-semibold text-zinc-200"><span className="inline-flex items-center gap-2">View sound and voice controls <span aria-hidden="true" className="text-xs text-sky-300">＋</span></span>
          <span className="ml-2 text-[11px] font-normal text-zinc-500">{settings.muted ? 'Muted' : settings.enabled ? audioStatus : 'Master audio OFF'}</span></summary>
      <section className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-semibold text-zinc-100">Behaviour sound and voice controls</h3><p className="mt-1 text-[11px] text-zinc-500">Live audio uses the shared notification manager; selections require preview and your explicit audibility confirmation.</p></div>
          <div className="flex flex-wrap gap-2"><button onClick={async () => setAudioStatus((await enableNotificationAudio()).message)} className="rounded border border-sky-600/50 px-3 py-1.5 text-xs font-semibold text-sky-200 hover:bg-sky-900/30">Enable Sounds</button>
            <button onClick={() => { stopSound(); stopVoice(); cancelCompletion(); setRepeatStopped(true); }} className="rounded border border-zinc-700 px-3 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-800">Stop</button>
            <button onClick={() => { if (!settings.muted) { stopSound(); stopVoice(); cancelCompletion(); setRepeatStopped(true); } else { setRepeatStopped(false); } persist({ ...settings, muted: !settings.muted }, settings.muted ? 'Notifications unmuted.' : 'All notification audio muted.'); }}
              className="rounded border border-zinc-700 px-3 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-800">{settings.muted ? 'Unmute' : 'Mute all'}</button></div>
        </div>
        <p role="status" className="mb-3 text-xs text-sky-200">{audioStatus}{notice ? ` · ${notice}` : ''}</p>
        <div className="grid gap-3 lg:grid-cols-3">
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300">Master notifications
            <select value={settings.enabled ? 'on' : 'off'} onChange={event => persist({ ...settings, enabled: event.target.value === 'on' })} className="mt-2 w-full rounded border border-zinc-700 bg-zinc-950 p-2"><option value="off">Disabled</option><option value="on">Enabled after verified previews</option></select>
          </label>
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300">Notification mode
            <select value={settings.mode} onChange={event => persist({ ...settings, mode: event.target.value })} className="mt-2 w-full rounded border border-zinc-700 bg-zinc-950 p-2"><option value="sound-voice">Sound + voice</option><option value="sound-only">Sound only</option><option value="voice-only">Voice only</option></select>
          </label>
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300">Notification priority
            <select value={settings.priority} onChange={event => persist({ ...settings, priority: event.target.value })} className="mt-2 w-full rounded border border-zinc-700 bg-zinc-950 p-2"><option value="all">All</option><option value="important">Warnings and critical</option><option value="critical">Critical only</option></select>
          </label>
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300">Master volume · {Math.round(Number(settings.volume) * 100)}%
            <input type="range" min="0" max="1" step="0.01" value={settings.volume} onChange={event => setSettings(current => ({ ...current, volume: Number(event.target.value) }))} onMouseUp={() => persist(settings)} onTouchEnd={() => persist(settings)} className="mt-3 w-full accent-sky-400" />
          </label>
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300"><span className="flex items-center justify-between gap-2">Repeat active dominance sound <input type="checkbox" checked={Boolean(settings.behaviourAlerts?.repeatEnabled)} onChange={event => persist({ ...settings, behaviourAlerts: { ...settings.behaviourAlerts, repeatEnabled: event.target.checked } })} className="accent-sky-400" /></span>
            <span className="mt-1 block text-[11px] text-zinc-500">Repeats only while confirmed dominance is active and verified data is fresh.</span>
            <span className="mt-2 flex items-center gap-2">Every <input aria-label="Dominance repeat seconds" type="number" min="10" max="300" value={settings.behaviourAlerts?.intervalSeconds ?? 60} onChange={event => setSettings(current => ({ ...current, behaviourAlerts: { ...current.behaviourAlerts, intervalSeconds: Math.max(10, Math.min(300, Number(event.target.value) || 60)) } }))} onBlur={() => persist(settings)} className="w-20 rounded border border-zinc-700 bg-zinc-950 p-1.5" /> seconds</span>
          </label>
          <label className="rounded-lg bg-zinc-900/80 p-3 text-xs text-zinc-300">Window completion sound duration · {settings.behaviourAlerts?.completionDurationSeconds ?? 5}s
            <input type="range" min="0" max="30" step="1" value={settings.behaviourAlerts?.completionDurationSeconds ?? 5} onChange={event => setSettings(current => ({ ...current, behaviourAlerts: { ...current.behaviourAlerts, completionDurationSeconds: Number(event.target.value) } }))} onMouseUp={() => persist(settings)} onTouchEnd={() => persist(settings)} className="mt-3 w-full accent-sky-400" />
            <span className="mt-1 block text-[11px] text-zinc-500">Set 0–30 seconds; Stop or Mute cancels active playback.</span>
          </label>
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-2">{[
          ['behaviour.high_rising', 'HIGH rising'], ['behaviour.low_rising', 'LOW rising'],
          ['behaviour.high', 'HIGH active'], ['behaviour.low', 'LOW active'],
          ['behaviour.high_signal_generated', 'HIGH signal generated'],
          ['behaviour.shadow_signal_accepted', 'SHADOW signal accepted'],
          ['behaviour.shadow_signal_rejected', 'SHADOW signal rejected'],
          ['behaviour.signal_settled_win', 'Signal settled WIN'],
          ['behaviour.signal_settled_loss', 'Signal settled LOSS'],
          ['behaviour.balance', 'Return to balance'], ['behaviour.transition', 'Dominance transition'],
          ['behaviour.window_completed', 'Window completed'], ['behaviour.possible_missing', 'Possible missing rounds'],
        ].map(([eventType, title]) => {
          const status = profileStatus[eventType]; const profile = profiles[eventType];
          return <div key={eventType} className="rounded-lg border border-zinc-800 bg-zinc-900/70 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2"><b className="text-sm text-zinc-200">{title}</b><StatusBadge value={settings.events[eventType]?.enabled ? 'APPROVED' : 'PREVIEW REQUIRED'} /></div>
            <div className="mt-2 flex flex-wrap gap-2"><select value={profile} onChange={event => updateProfile(eventType, event.target.value)} className="min-w-40 flex-1 rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs">{SOUND_PROFILES.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
              <button onClick={() => void previewProfile(eventType)} className="rounded border border-sky-700/60 px-3 py-1.5 text-xs text-sky-200">PLAY</button>
              <button onClick={stopSound} className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300">STOP</button></div>
            <label className="mt-2 block text-[11px] text-zinc-400">Event volume · {Math.round(Number(eventVolumes[eventType] ?? settings.volume) * 100)}%
              <input type="range" min="0" max="1" step="0.01" value={eventVolumes[eventType] ?? settings.volume} onChange={event => { setEventVolumes(current => ({ ...current, [eventType]: Number(event.target.value) })); setProfileStatus(current => ({ ...current, [eventType]: null })); }} className="mt-1 w-full accent-sky-400" /></label>
            {status && <p role="status" className="mt-2 text-[11px] text-zinc-400">{status.status}: {status.message || status.confirmation || ''}</p>}
            <div className="mt-2 flex flex-wrap gap-2"><button disabled={status?.status !== 'ended'} onClick={() => confirmSound(eventType, true)} className="rounded bg-emerald-900/50 px-2.5 py-1.5 text-[11px] text-emerald-200 disabled:opacity-40">Yes, I heard it</button>
              <button disabled={status?.status !== 'ended'} onClick={() => confirmSound(eventType, false)} className="rounded bg-rose-950/50 px-2.5 py-1.5 text-[11px] text-rose-200 disabled:opacity-40">No, I did not</button>
              <button disabled={!verifiedProfiles.has(soundVerificationKey(profile, Number(eventVolumes[eventType] ?? settings.events[eventType]?.volume ?? settings.volume)))} onClick={() => useSound(eventType)} className="rounded border border-emerald-700/60 px-2.5 py-1.5 text-[11px] text-emerald-200 disabled:opacity-40">Use This Sound</button></div>
          </div>;
        })}</div>
        <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-semibold text-zinc-200">Browser voice preview</h4><span className="text-[11px] text-zinc-500">{voices.length} available voices · browser provided</span></div>
          {voices.length ? <><div className="mt-2 flex flex-wrap gap-2"><select value={settings.voiceURI || voices[0].voiceURI} onChange={event => { const voice = voices.find(item => item.voiceURI === event.target.value); setSettings(current => ({ ...current, voiceURI: event.target.value, language: voice?.lang || '' })); setVoiceStatus('Voice selection changed. Preview before confirming audibility.'); }} className="min-w-56 flex-1 rounded border border-zinc-700 bg-zinc-950 p-2 text-xs">{voices.map(voice => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang}</option>)}</select>
            <button onClick={previewVoice} className="rounded border border-sky-700/60 px-3 py-1.5 text-xs text-sky-200">Preview voice</button><button onClick={stopVoice} className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300">Stop voice</button></div>
            <div className="mt-2 grid gap-2 sm:grid-cols-2"><label className="text-[11px] text-zinc-400">Preview text<input value={voiceSample} onChange={event => setVoiceSample(event.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 p-2 text-xs text-zinc-200" /></label>
              <label className="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" checked={Boolean(settings.voiceEnabled)} onChange={event => persist({ ...settings, voiceEnabled: event.target.checked })} className="accent-sky-400" />Enable voice announcements after preview</label></div>
            <div className="mt-2 grid gap-2 sm:grid-cols-3"><label className="text-[11px] text-zinc-400">Rate {number(settings.rate, 2)}<input type="range" min="0.5" max="2" step="0.05" value={settings.rate} onChange={event => { setSettings(current => ({ ...current, rate: Number(event.target.value) })); setVoiceStatus('Voice settings changed. Preview before confirming audibility.'); }} onBlur={() => persist(settings)} className="mt-1 w-full accent-sky-400" /></label>
              <label className="text-[11px] text-zinc-400">Pitch {number(settings.pitch, 2)}<input type="range" min="0.5" max="2" step="0.05" value={settings.pitch} onChange={event => { setSettings(current => ({ ...current, pitch: Number(event.target.value) })); setVoiceStatus('Voice settings changed. Preview before confirming audibility.'); }} onBlur={() => persist(settings)} className="mt-1 w-full accent-sky-400" /></label>
              <label className="text-[11px] text-zinc-400">Voice volume {Math.round(Number(settings.voiceVolume) * 100)}%<input type="range" min="0" max="1" step="0.05" value={settings.voiceVolume} onChange={event => { setSettings(current => ({ ...current, voiceVolume: Number(event.target.value) })); setVoiceStatus('Voice settings changed. Preview before confirming audibility.'); }} onBlur={() => persist(settings)} className="mt-1 w-full accent-sky-400" /></label></div>
            </> : <p className="mt-2 text-xs text-amber-200">SpeechSynthesis has no available voices in this browser. Kinyarwanda is not assumed unless the browser lists a voice with that language.</p>}
          <p role="status" className="mt-2 text-xs text-zinc-400">{voiceStatus || `Sample: “${voiceSample}”`}</p>
          {voices.length > 0 && <div className="mt-2 flex flex-wrap gap-2"><button disabled={!voiceStatus.includes('ended')} onClick={() => confirmVoice(true)} className="rounded bg-emerald-900/50 px-2.5 py-1.5 text-[11px] text-emerald-200 disabled:opacity-40">Yes, I heard the voice</button><button disabled={!voiceStatus.includes('ended')} onClick={() => confirmVoice(false)} className="rounded bg-rose-950/50 px-2.5 py-1.5 text-[11px] text-rose-200 disabled:opacity-40">No, I did not</button></div>}
        </div>
        <div className="mt-3 rounded-lg border border-violet-900/50 bg-violet-950/15 p-3">
          <div className="mb-2"><h4 className="text-sm font-semibold text-violet-100">Synthetic notification simulator</h4><p className="mt-1 text-[11px] text-zinc-500">Test events only enter the shared notification manager and never touch canonical rounds or analytics.</p></div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-[1fr_1.4fr_auto_auto_auto]"><select value={testEvent} onChange={event => setTestEvent(event.target.value)} className="rounded border border-zinc-700 bg-zinc-950 p-2 text-xs">{eventTypes.map(([key, [module, label]]) => <option key={key} value={key}>{module} · {label}</option>)}</select>
            <input value={testMessage} onChange={event => setTestMessage(event.target.value)} aria-label="Synthetic message" className="rounded border border-zinc-700 bg-zinc-950 p-2 text-xs" />
            <label className="flex items-center gap-1 text-xs text-zinc-300"><input type="checkbox" checked={testSound} onChange={event => setTestSound(event.target.checked)} />Sound</label>
            <label className="flex items-center gap-1 text-xs text-zinc-300"><input type="checkbox" checked={testVoice} onChange={event => setTestVoice(event.target.checked)} />Voice</label>
            <button onClick={() => void runSyntheticTest()} className="rounded bg-violet-700/60 px-3 py-2 text-xs font-bold text-violet-100">TEST NOTIFICATION</button></div>
          <div className="mt-2 flex items-center gap-3 text-xs text-zinc-400"><label>Volume <input aria-label="Test volume" type="range" min="0" max="1" step="0.01" value={testVolume} onChange={event => setTestVolume(Number(event.target.value))} className="ml-2 accent-violet-400" /></label><span role="status">{testStatus}</span></div>
        </div>
        <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-900/50 p-3"><h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Recent notification history</h4>
          {notificationHistory.length ? <ul className="mt-2 space-y-1">{notificationHistory.map(item => <li key={item.id} className="flex flex-wrap justify-between gap-x-4 gap-y-1 text-[11px] text-zinc-400"><span>{item.synthetic ? 'TEST · ' : ''}{item.message}</span><span>{item.playback || 'not played'} · {shortTime(item.timestamp)}</span></li>)}</ul>
            : <p className="mt-2 text-xs text-zinc-500">No notifications recorded in this browser yet.</p>}</div>
      </section>
      <p className="text-[11px] leading-5 text-zinc-500">Audio playback status is separate from speaker audibility. Confirming “Yes, I heard it” is stored as your test result. Suspended tabs and closed browsers may not play alerts; on return, freshness checks and visual notifications remain available.</p>
      </details>
    </>}
  </section>;
}

function Overview() {
  return <ShellPage title="Live behaviour" subtitle="Live rounds · sync progress · current behaviour" path="/overview" refreshMs={15000} render={data => {
    const live = data.live_context || {};
    const latest = live.latest_round || data.latest_round;
    const rolling = live.rolling50 || {};
    const counts = rolling.counts || live.counts || {};
    const next = data.next100 || {};
    const sourceHealth = data.health?.winner_predict_database || {};
    const sourceDetails = sourceHealth.details || {};
    const roundSync = data.high_dominance_2x?.round_sync || data.round_sync || {};
    const liveProcessor = data.high_dominance_2x?.live_processor || {};
    const workerHealth = data.health?.analysis_worker || {};
    const workerDetails = workerHealth.details || {};
    const isConnected = sourceHealth.state === 'HEALTHY';
    const statusLabel = isConnected ? 'DATABASE CONNECTED' : (sourceHealth.state || 'NOT REPORTING');
    const syncedRounds = Math.max(0, Number(sourceDetails.synced_round_count) || 0);
    const anchorIndex = sourceDetails.anchor_round_index == null ? null : Number(sourceDetails.anchor_round_index);
    const sourceLatestIndex = Number(sourceDetails.source_latest_round_index) || 0;
    const syncTotal = Math.max(syncedRounds, anchorIndex == null ? syncedRounds : Math.max(0, sourceLatestIndex - anchorIndex));
    const syncCompleted = Math.min(syncTotal, syncedRounds);
    const phase1Rounds = Number(data.population?.imported_canonical_rounds) || 0;
    const workerPosition = Number(workerDetails.last_position) || 0;
    const analysisCompleted = Math.min(syncedRounds, Math.max(0, workerPosition - phase1Rounds));
    const syncWaiting = String(sourceDetails.status || '').startsWith('WAITING');
    const connectionMessage = isConnected
      ? syncWaiting ? 'Database connected; waiting for another stored round.' : 'Winner Predict PostgreSQL sync status. This does not confirm a live Orbit round.'
      : sourceDetails.error_code || sourceDetails.status || sourceDetails.reason || 'Winner Predict database source status.';
    const stateName = live.current_state || data.behaviour?.observed_state || 'UNCERTAIN';
    const stateTone = stateName.includes('HIGH') ? 'text-emerald-300' : stateName.includes('LOW') ? 'text-rose-300' : 'text-sky-200';
    const currentRoundIndex = latest?.round_index ?? latest?.source_round_index ?? live.source_reading_id ?? '—';
    const contextCount = Math.min(50, Number(live.rolling_window_rounds) || 0);
    const outcomeCounts = { H: Number(counts.H) || 0, L: Number(counts.L) || 0, E: Number(counts.E) || 0 };
    const recovery = live.historical_recovery_comparison || data.live_outlook?.current_context || {};
    const historicalRange = data.live_outlook?.historical_baseline?.high_count_distribution
      || next.historical_count_distribution?.high_count || {};

    return <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <section className={`${panel} relative overflow-hidden border-emerald-500/20 bg-gradient-to-br from-emerald-950/40 to-zinc-900/80`}>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-400">Round source</p>
            <span className={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 text-[11px] font-bold ${isConnected ? 'bg-emerald-400/10 text-emerald-300' : 'bg-amber-400/10 text-amber-200'}`}>
              <i className={`h-2 w-2 rounded-full ${isConnected ? 'bg-emerald-400' : 'bg-amber-300'}`} />{statusLabel}
            </span>
          </div>
          <p className="mt-4 text-xl font-bold text-white">Winner Predict</p>
          <p className="mt-1 text-sm text-zinc-400">{connectionMessage}</p>
          <div className="mt-5 grid grid-cols-2 gap-3 border-t border-white/10 pt-3">
            <div><p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Synced through</p>
              <p className="mt-1 text-lg font-bold tabular-nums text-zinc-100">#{sourceDetails.sync_cursor_round_index || currentRoundIndex}</p></div>
            <div><p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Last DB check</p>
              <p className="mt-1 text-lg font-bold tabular-nums text-zinc-100">{shortTime(live.source_checked_at)}</p></div>
          </div>
          <div className="mt-3 border-t border-white/10 pt-3 text-xs text-zinc-400">
            <span className="font-semibold uppercase tracking-wide text-zinc-500">Live Orbit processing</span>
            <p className="mt-1 font-semibold text-zinc-200">{roundSync.live_status || (roundSync.provenance === 'LIVE_VERIFIED' ? 'LIVE_READY' : 'WAITING_FOR_LIVE')}</p>
            <p className="mt-1">Last live round: {roundSync.last_live_round_id || 'None recorded'} · {roundSync.last_live_round_processed_at ? shortTime(roundSync.last_live_round_processed_at) : 'time unavailable'}</p>
            <p className="mt-1">Stored-round age: {latest?.source_observed_at ? lagLabel(elapsedMs(latest.source_observed_at, new Date().toISOString())) : 'unknown'} · database sync is shown separately above.</p>
            <p className="mt-1">Decision latency p50/p95/p99: {liveProcessor.p50_ms == null ? 'no live samples' : `${liveProcessor.p50_ms}/${liveProcessor.p95_ms}/${liveProcessor.p99_ms} ms`} · n={liveProcessor.samples || 0}</p>
          </div>
        </section>

        <section className={`${panel} flex flex-col justify-between`}>
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-400">Latest round</p>
            <span className="rounded-md bg-zinc-950/70 px-2 py-1 text-xs font-semibold tabular-nums text-zinc-400">#{currentRoundIndex}</span>
          </div>
          {latest ? <>
            <div className="mt-4 flex items-end justify-between gap-3">
              <p className="text-5xl font-black leading-none tracking-tight text-white">{latest.multiplier}<span className="ml-1 text-2xl font-bold text-zinc-400">×</span></p>
              <span className={`rounded-lg px-3 py-1.5 text-sm font-extrabold tracking-wide ${latest.outcome === 'H' ? 'bg-emerald-400/10 text-emerald-300' : latest.outcome === 'L' ? 'bg-rose-400/10 text-rose-300' : 'bg-amber-300/10 text-amber-200'}`}>
                {latest.outcome === 'H' ? 'HIGH' : latest.outcome === 'L' ? 'LOW' : 'EXACT'}
              </span>
            </div>
            <div className="mt-5 flex flex-wrap gap-x-4 gap-y-1 border-t border-zinc-800 pt-3 text-xs text-zinc-500">
              <span title={timestamp(latest.source_observed_at)}>Observed <b className="text-zinc-300">{shortTime(latest.source_observed_at)}</b></span>
              <span title={timestamp(latest.ingestion_timestamp)}>Stored <b className="text-zinc-300">{shortTime(latest.ingestion_timestamp)}</b></span>
            </div>
          </> : <p className="mt-6 text-sm text-zinc-500">Waiting for stored rounds.</p>}
        </section>

        <section className={`${panel} flex flex-col justify-between`}>
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-400">Current behaviour</p>
            <span className={`h-3 w-3 rounded-full ${stateName.includes('HIGH') ? 'bg-emerald-400' : stateName.includes('LOW') ? 'bg-rose-400' : 'bg-sky-300'}`} />
          </div>
          <div className="mt-4">
            <p className={`text-3xl font-black tracking-tight ${stateTone}`}>{stateName.replaceAll('_', ' ')}</p>
            <p className="mt-2 text-sm text-zinc-400">Based on the latest {contextCount} rounds.</p>
          </div>
          <Link className="mt-5 inline-flex items-center text-xs font-semibold text-emerald-300 hover:text-emerald-200" to="/behaviour/state">View state details <span className="ml-1" aria-hidden="true">→</span></Link>
        </section>
      </div>

      <section className={panel}>
        <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
          <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-300">Progress</p>
            <h2 className="mt-1 text-lg font-semibold text-zinc-100">Data pipeline</h2></div>
          <span className="text-xs text-zinc-500">Completed and remaining rounds</span>
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          <ProgressBar label="Database sync" completed={syncCompleted} total={syncTotal}
            helper={`Winner Predict → Behaviour · round ${sourceDetails.sync_cursor_round_index || '—'}`} />
          <ProgressBar label="Behaviour analysis" completed={analysisCompleted} total={syncedRounds}
            helper={workerHealth.state === 'HEALTHY' ? 'Worker is running' : `Worker: ${workerHealth.state || 'waiting'}`} />
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <OutcomeBar counts={outcomeCounts} />
        <SequenceStrip sequence={live.recent_sequence_hle} />
      </div>

      <section className={panel}>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-300">Historical reference</p>
            <h2 className="mt-1 text-lg font-semibold text-zinc-100">Next 100 rounds</h2></div>
          <Link className="text-xs font-semibold text-emerald-300 hover:text-emerald-200" to="/behaviour/next100">Details →</Link>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          {[
            { key: 'HIGH', value: next.expected_high_count, color: 'text-emerald-300' },
            { key: 'LOW', value: next.expected_low_count, color: 'text-rose-300' },
            { key: 'EXACT', value: next.expected_exact_count, color: 'text-amber-200' },
          ].map(item => <div key={item.key} className="rounded-lg bg-zinc-950/50 p-3">
            <p className="text-[11px] font-semibold tracking-wide text-zinc-500">EXPECTED {item.key}</p>
            <p className={`mt-1 text-2xl font-bold tabular-nums ${item.color}`}>{number(item.value, 1)}</p>
          </div>)}
        </div>
        <details className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 px-3 py-2.5">
          <summary className="cursor-pointer text-xs font-semibold text-zinc-300">Show baseline and historical context</summary>
          <div className="mt-3 grid gap-2 text-xs text-zinc-500 sm:grid-cols-2">
            <p>Phase 1 HIGH reference: <b className="text-zinc-300">{percent(recovery.phase1_high_rate_nonexact)}</b></p>
            <p>Historical HIGH-count range: <b className="text-zinc-300">{number(historicalRange.p05, 1)}–{number(historicalRange.p95, 1)}</b></p>
            <p>Similar historical windows: <b className="text-zinc-300">{recovery.matched_historical_windows ?? 0}</b></p>
            <p>Forecast method: <b className="text-zinc-300">Phase 1 expanding baseline</b></p>
          </div>
        </details>
      </section>
    </div>;
  }} />;
}

function LiveRounds() {
  return <ShellPage title="Live round monitor" subtitle="Persisted rounds with source identity, event time, ingestion time and order quality." path="/rounds?limit=100" refreshMs={2000} render={data => {
    const items = data.items || [];
    const mix = items.reduce((counts, round) => { const key = String(round.outcome || '').toUpperCase(); if (key in counts) counts[key] += 1; return counts; }, { H: 0, L: 0, E: 0 });
    return <div className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-3"><Metric label="Rounds on screen" value={items.length} detail="Newest persisted rounds" />
      <Metric label="Latest event time" value={shortTime(items[0]?.event_timestamp)} detail={timestamp(items[0]?.event_timestamp)} />
      <Metric label="Latest received" value={shortTime(items[0]?.ingestion_timestamp)} detail={timestamp(items[0]?.ingestion_timestamp)} /></div>
    <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Outcome mix in this view</h2><span className="text-xs text-zinc-500">H / L / E kept separate</span></div>
      <ShareBar label="Live round outcomes" items={[{ label: 'HIGH', value: mix.H, color: 'bg-emerald-400' }, { label: 'LOW', value: mix.L, color: 'bg-rose-400' }, { label: 'EXACT', value: mix.E, color: 'bg-amber-300' }]} /></section>
    <Table columns={[
      { label: 'Position', key: 'analysis_position' }, { label: 'Round ID', render: r => r.round_id || r.identity_key },
      { label: 'Multiplier', render: r => <span className="font-semibold tabular-nums text-zinc-100">{r.multiplier}x</span> }, { label: 'Class', render: r => <StatusBadge value={r.outcome} /> },
      { label: 'Event time', render: r => timestamp(r.event_timestamp) },
      { label: 'Ingested', render: r => timestamp(r.ingestion_timestamp) },
      { label: 'Time / order quality', render: r => <span title={r.timestamp_quality} className="space-y-1"><StatusBadge value={r.timestamp_quality} /><br /><StatusBadge value={r.order_quality} /></span> },
    ]} rows={items} rowKey={r => r.identity_key} />
    </div>;
  }} />;
}

function StatePage() {
  return <ShellPage title="Current behavioural state" subtitle="Observed and confirmed states are shown separately with thresholds and sample support." path="/state" refreshMs={3000} render={data => {
    const evidence = data.evidence || {};
    const windows = data.rolling_windows || data.live_context?.rolling_windows || {};
    const evidenceRows = Object.entries(evidence).filter(([key, value]) => key !== 'explanation' && value !== null && typeof value !== 'object');
    return <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Confirmed duration" value={`${data.duration_rounds ?? 0} rounds`} detail="Consecutive analysis updates" />
        <Metric label="Rolling windows" value={Object.keys(windows).length} detail="Current analysis context" />
        <Metric label="Observed candidate" value={data.observed_state || '—'} detail="May be awaiting confirmation" />
        <Metric label="Confirmed state" value={data.confirmed_state || '—'} detail="Current persisted state" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className={`${panel} border-amber-500/20 bg-gradient-to-br from-amber-950/25 to-zinc-900/80`}><div className="flex items-center justify-between gap-3"><p className="text-xs font-semibold uppercase tracking-wide text-amber-200">Observed candidate</p><StatusBadge value={data.observed_state} /></div>
          <p className="mt-3 text-sm leading-6 text-zinc-300">{evidence.explanation || 'No candidate explanation supplied.'}</p></section>
        <section className={`${panel} border-emerald-500/20 bg-gradient-to-br from-emerald-950/20 to-zinc-900/80`}><div className="flex items-center justify-between gap-3"><p className="text-xs font-semibold uppercase tracking-wide text-emerald-200">Confirmed state</p><StatusBadge value={data.confirmed_state} /></div>
          <p className="mt-3 text-sm text-zinc-300">{data.duration_rounds ?? 0} consecutive analysis updates in the current state.</p>
          <p className="mt-2 text-xs text-zinc-500">A candidate must persist for three updates by default before a change is confirmed.</p></section>
      </div>
      <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">Rule evidence</h3><span className="text-xs text-zinc-500">Key checks first; full details expandable</span></div>
        {evidenceRows.length ? <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">{evidenceRows.map(([key, value]) => <div key={key} className="rounded-lg bg-zinc-950/60 p-3"><p className="text-xs uppercase tracking-wide text-zinc-500">{key.replaceAll('_', ' ')}</p><p className="mt-1 break-words text-sm font-semibold text-zinc-200">{String(value)}</p></div>)}</div> : <p className="text-sm text-zinc-500">No scalar evidence fields reported.</p>}
        <details className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-xs font-medium text-zinc-400">Full evidence details</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{JSON.stringify(evidence, null, 2)}</pre></details></section>
      <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">State context across rolling windows</h3><span className="text-xs text-zinc-500">Support, rates and alternation</span></div><WindowTable windows={windows} /></section>
    </div>;
  }} />;
}

function WindowTable({ windows = {} }) {
  const rows = Object.entries(windows).map(([window, value]) => ({ window, ...value }));
  return <Table columns={[
    { label: 'Window', key: 'window' }, { label: 'Support', render: r => `${r.support ?? 0}/${r.required ?? 0}` },
    { label: 'HIGH share (non-E)', render: r => percent(r.high_rate_nonexact) },
    { label: 'LOW share (non-E)', render: r => percent(r.low_rate_nonexact) },
    { label: 'Exact count', render: r => r.counts?.E ?? '—' },
    { label: 'Alternation', render: r => percent(r.alternation_rate) },
    { label: 'Max H/L run', render: r => `${r.run_max?.H ?? '—'} / ${r.run_max?.L ?? '—'}` },
  ]} rows={rows} rowKey={r => r.window} />;
}

function Next100() {
  return <ShellPage title="Next-100 assessment" subtitle="The Phase 1 expanding reference; exact outcomes are not predicted." path="/next100" refreshMs={3000} render={data => <div className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="Expected HIGH count" value={number(data.expected_high_count, 2)} detail={`${percent(data.expected_high_rate_nonexact)} among non-EXACT rounds`} />
      <Metric label="Expected LOW count" value={number(data.expected_low_count, 2)} />
      <Metric label="Expected EXACT count" value={number(data.expected_exact_count, 2)} detail="EXACT remains a separate category" />
      <Metric label="Historical reference blocks" value={data.training_blocks ?? 0} detail={data.data_sufficiency || '—'} />
    </div>
    <section className={`${panel} border-sky-500/20 bg-gradient-to-br from-sky-950/20 to-zinc-900/80`}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Expected outcomes across 100 rounds</h2><span className="text-xs text-zinc-500">Historical assessment · not an exact forecast</span></div>
      <ShareBar label="Expected next 100 outcome counts" items={[{ label: 'HIGH', value: data.expected_high_count, color: 'bg-emerald-400' }, { label: 'LOW', value: data.expected_low_count, color: 'bg-rose-400' }, { label: 'EXACT', value: data.expected_exact_count, color: 'bg-amber-300' }]} /></section>
    <section className={panel}><h2 className="font-semibold">Historical H-count distribution</h2>
      <div className="mt-3 grid gap-3 md:grid-cols-3">{['high_count', 'low_count', 'exact_count'].map(key => <div key={key} className="rounded-lg bg-zinc-950 p-3">
        <p className="text-xs uppercase text-zinc-500">{key.replace('_', ' ')}</p><p className="mt-2 text-sm">Mean {number(data.historical_count_distribution?.[key]?.mean, 2)}</p>
        <p className="text-xs text-zinc-400">Historical p05 / p50 / p95: {number(data.historical_count_distribution?.[key]?.p05, 1)} / {number(data.historical_count_distribution?.[key]?.p50, 1)} / {number(data.historical_count_distribution?.[key]?.p95, 1)}</p></div>)}</div>
      <p className="mt-3 text-xs text-zinc-500">Empirical ranges describe past disjoint blocks and are not guaranteed calibration.</p>
    </section>
    <section className={panel}><h2 className="font-semibold">Threshold probabilities</h2><div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {Object.entries(data.threshold_probabilities || {}).map(([k, v]) => <RateMeter key={k} label={k} value={v} />)}
    </div><p className="mt-3 text-xs text-zinc-500">Threshold probabilities use the research binomial approximation; it does not encode serial dependence.</p></section>
    <section className={panel}><h2 className="font-semibold">Method and limits</h2><p className="mt-2 text-sm text-zinc-300">{data.explanation?.high_rate}</p>
      <p className="mt-2 text-sm text-zinc-300">{data.explanation?.expected_counts}</p>
      <p className="mt-2 text-sm text-zinc-400">{data.explanation?.baseline_evidence}</p>
      <p className="mt-2 rounded border border-amber-800/60 bg-amber-950/20 p-3 text-sm text-amber-200">{data.prediction_status || 'Historical statistical assessment only. No exact future multipliers are known.'}</p>
    </section>
  </div>} />;
}

function Sequences({ outlookOnly = false }) {
  return <ShellPage title={outlookOnly ? 'Sequence outlook' : 'Sequence behaviour monitor'} subtitle={outlookOnly ? 'Empirical next-100 sequence ranges from disjoint historical blocks.' : 'Exact H/L/E motifs and runs from the latest ordered context.'} path="/sequences" refreshMs={5000} render={data => {
    const current = data.current || {};
    const motifRows = (data.historical_motifs || []).filter(m => m.length >= 4).sort((a, b) => b.count - a.count).slice(0, 40);
    if (outlookOnly) {
      const sequence = data.outlook?.sequence_outlook || {};
      const motifStats = Object.entries(sequence.motif_occurrences || {}).map(([motif, stats]) => ({ motif, ...stats }))
        .sort((a, b) => (b.mean || 0) - (a.mean || 0)).slice(0, 24);
      const runRows = Object.keys(sequence.probability_max_high_run_at_least || {}).map(length => ({
        length, high: sequence.probability_max_high_run_at_least?.[length],
        low: sequence.probability_max_low_run_at_least?.[length],
      }));
      return <div className="space-y-4">
      <section className={`${panel} border-sky-500/20 bg-gradient-to-br from-sky-950/20 to-zinc-900/80`}><div className="flex flex-wrap items-start justify-between gap-2"><h2 className="font-semibold">Expected 100-round sequence statistics</h2><StatusBadge value={data.outlook_evidence_status} /></div>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <Metric label="Historical blocks" value={sequence.historical_blocks ?? 0} />
          <Metric label="H/L alternations per block" value={number(sequence.alternation_changes?.mean, 1)} detail="empirical average" />
          <Metric label="Maximum H / L run" value={`${number(sequence.maximum_high_run?.p50, 0)} / ${number(sequence.maximum_low_run?.p50, 0)}`} detail="historical median per 100-round block" />
        </div>
        <p className="mt-3 text-xs text-zinc-500">These are empirical frequencies over historical disjoint blocks. They describe uncertainty; they do not predict exact sequences.</p>
      </section>
      <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Historical probability of long clusters</h2><span className="text-xs text-zinc-500">Chance a 100-round block contains a run at least this long</span></div>
        <div className="grid gap-2 sm:grid-cols-2">{runRows.map(row => <div key={row.length} className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><p className="mb-2 text-sm font-semibold text-zinc-200">{row.length} rounds or longer</p><RateMeter label="HIGH run" value={row.high} /><div className="mt-2"><RateMeter label="LOW run" value={row.low} /></div></div>)}</div></section>
      <section className={panel}><h2 className="mb-3 font-semibold">Most frequent historical motifs</h2><Table columns={[
        { label: 'H/L motif', key: 'motif' }, { label: 'Mean occurrences per 100', render: r => number(r.mean, 2) },
        { label: 'p05 / p50 / p95', render: r => `${number(r.p05, 0)} / ${number(r.p50, 0)} / ${number(r.p95, 0)}` },
        { label: 'Probability absent', render: r => percent(1 - (r.probability_nonzero ?? 0)) },
      ]} rows={motifStats} rowKey={r => r.motif} /></section>
      <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Current sequence context</h2><span className="text-xs text-zinc-500">Latest 500 analyzed outcomes</span></div>
        <div className="grid gap-3 sm:grid-cols-3"><Metric label="Current streak" value={`${current.current_streak?.symbol || '—'} × ${current.current_streak?.length ?? 0}`} />
          <Metric label="Observed alternation" value={percent(current.alternation_rate)} detail={`${current.alternation_count ?? 0} changes across ${current.alternation_denominator ?? 0} pairs`} />
          <Metric label="Maximum run in context" value={`${current.run_max?.H ?? 0} HIGH / ${current.run_max?.L ?? 0} LOW`} /></div></section>
    </div>;
    }
    const topMotifs = motifRows.slice(0, 12).map(row => ({ motif: row.motif, count: row.count }));
    return <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Current streak" value={<span className={current.current_streak?.symbol === 'H' ? 'text-emerald-300' : current.current_streak?.symbol === 'L' ? 'text-rose-300' : 'text-amber-200'}>{current.current_streak?.symbol || '—'} × {current.current_streak?.length ?? 0}</span>} detail="Latest consecutive outcomes" />
        <Metric label="H/L alternation" value={percent(current.alternation_rate)} detail={`${current.alternation_count ?? 0} changes / ${current.alternation_denominator ?? 0} pairs`} />
        <Metric label="Max H run / L run" value={`${current.run_max?.H ?? 0} / ${current.run_max?.L ?? 0}`} detail="Within current analysis context" />
        <Metric label="Sequence entropy" value={number(current.transition_entropy_bits, 3)} detail="bits, conditional transitions" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2"><section className={panel}><div className="mb-3 flex items-center justify-between gap-2"><h2 className="font-semibold">Outcome balance</h2><span className="text-xs text-zinc-500">Latest {current.rounds ?? 0}</span></div>
        <ShareBar label="Sequence outcome counts" items={[{ label: 'HIGH', value: current.counts?.H, color: 'bg-emerald-400' }, { label: 'LOW', value: current.counts?.L, color: 'bg-rose-400' }, { label: 'EXACT', value: current.counts?.E, color: 'bg-amber-300' }]} /></section>
        <section className={panel}><h2 className="mb-1 font-semibold">Most common motifs</h2><p className="text-xs text-zinc-500">Historical recurrence counts · descriptive</p><div className="mt-3 h-48"><ResponsiveContainer width="100%" height="100%"><BarChart data={topMotifs} layout="vertical" margin={{ left: 5, right: 12 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" /><XAxis type="number" stroke="#71717a" /><YAxis type="category" dataKey="motif" width={48} stroke="#a1a1aa" /><Tooltip /><Bar dataKey="count" fill="#34d399" radius={[0, 4, 4, 0]} /></BarChart></ResponsiveContainer></div></section></div>
      <section className={panel}><h2 className="font-semibold">Motifs discovered in Phase 1</h2>
        <p className="mb-3 mt-1 text-xs text-zinc-500">Rolling counts cover the latest 500 outcomes. Motif recurrence is descriptive, never proof of predictive power.</p>
        <Table columns={[
          { label: 'Motif', key: 'motif' }, { label: 'Historical count', key: 'count' },
          { label: 'Rolling 500 count', render: r => data.motif_counts_last_500?.[r.motif]?.count_last_500 ?? 0 },
          { label: 'Shuffle p', render: r => number(r.shuffle_p_value, 4) },
          { label: 'BH q', render: r => number(r.bh_q_value, 4) },
          { label: 'Evidence', key: 'evidence_status' },
        ]} rows={motifRows} rowKey={r => r.motif} />
      </section>
      <section className={panel}><h2 className="font-semibold">Current rolling sequence features</h2><WindowTable windows={data.rolling_windows} /></section>
    </div>;
  }} />;
}

function TimeGroups({ explore = false }) {
  return <ShellPage title={explore ? 'Historical group explorer' : 'Time-based behaviour'} subtitle={explore ? 'Browse all empirically grouped UTC timestamp sessions.' : 'The Phase 1 p99 positive-gap boundary is descriptive; timestamps may be shared or stale.'} path="/time-groups?limit=500" refreshMs={15000} render={data => {
    const rows = data.items || [];
    const points = rows.map(g => ({ group: g.id, high: Number(g.statistics?.sequence?.high_rate_nonexact ?? 0) * 100,
      rounds: g.round_count, time: g.start_at ? new Date(g.start_at).toLocaleDateString() : 'unknown' }));
    return <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3"><Metric label="Time groups" value={data.total ?? 0} detail="UTC event-time sessions" />
        <Metric label="Groups in this view" value={rows.length} detail="Each row is one observed group" />
        <Metric label="Gap threshold" value="180.05 sec" detail="Empirical p99 positive gap" /></div>
      <section className={panel}><div className="mb-1 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">HIGH share by time group</h2><span className="text-xs text-zinc-500">Events are grouped by UTC timestamps</span></div><div className="mt-3 h-64"><ResponsiveContainer width="100%" height="100%"><LineChart data={points}>
        <CartesianGrid strokeDasharray="3 3" stroke="#27272a" /><XAxis dataKey="group" stroke="#71717a" /><YAxis domain={[0, 100]} stroke="#71717a" tickFormatter={value => `${value}%`} />
        <Tooltip formatter={(value, name) => [`${number(value, 1)}%`, name === 'high' ? 'HIGH share' : name]} labelFormatter={value => `Group ${value}`} /><Line type="monotone" dataKey="high" name="HIGH share" stroke="#34d399" strokeWidth={2} dot={false} activeDot={{ r: 4 }} /></LineChart></ResponsiveContainer></div></section>
      {explore && <section className="rounded-xl border border-amber-800/40 bg-amber-950/15 px-4 py-3 text-xs leading-5 text-amber-100/80">Timestamp caution: event timestamps can be shared or stale. The research snapshot recorded 8,722 ties and 60 adjacent local-index inversions, so group order is descriptive.</section>}
      <Table columns={[
        { label: 'Group', key: 'id' }, { label: 'Rounds', key: 'round_count' },
        { label: 'Duration', render: r => r.duration_seconds == null ? '—' : `${number(r.duration_seconds, 1)} s` },
        { label: 'HIGH share (non-E)', render: r => percent(r.statistics?.sequence?.high_rate_nonexact) },
        { label: 'LOW share (non-E)', render: r => percent(r.statistics?.sequence?.low_rate_nonexact) },
        { label: 'Max H/L run', render: r => `${r.statistics?.sequence?.run_max?.H ?? '—'} / ${r.statistics?.sequence?.run_max?.L ?? '—'}` },
        { label: 'State', render: r => <StatusBadge value={r.statistics?.state} /> },
        { label: 'Timestamp quality', render: r => <StatusBadge value={r.timestamp_quality} /> },
        { label: 'Start / end', render: r => <>{timestamp(r.start_at)}<br />{timestamp(r.end_at)}</> },
      ]} rows={rows} rowKey={r => r.id} />
    </div>;
  }} />;
}

function Distribution() {
  return <ShellPage title="HIGH / LOW distribution" subtitle="Exact 2.00x rounds are preserved as E and excluded only where the research denominator specifies non-EXACT." path="/distribution" refreshMs={10000} render={data => {
    const counts = data.counts || {};
    const chart = [{ name: 'HIGH', value: counts.H || 0, color: '#34d399' }, { name: 'LOW', value: counts.L || 0, color: '#f87171' }, { name: 'EXACT', value: counts.E || 0, color: '#fbbf24' }];
    return <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Metric label="Analyzed rounds" value={(data.round_count ?? 0).toLocaleString()} detail="Persisted outcome history" />
        <Metric label="HIGH among non-EXACT" value={percent(data.high_rate_nonexact)} detail="EXACT outcomes excluded" />
        <Metric label="Cumulative mean" value={`${number(data.cumulative_summary?.mean_multiplier, 4)}x`} detail="Full available history" />
        <Metric label="Log volatility" value={number(data.cumulative_summary?.log_volatility_sample, 4)} detail="Sample statistic" /></div>
      <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Outcome shares at a glance</h2><span className="text-xs text-zinc-500">EXACT stays separate from H/L</span></div>
        <ShareBar label="Full history outcomes" items={[{ label: 'HIGH', value: counts.H, color: 'bg-emerald-400' }, { label: 'LOW', value: counts.L, color: 'bg-rose-400' }, { label: 'EXACT', value: counts.E, color: 'bg-amber-300' }]} /></section>
      <div className="grid gap-4 lg:grid-cols-2">
      <section className={panel}><h2 className="font-semibold">All-round shares</h2><div className="mt-3 h-72"><ResponsiveContainer width="100%" height="100%"><PieChart>
        <Pie data={chart} dataKey="value" nameKey="name" outerRadius={100} label>{chart.map(x => <Cell key={x.name} fill={x.color} />)}</Pie><Tooltip /><Legend /></PieChart></ResponsiveContainer></div></section>
      <section className={panel}><h2 className="font-semibold">Counts and rates</h2><Table columns={[
        { label: 'Outcome', key: 'name' }, { label: 'Count', render: r => r.value.toLocaleString() },
        { label: 'Share of all rounds', render: r => <RateMeter label="Share" value={r.value / Math.max(1, data.round_count)} /> },
      ]} rows={chart} rowKey={r => r.name} /><p className="mt-3 text-sm text-zinc-400">HIGH among non-EXACT: {percent(data.high_rate_nonexact)}</p>
        <div className="mt-4 grid grid-cols-2 gap-3 text-sm"><div><span className="text-zinc-500">Cumulative mean</span><p>{number(data.cumulative_summary?.mean_multiplier, 4)}x</p></div>
          <div><span className="text-zinc-500">Log volatility</span><p>{number(data.cumulative_summary?.log_volatility_sample, 4)}</p></div>
          <div><span className="text-zinc-500">Minimum / maximum</span><p>{data.cumulative_summary?.minimum_multiplier ?? '—'}x / {data.cumulative_summary?.maximum_multiplier ?? '—'}x</p></div>
          <div><span className="text-zinc-500">Cumulative rounds</span><p>{data.cumulative_summary?.round_count?.toLocaleString() ?? '—'}</p></div></div>
        <h3 className="mt-4 text-sm font-medium">Cumulative multiplier tails</h3><Table columns={[
          { label: 'At least', render: r => `${r.threshold}x` }, { label: 'Count', key: 'count' },
          { label: 'Rate', render: r => percent(r.rate) },
        ]} rows={Object.entries(data.cumulative_summary?.tail_counts || {}).map(([key, count]) => ({ threshold: key.replace('ge_', ''), count, rate: data.cumulative_summary?.tail_rates?.[key] }))} rowKey={r => r.threshold} /></section>
      <section className={panel}><h2 className="font-semibold">Frozen Phase 1 multiplier distribution</h2>
        <p className="mt-1 text-xs text-zinc-500">Full-history distribution and quantiles at the 2026-11-13 08:30 UTC research snapshot.</p>
        <details className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-xs font-medium text-zinc-400">View distribution details</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{JSON.stringify(data.phase1_snapshot_distribution || {}, null, 2)}</pre></details></section>
      <section className={`${panel} lg:col-span-2`}><h2 className="font-semibold">Rolling windows</h2><WindowTable windows={data.rolling_windows} /></section>
      </div>
    </div>;
  }} />;
}

function Motifs() {
  const [selectedMotif, setSelectedMotif] = useState('');
  return <ShellPage title="Historical motif explorer" subtitle="Phase 1 n-grams and correction evidence paired with current rolling recurrence." path="/sequences" refreshMs={15000} render={data => {
    const rows = (data.historical_motifs || []).map(m => ({ ...m, current: data.motif_counts_last_500?.[m.motif]?.count_last_500 ?? 0 }))
      .sort((a, b) => a.length - b.length || b.count - a.count);
    const corrected = rows.filter(row => /correct|significant|passed/i.test(String(row.evidence_status || ''))).length;
    const topRows = [...rows].sort((a, b) => b.count - a.count).slice(0, 12);
    return <div className="space-y-4"><div className="grid gap-3 sm:grid-cols-3"><Metric label="Motifs analyzed" value={rows.length} detail="Binary H/L patterns" />
      <Metric label="Latest rolling count" value={rows.reduce((sum, row) => sum + Number(row.current || 0), 0).toLocaleString()} detail="Occurrences in latest 500 outcomes" />
      <Metric label="Corrected evidence labels" value={corrected} detail="Based on API status labels" /></div>
      <section className={panel}><p className="text-sm leading-6 text-zinc-300">The historical motif analysis tests patterns against local-block shuffles and corrects for multiple comparisons. Recurrence describes structure; it does not establish predictive power.</p></section>
      <section className={panel}><h2 className="font-semibold">Most frequent historical patterns</h2><div className="mt-3 h-64"><ResponsiveContainer width="100%" height="100%"><BarChart data={topRows} margin={{ bottom: 24 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#27272a" /><XAxis dataKey="motif" stroke="#a1a1aa" angle={-35} textAnchor="end" interval={0} height={55} /><YAxis stroke="#71717a" /><Tooltip /><Bar dataKey="count" name="Historical occurrences" fill="#38bdf8" radius={[4, 4, 0, 0]} /></BarChart></ResponsiveContainer></div></section>
      <Table columns={[
        { label: 'Pattern', key: 'motif' }, { label: 'Length', key: 'length' },
        { label: 'Historical occurrences', key: 'count' }, { label: 'Latest 500', key: 'current' },
        { label: 'Shuffle p', render: r => number(r.shuffle_p_value, 4) }, { label: 'BH q', render: r => number(r.bh_q_value, 4) },
        { label: 'Status', key: 'evidence_status' },
        { label: 'Sequence details', render: r => <button className="text-emerald-300 underline" onClick={() => setSelectedMotif(r.motif)}>Inspect</button> },
      ]} rows={rows} rowKey={r => r.motif} />
      {selectedMotif && <MotifDetail motif={selectedMotif} onClose={() => setSelectedMotif('')} />}
    </div>;
  }} />;
}

function MotifDetail({ motif, onClose }) {
  const { data, error } = useBehaviourData(`/motifs/${encodeURIComponent(motif)}`, 0);
  const timeRows = data?.research_details?.time_distribution?.groups || [];
  const nextRows = data?.research_details?.subsequent_symbol_distribution?.rows || [];
  return <section className={`${panel} space-y-4`}>
    <div className="flex items-center justify-between"><h2 className="font-semibold">Motif {motif} historical evidence</h2>
      <button className="rounded border border-zinc-700 px-3 py-1 text-sm" onClick={onClose}>Close</button></div>
    {error && <p role="alert" className="text-amber-300">{error}</p>}
    {data && <>
      <p className="text-sm text-zinc-400">Frequency, time-group recurrence, and subsequent symbols are retrospective descriptions. They are not forecast signals.</p>
      <div className="grid gap-3 sm:grid-cols-3"><Metric label="Historical occurrences" value={data.count?.toLocaleString() ?? '—'} />
        <Metric label="Nonzero time groups" value={`${data.research_details?.time_distribution?.nonzero_group_count ?? 0}/${data.research_details?.time_distribution?.total_groups ?? 0}`} />
        <Metric label="BH corrected q" value={number(data.bh_q_value, 5)} /></div>
      <Table columns={[
        { label: 'Time group', key: 'group_number' }, { label: 'Occurrences', key: 'occurrences' },
        { label: 'Rounds', key: 'rounds' }, { label: 'Start', render: r => timestamp(r.start_at) },
        { label: 'End', render: r => timestamp(r.end_at) }, { label: 'Timestamp quality', key: 'timestamp_quality' },
      ]} rows={timeRows} rowKey={r => `${r.time_group_id}:${r.group_number}`} />
      <h3 className="font-medium">Historical next-symbol distribution</h3>
      <Table columns={[{ label: 'Next symbol', key: 'next' }, { label: 'Count', key: 'count' },
        { label: 'Context support', key: 'support' }, { label: 'Conditional frequency', render: r => percent(r.conditional_probability) },
      ]} rows={nextRows} rowKey={r => r.next} />
    </>}
  </section>;
}

function Rules() {
  return <ShellPage title="Rules and evidence" subtitle="Inspectable deterministic calculations; all active conditions are descriptive or baseline references." path="/rules" refreshMs={10000} render={data => {
    const rules = data.items || [];
    const statuses = rules.reduce((counts, rule) => { const key = rule.validation_status || 'Unknown'; counts[key] = (counts[key] || 0) + 1; return counts; }, {});
    const categories = new Set(rules.map(rule => rule.category).filter(Boolean)).size;
    return <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3"><Metric label="Registered rules" value={rules.length} detail="Inspectable calculations" />
        <Metric label="Categories" value={categories} detail="Rule groups" />
        <Metric label="Validation statuses" value={Object.keys(statuses).length} detail={Object.entries(statuses).map(([key, count]) => `${key}: ${count}`).join(' · ') || 'No rules reported'} /></div>
      <div className="grid gap-3">{rules.map(rule => <details key={`${rule.rule_id}:${rule.version}`} className={`${panel} group`}>
        <summary className="cursor-pointer list-none"><div className="flex flex-wrap items-center justify-between gap-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="text-[10px] font-bold uppercase tracking-widest text-sky-300">{rule.category || 'Rule'}</span><span className="text-xs text-zinc-600">v{rule.version}</span></div>
          <p className="mt-1 text-base font-semibold text-zinc-100">{rule.name}</p><p className="mt-1 break-all font-mono text-[11px] text-zinc-500">{rule.rule_id}</p></div>
          <div className="flex items-center gap-2"><StatusBadge value={rule.validation_status} /><span className="text-zinc-600 transition-transform group-open:rotate-180">⌄</span></div></div></summary>
        <div className="mt-4 space-y-3 border-t border-zinc-800 pt-4 text-sm"><p className="leading-6 text-zinc-300">{rule.description}</p>
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3"><p className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Calculation</p><p className="mt-1 break-words font-mono text-xs leading-5 text-emerald-100">{rule.formula || 'No formula supplied.'}</p></div>
          <div className="grid gap-3 md:grid-cols-2"><div className="rounded-lg bg-zinc-950/50 p-3"><p className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Inputs</p><pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{JSON.stringify(rule.input_features, null, 2)}</pre></div>
            <div className="rounded-lg bg-zinc-950/50 p-3"><p className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Thresholds / minimum evidence</p><pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{JSON.stringify({ thresholds: rule.thresholds, minimum: rule.minimum_evidence }, null, 2)}</pre></div></div>
          <details className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-xs font-semibold text-zinc-400">Latest evaluation details</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{JSON.stringify(rule.latest_evaluation || {}, null, 2)}</pre></details>
        </div>
      </details>)}</div>
    </div>;
  }} />;
}

function ForecastHistoryPage() {
  return <ShellPage title="Prediction history and evaluation" subtitle="Chronological baseline snapshots and disjoint outcomes as they mature." path="/forecasts?limit=250" refreshMs={10000} render={data => {
    const items = data.items || [];
    const newest = items[0]?.forecast || {};
    const points = [...items].reverse().map(row => ({ position: row.latest_position, high: Number(row.forecast?.expected_high_count || 0), low: Number(row.forecast?.expected_low_count || 0), exact: Number(row.forecast?.expected_exact_count || 0) }));
    return <div className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Metric label="Forecast snapshots" value={data.total ?? items.length} detail="Saved assessment history" />
      <Metric label="Latest expected H / L / E" value={`${number(newest.expected_high_count, 1)} / ${number(newest.expected_low_count, 1)} / ${number(newest.expected_exact_count, 1)}`} detail="Expected counts in 100 rounds" />
      <Metric label="Historical blocks" value={newest.historical_block_count ?? newest.historical_count_distribution?.historical_windows ?? 0} detail={newest.data_sufficiency || 'Latest reference'} />
      <Metric label="Latest state" value={newest.state || '—'} detail="State attached to newest assessment" /></div>
    <section className={panel}><div className="mb-1 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Expected counts over forecast history</h2><span className="text-xs text-zinc-500">Each point is a saved assessment</span></div><div className="mt-3 h-64"><ResponsiveContainer width="100%" height="100%"><AreaChart data={points}>
      <CartesianGrid strokeDasharray="3 3" stroke="#27272a" /><XAxis dataKey="position" stroke="#71717a" /><YAxis stroke="#71717a" /><Tooltip /><Legend />
      <Area type="monotone" dataKey="high" name="HIGH" stackId="1" stroke="#34d399" fill="#34d399" fillOpacity={0.22} />
      <Area type="monotone" dataKey="low" name="LOW" stackId="1" stroke="#fb7185" fill="#fb7185" fillOpacity={0.2} />
      <Area type="monotone" dataKey="exact" name="EXACT" stackId="1" stroke="#fbbf24" fill="#fbbf24" fillOpacity={0.45} />
    </AreaChart></ResponsiveContainer></div></section>
    <Table columns={[
      { label: 'Assessed', render: r => timestamp(r.assessed_at) }, { label: 'Position', key: 'latest_position' },
      { label: 'Expected H / L / E', render: r => `${number(r.forecast?.expected_high_count, 2)} / ${number(r.forecast?.expected_low_count, 2)} / ${number(r.forecast?.expected_exact_count, 2)}` },
      { label: 'Blocks', render: r => r.forecast?.historical_block_count ?? 0 },
      { label: 'State', render: r => <StatusBadge value={r.forecast?.state} /> },
      { label: 'Change', render: r => <details><summary className="cursor-pointer text-xs text-sky-300">View change</summary><pre className="mt-1 max-w-xs overflow-auto whitespace-pre-wrap text-[11px] text-zinc-400">{JSON.stringify(r.change_from_previous, null, 2)}</pre></details> },
    ]} rows={data.items || []} rowKey={r => r.id} />
    <div className="mt-5"><Evaluations /></div>
    </div>;
  }} />;
}

function Evaluations() {
  const { data, error } = useBehaviourData('/evaluations?limit=200', 10000);
  const items = data?.items || [];
  const averageError = items.length ? items.reduce((sum, row) => sum + Number(row.metrics?.high_rate_absolute_error || 0), 0) / items.length : null;
  return <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><h2 className="font-semibold">Matured disjoint forecast evaluations</h2><p className="mt-1 text-xs text-zinc-500">Scores use completed, non-overlapping outcomes</p></div><StatusBadge value={items.length ? `${items.length} evaluated` : 'Awaiting data'} /></div>
    {error && <p className="text-sm text-amber-300">{error}</p>}
    <div className="mb-3 grid gap-3 sm:grid-cols-3"><Metric label="Matured forecasts" value={items.length} detail="Disjoint evaluation records" />
      <Metric label="Mean absolute H-rate error" value={averageError === null ? '—' : number(averageError, 5)} detail="Across available evaluations" />
      <Metric label="Latest evaluation" value={items[0]?.evaluated_at ? shortTime(items[0].evaluated_at) : '—'} detail={items[0]?.evaluated_at ? timestamp(items[0].evaluated_at) : 'No evaluation yet'} /></div>
    <Table columns={[
      { label: 'Evaluated', render: r => timestamp(r.evaluated_at) }, { label: 'Forecast ID', key: 'forecast_id' },
      { label: 'End position', key: 'horizon_end_position' },
      { label: 'Observed H/L/E', render: r => `${r.outcome?.high_count ?? 0} / ${r.outcome?.low_count ?? 0} / ${r.outcome?.exact_count ?? 0}` },
      { label: 'Squared error', render: r => number(r.metrics?.high_rate_squared_error, 5) },
      { label: 'Absolute error', render: r => number(r.metrics?.high_rate_absolute_error, 5) },
    ]} rows={data?.items || []} rowKey={r => r.id} />
  </section>;
}

function HealthPage() {
  return <ShellPage title="Collector and system health" subtitle="Independent Behaviour API, database, collector and analysis worker status." path="/collector" refreshMs={3000} render={data => {
    const health = data.health || {};
    const entries = Object.entries(health);
    const healthyCount = entries.filter(([, item]) => /healthy|running|ok/i.test(String(item?.state || item?.status || ''))).length;
    const componentLabel = key => key.replaceAll('_', ' ');
    const summaryFields = detail => Object.entries(detail || {}).filter(([key, value]) => key !== 'error_code' && value !== null && typeof value !== 'object').slice(0, 4);
    return <div className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-3"><Metric label="Healthy components" value={`${healthyCount} / ${entries.length}`} detail="Independent service checks" />
      <Metric label="Recent saved readings" value={data.latest_readings?.length ?? 0} detail="Latest collector records" />
      <Metric label="Recent error events" value={data.recent_errors?.length ?? 0} detail="Collector and API diagnostics" /></div>
    <section className={panel}><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Independent components</h2><span className="text-xs text-zinc-500">Live health summary</span></div>
      <div className="grid gap-3 md:grid-cols-2">{entries.map(([key, item]) => {
        const state = item?.state || item?.status || (item?.healthy === true ? 'HEALTHY' : 'UNKNOWN');
        const details = summaryFields(item?.details);
        return <div key={key} className="rounded-xl border border-zinc-800 bg-zinc-950/50 p-4"><div className="flex flex-wrap items-start justify-between gap-2"><h3 className="text-sm font-semibold capitalize text-zinc-100">{componentLabel(key)}</h3><StatusBadge value={state} /></div>
          {details.length > 0 && <div className="mt-3 grid grid-cols-2 gap-2">{details.map(([field, value]) => <div key={field} className="rounded-lg bg-zinc-900/80 p-2"><p className="text-[10px] uppercase tracking-wide text-zinc-500">{componentLabel(field)}</p><p className="mt-1 break-words text-xs font-medium text-zinc-200">{String(value)}</p></div>)}</div>}
          {item?.checked_at && <p className="mt-3 text-[11px] text-zinc-500">Checked {timestamp(item.checked_at)}</p>}
          <details className="mt-3"><summary className="cursor-pointer text-[11px] text-sky-300">Technical details</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap text-[11px] text-zinc-400">{JSON.stringify(item, null, 2)}</pre></details>
        </div>;
      })}</div></section>
    <section className={panel}><h2 className="font-semibold">Recent Orbit readings</h2><Table columns={[
      { label: 'Watch', key: 'watch_id' }, { label: 'Reading', key: 'reading_id' },
      { label: 'Checked at', render: r => timestamp(r.checked_at) }, { label: 'Received at', render: r => timestamp(r.received_at) },
      { label: 'Status', render: r => <StatusBadge value={r.status} /> }, { label: 'Error', render: r => <span className="text-xs text-amber-200">{r.validation_error || '—'}</span> },
    ]} rows={data.latest_readings || []} rowKey={r => `${r.watch_id}:${r.reading_id}`} /></section>
    <section className={panel}><h2 className="font-semibold">Recent API and collector errors</h2><Table columns={[
      { label: 'Time', render: r => timestamp(r.occurred_at) }, { label: 'Component', key: 'component' },
      { label: 'Error', render: r => <StatusBadge value={r.error_code} /> }, { label: 'Details', render: r => <details><summary className="cursor-pointer text-xs text-sky-300">Inspect</summary><pre className="mt-1 max-w-sm overflow-auto whitespace-pre-wrap text-[11px] text-zinc-400">{JSON.stringify(r.details, null, 2)}</pre></details> },
    ]} rows={data.recent_errors || []} /></section>
    </div>;
  }} />;
}

export default function BehaviourDetection() {
  const location = useLocation();
  const active = useMemo(() => location.pathname.replace(/^\/behaviour\/?/, '').split('/')[0] || 'overview', [location.pathname]);
  const section = active === 'rounds' ? 'live' : active === 'groups' ? 'groups' : active;
  return <div className="space-y-5">
    <div className="border-b border-zinc-800 pb-3">
      <nav className="flex gap-2 overflow-x-auto pb-1">
        {links.map(([path, label]) => <NavLink key={path} to={path === 'overview' ? '/behaviour' : `/behaviour/${path}`}
          className={() => `whitespace-nowrap rounded-lg border px-3 py-2 text-xs font-medium ${section === path ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-zinc-800 text-zinc-400 hover:bg-zinc-900'}`}>
          {label}</NavLink>)}
      </nav>
    </div>
    <HybridOverview compact={active !== 'overview'} />
    <Routes>
      <Route index element={<Overview />} /><Route path="overview" element={<Overview />} />
      <Route path="live" element={<LiveRounds />} /><Route path="rounds" element={<LiveRounds />} />
      <Route path="state" element={<StatePage />} /><Route path="next100" element={<Next100 />} />
      <Route path="sequences" element={<Sequences />} /><Route path="outlook" element={<Sequences outlookOnly />} />
      <Route path="time" element={<TimeGroups />} /><Route path="groups" element={<TimeGroups explore />} />
      <Route path="distribution" element={<Distribution />} /><Route path="motifs" element={<Motifs />} />
      <Route path="rules" element={<Rules />} /><Route path="evaluation" element={<ForecastHistoryPage />} />
      <Route path="health" element={<HealthPage />} />
      <Route path="*" element={<Overview />} />
    </Routes>
    <p className="text-xs text-zinc-600">Research snapshot: 2026-11-13 08:30 UTC. Statistical assessments describe distributions, not exact future multipliers.</p>
  </div>;
}
