#!/usr/bin/env node
/**
 * Bot Controller API — Unified Orchestration Server
 *
 * Endpoints:
 *   POST /bot/start   – Start the Python bot (if not already running)
 *   POST /bot/stop    – Stop the running Python bot safely
 *   GET  /bot/status  – Return current bot state
 *
 * Safety:
 *   - Global botRunning flag prevents concurrent Node-side execution
 *   - bot.lock file prevents concurrent Python-side execution
 *   - Handles crash recovery (lock file cleanup on unexpected exit)
 */

const express = require("express");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

const app = express();
// Honor forwarded HTTPS only from a local reverse proxy that terminates TLS.
app.set("trust proxy", "loopback");
const PORT = process.env.PORT || 3000;

// ── Configuration ───────────────────────────────────────────────────────────
const LOCK_FILE = path.resolve(__dirname, "bot.lock");

// ── Global State ────────────────────────────────────────────────────────────
let botRunning = false;
let botProcess = null; // ChildProcess reference for kill

// Synchronise state with lock file on startup (cleanup stale locks)
if (fs.existsSync(LOCK_FILE)) {
  console.log("[CONTROLLER] Stale bot.lock detected — removing.");
  fs.unlinkSync(LOCK_FILE);
}

// ── Middleware ───────────────────────────────────────────────────────────────
app.use(express.json({ limit: "64kb" }));

// This legacy controller is not started by npm start, but keep its bot
// controls private if it is launched directly.
app.use("/bot", (req, res, next) => {
  const remote = String(req.socket.remoteAddress || "").toLowerCase();
  const host = String(req.hostname || "").toLowerCase();
  const loopbackPeer = remote === "127.0.0.1" || remote === "::1" || remote.endsWith(":ffff:127.0.0.1");
  const loopbackHost = host === "127.0.0.1" || host === "::1" || host === "localhost";
  const local = loopbackPeer && loopbackHost;
  if (!req.secure && !local) {
    return res.status(426).json({ ok: false, error: "https_required_for_external_access" });
  }

  const apiKey = process.env.API_KEY;
  if (apiKey && apiKey.length < 16) {
    return res.status(503).json({ ok: false, error: "api_key_configuration_invalid" });
  }
  if (!apiKey) {
    if (!local) {
      return res.status(503).json({ ok: false, error: "api_key_not_configured" });
    }
    return next();
  }
  const authorization = String(req.headers.authorization || "");
  const expected = `Bearer ${apiKey}`;
  const suppliedBuffer = Buffer.from(authorization);
  const expectedBuffer = Buffer.from(expected);
  if (suppliedBuffer.length !== expectedBuffer.length
      || !crypto.timingSafeEqual(suppliedBuffer, expectedBuffer)) {
    return res.status(401)
      .set("WWW-Authenticate", "Bearer")
      .json({ ok: false, error: "authentication_required" });
  }
  next();
});

app.use(express.static(path.join(__dirname, "public")));

// ── Helpers ─────────────────────────────────────────────────────────────────

function removeLock() {
  try {
    if (fs.existsSync(LOCK_FILE)) {
      fs.unlinkSync(LOCK_FILE);
      console.log("[CONTROLLER] bot.lock removed.");
    }
  } catch (err) {
    console.error("[CONTROLLER] Failed to remove lock file:", err.message);
  }
}

function createLock() {
  try {
    fs.writeFileSync(LOCK_FILE, String(process.pid), "utf-8");
    console.log("[CONTROLLER] bot.lock created (PID %d).", process.pid);
  } catch (err) {
    console.error("[CONTROLLER] Failed to create lock file:", err.message);
  }
}

// ── API: GET /bot/status ────────────────────────────────────────────────────

app.get("/bot/status", (_req, res) => {
  res.json({
    running: botRunning,
    lockExists: fs.existsSync(LOCK_FILE),
  });
});

