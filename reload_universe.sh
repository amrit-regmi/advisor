#!/bin/bash
# Reload universe — run monthly (or manually). NOT part of the daily pipeline.
set -e

LOCK_FILE=/tmp/advisor_universe.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "=== Universe reload already running — exiting ==="
    exit 1
fi

source /home/ubuntu/venv/bin/activate
cd /home/ubuntu/advisor

echo "=== $(date) Reloading universe ==="
python pipeline/universe_loader.py
echo "=== $(date) Universe reload complete ==="
