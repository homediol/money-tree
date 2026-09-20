import { useEffect, useState } from 'react';
import { api } from '../services/api.js';

export default function Recovery() {
  const [status, setStatus] = useState(null); const [message, setMessage] = useState('');
  const refresh = async () => { try { setStatus((await api.get('/api/recovery/status')).data); } catch (e) { setMessage(e.message); } };
  useEffect(() => { refresh(); }, []);
  async function action(path) { try { const r = await api.post(`/api/recovery/${path}`, { reason: 'operator_requested' }); setMessage(JSON.stringify(r.data)); await refresh(); } catch (e) { setMessage(e.response?.data?.error || e.message); } }
  return <section className="space-y-5"><h1 className="text-3xl font-semibold">Disaster Recovery</h1><p className="text-sm text-zinc-400">Recovery remains fail-closed. Unknown executions are preserved until verified.</p><div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5"><div className="text-xl font-bold text-amber-300">{status?.mode || 'UNKNOWN'}</div><div className="mt-2 text-sm">Open executions: {status?.open_executions ?? 'UNKNOWN'}</div><pre className="mt-3 max-h-64 overflow-auto text-xs text-zinc-400">{JSON.stringify(status?.last_recovery || {}, null, 2)}</pre></div><div className="flex flex-wrap gap-2"><button onClick={()=>action('health-check')} className="rounded bg-sky-700 px-3 py-2">Health check</button><button onClick={()=>action('integrity')} className="rounded bg-zinc-700 px-3 py-2">Integrity check</button><button onClick={()=>action('recover')} className="rounded bg-amber-700 px-3 py-2">Recover safely</button></div>{message && <pre className="rounded bg-zinc-950 p-3 text-xs text-zinc-300">{message}</pre>}</section>;
}
