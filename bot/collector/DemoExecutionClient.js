const API_BASE = (process.env.COLLECTOR_BACKEND_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const OWNER_SOURCE = 'WINNER_DEMO_BROWSER_OWNER';

async function request(path, payload, { fetchImpl = globalThis.fetch, timeoutMs = 2500 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  timer.unref?.();
  const headers = { 'content-type': 'application/json' };
  if (process.env.API_KEY) headers.authorization = `Bearer ${process.env.API_KEY}`;
  try {
    const response = await fetchImpl(`${API_BASE}${path}`, { method: 'POST', headers,
      body: JSON.stringify(payload), signal: controller.signal });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.ok !== true) {
      throw Object.assign(new Error(`Demo execution API returned HTTP ${response.status}`), {
        status: response.status, backend_error: body.error || null,
      });
    }
    return body;
  } finally { clearTimeout(timer); }
}

export async function claimDemoExecution(options = {}) {
  return request('/api/collector/demo-execution/claim', { source: OWNER_SOURCE }, options);
}

export async function reportDemoExecution(attemptId, claimToken, result, options = {}) {
  return request(`/api/collector/demo-execution/${encodeURIComponent(attemptId)}/result`, {
    source: OWNER_SOURCE, claim_token: claimToken, result,
  }, options);
}
