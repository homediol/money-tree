import { useCallback, useEffect, useRef, useState } from 'react';
import { getCurrentDecision, getCurrentEvidence, getCurrentSignal, getDataStatus, getDecisionStatus, getHistory, getHistoryStats, getHistoryStatus, getLatestFeatures, getLatestMLPrediction, getMLEstimate, getMLMetrics, getMLStatus, getModelPerformance, getPatternReport, getPatterns, getRecentDecisions, getRecentEvidence, getSignalHistory, getStatistics, getSystemStatus } from '../services/api.js';
import { getWebSocketUrl } from '../auth.js';

export function useLiveData() {
  const [state, setState] = useState({ loading: true, error: null });
  const loadingRef = useRef(false);
  const historyRefreshRef = useRef(false);
  const lastLoadRef = useRef(0);

  const load = useCallback(async (force = false) => {
    if (loadingRef.current) return;
    if (!force && Date.now() - lastLoadRef.current < 1000) return;
    loadingRef.current = true;
    try {
      const [signal, stats, patterns, history, signalHistory, models, system, historyStatus, historyStats, dataStatus, latestFeatures, patternReport, mlStatus, mlMetrics, mlPrediction, mlEstimate, evidence, evidenceHistory, decision, decisionHistory, decisionStatus] = await Promise.all([
        getCurrentSignal(),
        getStatistics(),
        getPatterns(),
        getHistory(),
        getSignalHistory(),
        getModelPerformance(),
        getSystemStatus(),
        getHistoryStatus(),
        getHistoryStats(),
        getDataStatus(),
        getLatestFeatures(),
        getPatternReport(),
        getMLStatus(),
        getMLMetrics(),
        getLatestMLPrediction(),
        getMLEstimate(),
        getCurrentEvidence(),
        getRecentEvidence(),
        getCurrentDecision(),
        getRecentDecisions(),
        getDecisionStatus(),
      ]);
      setState({ loading: false, error: null, signal, stats, patterns, history, signalHistory, models, system, historyStatus, historyStats, dataStatus, latestFeatures, patternReport, mlStatus, mlMetrics, mlPrediction, mlEstimate, evidence, evidenceHistory, decision, decisionHistory, decisionStatus });
      lastLoadRef.current = Date.now();
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || 'API unavailable' }));
    } finally {
      loadingRef.current = false;
    }
  }, []);

  // Keep collector-backed history fresh when the WebSocket is unavailable.
  // This deliberately refreshes only history endpoints instead of replaying
  // the full dashboard's expensive ML, pattern, and decision requests.
  const refreshHistory = useCallback(async () => {
    if (historyRefreshRef.current) return;
    historyRefreshRef.current = true;
    try {
      const [history, historyStatus, historyStats] = await Promise.all([
        getHistory(), getHistoryStatus(), getHistoryStats(),
      ]);
      setState((prev) => ({ ...prev, history, historyStatus, historyStats }));
    } catch {
      // Keep the last verified snapshot and its observed timestamp visible.
    } finally {
      historyRefreshRef.current = false;
    }
  }, []);

  useEffect(() => {
    let ws = null;
    let retryTimer = null;
    let closed = false;
    let attempts = 0;

    // Reconnect with capped exponential backoff so the dashboard recovers
    // automatically when the backend starts late or is restarted.
    function connect() {
      if (closed) return;
      ws = new WebSocket(getWebSocketUrl());
      ws.onopen = () => {
        attempts = 0;
      };
      ws.onmessage = () => load();
      ws.onerror = () => {};
      ws.onclose = () => {
        if (closed) return;
        const delay = Math.min(1000 * 2 ** attempts, 15000);
        attempts += 1;
        retryTimer = setTimeout(connect, delay);
      };
    }

    load();
    connect();

    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) ws.close();
    };
  }, [load]);

  return { ...state, refresh: () => load(true), refreshHistory };
}
