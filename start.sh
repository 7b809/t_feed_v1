#!/bin/bash

APP_NAME="isolation_consumer_service"
PORT=8003


PID_FILE="${APP_NAME}.pid"
LOG_DIR="logs"
VENV_DIR="myenv"
REQUIREMENTS_FILE="requirements.txt"

# --------------------------------------------------
# IP binding feature flag
# --------------------------------------------------
# Set ENABLE_IP_BINDING=true to bind Uvicorn to a specific IP (BIND_IP).
# Set ENABLE_IP_BINDING=false (default) to bind to 0.0.0.0 (all interfaces).
#
# Can be overridden from the shell, e.g.:
#   ENABLE_IP_BINDING=true BIND_IP=192.168.1.50 ./start.sh
ENABLE_IP_BINDING="${ENABLE_IP_BINDING:-false}"
BIND_IP="${BIND_IP:-127.0.0.1}"

echo "=========================================="
echo "  Market Data Foundation"
echo "=========================================="
echo
echo "Starting FastAPI application..."
echo

mkdir -p "$LOG_DIR"

# --------------------------------------------------
# Resolve bind host based on feature flag
# --------------------------------------------------

if [ "$ENABLE_IP_BINDING" = "true" ]; then
    HOST="$BIND_IP"

    # Basic IPv4 sanity check
    if ! [[ "$HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        echo "ERROR: Invalid BIND_IP '$HOST'. Expected IPv4 like 192.168.1.50."
        exit 1
    fi

    # Warn if the IP is not assigned to any local interface
    if command -v ip >/dev/null 2>&1; then
        if ! ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qx "$HOST"; then
            echo "WARNING: $HOST is not configured on any local interface."
            echo "Uvicorn may fail to bind. Continuing anyway..."
            echo
        fi
    fi

    echo "IP binding ENABLED. Binding to $HOST"
else
    HOST="0.0.0.0"
    echo "IP binding DISABLED. Binding to $HOST (all interfaces)"
fi

echo

# --------------------------------------------------
# Check if application is already running
# --------------------------------------------------

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")

    if kill -0 "$PID" 2>/dev/null; then
        echo "Application is already running."
        echo "PID: $PID"
        echo "Port: $PORT"
        exit 0
    else
        echo "Removing stale PID file."
        rm -f "$PID_FILE"
    fi
fi

# --------------------------------------------------
# Create virtual environment if it does not exist
# --------------------------------------------------

if [ ! -d "$VENV_DIR" ]; then
    echo
    echo "Virtual environment not found."
    echo "Creating $VENV_DIR..."

    if ! python3 -m venv "$VENV_DIR"; then
        echo
        echo "ERROR: Failed to create virtual environment."
        exit 1
    fi

    echo "Virtual environment created."
fi

# --------------------------------------------------
# Activate virtual environment
# --------------------------------------------------

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo
    echo "ERROR: Virtual environment activation script not found:"
    echo "$VENV_DIR/bin/activate"
    exit 1
fi

echo
echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

if [ $? -ne 0 ]; then
    echo
    echo "ERROR: Failed to activate virtual environment."
    exit 1
fi

echo "Virtual environment activated."

# --------------------------------------------------
# Verify Python from virtual environment
# --------------------------------------------------

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# Keep the consumer API listener separate from LIVE_FEED_PORT.
if [ -z "${APP_PORT:-}" ] && [ -f ".env" ]; then
    APP_PORT="$("$PYTHON" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("APP_PORT") or "8003")')"
fi
PORT="${APP_PORT:-8003}"

if [ ! -x "$PYTHON" ]; then
    echo
    echo "ERROR: Virtual environment Python not found:"
    echo "$PYTHON"
    exit 1
fi

echo
echo "Python environment:"
echo "Python: $(which python)"
echo "Version: $(python --version)"
echo "Pip: $(which pip)"
echo

# --------------------------------------------------
# Install requirements if requirements.txt exists
# --------------------------------------------------

if [ -f "$REQUIREMENTS_FILE" ]; then
    echo "Checking Python dependencies..."

    if ! "$PYTHON" -m pip install -r "$REQUIREMENTS_FILE"; then
        echo
        echo "ERROR: Failed to install Python packages."
        exit 1
    fi

    echo
    echo "Python dependencies are ready."
else
    echo
    echo "WARNING: $REQUIREMENTS_FILE not found."
    echo "Skipping dependency installation."
fi

# --------------------------------------------------
# Verify required Upstox package
# --------------------------------------------------

echo
echo "Checking required Python packages..."

if ! "$PYTHON" -c "import upstox_client; print('upstox_client: OK')"; then
    echo
    echo "ERROR: upstox_client is not installed in the virtual environment."
    echo
    echo "Install it using:"
    echo "  $PYTHON -m pip install upstox-python-sdk"
    exit 1
fi

echo "upstox_client: OK"

# --------------------------------------------------
# Start FastAPI
# --------------------------------------------------

echo
echo "Starting FastAPI..."
echo "Host: $HOST"
echo "Port: $PORT"
echo "Python: $PYTHON"
echo

# Keep startup errors in a dedicated log instead of /dev/null.
STARTUP_LOG="$LOG_DIR/fastapi_startup.log"

nohup "$PYTHON" -m uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    >"$STARTUP_LOG" 2>&1 &

PID=$!

echo "$PID" > "$PID_FILE"

sleep 2

# --------------------------------------------------
# Verify application process
# --------------------------------------------------

if kill -0 "$PID" 2>/dev/null; then
    echo
    echo "=========================================="
    echo "Application started successfully."
    echo "=========================================="
    echo "PID:    $PID"
    echo "Host:   $HOST"
    echo "Port:   $PORT"
    echo "Python: $PYTHON"
    echo "Venv:   $VENV_DIR"
    echo "Startup log: $STARTUP_LOG"
    echo
else
    echo
    echo "=========================================="
    echo "ERROR: Application failed to start."
    echo "=========================================="
    echo
    echo "Startup log:"
    echo "  $STARTUP_LOG"
    echo
    echo "Last 50 lines:"
    echo "------------------------------------------"

    if [ -f "$STARTUP_LOG" ]; then
        tail -n 50 "$STARTUP_LOG"
    fi

    echo "------------------------------------------"

    rm -f "$PID_FILE"

    exit 1
fi