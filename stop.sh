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

    # Try graceful shutdown first.
    kill "$PID"

    echo "Graceful stop signal sent to PID: $PID"
    echo "Waiting for process to stop..."

    # Give the application up to 10 seconds to shut down gracefully.
    for i in {1..10}; do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "Process stopped gracefully after ${i} second(s)."
            break
        fi

        sleep 1
    done

    # Double-check whether the process is still active.
    if kill -0 "$PID" 2>/dev/null; then
        echo
        echo "WARNING: Process PID $PID is still active."
        echo "Process did not stop gracefully."
        echo "Still active - force killing PID: $PID using kill -9..."

        kill -9 "$PID"

        # Final verification after kill -9.
        sleep 1

        if kill -0 "$PID" 2>/dev/null; then
            echo "ERROR: Process PID $PID is STILL ACTIVE after kill -9."
            echo "Please check the process manually."
            exit 1
        else
            echo "Process PID $PID successfully killed."
        fi
    fi

    echo
    echo "Application stopped."

else
    echo "Process $PID is no longer running."
fi

rm -f "$PID_FILE"

echo
echo "PID file removed."
echo "Done."