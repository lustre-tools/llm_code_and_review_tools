#!/usr/bin/env python3
"""Weekly report: open customer tickets → linked LU tickets → Gerrit patches → release tags.

Walks the chain end-to-end and emits a Markdown report. Caches LU patch lookups
and per-commit tag lookups in /tmp to make re-runs cheap.

Usage:
    python3 weekly_patch_report.py [filter_id] [--limit N] [--out FILE] [--no-cache]

Default filter is 44763 ("DDN open cases").
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

WORKERS = 8

CACHE_FILE = Path("/tmp/weekly_patch_report_cache.json")
GERRIT_ENV = Path.home() / ".config" / "gerrit-cli" / ".env"
LU_RE = re.compile(r"\bLU-\d+\b")

# Local lustre-release clones — used for fast `git tag --contains` lookups
# instead of the slow Gerrit `commits/{sha}/in` REST endpoint.
LOCAL_CLONES = [
    Path.home() / "src" / "lustre" / "b6",
    Path.home() / "src" / "lustre" / "b7",
]

# Branches we care about (others are filtered out)
TRACKED_BRANCHES = ("master", "b_es6_0", "b_es7_0")

# Branch → human label / release line
BRANCH_LABELS = {
    "master": "master (Lustre 2.17+)",
    "b_es6_0": "b_es6_0 (EXA 6.3.x)",
    "b_es7_0": "b_es7_0 (EXA 7.0)",
}

# Compact branch labels for Slack output
BRANCH_LABELS_SHORT = {
    "master": "master",
    "b_es6_0": "es6.3",
    "b_es7_0": "es7.0",
}

# Tags we treat as "release tags" (others like lipe-* are stripped)
RELEASE_TAG_PREFIXES = ("2.14.0-ddn", "2.15.", "2.16.", "2.17.", "es7.0")
# Suffixes to exclude (RC / beta / pre-release tags)
EXCLUDE_TAG_SUFFIXES = ("RC", "rc", "beta", "alpha")


# ---------- cache ----------

def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"lu_patches": {}, "commit_tags": {}}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ---------- jira ----------

_JIRA_AUTH = None
_JIRA_URL = None


def jira_creds():
    global _JIRA_AUTH, _JIRA_URL
    if _JIRA_AUTH is not None:
        return _JIRA_URL, _JIRA_AUTH
    import base64
    cfg = json.loads((Path.home() / ".jira-tool.json").read_text())
    c = cfg["instances"]["cloud"]
    _JIRA_URL = c["server"]
    auth = c["auth"]
    _JIRA_AUTH = "Basic " + base64.b64encode(
        f"{auth['email']}:{auth['token']}".encode()
    ).decode()
    return _JIRA_URL, _JIRA_AUTH


def jira_rest(path):
    """GET a JIRA Cloud REST endpoint and return parsed JSON."""
    url, auth = jira_creds()
    full = f"{url}{path}"
    result = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: {auth}", "-H", "Accept: application/json", full],
        capture_output=True, text=True
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def jira_cloud(*args):
    """Fallback for things still using the CLI (filter get, search)."""
    result = subprocess.run(
        ["jira", "-I", "cloud", *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"jira error ({args}): {result.stderr}", file=sys.stderr, flush=True)
        return None
    return json.loads(result.stdout)


def fetch_filter_issues(filter_id, limit=None):
    args = ["search", f"filter = {filter_id}"]
    if limit:
        args += ["--limit", str(limit)]
    else:
        args += ["--limit", "500"]
    data = jira_cloud(*args)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("issues", [])


def _adf_to_text(node):
    """Walk an ADF doc node and concatenate all text content."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        out = node.get("text", "") or ""
        for child in node.get("content", []) or []:
            out += " " + _adf_to_text(child)
        return out
    if isinstance(node, list):
        return " ".join(_adf_to_text(n) for n in node)
    return ""


