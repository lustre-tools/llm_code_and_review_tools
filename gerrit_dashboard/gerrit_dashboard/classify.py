"""Attention rules engine: raw Gerrit bundle → dashboard snapshot.

Priorities: 0 = act now, 1 = your turn, 2 = should act soon,
3 = informational.  A change can trigger several rules; it is shown in
the "Needs your action" section iff its best rule is P0/P1.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .review_rules import is_backport, review_state

from . import ci_parse
from .config import Config
from .store import SNAPSHOT_SCHEMA


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_record(change: dict, bundle: dict, config: Config) -> dict:
    """Derive the compact per-change record the template consumes."""
    my_id = bundle["self"]["account_id"]
    number = change.get("_number")
    ps = ci_parse.current_ps(change)
    rev = ci_parse.current_revision(change)
    uploader = rev.get("uploader") or {}
    build = ci_parse.parse_build(change)
    tests = ci_parse.parse_tests(change)
    signals = ci_parse.parse_signals(change)
    humans = ci_parse.human_review_votes(change)
    my_vote = ci_parse.my_current_vote(change, my_id)
    prior = ci_parse.my_prior_vote(change, my_id)
    activity = ci_parse.last_human_activity(change, my_id)
    try:
        rs = review_state(change, config.review_threshold,
                          config.backport_review_threshold)
    except Exception:
        rs = {"plus_one_count": 0, "plus_two_count": 0, "has_any_negative": False,
              "pass": False, "threshold": config.review_threshold}
    try:
        backport = is_backport(change)
    except Exception:
        backport = False

    # Landing queue is branch-specific — <branch>-next for <branch> —
    # signalled either by a hashtag or by the Change-Id sitting on the
    # <branch>-next staging branch (see Config.next_queues).
    hashtags = change.get("hashtags") or []
    landing_tag = f"{change.get('branch', '')}-next"
    staged = bundle.get("next_queues", {}).get(
        (change.get("project"), change.get("branch")), set())
    queued = landing_tag in hashtags or change.get("change_id") in staged
    owner = change.get("owner") or {}
    threads = bundle.get("threads", {}).get(number)
    watch_note = ""
    for e in bundle.get("watchlist", []):
        if e.get("number") == number:
            watch_note = e.get("note", "")

    return {
        "number": number,
        "url": f"{config.gerrit_base_url}/c/{change.get('project', '')}/+/{number}",
        "subject": change.get("subject", ""),
        "project": change.get("project", ""),
        "branch": change.get("branch", ""),
        "status": change.get("status", ""),
        "wip": bool(change.get("work_in_progress")),
        "ps": ps,
        "ci_ps": ci_parse.ci_relevant_ps(change),
        "ps_created": rev.get("created", ""),
        "ps_age": ci_parse.age_str(rev.get("created")),
        "updated": change.get("updated", ""),
        "updated_age": ci_parse.age_str(change.get("updated")),
        "owner": owner.get("name", ""),
        "owner_id": owner.get("_account_id"),
        "uploader": uploader.get("name", ""),
        "uploader_id": uploader.get("_account_id"),
        "starred": number in bundle.get("starred", set()),
        "hidden": number in bundle.get("hidden", set()),
        "queued": queued,
        "queued_tag": landing_tag,
        "hashtags": [h for h in hashtags if h != landing_tag],
        "unresolved": change.get("unresolved_comment_count", 0),
        "insertions": change.get("insertions", 0),
        "deletions": change.get("deletions", 0),
        "size_bucket": _size_bucket(change.get("insertions", 0) + change.get("deletions", 0)),
        "topic": change.get("topic", ""),
        "ps_history": ci_parse.patchset_history(change),
        "pending_reviewers": ci_parse.pending_reviewers(change, my_id),
        "backport": backport,
        "build": build,
        "test": tests,
        "gate": ci_parse.verified_gate(change, config.verified_override_emails),
        "verified_neg": ci_parse.verified_negatives(change),
        "review": {
            "humans": humans,
            "plus1": rs["plus_one_count"],
            "plus2": rs["plus_two_count"],
            "neg": rs["has_any_negative"],
            "pass": rs["pass"],
            "threshold": rs["threshold"],
            "my_vote": my_vote,
            "prior_vote": prior,
        },
        "signals": signals,
        # The unique-failure flag is only an ALERT while it is an open
        # problem: a later Maloo PASS supersedes it and an in-flight
        # retest is already reported as retest-running.  Template alarm
        # styling and the P0 rule both key on this, so they can't drift.
        "janitor_alert": (signals["unique_failure"]
                          and tests["state"] != "PASS"
                          and not tests["all_failures_retesting"]),
        "threads": threads,
        # Unanswered @-pings at me in unresolved threads (see
        # build_thread_buckets): the signal that drowns in Gerrit email.
        "pings": (threads or {}).get("ping_items") or [],
        "ping_count": (threads or {}).get("pings", 0),
        "ping_open": (threads or {}).get("pings_open", 0),
        "activity": activity,
        "watch_note": watch_note,
        # Already fetched via o=CURRENT_COMMIT (needed for is_backport) —
        # carrying it into the snapshot costs zero extra API calls.
        "commit_msg": (rev.get("commit") or {}).get("message", ""),
        "items": [],  # attention items, filled by classify_record
    }


def _size_bucket(total: int) -> str:
    """Gerrit's size labels: XS <10, S <50, M <250, L <1000, XL beyond."""
    for bucket, limit in (("XS", 10), ("S", 50), ("M", 250), ("L", 1000)):
        if total < limit:
            return bucket
    return "XL"


