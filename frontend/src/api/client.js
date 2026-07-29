import axios from 'axios';

// ── Main Prediction API (Flask, port 5000) ────────────────────────────────

export const api = axios.create({
  baseURL: '/api',
  timeout: 12000,
});

// Separate instance for /predict which can take up to 90s on cold start
export const predictApi = axios.create({
  baseURL: '/api',
  timeout: 95000,
});

// ── Bot Control API (FastAPI, port 5001) ──────────────────────────────────

export const botApi = axios.create({
  baseURL: '/bot-api',
  timeout: 10000,
});

// ── Simple response cache (TTL-based) ────────────────────────────────────
const _cache = new Map();
function cached(key, ttlMs, fn) {
  const hit = _cache.get(key);
  if (hit && Date.now() - hit.ts < ttlMs) return Promise.resolve(hit.data);
  return fn().then(data => { _cache.set(key, { data, ts: Date.now() }); return data; });
}
export function bustCache(key) { _cache.delete(key); }

// ═════════════════════════════════════════════════════════════════════════
// PREDICTION ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

export async function fetchPrediction() {
  const { data } = await predictApi.get('/predict');
  bustCache('history'); bustCache('riskOverview'); bustCache('riskHistory');
  return data;
}

export async function fetchFastPrediction() {
  const { data } = await api.get('/fast-predict');
  return data;
}

export async function fetchReadiness() {
  const { data } = await api.get('/ready');
  return data;
}

export async function fetchHistory(limit = 80) {
  return cached('history', 8000, async () => {
    const { data } = await api.get('/history', { params: { limit } });
    return data;
  });
}

export async function fetchAccuracy() {
  return cached('accuracy', 15000, async () => {
    const { data } = await api.get('/accuracy');
    return data;
  });
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
  bustCache('accuracy');
  return data;
}

// ═════════════════════════════════════════════════════════════════════════
// RISK ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

export async function fetchRiskOverview() {
  return cached('riskOverview', 10000, async () => {
    const { data } = await api.get('/risk/overview');
    return data;
  });
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
  return cached('riskHistory', 10000, async () => {
    const { data } = await api.get('/risk/history', { params: { limit } });
    return data;
  });
}

// ═════════════════════════════════════════════════════════════════════════
// INTELLIGENCE ENDPOINTS
// ═════════════════════════════════════════════════════════════════════════

export async function fetchSkipQuality() {
  return cached('skipQuality', 20000, async () => {
    const { data } = await api.get('/skip-quality');
    return data;
  });
}

export async function fetchVhQuality() {
  return cached('vhQuality', 20000, async () => {
    const { data } = await api.get('/vh-quality');
    return data;
  });
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