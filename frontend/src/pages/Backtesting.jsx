import { useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import { listBacktests, runBacktest } from '../services/api.js';

export default function Backtesting() {
  const [runs, setRuns] = useState([]); const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(null);
  const [profile, setProfile] = useState('PROFILE_A'); const [balance, setBalance] = useState(1000);
  async function refresh() { try { const rows = await listBacktests(); setRuns(rows); if (!selected && rows[0]) setSelected(rows[0]); } catch (e) { setError(e.message); } }
  useEffect(() => { refresh(); }, []);
  async function run() { setBusy(true); setError(null); try { const result = await runBacktest({ profile, starting_balance: Number(balance), min_history: 100 }); setSelected(result); await refresh(); } catch (e) { setError(e?.response?.data?.detail || e.message); } finally { setBusy(false); } }
  const summary = selected?.summary || {};
  return <div className="space-y-5"><div><h1 className="text-3xl font-semibold">Backtesting</h1><p className="mt-1 text-sm text-zinc-400">Historical, chronological simulation only. Results are not guarantees of future performance.</p></div>
    <Card title="Run historical simulation"><div className="flex flex-wrap items-end gap-3"><label className="text-sm">Profile<select value={profile} onChange={e => setProfile(e.target.value)} className="ml-2 rounded bg-zinc-900 p-2"><option>PROFILE_A</option><option>PROFILE_B</option></select></label><label className="text-sm">Starting balance<input type="number" value={balance} onChange={e => setBalance(e.target.value)} className="ml-2 w-28 rounded bg-zinc-900 p-2" /></label><button disabled={busy} onClick={run} className="rounded bg-emerald-600 px-4 py-2 text-sm">{busy ? 'Running…' : 'Run'}</button></div>{error && <p className="mt-3 text-sm text-rose-300">{error}</p>}</Card>
    {selected && <><section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[['Run', selected.run_id],['Bets',summary.bets],['P/L',summary.pnl],['ROI',summary.roi == null ? 'UNKNOWN' : `${(summary.roi * 100).toFixed(2)}%`],['Wins',summary.wins],['Losses',summary.losses],['Drawdown',summary.max_drawdown],['Win rate',summary.win_rate == null ? 'UNKNOWN' : `${(summary.win_rate * 100).toFixed(1)}%`]].map(([k,v]) => <Card key={k}><div className="text-xs uppercase text-zinc-500">{k}</div><div className="mt-1 text-lg font-semibold">{v == null ? 'UNKNOWN' : v}</div></Card>)}</section><Card title="Prediction quality (separate from profitability)"><div className="grid gap-2 sm:grid-cols-3"><span>Accuracy: {selected.prediction_metrics.accuracy ?? 'UNKNOWN'}</span><span>Brier: {selected.prediction_metrics.brier_score ?? 'UNKNOWN'}</span><span>Log loss: {selected.prediction_metrics.log_loss ?? 'UNKNOWN'}</span></div></Card><Card title="Equity / drawdown"><div className="max-h-56 overflow-auto text-xs">{(selected.equity_curve || []).slice(-100).map(p => <div key={p.round_id} className="grid grid-cols-4 border-b border-zinc-800 py-1"><span>{p.round_id}</span><span>{p.period}</span><span>{p.balance}</span><span>{p.drawdown}</span></div>)}</div></Card></>}
    <Card title="Previous runs"><div className="space-y-2">{runs.map(r => <button key={r.run_id} onClick={() => setSelected(r)} className="block w-full rounded bg-zinc-900 p-2 text-left text-xs">{r.run_id} · {r.config.profile} · {r.summary?.bets ?? 0} bets · {r.summary?.pnl ?? 0} P/L</button>)}</div></Card>
  </div>;
}
