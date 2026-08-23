#!/bin/bash

APP_NAME="upstox_order_receiver"
PID_FILE="${APP_NAME}.pid"
LOG_DIR="logs"
LOG_FILE="${LOG_DIR}/app.log"
VENV_DIR="myenv"
REQUIREMENTS_FILE="requirements.txt"

echo "=========================================="
echo "  Upstox Order Request Receiver"
echo "=========================================="
echo
echo "Starting FastAPI application..."
echo

# ---------------------------------------------------------
# Create logs directory
# ---------------------------------------------------------
mkdir -p "$LOG_DIR"


# ---------------------------------------------------------
# Check if application is already running
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# Create virtual environment if it does not exist
# ---------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then

    echo
    echo "Virtual environment not found."
    echo "Creating $VENV_DIR..."

    python3 -m venv "$VENV_DIR"

    if [ $? -ne 0 ]; then
        echo
        echo "ERROR: Failed to create virtual environment."
        exit 1
    fi

    echo "Virtual environment created."

    # -----------------------------------------------------
    # Install requirements only when environment is created
    # -----------------------------------------------------
    if [ -f "$REQUIREMENTS_FILE" ]; then

        echo
        echo "Installing Python packages..."

        "$VENV_DIR/bin/python" -m pip install --upgrade pip

        "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_FILE"

        if [ $? -ne 0 ]; then
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


# ---------------------------------------------------------
# Verify virtual environment Python exists
# ---------------------------------------------------------
PYTHON="$VENV_DIR/bin/python"

if [ ! -f "$PYTHON" ]; then
    echo
    echo "ERROR: Virtual environment Python not found:"
    echo "$PYTHON"
    exit 1
fi


# ---------------------------------------------------------
# Start FastAPI in background
# ---------------------------------------------------------
echo
echo "Starting FastAPI..."

nohup "$PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    >> "$LOG_FILE" 2>&1 &

PID=$!


# ---------------------------------------------------------
# Save PID
# ---------------------------------------------------------
echo "$PID" > "$PID_FILE"


# ---------------------------------------------------------
# Wait and verify application
# ---------------------------------------------------------
sleep 2

if kill -0 "$PID" 2>/dev/null; then

    echo
    echo "Application started successfully."
    echo "PID: $PID"
    echo "Port: 8000"
    echo "Python: $PYTHON"
    echo "Log: $LOG_FILE"

else

    echo
    echo "ERROR: Application failed to start."

    rm -f "$PID_FILE"

    echo
    echo "Check logs:"
    echo "tail -f $LOG_FILE"

    exit 1

fi