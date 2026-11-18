import axios from 'axios';
import { API_UNAUTHORIZED_EVENT, getApiToken, logout } from '../auth.js';
import {
  backendRequestsAllowed,
  getBackendSnapshot,
  reportBackendRequestFailure,
} from './backendConnection.js';
import { getApiBaseUrl } from './endpoints.js';
import { safeReadRetryDelay, shouldRetryReadFailure } from './readRetryPolicy.js';

export const api = axios.create({ baseURL: getApiBaseUrl(), timeout: 20000, withCredentials: true });

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
