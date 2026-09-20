import React, { useEffect, useState } from 'react';

const json = async (url, options) => (await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options })).json();

export default function LiveActivation() {
  const [status, setStatus] = useState(null); const [readiness, setReadiness] = useState(null); const [profile, setProfile] = useState('PROFILE_A');
  const [start, setStart] = useState(1000); const [goal, setGoal] = useState(1100); const [busy, setBusy] = useState(false);
  const refresh = async () => { const [s, r] = await Promise.all([json('/api/live/status'), json('/api/live/readiness', { method: 'POST', body: '{}' })]); setStatus(s); setReadiness(r); };
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);
  const action = async (path, body = {}) => { setBusy(true); try { await json(`/api/live/${path}`, { method: 'POST', body: JSON.stringify(body) }); await refresh(); } finally { setBusy(false); } };
  const mode = status?.mode || 'OBSERVING';
  const color = mode === 'LIVE_ACTIVE' ? 'text-red-300' : mode === 'EMERGENCY_STOP' ? 'text-red-500' : 'text-yellow-300';
  return <section className="space-y-5">
    <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-6"><div className={`text-2xl font-bold ${color}`}>● {mode}</div><p className="mt-2 text-sm text-zinc-400">Live activation is backend controlled. Shadow and live performance remain separate.</p></div>
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5"><h2 className="font-semibold">Production readiness</h2><div className="mt-4 space-y-2 text-sm">{Object.entries(readiness?.checks || {}).map(([k,v]) => <div key={k} className="flex justify-between"><span>{k}</span><b className={v === 'HEALTHY' || v === 'FRESH' || v === 'ENABLED' ? 'text-emerald-300' : 'text-red-300'}>{v}</b></div>)}</div>{(readiness?.reasons || []).map(x => <div key={x} className="mt-2 text-sm text-red-300">{x}</div>)}</div>
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5"><h2 className="font-semibold">Explicit activation</h2><div className="mt-4 grid gap-3"><select value={profile} onChange={e=>setProfile(e.target.value)} className="rounded bg-zinc-800 p-2"><option>PROFILE_A</option><option>PROFILE_B</option></select><input type="number" value={start} onChange={e=>setStart(+e.target.value)} className="rounded bg-zinc-800 p-2" placeholder="Starting balance"/><input type="number" value={goal} onChange={e=>setGoal(+e.target.value)} className="rounded bg-zinc-800 p-2" placeholder="Goal balance"/><button disabled={busy} onClick={()=>action('start',{confirmation:'ENABLE LIVE BETTING',profile,starting_balance:start,goal_balance:goal})} className="rounded bg-red-700 px-4 py-2 font-semibold">ENABLE LIVE BETTING</button></div><div className="mt-3 flex flex-wrap gap-2"><button onClick={()=>action('pause')} className="rounded bg-yellow-700 px-3 py-2">Pause</button><button onClick={()=>action('resume',{confirmation:'ENABLE LIVE BETTING'})} className="rounded bg-emerald-700 px-3 py-2">Resume</button><button onClick={()=>action('stop')} className="rounded bg-zinc-700 px-3 py-2">Stop</button><button onClick={()=>action('emergency-stop')} className="rounded bg-red-900 px-3 py-2">Emergency stop</button></div></div>
    </div>
  </section>;
}
