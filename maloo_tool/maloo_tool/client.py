"""Maloo REST API client."""

import re
from typing import Any

import requests

from .config import MalooConfig

CSRF_RE = re.compile(
    r'<meta\s+name="csrf-token"\s+content="([^"]+)"'
)


class MalooClient:
    """Client for the Maloo test results API."""

    def __init__(self, config: MalooConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.auth = (config.username, config.password)

    def _get(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Make a GET request and return the data array."""
        url = f"{self.config.base_url}/api/{endpoint}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, list):
            return body
        return body.get("data", [])

    def _get_all(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """GET with automatic pagination (200-record pages)."""
        params = dict(params) if params else {}
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            params["offset"] = offset
            page = self._get(endpoint, params)
            results.extend(page)
            if len(page) < 200:
                break
            offset += 200
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
        if max_records <= 0:
            return self._get_all("test_sessions", params)
        params = dict(params)
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < max_records:
            params["offset"] = offset
            page = self._get("test_sessions", params)
            results.extend(page)
            if len(page) < 200:
                break
            offset += 200
        return results[:max_records]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a single test session by ID."""
        rows = self._get("test_sessions", {"id": session_id})
        return rows[0] if rows else None

    def get_session_full(
        self, session_id: str
    ) -> dict[str, Any] | None:
        """Get a test session with test_sets and sub_tests."""
        rows = self._get(
            "test_sessions",
            {"id": session_id, "related": "['test_sets','sub_tests']"},
        )
        return rows[0] if rows else None

    def find_sessions_by_review(
        self, review_id: int, patch: int | None = None
    ) -> list[dict[str, Any]]:
        """Find test sessions for a Gerrit review via code_reviews."""
        params: dict[str, Any] = {"review_id": review_id}
        if patch is not None:
            params["review_patch"] = patch
        # First get the code reviews to find session IDs
        reviews = self._get_all("code_reviews", params)
        if not reviews:
            # Try via test_queues as fallback
            qparams: dict[str, Any] = {"review_id": review_id}
            if patch is not None:
                qparams["review_patch"] = patch
            return self._get_all("test_queues", qparams)
        # Fetch the actual sessions
        session_ids = {r["test_session_id"] for r in reviews}
        sessions = []
        for sid in session_ids:
            s = self.get_session(sid)
            if s:
                sessions.append(s)
        return sessions

    # -- Test Sets (suites) --

    def get_test_sets(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Get test sets for a session."""
        return self._get_all(
            "test_sets", {"test_session_id": session_id}
        )

    def get_test_set(
        self, test_set_id: str
    ) -> dict[str, Any] | None:
        """Get a single test set by ID."""
        rows = self._get("test_sets", {"id": test_set_id})
        return rows[0] if rows else None

    def get_test_set_with_subtests(
        self, test_set_id: str
    ) -> dict[str, Any] | None:
        """Get a test set with its child subtests."""
        rows = self._get(
            "test_sets",
            {"id": test_set_id, "related": "true"},
        )
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
        return self._get_all("sub_tests", params)

    # -- Script names (for resolving IDs to names) --

    def get_test_set_script(
        self, script_id: str
    ) -> dict[str, Any] | None:
        """Get test set script (suite name) by ID."""
        rows = self._get("test_set_scripts", {"id": script_id})
        return rows[0] if rows else None

    def get_sub_test_script(
        self, script_id: str
    ) -> dict[str, Any] | None:
        """Get sub test script (test name) by ID."""
        rows = self._get("sub_test_scripts", {"id": script_id})
        return rows[0] if rows else None

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

    # -- Test nodes --

    def get_test_nodes(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Get test nodes for a session."""
        return self._get_all(
            "test_nodes", {"test_session_id": session_id}
        )

    # -- Code reviews --

    def get_code_reviews(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Get code review info for a session."""
        return self._get_all(
            "code_reviews", {"test_session_id": session_id}
        )

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
        return self._get_all("bug_links", params)

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
        if max_records <= 0:
            return self._get_all("test_queues", params)
        params = dict(params)
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < max_records:
            params["offset"] = offset
            page = self._get("test_queues", params)
            results.extend(page)
            if len(page) < 200:
                break
            offset += 200
        return results[:max_records]

    # -- Test history --

    def find_sub_test_script_id(
        self, name: str
    ) -> str | None:
        """Find a sub_test_script ID by name."""
        rows = self._get("sub_test_scripts", {"name": name})
        return rows[0]["id"] if rows else None

    def find_test_set_script_id(
        self, name: str
    ) -> str | None:
        """Find a test_set_script ID by name."""
        rows = self._get("test_set_scripts", {"name": name})
        return rows[0]["id"] if rows else None

    def get_test_history(
        self,
        test_name: str,
        trigger_job: str,
        from_date: str,
        to_date: str,
        suite: str | None = None,
        max_sessions: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Get pass/fail history for a specific test.

        Args:
            test_name: Subtest name (e.g. "test_39b").
            trigger_job: Branch name (e.g. "lustre-master").
            from_date: Start date (yyyy-mm-dd).
            to_date: End date (yyyy-mm-dd).
            suite: Optional suite name to filter (e.g. "sanity").
            max_sessions: Max sessions to examine.

        Returns:
            (history_entries, suite_name_resolved)
            Each entry has: session_id, submission, test_host,
            suite, status, error, duration.
        """
        # Get sessions for the branch in the date range
        params: dict[str, Any] = {
            "trigger_job": trigger_job,
            "from": from_date,
            "to": to_date,
        }
        sessions = self.get_sessions(params, max_records=max_sessions)

        # If suite filter given, resolve its script ID for faster matching
        suite_script_id = None
        if suite:
            suite_script_id = self.find_test_set_script_id(suite)

        # Cache for script name lookups
        set_script_cache: dict[str, str] = {}
        sub_script_cache: dict[str, str] = {}

        history: list[dict[str, Any]] = []
        resolved_suite = suite

        for sess in sessions:
            sid = sess["id"]
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
        return history, resolved_suite

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
        return result, sessions_total, sessions_total

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
        resp = web.get(signin_url, timeout=30)
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

        web.post(
            f"{self.config.base_url}{action}",
            data={
                "authenticity_token": m.group(1),
                "email": self.config.username,
                "password": self.config.password,
                "commit": "Sign in",
            },
            timeout=30,
            allow_redirects=True,
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
        resp = web.get(url, timeout=30)
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
        resp = self.session.get(
            url, timeout=timeout, allow_redirects=True
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise RuntimeError(
                "Log download returned HTML (auth may have failed)"
            )
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
