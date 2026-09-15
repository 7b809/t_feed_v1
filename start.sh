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

    if [ -f "$REQUIREMENTS_FILE" ]; then
        echo
        echo "Installing Python packages..."

        if ! "$VENV_DIR/bin/python" -m pip install --upgrade pip; then
            echo
            echo "ERROR: Failed to upgrade pip."
            exit 1
        fi

        if ! "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"; then
            echo
            echo "ERROR: Failed to install Python packages."
            exit 1
        fi

        echo
        echo "Python packages installed successfully."
    else
        echo
        echo "WARNING: $REQUIREMENTS_FILE not found."
    fi
else
    echo
    echo "Virtual environment already exists."
    echo "Skipping environment creation and package installation."
fi

PYTHON="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo
    echo "ERROR: Virtual environment Python not found or not executable:"
    echo "$PYTHON"
    exit 1
fi

echo
echo "Starting FastAPI..."
echo "Host: 0.0.0.0"
echo "Port: $PORT"
echo

nohup "$PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    >/dev/null 2>&1 &

PID=$!

echo "$PID" > "$PID_FILE"

sleep 2

if kill -0 "$PID" 2>/dev/null; then
    echo
    echo "=========================================="
    echo "Application started successfully."
    echo "=========================================="
    echo "PID:    $PID"
    echo "Port:   $PORT"
    echo "Python: $PYTHON"
    echo "Logs:   Managed by application logging"
    echo
else
    echo
    echo "ERROR: Application failed to start."

    rm -f "$PID_FILE"

    echo
    echo "Check the application log files in: $LOG_DIR"
    exit 1
fi