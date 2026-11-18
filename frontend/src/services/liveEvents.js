import { getWebSocketAuthProtocols, getWebSocketUrl } from '../auth.js';

const subscribers = new Set();
let socket; let retryTimer; let attempts = 0; let referenceCount = 0;
const publish = (event) => subscribers.forEach((listener) => { try { listener(event); } catch { /* A consumer cannot interrupt transport delivery. */ } });

function connect() {
  if (referenceCount === 0 || (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState))) return;
  try {
    const connection = new WebSocket(getWebSocketUrl(), getWebSocketAuthProtocols());
    socket = connection;
    connection.onopen = () => {
      if (socket !== connection) return;
      const restored = attempts > 0; attempts = 0;
      publish({ __transport: true, status: restored ? 'restored' : 'connected' });
    };
    connection.onmessage = ({ data }) => {
      if (socket !== connection) return;
      try { publish(JSON.parse(data)); } catch { /* Ignore malformed frames. */ }
    };
    connection.onerror = () => {};
    connection.onclose = () => {
      if (socket !== connection) return;
      socket = null;
      if (referenceCount === 0) return;
      if (attempts === 0) publish({ __transport: true, status: 'disconnected' });
      const delay = Math.min(1000 * (2 ** attempts), 15000); attempts += 1;
      retryTimer = window.setTimeout(connect, delay);
    };
  } catch {
    attempts += 1;
    retryTimer = window.setTimeout(connect, Math.min(1000 * (2 ** attempts), 15000));
  }
}

export function subscribeLiveEvents(listener) {
  subscribers.add(listener); referenceCount += 1;
  if (referenceCount === 1) connect();
  return () => {
    subscribers.delete(listener); referenceCount = Math.max(0, referenceCount - 1);
    if (referenceCount === 0) {
      if (retryTimer) window.clearTimeout(retryTimer);
      retryTimer = null;
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
      socket = null; attempts = 0;
    }
  };
}
