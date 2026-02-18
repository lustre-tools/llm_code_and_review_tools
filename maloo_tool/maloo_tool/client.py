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

    # -- Retest (web form, not REST API) --

    def _get_csrf_token(self, session_id: str) -> str:
        """Fetch the CSRF token from the test session page."""
        url = f"{self.config.base_url}/test_sessions/{session_id}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        m = CSRF_RE.search(resp.text)
        if not m:
            raise RuntimeError("Could not extract CSRF token from session page")
        return m.group(1)

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
        token = self._get_csrf_token(session_id)
        url = (
            f"{self.config.base_url}"
            f"/test_sessions/{session_id}/retest"
        )
        data = {
            "authenticity_token": token,
            "retest_option": option,
            "bug_id": bug_id,
        }
        resp = self.session.post(url, data=data, timeout=30)
        resp.raise_for_status()
        return f"HTTP {resp.status_code}"
