import { Clock3 } from 'lucide-react';

export default function DataFreshness({ timestamp, records }) {
  const parsed = timestamp ? new Date(timestamp) : null;
  const valid = parsed && !Number.isNaN(parsed.getTime());
  const ageMs = valid ? Date.now() - parsed.getTime() : null;
  const stale = ageMs == null || ageMs > 10 * 60 * 1000;
  const label = valid ? parsed.toLocaleString() : 'No timestamp available';

  return (
    <div className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${stale ? 'border-amber-500/30 bg-amber-500/10 text-amber-200' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'}`}>
      <Clock3 size={14} />
      <span><strong>{stale ? 'Stored data' : 'Live data'}</strong> · latest {label}{records != null ? ` · ${records} records` : ''}</span>
    </div>
  );
}
