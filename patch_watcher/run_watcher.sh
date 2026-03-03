#!/bin/bash
# run_watcher.sh — Main entry point for the patch watcher daemon.
#
# Invoked by systemd timer (hourly) or manually for testing.
# Runs Claude Code with constrained tools to check watched patches.

set -euo pipefail

WATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"
REPORT_FILE="/tmp/patch_watcher_report.json"
LOG_DIR="${HOME}/.patch_watcher"
LOG_FILE="${LOG_DIR}/watcher.log"
CLAUDE_RAW_OUTPUT="${LOG_DIR}/claude_raw_output.json"

# Per-run log file for tail -f while running
RUN_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="${LOG_DIR}/run_${RUN_TIMESTAMP}.log"
# Symlink for easy access: tail -f ~/.patch_watcher/current_run.log
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

# Generate prompt
PROMPT="$(bash "$WATCHER_DIR/prompt_template.sh")"

# Clean up any previous report
rm -f "$REPORT_FILE"

# Run Claude Code with constrained tools
# --model haiku: cheap and fast
# --allowedTools: ONLY watcher_tool.sh (no Read, no raw Bash)
#   In non-interactive -p mode, tools not in this list are denied
#   (fail-closed: no user to prompt). This is the primary security gate.
# --max-budget-usd: hard cost cap per run
# --no-session-persistence: clean slate each run
# --output-format json: gives us usage stats (input/output tokens, cost)
#
# NOTE: We do NOT use --permission-mode bypassPermissions. That would
# override --allowedTools and grant access to ALL tools. Instead, we
# rely on --allowedTools as a restrictive allowlist; non-listed tools
# are denied in non-interactive mode.
log "Invoking Claude Code (haiku, budget \$2.00)..."

# Unset CLAUDECODE to allow running from within another Claude session
# (e.g., during testing). The systemd timer won't have this set.
unset CLAUDECODE 2>/dev/null || true

# Clean up stale rate limit files (>1 day old)
find /tmp -name 'patch_watcher_rates.*' -mtime +1 -delete 2>/dev/null || true

START_SECONDS=$SECONDS

# Stderr goes to both log files for live monitoring
claude -p \
	--model haiku \
	--allowedTools "Bash(${WATCHER_DIR}/watcher_tool.sh *)" \
	--max-budget-usd 2.00 \
	--output-format json \
	--no-session-persistence \
	"$PROMPT" < /dev/null > "$CLAUDE_RAW_OUTPUT" 2> >(tee -a "$LOG_FILE" >> "$RUN_LOG") || {
	EXITCODE=$?
	log "ERROR: Claude Code exited with status $EXITCODE (PID $$)"
	log "Raw output saved to $CLAUDE_RAW_OUTPUT"
	exit 1
}

ELAPSED=$(( SECONDS - START_SECONDS ))
log "Claude Code finished in ${ELAPSED}s (PID $$)."

# Extract usage stats from the JSON output
# Claude JSON output schema:
#   usage.inputTokens, usage.outputTokens, total_cost_usd, num_turns,
#   duration_ms, duration_api_ms
# Claude outputs JSONL (one JSON object per line). The result line
# has type=result. Parse each line and find it.
USAGE_STATS=$(python3 -c "
import json, sys
try:
    data = None
    with open('$CLAUDE_RAW_OUTPUT') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get('type') == 'result':
                    data = obj
                    break
            except json.JSONDecodeError:
                continue
    stats = {}
    if data:
        # modelUsage has the detailed per-model breakdown
        model_usage = data.get('modelUsage', {})
        for model, mu in model_usage.items():
            stats['input_tokens'] = mu.get('inputTokens', 0)
            stats['output_tokens'] = mu.get('outputTokens', 0)
            stats['cache_read_tokens'] = mu.get('cacheReadInputTokens', 0)
            stats['total_cost_usd'] = mu.get('costUSD', 0)
        stats['num_turns'] = data.get('num_turns', 0)
        stats['duration_api_ms'] = data.get('duration_api_ms', 0)
        stats['duration_ms'] = data.get('duration_ms', 0)
        # Fallback if modelUsage is empty
        if 'total_cost_usd' not in stats:
            stats['total_cost_usd'] = data.get('total_cost_usd', 0)
    print(json.dumps(stats))
except Exception as e:
    print(json.dumps({'error': str(e)}))
" 2>/dev/null || echo '{}')

log "Usage: $USAGE_STATS"

# Check if report was generated
if [[ ! -f "$REPORT_FILE" ]]; then
	log "WARNING: No report file generated at $REPORT_FILE"
	log "Raw output saved to $CLAUDE_RAW_OUTPUT"
	exit 0
fi

log "Report generated at $REPORT_FILE"

# Inject run metadata into the report before archiving
python3 -c "
import json, sys

with open('$REPORT_FILE') as f:
    report = json.load(f)

usage = json.loads('$USAGE_STATS') if '$USAGE_STATS' else {}

debug = report.setdefault('debug', {})
debug['duration_seconds'] = $ELAPSED
debug['input_tokens'] = usage.get('input_tokens', 0)
debug['output_tokens'] = usage.get('output_tokens', 0)
debug['total_cost_usd'] = usage.get('total_cost_usd', 0)
debug['num_turns'] = usage.get('num_turns', 0)

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
