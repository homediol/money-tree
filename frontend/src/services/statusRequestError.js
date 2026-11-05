import { getBackendSnapshot } from './backendConnection.js';

function readableDetail(error) {
  const detail = error?.response?.data?.detail;
  const value = typeof detail === 'string'
    ? detail
    : detail?.reason || detail?.error || error?.message;
  return typeof value === 'string' ? value.replaceAll('_', ' ') : '';
}

export function statusRequestError(error, subject) {
  const connection = getBackendSnapshot();
  const route = String(error?.config?.url || '').split('?')[0];
  const endpoint = route ? ` (${route})` : '';
  const retry = connection.nextRetryMs > 0
    ? ` Retrying in ${Math.ceil(connection.nextRetryMs / 1000)}s.` : '';

  if (error?.response) {
    const status = error.response.status;
    const detail = readableDetail(error);
    const reason = detail ? `: ${detail}` : '';
    return `${subject} request failed${endpoint} (HTTP ${status})${reason}.`;
  }
  if (error?.code === 'ERR_BACKEND_RECONNECTING' || connection.status === 'reconnecting') {
    const state = connection.status === 'reconnecting'
      ? 'disconnected; automatic reconnection is active'
      : connection.status === 'checking'
        ? 'not connected yet; the connection check is running'
        : `not accepting requests (connection state: ${connection.status})`;
    return `${subject} unavailable${endpoint}: backend ${state}.${retry}`;
  }
  if (error?.code === 'ECONNABORTED' || error?.code === 'ETIMEDOUT') {
    return `${subject} request timed out${endpoint}. The last known backend state is ${connection.status}.${retry}`;
  }
  return `${subject} could not reach the backend${endpoint}. The last known backend state is ${connection.status}.${retry}`;
}
