"""Gerrit data acquisition.

Five list queries (one HTTP round-trip each, paginated) cover every
section; /comments enrichment runs only for changes we are responsible
for and is cached by meta_rev_id (NoteDb bumps it on every update, so
an unchanged change costs nothing).

Strictly read-only: GETs only, by construction — this module never
issues anything else.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import requests
from gerrit_cli.client import GerritCommentsClient, GerritConfigError

from .ci_parse import is_bot, is_review_bot
from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_CHANGE_ID_RE = re.compile(r"^Change-Id: (I[0-9a-f]+)", re.M)

QUERY_OPTIONS = [
    "DETAILED_LABELS",
    "MESSAGES",
    "DETAILED_ACCOUNTS",
    "CURRENT_REVISION",
    "CURRENT_COMMIT",
    "SUBMITTABLE",
]

PAGE_SIZE = 200
ENRICH_WORKERS = 8
# Bump when build_thread_buckets logic changes: entries are keyed by
# meta_rev_id, so logic changes would otherwise never re-apply to
# unchanged changes.
ENRICH_VERSION = 8

# Lines that merely QUOTE a person are not pings: email-style "> ..."
# quotations and commit-message trailers pasted into comments.
_QUOTE_OR_TRAILER_RE = re.compile(
    r"^\s*>|^\s*(?:Signed-off|Reviewed|Tested|Acked|Reported|Suggested"
    r"|Co-authored|Co-developed)-by\s*:", re.I)


def mention_patterns(aliases) -> list[re.Pattern]:
    """Compile ping detectors for a person's aliases.

    A single word (username, or a one-word display name) only counts
    @-prefixed — bare in prose would be far too noisy.  An email counts
    with or without the leading @ (Gerrit's autocomplete inserts
    "@user@host").  A multi-word display name counts verbatim, since
    "First Last" rarely appears by accident outside quoted trailers.
    """
    pats = []
    for alias in aliases or ():
        alias = (alias or "").strip()
        if not alias:
            continue
        if "@" in alias:
            pats.append(re.compile(rf"(?<![\w.]){re.escape(alias)}\b", re.I))
        elif " " in alias:
            pats.append(re.compile(rf"\b{re.escape(alias)}\b", re.I))
        else:
            pats.append(re.compile(rf"(?<![\w.])@{re.escape(alias)}\b", re.I))
    return pats


def _ping_text(text: str) -> str:
    return "\n".join(line for line in (text or "").splitlines()
                     if not _QUOTE_OR_TRAILER_RE.match(line))


class NotAvailable(RuntimeError):
    """Gerrit answered definitively (4xx): absent, or not readable at all."""


class GerritFetcher:
    def __init__(self, config: Config, store: Store, target: str | None = None):
        self.config = config
        self.store = store
        # Gerrit username/account-id whose dashboard this is; None = the
        # authenticated account.  All fetching uses the authenticated
        # credentials — Gerrit data is readable across accounts.
        self.target = target
        self._tls = threading.local()
        # One long-lived pool: worker threads (and their thread-local
        # clients/sessions) persist across refreshes instead of churning.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=ENRICH_WORKERS, thread_name_prefix="gd-fetch")
        self._me: dict | None = None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _project_clause(self) -> str:
        """Query suffix restricting results to the configured projects."""
        projs = self.config.projects
        if not projs:
            return ""
        if len(projs) == 1:
            return f" project:{projs[0]}"
        return " (" + " OR ".join(f"project:{p}" for p in projs) + ")"

    # -- low level ---------------------------------------------------------

    def _client(self) -> GerritCommentsClient:
        client = getattr(self._tls, "client", None)
        if client is None:
            client = GerritCommentsClient()
            self._tls.client = client
        return client

    def _with_retry(self, fn: Callable[[], Any], what: str, tries: int = 3) -> Any:
        last_exc: Exception | None = None
        for attempt in range(tries):
            try:
                return fn()
            except GerritConfigError:
                raise  # missing credentials — retrying cannot help
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", 0)
                if 400 <= status < 500:
                    raise NotAvailable(f"{what}: HTTP {status}") from exc
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 - network layer raises many types
                last_exc = exc
            log.warning("%s failed (attempt %d/%d): %s", what, attempt + 1, tries, last_exc)
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{what}: {last_exc}") from last_exc

    def whoami(self) -> dict:
        """Resolve the dashboard's target account (cached across failures)."""
        ref = quote(str(self.target), safe="") if self.target else "self"
        try:
            me = self._with_retry(lambda: self._client().rest.get(f"/accounts/{ref}"),
                                  f"GET /accounts/{ref}")
            self._me = me
            return me
        except RuntimeError:
            if self._me is not None:
                return self._me  # transient failure: reuse cached identity
            raise

    def query_all(self, query: str, options: list[str] | None = None) -> list[dict]:
        """Run a change query, following pagination to completion."""
        results: list[dict] = []
        start = 0
        while True:
            page = self._with_retry(
                lambda s=start: self._client().search_changes(
                    query, limit=PAGE_SIZE, start=s,
                    options=QUERY_OPTIONS if options is None else options,
                ),
                f"query {query!r}",
            )
            results.extend(page)
            if not page or not page[-1].get("_more_changes"):
                return results
            start += len(page)

    def fetch_change_status(self, number: int) -> tuple[dict | None, str]:
        """(change, status) with status in ok | missing | error.

        'missing' is a definitive answer from Gerrit — no such change, or
        the credentials cannot read it — so the caller can stop asking.
        'error' is transient and worth retrying on the next refresh.
        """
        opts = "&".join(f"o={o}" for o in QUERY_OPTIONS)

        def _get() -> dict:
            return self._client().rest.get(f"/changes/{number}?{opts}")

        try:
            return self._with_retry(_get, f"GET change {number}"), "ok"
        except NotAvailable:
            return None, "missing"
        except RuntimeError:
            return None, "error"

    def fetch_change(self, number: int) -> dict | None:
        """Fetch one change with full dashboard options (any status, incl. WIP)."""
        return self.fetch_change_status(number)[0]

    def fetch_next_queues(self) -> dict[tuple[str, str], set[str]]:
        """Change-Ids currently staged on each <branch>-next landing branch.

        Uses the Gitiles range log <branch>..<branch>-next, so no local
        clone is needed.  Queues for projects outside the allowlist are
        never requested.
        """
        out: dict[tuple[str, str], set[str]] = {}
        queues = [(p, b) for p, b in self.config.next_queues
                  if not self.config.projects or p in self.config.projects]
        for project, branch in queues:
            path = (f"/plugins/gitiles/{quote(project, safe='')}/+log/"
                    f"refs/heads/{branch}..refs/heads/{branch}-next?format=JSON&n=300")
            try:
                data = self._with_retry(lambda p=path: self._client().rest.get(p),
                                        f"gitiles {branch}-next", tries=2)
            except RuntimeError as exc:
                log.warning("next-queue fetch for %s failed: %s", branch, exc)
                continue
            ids = set()
            for commit in data.get("log", []):
                m = _CHANGE_ID_RE.search(commit.get("message", ""))
                if m:
                    ids.add(m.group(1))
            out[(project, branch)] = ids
        return out

    # -- thread buckets ----------------------------------------------------

    def _fetch_threads(self, number: int, my_id: int, aliases: tuple = ()) -> dict:
        comments = self._client().get_comments(number)
        return build_thread_buckets(comments, my_id, aliases)

    def enrich_threads(self, changes: dict[int, dict], my_id: int,
                       aliases: tuple = (), prune: bool = True,
                       progress=None) -> dict[int, dict | None]:
        """Return {number: thread-buckets} using the meta_rev_id cache.

        progress(completed, todo_total) is called as fetches finish."""
        cache = self.store.load_enrich_cache()
        if cache.get("_v") != ENRICH_VERSION:
            cache = {"_v": ENRICH_VERSION}
        out: dict[int, dict | None] = {}
        todo: list[int] = []
        for number, change in changes.items():
            cached = cache.get(str(number))
            if cached and cached.get("meta_rev_id") == change.get("meta_rev_id"):
                out[number] = cached.get("threads")
            else:
                todo.append(number)

        if todo:
            futures = {self._pool.submit(self._fetch_threads, n, my_id, aliases): n
                       for n in todo}
            completed = 0
            for fut in concurrent.futures.as_completed(futures):
                n = futures[fut]
                try:
                    out[n] = fut.result()
                    cache[str(n)] = {
                        "meta_rev_id": changes[n].get("meta_rev_id"),
                        "threads": out[n],
                    }
                except Exception as exc:  # noqa: BLE001
                    log.warning("comments fetch for %d failed: %s", n, exc)
                    out[n] = None
                completed += 1
                if progress:
                    progress(completed, len(todo))
            if prune:
                # Drop cache entries for changes we no longer track — but
                # only on a clean refresh, so a partial failure does not
                # evict entries for changes that merely failed to list.
                keep = {str(n) for n in changes} | {"_v"}
                cache = {k: v for k, v in cache.items() if k in keep}
            self.store.save_enrich_cache(cache)
        return out

    # -- top level ---------------------------------------------------------

    def fetch_bundle(self, progress=None) -> dict:
        """Fetch everything the dashboard needs.  Returns a raw bundle:

        {self: {...}, changes: {number: change}, roles: {number: set[str]},
         starred: set[int], watchlist: [...], errors: [...], fetched_at: float}

        progress(phase, done, total) reports real fetch progress.
        """
        report = progress or (lambda *_: None)
        done = 0
        # account + 5 role queries + starred + merged + watchlist + queues,
        # then one unit per changed-change comments fetch (added later).
        total = 10

        def tick(phase: str) -> None:
            nonlocal done
            done += 1
            report(phase, done, total)

        errors: list[str] = []
        me = self.whoami()
        tick("account")
        my_id = me["_account_id"]
        # Username of the account whose board this is — validated by the
        # caller (app route regex), safe to interpolate into queries.
        u = me.get("username") or str(my_id)

        changes: dict[int, dict] = {}
        roles: dict[int, set[str]] = {}

        def add(batch: list[dict], role: str) -> None:
            for c in batch:
                n = c.get("_number")
                if n is None:
                    continue
                changes.setdefault(n, c)
                roles.setdefault(n, set()).add(role)

        # Core queries fail the whole refresh: a partial bundle would
        # overwrite the last good snapshot with gutted sections.
        pc = self._project_clause()
        queries = [
            ("mine", f"owner:{u} status:open{pc}"),
            ("mine", f"owner:{u} is:wip status:open{pc}"),
            ("review", f"reviewer:{u} -owner:{u} status:open{pc}"),
            ("cc", f"cc:{u} -owner:{u} status:open{pc}"),
            ("carry", f"status:open -owner:{u} (uploader:{u} OR committer:{u} OR author:{u}){pc}"),
        ]
        for role, q in queries:
            add(self.query_all(q), role)
            tick(f"changes: {role}")

        # Stars are only readable for the authenticated account.
        starred: set[int] = set()
        if u == self._client().username:
            try:
                for c in self.query_all(f"is:starred{pc}", options=[]):
                    if c.get("_number") is not None:
                        starred.add(c["_number"])
            except RuntimeError as exc:
                errors.append(str(exc))
        tick("starred")

        # Recently landed work (own + carried) — the encouraging part of
        # the dashboard.  Non-fatal: a failure only loses this section.
        try:
            add(self.query_all(f"status:merged -age:30d (owner:{u} OR uploader:{u}){pc}"), "merged")
        except RuntimeError as exc:
            errors.append(str(exc))
        tick("merged")

        watchlist = self.store.load_watchlist()
        watched = {e["number"] for e in watchlist}
        # Entries to unwatch: gone, or not servable by this instance.
        drop: set[int] = set()
        missing = [n for n in watched if n not in changes]
        if missing:
            # Watchlist entries may be merged/abandoned/WIP — fetch directly.
            for number, (change, status) in zip(
                    missing, self._pool.map(self.fetch_change_status, missing)):
                if change is not None:
                    change.setdefault("_number", number)
                    changes[number] = change
                elif status == "missing":
                    drop.add(number)  # definitive: stop refetching it
                else:
                    errors.append(f"watchlist change {number} could not be fetched")
        for e in watchlist:
            if e["number"] in changes:
                roles.setdefault(e["number"], set()).add("watch")
        tick("watchlist")

        # Defense-in-depth for restricted deployments: nothing outside the
        # project allowlist may survive into the bundle, whatever path it
        # arrived by.
        if self.config.projects:
            allowed = set(self.config.projects)
            for n in [n for n, c in changes.items() if c.get("project") not in allowed]:
                changes.pop(n)
                roles.pop(n, None)
                if n in watched:
                    drop.add(n)

        # Unwatch what this instance cannot serve, so it is not refetched
        # on every refresh forever.  The message is deliberately IDENTICAL
        # for "no such change" and "outside the configured projects":
        # otherwise adding a candidate number and reading the error back
        # would reveal whether a change exists in a project this instance
        # does not serve.
        for n in sorted(drop):
            self.store.watchlist_remove(n)
            errors.append(f"watchlist change {n} is not available here — removed")
        watchlist = [e for e in watchlist if e["number"] not in drop]

        next_queues = self.fetch_next_queues()
        tick("landing queues")

        # Thread buckets matter wherever unresolved comments exist — for
        # ANY role (a review-role patch must show its threads too).  This
        # is also cheaper: zero-unresolved changes need no /comments call.
        responsible = {
            n: c for n, c in changes.items()
            if c.get("status") in (None, "NEW")
            and (c.get("unresolved_comment_count") or 0) > 0
        }

        base_done, extended = done, False

        def comments_progress(completed: int, todo_total: int) -> None:
            nonlocal total, extended
            if not extended:
                total += todo_total
                extended = True
            report("comments", base_done + completed, total)

        # Aliases for @-ping detection in comment threads: the board
        # owner's username, email and display name.
        aliases = (me.get("username", ""), me.get("email", ""), me.get("name", ""))
        threads = self.enrich_threads(responsible, my_id, aliases=aliases,
                                      prune=not errors, progress=comments_progress)
        report("rendering", total, total)

        return {
            "self": {"account_id": my_id, "username": me.get("username", ""), "name": me.get("name", "")},
            "changes": changes,
            "roles": roles,
            "starred": starred,
            "watchlist": watchlist,
            "hidden": set(self.store.load_hidden()),
            "threads": threads,
            "next_queues": next_queues,
            "errors": errors,
            "fetched_at": time.time(),
        }


