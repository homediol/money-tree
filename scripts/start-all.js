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
    name:    "backend",
    cmd:     BACKEND_PY,
    args:    ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
    cwd:     BACKEND,
    color:   "\x1b[96m",   // bright cyan
    port:    8000,
    healthPath: "/health",
    expectedService: "winner-predict-backend",
    restart: true,
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

function probeJson(port, pathname) {
  return new Promise((resolve) => {
    const request = http.get({ host: "127.0.0.1", port, path: pathname, timeout: 1500 }, (response) => {
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
  const response = await probeJson(svc.port, svc.healthPath);
  return response?.data?.service === svc.expectedService;
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
          if (!await expectedServiceOwnsPort(svc)) {
            console.error(`${label(svc)} ${timestamp()} PORT CONFLICT: ${svc.port} is owned by a different service`);
            resolve(null);
            stopAll(1);
            return;
          }
          const state = stateFor(svc);
          state.external = true;
          state.restartAttempts = 0;
          console.warn(`${label(svc)} ${timestamp()} verified existing instance on port ${svc.port}; not starting a duplicate`);
          monitorExisting(svc);
          resolve(null);
          return;
        }

        console.log(`\x1b[90m[runner ${timestamp()}]\x1b[0m starting ${label(svc)}: ${svc.cmd} ${svc.args.join(" ")}`);

        const env = { ...process.env };
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
        prefixLines(svc, child.stdout, process.stdout);
        prefixLines(svc, child.stderr, process.stderr);

        let handled = false;
        const stopped = (reason) => {
          if (handled) return;
          handled = true;
          if (state.child === child) state.child = null;
          if (shuttingDown) return;
          if (Date.now() - state.startedAt > 60000) state.restartAttempts = 0;
          console.error(`${label(svc)} ${timestamp()} stopped unexpectedly (${reason})`);
          scheduleRestart(svc);
        };
        child.once("error", (error) => stopped(`spawn error: ${error.message}`));
        child.once("exit", (code, signal) => stopped(`code=${code} signal=${signal || "none"}`));

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
  const selectedServices = backendOnly
    ? SERVICES.filter((svc) => svc.name === "backend")
    : SERVICES;
  console.log("\x1b[1m\x1b[37m╔══════════════════════════════════════╗\x1b[0m");
  console.log(backendOnly
    ? "\x1b[1m\x1b[37m║   Winner Predict — Backend Supervisor ║\x1b[0m"
    : "\x1b[1m\x1b[37m║   Aviator ML — Starting All Services  ║\x1b[0m");
  console.log("\x1b[1m\x1b[37m╚══════════════════════════════════════╝\x1b[0m");
  console.log(`  Python  : ${PYTHON_BIN}`);
  console.log(`  Node    : ${NODE_BIN}`);
  console.log(`  npm     : ${NPM_BIN}`);
  console.log(`  Ent. Py : ${ENTERPRISE_PY}`);
  console.log("");

  const backendPython = ensureBackendEnvironment();
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
