export default function Metric({ label, value, tone = 'default' }) {
  const tones = {
    default: 'text-zinc-100',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-rose-300',
  };
  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <div className={`mt-2 text-2xl font-bold tracking-tight ${tones[tone]}`}>{value}</div>
    </div>
  );
}
