export default function Card({ title, children, action }) {
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 shadow-lg shadow-black/5 backdrop-blur transition-colors hover:border-zinc-700 sm:p-5">
      {(title || action) && (
        <div className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">{title}</h2>}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
