#!/bin/bash

APP_NAME="upstox_order_receiver"
PID_FILE="${APP_NAME}.pid"
LOG_DIR="logs"
LOG_FILE="${LOG_DIR}/app.log"

echo "=========================================="
echo "  Upstox Order Request Receiver"
echo "=========================================="
echo
echo "Starting FastAPI application..."
echo

# Create logs directory if it does not exist.
mkdir -p "$LOG_DIR"

# Check if application is already running.
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")

    if kill -0 "$PID" 2>/dev/null; then
        echo "Application is already running."
        echo "PID: $PID"
        exit 0
    else
        echo "Removing stale PID file."
        rm -f "$PID_FILE"
    fi
fi

# Start FastAPI in background.
nohup python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    >> "$LOG_FILE" 2>&1 &

PID=$!

# Save PID.
echo "$PID" > "$PID_FILE"

sleep 2

# Verify process is still running.
if kill -0 "$PID" 2>/dev/null; then
    echo
    echo "Application started successfully."
    echo "PID: $PID"
    echo "Port: 8000"
    echo "Log: $LOG_FILE"
else
    echo
    echo "ERROR: Application failed to start."
    rm -f "$PID_FILE"
    exit 1
fi