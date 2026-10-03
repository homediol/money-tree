#!/usr/bin/env node
/**
 * start-all.js
 * ============
 * Starts and supervises the project services in the correct order:
 *
 *  1. backend/run.py                 — legacy Flask prediction API (port 5000)
 *  2. backend/bot_api.py             — legacy FastAPI bot control API (port 5001)
 *  3. backend/main.py                — Winner Predict FastAPI API (port 8000)
 *  4. frontend (vite dev)            — React dashboard (port 5173)
 *  5. aviator_enterprise/main.py     — legacy Enterprise ML API (port 8002)
 *
 * Usage: npm start, or npm run backend for only the port-8000 API.
 */

const { spawn, spawnSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");
const http = require("http");

const ROOT    = path.resolve(__dirname, "..");
const BACKEND = path.join(ROOT, "backend");
const BOT_DIR = path.join(ROOT, "bot");
const FRONTEND = path.join(ROOT, "frontend");
const ENTERPRISE = path.join(ROOT, "aviator_enterprise");

// The Python backend reads backend/.env itself. Share the same database
// configuration with the Node collector so both use one PostgreSQL history.
const backendEnv = path.join(BACKEND, ".env");
if (fs.existsSync(backendEnv) && typeof process.loadEnvFile === "function") {
  process.loadEnvFile(backendEnv);
}

// ── Resolve Python binary (prefers root .venv with TensorFlow) ────────────
function findPython() {
  const candidates = [
    path.join(ROOT, ".venv", "bin", "python"),
    path.join(ROOT, ".venv", "bin", "python3"),
    path.join(ROOT, ".venv", "Scripts", "python.exe"),
    path.join(BACKEND, ".venv", "bin", "python"),
    path.join(BACKEND, ".venv", "bin", "python3"),
    path.join(BACKEND, ".venv", "Scripts", "python.exe"),
    "python3.11",
    "python3",
    "python",
  ];
  for (const c of candidates) {
    if (c.includes(path.sep) && fs.existsSync(c)) return c;
    if (!c.includes(path.sep)) return c;
  }
  return "python3";
}

// ── Resolve Node binary (supports nvm) ────────────────────────────────────
function findNode() {
  const nvmNode = path.join(os.homedir(), ".nvm", "versions", "node");
  if (fs.existsSync(nvmNode)) {
    const versions = fs.readdirSync(nvmNode).sort().reverse();
    for (const v of versions) {
      const bin = path.join(nvmNode, v, "bin", "node");
      if (fs.existsSync(bin)) return bin;
    }
  }
  return "node";
}

const PYTHON_BIN     = findPython();
const NODE_BIN       = findNode();
const NPM_BIN        = (() => {
  const candidate = path.join(path.dirname(NODE_BIN), "npm");
  return fs.existsSync(candidate) ? candidate : "npm";
})();
const ENTERPRISE_PY  = (() => {
  const candidates = [
    path.join(ENTERPRISE, ".venv-enterprise", "bin", "python3"),
    path.join(ENTERPRISE, ".venv-enterprise", "bin", "python"),
    path.join(ROOT, ".venv-enterprise", "bin", "python3"),
    path.join(ROOT, ".venv-enterprise", "bin", "python"),
    PYTHON_BIN,
  ];
  for (const c of candidates) {
    if (c.includes(path.sep) && fs.existsSync(c)) return c;
  }
  return PYTHON_BIN;
})();

// Winner Predict backend Python (backend/.venv has the tested deps; falls
// back to the root .venv that ensureBackendEnvironment() keeps up to date)
const BACKEND_PY = (() => {
  const candidates = [
    path.join(BACKEND, ".venv", "bin", "python3"),
    path.join(BACKEND, ".venv", "bin", "python"),
    path.join(BACKEND, ".venv", "Scripts", "python.exe"),
    PYTHON_BIN,
  ];
  for (const c of candidates) {
    if (c.includes(path.sep) && fs.existsSync(c)) return c;
  }
  return PYTHON_BIN;
})();

function runSetup(cmd, args, cwd = ROOT) {
  console.log(`\x1b[90m[runner]\x1b[0m setup: ${cmd} ${args.join(" ")}`);
  const result = spawnSync(cmd, args, {
    cwd,
    env: process.env,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

function ensureBackendEnvironment() {
  const probe = spawnSync(BACKEND_PY, ["-c", "import fastapi, uvicorn, pydantic_settings"], {
    cwd: BACKEND,
    env: process.env,
    stdio: "ignore",
  });
  if (probe.status === 0) return BACKEND_PY;

  console.warn("[runner] backend dependencies are incomplete; installing once…");
  runSetup(BACKEND_PY, ["-m", "pip", "install", "-r", path.join(BACKEND, "requirements.txt")]);
  return BACKEND_PY;
}

// ── Service definitions ────────────────────────────────────────────────────
const SERVICES = [
  {
    name:    "flask",
    cmd:     PYTHON_BIN,
    args:    ["run.py"],
    cwd:     BACKEND,
    color:   "\x1b[33m",   // yellow
    port:    5000,
    // Give Flask 3 s to start before launching bot_api
    delayMs: 3000,
  },
  {
    name:    "bot-api",
    cmd:     PYTHON_BIN,
    args:    ["bot_api.py"],
    cwd:     BACKEND,
    color:   "\x1b[35m",   // magenta
    port:    5001,
  },
  {
    name:    "collector",
    cmd:     NODE_BIN,
    args:    [path.join(BOT_DIR, "roundhistory-collector.js")],
    cwd:     BOT_DIR,
    color:   "\x1b[36m",
    restart: true,
  },
  {
    name:    "backend",
    cmd:     BACKEND_PY,
    args:    ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
    cwd:     BACKEND,
    color:   "\x1b[96m",   // bright cyan
    port:    8000,
    healthPath: "/health",
    expectedService: "winner-predict-backend",
    // Probe every capability the combined analytics page requires. Checking
    // reports alone misses older workers that have reports/current but lack
    // the progress route used by its live countdown cards.
    requiredApiPaths: [
      "/api/analytics/progress",
      "/api/analytics/reports?report_type=ROUND_100_REPORT&limit=1",
      "/api/backend/capabilities",
    ],
    restart: false,
    // Winner Predict API — owns port 8000 (frontend defaults point here)
  },
  {
    name:    "frontend",
    cmd:     NPM_BIN,
    args:    ["run", "dev"],
    cwd:     FRONTEND,
    color:   "\x1b[32m",   // green
    port:    5173,
    restart: true,
  },
  {
    name:    "enterprise",
    cmd:     ENTERPRISE_PY,
    // Legacy app — moved off 8000 so the Winner Predict backend owns it
    args:    ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"],
    cwd:     ENTERPRISE,
    color:   "\x1b[34m",   // blue
    port:    8002,
    optional: true,
  },
];

// ── Process registry / supervision ─────────────────────────────────────────
const serviceStates = new Map();
const scheduledTimers = new Set();
let shuttingDown = false;
const lifecyclePath = path.join(ROOT, "data", "backend_lifecycle.jsonl");
const collectorPidPath = path.join(ROOT, "data", "bot", "collector-supervisor.json");

function existingCollectorPid() {
  try {
    const status = JSON.parse(fs.readFileSync(collectorPidPath, "utf8"));
    const pid = Number(status.pid);
    if (!status.running || !Number.isInteger(pid) || pid <= 0) return null;
    process.kill(pid, 0);
    if (process.platform === "linux") {
      const command = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
      if (!command.includes("roundhistory-collector.js")) return null;
    }
    return pid;
  } catch (_) {
    return null;
  }
}

function recordCollector(child, running, reason = null) {
  try {
    fs.mkdirSync(path.dirname(collectorPidPath), { recursive: true });
    fs.writeFileSync(collectorPidPath, JSON.stringify({
      pid: child?.pid || null, running, reason, timestamp: timestamp(),
      supervisor_pid: process.pid,
    }) + "\n");
  } catch (error) {
    console.error(`[runner] could not persist collector status: ${error.message}`);
  }
}

function recordBackend(event, fields = {}) {
  try {
    fs.mkdirSync(path.dirname(lifecyclePath), { recursive: true });
    fs.appendFileSync(lifecyclePath, JSON.stringify({
      event, service: "winner-predict-backend", timestamp: timestamp(),
      supervisor_pid: process.pid, ...fields,
    }) + "\n");
  } catch (error) {
    console.error(`[runner] could not persist backend lifecycle: ${error.message}`);
  }
}

function label(svc) {
  return `${svc.color}[${svc.name}]\x1b[0m`;
}

function isPortInUse(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host });
    socket.setTimeout(800);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => resolve(false));
  });
}

