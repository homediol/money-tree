import { AlertTriangle, ArrowUpRight, BellRing, CheckCircle2, CircleX, Info, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { getNotificationPresentation, getNotificationSource } from '../services/notificationPresentation.js';

const tones = {
  success: { card: 'border-emerald-300/50 bg-gradient-to-br from-emerald-950 via-zinc-950 to-zinc-950', icon: 'bg-emerald-400/15 text-emerald-200', label: 'text-emerald-200' },
  danger: { card: 'border-rose-300/55 bg-gradient-to-br from-rose-950 via-zinc-950 to-zinc-950', icon: 'bg-rose-400/15 text-rose-200', label: 'text-rose-200' },
  warning: { card: 'border-amber-300/55 bg-gradient-to-br from-amber-950 via-zinc-950 to-zinc-950', icon: 'bg-amber-400/15 text-amber-100', label: 'text-amber-200' },
  info: { card: 'border-sky-300/50 bg-gradient-to-br from-sky-950 via-zinc-950 to-zinc-950', icon: 'bg-sky-400/15 text-sky-200', label: 'text-sky-200' },
};
const icons = { success: CheckCircle2, danger: CircleX, warning: AlertTriangle, info: Info };
const detailEventTypes = new Set(['shadow.stopped', 'shadow.paused', 'shadow.alert', 'system.error', 'system.disconnected', 'system.stale', 'rules.warning', 'adie.integrity']);

export default function NotificationToast({ event, onClose }) {
  const presentation = getNotificationPresentation(event);
  const source = getNotificationSource(event);
  const style = tones[presentation.tone] || tones.info;
  const Icon = icons[presentation.tone] || BellRing;
  const urgent = presentation.tone === 'danger' || presentation.tone === 'warning';

  return <article role={urgent ? 'alert' : 'status'} aria-live={urgent ? 'assertive' : 'polite'}
    className={`relative w-[min(460px,calc(100vw_-_24px))] overflow-hidden rounded-2xl border p-4 text-zinc-50 shadow-2xl shadow-black/60 backdrop-blur-xl sm:p-5 ${style.card}`}>
    <button type="button" aria-label="Close notification" onClick={onClose}
      className="absolute right-3 top-3 z-10 rounded-lg p-1.5 text-zinc-400 hover:bg-white/10 hover:text-white"><X size={18} /></button>
    <Link to={source.to} aria-label={`Open ${source.label} source`} className="group block rounded-lg pr-8 outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
      <div className="flex items-start gap-3 sm:gap-4">
        <div className={`grid h-12 w-12 shrink-0 place-items-center rounded-2xl sm:h-14 sm:w-14 ${style.icon}`}><Icon size={28} strokeWidth={2.5} /></div>
        <div className="min-w-0 flex-1">
          <div className={`text-xs font-black uppercase tracking-[0.16em] ${style.label}`}>{presentation.category}{event.test ? ' · TEST' : ''}</div>
          <h2 className="mt-1 text-xl font-black leading-tight tracking-tight sm:text-2xl">{presentation.title}</h2>
          <p className="mt-1.5 text-base font-semibold leading-snug text-zinc-100 sm:text-lg">{presentation.summary}</p>
          {detailEventTypes.has(event.eventType) && event.message && <p className="mt-2 break-words rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm font-medium leading-snug text-zinc-200">{event.message}</p>}
          <div className="mt-3 flex flex-wrap gap-2 text-xs font-bold text-zinc-300">
            {event.roundId && <span className="rounded-md border border-white/10 bg-black/20 px-2 py-1">ROUND #{event.roundId}</span>}
            {event.direction && <span className="rounded-md border border-white/10 bg-black/20 px-2 py-1">{String(event.direction).toUpperCase()}</span>}
            {event.source && <span className="rounded-md border border-white/10 bg-black/20 px-2 py-1">{event.source}</span>}
          </div>
        </div>
      </div>
      <span className="mt-3 inline-flex items-center gap-1 text-xs font-bold text-cyan-200 group-hover:text-cyan-100">Open {source.label} source <ArrowUpRight size={14} /></span>
    </Link>
  </article>;
}
