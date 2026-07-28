import axios from 'axios';
import { io } from 'socket.io-client';

// ── Main Prediction API (Flask, port 5000) ────────────────────────────────

export const api = axios.create({
  baseURL: '/api',
  timeout: 10000,  // 10s for most endpoints
});

// Separate instance for /predict which can take up to 90s on cold start
export const predictApi = axios.create({
  baseURL: '/api',
  timeout: 95000,  // 95s — allows full TF model cold-load
});

// ── Bot Control API (FastAPI, port 5001) ──────────────────────────────────

export const botApi = axios.create({
  baseURL: '/bot-api',
  timeout: 10000,
});

// ═════════════════════════════════════════════════════════════════════════
// WEBSOCKET CLIENT (real-time, <50 ms latency)
// Falls back to polling if socket.io is unavailable
// ═════════════════════════════════════════════════════════════════════════

let _socket = null;
let _wsReady = false;

/**
 * Connect to the Flask-SocketIO server.
 * Returns the socket instance (or null if socket.io-client not installed).
 */
export function connectSocket() {
  if (_socket) return _socket;
  try {
    _socket = io({
      path: '/socket.io',
      transports: ['websocket', 'polling'],
      reconnectionDelay: 1000,
      reconnectionAttempts: 10,
    });

    _socket.on('connect', () => {
      _wsReady = true;
      console.log('[WS] Connected');
    });
    _socket.on('disconnect', () => {
      _wsReady = false;
      console.log('[WS] Disconnected');
    });
    _socket.on('connect_error', () => {
      _wsReady = false;
    });

    return _socket;
  } catch {
    return null;
  }
}

export function isWsReady() { return _wsReady; }

export function onPrediction(callback) {
  const s = connectSocket();
  if (s) s.on('prediction', callback);
}

export function onRoundComplete(callback) {
  const s = connectSocket();
  if (s) s.on('round_complete', callback);
}

export function onStateUpdate(callback) {
  const s = connectSocket();
  if (s) s.on('state', callback);
}

export function onDecisionsUpdated(callback) {
  const s = connectSocket();
  if (s) s.on('decisions_updated', callback);
}

export function disconnectSocket() {
  if (_socket) { _socket.disconnect(); _socket = null; _wsReady = false; }
}

// ═════════════════════════════════════════════════════════════════════════
// PREDICTION ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

/** Full prediction with storage — uses long timeout for cold TF model start */
export async function fetchPrediction() {
  const { data } = await predictApi.get('/predict');
  return data;
}

/** Fast prediction — <50 ms, no disk write, for high-frequency polling */
export async function fetchFastPrediction() {
  const { data } = await predictApi.get('/fast-predict');
  return data;
}

/** Check if backend model is warmed up yet */
export async function fetchReadiness() {
  const { data } = await api.get('/ready');
  return data;
}

export async function fetchHistory(limit = 80) {
  const { data } = await api.get('/history', { params: { limit } });
  return data;
}

export async function fetchAccuracy() {
  const { data } = await api.get('/accuracy');
  return data;
}

export async function fetchDecisions(limit = 50) {
  const { data } = await api.get('/decisions', { params: { limit } });
  return data;
}

export async function trainModel(epochs = 30) {
  const { data } = await api.post('/train', { epochs });
  return data;
}

export async function runBackfill() {
  const { data } = await api.post('/backfill');
  return data;
}

// ═════════════════════════════════════════════════════════════════════════
// RISK ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

export async function fetchRiskOverview() {
  const { data } = await api.get('/risk/overview');
  return data;
}

export async function fetchRiskVolatility() {
  const { data } = await api.get('/risk/volatility');
  return data;
}

export async function fetchRiskStreaks() {
  const { data } = await api.get('/risk/streaks');
  return data;
}

export async function fetchRiskMovingAverages() {
  const { data } = await api.get('/risk/moving-averages');
  return data;
}

export async function fetchRiskHistory(limit = 100) {
  const { data } = await api.get('/risk/history', { params: { limit } });
  return data;
}

// ═════════════════════════════════════════════════════════════════════════
// INTELLIGENCE ENDPOINTS (skip quality, VH guard, calibration, momentum)
// ═════════════════════════════════════════════════════════════════════════

export async function fetchSkipQuality() {
  const { data } = await api.get('/skip-quality');
  return data;
}

export async function fetchVhQuality() {
  const { data } = await api.get('/vh-quality');
  return data;
}

export async function fetchCalibration() {
  const { data } = await api.get('/calibration');
  return data;
}

export async function resetCalibration() {
  const { data } = await api.post('/calibration/reset');
  return data;
}

export async function fetchConfidenceCalibration() {
  const { data } = await api.get('/confidence-calibration');
  return data;
}

export async function fetchConfidenceAudit(limit = 50) {
  const { data } = await api.get('/confidence-calibration/audit', { params: { limit } });
  return data;
}

export async function fetchStreakMatrix() {
  const { data } = await api.get('/streak-matrix');
  return data;
}

export async function fetchRiskTier() {
  const { data } = await api.get('/risk-tier');
  return data;
}

// ═════════════════════════════════════════════════════════════════════════
// BOT AUTOMATION ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

export async function startBot(phone, password, headless = false) {
  const { data } = await botApi.post('/bot/start', { phone, password, headless });
  return data;
}

export async function stopBot() {
  const { data } = await botApi.post('/bot/stop');
  return data;
}

export async function fetchBotStatus() {
  const { data } = await botApi.get('/bot/status');
  return data;
}

export async function fetchBotLogs(tail = 100) {
  const { data } = await botApi.get('/bot/logs', { params: { tail } });
  return data;
}
