/**
 * useLiveSync.js — Single source of truth for the live dashboard.
 *
 * Merges:
 *   - Socket.IO real-time events (primary)
 *   - REST API fallback polling (secondary, only when WS is stale)
 *
 * Exposes a single `live` object consumed by Dashboard and sub-components.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import useSocket from './useSocket.js';
import { fetchHistory, fetchDecisions, fetchRiskOverview, fetchRiskHistory, bustCache } from '../api/client.js';

const STALE_THRESHOLD_MS = 20000; // if no WS event for 20s, fall back to polling
const POLL_INTERVAL_MS   = 15000;

export default function useLiveSync() {
  const {
    connected, lastRound, nextPrediction, lastResolved,
    syncState, updatedDecisions, systemStatus, requestSync,
    lastPrediction,
  } = useSocket();

  // ── Core live state ───────────────────────────────────────────────────────
  const [currentRound,    setCurrentRound]    = useState(null);
  const [prediction,      setPrediction]      = useState(null);  // for NEXT round
  const [timeline,        setTimeline]        = useState([]);    // [{round_id, multiplier, result, ...}]
  const [stats,           setStats]           = useState(null);
  const [collectorStatus, setCollectorStatus] = useState(null);
  const [decisions,       setDecisions]       = useState([]);
  const [history,         setHistory]         = useState([]);
  const [riskOverview,    setRiskOverview]    = useState(null);
  const [riskHistory,     setRiskHistory]     = useState([]);
  const [lastUpdated,     setLastUpdated]      = useState(null);

  const lastWsEventRef = useRef(null);
  const pollTimerRef   = useRef(null);

  // ── Helpers ───────────────────────────────────────────────────────────────

  const markUpdated = useCallback(() => {
    lastWsEventRef.current = Date.now();
    setLastUpdated(new Date());
  }, []);

  // ── Sync:state — full snapshot from server ────────────────────────────────
  useEffect(() => {
    if (!syncState) return;
    if (syncState.current_round)   setCurrentRound(syncState.current_round);
    if (syncState.next_prediction) setPrediction(syncState.next_prediction);
    if (syncState.stats)           setStats(syncState.stats);
    if (syncState.collector)       setCollectorStatus(syncState.collector);
    if (syncState.timeline?.length) {
      setTimeline(prev => mergeTimeline(prev, syncState.timeline));
    }
    markUpdated();
  }, [syncState, markUpdated]);

  // ── Round:new — append to history and timeline ────────────────────────────
  useEffect(() => {
    if (!lastRound) return;
    setCurrentRound(lastRound);
    setHistory(prev => {
      if (prev.some(r => r.round_id === lastRound.round_id)) return prev;
      return [...prev, lastRound].slice(-200);
    });
    setTimeline(prev => {
      const exists = prev.some(e => e.round_id === lastRound.round_id);
      if (exists) return prev;
      const entry = {
        round_id:   lastRound.round_id,
        multiplier: lastRound.multiplier,
        category:   lastRound.category,
        timestamp:  lastRound.timestamp,
        result:     null,
        prediction: null,
        confidence: null,
      };
      return [...prev, entry].slice(-50);
    });
    // Bust caches so next poll gets fresh data
    bustCache('history');
    bustCache('riskOverview');
    bustCache('riskHistory');
    markUpdated();
  }, [lastRound, markUpdated]);

  // ── Prediction:new — update prediction state and timeline ─────────────────
  useEffect(() => {
    if (!nextPrediction) return;
    setPrediction(nextPrediction);
    // Mark the NEXT round in timeline as WAITING
    const forRound = nextPrediction.for_round;
    if (forRound != null) {
      setTimeline(prev => {
        const exists = prev.some(e => e.round_id === forRound);
        if (exists) {
          return prev.map(e => e.round_id === forRound
            ? { ...e, prediction: nextPrediction.prediction, confidence: nextPrediction.confidence, result: 'WAITING' }
            : e
          );
        }
        // Add a placeholder entry for the upcoming round
        return [...prev, {
          round_id:   forRound,
          multiplier: null,
          category:   null,
          timestamp:  nextPrediction.created_at,
          result:     'WAITING',
          prediction: nextPrediction.prediction,
          confidence: nextPrediction.confidence,
        }].slice(-50);
      });
    }
    markUpdated();
  }, [nextPrediction, markUpdated]);

  // ── Prediction:resolved — update timeline with WIN/LOSS ───────────────────
  useEffect(() => {
    if (!lastResolved) return;
    const { for_round, result, actual_multiplier, actual_category } = lastResolved;
    setTimeline(prev => prev.map(e =>
      e.round_id === for_round
        ? { ...e, result, actual_multiplier, actual_category, multiplier: actual_multiplier ?? e.multiplier }
        : e
    ));
    // Update decisions list
    setDecisions(prev => prev.map(d =>
      d.for_round === for_round
        ? { ...d, result, actual_multiplier, actual_category, correct: result === 'WIN' }
        : d
    ));
    markUpdated();
  }, [lastResolved, markUpdated]);

  // ── Decisions_updated — full decisions refresh ────────────────────────────
  useEffect(() => {
    if (!updatedDecisions?.length) return;
    setDecisions(updatedDecisions);
    markUpdated();
  }, [updatedDecisions, markUpdated]);

  // ── System:status — collector health ─────────────────────────────────────
  useEffect(() => {
    if (!systemStatus) return;
    setCollectorStatus(systemStatus);
  }, [systemStatus]);

  // ── Fallback polling — only when WS is stale ─────────────────────────────
  const pollFallback = useCallback(async () => {
    const age = lastWsEventRef.current ? Date.now() - lastWsEventRef.current : Infinity;
    if (age < STALE_THRESHOLD_MS && connected) return; // WS is fresh

    try {
      const [h, ro, rh, dec] = await Promise.all([
        fetchHistory(100),
        fetchRiskOverview(),
        fetchRiskHistory(80),
        fetchDecisions(100),
      ]);
      if (h?.rounds?.length)  setHistory(h.rounds);
      if (ro)                 setRiskOverview(ro);
      if (rh?.rounds?.length) setRiskHistory(rh.rounds);
      if (dec?.decisions)     setDecisions(dec.decisions);
      if (h?.rounds?.length) {
        const last = h.rounds[h.rounds.length - 1];
        setCurrentRound(last);
      }
      markUpdated();
    } catch { /* silent */ }
  }, [connected, markUpdated]);

  useEffect(() => {
    pollTimerRef.current = setInterval(pollFallback, POLL_INTERVAL_MS);
    return () => clearInterval(pollTimerRef.current);
  }, [pollFallback]);

  // ── Initial load ──────────────────────────────────────────────────────────
  useEffect(() => {
    pollFallback();
    // Also request WS sync immediately
    if (connected) requestSync();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (connected) requestSync();
  }, [connected, requestSync]);

  // ── Consistency check — every 5s verify frontend == backend ──────────────
  useEffect(() => {
    const t = setInterval(() => {
      if (!connected) return;
      const age = lastWsEventRef.current ? Date.now() - lastWsEventRef.current : Infinity;
      if (age > STALE_THRESHOLD_MS) {
        requestSync();
      }
    }, 5000);
    return () => clearInterval(t);
  }, [connected, requestSync]);

  return {
    // Live state
    connected,
    currentRound,
    prediction,       // always for NEXT round
    timeline,         // [{round_id, multiplier, result: WIN|LOSS|WAITING|null, ...}]
    stats,
    collectorStatus,
    decisions,
    history,
    riskOverview,
    riskHistory,
    lastUpdated,
    // Actions
    requestSync,
  };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function mergeTimeline(prev, incoming) {
  const map = new Map(prev.map(e => [e.round_id, e]));
  for (const entry of incoming) {
    const existing = map.get(entry.round_id);
    // Prefer resolved entries over WAITING
    if (!existing || entry.result === 'WIN' || entry.result === 'LOSS') {
      map.set(entry.round_id, { ...(existing || {}), ...entry });
    }
  }
  return Array.from(map.values())
    .sort((a, b) => a.round_id - b.round_id)
    .slice(-50);
}
