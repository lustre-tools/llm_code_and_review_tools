"""Usable new subtest numbers for lustre/tests scripts: free number, free slot.

A new subtest goes into its script in numeric order: its test_N() function
and run_test N line sit between the test before it and the test after it,
so the number picks the place.  Two open changes that add different numbers
into the same gap still conflict, since both patches' context is the same
neighbouring lines; the end of the list (the append point) is where every
"big round number" goes.  So a number is usable only when

1. nothing at the branch tip and no open change uses it, and no except list
   (ALWAYS_EXCEPT / always_except in the script, lustre/tests/except/*.ex)
   on master or a maintained branch names it, since an old server would
   skip the new test; and
2. no open change has a hunk in, or within NEAR_LINES of, its gap: from
   the run_test line of the test before it to the first line of the test
   after it (its comment or test_M() line), at the tip.

The rule is interim: once a new subtest can live in its own .ltsh fragment
it edits no shared region, and only the number check remains.

An open change's hunks are against its own base, not the tip.  Each hunk
is placed on the tip by the nearest test line above it (run_test X or
test_X()), found in both files, so a change on an older base still lands
in the right gap.
"""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .parallel import parallel_map

PROJECT = "fs/lustre-release"
TESTS_DIR = "lustre/tests/"
# Branches whose except lists a new number must avoid besides master.
MAINTAINED_BRANCHES = ("b2_15", "b2_16", "b2_17")
RUN_TEST_RE = re.compile(r"^\+?\s*run_test\s+(\d+[A-Za-z]*)\b")
DEF_RE = re.compile(r"^\s*test_(\d+[A-Za-z]*)\s*\(\s*\)")
ALWAYS_EXCEPT_RE = re.compile(r"^\s*always_except\s+\S+\s+(.*)$")
ALWAYS_EXCEPT_VAR_RE = re.compile(r'^\s*ALWAYS_EXCEPT\+?="([^"]*)"')
TEST_ID_RE = re.compile(r"^\d+[A-Za-z]*$")
# Hunks closer than this many lines can conflict when applied together.
NEAR_LINES = 3
LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def norm_path(name):
    return name if "/" in name else TESTS_DIR + name


def num(test_id):
    return int(re.match(r"\d+", test_id).group())


def id_key(test_id):
    """Sort key: 56 < 56a < 56z < 56aa < 57."""
    suffix = test_id[len(str(num(test_id))):]
    return (num(test_id), len(suffix), suffix)


def parse_tests(text):
    """The tests in a script, in file order.

    [{id, run_line, start_line}]: run_line is the 1-based run_test line;
    start_line the first line of the test's block, its test_X() line or
    the comment lines right above it.  A test with no test_X() line found
    starts at its run_test line.
    """
    lines = text.splitlines()
    defs = {}
    for i, line in enumerate(lines, 1):
        m = DEF_RE.match(line)
        if m:
            start = i
            while start > 1 and lines[start - 2].lstrip().startswith("#"):
                start -= 1
            defs.setdefault(m.group(1), start)
    tests = []
    for i, line in enumerate(lines, 1):
        m = RUN_TEST_RE.match(line)
        if m:
            tid = m.group(1)
            start = defs.get(tid, i)
            tests.append({"id": tid, "run_line": i,
                          "start_line": start if start <= i else i})
    return tests


def anchors(text):
    """{(kind, id): line} for every run_test and test_X() line."""
    out = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = RUN_TEST_RE.match(line)
        if m:
            out.setdefault(("run", m.group(1)), i)
        m = DEF_RE.match(line)
        if m:
            out.setdefault(("def", m.group(1)), i)
    return out


def except_ids(script_text=None, ex_texts=()):
    """Test ids an except list names: always_except and ALWAYS_EXCEPT
    literals in the script, and the subtest fields of except/*.ex files."""
    ids = set()
    for line in (script_text or "").splitlines():
        m = ALWAYS_EXCEPT_RE.match(line)
        words = m.group(1).split() if m else []
        m = ALWAYS_EXCEPT_VAR_RE.match(line)
        if m:
            words += m.group(1).split()
        ids |= {w for w in words if TEST_ID_RE.match(w)}
    for text in ex_texts:
        for line in (text or "").splitlines():
            fields = line.split("#", 1)[0].split()
            ids |= {w for w in fields[4:] if TEST_ID_RE.match(w)}
    return ids


