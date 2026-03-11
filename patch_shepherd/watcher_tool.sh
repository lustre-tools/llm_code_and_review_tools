#!/bin/bash
# watcher_tool.sh — Guardrail wrapper for the patch shepherd daemon.
#
# This is the ONLY tool the watcher Claude instance can call.
# It validates the action against an allowlist before executing.

set -euo pipefail

DRY_RUN="${PATCH_SHEPHERD_DRY_RUN:-}"

PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"

# --- Rate limiting for write actions ---
# Counter file in /tmp. PPID is the claude process that invoked us;
# stable across all tool calls within a single run.
RATE_FILE="/tmp/patch_shepherd_rates.${PPID}"
# Caps per run
MAX_RETESTS=15
MAX_RAISE_BUGS=5
MAX_LINK_BUGS=20

rate_check() {
	local action="$1"
	local max="$2"
	local count
	count=$(grep -c "^${action}$" "$RATE_FILE" 2>/dev/null || echo 0)
	if (( count >= max )); then
		die "Rate limit: ${action} called ${count} times (max ${max} per run)"
	fi
	echo "$action" >> "$RATE_FILE"
}

usage() {
	cat <<'EOF'
Usage: watcher_tool.sh <action> [args...]

Allowed actions:
  check-patch <url> [idx] [status] [ps] [rc]
                                      Full patch check (preferred)
  check-status <gerrit_url>           Check Gerrit change status
  check-ci <gerrit_url>               Check CI results (gerrit maloo)
  check-reviews <gerrit_url>          Get human review comments
  search-bug "<test_name>"            Search JIRA for known bugs
  check-linked-bugs <test_set_id>     Check bugs linked to a test set
  link-bug <test_set_id> <TICKET>     Link a JIRA bug to a test set
  raise-bug <test_set_id> [opts]      Raise a new bug via Maloo
  retest <session_id> <TICKET>        Request a retest
  get-failures <session_id>           Get failure details
  update-patch <idx> <field> <value>  Update patches_to_watch.json
  write-report <json_file>            Write the report JSON
EOF
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

ACTION="${1:-}"
shift || true

case "$ACTION" in
check-patch)
	# Meta-action: does the full check for one patch in a single call.
	# Runs status, reviews, CI checks, and auto-handles the mechanical
	# parts (link known bugs, request retests). Only returns unknown
	# failures for the LLM to assess relatedness.
	[[ $# -ge 1 ]] || die "check-patch requires <gerrit_url>"
	GERRIT_URL="$1"
	PATCH_INDEX="${2:-}"
	WATCH_STATUS="${3:-active}"
	LAST_PATCHSET="${4:-0}"
	LAST_REVIEW_COUNT="${5:-0}"

	python3 - "$GERRIT_URL" "$PATCH_INDEX" "$WATCH_STATUS" \
		"$LAST_PATCHSET" "$LAST_REVIEW_COUNT" <<'PYEOF'
import json, subprocess, sys, re, time

def dbg(msg):
    """Debug log to stderr (stdout is the JSON result)."""
    print(f"    check-patch: {msg}", file=sys.stderr)

def run_tool(args):
    """Run a CLI tool, return parsed JSON or raw text."""
    label = " ".join(args[:3])
    t0 = time.monotonic()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
        elapsed = time.monotonic() - t0
        dbg(f"{label} → {elapsed:.1f}s (rc={r.returncode})")
        out = r.stdout.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out}
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        dbg(f"{label} → TIMEOUT after {elapsed:.1f}s")
        return {"error": f"{label} timed out ({elapsed:.0f}s)"}
    except Exception as e:
        elapsed = time.monotonic() - t0
        dbg(f"{label} → ERROR after {elapsed:.1f}s: {e}")
        return {"error": str(e)}

url = sys.argv[1]
patch_index = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
watch_status = sys.argv[3] if len(sys.argv) > 3 else "active"
last_patchset = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "0" else 0
last_review_count = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] != "0" else 0

result = {
    "gerrit_url": url,
    "patch_index": patch_index,
    "skipped": False,
    "skip_reason": None,
    "status": None,
    "current_patchset": None,
    "review_count": 0,
    "new_reviews": False,
    "needs_human_review": False,
    "negative_cr": False,
    "review_summary": None,
    "ci_summary": None,
    "actions_taken": [],
    "needs_llm_decision": [],
    "errors": [],
}

