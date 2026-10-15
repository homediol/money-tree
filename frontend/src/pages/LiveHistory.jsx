import { useCallback, useEffect, useState } from 'react';
import { Clock3 } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';
import { api } from '../services/api.js';

const mult = (value) => value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value).toFixed(2)}x`;

export default function LiveHistory() {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [adminStatus, setAdminStatus] = useState(null);
  const [adminError, setAdminError] = useState('');
  const [exportRecord, setExportRecord] = useState(null);
  const [adminBusy, setAdminBusy] = useState(false);
  const [adminMessage, setAdminMessage] = useState('');
  const [adminActionError, setAdminActionError] = useState('');
  const [clearConfirmationOpen, setClearConfirmationOpen] = useState(false);
  const [confirmedCount, setConfirmedCount] = useState('');
  const [finalClearPhrase, setFinalClearPhrase] = useState('');

  const refreshAdminStatus = useCallback(async () => {
    try {
      const { data } = await api.get('/api/history/admin-status');
      setAdminStatus(data);
      setAdminError('');
    } catch (failure) {
      const code = failure?.response?.data?.detail?.error;
      setAdminStatus(null);
      setAdminError(code === 'https_required_for_external_access'
        ? 'Open this page over HTTPS to manage history from another device.'
        : 'History export/reset status is unavailable; confirm the backend is online and try again.');
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [historyResult, statusResult, statsResult] = await Promise.allSettled([
        api.get('/api/history', { params: { limit: 250 } }),
        api.get('/api/history/status'),
        api.get('/api/history/stats'),
      ]);
      // The older history route can fail independently of the collector's
      // canonical recent-round route. Keep verified rounds visible while the
      // backend update is being rolled out.
      const history = historyResult.status === 'fulfilled'
        ? historyResult.value.data
        : (await api.get('/api/history/recent', { params: { limit: 250 } })).data;
      const rounds = history.rounds || [];
      const fallbackStatus = { status: 'UNKNOWN', count: history.count,
        latest: rounds.at(-1) || null, previous: rounds.at(-2) || null,
        last_update: rounds.at(-1)?.timestamp || null };
      setSnapshot({ history,
        status: statusResult.status === 'fulfilled' ? statusResult.value.data : fallbackStatus,
        stats: statsResult.status === 'fulfilled' ? statsResult.value.data : {} });
      setError([historyResult, statusResult, statsResult]
        .some((result) => result.status === 'rejected')
        ? 'Some history services are unavailable.' : '');
    } catch {
      setError('Round history is unavailable.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    refreshAdminStatus();
    const timer = window.setInterval(() => { refresh(); refreshAdminStatus(); }, 15000);
    return () => window.clearInterval(timer);
  }, [refresh, refreshAdminStatus]);

  const createExport = async (format) => {
    setAdminBusy(true); setAdminActionError(''); setAdminMessage(''); setExportRecord(null);
    try {
      const { data } = await api.post('/api/history/exports', { format });
      setExportRecord(data);
      setAdminMessage(`${format.toUpperCase()} export completed: ${data.record_count} records, SHA-256 ${data.checksum_sha256}. Verify it before any clear.`);
      await refreshAdminStatus();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setAdminActionError(detail?.message || detail?.error || 'History export failed.');
    } finally { setAdminBusy(false); }
  };

  const verifyExport = async () => {
    if (!exportRecord?.export_id) return;
    setAdminBusy(true); setAdminActionError(''); setAdminMessage('');
    try {
      const { data } = await api.post(`/api/history/exports/${encodeURIComponent(exportRecord.export_id)}/verify`);
      setExportRecord((current) => ({ ...current, verification: data,
        verification_status: data.ok ? 'VERIFIED' : 'INVALID', verified_at: data.verified_at }));
      setAdminMessage(data.ok
        ? `Export verified: ${data.record_count} records; checksum, structure, boundaries, and count match.`
        : `Export verification failed${data.reason ? `: ${data.reason}` : '.'}`);
      await refreshAdminStatus();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setAdminActionError(detail?.message || detail?.error || 'Export verification failed.');
    } finally { setAdminBusy(false); }
  };

  const downloadExport = async () => {
    if (!exportRecord?.export_id) return;
    setAdminBusy(true); setAdminActionError('');
    try {
      const { data } = await api.get(`/api/history/exports/${encodeURIComponent(exportRecord.export_id)}/download`, { responseType: 'blob' });
      const url = URL.createObjectURL(data);
      const link = document.createElement('a');
      link.href = url;
      link.download = `round-history-${exportRecord.export_id}.${exportRecord.format}`;
      document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setAdminActionError(detail?.message || detail?.error || 'Export file could not be downloaded.');
    } finally { setAdminBusy(false); }
  };

  const clearHistory = async () => {
    const count = adminStatus?.total_records;
    if (!exportRecord?.export_id || exportRecord.verification_status !== 'VERIFIED'
        || String(confirmedCount) !== String(count) || finalClearPhrase !== 'CLEAR HISTORY') return;
    setAdminBusy(true); setAdminActionError(''); setAdminMessage('');
    try {
      const { data } = await api.post('/api/history/clear', {
        export_id: exportRecord.export_id, record_count: count,
        confirmation_count: Number(confirmedCount), final_confirmation: finalClearPhrase,
      });
      setAdminMessage(`History cleared after verified export. New epoch ${data.history_epoch}; next local round index is 1. Collector resume: ${data.collector_resume || 'UNVERIFIED'}.`);
      setClearConfirmationOpen(false); setConfirmedCount(''); setFinalClearPhrase(''); setExportRecord(null);
      await Promise.all([refresh(), refreshAdminStatus()]);
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setAdminActionError(detail?.message || detail?.error || 'History clear was refused or failed.');
      await refreshAdminStatus();
    } finally { setAdminBusy(false); }
  };

  if (loading) return <LoadingState label="Loading round history…" />;
  if (error && !snapshot) return <ErrorState message={error} />;
  const rounds = snapshot?.history?.rounds || [];
  const status = snapshot?.status || {};
  const stats = snapshot?.stats || {};

  return <div className="space-y-5">
    <PageHeader eyebrow="Live data" title="Round History" icon={Clock3}
      description="Real Aviator outcomes stored by the history collector."
      action={<DataFreshness timestamp={status.last_update} records={status.count} />} />
    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error} Showing the last available history.</div>}
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Collector" value={status.status || 'UNKNOWN'} /></Card>
      <Card><Metric label="Latest" value={mult(status.latest?.multiplier)} /></Card>
      <Card><Metric label="Previous" value={mult(status.previous?.multiplier)} /></Card>
      <Card><Metric label="Round Count" value={status.count ?? 0} /></Card>
    </section>
    <Card title="History Export and Reset">
      <p className="mb-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">Anyone who can reach this dashboard can export or clear round history; no administrator API key is required. Clearing is irreversible and still requires a freshly verified export, the exact current record count, and the phrase CLEAR HISTORY. Financial and audit records are preserved. History is never cleared automatically.</p>
      {adminError && <div role="status" className="mb-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{adminError}</div>}
      {adminStatus && <div className="mb-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded bg-zinc-950 p-2">History epoch: <b>{adminStatus.history_epoch}</b></div>
        <div className="rounded bg-zinc-950 p-2">Total records: <b>{adminStatus.total_records}</b></div>
        <div className="rounded bg-zinc-950 p-2">First / last: <b>{adminStatus.first_round?.local_round_index ?? '—'} / {adminStatus.last_round?.local_round_index ?? '—'}</b></div>
        <div className="rounded bg-zinc-950 p-2">Collector: <b>{adminStatus.collector_state || 'UNKNOWN'}</b></div>
        <div className="rounded bg-zinc-950 p-2">Pending queue: <b>{adminStatus.pending_queue_count ?? 'UNKNOWN'}</b></div>
        <div className="rounded bg-zinc-950 p-2">Local round index: <b>{adminStatus.current_local_round_index ?? 0}</b></div>
        <div className="rounded bg-zinc-950 p-2">Warm-up: <b>{adminStatus.warmup?.collected_rounds ?? 0}/{adminStatus.warmup?.required_rounds ?? '—'} · {adminStatus.warmup?.status || 'UNKNOWN'}</b></div>
        <div className="rounded bg-zinc-950 p-2">Last clear: <b>{adminStatus.last_clear?.cleared_at || 'Never'}</b></div>
        <div className="rounded bg-zinc-950 p-2">Last export: <b>{adminStatus.last_export?.created_at || 'Never'} · {adminStatus.export_verification_status || 'NOT_VERIFIED'}</b></div>
      </div>}
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => createExport('json')} disabled={adminBusy || !adminStatus} className="rounded border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sm text-sky-100 disabled:opacity-40">EXPORT JSON</button>
        <button type="button" onClick={() => createExport('csv')} disabled={adminBusy || !adminStatus} className="rounded border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sm text-sky-100 disabled:opacity-40">EXPORT CSV</button>
        <button type="button" onClick={verifyExport} disabled={adminBusy || !exportRecord?.export_id} className="rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-100 disabled:opacity-40">VERIFY EXPORT</button>
        <button type="button" onClick={downloadExport} disabled={adminBusy || !exportRecord?.export_id} className="rounded border border-zinc-600 px-3 py-2 text-sm text-zinc-100 disabled:opacity-40">DOWNLOAD EXPORT</button>
        <button type="button" onClick={() => { setClearConfirmationOpen(true); setConfirmedCount(''); setFinalClearPhrase(''); }} disabled={adminBusy || !adminStatus || exportRecord?.verification_status !== 'VERIFIED'} className="rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm font-semibold text-rose-100 disabled:opacity-40">CLEAR HISTORY</button>
        <button type="button" onClick={refreshAdminStatus} disabled={adminBusy} className="rounded border border-zinc-600 px-3 py-2 text-sm text-zinc-100 disabled:opacity-40">REFRESH HISTORY STATUS</button>
      </div>
      {exportRecord && <div className="mt-3 rounded border border-zinc-700 bg-zinc-950 p-3 text-xs text-zinc-300">
        <div>Export {exportRecord.export_id} · {exportRecord.format?.toUpperCase()} · {exportRecord.record_count} records · {exportRecord.verification_status || exportRecord.status || 'NOT VERIFIED'}</div>
        <div className="break-all">SHA-256: {exportRecord.checksum_sha256}</div>
      </div>}
      {clearConfirmationOpen && <div className="mt-4 space-y-3 rounded border border-rose-500/40 bg-rose-950/20 p-4">
        <p className="text-sm font-semibold text-rose-100">Starting a new history epoch deletes exactly {adminStatus?.total_records ?? 'UNKNOWN'} round-history records. Pending observation queues, live sessions, or financial references will block it.</p>
        <label className="block text-sm text-zinc-200">First confirmation: type the exact record count ({adminStatus?.total_records ?? '—'})
          <input inputMode="numeric" value={confirmedCount} onChange={(event) => setConfirmedCount(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2" />
        </label>
        <label className="block text-sm text-zinc-200">Second confirmation: type CLEAR HISTORY
          <input value={finalClearPhrase} onChange={(event) => setFinalClearPhrase(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2" />
        </label>
        <div className="flex gap-2">
          <button type="button" onClick={clearHistory} disabled={adminBusy || exportRecord?.verification_status !== 'VERIFIED' || String(confirmedCount) !== String(adminStatus?.total_records) || finalClearPhrase !== 'CLEAR HISTORY'} className="rounded border border-rose-500/50 bg-rose-600/20 px-3 py-2 text-sm font-bold text-rose-100 disabled:opacity-40">START NEW HISTORY</button>
          <button type="button" onClick={() => setClearConfirmationOpen(false)} disabled={adminBusy} className="rounded border border-zinc-600 px-3 py-2 text-sm">Cancel</button>
        </div>
      </div>}
      {adminMessage && <div role="status" className="mt-3 rounded border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-100">{adminMessage}</div>}
      {adminActionError && <div role="alert" className="mt-3 rounded border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100">{adminActionError}</div>}
    </Card>
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Minimum" value={mult(stats.min)} /></Card>
      <Card><Metric label="Maximum" value={mult(stats.max)} /></Card>
      <Card><Metric label="Mean" value={mult(stats.mean)} /></Card>
      <Card><Metric label="Median" value={mult(stats.median)} /></Card>
    </section>
    <Card title="Threshold Counts"><div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {Object.entries(stats.threshold_counts || {}).map(([label, value]) => <Metric key={label} label={label.replaceAll('_', ' ')} value={value} />)}
    </div></Card>
    <Card title="Latest Multipliers">
      {rounds.length === 0 ? <EmptyState title="No rounds recorded" description="The collector has not supplied round data yet." /> :
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-8">
          {rounds.slice(-40).reverse().map((round) => <div key={round.round_id || round.round_index}
            className={`rounded border p-2 text-center ${round.multiplier >= 2 ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-zinc-800 bg-zinc-950'}`}>
            <div className="text-sm font-semibold">{mult(round.multiplier)}</div><div className="text-xs text-zinc-500">#{round.round_index}</div>
          </div>)}
        </div>}
    </Card>
  </div>;
}