function timestamp() {
  return new Date().toISOString();
}

function stateFor(svc) {
  if (!serviceStates.has(svc.name)) {
    serviceStates.set(svc.name, {
      child: null, restartAttempts: 0, startedAt: 0,
      external: false, monitor: null,
    });
  }
  return serviceStates.get(svc.name);
}

function probeJson(port, pathname, headers = {}, timeoutMs = 5000) {
  return new Promise((resolve) => {
    const request = http.get({ host: "127.0.0.1", port, path: pathname, timeout: timeoutMs, headers }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { if (body.length < 65536) body += chunk; });
      response.on("end", () => {
        try { resolve({ status: response.statusCode, data: JSON.parse(body) }); }
        catch (_) { resolve({ status: response.statusCode, data: null }); }
      });
    });
    request.on("timeout", () => { request.destroy(); resolve(null); });
    request.on("error", () => resolve(null));
  });
}

async function expectedServiceOwnsPort(svc) {
  if (!svc.healthPath || !svc.expectedService) return true;
  // /health verifies PostgreSQL and includes lifecycle diagnostics; allow for
  // a slow but responsive database instead of treating it as an unknown owner.
  const response = await probeJson(svc.port, svc.healthPath, {}, 20000);
  if (!response) return null;
  const actualService = response?.data?.service;
  if (actualService !== svc.expectedService) {
    console.warn(`[runner] ${timestamp()} health probe on port ${svc.port} returned HTTP ${response.status} with service ${actualService || "UNKNOWN"}; expected ${svc.expectedService}`);
    return false;
  }
  return true;
}

