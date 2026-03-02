#!/bin/bash
# prompt_template.sh — Generate the prompt for the patch watcher daemon.
#
# Reads patches_to_watch.json and outputs the prompt to stdout.

set -euo pipefail

PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"
WATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"

PATCHES_JSON="$(cat "$PATCHES_FILE")"

cat <<EOF
You are the patch watcher daemon. Check all watched patches now.

Your tool is: ${WATCHER_DIR}/watcher_tool.sh
Call it via Bash like: ${WATCHER_DIR}/watcher_tool.sh <action> [args...]

Current watched patches state:
${PATCHES_JSON}

Use check-patch for each patch. It does the heavy lifting (status,
reviews, CI, auto-links bugs, auto-retests). You only need to handle
what it returns in needs_llm_decision (assess relatedness).

For each patch at index N, call:
  ${WATCHER_DIR}/watcher_tool.sh check-patch <gerrit_url> <N> <watch_status> <last_patchset> <last_review_count>

Use "active" for watch_status and "0" for last_patchset/last_review_count
if the field is missing from the patch data.

After ALL patches, collect the results into the report JSON and write it:
  echo '<json>' | ${WATCHER_DIR}/watcher_tool.sh write-report /tmp/patch_watcher_report.json

Be concise. Focus on actions. Do not explain your reasoning at length.
EOF
