#!/bin/bash
# =============================================================================
# deploy.sh — called by the webhook service when GitHub pushes to main
# Location on server: /opt/webhook/deploy.sh
# =============================================================================
set -e

REPO_PATH="/home/ubuntu/algo-semi-hft-flattrade"
BRANCH="main"
LOG_FILE="/var/log/deploy.log"

echo "---" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy triggered" >> "$LOG_FILE"

cd "$REPO_PATH"

# Make sure we are on main
git fetch origin "$BRANCH" >> "$LOG_FILE" 2>&1
git reset --hard "origin/$BRANCH" >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Pulled successfully ($(git rev-parse --short HEAD))" >> "$LOG_FILE"

# Optional: restart the trader systemd service after deploy
# Uncomment if you set up trader.service:
# systemctl restart trader >> "$LOG_FILE" 2>&1
