export function getApiBaseUrl() {
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL.replace(/\/$/, '');
  }
  // Development uses Vite's same-origin proxy. This avoids CORS/hostname
  // mismatches and still works when the dashboard is opened from another PC.
  if (typeof window !== 'undefined') return '';
  return 'http://127.0.0.1:8000';
}

export function getHealthUrl() {
  return `${getApiBaseUrl()}/health`;
}