def extract_lu_refs(issue_key):
    """Find LU-XXXX references via a single REST call.

    Pulls description, comments, and issue links in one round trip.
    """
    data = jira_rest(
        f"/rest/api/3/issue/{issue_key}"
        "?fields=description,issuelinks,comment&expand=renderedFields"
    )
    if not data:
        return []

    refs = set()
    fields = data.get("fields", {}) or {}

    # Issue links
    for link in fields.get("issuelinks", []) or []:
        for side in ("inwardIssue", "outwardIssue"):
            issue = link.get(side) or {}
            key = issue.get("key", "")
            if key.startswith("LU-"):
                refs.add(key)

    # Description (use rendered HTML for clean text)
    rendered = data.get("renderedFields", {}) or {}
    desc = rendered.get("description", "") or _adf_to_text(fields.get("description", ""))
    refs.update(LU_RE.findall(desc))

    # Comments (paginated by default — fetch additional pages if needed)
    comment_data = fields.get("comment", {}) or {}
    comments = comment_data.get("comments", []) or []
    total = comment_data.get("total", len(comments))

    for c in comments:
        body = c.get("body", "")
        if isinstance(body, (dict, list)):
            body = _adf_to_text(body)
        refs.update(LU_RE.findall(body or ""))

    # If there are more comments than returned in the first page, paginate
    if len(comments) < total:
        start = len(comments)
        while start < total:
            page = jira_rest(
                f"/rest/api/3/issue/{issue_key}/comment?startAt={start}&maxResults=100"
            ) or {}
            page_comments = page.get("comments", []) or []
            if not page_comments:
                break
            for c in page_comments:
                body = c.get("body", "")
                if isinstance(body, (dict, list)):
                    body = _adf_to_text(body)
                refs.update(LU_RE.findall(body or ""))
            start += len(page_comments)

    return sorted(refs)


# ---------- gerrit ----------

_GERRIT_CREDS = None


def gerrit_creds():
    global _GERRIT_CREDS
    if _GERRIT_CREDS is not None:
        return _GERRIT_CREDS
    if not GERRIT_ENV.exists():
        _GERRIT_CREDS = (None, None, None)
        return _GERRIT_CREDS
    env = {}
    for line in GERRIT_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    _GERRIT_CREDS = (env.get("GERRIT_URL"), env.get("GERRIT_USER"), env.get("GERRIT_PASS"))
    return _GERRIT_CREDS


def gerrit_get(path):
    """GET a Gerrit REST endpoint and return parsed JSON."""
    url, user, password = gerrit_creds()
    if not url:
        return None
    full = f"{url}{path}"
    result = subprocess.run(
        ["curl", "-s", "-u", f"{user}:{password}", full],
        capture_output=True, text=True
    )
    body = result.stdout.lstrip()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1] if "\n" in body else body[4:]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def get_lu_patches(lu_key, cache):
    """Return patches for an LU ticket, filtered by subject prefix.

    Uses Gerrit REST API directly (one call) instead of `gc search` + `gc info`.
    """
    if lu_key in cache["lu_patches"]:
        return cache["lu_patches"][lu_key]

    data = gerrit_get(f"/a/changes/?q=message:{lu_key}&o=CURRENT_REVISION") or []

    prefix = f"{lu_key} "
    matches = []
    for c in data:
        subject = c.get("subject", "")
        if not subject.startswith(prefix):
            continue
        if c.get("branch") not in TRACKED_BRANCHES:
            continue
        matches.append({
            "number": c.get("_number"),
            "subject": subject,
            "branch": c.get("branch"),
            "status": c.get("status"),
            "project": c.get("project"),
            "commit": c.get("current_revision"),
        })

    cache["lu_patches"][lu_key] = matches
    return matches


def _git_tags_local(commit):
    """Try each local clone with `git tag --contains`.

    Returns the tag list if any clone has the commit (possibly empty if the
    commit is not yet in any tag). Returns None only if no local clone has the
    commit at all — caller falls back to REST in that case.
    """
    for clone in LOCAL_CLONES:
        if not clone.exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(clone), "tag", "--contains", commit],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return [t.strip() for t in result.stdout.splitlines() if t.strip()]
    return None


def get_commit_tags(commit, project, cache):
    """Get tags containing a Gerrit commit. Tries local git first, then REST."""
    if not commit:
        return []
    if commit in cache["commit_tags"]:
        return cache["commit_tags"][commit]

    # Try local clones first (fast)
    local_tags = _git_tags_local(commit)
    if local_tags is not None:
        cache["commit_tags"][commit] = local_tags
        return local_tags

    # Fall back to Gerrit REST (slow)
    project_enc = project.replace("/", "%2F")
    data = gerrit_get(f"/a/projects/{project_enc}/commits/{commit}/in") or {}
    tags = data.get("tags", [])
    cache["commit_tags"][commit] = tags
    return tags


def first_release_tag(tags):
    """Return the first release tag (sorted), filtering out lipe-* and RC tags."""
    release = [
        t for t in tags
        if t.startswith(RELEASE_TAG_PREFIXES)
        and not any(suf in t for suf in EXCLUDE_TAG_SUFFIXES)
    ]
    if not release:
        return None

    def sort_key(t):
        nums = re.findall(r"\d+", t)
        return tuple(int(n) for n in nums)

    return sorted(release, key=sort_key)[0]


