/**
 * useSocket.js
 * ============
 * Shared React hook for the Flask-SocketIO WebSocket connection.
 *
 * Events pushed by the backend:
 *   "prediction"        — new prediction ready
 *   "round_complete"    — a round just finished (from history)
 *   "decisions_updated" — backfill complete, Actual column updated
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { io } from 'socket.io-client';

let _socket = null;
let _refCount = 0;

function getSocket() {
  if (!_socket) {
    _socket = io({
      path: '/socket.io',
      transports: ['websocket', 'polling'],
      reconnectionDelay: 1000,
      reconnectionAttempts: Infinity,
      timeout: 5000,
      autoConnect: true,
    });
  }
  return _socket;
}

export default function useSocket() {
  const [connected, setConnected]               = useState(false);
  const [lastPrediction, setLastPred]           = useState(null);
  const [lastRound, setLastRound]               = useState(null);
  const [updatedDecisions, setUpdatedDecisions] = useState(null);

  const socketRef = useRef(null);

  const on  = useCallback((event, handler) => { socketRef.current?.on(event, handler); },  []);
  const off = useCallback((event, handler) => { socketRef.current?.off(event, handler); }, []);

  useEffect(() => {
    const s = getSocket();
    socketRef.current = s;
    _refCount++;

    const onConnect          = () => setConnected(true);
    const onDisconnect       = () => setConnected(false);
    const onPrediction       = (data) => setLastPred(data);
    const onRound            = (data) => setLastRound(data);
    const onDecisionsUpdated = (data) => setUpdatedDecisions(data?.decisions ?? null);

    s.on('connect',           onConnect);
    s.on('disconnect',        onDisconnect);
    s.on('prediction',        onPrediction);
    s.on('round_complete',    onRound);
    s.on('decisions_updated', onDecisionsUpdated);

    setConnected(s.connected);

    return () => {
      s.off('connect',           onConnect);
      s.off('disconnect',        onDisconnect);
      s.off('prediction',        onPrediction);
      s.off('round_complete',    onRound);
      s.off('decisions_updated', onDecisionsUpdated);
      _refCount--;
    };
  }, []);

  return { connected, lastPrediction, lastRound, updatedDecisions, on, off };
}
