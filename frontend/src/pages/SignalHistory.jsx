import Card from '../components/Card.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { pct } from '../utils/format.js';

export default function SignalHistory() {
  const { signalHistory, loading, error } = useLiveData();
  if (loading) return <div className="text-zinc-400">Loading signal history...</div>;
  if (error) return <div className="text-rose-300">{error}</div>;
  return (
    <div className="space-y-5">
      <h1 className="text-3xl font-semibold">Signal History</h1>
      <Card title="Stored Signals">
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
              {(!signalHistory || signalHistory.length === 0) && <tr><td className="py-4 text-zinc-500" colSpan="6">No stored signals yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

