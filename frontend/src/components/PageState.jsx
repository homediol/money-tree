import { AlertCircle, Inbox, LoaderCircle, RefreshCw } from 'lucide-react';
import { useBackendConnection } from '../services/backendConnection.js';

export function LoadingState({ label = 'Loading data…' }) {
  return <div className="grid min-h-[320px] place-items-center"><div className="flex items-center gap-3 text-sm text-zinc-400"><LoaderCircle className="animate-spin text-emerald-400" size={20} />{label}</div></div>;
}

export function ErrorState({ message, onRetry }) {
  const backend = useBackendConnection();
  if (backend.status === 'checking' || backend.status === 'reconnecting') {
    return (
      <div role="status" className="inline-flex items-center gap-2 rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
        <RefreshCw className="animate-spin" size={15} /> Reconnecting…
      </div>
    );
  }
  return (
    <div role="alert" className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-5 text-rose-100">
      <div className="flex items-start gap-3"><AlertCircle className="mt-0.5 shrink-0" size={20} /><div><div className="font-semibold">Unable to load this page</div><div className="mt-1 text-sm text-rose-200/80">{message}</div></div></div>
      {onRetry && <button onClick={onRetry} className="mt-4 inline-flex items-center gap-2 rounded-lg border border-rose-300/30 px-3 py-2 text-sm font-semibold hover:bg-rose-400/10"><RefreshCw size={15} />Retry</button>}
    </div>
  );
}

export function EmptyState({ title = 'No data yet', description }) {
  return <div className="grid min-h-40 place-items-center rounded-xl border border-dashed border-zinc-700 bg-zinc-950/40 p-6 text-center"><div><Inbox className="mx-auto text-zinc-600" /><div className="mt-3 font-semibold text-zinc-300">{title}</div>{description && <p className="mt-1 text-sm text-zinc-500">{description}</p>}</div></div>;
}