// ── API: POST /bot/start ────────────────────────────────────────────────────

app.post("/bot/start", (_req, res) => {
  // ── Conflict check ──────────────────────────────────────────────────────
  if (botRunning) {
    return res.status(409).json({
      error: "Bot is already running",
      detail: "A bot instance is currently active in this process.",
    });
  }

  if (fs.existsSync(LOCK_FILE)) {
    return res.status(409).json({
      error: "Bot is already running",
      detail: "Lock file bot.lock exists — another instance may be active.",
    });
  }

  // ── Mark running & create lock ──────────────────────────────────────────
  botRunning = true;
  createLock();

  const botScript = path.join(__dirname, "bot.py");
  console.log("[CONTROLLER] Spawning python3 %s", botScript);

  // ── Spawn Python bot as a child process ─────────────────────────────────
  botProcess = spawn("python3", [botScript], {
    cwd: __dirname,
    stdio: ["pipe", "pipe", "pipe"],
  });

  // Stream stdout logs from Python bot → Node console (real-time)
  botProcess.stdout.on("data", (chunk) => {
    const lines = chunk.toString().trim().split("\n");
    for (const line of lines) {
      if (!line) continue;
      console.log(`[BOT:stdout] ${line}`);
    }
  });

  // Stream stderr logs from Python bot → Node console
  botProcess.stderr.on("data", (chunk) => {
    const lines = chunk.toString().trim().split("\n");
    for (const line of lines) {
      if (!line) continue;
      console.error(`[BOT:stderr] ${line}`);
    }
  });

  // ── Handle process exit (cleanup) ───────────────────────────────────────
  const cleanup = (code, signal) => {
    console.log(
      "[CONTROLLER] Bot process exited (code=%s, signal=%s).",
      code !== null ? code : "null",
      signal || "none"
    );
    botProcess = null;
    botRunning = false;
    removeLock();

    // If the client requested the final log chunk, send it via status
  };

  botProcess.on("exit", cleanup);
  botProcess.on("close", cleanup);
  botProcess.on("error", (err) => {
    console.error("[CONTROLLER] Failed to start bot process:", err.message);
    botProcess = null;
    botRunning = false;
    removeLock();
  });

  // ── Respond immediately ─────────────────────────────────────────────────
  res.json({
    status: "started",
    message: "Bot process spawned successfully. Streaming logs to console.",
  });
});

// ── API: POST /bot/stop ────────────────────────────────────────────────────

app.post("/bot/stop", (_req, res) => {
  if (!botRunning || !botProcess) {
    // Not running – clean up any stale state
    botRunning = false;
    removeLock();
    return res.json({
      status: "not_running",
      message: "No bot process was running.",
    });
  }

  // ── Kill the Python bot process safely ──────────────────────────────────
  const pid = botProcess.pid;
  console.log("[CONTROLLER] Stopping bot process (PID %d)...", pid);

  // Graceful: SIGTERM first, give it a moment, then SIGKILL if needed
  botProcess.kill("SIGTERM");

  // Force kill after 3 seconds if still alive
  const forceKillTimer = setTimeout(() => {
    if (botProcess && !botProcess.killed) {
      console.warn("[CONTROLLER] Bot did not exit on SIGTERM — sending SIGKILL.");
      botProcess.kill("SIGKILL");
    }
  }, 3000);

  botProcess.on("exit", () => {
    clearTimeout(forceKillTimer);
  });

  // Clean up state
  botProcess = null;
  botRunning = false;
  removeLock();

  res.json({
    status: "stopped",
    message: `Bot process (PID ${pid}) terminated.`,
  });
});

// ── Start Server ────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log("[CONTROLLER] Bot Controller API running on http://localhost:%d", PORT);
  console.log("[CONTROLLER] Endpoints:");
  console.log("  POST /bot/start");
  console.log("  POST /bot/stop");
  console.log("  GET  /bot/status");
});
