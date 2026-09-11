"""
run.py — Production entry point
================================
Starts Flask + SocketIO together so WebSocket and HTTP share the same port.

Usage:
    cd backend && python run.py

Features enabled:
  - HTTP REST API on port 5000
  - WebSocket events on same port (via flask-socketio)
  - Real-time sync pipeline (round → predict → backfill → emit)
  - Model pre-warm in background thread
"""

import logging
import sys
import importlib.util
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from utils import setup_logging, ensure_data_files

setup_logging()
ensure_data_files()

log = logging.getLogger("run")

# ── Import Flask app ──────────────────────────────────────────────────────
flask_app_path = BACKEND_DIR / "app.py"
flask_app_spec = importlib.util.spec_from_file_location("winner_predict_flask_app", flask_app_path)
if flask_app_spec is None or flask_app_spec.loader is None:
    raise RuntimeError(f"Unable to load Flask app from {flask_app_path}")

flask_app_module = importlib.util.module_from_spec(flask_app_spec)
sys.modules[flask_app_spec.name] = flask_app_module
flask_app_spec.loader.exec_module(flask_app_module)

app = flask_app_module.app
start_model_prewarm = flask_app_module.start_model_prewarm

start_model_prewarm()
log.info("Predictor pre-warm started in background")

# ── Setup SocketIO ────────────────────────────────────────────────────────
try:
    from flask_socketio import SocketIO

    sio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        logger=False,
        engineio_logger=False,
    )

    import ws_server
    ws_server._sio = sio
    ws_server.register_sync_handlers(sio)
    log.info("WebSocket server ready")

    WS_AVAILABLE = True
except ImportError:
    log.warning("flask-socketio not installed — WebSocket disabled. "
                "Install with: pip install flask-socketio")
    WS_AVAILABLE = False

# ── Run ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if WS_AVAILABLE:
        log.info("Starting Flask+SocketIO on http://0.0.0.0:5000")
        sio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
    else:
        log.info("Starting Flask (no WebSocket) on http://0.0.0.0:5000")
        app.run(host="0.0.0.0", port=5000, debug=False)
