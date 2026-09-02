"""Derive build/test/review state from a bulk-fetched Gerrit change.

Works on change dicts returned by /changes/?q=... with
o=DETAILED_LABELS&o=MESSAGES&o=DETAILED_ACCOUNTS&o=CURRENT_REVISION&
o=CURRENT_COMMIT — no further HTTP requests.

Message-parsing logic follows gerrit_cli/commands/ci.py
(_maloo_for_change/_info_for_change) so the dashboard agrees with
`gc maloo` / `gc info`; adapted here to operate on already-fetched
messages instead of doing two GETs per change.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Service accounts seen on review.whamcloud.com.  SERVICE_USER tags are
# not reliable on all bots (aireview, RISC-V builder and the janitor
# lack the tag), so filtering is by name.
JENKINS_BOT = "jenkins"
MALOO_BOT = "Maloo"
AUTOTEST_BOT = "Autotest"
CHECKPATCH_BOT = "wc-checkpatch"
JANITOR_BOT = "Lustre Gerrit Janitor"
BOT_NAMES = {
    JENKINS_BOT,
    "Jenkins",
    MALOO_BOT,
    AUTOTEST_BOT,
    CHECKPATCH_BOT,
    JANITOR_BOT,
    "Misc Code Checks Robot (Gatekeeper helper)",
    "smatch review bot",
    "smatchreview",
    "aireview",
    "Gerrit AI review for Lustre",
    "Lustre RISC-V Builder",
    "CI Bot",
    "Build Bot",
    "Janitor Bot",
}
BOT_USERNAMES = {
    "jenkins",
    "maloo",
    "autotest",
    "hpdd-checkpatch",
    "lgerritjanitor",
    "smatchreview",
    "aireview",
    "janitor-gerrit",
    "do-not-reply",
    "hpdd-test-coordinator",
    "lustre-gerrit",
    "mdt-test-coordinator",
}


# Bots whose comments are REVIEW content to be addressed, not CI noise —
# their threads count like human reviewer threads.
REVIEW_BOT_NAMES = {
    "Gerrit AI review for Lustre",
    "aireview",
    "Misc Code Checks Robot (Gatekeeper helper)",
}
REVIEW_BOT_USERNAMES = {"aireview"}


def is_bot(account: dict | None) -> bool:
    """Service-account check by tag, name or username.

    The SERVICE_USER tag alone is not enough: aireview, the RISC-V
    builder and janitor-gerrit lack it on this server.
    """
    if not account:
        return False
    if "SERVICE_USER" in (account.get("tags") or []):
        return True
    if (account.get("name") or "") in BOT_NAMES:
        return True
    return (account.get("username") or "") in BOT_USERNAMES


def is_review_bot(account: dict | None) -> bool:
    if not account:
        return False
    return ((account.get("name") or "") in REVIEW_BOT_NAMES
            or (account.get("username") or "") in REVIEW_BOT_USERNAMES)

CRASH_MARKER = "%% THIS TEST SESSION CRASHED %%"

_PRIOR_VOTE_RE = re.compile(r"^Patch Set (\d+): Code-Review([+-]\d)")
_VOTE_REMOVED_RE = re.compile(r"^Patch Set (\d+): -Code-Review\b")
_BUILD_URL_RE = re.compile(r"https?://build\.whamcloud\.com/\S+")
# All CI links must be locked to the real testing host: bot messages are
# attacker-influenced (any Gerrit user can post a comment, and the "Maloo"
# author is matched by display name), so a bare "https://testing." prefix
# would let a spoofed message inject an off-host link the dashboard
# renders as clickable.  Every extractor below is host-anchored.
_TESTING_URL = r"https://testing\.whamcloud\.com/\S+"
# Posted by Maloo at the bottom of its session-enumeration message.
_MALOO_RESULTS_RE = re.compile(rf"Maloo Results:\s*({_TESTING_URL})")
_MALOO_QUEUE_RE = re.compile(rf"Maloo Test Queue:\s*({_TESTING_URL})")
_SESSION_URL_RE = re.compile(_TESTING_URL)
# Janitor messages link their own job's results page ("All results and
# logs" / "Job output URL"), not Maloo sessions.
_JANITOR_URL_RE = re.compile(r"https://testing\.whamcloud\.com/gerrit-janitor/\S+?results\.html")
_JANITOR_ITEM_RE = re.compile(r"^> \S+", re.MULTILINE)
_SEEN_REVIEWS_RE = re.compile(r"^Seen in reviews:((?:\s+\d+)+)\s*$")
_SUBTEST_SPLIT_RE = re.compile(r"\),\s+(?=test_)")


def parse_gerrit_ts(ts: str | None) -> datetime | None:
    """Parse Gerrit's 'YYYY-MM-DD HH:MM:SS.nnnnnnnnn' UTC timestamps."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_str(ts: str | None, now: datetime | None = None) -> str:
    dt = parse_gerrit_ts(ts)
    if not dt:
        return ""
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def current_ps(change: dict) -> int:
    ps = change.get("current_revision_number")
    if isinstance(ps, int):
        return ps
    rev = (change.get("revisions") or {}).get(change.get("current_revision") or "", {})
    return rev.get("_number", 0)


