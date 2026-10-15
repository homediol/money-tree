import { useCallback, useEffect, useState } from 'react';
import { ExternalLink, Gamepad2, KeyRound, MonitorCheck, RefreshCw, ShieldCheck } from 'lucide-react';
import Card from '../components/Card.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { api } from '../services/api.js';

const LOGIN_URL = 'https://winner.rw/en/authentication/login';
const AVIATOR_URL = 'https://winner.rw/en/virtual/crash-games/aviator';

function stateTone(value) {
  const state = String(value || 'UNKNOWN').toUpperCase();
  if (['VERIFIED', 'AUTHENTICATED', 'OPEN', 'READY', 'FRESH', 'CONNECTED', 'COLLECTING'].includes(state)) return 'text-emerald-300';
  if (['AUTH_REQUIRED', 'BLOCKED', 'STALE', 'OFFLINE', 'STOPPED', 'CLOSED', 'UNVERIFIED', 'NOT READY', 'NOT FOUND'].includes(state)) return 'text-amber-200';
  return 'text-zinc-300';
}

function Status({ label, value, detail }) {
  const display = value == null || value === '' ? 'UNKNOWN' : String(value).replaceAll('_', ' ');
  return <div className="flex items-start justify-between gap-4 border-b border-zinc-800 py-3 last:border-0">
    <div><div className="text-sm text-zinc-300">{label}</div>{detail && <div className="mt-1 text-xs text-zinc-500">{detail}</div>}</div>
    <div className={`text-right text-sm font-semibold ${stateTone(value)}`}>{display}</div>
  </div>;
}

function PlatformLink({ href, children, primary = false }) {
  return <a href={href} target="_blank" rel="noopener noreferrer"
    className={`inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold transition ${primary
      ? 'bg-emerald-400 text-zinc-950 hover:bg-emerald-300'
      : 'border border-zinc-700 bg-zinc-950/60 text-zinc-200 hover:border-zinc-500 hover:bg-zinc-900'}`}>
    {children}<ExternalLink size={15} />
  </a>;
}

