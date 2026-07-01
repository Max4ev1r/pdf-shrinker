#!/bin/bash
LOG_DIR="$HOME/.hermes/logs"
KEEP_DAYS=7

for log in "$LOG_DIR"/*.log; do
    [ -f "$log" ] || continue
    cp "$log" "${log}.$(date +%Y%m%d)"
    : > "$log"
done

find "$LOG_DIR" -name "*.log.*" -mtime +$KEEP_DAYS -delete