# Step 0: Check Gerrit status
info = run_tool(["gerrit", "info", url])
if not info or not info.get("ok"):
    result["errors"].append(f"gerrit info failed: {info}")
    print(json.dumps(result))
    sys.exit(0)

data = info["data"]
result["status"] = data["status"]
result["current_patchset"] = data["current_patchset"]

# Merged or abandoned — done
if data["status"] == "MERGED":
    result["actions_taken"].append({
        "type": "merged",
        "description": f"Patch merged (patchset {data['current_patchset']})"
    })
    print(json.dumps(result))
    sys.exit(0)

if data["status"] == "ABANDONED":
    result["actions_taken"].append({
        "type": "abandoned",
        "description": "Patch abandoned"
    })
    print(json.dumps(result))
    sys.exit(0)

# Step 1: Check watch_status
if watch_status == "stopped":
    result["skipped"] = True
    result["skip_reason"] = "watch_status is stopped"
    print(json.dumps(result))
    sys.exit(0)

if watch_status == "merged":
    result["skipped"] = True
    result["skip_reason"] = "watch_status is merged"
    print(json.dumps(result))
    sys.exit(0)

if watch_status == "needs_review":
    if data["current_patchset"] != last_patchset:
        # New patchset uploaded — clear needs_review
        watch_status = "active"
    else:
        result["skipped"] = True
        result["skip_reason"] = "needs_review (no new patchset)"
        print(json.dumps(result))
        sys.exit(0)

# Step 2: Check human reviews
comments = run_tool(["gerrit", "comments", url])
if comments and comments.get("ok"):
    cdata = comments["data"]
    # Filter to human review messages on current patchset
    BOT_USERNAMES = {
        "smatchreview", "hpdd-checkpatch", "lgerritjanitor",
        "do-not-reply", "hpdd-test-coordinator", "maloo",
        "lustre-gerrit", "jenkins", "mdt-test-coordinator",
    }
    human_msgs = []
    for msg in cdata.get("review_messages", []):
        author = msg.get("author", {})
        username = author.get("username", "")
        if username in BOT_USERNAMES:
            continue
        if msg.get("patch_set") != data["current_patchset"]:
            continue
        human_msgs.append(msg)

    result["review_count"] = len(human_msgs)
    current_count = len(human_msgs)

    if current_count > last_review_count:
        result["new_reviews"] = True
        # Check if any new review is substantive (not just bare +1/+2)
        for msg in human_msgs[last_review_count:]:
            message = msg.get("message", "")
            # Check for inline comments (threads in the comments data)
            has_inline = len(cdata.get("threads", [])) > 0
            # Check for negative review
            is_negative = bool(re.search(r'Code-Review[-]', message))
            # Check for substantive message (more than just score line)
            lines = [l.strip() for l in message.split('\n')
                     if l.strip()
                     and not l.strip().startswith('Patch Set')]
            is_substantive = len(lines) > 0

            if has_inline or is_negative or is_substantive:
                reviewer = msg.get("author", {}).get("name", "unknown")
                result["needs_human_review"] = True
                result["negative_cr"] = is_negative
                result["review_summary"] = (
                    f"Review from {reviewer}: {message[:200]}"
                )
                result["actions_taken"].append({
                    "type": "needs_review",
                    "description": f"New review from {reviewer}"
                })
                break

    if result["needs_human_review"]:
        # Stop processing — human review takes priority
        print(json.dumps(result))
        sys.exit(0)

# Step 3: Check CI status
maloo = run_tool(["gerrit", "maloo", url])
if not maloo or not maloo.get("ok"):
    result["errors"].append(f"gerrit maloo failed: {maloo}")
    print(json.dumps(result))
    sys.exit(0)

mdata = maloo["data"]
enforced = mdata.get("enforced", {})
result["ci_summary"] = (
    f"enforced: {enforced.get('total_pass', 0)} pass, "
    f"{enforced.get('total_fail', 0)} fail"
)

