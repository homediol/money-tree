/**
 * useSocket.js — Socket.IO connection with the new event model.
 *
 * Events from server:
 *   round:new            — new round detected by collector
 *   prediction:new       — prediction for NEXT round
 *   prediction:resolved  — WIN/LOSS resolved for a previous prediction
 *   sync:state           — full dashboard snapshot
 *   decisions_updated    — backfill complete
 *   system:status        — collector health metrics
 *
 * Client → server:
 *   sync:request         — request immediate full state sync
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { io } from 'socket.io-client';

let _socket = null;

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
  const [connected,          setConnected]          = useState(false);
  const [lastRound,          setLastRound]           = useState(null);
  const [nextPrediction,     setNextPrediction]      = useState(null);
  const [lastResolved,       setLastResolved]        = useState(null);
  const [syncState,          setSyncState]           = useState(null);
  const [updatedDecisions,   setUpdatedDecisions]    = useState(null);
  const [systemStatus,       setSystemStatus]        = useState(null);

  // Legacy compat
  const [lastPrediction,     setLastPrediction]      = useState(null);

  const socketRef = useRef(null);

  const requestSync = useCallback(() => {
    socketRef.current?.emit('sync:request');
  }, []);

  useEffect(() => {
    const s = getSocket();
    socketRef.current = s;

    const onConnect    = () => { setConnected(true);  s.emit('sync:request'); };
    const onDisconnect = () => setConnected(false);

    const onRoundNew   = (data) => setLastRound(data);

    const onPredNew    = (data) => {
      setNextPrediction(data);
      setLastPrediction(data); // legacy compat
    };

    const onResolved   = (data) => setLastResolved(data);
    const onSyncState  = (data) => setSyncState(data);
    const onDecisions  = (data) => setUpdatedDecisions(data?.decisions ?? null);
    const onSysStatus  = (data) => setSystemStatus(data);

    s.on('connect',              onConnect);
    s.on('disconnect',           onDisconnect);
    s.on('round:new',            onRoundNew);
    s.on('prediction:new',       onPredNew);
    s.on('prediction:resolved',  onResolved);
    s.on('sync:state',           onSyncState);
    s.on('decisions_updated',    onDecisions);
    s.on('system:status',        onSysStatus);

    // Legacy event names (backward compat with old backend)
    s.on('round_complete',       onRoundNew);
    s.on('prediction',           onPredNew);

    setConnected(s.connected);
    if (s.connected) s.emit('sync:request');

    return () => {
      s.off('connect',             onConnect);
      s.off('disconnect',          onDisconnect);
      s.off('round:new',           onRoundNew);
      s.off('prediction:new',      onPredNew);
      s.off('prediction:resolved', onResolved);
      s.off('sync:state',          onSyncState);
      s.off('decisions_updated',   onDecisions);
      s.off('system:status',       onSysStatus);
      s.off('round_complete',      onRoundNew);
      s.off('prediction',          onPredNew);
    };
  }, []);

  return {
    connected,
    lastRound,
    nextPrediction,
    lastResolved,
    syncState,
    updatedDecisions,
    systemStatus,
    requestSync,
    // legacy
    lastPrediction,
  };
}
