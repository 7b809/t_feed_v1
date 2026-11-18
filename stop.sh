#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PID_FILE="sandbox_service.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "PID file not found; sandbox_v1 may not be running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stop signal sent to sandbox_v1 (PID=$PID)."
else
  echo "Stale PID file found; removing it."
fi
rm -f "$PID_FILE"
