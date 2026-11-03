"""Weigh reviewer candidates by how much review they already carry.

reviewer-candidates ranks by who knows the files, so the same few experts
came top for every change in an area, and a bot adding reviewers asked
them again and again: over six weeks four people took a third of its
asks, one with 23 of them still unanswered, while others were asked a
dozen times and never answered one.  This module adds what the history
of the files cannot say:

- Gerrit's load: recent open changes waiting on the person's vote against
  the votes they cast lately, a queue against a rate.
- The caller's own asks (--asked FILE): how many are still waiting on the
  person, how many came lately, and how often they were answered.

Expertise stays the first thing; the weights only reorder people who
know the code, and a hold marks someone not to ask now.
"""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from .reviewer_candidates import normalize_name

# Gerrit-wide load is read over changes updated in this many days.
LOAD_DAYS = 30
# Gerrit's page cap here; a count that reaches it is at least this.
LOAD_COUNT_CAP = 500
LOAD_WORKERS = 4

# How many of the caller's asks someone can carry: about three days of the
# Code-Review votes they cast, and never less than CAPACITY_MIN.  People who
# review the most are asked the most; this only keeps them from being buried.
CAPACITY_DAYS = 3
CAPACITY_MIN = 3
# Held when this many asks wait on them, or a week of their votes if more.
HOLD_PENDING = 6
HOLD_PENDING_DAYS = 7
# Asked at least this many times and answered none, and fewer than this many
# votes on Gerrit in the window: they seldom review, for us or anyone.
HOLD_UNANSWERED_ASKS = 4
HOLD_UNANSWERED_REVIEWED = 20
# Someone who seldom reviews still gets an ask now and then: held only this
# long after the last one, and past it their queue and answers weigh no
# less than SELDOM_FLOOR, so they come round in turn.
SELDOM_COOLDOWN_DAYS = 14
SELDOM_FLOOR = 0.5
# A Reviewed-by on this share of the commits, and at least this many, is the
# gatekeeper who lands them, not an expert in these files.
GATEKEEPER_SHARE = 0.8
GATEKEEPER_REVIEWS = 10


class AskedError(ValueError):
    pass


def resolve_account(client, candidate):
    """The candidate's Gerrit account id, or None: by each email the
    history has for them, then by an exact name with one match."""
    emails = list(dict.fromkeys(
        [candidate.get("email") or ""]
        + list((candidate.get("aliases") or {}).get("emails") or [])))
    for email in emails:
        if "@" not in email:
            continue
        found = client.search_accounts(email=email, limit=2) or []
        if len(found) == 1 and found[0].get("_account_id"):
            return found[0]["_account_id"]
    name = candidate.get("name") or ""
    if name:
        found = client.search_accounts(name=f'"{name}"', limit=5) or []
        exact = [a for a in found
                 if normalize_name(a.get("name", "")) == normalize_name(name)]
        if len(exact) == 1 and exact[0].get("_account_id"):
            return exact[0]["_account_id"]
    return None


def _count(client, query):
    found = client.search_all(query, max_results=LOAD_COUNT_CAP,
                              page_size=LOAD_COUNT_CAP,
                              options=["SKIP_DIFFSTAT"])
    return len(found)


def account_load(client, account_id, days=LOAD_DAYS):
    """{waiting, reviewed} over changes updated in the window: open
    changes they review, not their own, with no vote of theirs; and
    changes carrying a Code-Review vote of theirs."""
    who = int(account_id)
    age = f"-age:{days}d"
    waiting = _count(client, (
        f"reviewer:{who} status:open -owner:{who} -is:wip {age} "
        f"-label:Code-Review=ANY,user={who}"))
    reviewed = _count(client, f"label:Code-Review=ANY,user={who} {age}")
    load = {"window_days": days, "waiting": waiting, "reviewed": reviewed}
    if LOAD_COUNT_CAP in (waiting, reviewed):
        load["capped_at"] = LOAD_COUNT_CAP
    return load


def add_gerrit_load(client, candidates, workers=LOAD_WORKERS):
    """Set account_id and load on each candidate; a failure leaves that
    candidate's load null with load_error, and the rest go on."""
    def one(candidate):
        try:
            account = resolve_account(client, candidate)
            candidate["account_id"] = account
            candidate["load"] = (account_load(client, account)
                                 if account is not None else None)
            if account is None:
                candidate["load_error"] = "no unambiguous Gerrit account"
        except Exception as e:  # one person's lookup must not sink the list
            candidate.setdefault("account_id", None)
            candidate["load"] = None
            candidate["load_error"] = str(e)[:200]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, candidates))


