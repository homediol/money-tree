#!/usr/bin/env node
/**
 * start-all.js
 * ============
 * Starts all services in the correct order:
 *
 *  1. bot/roundhistory-collector.js  — collects live round data → data/roundhistory.json
 *  2. backend/app.py                 — Flask prediction API (port 5000)
 *  3. backend/bot_api.py             — FastAPI bot control API (port 5001)
 *  4. frontend (vite dev)            — React dashboard (port 5173)
 *  5. aviator_enterprise/main.py     — Enterprise ML API (port 8000)
 *
 * Usage:  npm start   (from project root)
 */

const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const ROOT    = path.resolve(__dirname, "..");
const BACKEND = path.join(ROOT, "backend");
const BOT_DIR = path.join(ROOT, "bot");
const FRONTEND = path.join(ROOT, "frontend");
const ENTERPRISE = path.join(ROOT, "aviator_enterprise");

// ── Resolve Python binary (prefers backend .venv) ─────────────────────────
function findPython(baseDir) {
  const candidates = [
    path.join(baseDir, ".venv", "bin", "python3"),
    path.join(baseDir, ".venv", "bin", "python"),
    path.join(baseDir, ".venv", "Scripts", "python.exe"),
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

const PYTHON_BIN     = findPython(BACKEND);
const NODE_BIN       = findNode();
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
    args:    ["app.py"],
    cwd:     BACKEND,
    color:   "\x1b[33m",   // yellow
    // Give Flask 3 s to start before launching bot_api
    delayMs: 3000,
  },
  {
    name:    "bot-api",
    cmd:     PYTHON_BIN,
    args:    ["bot_api.py"],
    cwd:     BACKEND,
    color:   "\x1b[35m",   // magenta
  },
  {
    name:    "frontend",
    cmd:     "npm",
    args:    ["run", "dev"],
    cwd:     FRONTEND,
    color:   "\x1b[32m",   // green
  },
  {
    name:    "enterprise",
    cmd:     ENTERPRISE_PY,
    args:    ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
    cwd:     ENTERPRISE,
    color:   "\x1b[34m",   // blue
    optional: true,
  },
];

// ── Process registry ───────────────────────────────────────────────────────
const children = [];
let shuttingDown = false;

function label(svc) {
  return `${svc.color}[${svc.name}]\x1b[0m`;
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
    setTimeout(() => {
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
  console.log(`  Ent. Py : ${ENTERPRISE_PY}`);
  console.log("");

  for (const svc of SERVICES) {
    await launch(svc);
  }

  console.log("\n\x1b[90m[runner] all services launched. Press Ctrl+C to stop.\x1b[0m\n");
})();

process.on("SIGINT",  () => stopAll(0));
process.on("SIGTERM", () => stopAll(0));
