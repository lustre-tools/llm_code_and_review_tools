#!/bin/bash
# send_report.sh — Format and send the patch shepherd email report.
#
# Reads the JSON report + patches file, generates HTML email, sends via mutt.

set -euo pipefail

REPORT_FILE="${1:-/tmp/patch_shepherd_report.json}"
PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"
RECIPIENT="${PATCH_SHEPHERD_EMAIL:-pfarrell@whamcloud.com}"
FROM="noreply@mulberrytree.us"

if [[ ! -f "$REPORT_FILE" ]]; then
	echo "ERROR: Report file not found: $REPORT_FILE" >&2
	exit 1
fi

# Generate HTML email from JSON report
HTML_FILE="/tmp/patch_shepherd_email.html"

python3 -c "
import json, sys, re
from datetime import datetime

with open('$REPORT_FILE') as f:
    report = json.load(f)

# Load patches file for the full status table
try:
    with open('$PATCHES_FILE') as f:
        patches_data = json.load(f)
    patches = patches_data.get('patches', [])
except Exception:
    patches = []

actions = report.get('actions', [])
summary = report.get('summary', {})
ts = report.get('timestamp', datetime.utcnow().isoformat())
n_patches = report.get('patches_checked', 0)

# Build a set of patch indices that had actions this run
acted_indices = set()
for a in actions:
    idx = a.get('patch_index')
    if idx is not None:
        acted_indices.add(int(idx))

