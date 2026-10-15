import axios from 'axios';
import { API_UNAUTHORIZED_EVENT, getApiToken, logout } from '../auth.js';
import {
  backendRequestsAllowed,
  getBackendSnapshot,
  reportBackendRequestFailure,
} from './backendConnection.js';
import { getApiBaseUrl } from './endpoints.js';
import { safeReadRetryDelay, shouldRetryReadFailure } from './readRetryPolicy.js';

export const api = axios.create({ baseURL: getApiBaseUrl(), timeout: 20000 });

api.interceptors.request.use((config) => {
  const retryingSafeRead = config.__allowReadDuringReconnect
    && String(config.method || 'get').toLowerCase() === 'get';
  if (!backendRequestsAllowed() && config.url !== '/health' && !retryingSafeRead) {
    const error = new axios.AxiosError('Backend is reconnecting', 'ERR_BACKEND_RECONNECTING', config);
    error.isBackendUnavailable = true;
    return Promise.reject(error);
  }
  const token = getApiToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use((response) => response, async (error) => {
  const config = error?.config;
  if (shouldRetryReadFailure(config, error)) {
    config._safeReadRetryCount = (Number(config._safeReadRetryCount) || 0) + 1;
    config.__allowReadDuringReconnect = true;
    await new Promise((resolve) => setTimeout(resolve, safeReadRetryDelay(config._safeReadRetryCount)));
    return api.request(config);
  }
  if (error?.response?.status === 401) {
    logout();
    const health = getBackendSnapshot().health;
    if (health?.public_access !== true && health?.public_read_only !== true) {
      window.dispatchEvent(new Event(API_UNAUTHORIZED_EVENT));
    }
  }
  reportBackendRequestFailure(error);
  return Promise.reject(error);
});

export async function getBettingStatus() {
  return (await api.get('/api/betting/status')).data;
}

export async function checkBettingBrowser(includeSnapshot = false) {
  return (await api.get('/api/betting/browser', {
    params: includeSnapshot ? { include_snapshot: true } : {},
  })).data;
}

export async function stopBettingSession() {
  return (await api.post('/api/betting/stop')).data;
}

export async function emergencyStopBetting() {
  return (await api.post('/api/betting/emergency-stop')).data;
}

export async function getBettingLedger(params = {}) {
  return (await api.get('/api/betting/ledger', { params })).data;
}

export async function getRiskStatus() {
  return (await api.get('/api/risk/status')).data;
}

export async function resetRiskEmergency() {
  return (await api.post('/api/risk/reset-emergency')).data;
}

export async function getResultExecutions(limit = 50) {
  return (await api.get('/api/results/executions', { params: { limit } })).data.executions || [];
}

export async function getExecutionDetail(id) {
  return (await api.get(`/api/results/executions/${encodeURIComponent(id)}`)).data;
}

export async function getBalanceLedger(limit = 100) {
  return (await api.get('/api/results/ledger', { params: { limit } })).data.entries || [];
}
