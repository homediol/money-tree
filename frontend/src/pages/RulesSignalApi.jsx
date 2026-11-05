import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, Check, CheckCircle2, Clipboard, Code2, Database,
  FileText, KeyRound, LoaderCircle, Radio, RefreshCw, ShieldCheck, Signal,
  Wifi, WifiOff, XCircle,
} from 'lucide-react';
import { api } from '../services/api.js';
import { getApiBaseUrl } from '../services/endpoints.js';
import { copyTextToClipboard } from '../services/rulesSignalApi.js';
import { useBackendConnection } from '../services/backendConnection.js';
import PageHeader from '../components/PageHeader.jsx';
import LiveSignalActionMonitor from '../components/LiveSignalActionMonitor.jsx';

const TABS = ['Overview', 'API Testing', 'API Keys', 'Documentation'];
const READ_SCOPES = ['signals:read', 'rules:read', 'metrics:read'];

const ENDPOINTS = [
  { method: 'GET', path: '/health/live', purpose: 'Process liveness check. Returns 200 while FastAPI can serve requests.', permission: 'Public', params: 'None', request: 'No body.', response: { ok: true, status: 'ONLINE' }, errors: 'Network error if the service is unreachable.' },
  { method: 'GET', path: '/health/ready', purpose: 'Dependency readiness. Checks application startup and database connectivity.', permission: 'Public', params: 'None', request: 'No body.', response: { ok: true, status: 'ONLINE', database: { status: 'CONNECTED', backend: 'postgresql' } }, errors: '503 when the API is starting or database is unavailable.' },
  { method: 'GET', path: '/api/v1/signals/latest/action', purpose: 'Public projection of the latest canonical published signal. BET maps to OK; an actually published WAIT is preserved. The index is that signal’s target_round_index.', permission: 'Public', params: 'None', request: 'No body.', response: { action: 'OK', index: '#20063' }, errors: 'When there is no eligible published action, returns { ok: true, signal: null } like /signals/latest. 403 disallowed browser origin; 426 HTTPS required; 429 rate limit; 503 storage unavailable.' },
  { method: 'GET', path: '/api/v1/signals/latest', purpose: 'Read the latest stored authoritative BET signal, or null before the first signal.', permission: 'signals:read', params: 'None', request: 'No body.', response: { ok: true, signal: null }, errors: '401 invalid key; 403 missing scope; 429 rate limit; 503 storage unavailable.' },
  { method: 'GET', path: '/api/v1/signals', purpose: 'List recent stored BET signals.', permission: 'signals:read', params: 'limit (1–1000), session_id (optional)', request: 'GET /api/v1/signals?limit=100', response: { ok: true, signals: [], count: 0 }, errors: '401, 403, 422 invalid limit, 429, 503.' },
  { method: 'GET', path: '/api/v1/signals/{signal_id}', purpose: 'Retrieve one stored signal by its ID.', permission: 'signals:read', params: 'signal_id path parameter', request: 'GET /api/v1/signals/bet-signal-id', response: { ok: true, signal: null }, errors: '401, 403, 404 signal not found, 429, 503.' },
  { method: 'GET', path: '/api/v1/rules/selected', purpose: 'Read all selected and enabled rules from the active SHADOW session.', permission: 'rules:read', params: 'None', request: 'No body.', response: { ok: true, session_id: null, selected_rules: [] }, errors: '401, 403, 429, 503.' },
  { method: 'GET', path: '/api/v1/rules/matched', purpose: 'Read matched rules from the latest published signal.', permission: 'rules:read', params: 'None', request: 'No body.', response: { ok: true, matched_rules: [] }, errors: '401, 403, 429, 503.' },
  { method: 'GET', path: '/api/v1/decisions/latest', purpose: 'Read the latest Rules Engine shared decision.', permission: 'rules:read', params: 'None', request: 'No body.', response: { ok: true, decision: null }, errors: '401, 403, 429, 503.' },
  { method: 'GET', path: '/api/v1/metrics', purpose: 'Read signal counts, active consumer connections, delivery failures, and measured fanout latency.', permission: 'metrics:read', params: 'None', request: 'No body.', response: { ok: true, total_bet_signals: 0, connected_external_consumers: 0, failed_deliveries: 0, average_delivery_latency_ms: null }, errors: '401, 403, 429, 503.' },
  { method: 'GET', path: '/api/v1/stream/signals', purpose: 'Open a server-sent event stream for live BET signals. Use Last-Event-ID or after_signal_id when reconnecting.', permission: 'signals:read', params: 'after_signal_id (optional), Last-Event-ID header (optional)', request: 'GET /api/v1/stream/signals?after_signal_id=previous-signal-id', response: 'Content-Type: text/event-stream; event: bet_signal; id: signal_id; data: authoritative BET JSON', errors: '401, 403, 409 invalid reconnect cursor, 429, 503.' },
  { method: 'POST', path: '/api/v1/ws/ticket', purpose: 'Issue a one-use, 30 second ticket for browser WebSocket authentication. Send the project key in Authorization, then send the ticket as the first WebSocket JSON message.', permission: 'signals:read', params: 'None', request: 'No body.', response: { ok: true, ticket: '<short-lived one-use value>', expires_in_seconds: 30 }, errors: '401 invalid key; 403 missing scope or disallowed origin; 429 rate limit; 503 storage unavailable.' },
  { method: 'POST', path: '/api/v1/signals', purpose: 'Accept an authoritative external Rules Engine result with one or more TRUE selected rules. The built-in Shadow Engine uses the same server-side validation and distribution service after a committed decision. This API does not evaluate rules or place bets.', permission: 'signals:write for external publishers; built-in Shadow bridge is internal', params: 'JSON body', request: { action: 'BET', rule: 'RULE_1', description: 'RULE_1 returned TRUE', selected_rules: ['RULE_1', 'RULE_2'], matched_rules: ['RULE_1'], round_id: 'committed-source-round', target_round_id: 'provider-verified-next-round', session_id: 'active-shadow-session', mode: 'SHADOW', timestamp: '2026-10-17T12:00:00Z' }, response: { ok: true, accepted: true, delivery: 'PUBLISHED', persistence: 'COMMITTED', signal: { action: 'BET', settlement_status: 'PENDING' } }, errors: '401 publisher auth; 409 stale target/session; 422 invalid or unselected rule; 503 authoritative store unavailable.' },
  { method: 'POST', path: '/api/v1/publisher/heartbeat', purpose: 'Authenticated internal publisher heartbeat. Sent every 15 seconds; a heartbeat becomes STALE after 45 seconds.', permission: 'signals:write (internal publisher only)', params: 'JSON publisher_id, shadow_status, queue_depth, queue_status, delivery_failures', request: { publisher_id: 'winner-predict-node-rules', shadow_status: 'ACTIVE', queue_depth: 0, queue_status: 'IDLE', delivery_failures: 0 }, response: { ok: true, publisher: 'CONNECTED', received_at: '2026-10-17T12:00:00Z', heartbeat_interval_seconds: 15, stale_after_seconds: 45 }, errors: '401 publisher auth; 422 invalid heartbeat; 429 rate limit.' },
];

const REALTIME_ENDPOINTS = [
  { method: 'WS', path: '/api/v1/ws/signals', purpose: 'Authenticated WebSocket stream with ready, heartbeat, and signal messages. Browser clients first request a short-lived ticket with POST /api/v1/ws/ticket and send it as their first JSON message; native clients may use an Authorization header. Replays up to 1,000 recent signals after connect; deduplicate by signal_id.' },
  { method: 'GET', path: '/api/v1/stream/signals', purpose: 'Authenticated SSE stream. Event name is bet_signal, event ID is signal_id, and comment heartbeats are sent every 15 seconds. Reconnect with Last-Event-ID or after_signal_id; use REST to reconcile gaps.' },
];

