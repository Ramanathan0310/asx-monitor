#!/bin/bash
# Wrapper invoked by launchd. Logs to logs/<date>.log so each run is preserved.
set -e

DIR="/Users/anirudh/Claude/asx-monitor"
cd "$DIR"

mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"

echo "=== Run started: $(date) ===" >> "$LOG"
"$DIR/.venv/bin/python" "$DIR/monitor.py" >> "$LOG" 2>&1
echo "=== Run finished: $(date) ===" >> "$LOG"
echo "" >> "$LOG"
