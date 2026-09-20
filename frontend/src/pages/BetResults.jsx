import { useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import { getBalanceLedger, getExecutionDetail, getResultExecutions, getSessionMetrics } from '../services/api.js';

const tone = (value) => value === 'WIN' || value === 'RECONCILED' || value === 'CLOSED'
  ? 'text-emerald-300' : value === 'LOSS' || value === 'FAILED'
    ? 'text-rose-300' : 'text-amber-300';

export default function BetResults() {
  const [executions, setExecutions] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [metrics, setMetrics] = useState({});
  const [detail, setDetail] = useState(null);

  async function refresh() {
    const [xs, ls, ms] = await Promise.all([getResultExecutions(), getBalanceLedger(), getSessionMetrics()]);
    setExecutions(xs); setLedger(ls); setMetrics(ms[0] || {});
  }
  useEffect(() => { refresh(); const id = setInterval(refresh, 5000); return () => clearInterval(id); }, []);

  async function inspect(id) { setDetail(await getExecutionDetail(id)); }
  const cards = [
    ['Starting Balance', metrics.starting_balance], ['Current Balance', metrics.current_balance],
    ['Goal', metrics.goal_balance], ['P/L', metrics.profit], ['Bets', metrics.total_bets ?? 0],
    ['Wins', metrics.wins ?? 0], ['Losses', metrics.losses ?? 0], ['Unknown', metrics.unknown ?? 0],
    ['Win Rate', metrics.win_rate == null ? '—' : `${(metrics.win_rate * 100).toFixed(1)}%`],
    ['Drawdown', metrics.drawdown ?? 0], ['Consecutive Losses', metrics.consecutive_losses ?? 0],
    ['Session', metrics.state || 'IDLE'],
  ];
  return <div className="space-y-5">
    <div><h1 className="text-3xl font-semibold">Bet Results / Session Analytics</h1>
      <p className="mt-1 text-sm text-zinc-400">Verified results only. Missing platform evidence remains UNKNOWN.</p></div>
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{cards.map(([k,v]) =>
      <Card key={k}><div className="text-xs uppercase text-zinc-500">{k}</div><div className="mt-1 text-xl font-semibold">{v ?? '—'}</div></Card>)}</section>
    <Card title="Recent executions"><div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="text-left text-zinc-500"><th>Decision / Round</th><th>Status</th><th>Requested</th><th>Actual</th><th>Balance</th></tr></thead><tbody>
      {executions.map(x => <tr key={x.execution_id} onClick={() => inspect(x.execution_id)} className="cursor-pointer border-t border-zinc-800 hover:bg-zinc-900"><td className="py-2">{x.decision_id}<div className="text-xs text-zinc-500">round {x.target_round_id}</div></td><td className={tone(x.result || x.status)}>{x.result || x.status}</td><td>{x.requested_bet_amount ?? x.bet_amount} @ {x.requested_cashout ?? x.cashout_target}x</td><td>{x.result_multiplier == null ? 'UNKNOWN' : `${x.result_multiplier}x`}</td><td>{x.balance_status || 'UNKNOWN'} · {x.balance_after ?? '—'}</td></tr>)}
    </tbody></table></div></Card>
    {detail && <Card title="Execution detail timeline"><div className="mb-3 text-sm">{detail.execution.execution_id} · <span className={tone(detail.execution.status)}>{detail.execution.status}</span></div><div className="space-y-2">{detail.timeline.map((e,i) => <div key={`${e.recorded_at}-${i}`} className="flex gap-3 rounded bg-zinc-900 p-2 text-sm"><span className={tone(e.state)}>{e.state}</span><span className="text-zinc-500">{new Date(e.recorded_at).toLocaleString()}</span></div>)}</div></Card>}
    <Card title="Immutable balance ledger"><div className="space-y-2">{ledger.slice(0,20).map(e => <div key={e.ledger_id} className="grid grid-cols-4 rounded bg-zinc-900 p-2 text-xs"><span>{e.event_type}</span><span>{e.amount ?? '—'}</span><span>{e.balance_status}</span><span>{e.reconciled_balance ?? 'UNKNOWN'}</span></div>)}</div></Card>
  </div>;
}
