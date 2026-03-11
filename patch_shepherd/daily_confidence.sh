#!/bin/bash
# daily_confidence.sh — Daily confidence report for the patch shepherd.
#
# Pure code — no LLM. Aggregates the last 24 hours of archived reports
# and watcher.log to produce a health summary email.

set -euo pipefail

WATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${HOME}/.patch_shepherd"
LOG_FILE="${LOG_DIR}/watcher.log"
PATCHES_FILE="${PATCHES_FILE:-/shared/support_files/patches_to_watch.json}"
RECIPIENT="${PATCH_SHEPHERD_EMAIL:-pfarrell@whamcloud.com}"
FROM="noreply@mulberrytree.us"
HTML_FILE="/tmp/patch_shepherd_daily.html"

# Generate the HTML report from archived reports + log
python3 << 'PYEOF'
import json, glob, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_DIR = os.path.expanduser("~/.patch_shepherd")
PATCHES_FILE = os.environ.get(
    "PATCHES_FILE", "/shared/support_files/patches_to_watch.json")
HTML_FILE = "/tmp/patch_shepherd_daily.html"

now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)

# --- Load archived reports from the last 24h ---
reports = []
for path in sorted(glob.glob(f"{LOG_DIR}/report_*.json")):
    fname = os.path.basename(path)
    m = re.match(r"report_(\d{8}_\d{6})\.json", fname)
    if not m:
        continue
    ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    ts = ts.replace(tzinfo=timezone.utc)
    if ts < cutoff:
        continue
    try:
        with open(path) as f:
            data = json.load(f)
        data["_file_ts"] = ts.isoformat()
        reports.append(data)
    except Exception:
        continue

# --- Scan watcher.log for failures in the last 24h ---
log_file = f"{LOG_DIR}/watcher.log"
failed_runs = 0
total_log_runs = 0
log_errors = []
if os.path.exists(log_file):
    with open(log_file) as f:
        for line in f:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            if not m:
                continue
            try:
                line_ts = datetime.fromisoformat(m.group(1))
                line_ts = line_ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if line_ts < cutoff:
                continue
            if "run starting" in line:
                total_log_runs += 1
            if "ERROR" in line:
                failed_runs += 1
                log_errors.append(line.strip())

# --- Aggregate stats ---
n_reports = len(reports)
total_cost = sum(
    r.get("debug", {}).get("total_cost_usd", 0) for r in reports)
total_input = sum(
    r.get("debug", {}).get("input_tokens", 0) for r in reports)
total_output = sum(
    r.get("debug", {}).get("output_tokens", 0) for r in reports)
total_duration = sum(
    r.get("debug", {}).get("duration_seconds", 0) for r in reports)
total_turns = sum(
    r.get("debug", {}).get("num_turns", 0) for r in reports)
total_tool_calls = sum(
    r.get("debug", {}).get("tool_calls", 0) for r in reports)

# Actions across all runs
all_actions = []
for r in reports:
    all_actions.extend(r.get("actions", []))

action_counts = {}
for a in all_actions:
    t = a.get("type", "unknown")
    action_counts[t] = action_counts.get(t, 0) + 1

# Collect all errors from reports
report_errors = []
for r in reports:
    report_errors.extend(r.get("debug", {}).get("errors", []))

# Deduplicate recurring errors
error_freq = {}
for e in report_errors:
    # Normalize: strip timestamps, session IDs
    key = re.sub(r"\d{5,}", "NNN", e)
    error_freq[key] = error_freq.get(key, 0) + 1

# Missing runs: if hourly, expect ~24 in 24h
expected_runs = 24
runs_started = max(total_log_runs, n_reports)

# --- Load current patch statuses ---
patches = []
try:
    with open(PATCHES_FILE) as f:
        patches = json.load(f).get("patches", [])
except Exception:
    pass

status_counts = {}
for p in patches:
    ws = p.get("watch_status", "active")
    status_counts[ws] = status_counts.get(ws, 0) + 1

# --- Health score (simple heuristic) ---
health_issues = []
if runs_started < expected_runs * 0.8:
    health_issues.append(
        f"Only {runs_started}/{expected_runs} runs detected "
        f"(expected ~hourly)")
if failed_runs > 0:
    health_issues.append(f"{failed_runs} runs had ERROR in log")
no_report_runs = total_log_runs - n_reports
if no_report_runs > 2:
    health_issues.append(
        f"{no_report_runs} runs produced no report")
if error_freq:
    health_issues.append(
        f"{len(report_errors)} tool errors across runs "
        f"({len(error_freq)} unique)")

if not health_issues:
    health_grade = "HEALTHY"
    health_color = "#2e7d32"
    health_bg = "#e8f5e9"
