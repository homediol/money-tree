import axios from 'axios';

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 20000,
});

export async function getCurrentSignal() {
  const { data } = await api.get('/api/signal/current');
  return data;
}

export async function getStatistics() {
  const { data } = await api.get('/api/statistics');
  return data;
}

export async function getPatterns() {
  const { data } = await api.get('/api/patterns');
  return data.patterns || [];
}

export async function getHistory(limit = 250) {
  const { data } = await api.get('/api/history', { params: { limit } });
  return data;
}

export async function getSignalHistory() {
  const { data } = await api.get('/api/signal/history');
  return data.signals || [];
}

export async function getModelPerformance() {
  const { data } = await api.get('/api/models/performance');
  return data;
}

export async function trainModels() {
  const { data } = await api.post('/api/models/train');
  return data;
}


// --- Betting automation (Part 1) ---

export async function getBettingStatus() {
  const { data } = await api.get('/api/betting/status');
  return data;
}

export async function getBettingProfiles() {
  const { data } = await api.get('/api/betting/profiles');
  return data; // { keys: [...], profiles: { PROFILE_A: {...}, ... } }
}

export async function checkBettingBrowser(includeSnapshot = false) {
  const { data } = await api.get('/api/betting/browser', {
    params: includeSnapshot ? { include_snapshot: true } : {},
  });
  return data;
}

export async function startBettingSession(payload) {
  const { data } = await api.post('/api/betting/session', { action: 'start', ...payload });
  return data;
}

export async function stopBettingSession() {
  const { data } = await api.post('/api/betting/session', { action: 'stop' });
  return data;
}

export async function emergencyStopBetting() {
  const { data } = await api.post('/api/betting/emergency-stop');
  return data;
}

export async function submitBettingDecision(payload) {
  const { data } = await api.post('/api/betting/decisions', payload);
  return data;
}

export async function getBettingLedger(params = {}) {
  const { data } = await api.get('/api/betting/ledger', { params });
  return data;
}



