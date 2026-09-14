#!/usr/bin/env bash

set -euo pipefail

# ============================================================
# Configuration
# ============================================================

HOST="0.0.0.0"
PORT="8080"

VENV_DIR=".venv"
PID_FILE="uvicorn.pid"
LOG_FILE="uvicorn.log"

# ============================================================
# Move to project directory
# ============================================================

cd "$(dirname "$0")"

echo "=========================================="
echo "       FastAPI Application"
echo "=========================================="
echo

# ============================================================
# Check existing process
# ============================================================

if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"

    if kill -0 "$PID" 2>/dev/null; then
        echo "[ERROR] Application is already running."
        echo "[INFO] PID  : $PID"
        echo "[INFO] Port : $PORT"
        echo
        exit 1
    else
        echo "[INFO] Removing stale PID file..."
        rm -f "$PID_FILE"
    fi
fi

# ============================================================
# Check / create virtual environment
# ============================================================

if [[ -f "$VENV_DIR/bin/activate" ]]; then

    echo "[OK] Existing virtual environment found."
    echo "[INFO] Assuming required packages are already installed."
    echo

else

    echo "[INFO] Virtual environment not found."
    echo "[INFO] Creating $VENV_DIR..."
    echo

    python3 -m venv "$VENV_DIR"

    echo
    echo "[OK] Virtual environment created."
    echo

    # --------------------------------------------------------
    # Activate newly created environment
    # --------------------------------------------------------

    source "$VENV_DIR/bin/activate"

    echo "[INFO] Installing required packages..."
    echo

    python -m pip install --upgrade pip

    if [[ -f "requirements.txt" ]]; then

        python -m pip install -r requirements.txt

        echo
        echo "[OK] Required packages installed."

    else

        echo
        echo "[WARNING] requirements.txt not found."
        echo "[WARNING] No packages installed."

    fi

    echo

fi

# ============================================================
# Activate virtual environment
# ============================================================

source "$VENV_DIR/bin/activate"

echo "[OK] Virtual environment activated."
echo

# ============================================================
# Start FastAPI application
# ============================================================

echo "=========================================="
echo "       Starting FastAPI Application"
echo "=========================================="
echo
echo "HOST : $HOST"
echo "PORT : $PORT"
echo "PID  : $PID_FILE"
echo "LOG  : $LOG_FILE"
echo

# ============================================================
# Run Uvicorn in background
# ============================================================

nohup python -m uvicorn main:app \
    --host "$HOST" \
    --port "$PORT" \
    > "$LOG_FILE" 2>&1 &

PID=$!

# ============================================================
# Save PID
# ============================================================

echo "$PID" > "$PID_FILE"

echo "[OK] Application started."
echo
echo "PID: $PID"
echo "URL: http://$HOST:$PORT"
echo "Log: $LOG_FILE"
echo

echo "=========================================="
echo "       Application Running"
echo "=========================================="