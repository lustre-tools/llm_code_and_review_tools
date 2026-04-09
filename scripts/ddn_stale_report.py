#!/usr/bin/env python3
"""Daily Slack report of DDN stale Blockers & Criticals (not updated in 48h)."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "U07B3LABTAT")
JIRA_BASE = "https://ime-ddn.atlassian.net/browse"
JQL = (
    "project = DDN AND priority in (Blocker, Critical) "
    "AND status not in (Closed, Resolved, Done) "
    "AND issuetype = Bug AND updated <= -48h "
    "ORDER BY priority DESC, updated DESC"
)


def fetch_issues():
    result = subprocess.run(
        ["jira", "-I", "cloud", "search", JQL, "--limit", "15"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"jira search failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else data.get("issues", [])


def days_since(updated_str):
    updated_str = updated_str.replace("Z", "+00:00")
    try:
        updated = datetime.fromisoformat(updated_str)
    except ValueError:
        return "?"
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - updated
    return delta.days


def build_message(issues):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f":rotating_light: *DDN Stale Blockers & Criticals* (not updated in 48h+)",
        f"_Report date: {today}_",
        "",
    ]
    if not issues:
        lines.append("No stale blockers or criticals found. :tada:")
    else:
        for i, issue in enumerate(issues, 1):
            key = issue.get("key", "?")
            summary = issue.get("summary", "")[:80]
            priority = issue.get("priority", "?")
            status = issue.get("status", "?")
            assignee = issue.get("assignee") or "Unassigned"
            updated = issue.get("updated", "")
            stale_days = days_since(updated)
            lines.append(
                f"{i}. <{JIRA_BASE}/{key}|{key}> | *{priority}* | {status} | {assignee}\n"
                f"    {summary} (_stale {stale_days}d_)"
            )
        lines.append("")
        lines.append(f"*Total: {len(issues)} tickets*")
    return "\n".join(lines)


def send_slack(text):
    import tempfile
    payload = json.dumps({
        "channel": SLACK_CHANNEL,
        "text": text,
        "mrkdwn": True,
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                "-H", f"Authorization: Bearer {SLACK_TOKEN}",
                "-H", "Content-Type: application/json",
                "-d", f"@{tmp_path}",
                "https://slack.com/api/chat.postMessage",
            ],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(tmp_path)
    resp = json.loads(result.stdout)
    if not resp.get("ok"):
        print(f"Slack error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


def main():
    if not SLACK_TOKEN:
        print("SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    issues = fetch_issues()
    msg = build_message(issues)
    send_slack(msg)
    print(f"Sent report with {len(issues)} issues")


if __name__ == "__main__":
    main()