# Bots whose messages mark a patchset as "CI actually ran here".
# wc-checkpatch is excluded: it re-runs on every upload, including
# commit-message-only ones.
_CI_RUN_AUTHORS = {"jenkins", "Jenkins", MALOO_BOT, AUTOTEST_BOT, JANITOR_BOT}
_COPY_KINDS = ("NO_CODE_CHANGE", "NO_CHANGE")


def ci_relevant_ps(change: dict) -> int:
    """The patchset whose CI results are authoritative.

    A commit-message-only upload (revision kind NO_CODE_CHANGE, or
    NO_CHANGE for a same-tree re-push) copies the Verified votes and does
    NOT re-run build/testing — the relevant CI messages live on the last
    patchset that actually ran.  For real code changes this is simply the
    current patchset.
    """
    ps = current_ps(change)
    if current_revision(change).get("kind") not in _COPY_KINDS:
        return ps
    best = 0
    for m in _messages(change):
        if _author_name(m) in _CI_RUN_AUTHORS:
            n = m.get("_revision_number", 0)
            if best < n <= ps:
                best = n
    return best or ps


def current_revision(change: dict) -> dict:
    return (change.get("revisions") or {}).get(change.get("current_revision") or "", {})


def _messages(change: dict) -> list[dict]:
    return change.get("messages") or []


def _author_name(msg: dict) -> str:
    return (msg.get("author") or {}).get("name", "")


def parse_build(change: dict) -> dict:
    """Jenkins build state for the current patchset.

    Returns {state: SUCCESS|FAILURE|ABORTED|BUILDING|NONE, url, number, when}.
    """
    ps = ci_relevant_ps(change)
    for m in reversed(_messages(change)):
        if _author_name(m) not in ("jenkins", "Jenkins"):
            continue
        if m.get("_revision_number", 0) != ps:
            continue
        text = m.get("message", "")
        url_m = _BUILD_URL_RE.search(text)
        url = url_m.group(0).rstrip(":/") if url_m else ""
        number = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        when = m.get("date", "")
        if "Build Successful" in text:
            return {"state": "SUCCESS", "url": url, "number": number, "when": when}
        if "Build Failed" in text:
            state = "ABORTED" if "ABORTED" in text else "FAILURE"
            return {"state": state, "url": url, "number": number, "when": when}
        if "Build Started" in text:
            return {"state": "BUILDING", "url": url, "number": number, "when": when}
    # No jenkins message on this PS: NO_CODE_CHANGE/NO_CHANGE patchsets get
    # the Verified vote copied without a new message — fall back to the vote.
    jvote = _bot_vote(change, JENKINS_BOT)
    if jvote > 0:
        return {"state": "SUCCESS", "url": "", "number": "", "when": ""}
    if jvote < 0:
        return {"state": "FAILURE", "url": "", "number": "", "when": ""}
    return {"state": "NONE", "url": "", "number": "", "when": ""}