# ---------- report ----------

def patch_summary_line(patch, cache):
    n = patch["number"]
    branch = patch["branch"]
    status = patch["status"]
    label = BRANCH_LABELS.get(branch, branch)

    if status == "MERGED":
        tags = get_commit_tags(patch.get("commit"), patch.get("project"), cache)
        first = first_release_tag(tags)
        if first:
            return f"  - **{label}**: [{n}](https://review.whamcloud.com/c/fs/lustre-release/+/{n}) MERGED → first in **{first}**"
        return f"  - **{label}**: [{n}](https://review.whamcloud.com/c/fs/lustre-release/+/{n}) MERGED (no release tag yet)"
    elif status == "ABANDONED":
        return f"  - **{label}**: [{n}](https://review.whamcloud.com/c/fs/lustre-release/+/{n}) ABANDONED"
    else:
        return f"  - **{label}**: [{n}](https://review.whamcloud.com/c/fs/lustre-release/+/{n}) {status}"


def status_bucket(patches_by_lu):
    """Compute a roll-up bucket across all LU tickets for a customer issue."""
    if not patches_by_lu:
        return "No LU references"

    has_any = False
    has_landed = False
    has_released = False
    all_merged = True
    for lu_key, patches in patches_by_lu.items():
        if not patches:
            continue
        has_any = True
        for p in patches:
            if p["status"] == "MERGED":
                has_landed = True
                if p.get("_first_tag"):
                    has_released = True
            else:
                all_merged = False

    if not has_any:
        return "No patches"
    if has_released and all_merged:
        return "Released"
    if has_landed:
        return "Partially landed"
    return "In review"


# Priority sort key (lower = more urgent)
PRIORITY_RANK = {
    "Blocker": 1,
    "Critical": 2,
    "Major": 3,
    "Medium": 4,
    "Minor": 5,
    "Trivial": 6,
}


def _branch_summary_for_slack(branch, patches, cache):
    """Compact per-branch summary including the winning patch number.

    Picks the highest-precedence patch (merged-with-tag > merged-no-tag > NEW
    > ABANDONED), shows it as a clickable Gerrit link, and appends ' +N' if
    there are additional patches on the same branch.

    Example: 'es6.3:<url|64266> ✓ddn248 +2'
    """
    label = BRANCH_LABELS_SHORT.get(branch, branch)
    if not patches:
        return f"{label}:—"

    def precedence(p):
        if p["status"] == "MERGED":
            tags = cache["commit_tags"].get(p.get("commit", ""), [])
            return 0 if first_release_tag(tags) else 1
        if p["status"] == "ABANDONED":
            return 3
        return 2  # NEW or other open state

    # Sort by precedence, then by patch number (lowest = earliest)
    sorted_patches = sorted(patches, key=lambda p: (precedence(p), p["number"]))
    winner = sorted_patches[0]
    n = winner["number"]
    url = f"https://review.whamcloud.com/c/fs/lustre-release/+/{n}"
    link = f"<{url}|{n}>"

    if winner["status"] == "MERGED":
        tags = cache["commit_tags"].get(winner.get("commit", ""), [])
        ft = first_release_tag(tags)
        if ft:
            short = ft.replace("2.14.0-", "")
            text = f"{link} ✓{short}"
        else:
            text = f"{link} MERGED (no tag)"
    elif winner["status"] == "ABANDONED":
        text = f"{link} ABND"
    else:
        text = f"{link} NEW"

    extra = len(patches) - 1
    if extra > 0:
        text += f" +{extra}"

    return f"{label}:{text}"