def _add(rec: dict, prio: int, rule: str, reason: str, when: str = "") -> None:
    rec["items"].append({"prio": prio, "rule": rule, "reason": reason,
                         "when": when or rec.get("updated", "")})


def classify_record(rec: dict, roles: set[str], config: Config) -> None:
    """Attach attention items and a display group to the record."""
    responsible = bool(roles & {"mine", "carry"})
    is_reviewer = "review" in roles and not responsible

    # Relationship to the patch — mixed lists (Action, Watchlist, Merged)
    # show this so urgency can be judged: my own patch vs one I carry vs
    # one I merely review.
    if rec["owner_id"] == rec.get("my_id"):
        rec["role"] = "mine"
    elif rec.get("uploader_id") == rec.get("my_id"):
        rec["role"] = "carrying"
    elif "review" in roles:
        rec["role"] = "reviewing"
    elif "cc" in roles:
        rec["role"] = "cc"
    elif "watch" in roles:
        rec["role"] = "watching"
    else:
        rec["role"] = ""

    if rec["status"] == "MERGED":
        if "watch" in roles:
            _add(rec, 3, "merged", "merged — can be removed from the watchlist")
        rec["group"] = "done"
        _finish(rec, config)
        return
    if rec["status"] == "ABANDONED":
        if "watch" in roles:
            _add(rec, 3, "abandoned", "abandoned — can be removed from the watchlist")
        rec["group"] = "done"
        _finish(rec, config)
        return

    # A direct @-ping is your turn whatever the role — even CC-only or
    # parked patches; people who ping expect an answer.  A ping lives
    # until the THREAD IS RESOLVED: while you have not replied it is a
    # P1 act-now item; after your reply it stays listed (informational)
    # because a "will look tomorrow" answer does not close anything.
    if rec["pings"]:
        waiting = [p for p in rec["pings"] if not p.get("answered")]
        if waiting:
            newest = max(waiting, key=lambda p: p.get("updated", ""))
            more = f" (+{rec['ping_count'] - 1} more)" if rec["ping_count"] > 1 else ""
            _add(rec, 1, "mentioned",
                 f"{newest.get('author', 'someone')} pinged you{more}: "
                 f"\u201c{newest.get('snippet', '')[:90]}\u201d",
                 when=newest.get("updated", ""))
        else:
            newest = max(rec["pings"], key=lambda p: p.get("updated", ""))
            _add(rec, 3, "mentioned-open",
                 f"open ping from {newest.get('author', 'someone')} — "
                 f"you replied, thread not resolved yet",
                 when=newest.get("updated", ""))

    # My own -1 on a change I own or carry is "parked by me", not attention.
    my_neg_on_own = responsible and rec["review"]["my_vote"] < 0

    if responsible:
        parked = rec["wip"] or my_neg_on_own
        if rec["wip"]:
            _add(rec, 4, "parked", "work in progress")
        elif my_neg_on_own:
            _add(rec, 4, "parked", "parked by your own -1")

        if not parked:
            if rec["signals"]["needs_rebase"]:
                _add(rec, 0, "needs-rebase", "needs rebase — checkpatch: cannot be cherry-picked",
                     when=rec["signals"]["needs_rebase_when"])
            if rec["build"]["state"] in ("FAILURE", "ABORTED"):
                num = rec["build"]["number"]
                _add(rec, 0, "build-failed",
                     f"build {rec['build']['state'].lower()}" + (f" (#{num})" if num else ""),
                     when=rec["build"]["when"])
            # Janitor unique-failure only while it is an open problem:
            # self-healed (tests PASS) says nothing anymore, and an
            # in-flight retest is already reported by retest-running.
            if rec["janitor_alert"]:
                uniq = rec["signals"]["unique_tests"]
                reason = "janitor: new test failures unique to this patch"
                if uniq:
                    names = ", ".join(u["test"] for u in uniq[:3])
                    if len(uniq) > 3:
                        names += f" +{len(uniq) - 3} more"
                    plural = "s" if len(uniq) != 1 else ""
                    reason = (f"janitor: {len(uniq)} test failure{plural} "
                              f"unique to this patch — {names}")
                _add(rec, 0, "unique-failure", reason,
                     when=rec["signals"]["unique_failure_when"])
            # A Verified -1 not already explained by build/test parsing is
            # a veto (human, or a bot state we could not parse) — surface it.
            if (rec["gate"] == "FAIL"
                    and rec["build"]["state"] not in ("FAILURE", "ABORTED")
                    and rec["test"]["state"] != "FAIL"):
                for veto in rec["verified_neg"]:
                    _add(rec, 0, "verified-veto",
                         f"Verified {veto['value']:+d} veto by {veto['name']}",
                         when=veto.get("date", ""))
            if rec["test"]["state"] == "FAIL":
                names = ", ".join(t["name"] for t in rec["test"]["failed_tests"])
                if rec["test"]["all_failures_retesting"]:
                    _add(rec, 2, "retest-running",
                         f"{names} failed — auto-retest in flight (often self-heals)",
                         when=rec["test"]["when"])
                elif names:
                    _add(rec, 0, "test-failed", f"enforced failed: {names}", when=rec["test"]["when"])
                else:
                    _add(rec, 0, "test-failed", "Maloo voted -1 — tests failed (details on the change)",
                         when=rec["test"]["when"])
            # A human -1 outranks comment threads as the headline.
            for v in rec["review"]["humans"]:
                if v["value"] < 0 and v.get("account_id") != rec.get("my_id"):
                    _add(rec, 1, "negative-review", f"{v['name']} voted {v['value']:+d}",
                         when=v.get("date", ""))
            th = rec["threads"] or {}
            review_green = rec["review"]["pass"]
            if th.get("my_turn"):
                who = ""
                for item in th.get("items", []):
                    if item["kind"] == "my_turn":
                        who = item["author"]
                        break
                suffix = f" — last from {who}" if who else ""
                when = max((i.get("updated", "") for i in th.get("items", [])
                            if i["kind"] == "my_turn"), default="")
                if review_green:
                    # Enough +1s: comments are notes to fold into the next
                    # respin (if one ever happens), not a blocker — and
                    # below 'ready', so green rows headline as ready.
                    _add(rec, 4, "comments-later",
                         f"{th['my_turn']} reviewer comment(s) to address if a respin is needed{suffix}",
                         when=when)
                else:
                    _add(rec, 1, "feedback",
                         f"{th['my_turn']} unresolved thread(s) await your reply{suffix}", when=when)

            if rec["gate"] == "OK" and not rec["review"]["humans"]:
                age_days = _age_days(rec["ps_created"])
                if age_days is not None and age_days >= config.nudge_days:
                    if rec["pending_reviewers"]:
                        _add(rec, 2, "needs-reviewers",
                             "CI green — no vote yet from " + ", ".join(rec["pending_reviewers"][:4]))
                    else:
                        _add(rec, 2, "needs-reviewers", "CI green, no reviews yet — add reviewers")
            elif (rec["gate"] == "OK" and not rec["review"]["pass"]
                    and rec["pending_reviewers"]):
                # Partially reviewed and green: name who still owes a vote.
                _add(rec, 4, "awaiting-votes",
                     "no vote yet from " + ", ".join(rec["pending_reviewers"][:4]))
            if rec["gate"] == "OK" and rec["review"]["pass"]:
                if rec["queued"]:
                    _add(rec, 3, "ready", f"all green — queued for landing ({rec['queued_tag']})")
                else:
                    _add(rec, 3, "ready", f"all green — ready to land (not in {rec['queued_tag']} yet)")
            if rec["build"]["state"] == "BUILDING":
                _add(rec, 3, "in-ci", "build running")
            elif (rec["build"]["state"] == "SUCCESS" and rec["gate"] != "OK"
                  and rec["test"]["state"] in ("RUNNING", "NONE")):
                _add(rec, 3, "in-ci", "build OK — testing in progress (typically 6–14h)")
            if th.get("their_turn"):
                _add(rec, 3, "waiting", f"{th['their_turn']} thread(s) waiting on others")

    if is_reviewer and not rec["wip"]:
        if rec["review"]["my_vote"] != 0:
            _add(rec, 4, "reviewed", f"you voted {rec['review']['my_vote']:+d} on current PS")
        else:
            prior = rec["review"]["prior_vote"]
            if prior:
                prio = 1 if prior["value"] < 0 else 2
                _add(rec, prio, "re-review",
                     f"PS{prior['ps']}→PS{rec['ps']} since your {prior['value']:+d}"
                     + (" — your objection was dropped" if prior["value"] < 0 else ""),
                     when=rec["ps_created"])
            elif rec["gate"] == "OK":
                _add(rec, 2, "review-requested", "CI green — awaiting your review")
            else:
                _add(rec, 3, "review-later", "awaiting review (CI not green yet)")

    _assign_group(rec, roles)
    _finish(rec, config)