const EXAMPLES = {
  python: `import os\nimport requests\n\nBASE_URL = os.environ["RULES_API_URL"].rstrip("/")\nAPI_KEY = os.environ["RULES_PROJECT_TOKEN"]\nresponse = requests.get(\n    f"{BASE_URL}/api/v1/signals/latest",\n    headers={"Authorization": f"Bearer {API_KEY}"},\n    timeout=5,\n)\nresponse.raise_for_status()\nsignal = response.json().get("signal")\nif signal is None:\n    print("NO_SIGNALS_YET")\nelse:\n    print(signal["action"], signal["matched_rules"], signal["target_round_id"])`,
  pythonWs: `import asyncio\nimport json\nimport os\nimport websockets\n\nasync def main():\n    url = os.environ["RULES_WS_URL"]  # wss://your-api.example/api/v1/ws/signals\n    token = os.environ["RULES_PROJECT_TOKEN"]\n    async with websockets.connect(\n        url, additional_headers={"Authorization": f"Bearer {token}"}\n    ) as socket:\n        async for raw in socket:\n            event = json.loads(raw)\n            if event.get("type") == "heartbeat":\n                continue\n            if event.get("type") == "signal":\n                signal = event["signal"]\n                print(signal["action"], signal["matched_rules"], signal["target_round_id"])\n\nasyncio.run(main())`,
  nodeFetch: `const baseUrl = process.env.RULES_API_URL.replace(/\\/$/, "");\nconst token = process.env.RULES_PROJECT_TOKEN;\nconst response = await fetch(\`${'${'}baseUrl}/api/v1/signals/latest\`, {\n  headers: { Authorization: \`Bearer ${'${'}token}\` },\n  signal: AbortSignal.timeout(5000),\n});\nif (!response.ok) throw new Error(\`HTTP ${'${'}response.status}\`);\nconst { signal } = await response.json();\nif (!signal) console.log("NO_SIGNALS_YET");\nelse console.log(signal.action, signal.matched_rules, signal.target_round_id);`,
  nodeWs: `// npm install ws\nimport WebSocket from "ws";\nconst socket = new WebSocket(process.env.RULES_WS_URL, {\n  headers: { Authorization: \`Bearer ${'${'}process.env.RULES_PROJECT_TOKEN}\` },\n});\nsocket.on("message", (raw) => {\n  const event = JSON.parse(raw.toString());\n  if (event.type === "signal") {\n    const signal = event.signal;\n    console.log(signal.action, signal.matched_rules, signal.target_round_id);\n  }\n});`,
  curl: `curl --fail-with-body --max-time 5 \\\n  -H "Authorization: Bearer $RULES_PROJECT_TOKEN" \\\n  "$RULES_API_URL/api/v1/signals/latest"`,
};

function apiBaseDisplay() {
  return getApiBaseUrl() || (typeof window !== 'undefined' ? window.location.origin : 'same origin');
}