def render_slack(report, filter_name, top_n=15, excluded=0):
    """Compact Slack message focused on actionable tickets.

    Returns a single mrkdwn-formatted string suitable for chat.postMessage.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    buckets = defaultdict(list)
    for entry in report:
        buckets[entry["bucket"]].append(entry)

    lines = [
        f":rotating_light: *Weekly Patch Status — {filter_name}*",
        f"_{today}_",
        "",
        "*Summary:*",
        f"• Tickets with linked LU tickets: *{len(report)}* (excluded {excluded} with no LU refs)",
        f"• :white_check_mark: Released: {len(buckets.get('Released', []))}",
        f"• :warning: Partially landed: {len(buckets.get('Partially landed', []))}",
        f"• :hourglass_flowing_sand: In review: {len(buckets.get('In review', []))}",
        f"• :grey_question: No patches: {len(buckets.get('No patches', []))}",
        "",
    ]

    # Actionable = Partially landed + In review (sorted by priority then key)
    actionable = (
        buckets.get("Partially landed", []) + buckets.get("In review", [])
    )
    actionable.sort(key=lambda e: (
        PRIORITY_RANK.get(e["priority"], 99),
        e["key"],
    ))

    if not actionable:
        lines.append("_No actionable tickets — all caught up._")
        return "\n".join(lines)

    shown = actionable[:top_n]
    lines.append(
        f"*Top {len(shown)} actionable tickets* "
        f"(of {len(actionable)}, by priority):"
    )
    lines.append("")

    for i, entry in enumerate(shown, 1):
        key = entry["key"]
        summary = entry["summary"][:75]
        prio = entry["priority"]
        url = f"https://ime-ddn.atlassian.net/browse/{key}"
        lines.append(f"*{i}. <{url}|{key}>* [{prio}] {summary}")

        # Group patches by branch per LU
        for lu_key, patches in entry["lu_patches"].items():
            if not patches:
                continue
            by_branch = defaultdict(list)
            for p in patches:
                by_branch[p["branch"]].append(p)

            # Render in canonical branch order
            parts = []
            for branch in TRACKED_BRANCHES:
                if branch in by_branch:
                    parts.append(
                        _branch_summary_for_slack(
                            branch, by_branch[branch], entry["_cache_ref"]
                        )
                    )
                else:
                    parts.append(f"{BRANCH_LABELS_SHORT[branch]}:—")

            lu_url = f"https://jira.whamcloud.com/browse/{lu_key}"
            lines.append(f"   • <{lu_url}|{lu_key}>: {' / '.join(parts)}")

    if len(actionable) > top_n:
        lines.append("")
        lines.append(f"_+{len(actionable) - top_n} more not shown — see full report._")

    return "\n".join(lines)


def send_slack(text, token, channel):
    payload = json.dumps({
        "channel": channel,
        "text": text,
        "mrkdwn": True,
    })
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "https://slack.com/api/chat.postMessage",
        ],
        capture_output=True, text=True,
    )
    try:
        resp = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Slack non-JSON response: {result.stdout[:200]}", file=sys.stderr, flush=True)
        return False
    if not resp.get("ok"):
        print(f"Slack error: {resp.get('error')}", file=sys.stderr, flush=True)
        return False
    return True


def render_markdown(report, filter_name, excluded=0):
    today = datetime.now().strftime("%Y-%m-%d")
    out = [
        f"# Weekly Patch Report — {filter_name}",
        f"_Generated {today}_",
        "",
        f"**Tickets with linked LU tickets:** {len(report)} _(excluded {excluded} with no LU refs)_",
        "",
    ]

    # Group by status bucket
    buckets = defaultdict(list)
    for entry in report:
        buckets[entry["bucket"]].append(entry)

    for bucket in ["Partially landed", "In review", "Released", "No patches"]:
        if bucket not in buckets:
            continue
        items = buckets[bucket]
        out.append(f"## {bucket} ({len(items)})")
        out.append("")
        for entry in items:
            key = entry["key"]
            summary = entry["summary"][:100]
            prio = entry["priority"]
            status = entry["status"]
            assignee = entry["assignee"] or "Unassigned"
            out.append(
                f"### [{key}](https://ime-ddn.atlassian.net/browse/{key}) — *{prio}* — {status}"
            )
            out.append(f"_{summary}_  ")
            out.append(f"Assignee: {assignee}")
            out.append("")
            if not entry["lu_patches"]:
                out.append("No LU references found in links/description/comments")
            else:
                for lu_key, patches in entry["lu_patches"].items():
                    out.append(f"- **[{lu_key}](https://jira.whamcloud.com/browse/{lu_key})**:")
                    if not patches:
                        out.append(f"  - No Gerrit patches found")
                    else:
                        for p in patches:
                            out.append(patch_summary_line(p, entry["_cache_ref"]))
            out.append("")
        out.append("")

    return "\n".join(out)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("filter_id", nargs="?", default="44763",
                    help="JIRA filter ID (default: 44763 DDN open cases)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Limit number of customer tickets (for testing)")
    ap.add_argument("--out", type=str, default=None,
                    help="Output file (default: stdout)")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore cache (re-fetch everything)")
    ap.add_argument("--slack", action="store_true",
                    help="Send compact report to Slack instead of writing markdown")
    ap.add_argument("--slack-channel", default=os.environ.get("SLACK_CHANNEL", "U07B3LABTAT"),
                    help="Slack channel/user ID (default: env SLACK_CHANNEL or U07B3LABTAT)")
    ap.add_argument("--slack-top", type=int, default=15,
                    help="Number of top actionable tickets to show in Slack mode (default: 15)")
    args = ap.parse_args()

    cache = {"lu_patches": {}, "commit_tags": {}} if args.no_cache else load_cache()

    filter_info = jira_cloud("filter", "get", args.filter_id) or {}
    filter_name = filter_info.get("name", f"Filter {args.filter_id}")

    print(f"Fetching tickets from filter {args.filter_id} ({filter_name})...", file=sys.stderr, flush=True)
    issues = fetch_filter_issues(args.filter_id, args.limit)
    print(f"Got {len(issues)} tickets", file=sys.stderr, flush=True)

    # Phase 1: extract LU refs from each ticket in parallel
    print(f"Phase 1: extracting LU refs from {len(issues)} tickets ({WORKERS} workers)...",
          file=sys.stderr, flush=True)
    ticket_lus = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        future_map = {ex.submit(extract_lu_refs, i["key"]): i for i in issues}
        for n, fut in enumerate(as_completed(future_map), 1):
            issue = future_map[fut]
            lu_refs = fut.result() or []
            ticket_lus[issue["key"]] = lu_refs
            if n % 10 == 0 or n == len(issues):
                print(f"  [{n}/{len(issues)}] extracted", file=sys.stderr, flush=True)

    # Phase 2: fetch patches for all unique LU refs in parallel
    all_lus = sorted({lu for refs in ticket_lus.values() for lu in refs})
    print(f"Phase 2: fetching patches for {len(all_lus)} unique LU tickets...",
          file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(get_lu_patches, lu, cache) for lu in all_lus]
        for n, _ in enumerate(as_completed(futures), 1):
            if n % 25 == 0 or n == len(all_lus):
                print(f"  [{n}/{len(all_lus)}] LU tickets done", file=sys.stderr, flush=True)
    save_cache(cache)

    # Phase 3: fetch tags for all unique merged commits in parallel
    all_commits = []
    for lu_key in all_lus:
        for p in cache["lu_patches"].get(lu_key, []):
            if p["status"] == "MERGED" and p.get("commit"):
                all_commits.append((p["commit"], p["project"]))
    unique_commits = list({c: p for c, p in all_commits}.items())
    print(f"Phase 3: fetching tags for {len(unique_commits)} unique merged commits...",
          file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(get_commit_tags, c, p, cache) for c, p in unique_commits]
        for n, _ in enumerate(as_completed(futures), 1):
            if n % 50 == 0 or n == len(unique_commits):
                print(f"  [{n}/{len(unique_commits)}] commits done", file=sys.stderr, flush=True)
    save_cache(cache)

    # Phase 4: assemble the report (skip tickets with no LU references)
    print(f"Phase 4: assembling report...", file=sys.stderr, flush=True)
    report = []
    skipped_no_lu = 0
    for issue in issues:
        key = issue.get("key", "?")
        lu_refs = ticket_lus.get(key, [])
        if not lu_refs:
            skipped_no_lu += 1
            continue
        lu_patches = {lu: cache["lu_patches"].get(lu, []) for lu in lu_refs}

        # Annotate patches with first_tag
        for patches in lu_patches.values():
            for p in patches:
                if p["status"] == "MERGED":
                    tags = cache["commit_tags"].get(p.get("commit", ""), [])
                    p["_first_tag"] = first_release_tag(tags)

        entry = {
            "key": key,
            "summary": issue.get("summary", ""),
            "priority": issue.get("priority", "?"),
            "status": issue.get("status", "?"),
            "assignee": issue.get("assignee"),
            "lu_refs": lu_refs,
            "lu_patches": lu_patches,
            "_cache_ref": cache,
        }
        entry["bucket"] = status_bucket(lu_patches)
        report.append(entry)

    print(f"Excluded {skipped_no_lu} tickets with no LU references", file=sys.stderr, flush=True)

    if args.slack:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            print("SLACK_BOT_TOKEN env var not set", file=sys.stderr, flush=True)
            sys.exit(1)
        msg = render_slack(report, filter_name, top_n=args.slack_top, excluded=skipped_no_lu)
        ok = send_slack(msg, token, args.slack_channel)
        if not ok:
            sys.exit(1)
        print(f"Sent Slack report ({len(msg)} chars)", file=sys.stderr, flush=True)
        if args.out:
            Path(args.out).write_text(msg)
    else:
        md = render_markdown(report, filter_name, excluded=skipped_no_lu)
        if args.out:
            Path(args.out).write_text(md)
            print(f"Wrote report to {args.out}", file=sys.stderr, flush=True)
        else:
            print(md)


if __name__ == "__main__":
    main()
