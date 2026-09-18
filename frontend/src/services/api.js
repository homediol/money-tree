import axios from 'axios';
import { getApiToken } from '../auth.js';

export function getApiBaseUrl() {
  if (import.meta.env.VITE_API_BASE_URL) return import.meta.env.VITE_API_BASE_URL;
  if (typeof window === 'undefined') return 'http://localhost:8000';
  return `${window.location.protocol}//${window.location.hostname}:8000`;
}

export const api = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 20000,
});

api.interceptors.request.use((config) => {
  const token = getApiToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
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

export async function getHistoryStatus() {
  const { data } = await api.get('/api/history/status');
  return data;
}

export async function getHistoryStats() {
  const { data } = await api.get('/api/history/stats');
  return data;
}

export async function getRecentHistory(limit = 25) {
  const { data } = await api.get('/api/history/recent', { params: { limit } });
  return data;
}

export async function startHistoryCollector() {
  const { data } = await api.post('/api/history/start');
  return data;
}

export async function stopHistoryCollector() {
  const { data } = await api.post('/api/history/stop');
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

export async function getSystemStatus() {
  const { data } = await api.get('/api/system/status');
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
  const { data } = await api.post('/api/betting/start', payload);
  return data;
}

export async function stopBettingSession() {
  const { data } = await api.post('/api/betting/stop');
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

// --- Risk management (Part 2) ---

export async function getRiskStatus() {
  const { data } = await api.get('/api/risk/status');
  return data;
}

export async function getRiskProfiles() {
  const { data } = await api.get('/api/risk/profiles');
  return data;
}

export async function getRiskSession() {
  const { data } = await api.get('/api/risk/session');
  return data;
}

export async function setRiskProfile(profile) {
  const { data } = await api.post('/api/risk/profile', { profile });
  return data;
}

export async function resetRiskEmergency() {
  const { data } = await api.post('/api/risk/reset-emergency');
  return data;
}
