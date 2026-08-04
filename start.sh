#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
PY311_VENV="$ROOT_DIR/.venv"

echo "Project: $ROOT_DIR"

if command -v python3.11 >/dev/null 2>&1; then
  PY311_BIN="python3.11"
elif [ -x /usr/local/bin/python3.11 ]; then
  PY311_BIN="/usr/local/bin/python3.11"
else
  echo "ERROR: Python 3.11 is required for the TensorFlow backend."
  echo "Install Python 3.11, then run this script again."
  exit 1
fi

if [ ! -x "$PY311_VENV/bin/python" ]; then
  echo "Creating Python 3.11 TensorFlow virtual environment..."
  "$PY311_BIN" -m venv "$PY311_VENV"
else
  echo "Repairing Python 3.11 TensorFlow virtual environment..."
  "$PY311_BIN" -m venv --upgrade "$PY311_VENV"
fi

echo "Installing full backend ML requirements for Python 3.11..."
"$PY311_VENV/bin/python" -m pip install --upgrade pip
"$PY311_VENV/bin/python" -m pip install -r "$BACKEND_DIR/requirements.txt"

echo "Starting TensorFlow backend on http://localhost:5000 ..."
cd "$BACKEND_DIR"
exec "$PY311_VENV/bin/python" run.py