def parse_tests(change: dict) -> dict:
    """Maloo/Autotest test state for the current patchset.

    Follows the message grammar 'Failed|Passed enforced|optional test
    <group> on <platform> uploaded ... <session-url> <detail>'.
    """
    ps = ci_relevant_ps(change)
    enforced: dict[str, dict] = {}
    optional_failed: list[dict] = []
    retests: list[dict] = []
    sessions_enumerated = False
    last_when = ""
    results_url = ""
    queue_url = ""

    for m in _messages(change):
        author = _author_name(m)
        if m.get("_revision_number", 0) != ps:
            continue
        text = m.get("message", "")

        if author == AUTOTEST_BOT and "retest" in text.lower():
            retests.append({"date": m.get("date", ""), "message": text.strip()[:200]})
            continue
        if author != MALOO_BOT:
            continue
        last_when = m.get("date", "")
        if "The following sessions will be run" in text:
            sessions_enumerated = True
            # The enumeration message carries the links to all of this
            # build's sessions; a re-run on the same patchset posts a
            # fresh enumeration, so the newest one wins.
            res = _MALOO_RESULTS_RE.search(text)
            if res:
                results_url = res.group(1)
            queue = _MALOO_QUEUE_RE.search(text)
            if queue:
                queue_url = queue.group(1)
            continue

        for kind in ("enforced", "optional"):
            for status in ("Failed", "Passed"):
                marker = f"{status} {kind} test "
                if marker not in text:
                    continue
                rest = text.split(marker, 1)[1]
                name_plat = rest.split(" uploaded")[0].strip()
                parts = name_plat.split(" on ", 1)
                test_name = parts[0].strip()
                platform = parts[1].strip() if len(parts) > 1 else ""
                url = ""
                detail = ""
                sm = _SESSION_URL_RE.search(text)
                if sm:
                    url = sm.group(0)
                    detail = text[sm.end():].strip()
                if kind == "enforced":
                    bucket = enforced.setdefault(test_name, {"pass": [], "fail": []})
                    entry = {"platform": platform, "url": url, "detail": detail,
                             "crashed": CRASH_MARKER in text, "when": m.get("date", "")}
                    bucket["pass" if status == "Passed" else "fail"].append(entry)
                elif status == "Failed":
                    optional_failed.append({"test": test_name, "platform": platform,
                                            "url": url, "detail": detail})

    # A retest is only *pending* while it postdates the group's newest
    # verdict — once the retest has run and failed again, a new Maloo
    # message exists and the group must count as failing again.  And a
    # Maloo +1 vote supersedes all retest bookkeeping: it is the
    # aggregate all-enforced-passed verdict, even though the retested
    # group may never get its own "Passed enforced" message.
    maloo_vote = _bot_vote(change, MALOO_BOT)
    retested = set()
    if maloo_vote <= 0:
        for rt in retests:
            low = rt["message"].lower()
            for name, r in enforced.items():
                if name.lower() not in low:
                    continue
                last_verdict = max((e["when"] for e in r["fail"] + r["pass"]), default="")
                if rt["date"] > last_verdict:
                    retested.add(name)

    failed_tests = []
    enforced_pass = 0
    enforced_fail = 0
    for name in sorted(enforced):
        r = enforced[name]
        # The same session (group+platform) often fails repeatedly across
        # retests — collapse to the newest attempt and count the repeats,
        # so it isn't displayed/counted as several distinct failures.
        dedup: dict[str, dict] = {}
        for e in r["fail"]:
            prev = dedup.get(e["platform"])
            if prev is None:
                e = dict(e)
                e["attempts"] = 1
                dedup[e["platform"]] = e
            elif e["when"] >= prev["when"]:
                e = dict(e)
                e["attempts"] = prev["attempts"] + 1
                dedup[e["platform"]] = e
            else:
                prev["attempts"] += 1
        # The newest verdict per (group, platform) wins: a "Passed
        # enforced" message newer than the failure means the session
        # healed on retest: a requested retest passes, Maloo posts the
        # pass but keeps -1 for the still-failing groups.
        for p in r["pass"]:
            prev = dedup.get(p["platform"])
            if prev is not None and p["when"] >= prev["when"]:
                del dedup[p["platform"]]
        fails = sorted(dedup.values(), key=lambda x: x["when"])
        enforced_pass += len(r["pass"])
        enforced_fail += len(fails)
        if fails:
            failed_tests.append({
                "name": name,
                "passed": len(r["pass"]),
                "failures": fails,
                "retest_pending": name in retested,
            })

    # Maloo's vote is the aggregate verdict and wins over per-session
    # messages: failed enforced sessions that later self-healed via
    # auto-retest still leave "Failed enforced" messages behind, but
    # Maloo flips its vote to +1 once all enforced sessions pass.
    if maloo_vote < 0:
        state = "FAIL"
    elif maloo_vote > 0:
        state = "PASS"
    elif enforced_fail > 0:
        state = "FAIL"
    elif sessions_enumerated:
        state = "RUNNING"
    else:
        state = "NONE"

    return {
        "state": state,
        "maloo_vote": maloo_vote,
        "enforced_pass": enforced_pass,
        "enforced_fail": enforced_fail,
        "failed_tests": failed_tests,
        "optional_fail": len(optional_failed),
        "optional_failures": optional_failed,
        "retests_pending": len(retests),
        "sessions_enumerated": sessions_enumerated,
        "results_url": results_url,
        "queue_url": queue_url,
        "all_failures_retesting": bool(failed_tests) and all(t["retest_pending"] for t in failed_tests),
        "when": last_when,
    }


