#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawn, spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const ENV_FILE = path.join(ROOT, "behaviour_detection", ".env");
const BACKEND_ENV_FILE = path.join(ROOT, "backend", ".env");
const PYTHON = path.join(ROOT, "behaviour_detection", ".venv", "bin", "python");
const SCRIPT = path.join(ROOT, "scripts", "behaviour");
const STATE_DIR = path.join(ROOT, "data", "behaviour");
const STATE_FILE = path.join(STATE_DIR, "runtime.json");
const STOP_REQUEST_FILE = path.join(STATE_DIR, "stop-request.json");
const LOG_DIR = path.join(STATE_DIR, "logs");
const EXPECTED_SERVICE = "advanced-crash-behaviour";
const services = new Map();
let shuttingDown = false;

function envValue(name, fallback = "") {
  if (process.env[name] !== undefined) return String(process.env[name]).trim();
  try {
    const line = fs.readFileSync(ENV_FILE, "utf8").split(/\r?\n/)
      .find((row) => row.trimStart().startsWith(`${name}=`));
    if (!line) return fallback;
    let value = line.slice(line.indexOf("=") + 1).trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) ||
        (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    return value || fallback;
  } catch {
    return fallback;
  }
}

function configuredWinnerDatabaseUrl() {
  return process.env.BEHAVIOUR_SOURCE_DATABASE_URL ||
    process.env.WINNER_PREDICT_SOURCE_DATABASE_URL || process.env.DATABASE_URL ||
    envValue("BEHAVIOUR_SOURCE_DATABASE_URL") || envValue("WINNER_PREDICT_SOURCE_DATABASE_URL") ||
    fileEnvValue(BACKEND_ENV_FILE, "DATABASE_URL");
}

function fileEnvValue(filePath, name) {
  try {
    const line = fs.readFileSync(filePath, "utf8").split(/\r?\n/)
      .find((row) => row.trimStart().startsWith(`${name}=`));
    if (!line) return "";
    let value = line.slice(line.indexOf("=") + 1).trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) ||
        (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    return value;
  } catch {
    return "";
  }
}

function writeState() {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  let supervisorPid = null;
  if (process.platform === "linux") {
    try {
      const parentCommand = fs.readFileSync(`/proc/${process.ppid}/cmdline`, "utf8");
      if (parentCommand.includes("start-all.js")) supervisorPid = process.ppid;
    } catch {}
  }
  const state = {
    pid: process.pid,
    supervisor_pid: supervisorPid,
    started_at: new Date().toISOString(),
    children: [...services.entries()].map(([name, entry]) => ({
      name, pid: entry.child?.pid || null,
    })),
  };
  const temporary = `${STATE_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, STATE_FILE);
}

function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, "utf8")); }
  catch { return null; }
}

function pidAlive(pid) {
  if (!Number.isInteger(Number(pid)) || Number(pid) < 2) return false;
  try { process.kill(Number(pid), 0); return true; }
  catch (error) { return error?.code === "EPERM"; }
}

function isRuntimePid(pid) {
  const candidate = Number(pid);
  if (candidate === process.pid || !pidAlive(candidate)) return false;
  if (process.platform !== "linux") return true;
  try {
    const args = fs.readFileSync(`/proc/${candidate}/cmdline`, "utf8").split("\0").filter(Boolean);
    const cwd = fs.realpathSync(`/proc/${candidate}/cwd`);
    const isControlCommand = args.some((arg) => ["--stop", "--status", "--logs"].includes(arg));
    const isRuntimeCommand = args.some((arg) => path.resolve(cwd, arg) === path.resolve(__filename));
    return !isControlCommand && isRuntimeCommand && cwd === fs.realpathSync(ROOT);
  } catch {
    return false;
  }
}

function requestJson(port, pathname) {
  return new Promise((resolve) => {
    const request = http.get({ host: "127.0.0.1", port, path: pathname, timeout: 1500 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try { resolve({ status: response.statusCode, payload: JSON.parse(body) }); }
        catch { resolve({ status: response.statusCode, payload: null }); }
      });
    });
    request.on("timeout", () => { request.destroy(); resolve(null); });
    request.on("error", () => resolve(null));
  });
}

function healthCheck(port) {
  return requestJson(port, "/health/live");
}

async function waitForReady(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !shuttingDown) {
    const response = await requestJson(port, "/health/ready");
    if (response?.status === 200 && response.payload?.ok === true) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

function runCommand(args, label) {
  const result = spawnSync(SCRIPT, args, { cwd: ROOT, env: process.env, stdio: "inherit" });
  if (result.error || result.status !== 0) {
    const detail = result.error?.message || `exit ${result.status ?? "unknown"}`;
    throw new Error(`${label} failed (${detail})`);
  }
}

function startChild(name, args) {
  const entry = services.get(name);
  if (!entry || shuttingDown) return;
  const child = spawn(PYTHON, args, {
    cwd: ROOT,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  entry.child = child;
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const log = fs.createWriteStream(path.join(LOG_DIR, `${name}.log`), { flags: "a", mode: 0o600 });
  for (const stream of [child.stdout, child.stderr]) {
    stream.on("data", (chunk) => {
      log.write(chunk);
      process.stdout.write(`[behaviour:${name}] ${chunk}`);
    });
  }
  child.once("error", (error) => process.stderr.write(`[behaviour:${name}] spawn failed: ${error.message}\n`));
  child.once("exit", (code, signal) => {
    log.end();
    if (entry.child === child) entry.child = null;
    writeState();
    if (shuttingDown) return;
    entry.attempts = code === 0 ? 0 : entry.attempts + 1;
    const delay = Math.min(1000 * (2 ** Math.max(0, entry.attempts - 1)), 30000);
    process.stderr.write(`[behaviour:${name}] stopped (code=${code}, signal=${signal || "none"}); restarting in ${delay}ms\n`);
    entry.timer = setTimeout(() => startChild(name, args), delay);
  });
  writeState();
  return child;
}

async function startRuntime() {
  const previous = readState();
  if (previous && isRuntimePid(Number(previous.pid))) {
    console.log(`Behaviour Detection is already managed by PID ${previous.pid}.`);
    return;
  }

  const port = Number(envValue("BEHAVIOUR_API_PORT", "8100")) || 8100;
  const current = await healthCheck(port);
  if (current?.payload?.service === EXPECTED_SERVICE) {
    console.log(`Behaviour Detection API is already running at http://127.0.0.1:${port}.`);
    return;
  }
  if (current) throw new Error(`port ${port} is already serving another application; leaving it untouched`);

  runCommand(["env-check"], "Behaviour environment check");
  runCommand(["migrate"], "Behaviour database migration");
  if (!fs.existsSync(PYTHON)) throw new Error("Behaviour Python environment is missing; run npm run behaviour:setup first");

  services.set("api", { child: null, attempts: 0, timer: null });
  services.set("worker", { child: null, attempts: 0, timer: null });
  const winnerDatabaseConfigured = Boolean(configuredWinnerDatabaseUrl());
  if (winnerDatabaseConfigured) services.set("winner-source", { child: null, attempts: 0, timer: null });

  writeState();
  startChild("api", ["-m", "uvicorn", "behaviour_detection.api:app", "--host", "127.0.0.1", "--port", String(port)]);
  if (!await waitForReady(port)) {
    await shutdown(1);
    return;
  }
  startChild("worker", ["-m", "behaviour_detection.cli", "worker-run"]);
  if (winnerDatabaseConfigured) {
    startChild("winner-source", ["-m", "behaviour_detection.cli", "winner-sync-run"]);
  } else {
    console.log("Winner Predict database sync is not starting because DATABASE_URL is not configured.");
  }
  console.log(`Behaviour Detection is running at http://127.0.0.1:${port}. Press Ctrl+C to stop.`);
}