async function stopVerifiedStaleBackend(pid, svc) {
  // A restart is allowed only when the health response gave us a PID whose
  // command and working directory prove it is this checkout's uvicorn app.
  // Never kill an arbitrary process that happens to use port 8000.
  if (process.platform !== "linux" || !Number.isInteger(pid) || pid <= 1 || pid === process.pid) return false;
  const isVerifiedBackend = () => {
    try {
      const processDir = fs.realpathSync(`/proc/${pid}/cwd`);
      const command = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8").replace(/\0/g, " ");
      return processDir === fs.realpathSync(BACKEND)
        && command.includes("uvicorn") && command.includes("main:app");
    } catch (_) {
      return false;
    }
  };
  if (!isVerifiedBackend()) return false;
  try {
    process.kill(pid, "SIGTERM");
  } catch (_) {
    return false;
  }
  // Uvicorn closes its listening socket before lifespan teardown releases the
  // single-instance flock. Waiting for the port alone can race the next start
  // into SingleInstanceError while the old worker is still shutting down.
  const exitedAndReleased = async () => {
    let processExited = false;
    try {
      process.kill(pid, 0);
    } catch (error) {
      processExited = error.code === "ESRCH";
    }
    return processExited && !await isPortInUse(svc.port);
  };
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (await exitedAndReleased()) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  // A verified obsolete backend can stop serving HTTP but remain alive in a
  // stuck background task, retaining the singleton lock forever. Escalate
  // only after TERM grace and only if PID identity still matches this app.
  if (!isVerifiedBackend()) return await exitedAndReleased();
  try {
    process.kill(pid, "SIGKILL");
  } catch (error) {
    if (error.code !== "ESRCH") return false;
  }
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (await exitedAndReleased()) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  console.error(`[runner] ${timestamp()} verified stale backend PID ${pid} did not exit after TERM/KILL; not starting a duplicate`);
  return false;
}