def _finish(rec: dict, config: Config) -> None:
    rec["items"].sort(key=lambda i: i["prio"])
    rec["top_prio"] = rec["items"][0]["prio"] if rec["items"] else 5
    rec["top_reason"] = rec["items"][0]["reason"] if rec["items"] else ""
    # Freshness of the most urgent signal, for the recent/longstanding split.
    urgent = [i for i in rec["items"] if i["prio"] <= 1]
    rec["attn_when"] = max((i.get("when", "") for i in urgent), default="") or rec.get("updated", "")
    # Row color tone: red = act now, amber = your turn, green = done/ready,
    # blue = machines working, none = neutral.
    if rec["top_prio"] == 0:
        rec["tone"] = "p0"
    elif rec["items"] and rec["items"][0]["rule"] == "negative-review":
        rec["tone"] = "p0"  # a human -1 reads red, even at P1 ranking
    elif rec["top_prio"] == 1:
        rec["tone"] = "p1"
    elif rec["status"] == "MERGED" or rec.get("group") == "landing":
        rec["tone"] = "ok"
    elif rec.get("group") == "in_ci":
        rec["tone"] = "run"
    else:
        rec["tone"] = ""
    age_days = _age_days(rec["updated"])
    rec["stalled"] = bool(age_days is not None and age_days >= config.stale_days
                          and rec["status"] == "NEW" and rec.get("group") != "parked")
    rec.pop("my_id", None)


