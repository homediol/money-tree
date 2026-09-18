import { useCallback, useEffect, useRef, useState } from 'react';
import { getCurrentSignal, getHistory, getModelPerformance, getPatterns, getSignalHistory, getStatistics, getSystemStatus } from '../services/api.js';
import { getWebSocketUrl } from '../auth.js';

export function useLiveData() {
  const [state, setState] = useState({ loading: true, error: null });
  const loadingRef = useRef(false);
  const lastLoadRef = useRef(0);

  const load = useCallback(async (force = false) => {
    if (loadingRef.current) return;
    if (!force && Date.now() - lastLoadRef.current < 1000) return;
    loadingRef.current = true;
    try {
      const [signal, stats, patterns, history, signalHistory, models, system] = await Promise.all([
        getCurrentSignal(),
        getStatistics(),
        getPatterns(),
        getHistory(),
        getSignalHistory(),
        getModelPerformance(),
        getSystemStatus(),
      ]);
      setState({ loading: false, error: null, signal, stats, patterns, history, signalHistory, models, system });
      lastLoadRef.current = Date.now();
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || 'API unavailable' }));
    } finally {
      loadingRef.current = false;
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

  return { ...state, refresh: () => load(true) };
}