def _bot_vote(change: dict, bot_name: str) -> int:
    for e in ((change.get("labels") or {}).get("Verified") or {}).get("all") or []:
        if (e.get("name") or "").strip() == bot_name:
            v = e.get("value")
            return v if isinstance(v, int) else 0
    return 0


def _janitor_unique_line(token: str) -> list[dict]:
    """'<suite>@<fstype>[+mode]:test_<id>(<history>)[, test_<id>(<history>)...]'
    → parsed entries.

    One line can carry several subtests of the same suite; only the
    first carries the '<suite>@<fstype>:' prefix.  The history annotation
    is either 'Seen in reviews: <numbers...>' (a known flake elsewhere
    too — summarized to a count, the raw list can exceed 60 review
    numbers) or 'NEW unique failure for this branch in the last 30
    days, ...'.
    """
    head = token.split("(", 1)[0]
    prefix = head.rsplit(":", 1)[0] if ":" in head else ""
    out = []
    for seg in _SUBTEST_SPLIT_RE.split(token.strip()):
        name, _, note = seg.strip().partition("(")
        name = name.strip()
        if prefix and not name.startswith(prefix):
            name = f"{prefix}:{name}"
        note = note[:-1] if note.endswith(")") else note
        seen = _SEEN_REVIEWS_RE.match(note)
        if seen:
            n = len(seen.group(1).split())
            out.append({"test": name, "new": False,
                        "note": f"seen in {n} other review{'s' if n != 1 else ''}"})
        else:
            out.append({"test": name, "new": note.startswith("NEW"),
                        "note": note[:160]})
    return out


def _janitor_unique_tests(text: str) -> list[dict]:
    """Entries of the janitor's IMPORTANT unique-failure block.

    Current format: marker line, then one '- <entry>' line per test,
    terminated by a blank line.  Older messages put the entries inline
    on the marker line itself (' - <entry>' after the marker phrase).
    """
    out = []
    in_block = False
    for line in text.splitlines():
        if "failures unique to this patch" in line:
            in_block = True
            rest = line.split("failures unique to this patch", 1)[1]
            for token in rest.split(" - ")[1:]:
                out.extend(_janitor_unique_line(token))
            continue
        if not in_block:
            continue
        if not line.strip():
            if out:
                break
            continue
        if not line.startswith("- "):
            break
        out.extend(_janitor_unique_line(line[2:]))
    return out