function prefixLines(svc, stream, writer) {
  let buf = "";
  let writable = true;
  writer.on?.("error", (err) => {
    if (err?.code === "EPIPE") writable = false;
  });
  const write = (value) => {
    if (!writable || writer.destroyed || !writer.writable) return;
    try { writer.write(value); } catch (err) {
      if (err?.code === "EPIPE") writable = false;
    }
  };
  stream.on("data", (chunk) => {
    buf += chunk.toString();
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? "";
    for (const line of lines) {
      if (line.trim()) write(`${label(svc)} ${line}\n`);
    }
  });
  stream.on("end", () => {
    if (buf.trim()) write(`${label(svc)} ${buf}\n`);
  });
}

function signalChild(child, signal) {
  if (!child?.pid) return;
  try {
    if (process.platform !== "win32") process.kill(-child.pid, signal);
    else child.kill(signal);
  } catch (_) {}
}

function stopAll(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  process.exitCode = code;
  console.log(`\n\x1b[90m[runner ${timestamp()}] graceful shutdown requested…\x1b[0m`);
  for (const timer of scheduledTimers) clearTimeout(timer);
  scheduledTimers.clear();
  for (const state of serviceStates.values()) {
    if (state.monitor) clearInterval(state.monitor);
    signalChild(state.child, "SIGTERM");
  }
  setTimeout(() => {
    for (const state of serviceStates.values()) signalChild(state.child, "SIGKILL");
  }, 7000).unref();
  setTimeout(() => process.exit(code), 7500).unref();
}

function scheduleRestart(svc) {
  if (shuttingDown || svc.optional || svc.restart === false) return;
  const state = stateFor(svc);
  state.restartAttempts += 1;
  const delay = Math.min(1000 * (2 ** (state.restartAttempts - 1)), 30000);
  console.error(`${label(svc)} ${timestamp()} restarting in ${delay}ms (attempt ${state.restartAttempts})`);
  const timer = setTimeout(() => {
    scheduledTimers.delete(timer);
    launch(svc, true);
  }, delay);
  scheduledTimers.add(timer);
}

function monitorExisting(svc) {
  const state = stateFor(svc);
  if (state.monitor || !svc.port) return;
  state.monitor = setInterval(async () => {
    if (shuttingDown) return;
    const listening = await isPortInUse(svc.port);
    if (listening) return;
    clearInterval(state.monitor);
    state.monitor = null;
    state.external = false;
    console.warn(`${label(svc)} ${timestamp()} reused instance disappeared; taking ownership`);
    scheduleRestart(svc);
  }, 5000);
}