def _age_days(ts: str) -> float | None:
    dt = ci_parse.parse_gerrit_ts(ts)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _assign_group(rec: dict, roles: set[str]) -> None:
    """Display group inside the My patches / Carrying / Reviews sections."""
    rules = {i["rule"] for i in rec["items"]}
    p0_rules = {i["rule"] for i in rec["items"] if i["prio"] == 0}
    responsible = bool(roles & {"mine", "carry"})
    if responsible:
        if "parked" in rules:
            rec["group"] = "parked"
        elif p0_rules & {"needs-rebase", "build-failed", "unique-failure", "test-failed", "verified-veto"}:
            rec["group"] = "failed"
        elif "ready" in rules or (rec["queued"] and rec["status"] == "NEW"):
            rec["group"] = "landing"
        elif rules & {"feedback", "negative-review"}:
            rec["group"] = "feedback"
        elif "needs-reviewers" in rules:
            rec["group"] = "needs_reviewers"
        elif rules & {"in-ci", "retest-running"}:
            rec["group"] = "in_ci"
        else:
            rec["group"] = "waiting"
    elif "review" in roles:
        if rec["wip"]:
            rec["group"] = "done"  # WIP: owner is not asking for review yet
        elif "re-review" in rules:
            rec["group"] = "re_review"
        elif "review-requested" in rules:
            rec["group"] = "requested"
        elif "reviewed" in rules:
            rec["group"] = "done"
        else:
            rec["group"] = "later"
    else:
        rec["group"] = "other"


