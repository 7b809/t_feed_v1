# stop.sh

#!/bin/bash

APP_NAME="ema_v2"
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
    echo "Application is not running (PID file not found: $PID_FILE)."
else
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
    echo "PID file removed."
fi

echo

# ------------------------------------------------------------------
# Precaution Check & Automatic Orphan Cleanup
# ------------------------------------------------------------------
# Detects and automatically terminates any lingering or untracked 
# processes still holding the port so startup never fails with 
# "address already in use".
# ------------------------------------------------------------------
if command -v lsof >/dev/null 2>&1; then
    PORT_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)

    if [ -n "$PORT_PIDS" ]; then
        echo
        echo "WARNING: Port $PORT is still held by untracked PID(s): $PORT_PIDS"
        echo "Automatically terminating orphan process(es)..."
        
        kill -9 $PORT_PIDS 2>/dev/null
        sleep 1

        if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
            echo "ERROR: Failed to free port $PORT. Please check manually."
        else
            echo "Port $PORT has been successfully cleared and is now free."
        fi
    else
        echo "Port $PORT is free."
    fi
elif command -v ss >/dev/null 2>&1; then
    if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q ":$PORT"; then
        echo
        echo "WARNING: Port $PORT appears to still be in use."
        echo "Attempting automatic cleanup using fuser..."
        
        if command -v fuser >/dev/null 2>&1; then
            fuser -k "$PORT"/tcp >/dev/null 2>&1
            sleep 1
            echo "Port cleanup attempted."
        else
            echo "Run: ss -ltnp | grep :$PORT to identify the process."
        fi
    else
        echo "Port $PORT is free."
    fi
else
    echo "Port check skipped (lsof/ss not available)."
fi

echo
echo "Done."