#!/usr/bin/env bash
# Send SIGTERM to the snapshot loop and wait up to 10 seconds for clean shutdown.
# Run from the project root: bash scripts/stop_loop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_ROOT/logs/snapshot_loop.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found at $PID_FILE — loop may not be running." >&2
    exit 1
fi

pid="$(cat "$PID_FILE")"

if ! kill -0 "$pid" 2>/dev/null; then
    echo "Process $pid is not running. Removing stale PID file."
    rm -f "$PID_FILE"
    exit 0
fi

echo "Sending SIGTERM to PID $pid..."
kill -TERM "$pid"

for i in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Loop stopped cleanly after ${i}s."
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

echo "Process $pid did not stop within 10s. It may still be shutting down." >&2
echo "Check: kill -0 $pid"
exit 1
