import axios from 'axios';
import { API_UNAUTHORIZED_EVENT, getApiToken, logout } from '../auth.js';
import {
  backendRequestsAllowed,
  reportBackendRequestFailure,
} from './backendConnection.js';
import { getApiBaseUrl } from './endpoints.js';

export { getApiBaseUrl } from './endpoints.js';

export const api = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 20000,
});

api.interceptors.request.use((config) => {
  if (!backendRequestsAllowed() && config.url !== '/health') {
    const error = new axios.AxiosError(
      'Backend is reconnecting', 'ERR_BACKEND_RECONNECTING', config,
    );
    error.isBackendUnavailable = true;
    return Promise.reject(error);
  }
  const token = getApiToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      logout();
      window.dispatchEvent(new Event(API_UNAUTHORIZED_EVENT));
    }
    reportBackendRequestFailure(error);
    return Promise.reject(error);
  },
);

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

export async function getPatternReport(target = 2) {
  const { data } = await api.get('/api/patterns/report', { params: { target } });
  return data;
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

export async function getSystemReadiness() {
  const { data } = await api.get('/api/readiness', { timeout: 4000 });
  return data.readiness;
}

export async function getDataStatus() {
  const { data } = await api.get('/api/data/status');
  return data;
}

export async function getFeatureMetadata() {
  const { data } = await api.get('/api/features');
  return data;
}

export async function getLatestFeatures() {
  const { data } = await api.get('/api/features/latest');
  return data;
}

export async function getDatasetStatus() {
  const { data } = await api.get('/api/dataset/status');
  return data;
}

export async function trainModels() {
  const { data } = await api.post('/api/ml/train');
  return data;
}

export async function getMLStatus() {
  const { data } = await api.get('/api/ml/status');
  return data;
}

export async function getMLMetrics() {
  const { data } = await api.get('/api/ml/metrics');
  return data;
}

export async function getLatestMLPrediction() {
  const { data } = await api.get('/api/ml/prediction/latest');
  return data.prediction;
}

export async function getMLEstimate() {
  const { data } = await api.get('/api/ml/estimate');
  return data;
}

export async function getCurrentEvidence() {
  const { data } = await api.get('/api/evidence/current');
  return data.evidence;
}

export async function getRecentEvidence(limit = 25) {
  const { data } = await api.get('/api/evidence/recent', { params: { limit } });
  return data.evidence || [];
}

export async function getCurrentDecision() {
  const { data } = await api.get('/api/decisions/current');
  return data.decision;
}

export async function getRecentDecisions(limit = 25) {
  const { data } = await api.get('/api/decisions/recent', { params: { limit } });
  return data.decisions || [];
}

export async function getDecisionStatus() {
  const { data } = await api.get('/api/decisions/status');
  return data;
}

export async function evaluateDecision() {
  const { data } = await api.post('/api/decisions/evaluate');
  return data.decision;
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

export async function getResultExecutions(limit = 50) {
  const { data } = await api.get('/api/results/executions', { params: { limit } });
  return data.executions || [];
}

export async function getExecutionDetail(id) {
  const { data } = await api.get(`/api/results/executions/${id}`);
  return data;
}

export async function getBalanceLedger(limit = 100) {
  const { data } = await api.get('/api/results/ledger', { params: { limit } });
  return data.entries || [];
}

export async function getReconciliations(limit = 50) {
  const { data } = await api.get('/api/results/reconciliations', { params: { limit } });
  return data.reconciliations || [];
}

export async function getSessionMetrics(limit = 10) {
  const { data } = await api.get('/api/results/session-metrics', { params: { limit } });
  return data.sessions || [];
}

export async function runBacktest(config = {}) {
  const { data } = await api.post('/api/backtests/run', config);
  return data.result;
}

export async function listBacktests(limit = 25) {
  const { data } = await api.get('/api/backtests', { params: { limit } });
  return data.runs || [];
}

export async function getBacktest(runId) {
  const { data } = await api.get(`/api/backtests/${runId}`);
  return data.result;
}
