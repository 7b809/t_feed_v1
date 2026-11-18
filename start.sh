#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PID_FILE="sandbox_service.pid"
VENV_DIR="myenv"
REQUIREMENTS_FILE="requirements.txt"

echo "=========================================="
echo "       Upstox Order Service"
echo "=========================================="

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Service is already running (PID $(cat "$PID_FILE"))."
  exit 0
fi
rm -f "$PID_FILE"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating virtual environment: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$REQUIREMENTS_FILE"

mkdir -p data/orders logs logs_text
HOST="$("$PYTHON" -c 'from core.config import API_HOST; print(API_HOST)')"
PORT="$("$PYTHON" -c 'from core.config import API_PORT; print(API_PORT)')"

echo "Binding: $HOST:$PORT"
echo "Trading enabled: $("$PYTHON" -c 'from core.config import TRADING_ENABLED; print(TRADING_ENABLED)')"
echo "Upstox sandbox: $("$PYTHON" -c 'from core.config import UPSTOX_SANDBOX; print(UPSTOX_SANDBOX)')"

nohup "$PYTHON" -m uvicorn main:app \
  --host "$HOST" \
  --port "$PORT" \
  > logs/uvicorn_startup.log 2>&1 &

PID=$!
echo "$PID" > "$PID_FILE"
sleep 2

if kill -0 "$PID" 2>/dev/null; then
  echo "Started successfully. PID=$PID PORT=$PORT"
  echo "Startup log: logs/uvicorn_startup.log"
else
  echo "Startup failed. Last startup log lines:"
  tail -n 60 logs/uvicorn_startup.log || true
  rm -f "$PID_FILE"
  exit 1
fi