def parse_signals(change: dict) -> dict:
    """Non-label bot signals on the current patchset.

    needs_rebase: hpdd-checkpatch 'cannot be cherry-picked' (this host is
    CHERRY_PICK submit type everywhere, so this is THE needs-rebase signal).
    unique_failure: gerrit janitor flagged test failures unique to this
    patch. Every janitor outcome message is a complete verdict of its
    run, so a later success ('Initial testing succeeded' / 'Testing has
    completed Successfully') or a later failure round WITHOUT the
    unique-failure block clears the flag.

    The janitor only runs on real code changes → ci_relevant_ps; checkpatch
    re-runs on every upload → current patchset (with ci_ps fallback while
    its verdict is still pending).
    """
    ps = ci_relevant_ps(change)
    cur = current_ps(change)
    checkpatch_ps = cur if any(
        _author_name(m) == CHECKPATCH_BOT and m.get("_revision_number", 0) == cur
        for m in _messages(change)) else ps
    needs_rebase = False
    unique_failure = False
    rebase_when = ""
    unique_when = ""
    janitor = "NONE"
    unique_tests: list[dict] = []
    janitor_url = ""
    janitor_fails = 0
    janitor_compile_fail = False
    for m in _messages(change):
        author = _author_name(m)
        text = m.get("message", "")
        if m.get("_revision_number", 0) != (checkpatch_ps if author == CHECKPATCH_BOT else ps):
            continue
        if author == CHECKPATCH_BOT:
            # Every checkpatch message is a fresh run verdict: a later
            # clean run ("Looks good to me." / vote removal) clears an
            # earlier cherry-pick refusal on the same PS.
            if "cannot be cherry-picked" in text:
                needs_rebase = True
                rebase_when = m.get("date", "")
            else:
                needs_rebase = False
        if author == JANITOR_BOT:
            if "failures unique to this patch" in text:
                unique_failure = True
                unique_when = m.get("date", "")
                janitor = "FAIL"
                unique_tests = _janitor_unique_tests(text)
                janitor_compile_fail = False
            elif ("Initial testing succeeded" in text
                  or "Testing has completed Successfully" in text):
                unique_failure = False
                unique_tests = []
                janitor = "OK"
                janitor_compile_fail = False
            elif ("Initial testing failed" in text
                  or "Testing has completed with errors" in text):
                # Run has failures but none unique to this patch — the
                # branch-wide known failures that fail everywhere.
                unique_failure = False
                unique_tests = []
                janitor = "ERRORS"
                janitor_compile_fail = False
            elif ": Compile failed" in text:
                # No tests ran, so this says nothing about a standing
                # unique-failure verdict — never clear one, and don't
                # mask it in the run state either.
                if janitor == "FAIL":
                    continue
                janitor = "ERRORS"
                janitor_compile_fail = True
            else:
                # Build-start notes, crash annotations, one-liners: not
                # an outcome verdict.
                continue
            janitor_fails = len(_JANITOR_ITEM_RE.findall(text))
            url_m = _JANITOR_URL_RE.search(text)
            if url_m:
                janitor_url = url_m.group(0)
    return {
        "needs_rebase": needs_rebase,
        "needs_rebase_when": rebase_when,
        "unique_failure": unique_failure,
        "unique_failure_when": unique_when,
        "janitor": janitor,
        "unique_tests": unique_tests,
        "janitor_url": janitor_url,
        "janitor_fails": janitor_fails,
        "janitor_compile_fail": janitor_compile_fail,
    }


def verified_gate(change: dict, override_emails: "list[str] | tuple" = ()) -> str:
    """OK | FAIL | PENDING — the CI gate.

    Gerrit's own DefaultSubmitRule marks Verified OK on ANY single +1
    (verified live: jenkins-only +1 with tests still running/failing
    yields submit_records OK), so the server record is only trusted for
    REJECT.  The workflow gate is: jenkins +1 (build) AND Maloo +1
    (tests); a +1 from one of override_emails may substitute a *missing*
    Maloo vote, and no Verified -1 from anyone.
    """
    entries = ((change.get("labels") or {}).get("Verified") or {}).get("all") or []
    any_neg = any(isinstance(e.get("value"), int) and e["value"] < 0 for e in entries)
    for rec in change.get("submit_records") or []:
        for lab in rec.get("labels") or []:
            if lab.get("label") == "Verified" and lab.get("status") == "REJECT":
                any_neg = True
    if any_neg:
        return "FAIL"
    jenkins = _bot_vote(change, JENKINS_BOT)
    maloo = _bot_vote(change, MALOO_BOT)
    if jenkins == 1 and (maloo == 1 or _maloo_override(change, override_emails)):
        return "OK"
    return "PENDING"


def verified_negatives(change: dict) -> list[dict]:
    """Accounts with a Verified -1 (vetoes; includes humans)."""
    out = []
    for e in ((change.get("labels") or {}).get("Verified") or {}).get("all") or []:
        v = e.get("value")
        if isinstance(v, int) and v < 0:
            out.append({"name": (e.get("name") or "").strip(), "value": v,
                        "date": e.get("date", "")})
    return out


def _maloo_override(change: dict, emails: "list[str] | tuple" = ()) -> bool:
    """A trusted human +1 standing in for a MISSING test-bot vote."""
    if not emails:
        return False
    allowed = {e.lower() for e in emails}
    for e in ((change.get("labels") or {}).get("Verified") or {}).get("all") or []:
        if (e.get("email") or "").lower() in allowed and e.get("value") == 1:
            return True
    return False