def build_thread_buckets(comments: dict[str, list[dict]], my_id: int,
                         mention_aliases: tuple = ()) -> dict:
    """Bucket unresolved comment threads by whose turn it is.

    A thread is unresolved iff its LAST comment has unresolved=true
    (matches Gerrit's unresolved_comment_count).  Buckets:
      my_turn    last comment by someone else — I owe a reply.  Review
                 bots (AI review, Misc Code Checks) count here too:
                 their comments are review content to be addressed.
      their_turn last comment by me — waiting on them
      sticky     I am the only author — my own note/TODO marker
      bot        last comment by a CI/lint bot — style noise, not urgent
    """
    by_id: dict[str, dict] = {}
    children: dict[str, list[str]] = {}
    roots: dict[str, str] = {}
    for path, clist in (comments or {}).items():
        for c in clist:
            c = dict(c)
            c["_path"] = path
            by_id[c["id"]] = c
    for cid, c in by_id.items():
        parent = c.get("in_reply_to")
        if parent and parent in by_id:
            children.setdefault(parent, []).append(cid)

    def find_root(cid: str) -> str:
        seen = set()
        while cid not in seen:
            seen.add(cid)
            parent = by_id[cid].get("in_reply_to")
            if not parent or parent not in by_id:
                return cid
            cid = parent
        return cid  # cycle guard

    threads: dict[str, list[dict]] = {}
    for cid, c in by_id.items():
        threads.setdefault(find_root(cid), []).append(c)

    def item(kind: str, c: dict, clist: list[dict]) -> dict:
        first_line = (c.get("message") or "").strip().splitlines()
        return {
            "kind": kind,
            "file": c.get("_path", ""),
            "line": c.get("line"),
            "ps": c.get("patch_set"),
            "author": (c.get("author") or {}).get("name", ""),
            "updated": c.get("updated", ""),
            "snippet": first_line[0][:140] if first_line else "",
            # Full conversation so the dashboard can unfold the thread.
            "conv": [{
                "author": (cc.get("author") or {}).get("name", ""),
                "ps": cc.get("patch_set"),
                "updated": cc.get("updated", ""),
                "message": (cc.get("message") or "")[:4000],
            } for cc in clist],
        }

    patterns = mention_patterns(mention_aliases)
    buckets = {"my_turn": 0, "their_turn": 0, "sticky": 0, "bot": 0,
               "pings": 0, "pings_open": 0, "items": [], "ping_items": []}
    for root, clist in threads.items():
        clist.sort(key=lambda c: c.get("updated", ""))
        last = clist[-1]
        if not last.get("unresolved"):
            continue
        authors = {(c.get("author") or {}).get("_account_id") for c in clist}
        last_author = last.get("author") or {}
        if is_bot(last_author) and not is_review_bot(last_author):
            kind = "bot"
        elif authors == {my_id}:
            kind = "sticky"
        elif last_author.get("_account_id") == my_id:
            kind = "their_turn"
        else:
            kind = "my_turn"
        buckets[kind] += 1
        if kind in ("my_turn", "sticky") and len(buckets["items"]) < 12:
            buckets["items"].append(item(kind, last, clist))

        # Ping: a person (never a bot) mentioned me in this unresolved
        # thread.  It lives until the THREAD IS RESOLVED — my own reply
        # does not discharge it (a "will look tomorrow" answer must not
        # make the ping disappear), it only downgrades the urgency:
        # answered=False → waiting on me; answered=True → I replied but
        # the thread is still open.  Newest mention wins; one per thread.
        if patterns:
            last_mine = max((i for i, c in enumerate(clist)
                             if (c.get("author") or {}).get("_account_id") == my_id),
                            default=-1)
            for i in range(len(clist) - 1, -1, -1):
                c = clist[i]
                author = c.get("author") or {}
                if author.get("_account_id") == my_id or is_bot(author):
                    continue
                if any(p.search(_ping_text(c.get("message"))) for p in patterns):
                    answered = last_mine > i
                    buckets["pings_open"] += 1
                    if not answered:
                        buckets["pings"] += 1
                    if len(buckets["ping_items"]) < 20:
                        it = item("ping", c, clist)
                        it["answered"] = answered
                        # Show the line that actually pings, not the
                        # comment's opening line.
                        for line in _ping_text(c.get("message")).splitlines():
                            if any(p.search(line) for p in patterns):
                                it["snippet"] = line.strip()[:140]
                                break
                        buckets["ping_items"].append(it)
                    break
    return buckets
