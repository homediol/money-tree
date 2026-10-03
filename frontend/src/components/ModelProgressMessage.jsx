import { completionMessage } from '../services/readinessView.js';

export default function ModelProgressMessage({ readiness }) {
  const { title, lines } = completionMessage(readiness);
  return <div role="status" className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-3 text-sm text-sky-100">
    <div className="mb-1 font-semibold">{title}</div>
    {lines.map(line => <p key={line} className="mt-1 text-xs text-sky-100/80">{line}</p>)}
  </div>;
}
