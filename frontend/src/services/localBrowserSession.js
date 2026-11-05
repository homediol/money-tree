export async function bootstrapLocalBrowserSession(url, fetchImpl = globalThis.fetch) {
  const response = await fetchImpl(url, {
    method: 'POST',
    cache: 'no-store',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || payload?.ok !== true || payload?.session !== 'LOCAL_BROWSER') {
    throw new Error(payload?.error || `Local dashboard session setup failed (${response.status})`);
  }
  return payload;
}
