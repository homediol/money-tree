import { useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import { getBalanceLedger, getExecutionDetail, getResultExecutions } from '../services/api.js';

const tone = (value) => value === 'WIN' || value === 'RECONCILED' || value === 'CLOSED'
  ? 'text-emerald-300' : value === 'LOSS' || value === 'FAILED'
    ? 'text-rose-300' : 'text-amber-300';

function statusOf(row) {
  return String(row?.status || row?.result || 'UNKNOWN').toUpperCase();
}

function acceptanceOf(row) {
  return row?.platform_acceptance || row?.acceptance_status || row?.transaction_acceptance || 'UNKNOWN';
}

function settlementOf(row) {
  return row?.settlement_status || row?.reconciliation_status || row?.balance_status || 'UNKNOWN';
}

export default function BetResults() {
  const [executions, setExecutions] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [detail, setDetail] = useState(null);

  async function refresh() {
    const [xs, ls] = await Promise.all([getResultExecutions(), getBalanceLedger()]);
    setExecutions(xs); setLedger(ls);
  }
  useEffect(() => { refresh(); const id = setInterval(refresh, 5000); return () => clearInterval(id); }, []);

  async function inspect(id) { setDetail(await getExecutionDetail(id)); }
  const reconciled = executions.filter((row) => settlementOf(row) === 'RECONCILED').length;
  const accepted = executions.filter((row) => ['ACCEPTED', 'PLACED', 'SETTLED', 'RECONCILED'].includes(acceptanceOf(row))).length;
  const unresolved = executions.length - reconciled;
  const summary = [
    ['Execution records', executions.length],
    ['Acceptance evidence', accepted],
    ['Settlement reconciled', reconciled],
    ['Unresolved records', unresolved],
  ];

  return <div className="space-y-5">
    <div><h1 className="text-3xl font-semibold">Settlement and execution records</h1>
      <p className="mt-1 text-sm text-zinc-400">Read-only transaction and settlement evidence. No bankroll targets, growth totals, or automatic sizing are calculated here.</p></div>
    {!executions.length && <p role="status" className="rounded border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">No execution records are available. Unknown acceptance or settlement remains unresolved.</p>}
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{summary.map(([label, value]) =>
      <Card key={label}><div className="text-xs uppercase text-zinc-500">{label}</div><div className="mt-1 text-xl font-semibold">{value}</div></Card>)}</section>
    <Card title="Recent execution evidence"><div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="text-left text-zinc-500"><th>Decision / target</th><th>Result</th><th>Multiplier</th><th>Acceptance</th><th>Settlement</th></tr></thead><tbody>
      {executions.map((row) => <tr key={row.execution_id} onClick={() => void inspect(row.execution_id)} className="cursor-pointer border-t border-zinc-800 hover:bg-zinc-900"><td className="py-2">{row.decision_id}<div className="text-xs text-zinc-500">target {row.target_round_id || 'UNKNOWN'}</div></td><td className={tone(statusOf(row))}>{statusOf(row)}</td><td>{row.result_multiplier == null ? 'UNKNOWN' : `${row.result_multiplier}x`}</td><td>{acceptanceOf(row)}</td><td>{settlementOf(row)}</td></tr>)}
    </tbody></table></div></Card>
    {detail && <Card title="Execution detail timeline"><div className="mb-3 text-sm">{detail.execution.execution_id} · <span className={tone(detail.execution.status)}>{detail.execution.status}</span></div><div className="space-y-2">{detail.timeline.map((event, index) => <div key={`${event.recorded_at}-${index}`} className="flex gap-3 rounded bg-zinc-900 p-2 text-sm"><span className={tone(event.state)}>{event.state}</span><span className="text-zinc-500">{new Date(event.recorded_at).toLocaleString()}</span></div>)}</div></Card>}
    <Card title="Settlement integrity ledger"><p className="mb-3 text-xs text-zinc-400">Immutable reconciliation evidence is retained for audit. This view does not calculate bankroll growth or policy thresholds.</p><div className="space-y-2">{ledger.slice(0, 20).map((event) => <div key={event.ledger_id} className="grid grid-cols-3 rounded bg-zinc-900 p-2 text-xs"><span>{event.event_type}</span><span>{event.balance_status || 'UNKNOWN'}</span><span className="text-right text-zinc-500">{event.recorded_at || 'timestamp unavailable'}</span></div>)}{!ledger.length && <p className="text-sm text-zinc-500">No settlement ledger entries are available.</p>}</div></Card>
  </div>;
}
