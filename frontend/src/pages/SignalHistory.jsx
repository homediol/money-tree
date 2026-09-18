import Card from '../components/Card.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { pct } from '../utils/format.js';
import { RadioTower } from 'lucide-react';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';

export default function SignalHistory() {
  const { signalHistory, loading, error } = useLiveData();
  if (loading) return <LoadingState label="Loading signal history…" />;
  if (error) return <ErrorState message={error} />;
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Analysis log" title="Signals" icon={RadioTower}
        description="A traceable record of statistical signals produced by the analysis layer. These entries do not authorize bets." />
      <Card title="Stored Signals">
        {(!signalHistory || signalHistory.length === 0) ? <EmptyState title="No signals stored" description="New analysis events will appear here automatically." /> :
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="text-xs uppercase text-zinc-500">
              <tr><th className="py-2">Time</th><th>Pattern</th><th>Probability</th><th>Confidence</th><th>Status</th><th>Actual</th></tr>
            </thead>
            <tbody>
              {(signalHistory || []).map((s) => (
                <tr key={s.id} className="border-t border-zinc-800">
                  <td className="py-3 text-zinc-400">{s.timestamp}</td>
                  <td>{s.detected_pattern}</td>
                  <td>{pct(s.probability)}</td>
                  <td>{s.confidence}</td>
                  <td><StatusPill status={s.status} /></td>
                  <td className="text-zinc-500">Pending next round</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>}
      </Card>
    </div>
  );
}