# Process each enforced failure
for test in enforced.get("tests", []):
    if test.get("verdict") != "FAIL":
        continue

    # retest_pending is on the test group, not individual
    # failures. If a retest is pending for this test group,
    # skip ALL its failures.
    if test.get("retest_pending"):
        result["actions_taken"].append({
            "type": "skipped_retest_pending",
            "description": (
                f"{test['test']}: retest already pending"
            ),
        })
        continue

    for failure in test.get("failures", []):
        session_url = failure.get("url", "")
        # Extract session ID from URL
        sid_match = re.search(
            r'/test_sessions/([0-9a-f-]+)', session_url)
        if not sid_match:
            result["errors"].append(
                f"Could not extract session ID from {session_url}")
            continue
        session_id = sid_match.group(1)

        # Get failure details
        fails = run_tool(["maloo", "failures", session_id])
        if not fails or not fails.get("ok"):
            result["errors"].append(
                f"maloo failures failed for {session_id}")
            continue

        fdata = fails["data"]
        for suite in fdata.get("failed_suites", []):
            suite_id = suite.get("suite_id", "")
            suite_name = suite.get("suite", "")

            # Check linked bugs for this test set
            bugs = run_tool(["maloo", "bugs", suite_id])
            linked_bugs = []
            if bugs and bugs.get("ok"):
                linked_bugs = bugs["data"].get("bug_links", [])

            if linked_bugs:
                # Bug already linked — request retest with it
                bug_id = linked_bugs[0].get("ticket",
                    linked_bugs[0].get("bug_id", ""))
                if bug_id:
                    retest_result = run_tool([
                        "maloo", "retest", session_id, bug_id])
                    result["actions_taken"].append({
                        "type": "retest",
                        "description": (
                            f"{test['test']} {suite_name}: "
                            f"retest with {bug_id}"
                        ),
                        "session_id": session_id,
                        "bug": bug_id,
                    })
                continue

            # No linked bug — collect failing subtests for LLM
            # research. The orchestrator will search JIRA,
            # assess relatedness, and take appropriate action.
            for subtest in suite.get("failed_subtests", []):
                test_name = subtest.get("name", "")
                error_msg = subtest.get("error", "")
                result["needs_llm_decision"].append({
                    "suite_id": suite_id,
                    "session_id": session_id,
                    "test": (f"{test['test']} {suite_name}"
                             f" {test_name}"),
                    "error": (error_msg[:300]
                              if error_msg else ""),
                })

print(json.dumps(result))
PYEOF
	;;

