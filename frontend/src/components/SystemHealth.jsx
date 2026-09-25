import { useEffect, useRef } from 'react';
import { CloudOff, RefreshCw, Server } from 'lucide-react';
import { toast } from 'sonner';
import { useBackendConnection } from '../services/backendConnection.js';

export default function SystemHealth() {
  const connection = useBackendConnection();
  const previousStatus = useRef(connection.status);

  useEffect(() => {
    if (previousStatus.current === 'reconnecting'
        && (connection.status === 'online' || connection.status === 'degraded')) {
      toast.success('Backend connection restored');
    }
    previousStatus.current = connection.status;
  }, [connection.status]);

  if (connection.status === 'online') {
    return (
      <div className="fixed bottom-4 right-4 z-40 hidden items-center gap-2 rounded-full border border-emerald-500/30 bg-zinc-950/90 px-3 py-2 text-xs font-semibold text-emerald-300 shadow-xl backdrop-blur sm:flex">
        <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
        API connected
      </div>
    );
  }

  if (connection.status === 'degraded') {
    return (
      <div role="status" className="fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-full border border-amber-500/40 bg-zinc-950/95 px-3 py-2 text-xs font-semibold text-amber-200 shadow-xl backdrop-blur">
        <CloudOff size={14} />
        Connected · database degraded
      </div>
    );
  }

  return (
    <div role="status" aria-live="polite" title={connection.message || ''} className="fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-full border border-amber-500/40 bg-zinc-950/95 px-3 py-2 text-xs font-semibold text-amber-100 shadow-xl backdrop-blur">
      <RefreshCw size={14} className="animate-spin" />
      <span>{connection.status === 'checking' ? 'Connecting…' : 'Reconnecting…'}</span>
      {connection.attempt > 1 && <span className="text-amber-300/70">#{connection.attempt}</span>}
    </div>
  );
}

export function ApiBadge() {
  return <Server size={14} aria-hidden="true" />;
}
