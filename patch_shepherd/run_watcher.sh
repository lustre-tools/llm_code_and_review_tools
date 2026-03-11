#!/bin/bash
# run_watcher.sh — Main entry point for the patch shepherd daemon.
#
# Invoked by systemd timer (hourly) or manually for testing.
# Runs orchestrator.py (pure code) for the mechanical work;
# Claude is only invoked for JIRA research on unknown failures.

set -euo pipefail

WATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"
REPORT_FILE="/tmp/patch_shepherd_report.json"
LOG_DIR="${HOME}/.patch_shepherd"
LOG_FILE="${LOG_DIR}/watcher.log"
ORCHESTRATOR_OUTPUT="${LOG_DIR}/orchestrator_output.json"

# Per-run log file for tail -f while running
RUN_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="${LOG_DIR}/run_${RUN_TIMESTAMP}.log"
# Symlink for easy access: tail -f ~/.patch_shepherd/current_run.log
CURRENT_RUN_LINK="${LOG_DIR}/current_run.log"

mkdir -p "$LOG_DIR"

# Create the per-run log and point the symlink at it
: > "$RUN_LOG"
ln -sf "$RUN_LOG" "$CURRENT_RUN_LINK"

log() {
	local msg="[$(date -Iseconds)] $*"
	echo "$msg" >> "$LOG_FILE"
	echo "$msg" >> "$RUN_LOG"
}

log "=== Patch watcher run starting (PID $$) ==="

# Verify patches file exists
if [[ ! -f "$PATCHES_FILE" ]]; then
	log "ERROR: Patches file not found: $PATCHES_FILE"
	exit 1
fi

PATCH_COUNT=$(python3 -c "
import json
with open('$PATCHES_FILE') as f:
    data = json.load(f)
print(len(data.get('patches', [])))
" 2>/dev/null || echo "?")
log "Checking $PATCH_COUNT patches from $PATCHES_FILE"

# Update last_checked timestamp
python3 -c "
import json, sys
from datetime import datetime, timezone
with open('$PATCHES_FILE') as f:
    data = json.load(f)
if isinstance(data, dict):
    data['last_checked'] = datetime.now(timezone.utc).isoformat()
    with open('$PATCHES_FILE', 'w') as f:
        json.dump(data, f, indent=4)
        f.write('\n')
"

# Clean up any previous report
rm -f "$REPORT_FILE"

# Clean up stale rate limit files (>1 day old)
find /tmp -name 'patch_shepherd_rates.*' -mtime +1 -delete 2>/dev/null || true

log "Running orchestrator..."

START_SECONDS=$SECONDS

# The orchestrator does the mechanical work in pure code.
# If unknown failures exist, it invokes Claude for JIRA research.
# Stderr has progress logs; stdout has a JSON summary.
PATCHES_FILE="$PATCHES_FILE" \
REPORT_FILE="$REPORT_FILE" \
python3 "$WATCHER_DIR/orchestrator.py" \
	> "$ORCHESTRATOR_OUTPUT" \
	2> >(tee -a "$LOG_FILE" >> "$RUN_LOG") || {
	EXITCODE=$?
	log "ERROR: orchestrator.py exited with status $EXITCODE"
	exit 1
}

ELAPSED=$(( SECONDS - START_SECONDS ))
log "Orchestrator finished in ${ELAPSED}s."

# Read orchestrator summary (JSON on stdout)
ORCH_SUMMARY="$(cat "$ORCHESTRATOR_OUTPUT" 2>/dev/null || echo '{}')"
log "Summary: $ORCH_SUMMARY"

# Check if report was generated
if [[ ! -f "$REPORT_FILE" ]]; then
	log "WARNING: No report file generated at $REPORT_FILE"
	log "Orchestrator output: $ORCH_SUMMARY"
	exit 0
fi

log "Report generated at $REPORT_FILE"

# Inject run metadata into the report before archiving
python3 -c "
import json, sys

with open('$REPORT_FILE') as f:
    report = json.load(f)

orch = json.loads('''$ORCH_SUMMARY''') if '''$ORCH_SUMMARY''' else {}

debug = report.setdefault('debug', {})
debug['duration_seconds'] = $ELAPSED
debug['llm_calls'] = orch.get('llm_calls', 0)

with open('$REPORT_FILE', 'w') as f:
    json.dump(report, f, indent=4)
    f.write('\n')
" 2>/dev/null || log "WARNING: Failed to inject run metadata into report"

# Archive the report with a timestamp so we can review past runs
REPORT_ARCHIVE="${LOG_DIR}/report_${RUN_TIMESTAMP}.json"
cp "$REPORT_FILE" "$REPORT_ARCHIVE"
log "Report archived to $REPORT_ARCHIVE"

# Check if there are any reportable events
HAS_ACTIONS=$(python3 -c "
import json, sys
with open('$REPORT_FILE') as f:
    report = json.load(f)
actions = report.get('actions', [])
print('yes' if actions else 'no')
" 2>/dev/null || echo "no")

if [[ "$HAS_ACTIONS" == "yes" ]]; then
	log "Actions found — sending email report."
	bash "$WATCHER_DIR/send_report.sh" "$REPORT_FILE"
else
	log "No actions — silent run, no email."
fi

log "=== Patch watcher run complete (PID $$) ==="