def human_review_votes(change: dict) -> list[dict]:
    """Non-bot Code-Review votes on the current patchset."""
    votes = []
    for e in ((change.get("labels") or {}).get("Code-Review") or {}).get("all") or []:
        v = e.get("value")
        if not isinstance(v, int) or v == 0:
            continue
        name = (e.get("name") or "").strip()
        if is_bot(e):
            continue
        votes.append({
            "name": name,
            "username": e.get("username", ""),
            "account_id": e.get("_account_id"),
            "value": v,
            "date": e.get("date", ""),
        })
    return votes


def my_current_vote(change: dict, account_id: int) -> int:
    for e in ((change.get("labels") or {}).get("Code-Review") or {}).get("all") or []:
        if e.get("_account_id") == account_id:
            v = e.get("value")
            return v if isinstance(v, int) else 0
    return 0


def am_reviewer(change: dict, account_id: int) -> bool:
    for e in ((change.get("labels") or {}).get("Code-Review") or {}).get("all") or []:
        if e.get("_account_id") == account_id:
            return True
    for r in (change.get("reviewers") or {}).get("REVIEWER") or []:
        if r.get("_account_id") == account_id:
            return True
    return False


def my_prior_vote(change: dict, account_id: int) -> dict | None:
    """My newest Code-Review vote on an OLDER patchset, from messages.

    New patchsets with real code changes outdate votes silently (the
    label drops to 0 with no trace in labels[]), so this is the only
    way to see 'I already reviewed this, my vote got wiped'.
    Returns {ps, value, date} or None.
    """
    ps_now = current_ps(change)
    best = None
    for m in _messages(change):
        if (m.get("author") or {}).get("_account_id") != account_id:
            continue
        text = m.get("message", "").strip()
        removed = _VOTE_REMOVED_RE.match(text)
        if removed:
            # An explicit vote removal supersedes the vote it cleared.
            if best is not None and int(removed.group(1)) >= best["ps"]:
                best = None
            continue
        match = _PRIOR_VOTE_RE.match(text)
        if not match:
            continue
        ps, value = int(match.group(1)), int(match.group(2))
        if ps >= ps_now:
            continue
        if best is None or ps >= best["ps"]:
            best = {"ps": ps, "value": value, "date": m.get("date", "")}
    return best


def patchset_history(change: dict) -> list[dict]:
    """Upload timeline reconstructed from newPatchSet messages.

    Returns [{ps, date, uploader}] sorted by patchset — no extra API
    call: the autogenerated upload messages ride along in o=MESSAGES.
    """
    seen: dict[int, dict] = {}
    for m in _messages(change):
        tag = m.get("tag") or ""
        if not tag.startswith("autogenerated:gerrit:newPatchSet"):
            continue
        ps = m.get("_revision_number", 0)
        if ps and ps not in seen:
            seen[ps] = {
                "ps": ps,
                "date": m.get("date", ""),
                "uploader": (m.get("author") or {}).get("name", ""),
            }
    return [seen[ps] for ps in sorted(seen)]


def pending_reviewers(change: dict, account_id: int) -> list[str]:
    """Human reviewers added to the change who have not voted yet."""
    owner_id = (change.get("owner") or {}).get("_account_id")
    out = []
    for e in ((change.get("labels") or {}).get("Code-Review") or {}).get("all") or []:
        v = e.get("value")
        if isinstance(v, int) and v != 0:
            continue
        acc = e.get("_account_id")
        if acc in (owner_id, account_id) or is_bot(e) or is_review_bot(e):
            continue
        name = (e.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def last_human_activity(change: dict, account_id: int) -> dict:
    """Dates of the last non-bot message by me vs by others."""
    mine = ""
    others = ""
    others_name = ""
    for m in _messages(change):
        author = m.get("author") or {}
        if is_bot(author):
            continue
        tag = m.get("tag") or ""
        if tag.startswith("autogenerated:gerrit:setHashtag"):
            continue
        date = m.get("date", "")
        if author.get("_account_id") == account_id:
            if date > mine:
                mine = date
        else:
            if date > others:
                others = date
                others_name = author.get("name", "")
    return {"me": mine, "others": others, "others_name": others_name}