def diff_hunks(diff):
    """A Gerrit file diff (full context) as (base_text, hunks, added, removed).

    hunks are inclusive (first, last) base-line ranges; a pure insertion
    is the line it goes before, as one line.
    """
    base, hunks, added, removed = [], [], set(), set()
    for chunk in diff.get("content", []):
        if "ab" in chunk:
            base += chunk["ab"]
            continue
        if "skip" in chunk:
            base += [""] * chunk["skip"]
            continue
        a, b = chunk.get("a", []), chunk.get("b", [])
        first = len(base) + 1
        hunks.append((first, first + max(len(a) - 1, 0)))
        added |= {m.group(1) for m in map(RUN_TEST_RE.match, b) if m}
        removed |= {m.group(1) for m in map(RUN_TEST_RE.match, a) if m}
        base += a
    return "\n".join(base), hunks, added, removed


def map_hunks(base_text, hunks, tip_anchors):
    """Place hunks (base lines) on the tip, by the nearest test line above.

    A hunk with no test line above it that the tip also has keeps its own
    line numbers.
    """
    base_anchors = sorted((line, key) for key, line in
                          anchors(base_text).items() if key in tip_anchors)
    out = []
    for first, last in hunks:
        shift = 0
        for line, key in base_anchors:
            if line > first:
                break
            shift = tip_anchors[key] - line
        out.append((first + shift, last + shift))
    return out


def touches(gap, ranges, near=NEAR_LINES):
    lo, hi = gap
    return any(r0 - near <= hi and lo - near <= r1 for r0, r1 in ranges)


def next_suffix(suffix):
    """56x -> 56xa, 56xa -> 56xb, ..., 56xZ -> 56xZa."""
    if not suffix or suffix[-1] not in LETTERS or suffix[-1] == LETTERS[-1]:
        return suffix + "a"
    return suffix[:-1] + LETTERS[LETTERS.index(suffix[-1]) + 1]


def _gap_after(tests, pred, total_lines):
    """The gap after test pred: its run_test line to the first line of the
    test after it in the file, or the next line when it is the last."""
    i = tests.index(pred)
    if i + 1 < len(tests):
        return [pred["run_line"], tests[i + 1]["start_line"]]
    return [pred["run_line"], min(pred["run_line"] + 1, max(total_lines, 1))]


def _pred_for(tests, test_id):
    """The test a new test_id goes after: the highest-numbered tip test
    below it, the last of those in the file when several share it."""
    below = [t for t in tests if id_key(t["id"]) < id_key(test_id)]
    if not below:
        return None
    best = max(id_key(t["id"]) for t in below)
    best_num = best[0]
    # The whole family of the number before goes ahead of a plain number:
    # 57 follows 56, 56a ... 56xe, placed after whichever is last in the file.
    if TEST_ID_RE.match(test_id) and test_id == str(num(test_id)):
        same = [t for t in below if num(t["id"]) == best_num]
    else:
        same = [t for t in below if id_key(t["id"]) == best]
    return max(same, key=lambda t: t["run_line"])


def slots(tip_text, changes, except_list=(), family=None, near=NEAR_LINES):
    """Every candidate gap and whether it is usable.

    changes: [{change, added, ranges}] with ranges already on the tip.
    Returns {tip_max, slots: [...], append, family}.  A slot is
    {kind: inside|append|family, after, gap, numbers, taken_by, usable};
    numbers are the free ids that go into that gap.
    """
    tests = parse_tests(tip_text)
    total = len(tip_text.splitlines())
    tip_ids = {t["id"] for t in tests}
    open_ids = {t for c in changes for t in c["added"]}
    excepted = set(except_list)
    used_nums = {num(t) for t in tip_ids | open_ids}
    tip_max = max((num(t) for t in tip_ids), default=0)

    def taken_by(gap, extra=()):
        hit = {c["change"] for c in changes if touches(gap, c["ranges"], near)}
        return sorted(hit | set(extra))

    out, by_gap = [], {}
    for n in range(1, tip_max):
        tid = str(n)
        if n in used_nums or tid in excepted:
            continue
        pred = _pred_for(tests, tid)
        if pred is None:
            continue
        gap = tuple(_gap_after(tests, pred, total))
        by_gap.setdefault((pred["id"], gap), []).append(tid)
    for (after, gap), numbers in by_gap.items():
        hit = taken_by(gap)
        out.append({"kind": "inside", "after": after, "gap": list(gap),
                    "numbers": numbers, "taken_by": hit, "usable": not hit})

    # The first free number above the tip's highest.  Not max + 1: a
    # stray far number in an open change (999p) must not push it away.
    ex_nums = {num(t) for t in excepted if TEST_ID_RE.match(t)}
    top = tip_max + 1
    while top in used_nums or top in ex_nums:
        top += 1
    append_id = str(top)
    append = None
    pred = _pred_for(tests, append_id)
    if pred is not None:
        gap = _gap_after(tests, pred, total)
        # A change that adds above the tip's highest number is at the
        # append point too, wherever its hunk sits.
        beyond = [c["change"] for c in changes
                  if any(num(t) > tip_max for t in c["added"])]
        hit = taken_by(gap, beyond)
        append = {"kind": "append", "after": pred["id"], "gap": gap,
                  "numbers": [append_id], "taken_by": hit, "usable": not hit}
        out.append(append)

    fam = None
    if family:
        members = [t for t in tests if t["id"].startswith(family)
                   and num(t["id"]) == num(family)]
        ids = {t["id"] for t in members} | {
            t for t in open_ids if t.startswith(family) and num(t) == num(family)}
        last = max(ids, key=id_key) if ids else family
        cand = next_suffix(last[len(str(num(last))):])
        cand = str(num(family)) + cand
        while cand in tip_ids or cand in open_ids or cand in excepted:
            cand = str(num(family)) + next_suffix(cand[len(str(num(family))):])
        pred = (max(members, key=lambda t: t["run_line"]) if members
                else _pred_for(tests, cand))
        if pred is not None:
            gap = _gap_after(tests, pred, total)
            rivals = [c["change"] for c in changes
                      if any(t.startswith(family) and num(t) == num(family)
                             for t in c["added"])]
            hit = taken_by(gap, rivals)
            fam = {"kind": "family", "after": pred["id"], "gap": gap,
                   "numbers": [cand], "taken_by": hit, "usable": not hit}
            out.append(fam)

    return {"tip_max": tip_max, "slots": out, "append": append,
            "family": fam, "except_listed": sorted(excepted, key=id_key)}


