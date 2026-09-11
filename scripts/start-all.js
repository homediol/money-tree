#!/usr/bin/env node
/**
 * start-all.js
 * ============
 * Starts all services in the correct order:
 *
 *  1. bot/roundhistory-collector.js  — collects live round data → data/roundhistory.json
 *  2. backend/app.py                 — legacy Flask prediction API (port 5000)
 *  3. backend/bot_api.py             — legacy FastAPI bot control API (port 5001)
 *  4. backend/main.py                — Winner Predict FastAPI API (port 8000)
 *  5. frontend (vite dev)            — React dashboard (port 5173)
 *  6. aviator_enterprise/main.py     — legacy Enterprise ML API (port 8002)
 *
 * Usage:  npm start   (from project root)
 */

const { spawn, spawnSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");

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
  const rootVenvPython = path.join(ROOT, ".venv", "bin", "python");
  const rootVenvPythonWin = path.join(ROOT, ".venv", "Scripts", "python.exe");
  const venvPython = process.platform === "win32" ? rootVenvPythonWin : rootVenvPython;

  if (!fs.existsSync(venvPython)) {
    runSetup("python3.11", ["-m", "venv", path.join(ROOT, ".venv")]);
  }

  runSetup(venvPython, ["-m", "pip", "install", "--upgrade", "pip"]);
  runSetup(venvPython, ["-m", "pip", "install", "-r", path.join(BACKEND, "requirements.txt")]);
  return venvPython;
}

// ── Service definitions ────────────────────────────────────────────────────
const SERVICES = [
  {
    name:    "collector",
    cmd:     NODE_BIN,
    args:    ["roundhistory-collector.js"],
    cwd:     BOT_DIR,
    color:   "\x1b[36m",   // cyan
    // Collector is optional — don't kill everything if it exits
    optional: true,
  },
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
    // Winner Predict API — owns port 8000 (frontend defaults point here)
  },
  {
    name:    "frontend",
    cmd:     NPM_BIN,
    args:    ["run", "dev"],
    cwd:     FRONTEND,
    color:   "\x1b[32m",   // green
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

// ── Process registry ───────────────────────────────────────────────────────
const children = [];
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

function prefixLines(svc, stream, writer) {
  let buf = "";
  stream.on("data", (chunk) => {
    buf += chunk.toString();
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? "";
    for (const line of lines) {
      if (line.trim()) writer.write(`${label(svc)} ${line}\n`);
    }
  });
  stream.on("end", () => {
    if (buf.trim()) writer.write(`${label(svc)} ${buf}\n`);
  });
}

function stopAll(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log("\n\x1b[90m[runner] shutting down all services…\x1b[0m");
  for (const child of children) {
    if (!child.killed) {
      try { child.kill("SIGTERM"); } catch (_) {}
    }
  }
  setTimeout(() => process.exit(code), 1000);
}

// ── Launch a single service (with optional delay) ─────────────────────────
function launch(svc) {
  return new Promise((resolve) => {
    setTimeout(async () => {
      if (svc.port && await isPortInUse(svc.port)) {
        console.warn(
          `${label(svc)} port ${svc.port} is already in use; assuming an existing instance is running and reusing it.`
        );
        resolve(null);
        return;
      }

      console.log(`\x1b[90m[runner]\x1b[0m starting ${label(svc)}: ${svc.cmd} ${svc.args.join(" ")}`);

      const env = { ...process.env };
      // Inject nvm node into PATH for npm/node commands
      const nvmBin = path.dirname(NODE_BIN);
      if (nvmBin !== "node" && fs.existsSync(nvmBin)) {
        env.PATH = `${nvmBin}:${env.PATH || ""}`;
      }

      const child = spawn(svc.cmd, svc.args, {
        cwd:   svc.cwd,
        env,
        stdio: ["ignore", "pipe", "pipe"],
        shell: process.platform === "win32",
      });

      children.push(child);
      prefixLines(svc, child.stdout, process.stdout);
      prefixLines(svc, child.stderr, process.stderr);

      child.on("exit", (code, signal) => {
        if (shuttingDown) return;
        if (svc.optional) {
          console.warn(`${label(svc)} exited (code=${code} signal=${signal || "none"}) — optional, continuing`);
        } else {
          console.error(`${label(svc)} exited unexpectedly (code=${code} signal=${signal || "none"})`);
          stopAll(code || 1);
        }
      });

      resolve(child);
    }, svc.delayMs || 0);
  });
}

// ── Main ───────────────────────────────────────────────────────────────────
(async () => {
  console.log("\x1b[1m\x1b[37m╔══════════════════════════════════════╗\x1b[0m");
  console.log("\x1b[1m\x1b[37m║   Aviator ML — Starting All Services  ║\x1b[0m");
  console.log("\x1b[1m\x1b[37m╚══════════════════════════════════════╝\x1b[0m");
  console.log(`  Python  : ${PYTHON_BIN}`);
  console.log(`  Node    : ${NODE_BIN}`);
  console.log(`  npm     : ${NPM_BIN}`);
  console.log(`  Ent. Py : ${ENTERPRISE_PY}`);
  console.log("");

  const backendPython = ensureBackendEnvironment();
  for (const svc of SERVICES) {
    if (svc.name === "flask" || svc.name === "bot-api") {
      svc.cmd = backendPython;
    }
  }

  for (const svc of SERVICES) {
    await launch(svc);
  }

  console.log("\n\x1b[90m[runner] all services launched. Press Ctrl+C to stop.\x1b[0m\n");
})();

process.on("SIGINT",  () => stopAll(0));
process.on("SIGTERM", () => stopAll(0));
