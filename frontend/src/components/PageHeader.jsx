export default function PageHeader({ eyebrow, title, description, icon: Icon, action, badge }) {
  return (
    <header className="relative overflow-hidden rounded-2xl border border-zinc-800 bg-gradient-to-br from-zinc-900 via-zinc-900/90 to-emerald-950/20 p-5 shadow-xl shadow-black/10 sm:p-6">
      <div className="absolute -right-16 -top-20 h-52 w-52 rounded-full bg-emerald-400/5 blur-3xl" />
      <div className="relative flex flex-col justify-between gap-5 md:flex-row md:items-center">
        <div className="flex min-w-0 items-start gap-4">
          {Icon && <div className="grid h-11 w-11 shrink-0 place-items-center rounded-xl border border-emerald-400/20 bg-emerald-400/10 text-emerald-300"><Icon size={21} /></div>}
          <div>
            <div className="text-[11px] font-bold uppercase tracking-[0.18em] text-emerald-300">{eyebrow}</div>
            <div className="mt-1 flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">{title}</h1>
              {badge}
            </div>
            {description && <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-400">{description}</p>}
          </div>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </header>
  );
}