// ── Launch a single service (with bounded exponential recovery) ───────────
function launch(svc, restarting = false) {
  return new Promise((resolve) => {
    const timer = setTimeout(async () => {
      scheduledTimers.delete(timer);
      try {
        if (svc.port && await isPortInUse(svc.port)) {
          const ownerMatches = await expectedServiceOwnsPort(svc);
          if (ownerMatches === false) {
            console.error(`${label(svc)} ${timestamp()} PORT CONFLICT: ${svc.port} is owned by a different service`);
            resolve(null);
            // A backend port conflict must not shut down independently useful
            // services such as the history collector. Never replace an owner
            // we cannot prove belongs to this checkout.
            if (svc.name !== "backend") stopAll(1);
            return;
          }
          if (ownerMatches === null) {
            console.error(`${label(svc)} ${timestamp()} could not verify the service on port ${svc.port}; leaving it untouched`);
            resolve(null);
            if (svc.name !== "backend") stopAll(1);
            return;
          }
          let compatible = true;
          let health = null;
          const requiredApiPaths = svc.requiredApiPaths || (svc.requiredApiPath ? [svc.requiredApiPath] : []);
          if (requiredApiPaths.length) {
            health = await probeJson(svc.port, svc.healthPath);
            const capabilities = await Promise.all(requiredApiPaths.map(async (apiPath) => ({
              apiPath, response: await probeJson(svc.port, apiPath, process.env.API_KEY
                ? { Authorization: `Bearer ${process.env.API_KEY}` } : {}),
            })));
            // Probe with the configured API key so middleware cannot mask a
            // missing route as a successful 401. A 404 means the route is
            // absent; 422 means a known capability rejects its query. Timeout
            // is inconclusive and must never terminate a healthy backend.
            const incompatible = capabilities.find(({ apiPath, response }) =>
              response?.status === 404
              || (apiPath.includes("ROUND_100_REPORT") && response?.status === 422));
            compatible = !incompatible;
            if (incompatible) {
              const { apiPath, response: capability } = incompatible;
              const stalePid = Number(health?.data?.backend?.pid);
              console.warn(`${label(svc)} ${timestamp()} healthy backend PID ${stalePid || "unknown"} failed compatibility probe (${capability.status}) ${apiPath}; checking whether it is safe to replace`);
              const stopped = await stopVerifiedStaleBackend(stalePid, svc);
              if (!stopped) {
                console.error(`${label(svc)} ${timestamp()} OUTDATED BACKEND: HTTP health passed but compatibility probe returned ${capability.status} for ${apiPath}. It was not replaced because its PID could not be verified as this checkout's uvicorn process.`);
                resolve(null);
                // Preserve the independent collector and leave an unverified
                // backend untouched. Its incompatibility remains visible in
                // the log, while collection can still recover.
                return;
              }
              recordBackend("supervisor_replaced_stale_backend", {
                pid: stalePid, port: svc.port, incompatible_probe: apiPath,
                probe_status: capability.status,
                missing_route: capability.status === 404 ? apiPath : undefined,
                reason: capability.status === 404 ? "required_api_route_missing" : "required_api_feature_rejected",
              });
              console.warn(`${label(svc)} ${timestamp()} stopped verified stale backend PID ${stalePid}; starting the current application`);
            }
          }
          if (compatible) {
            const state = stateFor(svc);
            state.external = true;
            state.restartAttempts = 0;
            console.warn(`${label(svc)} ${timestamp()} verified existing instance on port ${svc.port}; not starting a duplicate`);
            if (svc.name === "backend") recordBackend("supervisor_external_instance", { port: svc.port });
            else monitorExisting(svc);
            resolve(null);
            return;
          }
        }

        console.log(`\x1b[90m[runner ${timestamp()}]\x1b[0m starting ${label(svc)}: ${svc.cmd} ${svc.args.join(" ")}`);

        const env = { ...process.env };
        if (svc.name === "backend") env.WINNER_COLLECTOR_EXTERNAL = "1";
        const nvmBin = path.dirname(NODE_BIN);
        if (nvmBin !== "node" && fs.existsSync(nvmBin)) {
          env.PATH = `${nvmBin}:${env.PATH || ""}`;
        }

        const child = spawn(svc.cmd, svc.args, {
          cwd: svc.cwd, env,
          stdio: ["ignore", "pipe", "pipe"],
          shell: process.platform === "win32",
          detached: process.platform !== "win32",
        });

        const state = stateFor(svc);
        state.child = child;
        state.external = false;
        state.startedAt = Date.now();
        if (svc.name === "collector") recordCollector(child, true);
        if (svc.name === "backend") recordBackend("supervisor_spawn", {
          pid: child.pid, ppid: process.pid, startup_reason: restarting ? "supervisor_restart" : "supervisor_start",
          command: [svc.cmd, ...svc.args],
        });
        prefixLines(svc, child.stdout, process.stdout);
        prefixLines(svc, child.stderr, process.stderr);

        let handled = false;
        const stopped = (reason, exitCode = null, exitSignal = null) => {
          if (handled) return;
          handled = true;
          if (state.child === child) state.child = null;
          if (svc.name === "collector") recordCollector(child, false, reason);
          if (shuttingDown) {
            if (svc.name === "backend") recordBackend("supervisor_exit", {
              pid: child.pid, reason: `supervisor_shutdown:${reason}`, unexpected: false,
              exit_code: exitCode, signal: exitSignal,
              uptime_seconds: Math.round((Date.now() - state.startedAt) / 1000),
            });
            return;
          }
          if (svc.name === "backend") {
            recordBackend("supervisor_exit", {pid: child.pid, reason, unexpected: true,
              exit_code: exitCode, signal: exitSignal,
              uptime_seconds: Math.round((Date.now() - state.startedAt) / 1000)});
            console.error(`${label(svc)} ${timestamp()} stopped unexpectedly (${reason}); backend restart disabled`);
            process.exitCode = 1;
            return;
          }
          if (Date.now() - state.startedAt > 60000) state.restartAttempts = 0;
          console.error(`${label(svc)} ${timestamp()} stopped unexpectedly (${reason})`);
          scheduleRestart(svc);
        };
        child.once("error", (error) => stopped(`spawn error: ${error.message}`));
        child.once("exit", (code, signal) => stopped(`code=${code} signal=${signal || "none"}`, code, signal));

        resolve(child);
      } catch (error) {
        console.error(`${label(svc)} ${timestamp()} launch failure: ${error.stack || error}`);
        resolve(null);
        if (restarting) scheduleRestart(svc);
        else stopAll(1);
      }
    }, restarting ? 0 : (svc.delayMs || 0));
    scheduledTimers.add(timer);
  });
}

