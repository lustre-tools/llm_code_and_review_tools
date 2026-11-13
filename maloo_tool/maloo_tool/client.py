"""Maloo REST API client."""

import html
import json
from datetime import datetime
import os
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from llm_tool_common.config import parse_env_file, resolve_env_file

from .config import MalooConfig
from .errors import ErrorCode, InvalidInputError, NotFoundError, ToolError

CSRF_RE = re.compile(
    r'<meta\s+name="csrf-token"\s+content="([^"]+)"'
)

#: A bug list row on a test set or subtest page, and the Accept/Reject
#: links the web UI shows in it for a link nobody has reviewed yet.
BUG_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.DOTALL)
REVIEW_HREF_RE = re.compile(r'href="(/buggable_links/validate\?[^"]*)"')

#: A row of the test queue page, and its action cell:
#: <td class="clickable action priority abandon" data="<job>#<build>#<idx>">.
#: The page offers Prioritize on a row whose cell has the "priority"
#: class and Abandon on one with "abandon"; Maloo picks the classes for
#: the signed-in account and the row's state.
QUEUE_ROW_RE = re.compile(
    r'<tr class="(?P<status>[^"]*)">(?P<body>.*?)</tr>', re.DOTALL
)
QUEUE_ACTION_RE = re.compile(
    r'<td class="clickable action(?P<classes>[^"]*)" data="(?P<key>[^"]*)"'
)

#: Where the queue page's script posts Prioritize and Abandon.
QUEUE_ACTION_PATH = "/test_queue/actionable"

#: Where the related sessions page's script posts a Test-Parameter
#: request, and the TP attributes that page refuses to send.
TP_REQUEST_PATH = "/test_sessions/related_retest"
TP_PREFIX = "Test-Parameters:"
TP_NOT_ALLOWED_RE = re.compile(
    r"forjanitoronly|forbuildonly|fortestonly|ignore|optional|trivial",
    re.IGNORECASE,
)

#: Where Maloo sends a request it has no session for.
SIGNIN_PATH = "/signin"

#: The page that shows a buggable's bug links.
BUGGABLE_PAGES = {"TestSet": "test_sets", "SubTest": "sub_tests"}

GERRIT_URL = os.environ.get("GERRIT_URL", "https://review.whamcloud.com").rstrip("/")

GET_ATTEMPTS = 4
# The suite run count of test-history is best effort: one attempt for
# each read, a read limit shorter than the client's, and few pages.
COUNT_READ_TIMEOUT = 30.0
SUITE_COUNT_PAGES = 3
RETRY_STATUSES = (502, 503, 504)
RETRY_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

#: Script endpoints whose id -> name mapping is kept on disk.
SCRIPT_ENDPOINTS = ("test_set_scripts", "sub_test_scripts")


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "maloo-tool" / "script-names.json"


def _gerrit_auth() -> tuple[str, str] | None:
    """The Gerrit login the gerrit CLI uses, or None if there is none.

    The process environment wins over gerrit-cli's .env file, as it does
    for the gerrit CLI.  Only the default credential set is read.
    """
    user = os.environ.get("GERRIT_USER")
    password = os.environ.get("GERRIT_PASS")
    if not (user and password):
        try:
            path = resolve_env_file("gerrit-cli")
            if path is not None:
                defaults, _ = parse_env_file(path)
                user = user or defaults.get("GERRIT_USER")
                password = password or defaults.get("GERRIT_PASS")
        except Exception:
            return None
    if user and password:
        return user, password
    return None


class GerritLookupError(Exception):
    """A Gerrit change could not be resolved; the message says why."""


