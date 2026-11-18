import RulePerformancePanel from '../components/RulePerformancePanel.jsx';

export default function RulePerformancePage() {
  return <main className="space-y-5">
    <header className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-5">
      <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-300">Winner Predict · Analysis</p>
      <h1 className="mt-1 text-2xl font-bold text-zinc-100">Rule Performance</h1>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-400">
        See every registered Winner Predict rule, including rules that are Disabled or not configured. Compare verified SHADOW wins and losses across saved windows.
      </p>
    </header>
    <RulePerformancePanel />
  </main>;
}