def build_snapshot(bundle: dict, config: Config) -> dict:
    """Full pipeline: bundle → template-ready snapshot dict."""
    records: list[dict] = []
    for number, change in bundle["changes"].items():
        roles = bundle["roles"].get(number, set())
        rec = build_record(change, bundle, config)
        rec["my_id"] = bundle["self"]["account_id"]
        rec["roles"] = sorted(roles)
        classify_record(rec, roles, config)
        records.append(rec)

    # All row lists sort by last-modified, newest first;
    # urgency is carried by the row tone, not the ordering.
    all_action = [r for r in records
                  if r["top_prio"] <= 1 and r["status"] == "NEW" and not r["hidden"]]
    all_action.sort(key=lambda r: r["updated"], reverse=True)
    # Fresh signals get the spotlight; failures that have been sitting
    # for a while move to a collapsed "longstanding" list so the triage
    # view stays readable even with many broken patches in flight.
    action = [r for r in all_action
              if (_age_days(r["attn_when"]) or 0) <= config.action_recent_days]
    action_old = [r for r in all_action
                  if (_age_days(r["attn_when"]) or 0) > config.action_recent_days]

    def sect(role: str, groups: list[str]) -> dict:
        out = {g: [] for g in groups}
        for r in records:
            if role in r["roles"] and r.get("group") in out:
                out[r["group"]].append(r)
        for g in groups:
            out[g].sort(key=lambda r: r["updated"], reverse=True)
        return out

    mine = sect("mine", ["failed", "feedback", "in_ci", "needs_reviewers", "waiting", "landing", "parked"])
    carry = sect("carry", ["failed", "feedback", "in_ci", "needs_reviewers", "waiting", "landing", "parked"])

    def by_tag(role: str) -> dict:
        """Alternative grouping: personal hashtags → collapsible clusters.
        Multi-tag changes appear under each tag; untagged ones last."""
        tags: dict[str, list] = {}
        for r in records:
            if role not in r["roles"] or r["status"] != "NEW":
                continue
            for t in (r["hashtags"] or ["(untagged)"]):
                tags.setdefault(t, []).append(r)
        for rows in tags.values():
            rows.sort(key=lambda r: r["updated"], reverse=True)
        ordered = sorted((t for t in tags if t != "(untagged)"), key=str.lower)
        if "(untagged)" in tags:
            ordered.append("(untagged)")
        return {t: tags[t] for t in ordered}
    reviews = sect("review", ["re_review", "requested", "later", "done"])
    # A change I both carry and review shows under carrying only.
    for g in list(reviews):
        reviews[g] = [r for r in reviews[g] if not ({"mine", "carry"} & set(r["roles"]))]

    watch_rows = [r for r in records if "watch" in r["roles"]]
    watch_rows.sort(key=lambda r: r["updated"], reverse=True)

    ping_rows = [r for r in records if r["pings"]]
    # Rows still waiting on a reply come first, newest ping first within
    # each half.
    ping_rows.sort(key=lambda r: (bool(r["ping_count"]),
                                  max(p.get("updated", "") for p in r["pings"])),
                   reverse=True)

    merged_rows = [r for r in records if "merged" in r["roles"] and r["status"] == "MERGED"]
    merged_rows.sort(key=lambda r: r["updated"], reverse=True)

    # CC-only: informational — changes someone put on your radar without
    # asking for review; anything you also own or carry shows there instead.
    cc_rows = [r for r in records
               if "cc" in r["roles"] and not ({"mine", "carry"} & set(r["roles"]))
               and r["status"] == "NEW"]
    cc_rows.sort(key=lambda r: r["updated"], reverse=True)

    def visible(rows: list) -> int:
        return sum(1 for r in rows if not r["hidden"])

    in_ci_count = visible(mine["in_ci"]) + visible(carry["in_ci"])
    landing_count = visible(mine["landing"]) + visible(carry["landing"])

    branch_counts: dict[str, int] = {}
    for r in records:
        if r.get("branch"):
            branch_counts[r["branch"]] = branch_counts.get(r["branch"], 0) + 1
    branches = [{"name": b, "count": branch_counts[b]}
                for b in sorted(branch_counts, key=lambda b: (b != "master", b))]

    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        # The allowlist this snapshot was built under; load_snapshot
        # rejects a mismatch so restricted instances never serve rows
        # persisted by an unrestricted run (and vice versa).
        "projects": sorted(config.projects),
        "branches": branches,
        "branch_colors": dict(config.branch_colors),
        "generated_at": _iso(bundle["fetched_at"]),
        "fetched_at": bundle["fetched_at"],
        "self": bundle["self"],
        "errors": bundle.get("errors", []),
        "kpis": {
            "action": len(action),
            "action_old": len(action_old),
            "reviews": visible(reviews["re_review"]) + visible(reviews["requested"]),
            "in_ci": in_ci_count,
            "landing": landing_count,
            "watch": len(watch_rows),
            "pings": sum(r["ping_count"] for r in ping_rows if not r["hidden"]),
            "pings_open": sum(r["ping_open"] for r in ping_rows if not r["hidden"]),
            "merged": len(merged_rows),
            "mine_open": sum(visible(g) for g in mine.values()),
            "carry_open": sum(visible(g) for g in carry.values()),
            "hidden": sum(1 for r in records if r["hidden"]),
        },
        "action": action,
        "action_old": action_old,
        "watch": watch_rows,
        "pinged": ping_rows,
        "mine": mine,
        "carry": carry,
        "mine_by_tag": by_tag("mine"),
        "carry_by_tag": by_tag("carry"),
        "reviews": reviews,
        "merged": merged_rows,
        "cc": cc_rows,
    }
    payload = json.dumps(_strip_volatile(snapshot), sort_keys=True, default=str)
    snapshot["snapshot_id"] = hashlib.sha1(payload.encode()).hexdigest()[:12]
    return snapshot


_VOLATILE_KEYS = {"generated_at", "fetched_at", "ps_age", "updated_age", "stalled"}


def _strip_volatile(obj):
    """Drop clock-derived fields recursively so snapshot_id only changes
    when Gerrit data changes (a changed id makes the page reload)."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj
