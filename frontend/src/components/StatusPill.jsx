export default function StatusPill({ status }) {
  const good = status?.includes('STRONG') || status?.includes('MODERATE') || status === 'VALIDATED';
  const warn = status?.includes('INSUFFICIENT') || status?.includes('WEAK');
  const cls = good ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' : warn ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' : 'border-rose-500/40 bg-rose-500/10 text-rose-200';
  return <span className={`inline-flex rounded border px-2.5 py-1 text-xs font-semibold ${cls}`}>{status || 'UNKNOWN'}</span>;
}

