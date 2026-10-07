#!/bin/bash

APP_NAME="upstox_order_receiver"
PORT=8010

PID_FILE="${APP_NAME}.pid"
LOG_DIR="logs"
VENV_DIR="myenv"
REQUIREMENTS_FILE="requirements.txt"

echo "=========================================="
echo "  Upstox Order Request Receiver"
echo "=========================================="
echo
echo "Starting FastAPI application..."
echo

mkdir -p "$LOG_DIR"

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
echo "Host: 0.0.0.0"
echo "Port: $PORT"
echo "Python: $PYTHON"
echo

# Keep startup errors in a dedicated log instead of /dev/null.
STARTUP_LOG="$LOG_DIR/fastapi_startup.log"

nohup "$PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 \
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