def suggest(slot_info, k):
    """Up to k usable numbers, one per gap: the family first when asked,
    then gaps inside the range (highest first, near the newer tests),
    then the append point."""
    picks = []
    fam = slot_info.get("family")
    if fam and fam["usable"]:
        picks.append(fam)
    inside = [s for s in slot_info["slots"]
              if s["kind"] == "inside" and s["usable"]]
    inside.sort(key=lambda s: -num(s["numbers"][0]))
    picks += inside
    app = slot_info.get("append")
    if app and app["usable"]:
        picks.append(app)
    return [{"number": s["numbers"][0], "after": s["after"],
             "gap": s["gap"], "kind": s["kind"]} for s in picks[:k]]


# --- Gerrit ---------------------------------------------------------------

def find_changes(client, paths, branch, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    pq = " OR ".join(f'path:"{p}"' for p in paths)
    q = f'status:open project:{PROJECT} after:"{since}" ({pq})'
    if branch != "any":
        q += f" branch:{branch}"
    out = []
    while True:
        page = client.search_changes(
            q, limit=200, start=len(out),
            options=["CURRENT_REVISION", "CURRENT_FILES"]) or []
        out += page
        if not page or not page[-1].get("_more_changes"):
            return out


def branch_file(client, branch, path):
    """A file's text at a branch tip, or None when it is not there."""
    try:
        return client.get_text(
            f"/projects/{quote(PROJECT, safe='')}/branches/"
            f"{quote(branch, safe='')}/files/{quote(path, safe='')}/content")
    except Exception as exc:  # a branch without the file answers 404
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise


def gather(client, files, branch="master", days=60, family=None,
           except_branches=None, suggest_k=0):
    """The report for each file: open changes, collisions, slots."""
    paths = [norm_path(f) for f in files]
    changes = find_changes(client, paths, branch, days)
    base_branch = "master" if branch == "any" else branch
    if except_branches is None:
        except_branches = ["master", *MAINTAINED_BRANCHES]
    result = {}
    for path in paths:
        tip = branch_file(client, base_branch, path)
        if tip is None:
            raise FileNotFoundError(f"{path} is not on {base_branch}")
        tip_anchors = anchors(tip)
        suite = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ex_path = f"{TESTS_DIR}except/{suite}.ex"
        excepted = set()
        for b in dict.fromkeys([base_branch, *except_branches]):
            script = tip if b == base_branch else branch_file(client, b, path)
            excepted |= except_ids(script, [branch_file(client, b, ex_path)])

        touching = [c for c in changes if path in c.get("revisions", {}).get(
            c.get("current_revision"), {}).get("files", {})]
        diffs = parallel_map(
            lambda c: client.get_diff(c["_number"], path, None), touching)
        per_change = []
        for c, diff in zip(touching, diffs):
            base_text, hunks, added, removed = diff_hunks(diff)
            ranges = map_hunks(base_text, hunks, tip_anchors)
            per_change.append({
                "change": c["_number"], "subject": c.get("subject", ""),
                "branch": c.get("branch", ""),
                "owner": c.get("owner", {}).get(
                    "name", c.get("owner", {}).get("_account_id")),
                "updated": c.get("updated", "")[:10],
                "added": sorted(added, key=id_key),
                "removed": sorted(removed, key=id_key),
                "ranges": [list(r) for r in ranges],
            })
        result[path] = report(tip, per_change, excepted, family, suggest_k)
    return result


def report(tip, per_change, excepted, family=None, suggest_k=0):
    """One file's report from its tip text and its open changes."""
    tip_ids = {t["id"] for t in parse_tests(tip)}
    owners = {}
    for pc in per_change:
        for t in pc["added"]:
            owners.setdefault(t, []).append(pc["change"])
    collisions = {t: ch for t, ch in owners.items()
                  if len(ch) > 1 or t in tip_ids}
    by_num = {}
    for pc in per_change:
        for t in pc["added"]:
            by_num.setdefault(num(t), {}).setdefault(t, set()).add(pc["change"])
    shared_num = {n: sorted(set().union(*ids.values()))
                  for n, ids in by_num.items() if len(ids) > 1}
    near = []
    for i, a in enumerate(per_change):
        for b in per_change[i + 1:]:
            if any(touches((x0, x1), [(y0, y1)])
                   for x0, x1 in a["ranges"] for y0, y1 in b["ranges"]):
                near.append([a["change"], b["change"]])
    info = slots(tip, per_change, excepted, family)
    out = {
        "tip_max": info["tip_max"], "changes": per_change,
        "collisions": collisions, "shared_numbers": shared_num,
        "near_conflicts": near, "slots": info["slots"],
        "append": info["append"], "family": info["family"],
        "except_listed": info["except_listed"],
        "suggest": suggest(info, suggest_k) if suggest_k else [],
        "retired_check": "git log -S'run_test N ' <branch> -- <file>",
    }
    return out


def format_text(result, branch="master", days=60):
    """The report for people."""
    lines = []
    for path, r in result.items():
        lines.append(f"== {path} (branch {branch}, {len(r['changes'])} open "
                     f"change(s), last {days} days)")
        lines.append(f"   highest test number at tip: {r['tip_max']}")
        for pc in r["changes"]:
            adds = ", ".join(pc["added"]) or "no new tests"
            rem = (f"  removes: {', '.join(pc['removed'])}"
                   if pc["removed"] else "")
            lines.append(f"   {pc['change']} [{pc['branch']}] {pc['updated']} "
                         f"{pc['owner']}: {adds}{rem}")
            lines.append(f"        {pc['subject']}")
        for t, ch in sorted(r["collisions"].items(), key=lambda kv: id_key(kv[0])):
            lines.append(f"   COLLISION test {t}: changes {ch}" if len(ch) > 1
                         else f"   COLLISION test {t}: change {ch[0]} reuses a "
                              "tip test ID")
        for n, ch in sorted(r["shared_numbers"].items()):
            lines.append(f"   SAME NUMBER {n} (different suffix) in changes {ch}")
        for a, b in r["near_conflicts"]:
            lines.append(f"   NEAR HUNKS: {a} and {b} edit lines within "
                         f"{NEAR_LINES} of each other")
        taken = [s for s in r["slots"] if s["kind"] == "inside" and s["taken_by"]]
        free = [s for s in r["slots"] if s["kind"] == "inside" and s["usable"]]
        lines.append(f"   gaps inside the range: {len(free)} usable, "
                     f"{len(taken)} touched by an open change")
        for s in taken:
            lines.append(f"   TAKEN {_nums(s)} (after {s['after']}, lines "
                         f"{s['gap'][0]}-{s['gap'][1]}): change(s) "
                         f"{', '.join(map(str, s['taken_by']))}")
        for s in (r["family"], r["append"]):
            if not s:
                continue
            what = "family next" if s["kind"] == "family" else "append point"
            state = ("usable" if s["usable"] else "CONTESTED by change(s) "
                     + ", ".join(map(str, s["taken_by"])))
            lines.append(f"   {what} {s['numbers'][0]} (after {s['after']}, "
                         f"line {s['gap'][0]}): {state}")
        if r["suggest"]:
            lines.append("   suggest: " + "; ".join(
                f"{s['number']} (after {s['after']}, lines {s['gap'][0]}-"
                f"{s['gap'][1]}, no open change touches it)"
                for s in r["suggest"]))
        elif r["slots"]:
            lines.append("   (--suggest K lists usable numbers)")
        lines.append(f"   Check retired numbers: {r['retired_check']}")
    return "\n".join(lines)


def _nums(slot):
    """Free numbers as runs: 82, 84-98."""
    runs = []
    for n in map(int, slot["numbers"]):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
