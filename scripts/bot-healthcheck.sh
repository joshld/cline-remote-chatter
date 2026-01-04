#!/bin/bash
# Health check script for Cline Telegram Bot

# Configuration - Update these paths for your environment:
SERVICE_NAME="cline-telegram-bot"
WORKING_DIR="/path/to/your/cutesy-agent-router"  # Update this path
LOG_FILE="$WORKING_DIR/bot-health.log"

# Check if service is running
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "$(date): Service $SERVICE_NAME is not running" >> "$LOG_FILE"
    exit 1
fi

# Check if Python process exists
if ! pgrep -f "cline_telegram_bot.py" > /dev/null; then
    echo "$(date): Python process not found" >> "$LOG_FILE"
    exit 1
fi

# Check log file for recent activity (last 5 minutes)
if [ -f "$WORKING_DIR/bot.log" ]; then
    if [ $(find "$WORKING_DIR/bot.log" -mmin -5) ]; then
        echo "$(date): Service appears healthy" >> "$LOG_FILE"
        exit 0
    else
        echo "$(date): Log file not updated recently" >> "$LOG_FILE"
        exit 1
    fi
else
    echo "$(date): Log file not found" >> "$LOG_FILE"
    exit 1
fi