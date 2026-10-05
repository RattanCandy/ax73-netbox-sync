#!/usr/bin/env bash
# Collect AX73 clients and reconcile with NetBox under an exclusive lock.
# Dry-run is always the default; AX73_APPLY=1 opts in to live reconciliation.
set -euo pipefail

BASE="${AX73_BASE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
LOCK="${AX73_LOCK_FILE:-/tmp/ax73-cycle.lock}"
PYTHON="${AX73_PYTHON:-$BASE/.venv/bin/python}"

if [[ "${1:-}" != "--locked" ]]; then
    exec /usr/bin/flock -n "$LOCK" /usr/bin/bash "$0" --locked
fi

echo "===== AX73 discovery started: $(date --iso-8601=seconds) ====="
"$PYTHON" "$BASE/ax73_collect_clients.py"

if [[ "${AX73_APPLY:-0}" == "1" ]]; then
    echo "===== NetBox reconciliation: APPLY mode ====="
    "$PYTHON" "$BASE/ax73_sync.py" --apply
else
    echo "===== NetBox reconciliation: DRY-RUN mode ====="
    "$PYTHON" "$BASE/ax73_sync.py" --dry-run
fi

echo "===== AX73 discovery complete ====="
