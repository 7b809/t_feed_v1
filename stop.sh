#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="uvicorn.pid"

echo "=========================================="
echo "       Stopping FastAPI Application"
echo "=========================================="
echo

if [[ ! -f "$PID_FILE" ]]; then
    echo "[INFO] PID file not found."
    echo "[INFO] Application may already be stopped."
    exit 0
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then

    echo "[INFO] Stopping process: $PID"

    kill "$PID"

    sleep 2

    if kill -0 "$PID" 2>/dev/null; then
        echo "[INFO] Process still running. Force stopping..."
        kill -9 "$PID"
    fi

    echo "[OK] Application stopped."

else

    echo "[INFO] Process $PID is not running."

fi

rm -f "$PID_FILE"

echo "[OK] PID file removed."
echo