export default function PlatformConnection() {
  const [snapshot, setSnapshot] = useState(null);
  const [browser, setBrowser] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    const [collectorResult, browserResult] = await Promise.allSettled([
      api.get('/api/history/status'),
      api.get('/api/betting/browser'),
    ]);
    setSnapshot(collectorResult.status === 'fulfilled' ? collectorResult.value.data || null : null);
    setBrowser(browserResult.status === 'fulfilled' ? browserResult.value.data?.browser || null : null);
    const failures = [collectorResult, browserResult].filter((result) => result.status === 'rejected');
    setError(failures.length
      ? 'Some platform evidence is unavailable; missing or stale session state remains unverified.'
      : '');
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const pages = browser?.browser_pages || {};
  const auth = browser?.authentication_state || snapshot?.authentication_state || 'UNKNOWN';
  const observedAt = snapshot?.last_observed_at || snapshot?.heartbeat_at || snapshot?.observed_at;
  const pageState = browser?.page_found === true ? 'OPEN'
    : browser?.page_found === false ? 'NOT FOUND'
      : pages?.aviator_page_state || 'UNKNOWN';
  const gameFrame = browser?.aviator_next_reachable === true ? 'READY'
    : browser?.aviator_next_reachable === false ? 'NOT READY'
      : pages?.game_frame_state || 'UNKNOWN';
  const mainAction = auth === 'AUTHENTICATED'
    ? <PlatformLink href={AVIATOR_URL} primary><Gamepad2 size={16} />Open Aviator</PlatformLink>
    : <PlatformLink href={LOGIN_URL} primary><KeyRound size={16} />Sign in to Winner.rw</PlatformLink>;

  return <div className="space-y-5">
    <PageHeader eyebrow="Component 1 · operator controlled" title="Login & Aviator"
      icon={KeyRound}
      description="Sign in to Winner.rw and open Aviator manually. This page reads the collector-owned browser session; it never handles platform credentials or changes stake, Auto Cashout, Auto Bet, or submits bets."
      action={<button type="button" onClick={() => void refresh()} disabled={loading}
        className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-900 disabled:opacity-50">
        <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />{loading ? 'Refreshing…' : 'Refresh status'}
      </button>} />

    <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4">
      <div className="flex items-start gap-3">
        <ShieldCheck size={18} className="mt-0.5 shrink-0 text-emerald-300" />
        <div>
          <h2 className="text-sm font-semibold text-emerald-100">Manual platform access</h2>
          <p className="mt-1 text-sm leading-6 text-zinc-300">These shortcuts open the official login and Aviator routes in another tab for manual use. They do not navigate or authenticate the collector-owned browser. The status below reports that browser only; if it remains unverified, complete login and navigation in its existing session.</p>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {mainAction}
        <PlatformLink href={AVIATOR_URL}><Gamepad2 size={16} />Aviator game</PlatformLink>
      </div>
    </div>

    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">{error} Unknown session state stays unverified.</div>}

    <section className="grid gap-5 lg:grid-cols-2">
      <Card title="Winner.rw login">
        <Status label="Authentication" value={auth}
          detail={snapshot?.logged_in === true ? 'Collector observed an authenticated platform session.' : snapshot?.logged_in === false ? 'Collector observed that the platform session is signed out.' : 'No current authenticated-session proof.'} />
        <Status label="Login requirement" value={snapshot?.auth_required === true ? 'AUTH_REQUIRED' : snapshot?.auth_required === false && auth === 'AUTHENTICATED' ? 'VERIFIED' : 'UNKNOWN'}
          detail={snapshot?.auth_required_reason || undefined} />
        <Status label="Platform site" value={snapshot?.site_status || 'UNKNOWN'} />
        <div className="mt-3 text-xs text-zinc-500">Credentials are entered directly on Winner.rw and are not collected or stored by this console.</div>
      </Card>

      <Card title="Aviator page and game frame">
        <Status label="Collector browser" value={browser?.connected === true ? 'CONNECTED' : browser?.connected === false ? 'DISCONNECTED' : snapshot?.browser_connected === true ? 'CONNECTED' : 'UNKNOWN'} />
        <Status label="Aviator page" value={pageState}
          detail={pages?.aviator_target_id ? `Shared page target ${pages.aviator_target_id}` : 'Collector has not supplied a current Aviator page target.'} />
        <Status label="Game frame" value={gameFrame}
          detail={pages?.game_frame_count == null ? browser?.stage || 'Frame count unavailable' : `${pages.game_frame_count} Aviator game frame(s) observed`} />
        <Status label="Page topology" value={browser?.browser_topology_verified === true ? 'VERIFIED' : browser?.browser_topology_verified === false ? 'UNVERIFIED' : 'UNKNOWN'}
          detail={pages?.page_count == null ? 'Page and context counts unavailable' : `${pages.browser_count ?? '?'} browser · ${pages.context_count ?? '?'} context · ${pages.page_count} page(s)`} />
      </Card>
    </section>

    <Card title="Collector session evidence" action={<span className="inline-flex items-center gap-2 text-xs text-zinc-400"><MonitorCheck size={15} />Read only</span>}>
      <div className="grid gap-x-8 md:grid-cols-2">
        <Status label="Collector state" value={snapshot?.status || 'UNKNOWN'} />
        <Status label="Heartbeat" value={snapshot?.heartbeat_state || 'UNKNOWN'} detail={snapshot?.heartbeat_age_seconds == null ? undefined : `${snapshot.heartbeat_age_seconds}s old`} />
        <Status label="Latest observed round" value={snapshot?.last_observed_round || 'UNKNOWN'} />
        <Status label="Evidence observed at" value={observedAt ? new Date(observedAt).toLocaleString() : 'UNKNOWN'} />
      </div>
      <p className="mt-3 border-t border-zinc-800 pt-3 text-xs text-zinc-500">Source: {snapshot?.data_source || 'collector_observation'} · Evidence is not treated as current when collector freshness is unknown or stale.</p>
    </Card>
  </div>;
}
