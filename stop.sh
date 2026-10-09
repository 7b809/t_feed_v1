#!/bin/bash

APP_NAME="upstox_market_stream"
PORT=8002

PID_FILE="${APP_NAME}.pid"

echo "=========================================="
echo "  Upstox Market Stream Gateway"
echo "=========================================="
echo
echo "Stopping FastAPI application..."
echo "Port: $PORT"
echo

if [ ! -f "$PID_FILE" ]; then
    echo "Application is not running."
    echo "PID file not found: $PID_FILE"
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

# ------------------------------------------------------------------
# Safety check: is anything still holding the port?
# ------------------------------------------------------------------
# The PID file only knows about the process start.sh launched. If an
# orphan Uvicorn (or a manually-started instance) is still bound to
# the port, the next start.sh run will fail with "address already in
# use". Surface that here so it is not a surprise later.
# ------------------------------------------------------------------
if command -v lsof >/dev/null 2>&1; then
    PORT_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)

    if [ -n "$PORT_PIDS" ]; then
        echo
        echo "WARNING: Port $PORT is still in use by PID(s): $PORT_PIDS"
        echo "These processes are not tracked by $PID_FILE."
        echo "You may need to stop them manually, for example:"
        echo "  kill -9 $PORT_PIDS"
    else
        echo "Port $PORT is free."
    fi
elif command -v ss >/dev/null 2>&1; then
    if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q ":$PORT"; then
        echo
        echo "WARNING: Port $PORT appears to still be in use."
        echo "Run: ss -ltnp | grep :$PORT   to identify the process."
    else
        echo "Port $PORT is free."
    fi
else
    echo "Port check skipped (lsof/ss not available)."
fi

echo
echo "Done."