#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if command -v node >/dev/null 2>&1; then
  NODE_BIN="$(command -v node)"
else
  NODE_BIN="$(find "${NVM_DIR:-$HOME/.nvm}/versions/node" -path '*/bin/node' -type f 2>/dev/null | sort -V | tail -n 1)"
fi

if [ -z "${NODE_BIN:-}" ] || [ ! -x "$NODE_BIN" ]; then
  echo "ERROR: Node.js is required to run the supervised Winner Predict stack." >&2
  exit 1
fi

echo "Starting the supervised Winner Predict stack from $PROJECT_DIR"
exec "$NODE_BIN" scripts/start-all.js
