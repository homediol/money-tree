import { useEffect, useRef, useState } from 'react';
import { CloudOff, RefreshCw, Server } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '../services/api.js';

export default function SystemHealth() {
  const [health, setHealth] = useState({ checking: true, online: false });
  const wasOnline = useRef(null);

  useEffect(() => {
    let active = true;
    let timer;

    async function check() {
      try {
        const { data } = await api.get('/health', { timeout: 5000 });
        if (!active) return;
        const online = data?.ok === true;
        if (wasOnline.current === false && online) toast.success('Backend connection restored');
        wasOnline.current = online;
        setHealth({ checking: false, online });
      } catch (error) {
        if (!active) return;
        wasOnline.current = false;
        setHealth({ checking: false, online: false, message: error?.message || 'Connection failed' });
      }
    }

    check();
    timer = window.setInterval(check, 15000);
    window.addEventListener('online', check);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener('online', check);
    };
  }, []);

  if (health.online) {
    return (
      <div className="fixed bottom-4 right-4 z-40 hidden items-center gap-2 rounded-full border border-emerald-500/30 bg-zinc-950/90 px-3 py-2 text-xs font-semibold text-emerald-300 shadow-xl backdrop-blur sm:flex">
        <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
        API connected
      </div>
    );
  }

  return (
    <div role="alert" className="sticky top-0 z-50 flex items-center justify-center gap-3 border-b border-rose-500/40 bg-rose-950/95 px-4 py-2 text-sm text-rose-100 backdrop-blur">
      {health.checking ? <RefreshCw size={16} className="animate-spin" /> : <CloudOff size={16} />}
      <span>{health.checking ? 'Checking backend…' : 'Backend unavailable on port 8000'}</span>
      {!health.checking && (
        <button onClick={() => window.location.reload()} className="min-h-0 rounded border border-rose-300/30 px-2 py-1 text-xs font-semibold hover:bg-rose-400/10">
          Retry
        </button>
      )}
    </div>
  );
}

export function ApiBadge() {
  return <Server size={14} aria-hidden="true" />;
}
