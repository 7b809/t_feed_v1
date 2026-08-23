#!/bin/bash

APP_NAME="upstox_order_receiver"
PID_FILE="${APP_NAME}.pid"

echo "=========================================="
echo "  Upstox Order Request Receiver"
echo "=========================================="
echo
echo "Stopping FastAPI application..."
echo

if [ ! -f "$PID_FILE" ]; then
    echo "Application is not running."
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping process PID: $PID"

    kill "$PID"

    # Give the application a few seconds to shut down.
    for i in {1..10}; do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi

        sleep 1
    done

    # Force kill if still running.
    if kill -0 "$PID" 2>/dev/null; then
        echo "Process did not stop gracefully."
        echo "Force stopping PID: $PID"
        kill -9 "$PID"
    fi

    echo "Application stopped."
else
    echo "Process $PID is no longer running."
fi

rm -f "$PID_FILE"

echo
echo "Done."