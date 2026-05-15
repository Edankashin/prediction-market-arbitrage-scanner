#!/usr/bin/env bash
# Launch the snapshot loop in the background using the project venv.
# Output is appended to logs/snapshot_loop.log; PID written to logs/snapshot_loop.pid.
# Run from the project root: bash scripts/start_loop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_ROOT/../.venv/bin/python"
LOG_FILE="$PROJECT_ROOT/logs/snapshot_loop.log"
PID_FILE="$PROJECT_ROOT/logs/snapshot_loop.pid"

if [[ -f "$PID_FILE" ]]; then
    existing_pid="$(cat "$PID_FILE")"
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo "Loop already running (PID $existing_pid). Aborting." >&2
        exit 1
    fi
    echo "Stale PID file found (PID $existing_pid not running). Removing."
    rm -f "$PID_FILE"
fi

mkdir -p "$PROJECT_ROOT/logs"

nohup caffeinate -i "$VENV_PYTHON" -m app.run loop \
    --interval 60 \
    --top 50 \
    --by volume_24h \
    --discover-every 900 \
    >> "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "Loop started (PID $(cat "$PID_FILE")). Logging to $LOG_FILE"
