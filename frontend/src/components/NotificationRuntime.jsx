import { useEffect } from 'react';
import { toast } from 'sonner';
import { api } from '../services/api.js';
import { behaviourGet } from '../services/behaviourApi.js';
import NotificationToast from './NotificationToast.jsx';
import { enableNotificationAudio, enableNotificationVoice, getNotificationAudioStatus, getNotificationSettings, handleLiveTransportEvent, publishNotification, shouldShowNotificationToast, subscribeNotifications } from '../services/notifications.js';
import { subscribeLiveEvents } from '../services/liveEvents.js';

export default function NotificationRuntime({ behaviourOnly = false }) {
  useEffect(() => {
    let stopped = false; let timer;
    let audioLockedToastId = null;
    let sessionSnapshot = null; let rulePerformanceSnapshot = null; let adieIds = null; let adieStates = new Map(); let behaviourState = null;
    let collectorSnapshot = null;
    const showAudioUnlock = (message) => {
      if (audioLockedToastId) return;
      audioLockedToastId = toast.custom((toastId) => <div role="status" className="w-[min(26rem,calc(100vw-2rem))] rounded-xl border border-amber-400/30 bg-zinc-950 p-4 text-zinc-100 shadow-2xl">
        <p className="text-sm font-semibold text-amber-200">Sound is enabled, but browser audio is locked</p>
        <p className="mt-1 text-xs leading-5 text-zinc-300">{message || 'Click to allow sound playback in this tab.'}</p>
        <div className="mt-3 flex gap-2"><button type="button" className="rounded-lg bg-amber-300 px-3 py-1.5 text-xs font-bold text-zinc-950" onClick={async () => {
          const result = await enableNotificationAudio();
          if (result.status === 'ready') { toast.dismiss(toastId); audioLockedToastId = null; toast.success('Browser sound is ready for this tab.'); }
          else toast.error(result.message || `Audio ${result.status}.`);
        }}>Enable Sounds</button><button type="button" className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300" onClick={() => { toast.dismiss(toastId); audioLockedToastId = null; }}>Dismiss</button></div>
      </div>, { duration: Infinity, id: 'notification-audio-unlock' });
    };
    const onUserGesture = () => {
      const preferences = getNotificationSettings();
      if (!preferences.enabled || preferences.muted) return;
      if (preferences.voiceEnabled && preferences.mode !== 'sound-only') enableNotificationVoice();
      if (preferences.mode === 'voice-only'
          || ['running', 'unsupported'].includes(getNotificationAudioStatus())) return;
      void enableNotificationAudio();
    };
    window.addEventListener('pointerdown', onUserGesture, true);
    window.addEventListener('keydown', onUserGesture, true);
    const unsubscribeNotifications = subscribeNotifications((item) => {
      if (item.kind === 'audio-state' && ['ready', 'running'].includes(item.status)) {
        if (audioLockedToastId) toast.dismiss(audioLockedToastId);
        audioLockedToastId = null;
        return;
      }
      if (item.kind === 'audio-blocked' || (item.kind === 'playback-result' && item.result?.status === 'blocked')) {
        showAudioUnlock(item.result?.message || item.message);
        return;
      }
      if (item.kind === 'voice' && item.status === 'failed') {
        toast.error('Voice announcement did not play', { description: item.message || 'Check browser voice availability and click the page before retrying.' });
        return;
      }
      if (item.kind !== 'event' || !shouldShowNotificationToast(item.event)) return;
      const duration = item.event.severity === 'critical' ? 14000 : item.event.severity === 'warning' ? 11000 : 9000;
      toast.custom((toastId) => <NotificationToast event={item.event} onClose={() => toast.dismiss(toastId)} />, { duration });
    });
    const unsubscribeLive = behaviourOnly ? () => {} : subscribeLiveEvents((frame) => {
          if (frame?.__transport) {
            if (frame.status === 'disconnected') void publishNotification({ eventType: 'system.disconnected', id: 'transport:disconnected', deduplicationKey: 'transport:disconnected', message: 'Live notification connection disconnected.' });
            if (frame.status === 'restored') void publishNotification({ eventType: 'system.restored', id: `transport:restored:${Date.now()}`, deduplicationKey: `transport:restored:${Date.now()}`, message: 'Live notification connection restored.' });
            return;
          }
          try {
            handleLiveTransportEvent(frame);
            if (frame.type === 'rules-engine:evaluated') {
              for (const settlement of frame.evaluation?.target_settlements || []) {
                const outcome = String(settlement?.result || settlement?.status || '').toUpperCase();
                if (!['WIN', 'LOSS'].includes(outcome)) continue;
                const targetRound = settlement.target_round_id || settlement.target_round?.roundId || null;
                const identity = settlement.decision_id || settlement.selection_id
                  || `${settlement.source_round_id || 'source'}:${targetRound || settlement.settled_at || 'target'}`;
                const eventType = outcome === 'WIN' ? 'shadow.win' : 'shadow.loss';
                void publishNotification({ eventType, id: `rules-settlement:${identity}:${outcome}`,
                  deduplicationKey: `rules-settlement:${identity}:${outcome}`, roundId: targetRound,
                  source: 'SHADOW Predictions', message: `SHADOW prediction ${outcome.toLowerCase()}${targetRound ? ` on round #${targetRound}` : ''}.` });
              }
            }
          } catch { /* Ignore malformed or unrelated transport frames. */ }
    });
    const poll = async () => {
      if (stopped) return;
      const tasks = [
        (behaviourOnly ? Promise.resolve() : api.get('/api/history/status')).then(({ data } = {}) => {
          if (stopped || !data) return;
          const state = String(data.status || 'UNKNOWN').toUpperCase();
          const roundId = data.last_observed_round || data.last_persisted_round || data.latest?.round_id || null;
          const lastRoundAt = data.last_observed_at || data.last_persisted_at || data.last_successful_sync;
          const roundAgeSeconds = lastRoundAt ? (Date.now() - Date.parse(lastRoundAt)) / 1000 : Infinity;
          const previous = collectorSnapshot;
          const receiving = ['HEALTHY', 'COLLECTING'].includes(state) && roundId != null
            && Number.isFinite(roundAgeSeconds) && roundAgeSeconds >= -60 && roundAgeSeconds <= 120;
          const disconnected = ['OFFLINE', 'STOPPED', 'AUTH_REQUIRED'].includes(state);
          const normalizedState = receiving ? 'HEALTHY' : disconnected ? 'DISCONNECTED' : state === 'UNKNOWN' ? 'UNKNOWN' : 'STALE';
          collectorSnapshot = { state: normalizedState, roundId };
          const healthy = receiving;
          if (!previous) {
            if (healthy) void publishNotification({ eventType: 'collector.receiving',
              id: `collector:receiving:${roundId}`, deduplicationKey: 'collector:receiving', roundId,
              message: `Round collector is connected and receiving rounds. Latest round: #${roundId}.` });
            else if (disconnected) void publishNotification({ eventType: 'collector.disconnected',
              id: `collector:disconnected:${state}`, deduplicationKey: `collector:disconnected:${state}`,
              message: state === 'AUTH_REQUIRED' ? 'Round collector needs you to sign in again.' : 'Round collector is offline.' });
            else if (!receiving && state !== 'UNKNOWN') void publishNotification({ eventType: 'collector.stale',
              id: 'collector:stale:initial', deduplicationKey: 'collector:stale',
              message: `Round collector status is ${state.toLowerCase()}, and no fresh rounds are arriving.` });
            return;
          }
          const wasHealthy = previous.state === 'HEALTHY' && previous.roundId != null;
          if (healthy && (!wasHealthy || roundId !== previous.roundId)) {
            const resumed = previous.state !== 'HEALTHY';
            void publishNotification({ eventType: resumed ? 'collector.restored' : 'collector.receiving',
              id: `collector:${resumed ? 'restored' : 'receiving'}:${roundId}`,
              deduplicationKey: resumed ? 'collector:restored' : 'collector:receiving', roundId,
              message: `${resumed ? 'Round collection resumed.' : 'Round collector received new rounds.'} Latest round: #${roundId}.` });
          } else if (!healthy && (wasHealthy || (normalizedState !== 'UNKNOWN' && normalizedState !== previous.state))) {
            void publishNotification({ eventType: disconnected ? 'collector.disconnected' : 'collector.stale',
              id: `collector:${disconnected ? 'disconnected' : 'stale'}:${state}:${roundId || 'none'}`,
              deduplicationKey: `collector:${disconnected ? 'disconnected' : 'stale'}`,
              message: state === 'AUTH_REQUIRED' ? 'Round collector needs you to sign in again.'
                : disconnected ? 'Round collector connection was lost.'
                  : 'Round collector is connected, but fresh rounds have stopped arriving.' });
          }
        }).catch(() => {}),
        (behaviourOnly ? Promise.resolve() : api.get('/api/adie/forecasts?limit=50')).then((response) => {
          const data = response?.data;
          if (!data) return;
          if (stopped) return;
          const rows = data?.items || [];
          if (adieIds === null) { adieIds = new Set(rows.map((row) => row.forecast_id)); rows.forEach((row) => adieStates.set(row.forecast_id, row.status)); return; }
          for (const row of rows) {
            const previous = adieStates.get(row.forecast_id);
            if (!previous && row.forecast_id) {
              const forced = row.selection_status === 'FORCED_EXPLORATORY' || row.selection_status === 'EXPLORATORY';
              const announcement = `${row.direction === 'HIGH' ? 'High' : 'Low'} window selected. ${row.horizon} rounds.${forced ? ' Exploratory selection.' : ''}`;
              void publishNotification({ eventType: forced ? 'adie.exploratory' : 'adie.selected', id: `adie:selected:${row.forecast_id}`, deduplicationKey: `adie:selected:${row.forecast_id}`, direction: row.direction,
                message: announcement, voiceAnnouncement: announcement });
            }
            if (previous && previous !== row.status && row.status === 'COMPLETED') {
              const net = Number(row.net_units || 0);
              void publishNotification({ eventType: net > 0 ? 'adie.positive' : net < 0 ? 'adie.negative' : 'adie.completed', id: `adie:complete:${row.forecast_id}`, deduplicationKey: `adie:complete:${row.forecast_id}`, direction: row.direction });
            }
            adieStates.set(row.forecast_id, row.status);
          }
        }).catch(() => {}),
        (behaviourOnly ? Promise.resolve() : api.get('/api/rules-engine/sessions')).then(({ data } = {}) => {
          if (stopped || !data) return;
          const rows = Array.isArray(data) ? data : data.sessions || [];
          const snapshot = new Map(rows.filter((row) => row?.session_id).map((row) => [String(row.session_id), row]));
          if (sessionSnapshot) for (const [id, session] of snapshot) {
            const previous = sessionSnapshot.get(id);
            const status = String(session.status || '').toUpperCase();
            const priorStatus = String(previous?.status || '').toUpperCase();
            if (!previous) {
              void publishNotification({ eventType: 'shadow.created', id: `rules-session:${id}:created`, deduplicationKey: `rules-session:${id}:created`, source: 'SHADOW Predictions', message: 'New SHADOW prediction session created.' });
              if (status === 'RUNNING') void publishNotification({ eventType: 'shadow.activated', id: `rules-session:${id}:active`, deduplicationKey: `rules-session:${id}:active`, source: 'SHADOW Predictions', message: 'SHADOW prediction session activated.' });
            } else {
              if (status === 'RUNNING' && priorStatus !== 'RUNNING') void publishNotification({ eventType: 'shadow.activated', id: `rules-session:${id}:active:${session.last_activity_at || ''}`, deduplicationKey: `rules-session:${id}:active:${session.last_activity_at || ''}`, source: 'SHADOW Predictions', message: 'SHADOW prediction session activated.' });
              if (status === 'COMPLETED' && priorStatus !== 'COMPLETED') void publishNotification({ eventType: 'shadow.completed', id: `rules-session:${id}:completed`, deduplicationKey: `rules-session:${id}:completed`, source: 'SHADOW Predictions' });
              if (status === 'PAUSED' && priorStatus !== 'PAUSED') {
                const reason = session.stop_reason || session.selection_stop_reason || 'No reason provided';
                void publishNotification({ eventType: 'shadow.paused', id: `rules-session:${id}:paused:${reason}`, deduplicationKey: `rules-session:${id}:paused:${reason}`, source: 'SHADOW Predictions', message: `SHADOW session paused: ${reason}.` });
              }
              if (['MANUALLY_STOPPED', 'STOPPED', 'RESET', 'RECONFIGURED'].includes(status) && status !== priorStatus) {
                const reason = session.stop_reason || session.selection_stop_reason || status;
                void publishNotification({ eventType: 'shadow.stopped', id: `rules-session:${id}:stopped:${status}:${reason}`, deduplicationKey: `rules-session:${id}:stopped:${status}:${reason}`, source: 'SHADOW Predictions', message: `SHADOW session stopped: ${reason}.` });
              }
            }
          }
          sessionSnapshot = snapshot;
        }).catch(() => {}),
        behaviourGet('/overview').then((data) => {
          if (stopped) return;
          const state = data?.behaviour?.confirmed_state || data?.behaviour?.observed_state;
          if (!state) return;
          const previous = behaviourState;
          behaviourState = state;
          if (!previous || previous === state) return;
          const direction = /HIGH|DOMINANT_HIGH/i.test(state) ? 'HIGH' : /LOW|DOMINANT_LOW/i.test(state) ? 'LOW' : null;
          const rising = /RISING|INCREASING/i.test(state);
          const eventType = previous === 'UNCERTAIN'
            ? direction ? `behaviour.${direction.toLowerCase()}${rising ? '_rising' : ''}` : 'behaviour.detected'
            : direction ? `behaviour.${direction.toLowerCase()}${rising ? '_rising' : ''}`
              : /BALANCE/i.test(state) ? 'behaviour.balance' : 'behaviour.transition';
          const id = `behaviour:${previous}:${state}:${data.latest_round?.round_id || data.updated_at}`;
          void publishNotification({ eventType, id,
            roundId: data.latest_round?.round_id, deduplicationKey: id,
            direction, message: previous === 'UNCERTAIN' ? `New behaviour detected: ${state}.` : `Behaviour transitioned from ${previous} to ${state}.` });
        }).catch(() => {}),
        (behaviourOnly ? Promise.resolve() : api.get('/api/rules-engine/rule-performance')).then(({ data } = {}) => {
          if (!data) return;
          if (stopped) return;
          const items = data?.rules || data?.windows || data?.items || [];
          const snapshot = new Map(items.map((row) => [String(row.rule_id), { status: row.status || row.performance_trend, net: Number(row.net || 0) }]));
          if (rulePerformanceSnapshot) for (const [ruleId, current] of snapshot) {
            const previous = rulePerformanceSnapshot.get(ruleId);
            if (!previous) continue;
            const threshold = Math.max(1, Number(getNotificationSettings().events['rules.improved']?.threshold || 5));
            const improved = (previous.status !== current.status && ['WINNING_LATEST_WINDOW', 'REPEATEDLY_WINNING', 'RISING'].includes(current.status))
              || (previous.net < threshold && current.net >= threshold);
            const warning = (previous.status !== current.status && ['LOSING_LATEST_WINDOW', 'FALLING'].includes(current.status))
              || (previous.net > -threshold && current.net <= -threshold);
            if (improved) void publishNotification({ eventType: 'rules.improved', id: `rules:${ruleId}:improved:${current.status}:${current.net}`, deduplicationKey: `rules:${ruleId}:improved:${current.status}:${current.net}`, message: `Rule ${ruleId} performance improved or reached the configured net threshold (${threshold}).` });
            if (warning) void publishNotification({ eventType: 'rules.warning', id: `rules:${ruleId}:warning:${current.status}:${current.net}`, deduplicationKey: `rules:${ruleId}:warning:${current.status}:${current.net}`, message: `Rule ${ruleId} performance warning or reached the configured net threshold (-${threshold}).` });
          }
          rulePerformanceSnapshot = snapshot;
        }).catch(() => {}),
      ];
      await Promise.allSettled(tasks);
    };
    void poll(); timer = window.setInterval(poll, 15000);
    return () => { stopped = true; if (timer) clearInterval(timer); unsubscribeLive(); unsubscribeNotifications(); window.removeEventListener('pointerdown', onUserGesture, true); window.removeEventListener('keydown', onUserGesture, true); if (audioLockedToastId) toast.dismiss(audioLockedToastId); };
  }, [behaviourOnly]);
  return null;
}