function websocketAddress() {
  const base = getApiBaseUrl() || window.location.origin;
  const url = new URL('/api/v1/ws/signals', base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function requestWebSocketTicket(credential) {
  const response = await fetch(`${getApiBaseUrl()}/api/v1/ws/ticket`, {
    method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: { Authorization: `Bearer ${credential}`, 'Content-Type': 'application/json' },
    body: '{}', signal: AbortSignal.timeout(5000),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.ticket) {
    const error = new Error(body?.detail?.error || body?.error || `HTTP ${response.status}`);
    error.httpStatus = response.status;
    error.response = body;
    throw error;
  }
  return { ticket: body.ticket, httpStatus: response.status };
}

function recoveryAction(status, error, testId) {
  const code = String(error || '');
  if (status === 401 || /invalid_or_revoked_project_token|project_token_required/.test(code)) {
    return 'Open API Keys, generate a consumer key with signals:read, then use that key here. A newly generated consumer key is selected automatically.';
  }
  if (status === 403 || /scope_required|ORIGIN_NOT_ALLOWED/.test(code)) {
    return 'Use a consumer key with signals:read and allow this dashboard origin in the API configuration.';
  }
  if (testId === 'publisher') {
    return 'Generate an internal publisher key with signals:write, configure it as RULES_SIGNAL_PUBLISHER_TOKEN for the Node Rules Engine service, and restart that service.';
  }
  if (testId === 'websocket') {
    return 'Check the configured API base URL, HTTPS/WebSocket proxy upgrade, dashboard origin allowlist, and consumer key scope; then retry.';
  }
  if (status === 503) return 'Check FastAPI readiness and PostgreSQL connectivity, then retry.';
  if (status === 429) return 'Wait for the Retry-After interval before repeating this test.';
  return 'Check the API response details and service logs, then retry.';
}

function timeText(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function Badge({ status }) {
  const color = ['ONLINE', 'ACTIVE', 'AUTHENTICATED', 'CONNECTED', 'AVAILABLE', 'READY', 'PASS', 'PUBLISHED', 'ALREADY_PUBLISHED', 'IDLE', 'SETTLED', 'COMMITTED', 'ACCEPTED', 'VALIDATED', 'OK'].includes(status)
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
    : ['DEGRADED', 'RUNNING', 'QUEUED', 'STALE', 'PENDING', 'CONNECTING', 'RECONNECTING', 'TRANSITIONING', 'PROCESSING', 'RETRYING', 'NOT_CONFIGURED', 'INACTIVE', 'WARNING', 'EXPIRING', 'RECOVERING', 'WAIT', 'UNVERIFIED', 'NOT_PERSISTED', 'NOT_ACCEPTED'].includes(status)
      ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
      : ['FAIL', 'ERROR', 'OFFLINE', 'UNAVAILABLE', 'BLOCKED', 'REJECTED_INVALID_DECISION', 'STOPPED', 'DISCONNECTED', 'REVOKED', 'EXPIRED', 'AUTHENTICATION_FAILED'].includes(status)
        ? 'border-rose-500/30 bg-rose-500/10 text-rose-200'
        : 'border-zinc-700 bg-zinc-900 text-zinc-300';
  return <span className={`inline-flex rounded-full border px-2 py-1 text-[11px] font-semibold tracking-wide ${color}`}>{status || 'UNKNOWN'}</span>;
}

function StatusTile({ title, status, detail, icon: Icon }) {
  return <div className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4">
    <div className="flex items-center justify-between gap-3"><span className="text-sm text-zinc-400">{title}</span><Icon size={17} className="text-zinc-500" /></div>
    <div className="mt-3"><Badge status={status} /></div>
    {detail && <div className="mt-2 text-xs text-zinc-500">{detail}</div>}
  </div>;
}

function CopyButton({ value, children = 'Copy' }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await copyTextToClipboard(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }
  return <button type="button" onClick={copy} className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800">
    {copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? 'Copied' : children}
  </button>;
}

function CodeBlock({ children }) {
  return <pre className="max-h-[32rem] overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-4 text-xs leading-6 text-emerald-100"><code>{children}</code></pre>;
}

function SignalTable({ signals }) {
  if (!signals?.length) return <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center text-sm text-zinc-500">No authoritative BET signals have been published yet.</div>;
  return <div className="overflow-x-auto rounded-xl border border-zinc-800">
    <table className="w-full min-w-[1050px] text-left text-sm">
      <thead className="bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500"><tr>
        {['Signal ID', 'Round', 'Target Round', 'Action', 'Rule', 'Description', 'Selected Rules', 'Matched Rules', 'Timestamp', 'Settlement'].map((label) => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}
      </tr></thead>
      <tbody className="divide-y divide-zinc-800 bg-zinc-950/40">{signals.map((signal) => <tr key={signal.signal_id}>
        <td className="px-3 py-3 font-mono text-xs text-zinc-400">{signal.signal_id}</td>
        <td className="px-3 py-3 font-mono text-xs">{signal.round_id}</td>
        <td className="px-3 py-3 font-mono text-xs">{signal.target_round_id}
          {signal.target_identity_status === 'INDEX_ONLY_SHADOW' && <div className="mt-1 font-sans text-[10px] font-medium text-amber-300">INDEX ONLY · SHADOW · NO PROVIDER ID</div>}
          {signal.resolved_target_round_id && <div className="mt-1 font-sans text-[10px] text-zinc-500">resolved round {signal.resolved_target_round_id}</div>}
        </td>
        <td className="px-3 py-3"><Badge status={signal.action === 'BET' ? 'PUBLISHED' : 'INVALID'} /></td>
        <td className="px-3 py-3">{signal.rule || '—'}</td>
        <td className="max-w-xs px-3 py-3 text-zinc-400">{signal.description}</td>
        <td className="px-3 py-3 text-xs">{(signal.selected_rules || []).join(', ') || '—'}</td>
        <td className="px-3 py-3 text-xs text-emerald-200">{(signal.matched_rules || []).join(', ') || '—'}</td>
        <td className="px-3 py-3 text-xs text-zinc-400">{timeText(signal.timestamp)}</td>
        <td className="px-3 py-3"><Badge status={signal.settlement_status || 'PENDING'} /></td>
      </tr>)}</tbody>
    </table>
  </div>;
}

function OverviewTab({ overview, loading, error, onRefresh, socketStatus, signals, hasSignalCredential }) {
  const backendConnection = useBackendConnection();
  const backendConnectionStatus = backendConnection.status === 'online'
    ? 'CONNECTED'
    : backendConnection.status === 'degraded' ? 'DEGRADED'
      : backendConnection.state === 'INITIAL_CONNECT' ? 'CONNECTING' : 'RECONNECTING';
  const backendConnectionDetail = [
    backendConnection.message || (backendConnection.online ? 'Dashboard can reach the Winner Predict backend.' : 'Dashboard is trying to reach the Winner Predict backend.'),
    backendConnection.nextRetryMs > 0 && !backendConnection.online ? `Retry in ${Math.ceil(backendConnection.nextRetryMs / 1000)}s` : null,
    backendConnection.lastSuccessfulAt ? `Last connected ${new Date(backendConnection.lastSuccessfulAt).toLocaleTimeString()}` : null,
  ].filter(Boolean).join(' · ');
  const rulesState = overview?.rules_engine?.status;
  const rulesDetail = overview?.rules_engine?.session_id
    ? `Session ${overview.rules_engine.session_id}`
    : rulesState === 'NOT_CONFIGURED' ? 'No rules configured'
      : rulesState === 'READY' ? `Ready · ${overview.rules_engine.enabled_rules ?? 0} enabled rules · no active session`
        : rulesState === 'UNKNOWN' || rulesState === 'ERROR' || rulesState === 'UNAVAILABLE'
          ? 'Engine status could not be confirmed' : 'No active session';
  const bridge = overview?.shadow_signal_bridge || {};
  const actionDiagnostics = overview?.signal_action || {};
  const signalPersistenceStatus = actionDiagnostics.signal_persisted === true
    ? 'COMMITTED'
    : actionDiagnostics.signal_persisted !== false ? 'UNAVAILABLE'
      : actionDiagnostics.decision_found === false ? 'NO_SIGNAL' : 'UNAVAILABLE';
  const signalPersistenceDetail = signalPersistenceStatus === 'COMMITTED'
    ? 'Latest canonical published signal is present in PostgreSQL.'
    : signalPersistenceStatus === 'NO_SIGNAL'
      ? 'No eligible canonical signal has been published.'
      : 'Persistence status could not be confirmed.';
  const signalAcceptanceStatus = actionDiagnostics.signal_accepted === true
    ? 'ACCEPTED'
    : actionDiagnostics.signal_accepted !== false ? 'UNAVAILABLE'
      : actionDiagnostics.decision_found === false ? 'NO_SIGNAL' : 'NOT_ACCEPTED';
  const signalAcceptanceDetail = signalAcceptanceStatus === 'ACCEPTED'
    ? 'Canonical signal distribution accepted this signal.'
    : signalAcceptanceStatus === 'NO_SIGNAL'
      ? 'There is no published signal to accept.'
      : signalAcceptanceStatus === 'NOT_ACCEPTED'
        ? 'The canonical signal distribution service has not accepted this signal.'
        : 'Acceptance status could not be confirmed.';
  const indexOnlyTargetDetail = bridge.target_identity_status === 'INDEX_ONLY_SHADOW'
    ? ` · simulation target #${bridge.last_target_round_index ?? '?'} (index only; provider ID unavailable)` : '';
  const actionReasonDetail = actionDiagnostics.wait_reason
    ? ` · action ${actionDiagnostics.action || 'WAIT'}: ${actionDiagnostics.wait_reason}` : '';
  const bridgeDetail = bridge.last_error
    ? `Last error: ${bridge.last_error}`
    : bridge.last_published_at
      ? `Persisted/accepted ${bridge.published_count ?? 0} · settled ${bridge.settled_count ?? 0} · last ${timeText(bridge.last_published_at)}${indexOnlyTargetDetail}${actionReasonDetail}`
      : `Waiting for a committed SHADOW decision · ${bridge.published_count ?? 0} published`;
  const watchdog = overview?.session_watchdog || {};
  const watchdogDetail = watchdog.status
    ? [
      watchdog.monitor_tab_status ? `monitor tab ${watchdog.monitor_tab_status.toLowerCase()}` : null,
      watchdog.last_successful_check_at ? `checked ${timeText(watchdog.last_successful_check_at)}` : null,
      watchdog.estimated_remaining_seconds != null && Number.isFinite(Number(watchdog.estimated_remaining_seconds))
        ? `estimated ${Math.max(0, Number(watchdog.estimated_remaining_seconds))}s remaining`
        : 'remaining time unavailable',
      watchdog.last_successful_activity_at ? `activity ${timeText(watchdog.last_successful_activity_at)}` : 'activity time unavailable',
      watchdog.next_scheduled_maintenance_at ? `maintenance ${timeText(watchdog.next_scheduled_maintenance_at)}` : 'maintenance not scheduled',
      watchdog.last_verified_no_click_at ? `last verified NO click ${timeText(watchdog.last_verified_no_click_at)}` : 'no verified NO click',
      watchdog.logout_countdown_seconds != null && Number.isFinite(Number(watchdog.logout_countdown_seconds)) ? `logout countdown ${watchdog.logout_countdown_seconds}s` : null,
      `collector ${watchdog.collector_connected === true ? 'connected' : watchdog.collector_connected === false ? 'disconnected' : 'unknown'}`,
      watchdog.recovery_attempts != null && Number.isFinite(Number(watchdog.recovery_attempts)) ? `recovery attempts ${watchdog.recovery_attempts}` : null,
      watchdog.reason_code || null,
    ].filter(Boolean).join(' · ')
    : 'No watchdog status reported by the collector';
  const websocketCount = overview?.websocket?.connected_consumers ?? 0;
  const dashboardFeed = hasSignalCredential ? socketStatus : 'not authenticated';
  return <div className="space-y-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-xl font-semibold">API status</h2><p className="mt-1 text-sm text-zinc-500">Live values from FastAPI health, the built-in SHADOW bridge, publisher heartbeat, database, and signal hub.</p></div>
      <button type="button" onClick={onRefresh} disabled={loading} className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800 disabled:opacity-50"><RefreshCw size={15} className={loading ? 'animate-spin' : ''} />Refresh</button>
    </div>
    {error && <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">{error}</div>}
    {!overview && loading && <div className="rounded-xl border border-zinc-800 p-8 text-sm text-zinc-400">Loading current backend status…</div>}
    <LiveSignalActionMonitor diagnostics={actionDiagnostics} />
    {overview && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <StatusTile title="Dashboard to Backend Connection" status={backendConnectionStatus} detail={backendConnectionDetail} icon={backendConnection.online ? Wifi : WifiOff} />
        <StatusTile title="Python API Server" status={overview.api_server?.status} detail={`Checked ${timeText(overview.checked_at)}`} icon={Activity} />
        <StatusTile title="Rules Engine" status={rulesState} detail={rulesDetail} icon={Code2} />
        <StatusTile title="SHADOW Mode" status={overview.shadow_mode?.status} detail={overview.shadow_mode?.detail || overview.shadow_mode?.mode || 'Mode unavailable'} icon={ShieldCheck} />
        <StatusTile title="Winner Session Watchdog" status={watchdog.status || 'UNKNOWN'} detail={watchdogDetail} icon={Activity} />
        <StatusTile title="Built-in SHADOW Bridge" status={bridge.status || 'UNKNOWN'} detail={bridgeDetail} icon={Signal} />
        <StatusTile title="External Node.js Publisher" status={overview.node_publisher?.status} detail={overview.node_publisher?.last_heartbeat_at ? `${overview.node_publisher.publisher_id || 'Publisher'} · SHADOW ${overview.node_publisher.shadow_status || 'UNKNOWN'} · auth ${overview.node_publisher.authentication_status || 'UNKNOWN'} · ${timeText(overview.node_publisher.last_heartbeat_at)}` : overview.node_publisher?.status === 'AUTHENTICATION_FAILED' ? `Publisher authentication failed · ${timeText(overview.node_publisher.last_auth_failure_at)}` : 'No authenticated publisher heartbeat received. Check the external service URL, signals:write key, and process state.'} icon={Radio} />
        <StatusTile title="Signal Persistence" status={signalPersistenceStatus} detail={signalPersistenceDetail} icon={Database} />
        <StatusTile title="Signal Acceptance" status={signalAcceptanceStatus} detail={signalAcceptanceDetail} icon={CheckCircle2} />
        <StatusTile title="Signal Validation" status={actionDiagnostics.validation_passed === true ? 'VALIDATED' : actionDiagnostics.validation_passed === false ? 'NO_ACTION' : 'UNAVAILABLE'} detail={actionDiagnostics.validation_passed === true ? 'The signal passed canonical publishing validation before distribution.' : actionDiagnostics.reason === 'NO_SIGNAL' ? 'No published signal is available to validate.' : actionDiagnostics.reason || 'Validation status unavailable.'} icon={ShieldCheck} />
        <StatusTile title="Action API" status={actionDiagnostics.action || (actionDiagnostics.diagnostics_available === true ? 'NO_SIGNAL' : 'UNAVAILABLE')} detail={`Target ${actionDiagnostics.index || 'index unavailable'}${actionDiagnostics.wait_reason ? ` · ${actionDiagnostics.wait_reason}` : ''}`} icon={Signal} />
        <StatusTile title="PostgreSQL" status={overview.postgresql?.status} detail="Readiness check uses SELECT 1" icon={Database} />
        <StatusTile title="WebSocket Hub" status={overview.websocket?.status || 'UNKNOWN'} detail={`${websocketCount} connected sockets on the hub · this dashboard feed ${dashboardFeed}`} icon={['CONNECTED', 'AVAILABLE'].includes(overview.websocket?.status) ? Wifi : WifiOff} />
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Total BET signals" value={overview.total_bet_signals ?? 'Unavailable'} detail={overview.last_published_at ? `All sessions · persisted records · latest ${timeText(overview.last_published_at)}` : 'All sessions · persisted signal records'} />
        <MetricCard label="Latest session signals" value={overview.reporting_session_signal_coverage ? `${overview.reporting_session_signal_coverage.accepted_signal_count} / ${overview.reporting_session_signal_coverage.selection_count}` : 'Unavailable'} detail={overview.reporting_session_signal_coverage ? `${overview.reporting_session_signal_coverage.session_status} session · ${overview.reporting_session_signal_coverage.settled_outcomes_without_signal} settled outcomes without an accepted signal · ${overview.reporting_session_signal_coverage.pending_selections} pending` : 'No session signal breakdown'} />
        <MetricCard label="Connected external consumers" value={overview.connected_external_consumers ?? 'Unavailable'} detail={overview.consumer_connections ? `${overview.consumer_connections.websocket || 0} WebSocket · ${overview.consumer_connections.sse || 0} SSE` : 'Metrics unavailable'} />
        <MetricCard label="Failed deliveries" value={overview.failed_deliveries ?? 'Unavailable'} detail="Queue overflows and exhausted persistence retries" />
        <MetricCard label="Average delivery latency" value={overview.average_delivery_latency_ms == null ? 'No samples' : `${overview.average_delivery_latency_ms} ms`} detail="Measured publisher-to-fanout-queue enqueue time" />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4"><h3 className="font-medium">Last received signal</h3>
          {overview.has_received_signal ? <p className="mt-2 text-sm text-emerald-200">Received {timeText(overview.last_received_at)} · signal details require signals:read access.</p> : <p className="mt-2 text-sm text-zinc-500">No publisher signal has been received.</p>}</div>
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4"><h3 className="font-medium">Latest persisted BET signal</h3>
          {overview.has_last_published_signal ? <p className="mt-2 text-sm text-emerald-200">Stored {timeText(overview.last_published_at)} · signal details require signals:read access.</p> : <p className="mt-2 text-sm text-zinc-500">No stored BET signals yet.</p>}</div>
      </div>
      <section className="space-y-3"><div><h3 className="text-lg font-semibold">Live BET signal monitor</h3><p className="mt-1 text-sm text-zinc-500">Only accepted authoritative BET signals are shown. Settlement stays pending until the matching target round is persisted and committed.</p></div>{hasSignalCredential ? <SignalTable signals={signals} /> : <div className="rounded-lg border border-zinc-800 p-4 text-sm text-zinc-400">Enter a consumer key with signals:read scope in API Testing to view the protected signal feed.</div>}</section>
    </>}
  </div>;
}

function MetricCard({ label, value, detail }) {
  return <div className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4"><div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div><div className="mt-2 text-2xl font-semibold">{value}</div><div className="mt-2 text-xs text-zinc-500">{detail}</div></div>;
}

function TestPanel({ consumerCredential, onCredentialChange, selectedCredentialLabel, onOpenKeys, canManageKeys }) {
  const [results, setResults] = useState({});
  const [runningAll, setRunningAll] = useState(false);

  const save = useCallback((id, value) => setResults((old) => {
    const timestamp = value.timestamp || new Date().toISOString();
    return { ...old, [id]: { ...value, timestamp,
      lastSuccessfulAt: value.status === 'PASS' ? timestamp : old[id]?.lastSuccessfulAt || null } };
  }), []);
  const httpTest = useCallback(async (id, name, path, { credential = '', pass = (body, response) => response.ok,
    noSignalsPass = false, failureMessage = null, headers = {} } = {}) => {
    const started = performance.now();
    save(id, { name, status: 'RUNNING', timestamp: new Date().toISOString() });
    try {
      const response = await fetch(`${getApiBaseUrl()}${path}`, {
        method: 'GET', cache: 'no-store', signal: AbortSignal.timeout(8000),
        headers: { ...(credential ? { Authorization: `Bearer ${credential}` } : {}), ...headers },
      });
      const body = await response.json().catch(async () => ({ response_text: await response.text().catch(() => '') }));
      const elapsed = Math.round(performance.now() - started);
      const noSignals = noSignalsPass && body?.signal == null && response.ok;
      const success = Boolean(pass(body, response));
      const reason = success ? '' : (typeof failureMessage === 'function' ? failureMessage(body) : failureMessage)
        || body?.detail?.error || body?.error || `HTTP ${response.status}`;
      save(id, { name, status: success ? 'PASS' : 'FAIL', httpStatus: response.status,
        responseTime: elapsed, response: body, error: reason,
        recoveryAction: success ? '' : recoveryAction(response.status, reason, id),
        label: noSignals ? 'NO_SIGNALS_YET' : '', timestamp: new Date().toISOString() });
      return success;
    } catch (error) {
      save(id, { name, status: 'FAIL', httpStatus: null, responseTime: Math.round(performance.now() - started),
        response: null, error: error?.message || 'Request failed',
        recoveryAction: recoveryAction(null, error?.message, id), timestamp: new Date().toISOString() });
      return false;
    }
  }, [save]);

  const tests = useMemo(() => [
    ['connection', 'Test API Connection', () => httpTest('connection', 'Test API Connection', '/health/ready', { pass: (body, response) => response.ok && body?.ok === true })],
    ['authentication', 'Test Authentication', () => {
      if (consumerCredential) return httpTest('authentication', 'Test Authentication', '/api/v1/signals/latest', { credential: consumerCredential });
      save('authentication', { name: 'Test Authentication', status: 'FAIL', httpStatus: null, responseTime: null, response: null,
        error: 'Select an authorized consumer credential with signals:read scope.',
        recoveryAction: 'Open API Keys and generate a read-only consumer key with signals:read. It will be selected automatically for both authentication and signal retrieval.',
        timestamp: new Date().toISOString() });
      return Promise.resolve(false);
    }],
    ['database', 'Test Database', () => httpTest('database', 'Test Database', '/api/v1/diagnostics/database', { pass: (body, response) => response.ok && body?.database === 'CONNECTED' })],
    ['websocket', 'Test WebSocket', () => testWebSocket(save)],
    ['publisher', 'Test Rules Engine Publisher', async () => {
      const reachable = await httpTest('publisher', 'Test Rules Engine Publisher', '/api/v1/diagnostics/publisher', {
        pass: (body, response) => response.ok && body?.publisher?.status === 'CONNECTED',
        failureMessage: (body) => body?.publisher?.status === 'DISCONNECTED'
          ? 'Node.js publisher is disconnected; no authenticated heartbeat has been received.'
          : body?.publisher?.status === 'STALE' ? 'Node.js publisher heartbeat is stale.'
            : body?.publisher?.status === 'AUTHENTICATION_FAILED' ? 'Node.js publisher heartbeat authentication failed.' : null,
      });
      return reachable;
    }],
    ['signal', 'Test Signal Retrieval', () => consumerCredential
      ? httpTest('signal', 'Test Signal Retrieval', '/api/v1/signals/latest', { credential: consumerCredential, noSignalsPass: true })
      : (save('signal', { name: 'Test Signal Retrieval', status: 'FAIL', httpStatus: null, responseTime: null, response: null,
        error: 'Select an authorized consumer credential with signals:read scope.',
        recoveryAction: 'Open API Keys and generate a read-only consumer key with signals:read. It will be selected automatically for both authentication and signal retrieval.',
        timestamp: new Date().toISOString() }), Promise.resolve(false))],
  ], [consumerCredential, httpTest, save]);

  async function testWebSocket(saveResult) {
    const started = performance.now();
    const name = 'Test WebSocket';
    saveResult('websocket', { name, status: 'RUNNING', timestamp: new Date().toISOString() });
    const token = consumerCredential;
    if (!token) {
      saveResult('websocket', { name, status: 'FAIL', httpStatus: null, responseTime: null,
        response: null, error: 'Select a consumer key with signals:read scope to test authenticated WebSocket access.',
        recoveryAction: recoveryAction(401, '', 'websocket'), timestamp: new Date().toISOString() });
      return false;
    }
    let ticket;
    let ticketHttpStatus = null;
    try {
      const issued = await requestWebSocketTicket(token);
      ticket = issued.ticket;
      ticketHttpStatus = issued.httpStatus;
    } catch (error) {
      const status = error?.httpStatus ?? null;
      const reason = error?.message || 'Could not request a WebSocket ticket';
      saveResult('websocket', { name, status: 'FAIL', httpStatus: status,
        responseTime: Math.round(performance.now() - started), response: error?.response || null,
        error: reason, recoveryAction: recoveryAction(status, reason, 'websocket'),
        timestamp: new Date().toISOString() });
      return false;
    }
    return new Promise((resolve) => {
      let sawReady = false;
      let sawHeartbeat = false;
      let opened = false;
      let complete = false;
      let socketError = '';
      let timeout;
      let socket;
      const finish = (ok, response, error = '', closeCode = null) => {
        if (complete) return;
        complete = true;
        window.clearTimeout(timeout);
        try { socket?.close(); } catch { /* already closed */ }
        const reason = error || '';
        saveResult('websocket', { name, status: ok ? 'PASS' : 'FAIL', httpStatus: opened ? 101 : ticketHttpStatus,
          closeCode, responseTime: Math.round(performance.now() - started), response, error: reason,
          recoveryAction: ok ? '' : recoveryAction(closeCode, reason, 'websocket'),
          timestamp: new Date().toISOString(),
          label: ok ? 'Authenticated WebSocket opened; ready and heartbeat verified. No signal is assumed.' : '' });
        resolve(ok);
      };
      try {
        socket = new WebSocket(websocketAddress());
        socket.onopen = () => {
          opened = true;
          socket.send(JSON.stringify({ type: 'authenticate', ticket }));
        };
        timeout = window.setTimeout(() => finish(false,
          { ready: sawReady, heartbeat: sawHeartbeat, handshake: opened ? 'OPEN' : 'PENDING' },
          'WebSocket authentication or heartbeat timed out.'), 8000);
        socket.onmessage = (event) => {
          let payload;
          try { payload = JSON.parse(event.data); } catch { return; }
          if (payload.type === 'ready') sawReady = true;
          if (payload.type === 'heartbeat') sawHeartbeat = true;
          if (sawReady && sawHeartbeat) finish(true, { ready: true, heartbeat: payload });
        };
        socket.onerror = () => { socketError = 'Browser reported a WebSocket network or proxy error.'; };
        socket.onclose = (event) => { if (!complete) finish(false,
          { ready: sawReady, heartbeat: sawHeartbeat, closeCode: event.code, closeReason: event.reason },
          event.reason || socketError || 'WebSocket closed before authenticated heartbeat.', event.code); };
      } catch (error) {
        finish(false, null, error?.message || 'WebSocket connection failed.');
      }
    });
  }

  async function runAll() {
    setRunningAll(true);
    try { for (const [, , run] of tests) await run(); }
    finally { setRunningAll(false); }
  }

  return <div className="space-y-5">
    <div><h2 className="text-xl font-semibold">API Testing</h2><p className="mt-1 text-sm text-zinc-500">Run real health, auth, database, WebSocket, publisher, and signal retrieval checks. Tests never create a signal.</p></div>
    <label className="block max-w-2xl text-sm text-zinc-300">Authorized consumer credential
      <input type="password" autoComplete="off" value={consumerCredential} onChange={(event) => onCredentialChange(event.target.value)} placeholder="Paste a consumer key with signals:read scope" className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" />
      <span className="mt-1 block text-xs text-zinc-500">{selectedCredentialLabel ? `Selected: ${selectedCredentialLabel}. ` : ''}Kept in page memory only. Sent in the Authorization header, never in a URL or browser storage.</span>
    </label>
    <div className="flex flex-wrap gap-2">
      {canManageKeys && <button type="button" onClick={onOpenKeys} className="rounded-lg border border-emerald-500/40 px-3 py-2 text-sm text-emerald-200 hover:bg-emerald-500/10">Generate replacement consumer key</button>}
      {tests.map(([id, name, run]) => <button key={id} type="button" onClick={run} disabled={runningAll} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800 disabled:opacity-50">{name}</button>)}
      <button type="button" onClick={runAll} disabled={runningAll} className="inline-flex items-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 text-sm font-semibold text-zinc-950 disabled:opacity-50">{runningAll && <LoaderCircle size={15} className="animate-spin" />}Run All Tests</button>
    </div>
    <div className="space-y-3">{tests.map(([id, name]) => {
      const result = results[id];
      return <article key={id} className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3"><div className="font-medium">{name}</div><div className="flex items-center gap-3"><span className="text-xs text-zinc-500">{id === 'websocket' ? `HTTP ${result?.httpStatus ?? '—'}${result?.closeCode != null ? ` · close ${result.closeCode}` : ''}` : `HTTP ${result?.httpStatus ?? '—'}`}{result?.responseTime != null ? ` · ${result.responseTime} ms` : ''}</span><Badge status={result?.status || 'NOT RUN'} /></div></div>
        {result?.label && <p className="mt-2 text-sm text-emerald-200">{result.label}</p>}
        {result?.error && <p className="mt-2 text-sm text-rose-200">{result.error}</p>}
        {result?.recoveryAction && <p className="mt-2 text-sm text-amber-200">Suggested recovery: {result.recoveryAction}</p>}
        {result?.lastSuccessfulAt && <p className="mt-2 text-xs text-zinc-500">Last successful check: {timeText(result.lastSuccessfulAt)}</p>}
        {result?.response && <details className="mt-3"><summary className="cursor-pointer text-xs text-zinc-400">Response JSON</summary><CodeBlock>{JSON.stringify(result.response, null, 2)}</CodeBlock></details>}
        {result && <div className="mt-2 text-xs text-zinc-600">Timestamp: {timeText(result.timestamp)}</div>}
      </article>;
    })}</div>
  </div>;
}

function KeysTab({ onSecret }) {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [projectId, setProjectId] = useState('external-project');
  const [keyType, setKeyType] = useState('consumer');
  const [scopes, setScopes] = useState(READ_SCOPES);
  const [expires, setExpires] = useState('');
  const [busy, setBusy] = useState(false);

  const loadKeys = useCallback(async () => {
    setLoading(true);
    setError('');
    try { setKeys((await api.get('/api/v1/keys')).data.keys || []); }
    catch (reason) { setError(reason?.response?.data?.detail?.error || reason.message || 'Could not load keys'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { loadKeys(); }, [loadKeys]);

  function setScope(scope, enabled) {
    setScopes((current) => enabled ? [...new Set([...current, scope])] : current.filter((item) => item !== scope));
  }

  async function generate(event) {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      const body = { name, project_id: projectId, key_type: keyType,
        scopes: keyType === 'publisher' ? ['signals:write'] : scopes,
        ...(expires ? { expires_at: new Date(expires).toISOString() } : {}) };
      const result = (await api.post('/api/v1/keys', body)).data;
      onSecret(result.token, result);
      setName('');
      await loadKeys();
    } catch (reason) { setError(reason?.response?.data?.detail?.error || reason.message || 'Could not generate key'); }
    finally { setBusy(false); }
  }

  async function revoke(key) {
    if (!window.confirm(`Revoke “${key.label}” (${key.token_prefix}) now? This immediately disables the key for all connected consumers.`)) return;
    try { await api.delete(`/api/v1/keys/${encodeURIComponent(key.token_id)}`); await loadKeys(); }
    catch (reason) { setError(reason?.response?.data?.detail?.error || reason.message || 'Could not revoke key'); }
  }

  async function rotate(key) {
    if (!window.confirm(`Rotate “${key.label}” now? The existing key is revoked as the new secret is created. Copy the new secret immediately.`)) return;
    setBusy(true); setError('');
    try { const result = (await api.post(`/api/v1/keys/${encodeURIComponent(key.token_id)}/rotate`, {})).data; onSecret(result.token, result); await loadKeys(); }
    catch (reason) { setError(reason?.response?.data?.detail?.error || reason.message || 'Could not rotate key'); }
    finally { setBusy(false); }
  }

  return <div className="space-y-5">
    <div><h2 className="text-xl font-semibold">API Keys</h2><p className="mt-1 text-sm text-zinc-500">Only this authenticated owner view can create, rotate, or revoke credentials. The database stores a SHA-256 hash; a secret is returned once.</p></div>
    {error && <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">{error}</div>}
    <form onSubmit={generate} className="grid gap-4 rounded-xl border border-zinc-800 bg-zinc-900/50 p-4 lg:grid-cols-2">
      <label className="text-sm text-zinc-300">Key name<input required maxLength={100} value={name} onChange={(event) => setName(event.target.value)} placeholder="Production analytics consumer" className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /></label>
      <label className="text-sm text-zinc-300">Credential type<select value={keyType} onChange={(event) => { setKeyType(event.target.value); setScopes(event.target.value === 'publisher' ? ['signals:write'] : READ_SCOPES); }} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2"><option value="consumer">External consumer · read only</option><option value="publisher">Internal publisher · signals:write</option></select></label>
      {keyType === 'consumer' && <label className="text-sm text-zinc-300">Project ID<input maxLength={128} value={projectId} onChange={(event) => setProjectId(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /></label>}
      <label className="text-sm text-zinc-300">Expires at (optional)<input type="datetime-local" value={expires} onChange={(event) => setExpires(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /><span className="mt-1 block text-xs text-zinc-500">Use a short expiry for integrations that can rotate credentials.</span></label>
      <fieldset className="rounded-lg border border-zinc-800 p-3 lg:col-span-2"><legend className="px-1 text-sm text-zinc-300">Allowed scopes</legend>
        {keyType === 'consumer' ? <div className="flex flex-wrap gap-x-5 gap-y-2">{READ_SCOPES.map((scope) => <label key={scope} className="flex items-center gap-2 text-sm text-zinc-400"><input type="checkbox" checked={scopes.includes(scope)} onChange={(event) => setScope(scope, event.target.checked)} />{scope}</label>)}</div> : <div className="text-sm text-amber-200">signals:write · internal publisher only. This key cannot read signals and must not be given to an external consumer.</div>}
      </fieldset>
      <button type="submit" disabled={busy || !name.trim()} className="inline-flex items-center justify-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-zinc-950 disabled:opacity-50">{busy && <LoaderCircle size={15} className="animate-spin" />}<KeyRound size={15} />Generate API key</button>
    </form>
    <div className="flex items-center justify-between gap-3"><h3 className="text-lg font-semibold">Managed keys</h3><button type="button" onClick={loadKeys} disabled={loading} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm">{loading ? 'Loading…' : 'Refresh keys'}</button></div>
    <div className="overflow-x-auto rounded-xl border border-zinc-800"><table className="w-full min-w-[900px] text-left text-sm">
      <thead className="bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500"><tr>{['Name / key ID', 'Type', 'Scopes', 'Created', 'Last used', 'Expires', 'Status', 'Actions'].map((text) => <th key={text} className="px-3 py-3 font-medium">{text}</th>)}</tr></thead>
      <tbody className="divide-y divide-zinc-800">{keys.map((key) => {
        const expired = key.expires_at && new Date(key.expires_at).getTime() <= Date.now();
        const status = key.revoked_at ? 'REVOKED' : expired ? 'EXPIRED' : 'ACTIVE';
        return <tr key={key.token_id}>
          <td className="px-3 py-3"><div>{key.label || 'Unnamed key'}</div><code className="text-xs text-zinc-500">{key.token_prefix}… · {key.token_id}</code></td>
          <td className="px-3 py-3">{key.key_type}</td><td className="px-3 py-3"><div className="flex max-w-xs flex-wrap gap-1">{(key.scopes || []).map((scope) => <span key={scope} className="rounded bg-zinc-800 px-2 py-1 text-[10px]">{scope}</span>)}</div></td>
          <td className="px-3 py-3 text-xs text-zinc-400">{timeText(key.created_at)}</td><td className="px-3 py-3 text-xs text-zinc-400">{timeText(key.last_used_at)}</td><td className="px-3 py-3 text-xs text-zinc-400">{timeText(key.expires_at)}</td><td className="px-3 py-3"><Badge status={status} /></td>
          <td className="px-3 py-3"><div className="flex gap-2">{status === 'ACTIVE' && <><button type="button" onClick={() => rotate(key)} disabled={busy} className="rounded border border-zinc-700 px-2 py-1 text-xs hover:bg-zinc-800">Rotate</button><button type="button" onClick={() => revoke(key)} className="rounded border border-rose-500/30 px-2 py-1 text-xs text-rose-200 hover:bg-rose-500/10">Revoke</button></>}</div></td>
        </tr>;
      })}{!keys.length && <tr><td colSpan={8} className="p-6 text-center text-sm text-zinc-500">{loading ? 'Loading keys…' : 'No managed keys found.'}</td></tr>}</tbody>
    </table></div>
  </div>;
}

function EndpointDetails({ endpoint }) {
  return <details className="group rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
    <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3"><span className={`rounded px-2 py-1 text-xs font-bold ${endpoint.method === 'POST' ? 'bg-amber-500/10 text-amber-200' : 'bg-emerald-500/10 text-emerald-200'}`}>{endpoint.method}</span><code className="text-sm">{endpoint.path}</code><span className="ml-auto text-xs text-zinc-500">{endpoint.permission}</span></summary>
    <div className="mt-4 grid gap-4 lg:grid-cols-2"><div><p className="text-sm text-zinc-300">{endpoint.purpose}</p><p className="mt-2 text-xs text-zinc-500"><b>Parameters:</b> {endpoint.params}</p><p className="mt-1 text-xs text-zinc-500"><b>Permission:</b> {endpoint.permission}</p><p className="mt-1 text-xs text-zinc-500"><b>Errors:</b> {endpoint.errors}</p></div><div><div className="mb-2 text-xs uppercase text-zinc-500">Request example</div><CodeBlock>{typeof endpoint.request === 'string' ? endpoint.request : JSON.stringify(endpoint.request, null, 2)}</CodeBlock><div className="mb-2 mt-3 text-xs uppercase text-zinc-500">Response example</div><CodeBlock>{JSON.stringify(endpoint.response, null, 2)}</CodeBlock></div></div>
  </details>;
}

function ApiTester({ consumerCredential }) {
  const readEndpoints = ENDPOINTS.filter((item) => item.method === 'GET');
  const [selection, setSelection] = useState(readEndpoints[2].path);
  const [paramValue, setParamValue] = useState('100');
  const [secondaryParam, setSecondaryParam] = useState('');
  const [credentialMode, setCredentialMode] = useState('consumer');
  const [response, setResponse] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const selected = readEndpoints.find((item) => item.path === selection) || readEndpoints[0];
  const needsToken = selected.permission !== 'Public';

  async function execute() {
    setBusy(true); setError(''); setResponse(null);
    const started = performance.now();
    try {
      if (needsToken && (!consumerCredential || credentialMode !== 'consumer')) throw new Error('Select an authorized consumer key. The owner credential cannot grant consumer scopes.');
      let path = selected.path;
      const params = new URLSearchParams();
      if (path.includes('{signal_id}')) {
        if (!paramValue.trim()) throw new Error('Enter signal_id.');
        path = path.replace('{signal_id}', encodeURIComponent(paramValue.trim()));
      } else if (path === '/api/v1/signals') {
        if (paramValue.trim()) params.set('limit', paramValue.trim());
        if (secondaryParam.trim()) params.set('session_id', secondaryParam.trim());
      } else if (path === '/api/v1/stream/signals' && secondaryParam.trim()) {
        params.set('after_signal_id', secondaryParam.trim());
      }
      if (params.size) path += `?${params.toString()}`;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 7000);
      const result = await fetch(`${getApiBaseUrl()}${path}`, {
        method: 'GET', cache: 'no-store', signal: controller.signal,
        headers: needsToken ? { Authorization: `Bearer ${consumerCredential}` } : {},
      });
      let data;
      if (path.startsWith('/api/v1/stream/signals')) {
        const reader = result.body?.getReader();
        if (reader) {
          const chunk = await Promise.race([reader.read(), new Promise((resolve) => window.setTimeout(() => resolve({ timeout: true }), 4500))]);
          data = chunk?.timeout ? 'Connected; no SSE event arrived during the 4.5 second sample.' : new TextDecoder().decode(chunk.value || new Uint8Array());
          await reader.cancel().catch(() => {});
        } else data = await result.text();
      } else data = await result.json().catch(async () => await result.text());
      window.clearTimeout(timeout);
      setResponse({ status: result.status, latency: Math.round(performance.now() - started), data, timestamp: new Date().toISOString() });
      if (!result.ok) setError(data?.detail?.error || `HTTP ${result.status}`);
    } catch (reason) {
      setError(reason?.message || 'Request failed');
      setResponse({ status: null, latency: Math.round(performance.now() - started), data: null, timestamp: new Date().toISOString() });
    } finally { setBusy(false); }
  }

  return <section className="space-y-4 rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04] p-4">
    <div><h3 className="text-lg font-semibold">Try API</h3><p className="mt-1 text-sm text-zinc-500">Only documented GET endpoints are available. Requests use the configured API origin and a Bearer header; arbitrary URLs and write operations are disabled.</p></div>
    <div className="grid gap-3 md:grid-cols-2">
      <label className="text-sm text-zinc-300">Endpoint<select value={selection} onChange={(event) => { setSelection(event.target.value); setResponse(null); }} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2">{readEndpoints.map((endpoint) => <option key={endpoint.path} value={endpoint.path}>{endpoint.method} {endpoint.path}</option>)}</select></label>
      <label className="text-sm text-zinc-300">Credential<select value={credentialMode} onChange={(event) => setCredentialMode(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2"><option value="consumer">Consumer key in page memory</option><option value="none">No credential (public health only)</option></select></label>
    </div>
    {(selected.path === '/api/v1/signals' || selected.path.includes('{signal_id}')) && <label className="block max-w-md text-sm text-zinc-300">{selected.path.includes('{signal_id}') ? 'signal_id' : 'limit'}<input value={paramValue} onChange={(event) => setParamValue(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /></label>}
    {selected.path === '/api/v1/signals' && <label className="block max-w-md text-sm text-zinc-300">session_id (optional)<input value={secondaryParam} onChange={(event) => setSecondaryParam(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /></label>}
    {selected.path === '/api/v1/stream/signals' && <label className="block max-w-md text-sm text-zinc-300">after_signal_id (optional)<input value={secondaryParam} onChange={(event) => setSecondaryParam(event.target.value)} className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" /></label>}
    <button type="button" onClick={execute} disabled={busy} className="inline-flex items-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-zinc-950 disabled:opacity-50">{busy && <LoaderCircle size={15} className="animate-spin" />}Execute GET request</button>
    {error && <p role="alert" className="text-sm text-rose-200">{error}</p>}
    {response && <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3"><div className="flex flex-wrap justify-between gap-2 text-sm"><span>HTTP {response.status ?? 'Network error'}</span><span>{response.latency} ms</span><span>{timeText(response.timestamp)}</span></div><div className="mt-3 flex justify-end">{response.data && <CopyButton value={typeof response.data === 'string' ? response.data : JSON.stringify(response.data, null, 2)}>Copy response JSON</CopyButton>}</div><div className="mt-2">{typeof response.data === 'string' ? <CodeBlock>{response.data}</CodeBlock> : <CodeBlock>{JSON.stringify(response.data, null, 2)}</CodeBlock>}</div></div>}
  </section>;
}

function DocumentationTab({ consumerCredential }) {
  const [example, setExample] = useState('python');
  const apiBase = apiBaseDisplay();
  const [connectStep, setConnectStep] = useState(1);
  return <div className="space-y-7">
    <section className="space-y-3"><h2 className="text-xl font-semibold">API Overview</h2><p className="max-w-4xl text-sm leading-6 text-zinc-400">Rules Signal API distributes authoritative BET signals emitted after the existing Rules Engine evaluates selected, enabled rules during an active SHADOW session. At least one TRUE rule is required. Multiple TRUE rules are carried in one signal. The API validates and persists each accepted signal to PostgreSQL before broadcasting it; it does not evaluate betting rules or place real-money bets.</p><p className="max-w-4xl text-sm leading-6 text-zinc-400">Signals are de-duplicated per session and target round. WebSocket and SSE clients should deduplicate by signal_id and reconcile through REST after reconnects. The API does not claim exactly-once delivery to each external consumer.</p>
      <div className="grid gap-3 md:grid-cols-2"><div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4"><div className="text-xs uppercase text-zinc-500">Configured API base URL</div><code className="mt-2 block break-all text-emerald-200">{apiBase}</code><p className="mt-2 text-xs text-zinc-500">This workspace's FastAPI default port is 8000. A loopback URL is reachable only from the same machine; remote projects must use the deployment's HTTPS API origin.</p></div><div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4"><div className="text-xs uppercase text-zinc-500">OpenAPI references</div><div className="mt-3 flex gap-4 text-sm"><a className="text-emerald-200 underline" href={`${apiBase}/docs`} target="_blank" rel="noreferrer">Interactive API docs · /docs</a><a className="text-emerald-200 underline" href={`${apiBase}/openapi.json`} target="_blank" rel="noreferrer">Schema · /openapi.json</a></div></div></div>
    </section>
    <section className="space-y-3"><h2 className="text-xl font-semibold">REST Endpoints</h2>{ENDPOINTS.map((endpoint) => <EndpointDetails key={`${endpoint.method}-${endpoint.path}`} endpoint={endpoint} />)}</section>
    <section className="space-y-3"><h2 className="text-xl font-semibold">Real-time endpoints</h2>{REALTIME_ENDPOINTS.map((endpoint) => <div key={endpoint.path} className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4"><div className="flex flex-wrap gap-3"><Badge status={endpoint.method} /><code>{endpoint.path}</code></div><p className="mt-3 text-sm text-zinc-400">{endpoint.purpose}</p><p className="mt-2 text-xs text-zinc-500">Native clients authenticate with Authorization: Bearer &lt;project key&gt;. Browser clients POST /api/v1/ws/ticket with that header, then send {`{"type":"authenticate","ticket":"<short-lived ticket>"}`} as the first WebSocket message. The ticket is one-use, expires after 30 seconds, and is never placed in the URL or subprotocol. Reconnect with capped exponential backoff, use REST to reconcile cursor gaps, and treat heartbeat messages as connection health only.</p></div>)}</section>
    <section className="space-y-3"><div><h2 className="text-xl font-semibold">Copyable integration examples</h2><p className="mt-1 text-sm text-zinc-500">Set RULES_API_URL and RULES_PROJECT_TOKEN in your environment. Keep credentials out of source control and logs.</p></div><div className="flex flex-wrap gap-2">{[['python', 'Python requests'], ['pythonWs', 'Python WebSocket'], ['nodeFetch', 'Node.js fetch'], ['nodeWs', 'Node.js WebSocket'], ['curl', 'cURL']].map(([key, label]) => <button key={key} type="button" onClick={() => setExample(key)} className={`rounded-lg px-3 py-2 text-sm ${example === key ? 'bg-emerald-400 text-zinc-950' : 'border border-zinc-700 text-zinc-300'}`}>{label}</button>)}</div><div className="flex justify-end"><CopyButton value={EXAMPLES[example]}>Copy example</CopyButton></div><CodeBlock>{EXAMPLES[example]}</CodeBlock></section>
    <ApiTester consumerCredential={consumerCredential} />
    <section className="space-y-3 rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04] p-5"><h2 className="text-xl font-semibold">Connect Another Project</h2><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{[
      ['1', 'Generate a read-only API key', 'Create a consumer key and grant only the scopes the project needs. Never share a publisher key.'],
      ['2', 'Copy the configured API base URL', `Use ${apiBase}. Do not use a loopback URL from another machine.`],
      ['3', 'Connect with REST, SSE, or WebSocket', 'Send the key in the Authorization header. Browser WebSockets first request a 30-second one-use ticket and send it in the first WebSocket message; never put credentials in URLs or subprotocols.'],
      ['4', 'Subscribe to BET signals', 'Start with GET /api/v1/signals/latest, then connect to SSE or WebSocket for new events.'],
      ['5', 'Read the signal fields', 'Use action, rule, description, selected_rules, matched_rules, and round_id. Check target_identity_status before using target_round_id: INDEX_ONLY_SHADOW includes a synthetic key plus target_stream_id and target_round_index, and is simulation only.'],
      ['6', 'Handle reconnects and duplicates', 'Deduplicate by signal_id; discard stale targets; reconnect with backoff; reconcile from REST after a connection gap.'],
    ].map(([number, title, detail]) => <article key={number} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4"><div className="text-xs font-bold text-emerald-300">STEP {number}</div><h3 className="mt-2 font-medium">{title}</h3><p className="mt-2 text-sm text-zinc-500">{detail}</p></article>)}</div><p className="text-sm text-zinc-400">External projects receive read-only access. They cannot change Rules Engine configuration or authorize platform actions.</p></section>
    <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4"><h2 className="text-lg font-semibold">Node.js publisher heartbeat</h2><p className="max-w-4xl text-sm leading-6 text-zinc-400">For the existing Node Rules Engine service, create a separate internal publisher key in API Keys and grant only signals:write. Configure the Node service with the API origin and that key, then restart it. The publisher reports its observed SHADOW state every 15 seconds; FastAPI marks it stale after 45 seconds. A heartbeat is a connection check and does not create or imply a BET signal.</p><CodeBlock>{'RULES_SIGNAL_API_URL=http://127.0.0.1:8000\nRULES_SIGNAL_PUBLISHER_TOKEN=<copy-once publisher key>\n\ncd /home/diolo/aviator-shadow-experiment-node\nnpm start'}</CodeBlock><p className="text-xs text-zinc-500">Use the deployment HTTPS origin when Node runs on another host. Never reuse an external consumer key or the owner API key.</p></section>
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4"><h2 className="text-lg font-semibold">Security and operating limits</h2><ul className="mt-3 list-disc space-y-2 pl-5 text-sm text-zinc-400"><li>Use HTTPS for remote access and allow only trusted dashboard origins in CORS.</li><li>Consumer credentials are scoped, expire when configured, are revocable, and are stored only as SHA-256 hashes.</li><li>Publisher write scope is internal only and cannot be assigned to external consumer keys.</li><li>Requests are rate limited per key. API key creation, rotation, and revocation are owner-authenticated and audited.</li><li>Index-only SHADOW targets are provisional simulation slots and must never authorize platform actions.</li><li>Signal delivery only distributes source-engine decisions. It never places a real-money bet.</li></ul></section>
  </div>;
}

export default function RulesSignalApi() {
  const [tab, setTab] = useState('Overview');
  const [admin, setAdmin] = useState(null);
  const [overview, setOverview] = useState(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState('');
  const [socketStatus, setSocketStatus] = useState('DISCONNECTED');
  const [signals, setSignals] = useState([]);
  const [consumerCredential, setConsumerCredential] = useState('');
  const [consumerCredentialLabel, setConsumerCredentialLabel] = useState('');
  const [newSecret, setNewSecret] = useState(null);
  const socketRef = useRef(null);
  const overviewRequestInFlight = useRef(false);
  const signalsRequestInFlight = useRef(false);
  const signalCredentialGeneration = useRef(0);

  useEffect(() => {
    let active = true;
    api.get('/api/v1/console/access').then((response) => {
      if (active) setAdmin(response.data?.api_key_management === true);
    }).catch(() => { if (active) setAdmin(false); });
    return () => { active = false; };
  }, []);

  const refreshOverview = useCallback(async () => {
    if (overviewRequestInFlight.current) return;
    overviewRequestInFlight.current = true;
    setOverviewLoading(true); setOverviewError('');
    try {
      const [overviewResult, stabilityResult] = await Promise.allSettled([
        api.get('/api/v1/console/overview'),
        api.get('/api/backend/stability'),
      ]);
      if (overviewResult.status !== 'fulfilled') throw overviewResult.reason;
      const result = overviewResult.value.data;
      result.session_watchdog = stabilityResult.status === 'fulfilled'
        ? stabilityResult.value.data?.stability?.collector_current?.session_watchdog || null
        : null;
      setOverview(result);
    } catch (error) {
      setOverviewError(error?.response?.data?.detail?.error || error.message || 'Could not read API health metrics');
      setOverview((current) => current ? { ...current, checked_at: new Date().toISOString(), api_server: { status: 'OFFLINE', ready: false } } : {
        ok: false, checked_at: new Date().toISOString(), api_server: { status: 'OFFLINE', ready: false },
        rules_engine: { status: 'UNKNOWN' }, shadow_mode: { status: 'UNKNOWN' },
        node_publisher: { status: 'UNKNOWN' }, postgresql: { status: 'UNKNOWN' },
        connected_external_consumers: null, consumer_connections: null,
        failed_deliveries: null, average_delivery_latency_ms: null,
      });
    } finally {
      overviewRequestInFlight.current = false;
      setOverviewLoading(false);
    }
  }, []);

  const refreshSignals = useCallback(async () => {
    if (signalsRequestInFlight.current) return;
    const requestGeneration = signalCredentialGeneration.current;
    signalsRequestInFlight.current = true;
    if (!consumerCredential) {
      setSignals([]);
      if (requestGeneration === signalCredentialGeneration.current) {
        signalsRequestInFlight.current = false;
      }
      return;
    }
    try {
      const response = await fetch(`${getApiBaseUrl()}/api/v1/console/signals?limit=100`, {
        cache: 'no-store',
        headers: { Authorization: `Bearer ${consumerCredential}` },
        signal: AbortSignal.timeout(8000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result?.detail?.error || `HTTP ${response.status}`);
      if (requestGeneration === signalCredentialGeneration.current) {
        setSignals(result.signals || []);
      }
    } catch {
      if (requestGeneration === signalCredentialGeneration.current) setSignals([]);
    } finally {
      if (requestGeneration === signalCredentialGeneration.current) {
        signalsRequestInFlight.current = false;
      }
    }
  }, [consumerCredential]);

  useEffect(() => {
    signalCredentialGeneration.current += 1;
    signalsRequestInFlight.current = false;
    setSignals([]);
  }, [consumerCredential]);

  useEffect(() => {
    refreshOverview(); refreshSignals();
    const timer = window.setInterval(() => { refreshOverview(); refreshSignals(); }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshOverview, refreshSignals]);

  useEffect(() => {
    if (!consumerCredential) { setSocketStatus('DISCONNECTED'); return undefined; }
    let alive = true;
    let socket;
    let retryTimer = null;
    let retryAttempt = 0;
    let connecting = false;
    function scheduleReconnect() {
      if (!alive || retryTimer !== null) return;
      const delay = Math.min(30000, 1000 * (2 ** Math.min(retryAttempt, 5)));
      retryAttempt += 1;
      setSocketStatus('RECONNECTING');
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    }
    async function connect() {
      if (!alive || connecting) return;
      connecting = true;
      try {
        const { ticket } = await requestWebSocketTicket(consumerCredential);
        if (!alive) { connecting = false; return; }
        const currentSocket = new WebSocket(websocketAddress());
        let connectedAt = null;
        socket = currentSocket;
        socketRef.current = currentSocket;
        const handleDisconnect = () => {
          if (!alive) return;
          if (connectedAt !== null && Date.now() - connectedAt >= 30000) retryAttempt = 0;
          connectedAt = null;
          if (socketRef.current === currentSocket) socketRef.current = null;
          setSocketStatus('DISCONNECTED');
          scheduleReconnect();
        };
        currentSocket.onopen = () => {
          if (alive) currentSocket.send(JSON.stringify({ type: 'authenticate', ticket }));
        };
        currentSocket.onmessage = (event) => {
          if (!alive) return;
          try {
            const message = JSON.parse(event.data);
            if (message.type === 'ready') {
              connectedAt = Date.now();
              setSocketStatus('CONNECTED');
            }
            if (message.type === 'signal' && message.signal?.action === 'BET') {
              setSignals((current) => [message.signal, ...current.filter((row) => row.signal_id !== message.signal.signal_id)].slice(0, 100));
              refreshOverview();
            }
          } catch { /* ignore malformed events; REST history remains the recovery path */ }
        };
        currentSocket.onerror = () => {
          if (!alive) return;
          handleDisconnect();
          currentSocket.close();
        };
        currentSocket.onclose = handleDisconnect;
      } catch {
        if (alive) scheduleReconnect();
      } finally {
        connecting = false;
      }
    }
    void connect();
    return () => {
      alive = false;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
      socketRef.current = null;
    };
  }, [consumerCredential, refreshOverview]);

  function showNewSecret(secret, metadata) {
    setNewSecret({ secret, metadata });
    if (metadata?.key_type === 'consumer') {
      setConsumerCredential(secret);
      setConsumerCredentialLabel(metadata?.label || 'New read-only consumer key');
    }
  }

  function updateConsumerCredential(value) {
    setConsumerCredential(value);
    setConsumerCredentialLabel(value ? 'Manually entered consumer key' : '');
  }

  return <div className="space-y-6">
    <PageHeader eyebrow="Developer tools" title="Rules Signal API" icon={Signal} description="View API health, test integrations, and read the API contract. This console requires owner authentication; external consumer credentials remain separately scoped." />
    <div role="tablist" aria-label="Rules Signal API sections" className="flex flex-wrap gap-2 border-b border-zinc-800 pb-3">{TABS.map((label) => <button key={label} type="button" role="tab" aria-selected={tab === label} onClick={() => setTab(label)} className={`rounded-lg px-4 py-2 text-sm font-medium ${tab === label ? 'bg-emerald-400 text-zinc-950' : 'border border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-100'}`}>{label}</button>)}</div>
    {newSecret && <div className="space-y-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4"><div className="flex items-start gap-3"><AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-200" /><div><h2 className="font-semibold text-amber-100">Copy this API key now</h2><p className="mt-1 text-sm text-amber-100/70">This is the only time the full secret is shown. It is kept in this page's memory only and is not written to browser storage or logs.{newSecret.metadata?.key_type === 'consumer' ? ' This consumer key is selected for the API tests and live signal feed.' : ''}</p></div></div><code className="block break-all rounded-lg border border-amber-500/20 bg-zinc-950 p-3 text-sm text-emerald-200">{newSecret.secret}</code><div className="flex flex-wrap gap-2"><CopyButton value={newSecret.secret}>Copy key</CopyButton>{newSecret.metadata?.key_type === 'consumer' && <button type="button" onClick={() => setTab('API Testing')} className="rounded-lg border border-emerald-500/40 px-3 py-2 text-sm text-emerald-200">Use in API Testing</button>}<button type="button" onClick={() => setNewSecret(null)} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm">Hide secret</button></div></div>}
    {tab === 'Overview' && <OverviewTab overview={overview} loading={overviewLoading} error={overviewError} onRefresh={() => { refreshOverview(); refreshSignals(); }} socketStatus={socketStatus} signals={signals} hasSignalCredential={Boolean(consumerCredential)} />}
    {tab === 'API Testing' && <TestPanel consumerCredential={consumerCredential} onCredentialChange={updateConsumerCredential} selectedCredentialLabel={consumerCredentialLabel} canManageKeys={admin === true} onOpenKeys={() => setTab('API Keys')} />}
    {tab === 'API Keys' && (admin === true ? <KeysTab onSecret={showNewSecret} /> : <div className="space-y-2 rounded-xl border border-zinc-800 bg-zinc-900/50 p-5"><h2 className="text-xl font-semibold">API Keys</h2><p className="text-sm text-zinc-400">Owner authentication is required for this console and for creating, rotating, or revoking keys. External consumer keys remain read-only and separately scoped.</p></div>)}
    {tab === 'Documentation' && <DocumentationTab consumerCredential={consumerCredential} />}
  </div>;
}
