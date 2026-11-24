#!/usr/bin/env python3
"""Find test numbers that open Gerrit changes use in lustre/tests files.

Superseded by `gerrit test-numbers FILE --suggest K` (gerrit_cli), which
also places hunks by position, marks gaps taken and suggests a number;
this script is kept for people who already use it.

For each named test file the script reports:
  - the test IDs in the branch tip,
  - the test IDs that each open, recently updated change adds,
  - IDs that two changes add (collisions),
  - changes whose hunks sit within a few lines of each other (textual
    conflict risk), and
  - the free number ranges.

Example:
  gerrit_test_numbers.py sanity-lnet.sh conf-sanity.sh
  gerrit_test_numbers.py --days 30 --branch b_es6_0 --json lustre/tests/sanity.sh
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROJECT = "fs/lustre-release"
TESTS_DIR = "lustre/tests/"
RUN_TEST_RE = re.compile(r"^\+?\s*run_test\s+(\d+[A-Za-z]*)\b")
# Hunks closer than this many base lines can conflict when applied together.
NEAR_LINES = 3


def api(path, params=None):
    """GET a Gerrit REST path. Return parsed JSON."""
    base = os.environ.get("GERRIT_URL", "https://review.whamcloud.com")
    user = os.environ.get("GERRIT_USER") or os.environ.get("GERRIT_HTTP_USER")
    pw = os.environ.get("GERRIT_PASS") or os.environ.get("GERRIT_HTTP_PASSWORD")
    url = base.rstrip("/") + ("/a" if user and pw else "") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    if user and pw:
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()
    return json.loads(body.split("\n", 1)[1])  # drop the )]}' prefix


def quote(s):
    return urllib.parse.quote(s, safe="")


def norm_path(name):
    return name if "/" in name else TESTS_DIR + name


def num(test_id):
    return int(re.match(r"\d+", test_id).group())


def baseline_ids(branch, path):
    """Return the test IDs that run_test uses at the branch tip."""
    url = f"/projects/{quote(PROJECT)}/branches/{quote(branch)}/files/{quote(path)}/content"
    base = os.environ.get("GERRIT_URL", "https://review.whamcloud.com")
    user = os.environ.get("GERRIT_USER") or os.environ.get("GERRIT_HTTP_USER")
    pw = os.environ.get("GERRIT_PASS") or os.environ.get("GERRIT_HTTP_PASSWORD")
    req = urllib.request.Request(base.rstrip("/") + ("/a" if user and pw else "") + url)
    if user and pw:
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=60) as r:
        text = base64.b64decode(r.read()).decode(errors="replace")
    ids = set()
    for line in text.splitlines():
        m = RUN_TEST_RE.match(line)
        if m:
            ids.add(m.group(1))
    return ids


def change_hunks(cnum, path):
    """Return (added_ids, removed_ids, base_line_ranges) for one file."""
    d = api(f"/changes/{cnum}/revisions/current/files/{quote(path)}/diff", {"context": 0})
    added, removed, ranges = set(), set(), []
    old = 1
    for chunk in d.get("content", []):
        if "ab" in chunk:
            old += len(chunk["ab"])
            continue
        a, b = chunk.get("a", []), chunk.get("b", [])
        ranges.append((old, old + max(len(a) - 1, 0)))
        for line in b:
            m = RUN_TEST_RE.match(line)
            if m:
                added.add(m.group(1))
        for line in a:
            m = RUN_TEST_RE.match(line)
            if m:
                removed.add(m.group(1))
        old += len(a)
    return added, removed, ranges


def free_ranges(used_nums, lo, hi):
    """Return inclusive (start, end) ranges in [lo, hi] absent from used_nums."""
    out, start = [], None
    for n in range(lo, hi + 1):
        if n in used_nums:
            if start is not None:
                out.append((start, n - 1))
                start = None
        elif start is None:
            start = n
    if start is not None:
        out.append((start, hi))
    return out


def fmt_ranges(rs):
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in rs) or "none"


def find_changes(paths, branch, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    pq = " OR ".join(f'path:"{p}"' for p in paths)
    q = f'status:open project:{PROJECT} after:"{since}" ({pq})'
    if branch != "any":
        q += f" branch:{branch}"
    return api("/changes/", {"q": q, "n": 200, "o": ["CURRENT_REVISION", "CURRENT_FILES"]})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="test file names, e.g. sanity-lnet.sh")
    ap.add_argument("--days", type=int, default=60, help="updated within N days (default 60)")
    ap.add_argument("--branch", default="master", help="branch, or 'any' (default master)")
    ap.add_argument("--headroom", type=int, default=20,
                    help="free numbers to show above the highest used number (default 20)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    paths = [norm_path(f) for f in args.files]
    changes = find_changes(paths, args.branch, args.days)
    base_branch = "master" if args.branch == "any" else args.branch
    result = {}

    for path in paths:
        base_ids = baseline_ids(base_branch, path)
        per_change = []
        for c in changes:
            if path not in c.get("revisions", {}).get(c["current_revision"], {}).get("files", {}):
                continue
            added, removed, ranges = change_hunks(c["_number"], path)
            per_change.append({
                "change": c["_number"], "subject": c["subject"], "branch": c["branch"],
                "owner": c["owner"].get("name", c["owner"].get("_account_id")),
                "updated": c["updated"][:10], "added": sorted(added, key=lambda s: (num(s), s)),
                "removed": sorted(removed, key=lambda s: (num(s), s)), "ranges": ranges,
            })

        owners = {}
        for pc in per_change:
            for t in pc["added"]:
                owners.setdefault(t, []).append(pc["change"])
        # A number clashes when two changes add it, or the tip already has it.
        collisions = {t: ch for t, ch in owners.items() if len(ch) > 1 or t in base_ids}
        # Clash on the numeric part too, e.g. 77a in one change and 77b in another.
        by_num = {}
        for pc in per_change:
            for t in pc["added"]:
                by_num.setdefault(num(t), {}).setdefault(t, set()).add(pc["change"])
        shared_num = {n: sorted(set().union(*ids.values()))
                      for n, ids in by_num.items() if len(ids) > 1}

        near = []
        for i, a in enumerate(per_change):
            for b in per_change[i + 1:]:
                if any(x0 - NEAR_LINES <= y1 and y0 - NEAR_LINES <= x1
                       for x0, x1 in a["ranges"] for y0, y1 in b["ranges"]):
                    near.append((a["change"], b["change"]))

        used = {num(t) for t in base_ids} | {num(t) for t in owners}
        top = max(used, default=0)
        result[path] = {
            "tip_max": max((num(t) for t in base_ids), default=0),
            "changes": per_change, "collisions": collisions,
            "shared_numbers": shared_num, "near_conflicts": near,
            "free_below_max": free_ranges(used, 1, top),
            "free_above_max": [top + 1, top + args.headroom],
        }

    if args.json:
        print(json.dumps(result, indent=2, default=list))
        return

    for path, r in result.items():
        print(f"== {path} (branch {args.branch}, {len(r['changes'])} open change(s), "
              f"last {args.days} days)")
        print(f"   highest test number at tip: {r['tip_max']}")
        for pc in r["changes"]:
            adds = ", ".join(pc["added"]) or "no new tests"
            rem = f"  removes: {', '.join(pc['removed'])}" if pc["removed"] else ""
            print(f"   {pc['change']} [{pc['branch']}] {pc['updated']} {pc['owner']}: {adds}{rem}")
            print(f"        {pc['subject']}")
        for t, ch in sorted(r["collisions"].items(), key=lambda kv: num(kv[0])):
            print(f"   COLLISION test {t}: changes {ch}" if len(ch) > 1
                  else f"   COLLISION test {t}: change {ch[0]} reuses a tip test ID")
        for n, ch in sorted(r["shared_numbers"].items()):
            print(f"   SAME NUMBER {n} (different suffix) in changes {ch}")
        for a, b in r["near_conflicts"]:
            print(f"   NEAR HUNKS: {a} and {b} edit lines within {NEAR_LINES} of each other")
        print(f"   free below max: {fmt_ranges([tuple(x) for x in r['free_below_max']])}")
        print(f"   free above max: {r['free_above_max'][0]}-{r['free_above_max'][1]}")
        print("   Check retired numbers: git log -S'run_test N ' <branch> -- <file>")


if __name__ == "__main__":
    sys.exit(main())
