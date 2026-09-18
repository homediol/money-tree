import { AlertTriangle, RefreshCw } from 'lucide-react';

export default function AppErrorFallback({ error, resetErrorBoundary }) {
  return (
    <main className="grid min-h-screen place-items-center bg-zinc-950 p-6 text-zinc-100">
      <section role="alert" className="w-full max-w-lg rounded-2xl border border-rose-500/30 bg-zinc-900 p-6 shadow-2xl">
        <AlertTriangle className="text-rose-400" size={32} />
        <h1 className="mt-4 text-2xl font-bold">The dashboard encountered an error</h1>
        <p className="mt-2 text-sm text-zinc-400">Your backend and betting session were not changed.</p>
        <pre className="mt-4 max-h-36 overflow-auto rounded bg-zinc-950 p-3 text-xs text-rose-200">{error?.message || 'Unknown frontend error'}</pre>
        <button onClick={resetErrorBoundary} className="mt-5 inline-flex items-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-zinc-950 hover:bg-emerald-300">
          <RefreshCw size={16} /> Try again
        </button>
      </section>
    </main>
  );
}