check-status)
	[[ $# -ge 1 ]] || die "check-status requires <gerrit_url>"
	exec gerrit info "$1"
	;;

check-ci)
	[[ $# -ge 1 ]] || die "check-ci requires <gerrit_url>"
	exec gerrit maloo "$1"
	;;

check-reviews)
	[[ $# -ge 1 ]] || die "check-reviews requires <gerrit_url>"
	exec gerrit comments "$1"
	;;

search-bug)
	[[ $# -ge 1 ]] || die "search-bug requires <test_name>"
	TEST_NAME="$1"
	# Search for open bugs matching this test name
	JQL="project in (LU, EX) AND summary ~ \"${TEST_NAME}\" AND status in (New, \"To Do\", Open, \"In Progress\")"
	exec jira search "$JQL"
	;;

check-linked-bugs)
	[[ $# -ge 1 ]] || die "check-linked-bugs requires <test_set_id>"
	exec maloo bugs "$1"
	;;

link-bug)
	[[ $# -ge 2 ]] || die "link-bug requires <test_set_id> <JIRA_TICKET>"
	if [[ -n "$DRY_RUN" ]]; then
		echo "{\"ok\": true, \"dry_run\": true, \"action\": \"link-bug\", \"args\": [\"$1\", \"$2\"]}"
		exit 0
	fi
	rate_check link_bug "$MAX_LINK_BUGS"
	exec maloo link-bug "$1" "$2"
	;;

raise-bug)
	[[ $# -ge 1 ]] || die "raise-bug requires <test_set_id>"
	if [[ -n "$DRY_RUN" ]]; then
		echo "{\"ok\": true, \"dry_run\": true, \"action\": \"raise-bug\", \"args\": [\"$1\"]}"
		exit 0
	fi
	rate_check raise_bug "$MAX_RAISE_BUGS"
	exec maloo raise-bug "$@"
	;;

retest)
	[[ $# -ge 2 ]] || die "retest requires <session_id> <JIRA_TICKET>"
	if [[ -n "$DRY_RUN" ]]; then
		echo "{\"ok\": true, \"dry_run\": true, \"action\": \"retest\", \"args\": [\"$1\", \"$2\"]}"
		exit 0
	fi
	rate_check retest "$MAX_RETESTS"
	exec maloo retest "$1" "$2"
	;;

get-failures)
	[[ $# -ge 1 ]] || die "get-failures requires <session_id>"
	exec maloo failures "$1"
	;;

update-patch)
	[[ $# -ge 3 ]] || die "update-patch requires <index> <field> <value>"
	INDEX="$1"
	FIELD="$2"
	VALUE="$3"

	# Only allow specific fields to be updated
	case "$FIELD" in
	watch_status|stop_reason|last_review_count|last_patchset)
		;;
	*)
		die "Field '$FIELD' is not permitted. Allowed: watch_status, stop_reason, last_review_count, last_patchset"
		;;
	esac

	# Validate watch_status values
	if [[ "$FIELD" == "watch_status" ]]; then
		case "$VALUE" in
		active|needs_review|stopped|merged|abandoned)
			;;
		*)
			die "Invalid watch_status '$VALUE'. Allowed: active, needs_review, stopped, merged, abandoned"
			;;
		esac
	fi

	# Use python to update the JSON safely
	python3 -c "
import json, sys

idx = int(sys.argv[1])
field = sys.argv[2]
value = sys.argv[3]

with open('$PATCHES_FILE') as f:
    data = json.load(f)

patches = data.get('patches', data) if isinstance(data, dict) else data
if idx < 0 or idx >= len(patches):
    print(f'ERROR: Index {idx} out of range (0-{len(patches)-1})', file=sys.stderr)
    sys.exit(1)

# Try to convert numeric values
try:
    value = int(value)
except ValueError:
    pass

patches[idx][field] = value
if isinstance(data, dict):
    data['patches'] = patches

with open('$PATCHES_FILE', 'w') as f:
    json.dump(data, f, indent=4)
    f.write('\n')

print(json.dumps({'ok': True, 'index': idx, 'field': field, 'value': value}))
" "$INDEX" "$FIELD" "$VALUE"
	;;

write-report)
	[[ $# -ge 1 ]] || die "write-report requires <json_file_path>"
	REPORT_FILE="$1"
	# Read JSON from stdin, write to file, then apply status changes
	# to patches_to_watch.json automatically. This is the reliable
	# path — don't depend on the LLM to call update-patch separately.
	cat > "$REPORT_FILE"

	python3 -c "
import json, sys

with open('$REPORT_FILE') as f:
    report = json.load(f)

with open('$PATCHES_FILE') as f:
    patches_data = json.load(f)

patches = patches_data.get('patches', [])
changed = False

# Map action types to watch_status values
STATUS_MAP = {
    'merged': 'merged',
    'abandoned': 'abandoned',
    'stopped': 'stopped',
    'needs_review': 'needs_review',
}

for action in report.get('actions', []):
    idx = action.get('patch_index')
    atype = action.get('type', '')
    new_status = STATUS_MAP.get(atype)

    if new_status is None or idx is None:
        continue
    if idx < 0 or idx >= len(patches):
        print(f'WARNING: patch_index {idx} out of range', file=sys.stderr)
        continue

    old_status = patches[idx].get('watch_status', 'active')
    if old_status != new_status:
        patches[idx]['watch_status'] = new_status
        changed = True

    # For stopped patches, store the reason
    if atype == 'stopped' and action.get('description'):
        patches[idx]['stop_reason'] = action['description']
        changed = True

patches_data['patches'] = patches

if changed:
    with open('$PATCHES_FILE', 'w') as f:
        json.dump(patches_data, f, indent=4)
        f.write('\n')
    print(json.dumps({'ok': True, 'path': '$REPORT_FILE',
                       'patches_updated': True}))
else:
    print(json.dumps({'ok': True, 'path': '$REPORT_FILE',
                       'patches_updated': False}))
" || {
		# Report was written even if patch update fails
		echo "{\"ok\": true, \"path\": \"$REPORT_FILE\", \"patches_updated\": false, \"error\": \"failed to update patches file\"}"
	}
	;;

""|--help|-h)
	usage
	;;

*)
	echo "ERROR: Action '$ACTION' is not permitted." >&2
	echo "" >&2
	usage >&2
	exit 1
	;;
esac
