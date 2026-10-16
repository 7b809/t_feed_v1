#!/bin/bash

# Base directory where your projects live
PROJECTS_DIR="/home/ubuntu/TheProjects/novag7"
OPS_DIR="\$PROJECTS_DIR/ops"

# Load Telegram credentials securely if .env exists
if [ -f "\$OPS_DIR/.env" ]; then
    source "\$OPS_DIR/.env"
fi

# Function to send Telegram notifications securely
send_telegram() {
    local message="\$1"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=\${TELEGRAM_CHAT_ID}" \
            -d "text=\${message}" \
            -d "parse_mode=Markdown" > /dev/null 2>&1
    fi
}

# List your active project folder names here
PROJECTS=(
    "market-engine-v2"
    # Add other projects here if needed, e.g., "livefeed"
)

echo "=========================================="
echo " Starting Trading Projects Startup Routine"
echo " Time: \$(date)"
echo "=========================================="

send_telegram "🚀 *AWS Instance Boot:* Trading startup routine initiated at \$(date)"

for project in "\${PROJECTS[@]}"; do
    PROJECT_PATH="$PROJECTS_DIR/$project"

    if [ -d "\$PROJECT_PATH" ]; then
        echo "------------------------------------------"
        echo "Processing project: \$project"
        echo "Path: \$PROJECT_PATH"
        echo "------------------------------------------"

        send_telegram "🔄 *Project:* \`$project\` startup initiated..."

        cd "\$PROJECTS_DIR" || exit
        cd "\$PROJECT_PATH" || continue

        if [ -f "update_project.py" ]; then
            echo "Running update_project.py for \$project..."
            
            # Capture python execution output and exit code safely
            PYTHON_OUTPUT=\$(python3 update_project.py 2>&1)
            EXIT_CODE=\$?

            if [ \$EXIT_CODE -eq 0 ]; then
                echo "Project \$project started successfully."
                send_telegram "✅ *Project:* \`$project\` started successfully!"
            else
                echo "ERROR: update_project.py failed for $project with exit code$EXIT_CODE"
                send_telegram "❌ *ERROR in Project:* \`$project\` failed to start with exit code \$EXIT_CODE. Check local logs for details."
            fi
        else
            echo "WARNING: update_project.py not found in \$PROJECT_PATH"
            send_telegram "⚠️ *Warning:* \`update_project.py\` not found for \`$project\`."
        fi

        # Safely return back to base directory before next iteration
        cd "\$PROJECTS_DIR" || exit
    else
        echo "WARNING: Directory \$PROJECT_PATH does not exist."
        send_telegram "⚠️ *Warning:* Directory for \`$project\` does not exist on AWS instance."
    fi

    # Small pause between starting projects to prevent resource spikes
    sleep 3
done

echo "=========================================="
echo " All startup routines completed."
echo "=========================================="
send_telegram "🏁 *AWS Instance Boot:* All startup routines completed."