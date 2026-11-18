import { useEffect, useMemo, useState } from 'react';
import { BellRing, CheckCircle2, CircleX, Info, TriangleAlert } from 'lucide-react';
import { Link } from 'react-router-dom';
import PageHeader from '../components/PageHeader.jsx';
import { getNotificationHistory, subscribeNotifications } from '../services/notifications.js';
import { getNotificationPresentation, getNotificationSource, notificationCategory } from '../services/notificationPresentation.js';

const filters = [
  { id: 'all', label: 'All notifications' }, { id: 'shadow', label: 'SHADOW' }, { id: 'rules', label: 'Rules' },
  { id: 'behaviour', label: 'Behaviour' }, { id: 'adie', label: 'ADIE' }, { id: 'system', label: 'System' },
];
const toneClasses = {
  success: 'border-emerald-400/30 bg-emerald-950/20 text-emerald-100',
  danger: 'border-rose-400/35 bg-rose-950/25 text-rose-100',
  warning: 'border-amber-400/35 bg-amber-950/20 text-amber-100',
  info: 'border-sky-400/30 bg-sky-950/20 text-sky-100',
};
const toneIcons = { success: CheckCircle2, danger: CircleX, warning: TriangleAlert, info: Info };
const timeText = (value) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : 'Time unavailable';

export default function NotificationCenter() {
  const [history, setHistory] = useState(() => getNotificationHistory());
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    const refresh = () => setHistory(getNotificationHistory());
    refresh();
    return subscribeNotifications((item) => {
      if (item.kind === 'event' || item.kind === 'playback-result' || item.kind === 'history-cleared') refresh();
    });
  }, []);

  const counts = useMemo(() => Object.fromEntries(filters.map(({ id }) => [id,
    id === 'all' ? history.length : history.filter((event) => notificationCategory(event) === id).length])), [history]);
  const visible = filter === 'all' ? history : history.filter((event) => notificationCategory(event) === filter);

  return <div className="space-y-5">
    <PageHeader eyebrow="Saved alerts" title="Notification Center" icon={BellRing}
      description="See what happened at a glance, then open any entry for its original event message. The latest 100 alerts are saved in this browser."
      action={<span className="rounded-xl border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm font-bold text-zinc-200">{history.length} saved</span>} />

    <section aria-label="Filter notification history" className="flex gap-2 overflow-x-auto pb-1">
      {filters.map((item) => <button key={item.id} type="button" onClick={() => setFilter(item.id)}
        aria-pressed={filter === item.id}
        className={`whitespace-nowrap rounded-xl border px-3 py-2 text-sm font-bold transition ${filter === item.id ? 'border-cyan-300/50 bg-cyan-400/10 text-cyan-100' : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-100'}`}>
        {item.label}<span className="ml-2 rounded-md bg-black/25 px-1.5 py-0.5 text-xs">{counts[item.id]}</span>
      </button>)}
    </section>

    {visible.length === 0 ? <section className="rounded-2xl border border-dashed border-zinc-700 bg-zinc-900/50 p-8 text-center">
      <BellRing size={30} className="mx-auto text-zinc-500" />
      <h2 className="mt-3 text-lg font-bold text-zinc-200">No notifications here yet</h2>
      <p className="mt-1 text-sm text-zinc-500">New alerts will appear here and stay saved in this browser.</p>
    </section> : <section aria-label="Saved notification history" className="space-y-3">
      {visible.map((event) => {
        const view = getNotificationPresentation(event);
        const source = getNotificationSource(event);
        const Icon = toneIcons[view.tone] || BellRing;
        const severity = event.eventType === 'adie.integrity' ? 'WARNING'
          : event.severity === 'critical' ? 'CRITICAL' : event.severity === 'warning' ? 'WARNING' : 'INFO';
        return <article key={`${event.id}-${event.receivedAt || event.timestamp}`} className={`rounded-2xl border p-4 shadow-lg shadow-black/10 sm:p-5 ${toneClasses[view.tone] || toneClasses.info}`}>
          <Link to={source.to} aria-label={`Open ${source.label} source`} className="group block rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
            <div className="flex items-start gap-3">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-black/20"><Icon size={22} /></div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs font-black uppercase tracking-wider">
                    <span>{view.category}</span><span className="rounded-md bg-black/20 px-2 py-1">{severity}</span>
                    {event.test && <span className="rounded-md bg-black/20 px-2 py-1">TEST</span>}
                  </div>
                  <time className="text-xs font-medium text-zinc-400">{timeText(event.receivedAt || event.timestamp)}</time>
                </div>
                <h2 className="mt-2 text-lg font-black leading-tight text-white group-hover:text-cyan-100 sm:text-xl">{view.title}</h2>
                <p className="mt-1 text-base font-semibold leading-snug">{view.summary}</p>
                <div className="mt-3 flex flex-wrap gap-2 text-xs font-bold text-zinc-300">
                  {event.roundId && <span className="rounded-md bg-black/20 px-2 py-1">ROUND #{event.roundId}</span>}
                  {event.direction && <span className="rounded-md bg-black/20 px-2 py-1">{String(event.direction).toUpperCase()}</span>}
                  {event.source && <span className="rounded-md bg-black/20 px-2 py-1">{event.source}</span>}
                  {event.playback && event.playback !== 'not_played' && <span className="rounded-md bg-black/20 px-2 py-1">SOUND: {event.playback.toUpperCase()}</span>}
                </div>
                <span className="mt-3 inline-flex items-center gap-1 text-xs font-bold text-cyan-200 group-hover:text-cyan-100">Open {source.label} source <span aria-hidden="true">↗</span></span>
              </div>
            </div>
          </Link>
          {event.message && <details className="mt-3 rounded-lg border border-white/10 bg-black/15 px-3 py-2 text-sm text-zinc-300">
            <summary className="cursor-pointer font-semibold">Original event message</summary>
            <p className="mt-2 break-words leading-relaxed">{event.message}</p>
            {event.playbackMessage && <p className="mt-2 break-words text-xs text-zinc-400">Audio: {event.playbackMessage}</p>}
          </details>}
        </article>;
      })}
    </section>}
  </div>;
}