function stopRuntime() {
  const state = readState();
  if (!state || !isRuntimePid(Number(state.pid))) {
    console.log("Behaviour Detection runtime is not running.");
    return;
  }
  if (pidAlive(Number(state.supervisor_pid))) {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(STOP_REQUEST_FILE, `${JSON.stringify({
      pid: Number(state.pid), supervisor_pid: Number(state.supervisor_pid),
    })}\n`, { mode: 0o600 });
  }
  process.kill(Number(state.pid), "SIGTERM");
  console.log(`Sent a stop signal to Behaviour Detection runtime PID ${state.pid}.`);
}

async function showStatus() {
  const state = readState();
  const port = Number(envValue("BEHAVIOUR_API_PORT", "8100")) || 8100;
  const health = await healthCheck(port);
  const managed = Boolean(state && isRuntimePid(Number(state.pid)));
  console.log(JSON.stringify({
    managed,
    pid: managed ? state.pid : null,
    api: health?.payload?.service === EXPECTED_SERVICE ? health.payload : null,
    api_port: port,
    children: managed ? state.children : [],
  }, null, 2));
}

function showLogs() {
  let found = false;
  for (const name of ["api", "worker", "collector"]) {
    const file = path.join(LOG_DIR, `${name}.log`);
    if (!fs.existsSync(file)) continue;
    found = true;
    const lines = fs.readFileSync(file, "utf8").trimEnd().split(/\r?\n/).slice(-200);
    console.log(`--- ${name} ---`);
    if (lines.length) console.log(lines.join("\n"));
  }
  if (!found) console.log("No Behaviour Detection logs yet. Start it with npm start.");
}

async function shutdown(exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const entry of services.values()) {
    if (entry.timer) clearTimeout(entry.timer);
    if (entry.child && !entry.child.killed) entry.child.kill("SIGTERM");
  }
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline && [...services.values()].some((entry) => entry.child)) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  for (const entry of services.values()) {
    if (entry.child && entry.child.exitCode === null && entry.child.signalCode === null) {
      entry.child.kill("SIGKILL");
    }
  }
  try {
    const state = readState();
    if (Number(state?.pid) === process.pid) fs.unlinkSync(STATE_FILE);
  } catch {}
  process.exit(exitCode);
}

async function main() {
  const action = process.argv[2];
  if (action === "--stop") return stopRuntime();
  if (action === "--status") return showStatus();
  if (action === "--logs") return showLogs();
  await startRuntime();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
main().catch((error) => {
  process.stderr.write(`[behaviour] ${error.message}\n`);
  if (services.size) shutdown(1);
  else process.exitCode = 1;
});