def read_asked(path):
    """The asks file: {"by": ..., "people": [{account_id, name, email,
    asked, answered, pending, asked_14d, last_asked, ...}]}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise AskedError(f"cannot read --asked {path}: {e}") from e
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list):
        raise AskedError(f"--asked {path} has no people list")
    return data


def _asked_index(people):
    index = {}
    for person in people:
        if not isinstance(person, dict):
            continue
        if person.get("account_id") is not None:
            index.setdefault(("a", int(person["account_id"])), person)
        for email in [person.get("email")] + list(person.get("emails") or []):
            if email:
                index.setdefault(("e", email.strip().lower()), person)
        if person.get("name"):
            index.setdefault(("n", normalize_name(person["name"])), person)
    return index


def match_asked(candidate, index):
    """The candidate's asks record: by account, then email, then name."""
    if candidate.get("account_id") is not None:
        found = index.get(("a", int(candidate["account_id"])))
        if found:
            return found
    emails = [candidate.get("email")] + list(
        (candidate.get("aliases") or {}).get("emails") or [])
    for email in emails:
        if email and ("e", email.lower()) in index:
            return index[("e", email.lower())]
    names = [candidate.get("name")] + list(
        (candidate.get("aliases") or {}).get("names") or [])
    for name in names:
        if name and ("n", normalize_name(name)) in index:
            return index[("n", normalize_name(name))]
    return None


def _days_since(stamp, now):
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - then).total_seconds() / 86400


def weigh(candidate, now=None):
    """(balanced, factors, holds) for one candidate.

    balanced = sqrt(score) x queue x asks x answers.  The square root
    keeps a big expertise gap decisive while letting load decide among
    people who all know the code.
    """
    now = now or datetime.now(UTC)
    factors = {}
    holds = []
    seldom = None
    balanced = math.sqrt(max(0, candidate.get("score") or 0))
    if ((candidate.get("review_share") or 0) >= GATEKEEPER_SHARE
            and (candidate.get("reviewed") or 0) >= GATEKEEPER_REVIEWS):
        holds.append("on nearly every commit: the gatekeeper who lands them")
    load = candidate.get("load")
    reviewed = None
    if load:
        waiting, reviewed = load["waiting"], load["reviewed"]
        factors["queue"] = round((reviewed + 1) / (reviewed + 1 + waiting), 2)
        if reviewed == 0:
            seldom = f"no Code-Review vote on Gerrit in {load['window_days']} days"
    asked = candidate.get("asked")
    if asked:
        pending = int(asked.get("pending") or 0)
        recent = int(asked.get("asked_14d") or 0)
        total = int(asked.get("asked") or 0)
        answered = int(asked.get("answered") or 0)
        rate = (reviewed or 0) / load["window_days"] if load else 0
        capacity = max(CAPACITY_MIN, rate * CAPACITY_DAYS)
        factors["asks"] = round(1 / (1 + (pending + recent / 2) / capacity), 2)
        factors["answers"] = round(min(1.0, 2 * (answered + 1) / (total + 2)), 2)
        if pending >= max(HOLD_PENDING, rate * HOLD_PENDING_DAYS):
            holds.append(f"{pending} asks already waiting on them")
        if (total >= HOLD_UNANSWERED_ASKS and answered == 0
                and (reviewed is None or reviewed < HOLD_UNANSWERED_REVIEWED)):
            seldom = f"asked {total} times, answered none"
    if seldom:
        since = _days_since((asked or {}).get("last_asked"), now)
        if since is not None and since < SELDOM_COOLDOWN_DAYS:
            holds.append(f"{seldom}; last asked {since:.0f} days ago")
        else:
            for name in ("queue", "answers"):
                if name in factors:
                    factors[name] = max(factors[name], SELDOM_FLOOR)
    for value in factors.values():
        balanced *= value
    return round(balanced, 2), factors, holds


def balance(candidates, asked=None, now=None):
    """Attach asked, balanced, factors and hold to each candidate and
    return them in balanced order, held ones last."""
    index = _asked_index((asked or {}).get("people") or [])
    for candidate in candidates:
        if asked is not None:
            record = match_asked(candidate, index)
            candidate["asked"] = ({k: record.get(k) for k in (
                "asked", "answered", "pending", "asked_14d", "last_asked",
                "median_answer_days") if k in record} if record else None)
        balanced, factors, holds = weigh(candidate, now)
        candidate["balanced"] = balanced
        candidate["factors"] = factors
        candidate["hold"] = holds or None
    return sorted(candidates, key=lambda c: (bool(c["hold"]), -c["balanced"],
                                             -(c.get("score") or 0),
                                             c.get("name") or ""))
