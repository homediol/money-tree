const PREFIX = '/api/v1/behaviour';

function baseUrl() {
  return String(import.meta.env?.VITE_BEHAVIOUR_API_BASE_URL || '').replace(/\/$/, '');
}

export async function behaviourGet(path, { signal } = {}) {
  const response = await fetch(`${baseUrl()}${PREFIX}${path}`, {
    method: 'GET', cache: 'no-store', credentials: 'omit', signal,
    headers: { Accept: 'application/json' },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message || detail?.code;
    throw new Error(message || `Behaviour API request failed (${response.status})`);
  }
  return payload;
}

export async function behaviourDelete(path, body) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`${baseUrl()}${PREFIX}${path}`, {
    method: 'DELETE', cache: 'no-store', credentials: 'omit',
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message || detail?.code;
    throw new Error(message || `Behaviour API request failed (${response.status})`);
  }
  return payload;
}

export async function behaviourPut(path, body) {
  const response = await fetch(`${baseUrl()}${PREFIX}${path}`, {
    method: 'PUT', cache: 'no-store', credentials: 'omit',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error((typeof detail === 'string' ? detail : detail?.message || detail?.code)
      || `Behaviour API request failed (${response.status})`);
  }
  return payload;
}

export const behaviourApiPrefix = PREFIX;