# Build subject
n_actions = len(actions)
parts = []
if summary.get('merged', 0):
    parts.append(f\"{summary['merged']} merged\")
if summary.get('stopped', 0):
    parts.append(f\"{summary['stopped']} stopped\")
if summary.get('needs_review', 0):
    parts.append(f\"{summary['needs_review']} needs review\")
if summary.get('retests_requested', 0):
    parts.append(f\"{summary['retests_requested']} retested\")
if summary.get('bugs_raised', 0):
    parts.append(f\"{summary['bugs_raised']} bugs raised\")
status_str = ', '.join(parts) if parts else 'no changes'
subject = f'[Patch Shepherd] {n_actions} actions, {n_patches} patches ({status_str})'

# Color map for statuses and action types
status_colors = {
    'active':       ('#1565c0', '#e3f2fd'),
    'needs_review': ('#f57f17', '#fff8e1'),
    'stopped':      ('#c62828', '#ffebee'),
    'merged':       ('#2e7d32', '#e8f5e9'),
    'abandoned':    ('#616161', '#f5f5f5'),
}

action_colors = {
    'merged': '#2e7d32',
    'abandoned': '#616161',
    'retest': '#1565c0',
    'link_bug': '#1565c0',
    'raise_bug': '#e65100',
    'needs_review': '#f9a825',
    'stopped': '#c62828',
}

action_labels = {
    'merged': 'MERGED',
    'abandoned': 'ABANDONED',
    'retest': 'RETEST',
    'link_bug': 'BUG LINKED',
    'raise_bug': 'BUG RAISED',
    'needs_review': 'NEEDS REVIEW',
    'stopped': 'STOPPED',
}

# Build HTML
html = '''<!DOCTYPE html>
<html>
<head><meta charset=\"utf-8\"></head>
<body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 720px; margin: 0 auto; padding: 20px; color: #333;\">

<h2 style=\"color: #1a237e; border-bottom: 2px solid #1a237e; padding-bottom: 8px; margin-bottom: 16px;\">
  Lustre Patch Shepherd Report
</h2>
<p style=\"color: #666; font-size: 13px; margin-top: -12px;\">''' + ts + '''</p>

'''

# === PATCH STATUS TABLE ===
html += '''<h3 style=\"color: #1a237e; margin-bottom: 8px; font-size: 15px;\">All Patches</h3>
<table style=\"width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 24px;\">
<tr style=\"background: #e8eaf6; font-weight: 600;\">
  <td style=\"padding: 6px 8px; border: 1px solid #c5cae9;\">#</td>
  <td style=\"padding: 6px 8px; border: 1px solid #c5cae9;\">Change</td>
  <td style=\"padding: 6px 8px; border: 1px solid #c5cae9;\">Description</td>
  <td style=\"padding: 6px 8px; border: 1px solid #c5cae9;\">Status</td>
</tr>
'''

for i, p in enumerate(patches):
    ws = p.get('watch_status', 'active')
    color, bg = status_colors.get(ws, ('#333', '#fafafa'))
    gerrit_url = p.get('gerrit_url', '')
    desc = p.get('description', '?')
    jira = p.get('jira', '')

    # Extract change number
    cn_match = re.search(r'/\\+/(\\d+)', gerrit_url)
    cn = cn_match.group(1) if cn_match else '?'

    # Highlight rows that had actions this run
    if i in acted_indices:
        row_bg = bg
        marker = ' *'
    else:
        row_bg = '#fff'
        marker = ''

    # Status badge
    badge = f'<span style=\"background: {color}; color: #fff; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600;\">{ws.upper()}</span>'

    # Change link
    change_link = f'<a href=\"{gerrit_url}\" style=\"color: #1565c0; text-decoration: none;\">{cn}</a>' if gerrit_url else cn

    # JIRA link
    jira_link = ''
    if jira:
        jira_url = f'https://jira.whamcloud.com/browse/{jira}'
        jira_link = f' <a href=\"{jira_url}\" style=\"color: #888; text-decoration: none; font-size: 11px;\">{jira}</a>'

    html += f'''<tr style=\"background: {row_bg};\">
  <td style=\"padding: 5px 8px; border: 1px solid #e0e0e0; text-align: center;\">{i}{marker}</td>
  <td style=\"padding: 5px 8px; border: 1px solid #e0e0e0;\">{change_link}</td>
  <td style=\"padding: 5px 8px; border: 1px solid #e0e0e0;\">{desc}{jira_link}</td>
  <td style=\"padding: 5px 8px; border: 1px solid #e0e0e0; text-align: center;\">{badge}</td>
</tr>
'''

html += '</table>\n'

# === ACTIONS ===
if actions:
    html += '<h3 style=\"color: #1a237e; margin-bottom: 8px; font-size: 15px;\">Actions This Run</h3>\n'

    # Group actions by type priority
    priority = ['stopped', 'needs_review', 'raise_bug', 'retest', 'link_bug', 'merged', 'abandoned']
    grouped = {}
    for a in actions:
        t = a.get('type', 'unknown')
        grouped.setdefault(t, []).append(a)

    for atype in priority:
        if atype not in grouped:
            continue
        items = grouped[atype]
        color = action_colors.get(atype, '#333')
        label = action_labels.get(atype, atype.upper())

        html += f'''<div style=\"margin-bottom: 16px;\">
  <h4 style=\"color: {color}; margin-bottom: 6px; font-size: 13px;\">
    {label} ({len(items)})
  </h4>
'''
        for a in items:
            gerrit = a.get('gerrit_url', '')
            jira = a.get('jira', '')
            desc = a.get('description', '')
            idx = a.get('patch_index', '')

            # Build links
            links = ''
            if idx != '':
                links += f'<span style=\"color: #999;\">patch #{idx}</span>'
            if gerrit:
                cn = re.search(r'/\\+/(\\d+)', gerrit)
                cn = cn.group(1) if cn else gerrit
                sep = ' &middot; ' if links else ''
                links += f'{sep}<a href=\"{gerrit}\" style=\"color: #1565c0; text-decoration: none;\">change {cn}</a>'
            if jira:
                jira_url = f'https://jira.whamcloud.com/browse/{jira}' if not jira.startswith('http') else jira
                sep = ' &middot; ' if links else ''
                links += f'{sep}<a href=\"{jira_url}\" style=\"color: #1565c0; text-decoration: none;\">{jira}</a>'

            action_bg = '#fff3e0' if atype == 'stopped' else '#f5f5f5'

            html += f'''  <div style=\"background: {action_bg}; border-left: 4px solid {color}; padding: 8px 12px; margin-bottom: 4px; border-radius: 0 4px 4px 0;\">
    <div style=\"font-size: 13px; font-weight: 600;\">{desc}</div>
    <div style=\"font-size: 11px; color: #666; margin-top: 3px;\">{links}</div>
  </div>
'''
        html += '</div>\n'

# === RUN INFO ===
html += '''<div style=\"background: #fafafa; border: 1px solid #e0e0e0; padding: 10px 14px; border-radius: 4px; margin-top: 20px; font-size: 11px; color: #888;\">
  <strong>Run Info</strong><br>'''

debug = report.get('debug', {})
last_checked = debug.get('last_checked', 'unknown')
tool_calls = debug.get('tool_calls', '?')
errors = debug.get('errors', [])
skipped = debug.get('skipped', [])
duration = debug.get('duration_seconds', 0)
input_tokens = debug.get('input_tokens', 0)
output_tokens = debug.get('output_tokens', 0)
total_cost = debug.get('total_cost_usd', 0)
num_turns = debug.get('num_turns', 0)

# Format duration
if duration:
    mins, secs = divmod(duration, 60)
    duration_str = f'{mins}m {secs}s' if mins else f'{secs}s'
else:
    duration_str = '?'

# Format cost
cost_str = f'\${total_cost:.4f}' if total_cost else '?'

html += f'Duration: {duration_str}<br>'
html += f'Previous run: {last_checked}<br>'
html += f'LLM turns: {num_turns} &middot; Tool calls: {tool_calls}<br>'
html += f'Tokens: {input_tokens:,} in / {output_tokens:,} out &middot; Cost: {cost_str}<br>'

if errors:
    html += '<br><strong style=\"color: #c62828;\">Errors:</strong><br>'
    for e in errors:
        html += f'&bull; {e}<br>'

if skipped:
    html += '<br><strong>Skipped:</strong><br>'
    for s in skipped:
        html += f'&bull; {s}<br>'

html += '''</div>

<p style=\"color: #999; font-size: 11px; margin-top: 12px;\">
  Automated by Patch Shepherd &middot; Next check in ~1 hour
</p>

</body>
</html>'''

with open('$HTML_FILE', 'w') as f:
    f.write(html)

# Write subject to a temp file for the shell to read
with open('/tmp/patch_shepherd_subject.txt', 'w') as f:
    f.write(subject)
" || {
	echo "ERROR: Failed to generate HTML email" >&2
	exit 1
}

SUBJECT="$(cat /tmp/patch_shepherd_subject.txt)"

# Send via mutt with HTML content type
mutt -e "set from=$FROM" \
	-e "set realname='Patch Shepherd'" \
	-e "set use_from=yes" \
	-e "set envelope_from=yes" \
	-e "set content_type=text/html" \
	-s "$SUBJECT" \
	"$RECIPIENT" < "$HTML_FILE"

echo "Email sent to $RECIPIENT: $SUBJECT"

# Cleanup
rm -f "$HTML_FILE" /tmp/patch_shepherd_subject.txt