// ── Main ───────────────────────────────────────────────────────────────────
(async () => {
  const backendOnly = process.argv.includes("--backend-only");
  const apiOnly = process.argv.includes("--api-only");
  let selectedServices = apiOnly
    ? SERVICES.filter((svc) => svc.name === "backend")
    : backendOnly
    ? SERVICES.filter((svc) => svc.name === "collector" || svc.name === "backend")
    : SERVICES;
  console.log("\x1b[1m\x1b[37m╔══════════════════════════════════════╗\x1b[0m");
  console.log(apiOnly
    ? "\x1b[1m\x1b[37m║   Winner Predict — API Only            ║\x1b[0m"
    : backendOnly
    ? "\x1b[1m\x1b[37m║   Winner Predict — Backend Supervisor ║\x1b[0m"
    : "\x1b[1m\x1b[37m║   Aviator ML — Starting All Services  ║\x1b[0m");
  console.log("\x1b[1m\x1b[37m╚══════════════════════════════════════╝\x1b[0m");
  console.log(`  Python  : ${PYTHON_BIN}`);
  console.log(`  Node    : ${NODE_BIN}`);
  console.log(`  npm     : ${NPM_BIN}`);
  console.log(`  Ent. Py : ${ENTERPRISE_PY}`);
  console.log("");

  const backendPython = ensureBackendEnvironment();
  const backend = SERVICES.find((svc) => svc.name === "backend");
  const backendPortBusy = await isPortInUse(backend.port);
  if (backendPortBusy) {
    const ownerMatches = await expectedServiceOwnsPort(backend);
    if (ownerMatches === false) {
      console.error(`[runner] ${timestamp()} PORT CONFLICT: ${backend.port} is owned by a different service`);
      selectedServices = selectedServices.filter((svc) => svc.name !== "backend");
    }
    if (ownerMatches === null) {
      console.error(`[runner] ${timestamp()} could not verify the service on port ${backend.port}; leaving it untouched`);
      selectedServices = selectedServices.filter((svc) => svc.name !== "backend");
    }
  }
  const collectorPid = existingCollectorPid();
  if (collectorPid) {
    selectedServices = selectedServices.filter((svc) => svc.name !== "collector");
    console.warn(`[runner] ${timestamp()} leaving verified existing collector ownership intact (pid ${collectorPid})`);
  }
  for (const svc of selectedServices) {
    if (svc.name === "flask" || svc.name === "bot-api") {
      svc.cmd = backendPython;
    }
  }

  for (const svc of selectedServices) {
    await launch(svc);
  }

  console.log("\n\x1b[90m[runner] requested services launched and supervised. Press Ctrl+C to stop.\x1b[0m\n");
})().catch((error) => {
  console.error(`[runner ${timestamp()}] fatal startup error:`, error);
  stopAll(1);
});

process.on("SIGINT",  () => stopAll(0));
process.on("SIGTERM", () => stopAll(0));
process.on("uncaughtException", (error) => {
  console.error(`[runner ${timestamp()}] uncaught exception:`, error);
  stopAll(1);
});
process.on("unhandledRejection", (error) => {
  console.error(`[runner ${timestamp()}] unhandled rejection:`, error);
  stopAll(1);
});