def get_patchset_revisions(review_id: int) -> tuple[dict[int, str], int]:
    """Map every patchset of a Gerrit change to its revision SHA.

    One request with ALL_REVISIONS answers the current patchset, a
    chosen one and all of them.  A project that is not public returns
    404 to an anonymous read, so the request is authenticated with the
    gerrit CLI's login when one is configured.

    Returns the map and the current patchset number.  Raises
    GerritLookupError with the reason when the lookup fails.
    """
    auth = _gerrit_auth()
    url = f"{GERRIT_URL}{'/a' if auth else ''}/changes/{review_id}"
    try:
        resp = requests.get(
            url, params={"o": "ALL_REVISIONS"}, auth=auth, timeout=15
        )
    except requests.RequestException as exc:
        raise GerritLookupError(f"Gerrit request failed: {exc}") from exc
    if resp.status_code != 200:
        hint = "" if auth else (
            "; no Gerrit login is configured, and private changes need one"
        )
        raise GerritLookupError(
            f"Gerrit returned HTTP {resp.status_code}{hint}"
        )
    text = resp.text
    if text.startswith(")]}'"):
        text = text[4:]
    try:
        change = json.loads(text)
        revisions = {
            rev["_number"]: sha for sha, rev in change["revisions"].items()
        }
        current = change["revisions"][change["current_revision"]]["_number"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GerritLookupError("Gerrit returned no revisions") from exc
    return revisions, current


class MalooClient:
    """Client for the Maloo test results API."""

    def __init__(self, config: MalooConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.auth = (config.username, config.password)
        self._script_names: dict[str, dict[str, str]] | None = None
        self._script_names_dirty = False

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        attempts: int = GET_ATTEMPTS,
        read_timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Make a GET request and return the data array.

        testing.whamcloud.com drops a small fraction of connections, and a
        command can make a thousand requests, so a dropped connection, a
        timeout or a 502/503/504 is retried, GET_ATTEMPTS in all.
        A best-effort read gives ``attempts`` and a shorter
        ``read_timeout`` instead.
        """
        url = f"{self.config.base_url}/api/{endpoint}"
        timeout = self.config.timeout
        if read_timeout is not None:
            timeout = (timeout[0], min(timeout[1], read_timeout))
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.get(
                    url, params=params, timeout=timeout
                )
                if (resp.status_code in RETRY_STATUSES
                        and attempt < attempts):
                    time.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                resp.raise_for_status()
                body = resp.json()
                break
            except RETRY_EXCEPTIONS as exc:
                if attempt < attempts:
                    time.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                msg = f"{exc} (after {attempts} attempts)"
                if isinstance(exc, requests.exceptions.ChunkedEncodingError):
                    raise requests.ConnectionError(msg) from exc
                raise type(exc)(msg) from exc
        if isinstance(body, list):
            return body
        return body.get("data", [])

    @staticmethod
    def _retried(send, url: str, **kwargs) -> requests.Response:
        """send(url, **kwargs), retried as _get retries its GETs.

        Only for requests that are safe to repeat: page reads, the
        sign-in and log downloads, never a retest or a ticket filing.
        """
        for attempt in range(1, GET_ATTEMPTS + 1):
            try:
                resp = send(url, **kwargs)
            except RETRY_EXCEPTIONS as exc:
                if attempt < GET_ATTEMPTS:
                    time.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                msg = f"{exc} (after {GET_ATTEMPTS} attempts)"
                if isinstance(exc, requests.exceptions.ChunkedEncodingError):
                    raise requests.ConnectionError(msg) from exc
                raise type(exc)(msg) from exc
            if (resp.status_code in RETRY_STATUSES
                    and attempt < GET_ATTEMPTS):
                time.sleep(0.5 * 2 ** (attempt - 1))
                continue
            return resp
        raise AssertionError("unreachable")

    # -- Script name cache --

    def _names(self, endpoint: str) -> dict[str, str]:
        """The cached id -> name map for a script endpoint.

        Script names never change, so they are kept on disk per server and
        written back by save_script_names().
        """
        if self._script_names is None:
            self._script_names = {e: {} for e in SCRIPT_ENDPOINTS}
            try:
                with open(_cache_path()) as f:
                    stored = json.load(f).get(self.config.base_url, {})
                for e in SCRIPT_ENDPOINTS:
                    names = stored.get(e, {})
                    if isinstance(names, dict):
                        self._script_names[e].update(
                            (k, v) for k, v in names.items()
                            if isinstance(v, str)
                        )
            except (OSError, ValueError, AttributeError):
                pass
        return self._script_names[endpoint]

    def _get_script(
        self, endpoint: str, script_id: str
    ) -> dict[str, Any] | None:
        names = self._names(endpoint)
        if script_id in names:
            return {"id": script_id, "name": names[script_id]}
        rows = self._get(endpoint, {"id": script_id})
        if not rows:
            return None
        if isinstance(rows[0].get("name"), str):
            names[script_id] = rows[0]["name"]
            self._script_names_dirty = True
        return rows[0]

    def save_script_names(self) -> None:
        """Merge the script names this client learned into the disk cache.

        A cache that cannot be written is skipped.
        """
        if not self._script_names_dirty or self._script_names is None:
            return
        path = _cache_path()
        try:
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            server = data.get(self.config.base_url)
            if not isinstance(server, dict):
                server = data[self.config.base_url] = {}
            for e in SCRIPT_ENDPOINTS:
                names = server.get(e)
                if not isinstance(names, dict):
                    names = server[e] = {}
                names.update(self._script_names[e])
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=".script-names.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            return
        self._script_names_dirty = False

    def _get_paginated(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        max_records: int = 0,
    ) -> list[dict[str, Any]]:
        """GET every page of a result, whatever page size the server uses.

        The page size was assumed to be 200 and is in fact 1500, so the
        short-page test never fired and the offset advanced by less than each
        page returned: every unbounded call re-read most of what it had
        already collected and walked to the end of the table, holding all of
        it.  One such call reached 1.4 GB.

        The size is learned from the first page instead, and the offset
        advances by what was actually returned.  The API has no page-size
        parameter -- limit, per_page and page_size are all ignored -- so it
        cannot be pinned from this end.

        Args:
            endpoint: API endpoint name.
            params: Query parameters.
            max_records: Stop after this many records (0 = no limit).
        """
        params = dict(params) if params else {}
        results: list[dict[str, Any]] = []
        offset = 0
        page_size: int | None = None
        previous: list[dict[str, Any]] | None = None
        while True:
            if max_records > 0 and len(results) >= max_records:
                break
            params["offset"] = offset
            page = self._get(endpoint, params)
            if not page:
                break
            # bug_links with related=true ignores offset and answers every
            # request with the same full page, so this is the only end.
            if page == previous:
                break
            previous = page
            results.extend(page)
            offset += len(page)
            if page_size is None:
                page_size = len(page)
            if len(page) < page_size:
                break
        if max_records > 0:
            return results[:max_records]
        return results

    # -- Test Sessions --

    def get_sessions(
        self,
        params: dict[str, Any],
        max_records: int = 0,
    ) -> list[dict[str, Any]]:
        """Get test sessions with arbitrary query params.

        Args:
            params: Query parameters for the test_sessions endpoint.
            max_records: Stop after this many records (0 = no limit).
        """
        return self._get_paginated(
            "test_sessions", params, max_records
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a single test session by ID."""
        rows = self._get("test_sessions", {"id": session_id})
        return rows[0] if rows else None

    def get_session_review(self, session_id: str) -> dict[str, Any] | None:
        """Gerrit change, patchset and revision a test session tested.

        None for a session that tested no review, such as a branch build.
        """
        rows = self._get("code_reviews", {"test_session_id": session_id})
        if not rows:
            return None
        row = rows[0]
        try:
            data = json.loads(row.get("data") or "{}")
        except ValueError:
            data = {}
        change = data.get("review_no")
        return {
            "change": int(change) if str(change or "").isdigit() else None,
            "patchset": data.get("patch_no"),
            "commit": row.get("commit_id"),
            "project": data.get("project"),
            "branch": data.get("branch"),
        }

    def find_sessions_by_commit(
        self, commit_id: str
    ) -> list[dict[str, Any]]:
        """Find test sessions for one exact Gerrit revision.

        Looked up by commit rather than by change number because the change
        number is not a column: ``code_reviews`` carries commit_id and
        test_session_id, and the project, branch and patch number live inside
        its ``data`` blob.  The API ignores a parameter it does not recognise,
        so the old query by review_id and review_patch filtered nothing and
        matched the whole table -- for change 35302 it answered with a
        different project's patch.  A revision identifies a patchset exactly,
        which is also what the caller wants to verify the answer against.
        """
        commit = str(commit_id or "").strip().lower()
        if not commit:
            raise ValueError("a commit id is required to find test sessions")
        reviews = self._get_paginated("code_reviews", {"commit_id": commit})
        session_ids = {
            str(review.get("test_session_id") or "").strip()
            for review in reviews
        }
        sessions = []
        for sid in sorted(session_ids - {""}):
            s = self.get_session(sid)
            if s:
                sessions.append(s)
        return sessions

    # -- Test Sets (suites) --

    def get_test_sets(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Get test sets for a session."""
        return self._get_paginated(
            "test_sets", {"test_session_id": session_id}
        )

    def get_test_set(
        self, test_set_id: str
    ) -> dict[str, Any] | None:
        """Get a single test set by ID."""
        rows = self._get("test_sets", {"id": test_set_id})
        return rows[0] if rows else None

    # -- Sub Tests --

    def get_subtests(
        self,
        test_set_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get subtests, filtered by test_set or session."""
        params: dict[str, Any] = {}
        if test_set_id:
            params["test_set_id"] = test_set_id
        if session_id:
            params["test_session_id"] = session_id
        return self._get_paginated("sub_tests", params)

    def get_subtest(self, subtest_id: str) -> dict[str, Any] | None:
        """Get a single subtest by ID."""
        rows = self._get("sub_tests", {"id": subtest_id})
        return rows[0] if rows else None

    # -- Script names (for resolving IDs to names) --

    def get_test_set_script(
        self, script_id: str
    ) -> dict[str, Any] | None:
        """Get test set script (suite name) by ID.

        A cached script has only its id and name.
        """
        return self._get_script("test_set_scripts", script_id)

    def get_sub_test_script(
        self, script_id: str
    ) -> dict[str, Any] | None:
        """Get sub test script (test name) by ID.

        A cached script has only its id and name.
        """
        return self._get_script("sub_test_scripts", script_id)

    # -- Batch name resolution --

    def resolve_test_set_names(
        self, test_sets: list[dict[str, Any]]
    ) -> dict[str, str]:
        """Resolve test_set_script_id -> name for a list of test sets."""
        script_ids = {
            ts["test_set_script_id"]
            for ts in test_sets
            if "test_set_script_id" in ts
        }
        names: dict[str, str] = {}
        for sid in script_ids:
            script = self.get_test_set_script(sid)
            if script:
                names[sid] = script["name"]
        return names

    def resolve_subtest_names(
        self, subtests: list[dict[str, Any]]
    ) -> dict[str, str]:
        """Resolve sub_test_script_id -> name for a list of subtests."""
        script_ids = {
            st["sub_test_script_id"]
            for st in subtests
            if "sub_test_script_id" in st
        }
        names: dict[str, str] = {}
        for sid in script_ids:
            script = self.get_sub_test_script(sid)
            if script:
                names[sid] = script["name"]
        return names

    # -- Bug links --

    def get_bug_links(
        self,
        buggable_id: str,
        buggable_type: str | None = None,
        related: bool = False,
    ) -> list[dict[str, Any]]:
        """Get bug links for a test set or subtest."""
        params: dict[str, Any] = {"buggable_id": buggable_id}
        if buggable_type:
            params["buggable_type"] = buggable_type
        if related:
            params["related"] = "true"
        return self._get_paginated("bug_links", params)

    def create_bug_link(
        self,
        buggable_class: str,
        buggable_id: str,
        bug_upstream_id: str,
        bug_state: str = "accepted",
    ) -> str:
        """Create a bug link on a test set or subtest.

        Returns the response text from the server ("OK" or "ERROR ...").
        """
        url = f"{self.config.base_url}/api/bug_links"
        params = {
            "buggable_class": buggable_class,
            "buggable_id": buggable_id,
            "bug_upstream_id": bug_upstream_id,
            "bug_state": bug_state,
        }
        resp = self.session.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.text.strip()

    def review_bug_link(
        self,
        buggable_class: str,
        buggable_id: str,
        bug_upstream_id: str,
        valid: str = "Accepted",
    ) -> str:
        """Set a bug link's state, as the web UI does: ``valid`` is
        Accepted, Rejected or Pending.

        The REST API cannot change an existing link's state.  The UI's
        Accept, Reject and Reset links are an XHR GET of
        /buggable_links/validate?bug_reference_id=&buggable_id=&valid=,
        and the bug reference id is only on the page, so it is read from
        the row for the ticket on the buggable's page.  A pending link
        offers Accept and Reject; an accepted one Reject and Reset.

        Returns the bug reference id.  Raises PermissionError if the web
        login did not take, LookupError if the page offers no such link
        to follow (the link is already in that state, or not on this
        buggable).
        """
        web = self._web_login()
        page_url = (
            f"{self.config.base_url}/{BUGGABLE_PAGES[buggable_class]}"
            f"/{buggable_id}"
        )
        resp = self._retried(web.get, page_url, timeout=60)
        resp.raise_for_status()
        if urlsplit(resp.url).path == SIGNIN_PATH:
            raise PermissionError(
                "the Maloo web sign-in did not take; check MALOO_USER "
                "and MALOO_PASS"
            )

        ticket = bug_upstream_id.upper()
        href = None
        for row in BUG_ROW_RE.findall(resp.text):
            texts = re.findall(r">\s*([A-Za-z]+-\d+)\s*</a>", row)
            if ticket not in (t.upper() for t in texts):
                continue
            for raw in REVIEW_HREF_RE.findall(row):
                link = html.unescape(raw)
                query = parse_qs(urlsplit(link).query)
                if (query.get("buggable_id") == [buggable_id]
                        and query.get("valid") == [valid]
                        and query.get("bug_reference_id")):
                    href = link
                    break
            if href:
                break
        if href is None:
            raise LookupError(
                f"{page_url} offers no {valid} for a {bug_upstream_id} "
                f"link on {buggable_id}: it is not there, or already {valid}"
            )

        resp = web.get(
            f"{self.config.base_url}{href}",
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        resp.raise_for_status()
        if urlsplit(resp.url).path == SIGNIN_PATH:
            raise PermissionError(
                "Maloo sent the link review to the sign-in page"
            )
        return parse_qs(urlsplit(href).query)["bug_reference_id"][0]

    # -- Test queues --

    def get_test_queues(
        self,
        params: dict[str, Any],
        max_records: int = 0,
    ) -> list[dict[str, Any]]:
        """Get test queue entries with arbitrary query params.

        Args:
            params: Query parameters for the test_queues endpoint.
            max_records: Stop after this many records (0 = no limit).
        """
        return self._get_paginated(
            "test_queues", params, max_records
        )

    # -- Test history --

    def find_test_set_script_id(
        self, name: str
    ) -> str | None:
        """Find a test_set_script ID by name."""
        rows = self._get("test_set_scripts", {"name": name})
        return rows[0]["id"] if rows else None

    def find_sub_test_script_ids(
        self, name: str, suite_script_id: str
    ) -> list[str]:
        """The sub_test_script IDs of a subtest name in one suite.

        One name can have more than one ID, and the same name can
        exist in other suites.  A script row carries the ID of its
        suite in test_set_script_id, so the suite needs no further
        read.  The server may match a name loosely, so rows of another
        name are dropped.
        """
        rows = self._get_paginated("sub_test_scripts", {"name": name})
        return [
            r["id"] for r in rows
            if "id" in r and r.get("name", name) == name
            and r.get("test_set_script_id") == suite_script_id
        ]

    def count_suite_runs(
        self,
        suite_script_id: str,
        from_date: str,
        to_date: str,
        days: int,
    ) -> dict[str, Any] | None:
        """How many runs a suite had in a range, on every job.

        Best effort: one attempt and COUNT_READ_TIMEOUT for each read, at
        most SUITE_COUNT_PAGES pages of test_sets.  The page size is
        learned from the first page, so the end of a short list costs
        one more read.  Rows come newest first and each has
        ``submission``.  When a page is short or
        empty, the pages reached the start of the range and the count is
        exact.  Otherwise the count is an estimate: the rows read,
        divided by the days between the newest and the oldest
        submission among them (at least one hour), times ``days``.

        Returns {"runs", "exact", "seen"} (``seen``: the first page had
        a row), or None when a read fails or no row has a submission.
        """
        rows: list[dict[str, Any]] = []
        page_size: int | None = None
        reached_start = False
        try:
            for _ in range(SUITE_COUNT_PAGES):
                page = self._get(
                    "test_sets",
                    {"test_set_script_id": suite_script_id,
                     "from": from_date, "to": to_date,
                     "offset": len(rows)},
                    attempts=1, read_timeout=COUNT_READ_TIMEOUT,
                )
                rows.extend(page)
                if page_size is None:
                    page_size = len(page)
                if not page or len(page) < page_size:
                    reached_start = True
                    break
        except requests.RequestException:
            return None
        if not rows:
            return {"runs": 0, "exact": True, "seen": False}
        if reached_start:
            return {"runs": len(rows), "exact": True, "seen": True}
        stamps = []
        for r in rows:
            try:
                stamps.append(datetime.fromisoformat(
                    str(r["submission"]).replace("Z", "+00:00")))
            except (KeyError, ValueError):
                continue
        if not stamps:
            return None
        span = (max(stamps) - min(stamps)).total_seconds() / 86400
        span = max(span, 1 / 24)
        return {
            "runs": round(len(stamps) / span * days),
            "exact": False,
            "seen": True,
        }

    def get_failed_subtests(
        self,
        script_ids: list[str],
        statuses: list[str],
        from_date: str,
        to_date: str,
        job: str | None = None,
        limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]], int]:
        """Runs of one test that ended in the given statuses, newest first.

        script_ids are the test's scripts in one suite, so the suite
        needs no filter here.  One sub_tests read for each script ID
        and status.  A read with no status costs the server about 30 s
        a day for a heavy test and can pass the client's read timeout,
        so a status is always sent.  _get_paginated reads every page: a
        bare _get returns the first page only and would cut a long list
        without a sign.

        With ``job`` only runs whose session has that trigger_job
        count; that reads each distinct session once.  Returns (total
        before limit, the newest ``limit`` entries, the total on every
        job).  An entry is {"subtest", "session", "review",
        "bug_links"}.
        """
        rows: dict[str, dict[str, Any]] = {}
        for script_id in script_ids:
            for status in statuses:
                for row in self._get_paginated("sub_tests", {
                    "sub_test_script_id": script_id,
                    "status": status,
                    "from": from_date,
                    "to": to_date,
                }):
                    rows.setdefault(row["id"], row)

        sessions: dict[str, dict[str, Any] | None] = {}

        def session_of(row: dict[str, Any]) -> dict[str, Any] | None:
            sid = row.get("test_session_id")
            if sid not in sessions:
                sessions[sid] = self.get_session(sid) if sid else None
            return sessions[sid]

        kept = sorted(
            rows.values(),
            key=lambda r: r.get("created_at") or "",
            reverse=True,
        )
        if job is not None:
            kept = [
                r for r in kept
                if (s := session_of(r)) and s.get("trigger_job") == job
            ]

        reviews: dict[str, dict[str, Any] | None] = {}
        entries = []
        for row in kept[:limit]:
            sid = row.get("test_session_id")
            if sid not in reviews:
                reviews[sid] = (
                    self.get_session_review(sid) if sid else None)
            entries.append({
                "subtest": row,
                "session": session_of(row),
                "review": reviews[sid],
                "bug_links": self.get_bug_links(row["id"]),
            })
        return len(kept), entries, len(rows)

    def _test_history_by_suite(
        self,
        test_name: str,
        trigger_job: str,
        from_date: str,
        to_date: str,
        suite: str,
        suite_script_id: str,
        max_sessions: int,
        max_scan: int,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, int]]:
        """Test history for one named suite, found by its test sets.

        The branch lives on the session, not the test set, so each
        candidate costs one session lookup to check trigger_job.
        """
        rows = self._get("test_sets", {
            "test_set_script_id": suite_script_id,
            "from": from_date,
            "to": to_date,
        })
        rows.sort(key=lambda r: r.get("submission") or "", reverse=True)

        sub_script_cache: dict[str, str] = {}
        session_cache: dict[str, dict[str, Any] | None] = {}
        history: list[dict[str, Any]] = []
        resolved_suite = None
        scanned = 0
        with_suite = 0

        for ts in rows[:max_scan]:
            if with_suite >= max_sessions:
                break
            sid = ts.get("test_session_id")
            if not sid:
                continue
            scanned += 1
            if sid not in session_cache:
                session_cache[sid] = self.get_session(sid)
            sess = session_cache[sid]
            if not sess or sess.get("trigger_job") != trigger_job:
                continue
            with_suite += 1

            subtests = self.get_subtests(test_set_id=ts["id"])
            new_sub_ids = {
                st["sub_test_script_id"]
                for st in subtests
                if "sub_test_script_id" in st
                and st["sub_test_script_id"] not in sub_script_cache
            }
            for script_id in new_sub_ids:
                script = self.get_sub_test_script(script_id)
                if script:
                    sub_script_cache[script_id] = script["name"]

            for st in subtests:
                if sub_script_cache.get(
                    st.get("sub_test_script_id", ""), ""
                ) != test_name:
                    continue
                resolved_suite = suite
                history.append({
                    "session_id": sid,
                    "submission": sess.get("submission", ""),
                    "test_host": sess.get("test_host", ""),
                    "test_name": sess.get("test_name", ""),
                    "suite": suite,
                    "status": st["status"],
                    "error": st.get("error", ""),
                    "duration": st.get("duration"),
                    "test_set_id": ts["id"],
                })

        history.sort(key=lambda x: x["submission"])
        stats = {
            "sessions_scanned": scanned,
            "sessions_with_suite": with_suite,
        }
        return history, resolved_suite, stats

    def get_test_history(
        self,
        test_name: str,
        trigger_job: str,
        from_date: str,
        to_date: str,
        suite: str | None = None,
        max_sessions: int = 50,
        max_scan: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, int]]:
        """Get pass/fail history for a specific test.

        Args:
            test_name: Subtest name (e.g. "test_39b").
            trigger_job: Branch name (e.g. "lustre-master").
            from_date: Start date (yyyy-mm-dd).
            to_date: End date (yyyy-mm-dd).
            suite: Optional suite name to filter (e.g. "sanity").
            max_sessions: Max sessions *containing the suite* to examine.
            max_scan: Max sessions to look at while finding those
                (default 5x max_sessions).

        Returns:
            (history_entries, suite_name_resolved, stats)
            Each entry has: session_id, submission, test_host,
            suite, status, error, duration.  stats carries
            sessions_scanned and sessions_with_suite, which is how a
            caller tells "the test never failed" from "the sample held
            no run of this suite at all" -- a branch runs only some
            suites, so the first N sessions can contain none of it.
        """
        # Get sessions for the branch in the date range
        params: dict[str, Any] = {
            "trigger_job": trigger_job,
            "from": from_date,
            "to": to_date,
        }
        if max_scan is None:
            max_scan = max_sessions * 5 if suite else max_sessions
        max_scan = max(max_scan, max_sessions)

        # If suite filter given, resolve its script ID for faster matching
        suite_script_id = None
        if suite:
            suite_script_id = self.find_test_set_script_id(suite)

        # With a suite, go at its test sets directly.  Walking the
        # branch's most recent sessions instead finds nothing whenever
        # that suite is not in them -- on a busy branch the newest
        # sessions are a handful of groups, and a suite belonging to any
        # other group never appears however many are scanned.
        if suite_script_id:
            return self._test_history_by_suite(
                test_name=test_name,
                trigger_job=trigger_job,
                from_date=from_date,
                to_date=to_date,
                suite=suite,
                suite_script_id=suite_script_id,
                max_sessions=max_sessions,
                max_scan=max_scan,
            )

        sessions = self.get_sessions(params, max_records=max_scan)

        # Cache for script name lookups
        set_script_cache: dict[str, str] = {}
        sub_script_cache: dict[str, str] = {}

        history: list[dict[str, Any]] = []
        resolved_suite = suite
        scanned = 0
        with_suite = 0

        for sess in sessions:
            if with_suite >= max_sessions:
                break
            sid = sess["id"]
            scanned += 1
            test_sets = self.get_test_sets(sid)

            # Resolve set names we haven't seen
            new_set_ids = {
                ts["test_set_script_id"]
                for ts in test_sets
                if "test_set_script_id" in ts
                and ts["test_set_script_id"] not in set_script_cache
            }
            for script_id in new_set_ids:
                script = self.get_test_set_script(script_id)
                if script:
                    set_script_cache[script_id] = script["name"]

            # Filter test sets by suite if specified
            target_sets = test_sets
            if suite_script_id:
                target_sets = [
                    ts for ts in test_sets
                    if ts.get("test_set_script_id") == suite_script_id
                ]
            elif suite:
                target_sets = [
                    ts for ts in test_sets
                    if set_script_cache.get(
                        ts.get("test_set_script_id", "")
                    ) == suite
                ]

            if target_sets:
                with_suite += 1

            for ts in target_sets:
                suite_name = set_script_cache.get(
                    ts.get("test_set_script_id", ""), "unknown"
                )
                subtests = self.get_subtests(test_set_id=ts["id"])

                # Resolve subtest names we haven't seen
                new_sub_ids = {
                    st["sub_test_script_id"]
                    for st in subtests
                    if "sub_test_script_id" in st
                    and st["sub_test_script_id"] not in sub_script_cache
                }
                for script_id in new_sub_ids:
                    script = self.get_sub_test_script(script_id)
                    if script:
                        sub_script_cache[script_id] = script["name"]

                for st in subtests:
                    st_name = sub_script_cache.get(
                        st.get("sub_test_script_id", ""), ""
                    )
                    if st_name != test_name:
                        continue
                    resolved_suite = suite_name
                    history.append({
                        "session_id": sid,
                        "submission": sess.get("submission", ""),
                        "test_host": sess.get("test_host", ""),
                        "test_name": sess.get("test_name", ""),
                        "suite": suite_name,
                        "status": st["status"],
                        "error": st.get("error", ""),
                        "duration": st.get("duration"),
                        "test_set_id": ts["id"],
                    })

        # Sort by submission date
        history.sort(key=lambda x: x["submission"])
        stats = {
            "sessions_scanned": scanned,
            "sessions_with_suite": with_suite,
        }
        return history, resolved_suite, stats

    # -- Failure aggregation --

    def get_top_failures(
        self,
        trigger_job: str,
        from_date: str,
        to_date: str,
        max_sessions: int = 50,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Aggregate most common subtest failures for a branch.

        Args:
            trigger_job: Branch name (e.g. "lustre-master").
            from_date: Start date (yyyy-mm-dd).
            to_date: End date (yyyy-mm-dd).
            max_sessions: Max sessions to examine.

        Returns:
            (failures_list, sessions_examined, sessions_total)
            where failures_list is sorted by count descending, each
            entry has keys: test_name, suite, count, sessions,
            error_sample, example_session_id, example_test_set_id.
        """
        params: dict[str, Any] = {
            "trigger_job": trigger_job,
            "from": from_date,
            "to": to_date,
            "test_sets_failed": "true",
        }
        sessions = self.get_sessions(params, max_records=max_sessions)
        sessions_total = len(sessions)

        # Map: (suite_name, subtest_name) -> aggregation data
        agg: dict[tuple[str, str], dict[str, Any]] = {}

        # Cache for script name lookups
        set_script_cache: dict[str, str] = {}
        sub_script_cache: dict[str, str] = {}

        for sess in sessions:
            sid = sess["id"]
            test_sets = self.get_test_sets(sid)
            set_names = self.resolve_test_set_names(test_sets)
            set_script_cache.update(set_names)

            failed_sets = [
                ts for ts in test_sets
                if ts["status"] in ("FAIL", "CRASH", "ABORT", "TIMEOUT")
            ]

            for ts in failed_sets:
                suite = set_names.get(
                    ts.get("test_set_script_id", ""), "unknown"
                )
                subtests = self.get_subtests(test_set_id=ts["id"])

                # Batch-resolve subtest names (use cache)
                new_ids = {
                    st["sub_test_script_id"]
                    for st in subtests
                    if "sub_test_script_id" in st
                    and st["sub_test_script_id"] not in sub_script_cache
                }
                for script_id in new_ids:
                    script = self.get_sub_test_script(script_id)
                    if script:
                        sub_script_cache[script_id] = script["name"]

                for st in subtests:
                    if st["status"] not in (
                        "FAIL", "CRASH", "ABORT", "TIMEOUT",
                    ):
                        continue
                    st_name = sub_script_cache.get(
                        st.get("sub_test_script_id", ""),
                        f"order_{st.get('order', '?')}",
                    )
                    key = (suite, st_name)
                    if key not in agg:
                        agg[key] = {
                            "test_name": st_name,
                            "suite": suite,
                            "count": 0,
                            "sessions": set(),
                            "statuses": {},
                            "error_sample": "",
                            "example_session_id": "",
                            "example_test_set_id": "",
                        }
                    entry = agg[key]
                    entry["count"] += 1
                    entry["sessions"].add(sid)
                    status = st["status"]
                    entry["statuses"][status] = (
                        entry["statuses"].get(status, 0) + 1
                    )
                    err = st.get("error", "")
                    if err and not entry["error_sample"]:
                        entry["error_sample"] = err[:300]
                    if not entry["example_session_id"]:
                        entry["example_session_id"] = sid
                        entry["example_test_set_id"] = ts["id"]

        # Convert sets to counts and sort
        result = []
        for entry in agg.values():
            entry["session_count"] = len(entry["sessions"])
            del entry["sessions"]
            result.append(entry)
        result.sort(key=lambda x: x["count"], reverse=True)
        return result, len(sessions), sessions_total

    # -- Web session auth (for non-API endpoints) --

    _web_session: requests.Session | None = None

    def _web_login(self) -> requests.Session:
        """Log in via the web sign-in form for cookie-based auth.

        The REST API uses basic auth, but web endpoints like
        download_logs and retest require cookie-based auth via the
        sign-in form.  Returns a separate requests.Session that has
        web cookies but no basic-auth header.
        """
        if self._web_session is not None:
            return self._web_session

        import re as _re

        # Use a separate session without basic auth
        web = requests.Session()
        signin_url = f"{self.config.base_url}/signin"
        resp = self._retried(web.get, signin_url, timeout=30)
        resp.raise_for_status()

        # Extract CSRF token from form
        m = _re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"',
            resp.text,
        )
        if not m:
            raise RuntimeError(
                "Could not extract CSRF token from sign-in page"
            )

        # Find the form action
        action_m = _re.search(
            r'<form[^>]*action="([^"]+)"', resp.text
        )
        action = action_m.group(1) if action_m else "/sessions"

        self._retried(
            web.post,
            f"{self.config.base_url}{action}",
            data={
                "authenticity_token": m.group(1),
                "email": self.config.username,
                "password": self.config.password,
                "commit": "Sign in",
            },
            timeout=30,
            # The session cookie comes with the redirect; following it
            # fetches the whole test session list, some 850 KB.
            allow_redirects=False,
        )

        # Also merge cookies into the main session for download_logs
        self.session.cookies.update(web.cookies)
        self._web_session = web
        return web

    # -- Retest (web form, not REST API) --

    def _get_csrf_token(
        self, web: requests.Session, session_id: str
    ) -> str:
        """Fetch the CSRF token from the test session page."""
        url = f"{self.config.base_url}/test_sessions/{session_id}"
        resp = self._retried(web.get, url, timeout=30)
        resp.raise_for_status()
        # Try <input> hidden field first, then <meta> tag
        m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"',
            resp.text,
        )
        if not m:
            m = CSRF_RE.search(resp.text)
        if not m:
            raise RuntimeError(
                "Could not extract CSRF token from session page"
            )
        return m.group(1)

    def download_logs(
        self, test_set_id: str, timeout: int = 120
    ) -> bytes:
        """Download test logs for a test set (suite).

        Args:
            test_set_id: UUID of the test set.
            timeout: HTTP timeout in seconds.

        Returns:
            Raw bytes of the log archive (zip format).
        """
        self._web_login()
        url = (
            f"{self.config.base_url}"
            f"/test_sets/{test_set_id}/download_logs"
        )
        resp = self._retried(
            self.session.get, url, timeout=timeout, allow_redirects=True
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise RuntimeError(
                "Log download returned HTML (auth may have failed)"
            )
        return resp.content

    def list_test_logs(
        self, test_set_id: str, timeout: int = 60
    ) -> list[dict[str, str]]:
        """List the log files linked on a test set's web page.

        The download_logs archive omits the per-test debug_log, dmesg
        and test_log files. The test set page links every file as
        /test_logs/<id>/download.

        Returns:
            A list of {"name": file name, "id": test_log UUID}.
        """
        web = self._web_login()
        resp = self._retried(
            web.get,
            f"{self.config.base_url}/test_sets/{test_set_id}",
            timeout=timeout,
        )
        resp.raise_for_status()
        return [
            {"name": m.group(1).strip(), "id": m.group(2)}
            for m in re.finditer(
                r"<p[^>]*>([^<]+)</p>\s*\[ <a[^>]*"
                r'href="/test_logs/([0-9a-f-]+)/download"',
                resp.text,
            )
        ]

    def download_test_log(
        self, test_log_id: str, timeout: int = 120
    ) -> bytes:
        """Download one file from /test_logs/<id>/download."""
        web = self._web_login()
        resp = self._retried(
            web.get,
            f"{self.config.base_url}/test_logs/{test_log_id}/download",
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.content

    def retest(
        self,
        session_id: str,
        option: str = "single",
        bug_id: str = "",
    ) -> str:
        """Request a retest for a test session.

        Args:
            session_id: The test session UUID
            option: One of "single", "all", or "livedebug"
            bug_id: JIRA ticket number (e.g., "LU-19487")

        Returns:
            Response status text
        """
        web = self._web_login()
        token = self._get_csrf_token(web, session_id)
        url = (
            f"{self.config.base_url}"
            f"/test_sessions/{session_id}/retest"
        )
        data = {
            "authenticity_token": token,
            "retest_option": option,
            "bug_id": bug_id,
            "commit": "Retest",
        }
        resp = web.post(url, data=data, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        # Check for success flash message
        m = re.search(
            r'class="flash success">([^<]+)', resp.text
        )
        if m:
            return m.group(1).strip()

        # Check for error flash
        m = re.search(
            r'class="flash (?:error|alert)">([^<]+)', resp.text
        )
        if m:
            raise RuntimeError(f"Retest failed: {m.group(1).strip()}")

        return f"HTTP {resp.status_code}"

    # -- Test queue actions (web page script, not REST API) --

    @staticmethod
    def parse_queue_rows(page: str) -> list[dict[str, Any]]:
        """The test queue page's sessions and the actions it offers.

        Returns one dict per row that has an action cell: its ``key``
        (``<job>#<build>#<idx>``, what the page posts), ``job``,
        ``buildno``, ``idx`` (None when the row has none), ``status``
        (Waiting, Queued, Testing, ...) and ``actions`` (a sorted subset
        of "abandon" and "priority").
        """
        rows = []
        for row in QUEUE_ROW_RE.finditer(page):
            cell = QUEUE_ACTION_RE.search(row.group("body"))
            if not cell:
                continue
            key = cell.group("key")
            job, build, idx = (key.split("#") + ["", ""])[:3]
            classes = set(cell.group("classes").split())
            status = row.group("status").split()
            rows.append({
                "key": key,
                "job": job,
                "buildno": int(build) if build.isdigit() else build,
                "idx": int(idx) if idx.isdigit() else None,
                "status": status[0] if status else "",
                "actions": sorted(classes & {"abandon", "priority"}),
            })
        return rows

    def get_queue_page(
        self, job: str, buildno: int
    ) -> tuple[requests.Session, str]:
        """Sign in and read the test queue page filtered to one build.

        Returns the signed-in web session and the page HTML.
        """
        web = self._web_login()
        resp = self._retried(
            web.get,
            f"{self.config.base_url}/test_queue",
            params={"jobs": job, "builds": buildno,
                    "commit": "Apply Filter"},
            timeout=60,
        )
        resp.raise_for_status()
        return web, resp.text

    #: The queue page's actions: the ``call`` it posts, the row class
    #: that offers it, the name it shows, and the result's count key.
    QUEUE_ACTIONS: dict[str, dict[str, Any]] = {
        "prioritize": {
            "cls": "priority", "label": "Prioritize",
            "count": "prioritizable",
            "not_offered": ErrorCode.PRIORITIZE_NOT_OFFERED,
        },
        "abandon": {
            "cls": "abandon", "label": "Abandon",
            "count": "abandonable",
            "not_offered": ErrorCode.ABANDON_NOT_OFFERED,
        },
    }

    def prioritize(
        self,
        job: str,
        buildno: int,
        idx: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prioritize a build's sessions in the test queue.

        Does what the queue page does when Prioritize is picked on a
        row: "This session" (``idx`` given) or "Entire build" (no
        ``idx``).  Access-controlled: Maloo offers it only to accounts
        given the prioritize permission, which many lack, and in
        practice on sessions still waiting for a test cluster.  See
        :meth:`queue_action`.
        """
        return self.queue_action("prioritize", job, buildno, idx, dry_run)

    def abandon(
        self,
        job: str,
        buildno: int,
        idx: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Abandon a build's sessions in the test queue.

        Does what the queue page does when Abandon is picked on a row:
        "This session" (``idx`` given) or "Entire build" (no ``idx``).
        Access-controlled: Maloo offers it only to accounts given the
        abandon permission, and by the session's state.  See
        :meth:`queue_action`.
        """
        return self.queue_action("abandon", job, buildno, idx, dry_run)

    def queue_action(
        self,
        call: str,
        job: str,
        buildno: int,
        idx: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prioritize or abandon a build's sessions in the test queue.

        The queue page posts ``call`` (prioritize or abandon),
        ``kind=idx`` ("This session") or ``kind=all`` ("Entire build"),
        and the ``session`` key of the row picked to
        /test_queue/actionable.  It is not in the REST API.

        The page is read first, and nothing is sent unless it offers
        the action on that row (for the whole build, on some row of
        it).  Both actions are access-controlled: Maloo shows them only
        to accounts given the permission, then by the session's state,
        so an account without it is refused on every row.  The post is
        not retried.

        Args:
            call: "prioritize" or "abandon".
            job: Jenkins job of the build (e.g. "lustre-reviews").
            buildno: Jenkins build number.
            idx: The session's queue idx, or None for the whole build.
            dry_run: Check the page and return the request unsent.

        Returns:
            The request, whether it was sent, and Maloo's reply.

        Raises:
            NotFoundError: QUEUE_ENTRY_NOT_FOUND, the build (or that
                session of it) is not in the test queue.
            ToolError: PRIORITIZE_NOT_OFFERED or ABANDON_NOT_OFFERED,
                the page does not offer the action there.
            RuntimeError: the page had no CSRF token.
        """
        action = self.QUEUE_ACTIONS[call]
        label = action["label"]
        web, page = self.get_queue_page(job, buildno)
        rows = [
            r for r in self.parse_queue_rows(page)
            if r["job"] == job and r["buildno"] == buildno
        ]
        offered = [r for r in rows if action["cls"] in r["actions"]]
        statuses: dict[str, int] = {}
        for r in rows:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        details = {"sessions": len(rows), action["count"]: len(offered),
                   "statuses": statuses}

        if idx is not None:
            target = next((r for r in rows if r["idx"] == idx), None)
            if target is None:
                raise NotFoundError(
                    ErrorCode.QUEUE_ENTRY_NOT_FOUND,
                    f"{job} build {buildno} has no session idx {idx} "
                    "in the test queue",
                    details=details,
                )
            if action["cls"] not in target["actions"]:
                raise ToolError(
                    action["not_offered"],
                    f"The test queue does not offer {label} for {job} "
                    f"build {buildno} idx {idx} (status "
                    f"{target['status'] or 'unknown'}); it offers it on "
                    f"{len(offered)} of the build's {len(rows)} sessions"
                    + (f"; offered nowhere, so this Maloo account may "
                       f"lack the {call} permission" if not offered
                       else ""),
                    details=details,
                )
        else:
            if not rows:
                raise NotFoundError(
                    ErrorCode.QUEUE_ENTRY_NOT_FOUND,
                    f"{job} build {buildno} has no sessions in the test "
                    "queue",
                    details=details,
                )
            if not offered:
                raise ToolError(
                    action["not_offered"],
                    f"The test queue does not offer {label} on any of "
                    f"{job} build {buildno}'s {len(rows)} sessions; "
                    f"this Maloo account may lack the {call} permission",
                    details=details,
                )
            target = offered[0]

        token = CSRF_RE.search(page)
        if not token:
            raise RuntimeError(
                "Could not extract CSRF token from the test queue page"
            )
        data = {
            "kind": "idx" if idx is not None else "all",
            "session": target["key"],
            "call": call,
        }
        result: dict[str, Any] = {
            "job": job,
            "buildno": buildno,
            "idx": idx,
            "scope": "session" if idx is not None else "build",
            "request": {"path": QUEUE_ACTION_PATH, "data": data},
            **details,
            "sent": False,
        }
        if dry_run:
            return result

        resp = web.post(
            f"{self.config.base_url}{QUEUE_ACTION_PATH}",
            data={**data, "authenticity_token": token.group(1)},
            headers={"X-CSRF-Token": token.group(1),
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        resp.raise_for_status()
        result["sent"] = True
        result["http_status"] = resp.status_code
        result["response"] = resp.text.strip()[:500]
        return result

    # -- Test-Parameter request (web page script, not REST API) --

    @staticmethod
    def tp_request_lines(
        params: Iterable[str], optional: bool = False
    ) -> list[str]:
        """The 'Test-Parameters:' lines for a Test-Parameter request.

        Each entry may hold several lines and may leave out the
        'Test-Parameters:' prefix.  ``optional`` adds signofftest=false
        to a line that does not set signofftest.

        Raises:
            InvalidInputError: no line, or an attribute the related
                sessions page refuses (optional, trivial, ...).
        """
        lines = []
        for entry in params:
            for line in entry.splitlines():
                line = line.strip()
                if line[:len(TP_PREFIX)].lower() == TP_PREFIX.lower():
                    line = line[len(TP_PREFIX):].strip()
                if not line:
                    continue
                bad = TP_NOT_ALLOWED_RE.search(line)
                if bad:
                    raise InvalidInputError(
                        ErrorCode.INVALID_INPUT,
                        f"'{bad.group(0)}' cannot be requested for an "
                        "existing build (not allowed: forjanitoronly, "
                        "forbuildonly, fortestonly, ignore, optional, "
                        "trivial). Once Maloo has voted, requested "
                        "sessions are optional; before that, "
                        "signofftest=false (--optional) makes them so",
                    )
                if optional and "signofftest=" not in line:
                    line += " signofftest=false"
                lines.append(f"{TP_PREFIX} {line}")
        if not lines:
            raise InvalidInputError(
                ErrorCode.INVALID_INPUT, "No Test-Parameters line given"
            )
        return lines

    def request_tests(
        self,
        job: str,
        buildno: int,
        lines: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request additional test sessions for an existing build.

        Does what the related sessions page's "Test-Parameter request"
        does: posts the lines from :meth:`tp_request_lines` to
        /test_sessions/related_retest.  Maloo runs them in addition to
        the build's sessions; they are enforced unless Maloo has already
        voted on the patchset or a line sets signofftest=false.  They
        reach the test queue a few minutes later.  The post is not
        retried.

        Raises:
            ToolError: TEST_REQUEST_FAILED, Maloo refused the request
                (e.g. the build is not in Jenkins); its reason is the
                message.
            RuntimeError: the page had no CSRF token.
        """
        data = {"job": job, "build": buildno, "name": "Test-Parameter",
                "tp": "\n".join(lines)}
        result: dict[str, Any] = {
            "job": job,
            "buildno": buildno,
            "test_parameters": lines,
            "request": {"path": TP_REQUEST_PATH, "data": data},
            "sent": False,
        }
        if dry_run:
            return result

        web = self._web_login()
        page = self._retried(
            web.get, f"{self.config.base_url}/test_sessions/related",
            timeout=60,
        )
        page.raise_for_status()
        token = CSRF_RE.search(page.text)
        if not token:
            raise RuntimeError(
                "Could not extract CSRF token from the related sessions page"
            )
        resp = web.post(
            f"{self.config.base_url}{TP_REQUEST_PATH}",
            data=data,
            headers={"X-CSRF-Token": token.group(1),
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        reply = resp.text.strip()[:500]
        # Maloo answers a refused request with 406 and its reason as text
        if 400 <= resp.status_code < 500:
            raise ToolError(
                ErrorCode.TEST_REQUEST_FAILED,
                f"Maloo refused the request for {job} build {buildno}: "
                f"{reply or f'HTTP {resp.status_code}'}",
                http_status=resp.status_code,
            )
        resp.raise_for_status()
        result["sent"] = True
        result["http_status"] = resp.status_code
        result["response"] = reply
        return result

    # -- Raise bug (web form, not REST API) --

    def raise_bug(
        self,
        buggable_id: str,
        buggable_type: str = "TestSet",
        project: str = "LU",
        summary: str = "",
        description: str = "",
        component: str = "",
        version: str = "",
    ) -> dict[str, str]:
        """Raise a new JIRA bug via Maloo's web form.

        This uses the same "Raise bug..." button from the Maloo web UI.
        Maloo creates the JIRA issue and auto-links it to the test
        failure.

        Args:
            buggable_id: Test set or subtest UUID.
            buggable_type: "TestSet" or "SubTest".
            project: JIRA project (LU, EX, DDN, etc.).
            summary: Bug summary line, used as given.  Required.
            description: Triage details.  Required.  They go in place
                of the placeholder at the end of Maloo's default
                description (test set link, the subtest's error,
                session details), or after it if it has none.
            component: Jira component.  Required for a project that
                the form lists in JIRA_ATTRS; refused for any other.
            version: Affected version.  Same rule as component.

        Raises:
            InvalidInputError: a required value is missing, or a value
                is not one the form offers.  The error names every such
                value at once, with the choices.  Nothing is submitted.

        Returns:
            Dict with 'ticket' (e.g. "LU-12345") and 'url' keys.
        """
        web = self._web_login()

        # Step 1: GET the form to extract defaults (pre-populated
        # description with session links, etc.)
        form_url = (
            f"{self.config.base_url}/bug_references/new"
            f"?buggable_id={buggable_id}"
            f"&buggable_type={buggable_type}"
        )
        resp = self._retried(web.get, form_url, timeout=30)
        resp.raise_for_status()

        extra = _check_raise_bug_input(
            resp.text, project, summary, description, component, version)
        description = _raise_bug_description(resp.text, description)
        before = _linked_tickets(
            self.get_bug_links(buggable_id, buggable_type))

        # Step 2: Submit the form (method=GET with commit=Create issue)
        params = {
            "buggable_id": buggable_id,
            "buggable_type": buggable_type,
            "project": project,
            "summary": summary,
            "description": description,
            **extra,
            "commit": "Create issue",
        }
        resp = web.get(form_url, params=params, timeout=30)
        resp.raise_for_status()

        # Check for error
        m = re.search(
            r'class="flash (?:error|alert)"[^>]*>([^<]+)', resp.text
        )
        if m:
            raise RuntimeError(
                f"Raise bug failed: {m.group(1).strip()}"
            )
        m = re.search(
            r'class="flash success"[^>]*>([^<]+)', resp.text
        )
        flash_msg = m.group(1).strip() if m else ""

        # The submit lands on the test set page, which also shows
        # unrelated tickets (such as the Jira link of the Gerrit change
        # under test).  The new ticket is the bug link that was not
        # there before the submit.
        new = [
            t for t in _linked_tickets(
                self.get_bug_links(buggable_id, buggable_type))
            if t not in before
        ]
        if not new:
            raise RuntimeError(
                "Raise bug failed: Maloo reported no error, but no new "
                f"bug link is on {buggable_id}. Check `maloo bugs "
                f"{buggable_id}` and Jira project {project} before you "
                "try again."
            )
        ticket = new[0]
        m = re.search(
            rf'href="(https://[^"]+/browse/{re.escape(ticket)})"', resp.text
        )
        result = {
            "ticket": ticket,
            "url": m.group(1) if m else "",
            "flash": flash_msg,
        }
        if len(new) > 1:
            result["new_links"] = new
        return result


def _linked_tickets(links: list[dict[str, Any]]) -> list[str]:
    """Return the ticket keys of bug links, in order, without repeats."""
    keys = (link.get("jira") or link.get("bug_upstream_id") for link in links)
    return list(dict.fromkeys(k.upper() for k in keys if k))


def _raise_bug_form_projects(form_html: str) -> list[str]:
    """Return the project choices of the Maloo raise-bug form."""
    m = re.search(
        r'<select[^>]*name="project"[^>]*>(.*?)</select>', form_html, re.S
    )
    if not m:
        return []
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(1)) if v]


def _raise_bug_form_attrs(form_html: str) -> dict[str, Any]:
    """Return the JIRA_ATTRS object of the Maloo raise-bug form.

    The form script fills the component and version lists from it for
    each project that it names.  An empty dict means none do.
    """
    marker = "const JIRA_ATTRS = "
    i = form_html.find(marker)
    if i < 0:
        return {}
    try:
        attrs, _ = json.JSONDecoder().raw_decode(form_html, i + len(marker))
    except ValueError:
        return {}
    return attrs if isinstance(attrs, dict) else {}


_DESCRIPTION_PLACEHOLDER = re.compile(r"<<Please provide additional[^>]*>>")


def _raise_bug_form_text(form_html: str, name: str) -> str:
    """Return the default text of textarea NAME on the raise-bug form."""
    m = re.search(
        rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>',
        form_html, re.DOTALL,
    )
    return html.unescape(m.group(1)).strip() if m else ""


def _raise_bug_description(form_html: str, details: str) -> str:
    """Put DETAILS into Maloo's default description.

    DETAILS replace the form's "<<Please provide additional
    information ...>>" placeholder, or follow the default if it has
    none.
    """
    default = _raise_bug_form_text(form_html, "description")
    if not default:
        return details
    if _DESCRIPTION_PLACEHOLDER.search(default):
        return _DESCRIPTION_PLACEHOLDER.sub(lambda _: details, default, 1)
    return f"{default}\n\n{details}"


def _check_raise_bug_input(
    form_html: str, project: str, summary: str, description: str,
    component: str, version: str,
) -> dict[str, str]:
    """Check the raise-bug values against the Maloo form.

    Return the extra form fields to submit.  Raise one
    InvalidInputError that names every missing or bad value, with the
    choices, before anything is submitted.
    """
    problems: list[str] = []
    details: dict[str, Any] = {}

    if not summary.strip():
        prefix = re.sub(
            r"<<[^>]*>>", "",
            _raise_bug_form_text(form_html, "summary")).strip()
        # A subtest's form gives "<suite> test_<n>:", a test set's only
        # "<suite>:".
        if " test_" not in prefix:
            prefix = f"{prefix.rstrip(':') or '<suite>'} test_<n>:"
        example = f"{prefix} <@@@@@@ FAIL message>"
        problems.append(f"--summary is required, e.g. '{example}'")
    if not description.strip():
        problems.append(
            "--description is required: the triage details. They are "
            "added to Maloo's default description, which already has "
            "the test set link, the error and the session details")

    projects = _raise_bug_form_projects(form_html)
    attrs: dict[str, Any] = {}
    if projects and project not in projects:
        problems.append(
            f"--project {project!r} is not offered. "
            f"Choices: {', '.join(projects)}")
        details["projects"] = projects
    else:
        attrs = _raise_bug_form_attrs(form_html).get(project) or {}

    extra: dict[str, str] = {}
    if attrs:
        versions = sorted(attrs.get("versions") or {})
        open_versions = sorted(
            n for n, v in (attrs.get("versions") or {}).items()
            if not v.get("released") and not v.get("archived")
        )
        components = sorted(attrs.get("components") or {})
        details.update(open_versions=open_versions, versions=versions,
                       components=components)
        if version not in versions:
            what = "is required" if not version else (
                f"{version!r} is not a {project} version")
            problems.append(
                f"--version {what} for {project}. Unreleased versions: "
                f"{', '.join(open_versions)}")
        if component not in components:
            what = "is required" if not component else (
                f"{component!r} is not a {project} component")
            problems.append(
                f"--component {what} for {project}. Choices: "
                f"{', '.join(components)}")
        extra = {"component": component, "version": version}
    elif "projects" not in details:
        for name, value in (("--component", component),
                            ("--version", version)):
            if value:
                problems.append(f"{name} is not taken by project {project}")

    if problems:
        raise InvalidInputError(
            ErrorCode.INVALID_INPUT,
            "raise-bug input is incomplete or wrong; nothing was sent. "
            + " | ".join(problems),
            details={"problems": problems, **details},
        )
    return extra