elif len(health_issues) <= 2 and failed_runs == 0:
    health_grade = "OK"
    health_color = "#f57f17"
    health_bg = "#fff8e1"
else:
    health_grade = "NEEDS ATTENTION"
    health_color = "#c62828"
    health_bg = "#ffebee"

# --- Format duration ---
if total_duration:
    mins, secs = divmod(int(total_duration), 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        dur_str = f"{hrs}h {mins}m {secs}s"
    elif mins:
        dur_str = f"{mins}m {secs}s"
    else:
        dur_str = f"{secs}s"
    avg_dur = int(total_duration / n_reports) if n_reports else 0
    avg_m, avg_s = divmod(avg_dur, 60)
    avg_str = f"{avg_m}m {avg_s}s" if avg_m else f"{avg_s}s"
else:
    dur_str = "?"
    avg_str = "?"

cost_str = f"${total_cost:.4f}" if total_cost else "$0"

# --- Build subject ---
subject = (
    f"[Patch Shepherd] Daily: {health_grade} — "
    f"{n_reports} runs, {len(all_actions)} actions, {cost_str}")

# --- Build HTML ---
html = f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
Roboto, sans-serif; max-width: 720px; margin: 0 auto; padding: 20px;
color: #333;">

<h2 style="color: #1a237e; border-bottom: 2px solid #1a237e;
padding-bottom: 8px; margin-bottom: 16px;">
  Patch Shepherd — Daily Confidence Report
</h2>
<p style="color: #666; font-size: 13px; margin-top: -12px;">
  {now.strftime("%Y-%m-%d %H:%M UTC")} (last 24 hours)
</p>

<!-- Health badge -->
<div style="background: {health_bg}; border: 2px solid {health_color};
border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;">
  <span style="background: {health_color}; color: #fff; padding: 4px 10px;
  border-radius: 4px; font-weight: 700; font-size: 14px;">
    {health_grade}
  </span>
'''

if health_issues:
    html += '  <ul style="margin: 8px 0 0 0; padding-left: 20px; '\
            'font-size: 13px;">\n'
    for issue in health_issues:
        html += f"    <li>{issue}</li>\n"
    html += "  </ul>\n"
else:
    html += ('  <span style="margin-left: 10px; font-size: 13px;">'
             'All systems nominal.</span>\n')

html += "</div>\n\n"

# --- Run Statistics ---
html += '''<h3 style="color: #1a237e; margin-bottom: 8px;
font-size: 15px;">Run Statistics</h3>
<table style="width: 100%; border-collapse: collapse; font-size: 13px;
margin-bottom: 20px;">
'''

stats_rows = [
    ("Runs completed", f"{n_reports}"),
    ("Runs started (from log)", f"{total_log_runs}"),
    ("Failed runs", f"{failed_runs}"),
    ("Total duration", dur_str),
    ("Avg duration per run", avg_str),
    ("Total LLM turns", f"{total_turns:,}"),
    ("Total tool calls", f"{total_tool_calls:,}"),
    ("Total tokens (in/out)",
     f"{total_input:,} / {total_output:,}"),
    ("Total cost (API equiv)", cost_str),
]
for label, value in stats_rows:
    html += f'''<tr>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;
  font-weight: 600; width: 200px;">{label}</td>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;">{value}</td>
</tr>
'''
html += "</table>\n\n"

# --- Actions summary ---
if all_actions:
    html += '''<h3 style="color: #1a237e; margin-bottom: 8px;
font-size: 15px;">Actions Taken (24h)</h3>
<table style="width: 100%; border-collapse: collapse; font-size: 13px;
margin-bottom: 20px;">
<tr style="background: #e8eaf6; font-weight: 600;">
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">Type</td>
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">Count</td>
</tr>
'''
    action_colors = {
        "merged": "#2e7d32", "abandoned": "#616161",
        "retest": "#1565c0", "link_bug": "#1565c0",
        "raise_bug": "#e65100", "needs_review": "#f9a825",
        "stopped": "#c62828",
    }
    for atype, count in sorted(
            action_counts.items(), key=lambda x: -x[1]):
        color = action_colors.get(atype, "#333")
        html += f'''<tr>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;">
    <span style="color: {color}; font-weight: 600;">
      {atype.upper().replace("_", " ")}
    </span>
  </td>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;">{count}</td>
</tr>
'''
    html += "</table>\n\n"
else:
    html += ('<p style="font-size: 13px; color: #666;">'
             "No actions taken in the last 24 hours.</p>\n\n")

# --- Patch status table ---
html += '''<h3 style="color: #1a237e; margin-bottom: 8px;
font-size: 15px;">Current Patch Statuses</h3>
<table style="width: 100%; border-collapse: collapse; font-size: 13px;
margin-bottom: 20px;">
<tr style="background: #e8eaf6; font-weight: 600;">
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">#</td>
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">Change</td>
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">
    Description</td>
  <td style="padding: 6px 8px; border: 1px solid #c5cae9;">Status</td>
</tr>
'''

status_colors = {
    "active":       ("#1565c0", "#e3f2fd"),
    "needs_review": ("#f57f17", "#fff8e1"),
    "stopped":      ("#c62828", "#ffebee"),
    "merged":       ("#2e7d32", "#e8f5e9"),
    "abandoned":    ("#616161", "#f5f5f5"),
}

for i, p in enumerate(patches):
    ws = p.get("watch_status", "active")
    color, bg = status_colors.get(ws, ("#333", "#fafafa"))
    gerrit_url = p.get("gerrit_url", "")
    desc = p.get("description", "?")
    jira = p.get("jira", "")

    cn_match = re.search(r"/\+/(\d+)", gerrit_url)
    cn = cn_match.group(1) if cn_match else "?"

    badge = (
        f'<span style="background: {color}; color: #fff; '
        f'padding: 2px 6px; border-radius: 3px; font-size: 11px; '
        f'font-weight: 600;">{ws.upper()}</span>')

    change_link = (
        f'<a href="{gerrit_url}" style="color: #1565c0; '
        f'text-decoration: none;">{cn}</a>'
        if gerrit_url else cn)

    jira_link = ""
    if jira:
        jira_url = f"https://jira.whamcloud.com/browse/{jira}"
        jira_link = (
            f' <a href="{jira_url}" style="color: #888; '
            f'text-decoration: none; font-size: 11px;">{jira}</a>')

    html += f'''<tr>
  <td style="padding: 5px 8px; border: 1px solid #e0e0e0;
  text-align: center;">{i}</td>
  <td style="padding: 5px 8px; border: 1px solid #e0e0e0;">
    {change_link}</td>
  <td style="padding: 5px 8px; border: 1px solid #e0e0e0;">
    {desc}{jira_link}</td>
  <td style="padding: 5px 8px; border: 1px solid #e0e0e0;
  text-align: center;">{badge}</td>
</tr>
'''

html += "</table>\n\n"

# --- Status summary ---
html += '<p style="font-size: 13px; margin-bottom: 20px;">'
for ws, count in sorted(status_counts.items()):
    color = status_colors.get(ws, ("#333", "#fafafa"))[0]
    html += (
        f'<span style="color: {color}; font-weight: 600;">'
        f'{ws.upper()}: {count}</span> &nbsp; ')
html += "</p>\n\n"

# --- Recurring errors ---
if error_freq:
    html += '''<h3 style="color: #c62828; margin-bottom: 8px;
font-size: 15px;">Recurring Errors</h3>
<table style="width: 100%; border-collapse: collapse; font-size: 12px;
margin-bottom: 20px;">
<tr style="background: #ffebee; font-weight: 600;">
  <td style="padding: 6px 8px; border: 1px solid #e0e0e0;">Error</td>
  <td style="padding: 6px 8px; border: 1px solid #e0e0e0;
  width: 60px;">Count</td>
</tr>
'''
    for err, cnt in sorted(error_freq.items(), key=lambda x: -x[1]):
        html += f'''<tr>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;
  font-family: monospace; font-size: 11px;">{err}</td>
  <td style="padding: 4px 8px; border: 1px solid #e0e0e0;
  text-align: center;">{cnt}</td>
</tr>
'''
    html += "</table>\n\n"

# --- Footer ---
html += '''<div style="background: #fafafa; border: 1px solid #e0e0e0;
padding: 10px 14px; border-radius: 4px; margin-top: 20px;
font-size: 11px; color: #888;">
  <strong>Patch Shepherd Daily Confidence Report</strong><br>
  Generated by code (no LLM). Sent once daily at 08:00 EST.<br>
  Hourly action emails are sent separately when actions are taken.
</div>

</body>
</html>'''

with open(HTML_FILE, "w") as f:
    f.write(html)

# Write subject for the shell to read
with open("/tmp/patch_shepherd_daily_subject.txt", "w") as f:
    f.write(subject)

print(f"Generated daily report: {n_reports} runs, "
      f"{len(all_actions)} actions, {cost_str}")
PYEOF

SUBJECT="$(cat /tmp/patch_shepherd_daily_subject.txt)"

# Send via mutt
mutt -e "set from=$FROM" \
	-e "set realname='Patch Shepherd'" \
	-e "set use_from=yes" \
	-e "set envelope_from=yes" \
	-e "set content_type=text/html" \
	-s "$SUBJECT" \
	"$RECIPIENT" < "$HTML_FILE"

echo "Daily confidence report sent to $RECIPIENT: $SUBJECT"

# Cleanup
rm -f "$HTML_FILE" /tmp/patch_shepherd_daily_subject.txt
