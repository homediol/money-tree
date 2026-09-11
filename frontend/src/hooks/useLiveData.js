import { useEffect, useState } from 'react';
import { getCurrentSignal, getHistory, getModelPerformance, getPatterns, getSignalHistory, getStatistics } from '../services/api.js';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/live';

export function useLiveData() {
  const [state, setState] = useState({ loading: true, error: null });

  async function load() {
    try {
      const [signal, stats, patterns, history, signalHistory, models] = await Promise.all([
        getCurrentSignal(),
        getStatistics(),
        getPatterns(),
        getHistory(),
        getSignalHistory(),
        getModelPerformance(),
      ]);
      setState({ loading: false, error: null, signal, stats, patterns, history, signalHistory, models });
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: error.message || 'API unavailable' }));
    }
  }

  useEffect(() => {
    let ws = null;
    let retryTimer = null;
    let closed = false;
    let attempts = 0;

    // Reconnect with capped exponential backoff so the dashboard recovers
    // automatically when the backend starts late or is restarted.
    function connect() {
      if (closed) return;
      ws = new WebSocket(WS_URL);
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
  }, []);

  return { ...state, refresh: load };
}

