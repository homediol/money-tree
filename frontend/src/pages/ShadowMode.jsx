import { useCallback, useEffect, useRef, useState } from 'react';
import Card from '../components/Card.jsx';
import { api } from '../services/api.js';
import BettingModeControl from '../components/BettingModeControl.jsx';

export default function ShadowMode() {
  const [status, setStatus] = useState(null); const [trades, setTrades] = useState([]); const [error, setError] = useState(null); const [lastUpdated, setLastUpdated] = useState(null);
  const requestInFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    try {
      const [s, t] = await Promise.all([api.get('/api/shadow/status'), api.get('/api/shadow/trades')]);
      setStatus(s.data.status); setTrades(t.data.trades || []); setLastUpdated(Date.now()); setError(null);
    } catch (e) {
      if (e?.code !== 'ERR_BACKEND_RECONNECTING') setError(e.message || 'Shadow status is unavailable');
    } finally {
      requestInFlight.current = false;
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 10000);
    return () => window.clearInterval(id);
  }, [refresh]);

  async function action(name) {
    if (name === 'start') {
      setError('Start a Shadow session from the confirmed Manual/Automatic configuration above. No default balance or goal is used.');
      return;
    }
    try { await api.post(`/api/shadow/${name}`, {}); await refresh(); }
    catch (e) { setError(e?.response?.data?.detail?.message || e?.response?.data?.error || e.message); }
  }

  return <div className="space-y-5"><BettingModeControl /><div><h1 className="text-3xl font-semibold">System Shadow Mode</h1><p className="mt-1 text-sm text-zinc-400">SHADOW_REALISTIC uses real completed rounds and never clicks Bet/Cashout controls.</p></div>{error && <div className="rounded border border-amber-500/40 p-3 text-sm text-amber-200" role="status">Shadow data is STALE · last successful refresh: {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : 'UNKNOWN'} · {error}</div>}<section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[['Mode',status?.mode],['Balance',status?.current_balance],['Goal',status?.goal_balance],['P/L',status?.profit],['Bets',status?.bets],['Wins',status?.wins],['Losses',status?.losses],['ROI',status?.roi == null ? 'UNKNOWN' : `${(status.roi * 100).toFixed(1)}%`]].map(([k,v]) => <Card key={k}><div className="text-xs uppercase text-zinc-500">{k}</div><div className="mt-1 text-xl font-semibold">{v == null ? 'UNKNOWN' : v}</div></Card>)}</section><Card title="Shadow controls"><div className="flex flex-wrap gap-2"><button onClick={() => action('start')} className="rounded bg-emerald-600 px-3 py-2 text-sm">Configure / Start Shadow</button><button onClick={() => action('pause')} className="rounded bg-amber-600 px-3 py-2 text-sm">Pause</button><button onClick={() => action('resume')} className="rounded bg-sky-600 px-3 py-2 text-sm">Resume</button><button onClick={() => action('stop')} className="rounded bg-zinc-700 px-3 py-2 text-sm">Stop</button></div></Card><Card title="Paper trades"><div className="space-y-2">{trades.map(t => <div key={t.execution_id} className="grid grid-cols-5 rounded bg-zinc-900 p-2 text-xs"><span>{t.target_round_id}</span><span>{t.status}</span><span>{t.result || 'PENDING'}</span><span>{t.pnl ?? 'UNKNOWN'}</span><span>SHADOW</span></div>)}</div></Card></div>;
}
