"""Unit tests for MalooClient methods.

All HTTP calls are mocked via unittest.mock patches on requests.Session.
"""

import json
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest
import requests

from maloo_tool.client import MalooClient
from maloo_tool.config import MalooConfig


@pytest.fixture
def config():
    """Create test config."""
    return MalooConfig(
        base_url="https://testing.example.com",
        username="testuser",
        password="testpass",
    )


@pytest.fixture
def client(config):
    """Create test client with mocked session."""
    c = MalooClient(config)
    c.session = MagicMock(spec=requests.Session)
    return c


def _mock_response(json_data=None, status_code=200, text="", content=b"",
                   headers=None, raise_for_status=None):
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
    if raise_for_status:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

class TestClientInit:
    def test_creation(self, config):
        client = MalooClient(config)
        assert client.config is config
        assert client.session.auth == ("testuser", "testpass")

    def test_base_url_trailing_slash_stripped(self):
        config = MalooConfig(
            base_url="https://testing.example.com/",
            username="u", password="p",
        )
        assert config.base_url == "https://testing.example.com"


# ---------------------------------------------------------------------------
# _get (low-level GET helper)
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_returns_list_body(self, client):
        """When API returns a bare list, _get should return it directly."""
        data = [{"id": "a"}, {"id": "b"}]
        client.session.get.return_value = _mock_response(json_data=data)
        result = client._get("test_sessions", {"id": "a"})
        assert result == data
        client.session.get.assert_called_once_with(
            "https://testing.example.com/api/test_sessions",
            params={"id": "a"},
            timeout=30,
        )

    def test_get_returns_data_field(self, client):
        """When API returns {data: [...]}, _get should unwrap it."""
        data = [{"id": "x"}]
        client.session.get.return_value = _mock_response(
            json_data={"data": data, "total": 1}
        )
        result = client._get("test_sets")
        assert result == data

    def test_get_empty_data_field(self, client):
        """When API returns {data: []}, _get should return empty list."""
        client.session.get.return_value = _mock_response(
            json_data={"data": []}
        )
        result = client._get("test_sessions")
        assert result == []

    def test_get_raises_on_http_error(self, client):
        """_get should propagate HTTP errors."""
        client.session.get.return_value = _mock_response(
            status_code=401,
            raise_for_status=requests.exceptions.HTTPError("401 Unauthorized"),
        )
        with pytest.raises(requests.exceptions.HTTPError):
            client._get("test_sessions")

    def test_get_network_error(self, client):
        """_get should propagate network errors."""
        client.session.get.side_effect = requests.exceptions.ConnectionError(
            "Connection refused"
        )
        with pytest.raises(requests.exceptions.ConnectionError):
            client._get("test_sessions")

    def test_get_timeout(self, client):
        """_get should propagate timeout errors."""
        client.session.get.side_effect = requests.exceptions.Timeout(
            "Read timed out"
        )
        with pytest.raises(requests.exceptions.Timeout):
            client._get("test_sessions")


# ---------------------------------------------------------------------------
# _get_paginated
# ---------------------------------------------------------------------------

class TestGetPaginated:
    def test_single_page(self, client):
        """When page has < 200 records, no second request."""
        page = [{"id": str(i)} for i in range(50)]
        client._get = MagicMock(return_value=page)
        result = client._get_paginated("test_sessions", {})
        assert len(result) == 50
        assert client._get.call_count == 1

    def test_multiple_pages(self, client):
        """Should fetch multiple pages until a short page."""
        page1 = [{"id": str(i)} for i in range(200)]
        page2 = [{"id": str(i)} for i in range(200, 350)]
        client._get = MagicMock(side_effect=[page1, page2])
        result = client._get_paginated("test_sessions", {})
        assert len(result) == 350
        assert client._get.call_count == 2
        # Verify offsets were passed (params dict is copied, check via call args)
        first_call_params = client._get.call_args_list[0][0][1]
        second_call_params = client._get.call_args_list[1][0][1]
        assert first_call_params.get("offset") is not None
        assert second_call_params.get("offset") is not None
        # Second call should have offset 200 (after first page of 200)
        assert second_call_params["offset"] == 200

    def test_max_records_limits_results(self, client):
        """max_records should truncate results."""
        page = [{"id": str(i)} for i in range(200)]
        client._get = MagicMock(return_value=page)
        result = client._get_paginated("test_sessions", {}, max_records=50)
        assert len(result) == 50

    def test_max_records_stops_early(self, client):
        """When max_records reached, don't fetch next page."""
        page = [{"id": str(i)} for i in range(200)]
        client._get = MagicMock(return_value=page)
        result = client._get_paginated("test_sessions", {}, max_records=200)
        assert len(result) == 200
        # Should still only make 1 call since first page filled max_records
        assert client._get.call_count == 1

    def test_zero_max_records_means_no_limit(self, client):
        """max_records=0 should fetch all pages."""
        page1 = [{"id": str(i)} for i in range(200)]
        page2 = [{"id": str(i)} for i in range(200, 250)]
        client._get = MagicMock(side_effect=[page1, page2])
        result = client._get_paginated("test_sessions", {}, max_records=0)
        assert len(result) == 250

    def test_preserves_existing_params(self, client):
        """Should not clobber caller-supplied params."""
        client._get = MagicMock(return_value=[])
        client._get_paginated("test_sessions", {"trigger_job": "lustre-master"})
        params = client._get.call_args[0][1]
        assert params["trigger_job"] == "lustre-master"
        assert params["offset"] == 0

    def test_does_not_mutate_original_params(self, client):
        """Should not mutate the original params dict."""
        original = {"trigger_job": "lustre-master"}
        client._get = MagicMock(return_value=[])
        client._get_paginated("test_sessions", original)
        assert "offset" not in original


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------

class TestGetSession:
    def test_found(self, client):
        session_data = {"id": "abc-123", "test_group": "full"}
        client._get = MagicMock(return_value=[session_data])
        result = client.get_session("abc-123")
        assert result == session_data
        client._get.assert_called_once_with(
            "test_sessions", {"id": "abc-123"}
        )

    def test_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.get_session("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# get_sessions
# ---------------------------------------------------------------------------

class TestGetSessions:
    def test_delegates_to_paginated(self, client):
        """get_sessions should delegate to _get_paginated."""
        client._get_paginated = MagicMock(return_value=[{"id": "s1"}])
        params = {"trigger_job": "lustre-master"}
        result = client.get_sessions(params, max_records=10)
        client._get_paginated.assert_called_once_with(
            "test_sessions", params, 10
        )
        assert result == [{"id": "s1"}]


# ---------------------------------------------------------------------------
# find_sessions_by_review
# ---------------------------------------------------------------------------

class TestFindSessionsByReview:
    def test_via_code_reviews(self, client):
        """Should find sessions through code_reviews endpoint."""
        reviews = [
            {"test_session_id": "s1"},
            {"test_session_id": "s2"},
        ]
        session_1 = {"id": "s1", "test_group": "full"}
        session_2 = {"id": "s2", "test_group": "full"}

        client._get_paginated = MagicMock(return_value=reviews)
        client.get_session = MagicMock(side_effect=[session_1, session_2])

        result = client.find_sessions_by_review(64266)
        assert len(result) == 2
        client._get_paginated.assert_called_once_with(
            "code_reviews", {"review_id": 64266}
        )

    def test_fallback_to_test_queues(self, client):
        """When code_reviews returns nothing, fall back to test_queues."""
        queue_data = [{"id": "q1", "test_group": "full"}]
        client._get_paginated = MagicMock(side_effect=[[], queue_data])

        result = client.find_sessions_by_review(64266)
        assert result == queue_data
        calls = client._get_paginated.call_args_list
        assert calls[0][0][0] == "code_reviews"
        assert calls[1][0][0] == "test_queues"

    def test_with_patch_number(self, client):
        """Should pass patch number to the query."""
        client._get_paginated = MagicMock(return_value=[])
        client.find_sessions_by_review(64266, patch=3)
        params = client._get_paginated.call_args[0][1]
        assert params["review_id"] == 64266
        assert params["review_patch"] == 3

    def test_deduplicates_sessions(self, client):
        """When multiple reviews point to same session, should deduplicate."""
        reviews = [
            {"test_session_id": "s1"},
            {"test_session_id": "s1"},
        ]
        session = {"id": "s1", "test_group": "full"}
        client._get_paginated = MagicMock(return_value=reviews)
        client.get_session = MagicMock(return_value=session)

        result = client.find_sessions_by_review(64266)
        assert len(result) == 1
        # get_session called once because set deduplicates
        client.get_session.assert_called_once_with("s1")


# ---------------------------------------------------------------------------
# get_test_sets / get_test_set
# ---------------------------------------------------------------------------

class TestTestSets:
    def test_get_test_sets(self, client):
        client._get_paginated = MagicMock(return_value=[{"id": "ts1"}])
        result = client.get_test_sets("session-123")
        client._get_paginated.assert_called_once_with(
            "test_sets", {"test_session_id": "session-123"}
        )
        assert result == [{"id": "ts1"}]

    def test_get_test_set_found(self, client):
        client._get = MagicMock(return_value=[{"id": "ts1", "status": "FAIL"}])
        result = client.get_test_set("ts1")
        assert result == {"id": "ts1", "status": "FAIL"}

    def test_get_test_set_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.get_test_set("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# get_subtests
# ---------------------------------------------------------------------------

class TestGetSubtests:
    def test_by_test_set(self, client):
        client._get_paginated = MagicMock(return_value=[])
        client.get_subtests(test_set_id="ts1")
        client._get_paginated.assert_called_once_with(
            "sub_tests", {"test_set_id": "ts1"}
        )

    def test_by_session(self, client):
        client._get_paginated = MagicMock(return_value=[])
        client.get_subtests(session_id="s1")
        client._get_paginated.assert_called_once_with(
            "sub_tests", {"test_session_id": "s1"}
        )

    def test_both_params(self, client):
        client._get_paginated = MagicMock(return_value=[])
        client.get_subtests(test_set_id="ts1", session_id="s1")
        params = client._get_paginated.call_args[0][1]
        assert params["test_set_id"] == "ts1"
        assert params["test_session_id"] == "s1"

    def test_no_params(self, client):
        """Should work with no filter params (though unlikely in practice)."""
        client._get_paginated = MagicMock(return_value=[])
        client.get_subtests()
        client._get_paginated.assert_called_once_with("sub_tests", {})


# ---------------------------------------------------------------------------
# Script name resolution
# ---------------------------------------------------------------------------

class TestScriptResolution:
    def test_get_test_set_script_found(self, client):
        client._get = MagicMock(return_value=[{"id": "sc1", "name": "sanity"}])
        result = client.get_test_set_script("sc1")
        assert result == {"id": "sc1", "name": "sanity"}

    def test_get_test_set_script_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.get_test_set_script("nonexistent")
        assert result is None

    def test_get_sub_test_script_found(self, client):
        client._get = MagicMock(return_value=[{"id": "st1", "name": "test_39b"}])
        result = client.get_sub_test_script("st1")
        assert result == {"id": "st1", "name": "test_39b"}

    def test_get_sub_test_script_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.get_sub_test_script("nonexistent")
        assert result is None

    def test_find_sub_test_script_id(self, client):
        client._get = MagicMock(return_value=[{"id": "st1", "name": "test_39b"}])
        result = client.find_sub_test_script_id("test_39b")
        assert result == "st1"

    def test_find_sub_test_script_id_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.find_sub_test_script_id("nonexistent")
        assert result is None

    def test_find_test_set_script_id(self, client):
        client._get = MagicMock(return_value=[{"id": "sc1", "name": "sanity"}])
        result = client.find_test_set_script_id("sanity")
        assert result == "sc1"

    def test_find_test_set_script_id_not_found(self, client):
        client._get = MagicMock(return_value=[])
        result = client.find_test_set_script_id("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Batch name resolution
# ---------------------------------------------------------------------------

class TestBatchNameResolution:
    def test_resolve_test_set_names(self, client):
        test_sets = [
            {"id": "ts1", "test_set_script_id": "sc1"},
            {"id": "ts2", "test_set_script_id": "sc2"},
        ]
        script_map = {
            "sc1": {"id": "sc1", "name": "sanity"},
            "sc2": {"id": "sc2", "name": "replay-vbr"},
        }
        client.get_test_set_script = MagicMock(
            side_effect=lambda sid: script_map[sid]
        )
        names = client.resolve_test_set_names(test_sets)
        assert names == {"sc1": "sanity", "sc2": "replay-vbr"}

    def test_resolve_test_set_names_missing_script(self, client):
        """Sets without test_set_script_id should be skipped."""
        test_sets = [
            {"id": "ts1"},  # no test_set_script_id
            {"id": "ts2", "test_set_script_id": "sc2"},
        ]
        client.get_test_set_script = MagicMock(
            return_value={"id": "sc2", "name": "sanity"}
        )
        names = client.resolve_test_set_names(test_sets)
        assert names == {"sc2": "sanity"}
        client.get_test_set_script.assert_called_once()

    def test_resolve_test_set_names_deduplicates(self, client):
        """Same script_id should only be looked up once."""
        test_sets = [
            {"id": "ts1", "test_set_script_id": "sc1"},
            {"id": "ts2", "test_set_script_id": "sc1"},
        ]
        client.get_test_set_script = MagicMock(
            return_value={"id": "sc1", "name": "sanity"}
        )
        names = client.resolve_test_set_names(test_sets)
        assert names == {"sc1": "sanity"}
        client.get_test_set_script.assert_called_once()

    def test_resolve_subtest_names(self, client):
        subtests = [
            {"sub_test_script_id": "st1"},
            {"sub_test_script_id": "st2"},
        ]
        # Use a function for side_effect since set iteration order is non-deterministic
        script_map = {
            "st1": {"id": "st1", "name": "test_1a"},
            "st2": {"id": "st2", "name": "test_1b"},
        }
        client.get_sub_test_script = MagicMock(
            side_effect=lambda sid: script_map[sid]
        )
        names = client.resolve_subtest_names(subtests)
        assert names == {"st1": "test_1a", "st2": "test_1b"}

    def test_resolve_subtest_names_script_not_found(self, client):
        """When get_sub_test_script returns None, skip it."""
        subtests = [{"sub_test_script_id": "st1"}]
        client.get_sub_test_script = MagicMock(return_value=None)
        names = client.resolve_subtest_names(subtests)
        assert names == {}


# ---------------------------------------------------------------------------
# Bug links
# ---------------------------------------------------------------------------

class TestBugLinks:
    def test_get_bug_links(self, client):
        links = [{"bug_upstream_id": "LU-12345", "buggable_id": "ts1"}]
        client._get_paginated = MagicMock(return_value=links)
        result = client.get_bug_links("ts1")
        assert result == links
        client._get_paginated.assert_called_once_with(
            "bug_links", {"buggable_id": "ts1"}
        )

    def test_get_bug_links_with_type(self, client):
        client._get_paginated = MagicMock(return_value=[])
        client.get_bug_links("ts1", buggable_type="TestSet")
        params = client._get_paginated.call_args[0][1]
        assert params["buggable_type"] == "TestSet"

    def test_get_bug_links_related(self, client):
        client._get_paginated = MagicMock(return_value=[])
        client.get_bug_links("ts1", related=True)
        params = client._get_paginated.call_args[0][1]
        assert params["related"] == "true"

    def test_create_bug_link_success(self, client):
        client.session.post.return_value = _mock_response(text="OK")
        result = client.create_bug_link(
            buggable_class="TestSet",
            buggable_id="ts1",
            bug_upstream_id="LU-12345",
        )
        assert result == "OK"
        client.session.post.assert_called_once()
        call_kwargs = client.session.post.call_args
        assert call_kwargs[1]["params"]["buggable_class"] == "TestSet"
        assert call_kwargs[1]["params"]["bug_upstream_id"] == "LU-12345"
        assert call_kwargs[1]["params"]["bug_state"] == "accepted"

    def test_create_bug_link_custom_state(self, client):
        client.session.post.return_value = _mock_response(text="OK")
        client.create_bug_link(
            buggable_class="SubTest",
            buggable_id="st1",
            bug_upstream_id="LU-99999",
            bug_state="pending",
        )
        params = client.session.post.call_args[1]["params"]
        assert params["bug_state"] == "pending"
        assert params["buggable_class"] == "SubTest"

    def test_create_bug_link_error_response(self, client):
        """Should return error text without raising."""
        client.session.post.return_value = _mock_response(
            text="ERROR: duplicate link"
        )
        result = client.create_bug_link(
            buggable_class="TestSet",
            buggable_id="ts1",
            bug_upstream_id="LU-12345",
        )
        assert result == "ERROR: duplicate link"

    def test_create_bug_link_http_error(self, client):
        client.session.post.return_value = _mock_response(
            status_code=500,
            raise_for_status=requests.exceptions.HTTPError("500"),
        )
        with pytest.raises(requests.exceptions.HTTPError):
            client.create_bug_link(
                buggable_class="TestSet",
                buggable_id="ts1",
                bug_upstream_id="LU-12345",
            )


# ---------------------------------------------------------------------------
# get_test_queues
# ---------------------------------------------------------------------------

class TestGetTestQueues:
    def test_delegates_to_paginated(self, client):
        client._get_paginated = MagicMock(return_value=[{"id": "q1"}])
        params = {"review_id": "abc123"}
        result = client.get_test_queues(params, max_records=5)
        client._get_paginated.assert_called_once_with(
            "test_queues", params, 5
        )
        assert result == [{"id": "q1"}]


# ---------------------------------------------------------------------------
# Web login
# ---------------------------------------------------------------------------

class TestWebLogin:
    def test_web_login_extracts_csrf(self, client):
        """_web_login should extract CSRF token and POST credentials."""
        signin_html = (
            '<form action="/sessions">'
            '<input name="authenticity_token" value="csrf-tok-123" />'
            '</form>'
        )
        get_resp = _mock_response(text=signin_html)
        post_resp = _mock_response(text="logged in")

        # client.session needs a cookies attribute for the update call
        client.session.cookies = MagicMock()

        # Need a real Session for web login (it creates a new one)
        with patch("maloo_tool.client.requests.Session") as MockSession:
            web_session = MagicMock()
            MockSession.return_value = web_session
            web_session.get.return_value = get_resp
            web_session.post.return_value = post_resp
            # Reset cached web session
            client._web_session = None

            result = client._web_login()

            web_session.get.assert_called_once()
            web_session.post.assert_called_once()
            post_data = web_session.post.call_args[1]["data"]
            assert post_data["authenticity_token"] == "csrf-tok-123"
            assert post_data["email"] == "testuser"
            assert post_data["password"] == "testpass"

    def test_web_login_caches_session(self, client):
        """Second call should reuse the cached session."""
        cached = MagicMock()
        client._web_session = cached
        result = client._web_login()
        assert result is cached

    def test_web_login_no_csrf_raises(self, client):
        """Should raise RuntimeError if CSRF token not found."""
        with patch("maloo_tool.client.requests.Session") as MockSession:
            web_session = MagicMock()
            MockSession.return_value = web_session
            web_session.get.return_value = _mock_response(text="<html>no token</html>")
            client._web_session = None

            with pytest.raises(RuntimeError, match="CSRF token"):
                client._web_login()

    def test_web_login_custom_form_action(self, client):
        """Should use the form action URL from the HTML."""
        signin_html = (
            '<form action="/custom/login">'
            '<input name="authenticity_token" value="tok" />'
            '</form>'
        )
        client.session.cookies = MagicMock()
        with patch("maloo_tool.client.requests.Session") as MockSession:
            web_session = MagicMock()
            MockSession.return_value = web_session
            web_session.get.return_value = _mock_response(text=signin_html)
            web_session.post.return_value = _mock_response(text="ok")
            client._web_session = None

            client._web_login()
            post_url = web_session.post.call_args[0][0]
            assert post_url == "https://testing.example.com/custom/login"


# ---------------------------------------------------------------------------
# _get_csrf_token
# ---------------------------------------------------------------------------

class TestGetCsrfToken:
    def test_from_hidden_input(self, client):
        html = '<input name="authenticity_token" value="hidden-tok" />'
        web = MagicMock()
        web.get.return_value = _mock_response(text=html)
        token = client._get_csrf_token(web, "session-123")
        assert token == "hidden-tok"

    def test_from_meta_tag(self, client):
        html = '<meta name="csrf-token" content="meta-tok" />'
        web = MagicMock()
        web.get.return_value = _mock_response(text=html)
        token = client._get_csrf_token(web, "session-123")
        assert token == "meta-tok"

    def test_no_token_raises(self, client):
        web = MagicMock()
        web.get.return_value = _mock_response(text="<html>nothing</html>")
        with pytest.raises(RuntimeError, match="CSRF token"):
            client._get_csrf_token(web, "session-123")


# ---------------------------------------------------------------------------
# download_logs
# ---------------------------------------------------------------------------

class TestDownloadLogs:
    def test_success(self, client):
        """Should return raw bytes from download."""
        log_bytes = b"PK\x03\x04fake-zip-content"
        client._web_login = MagicMock()
        client.session.get.return_value = _mock_response(
            content=log_bytes,
            headers={"Content-Type": "application/zip"},
        )
        result = client.download_logs("ts1")
        assert result == log_bytes
        url_called = client.session.get.call_args[0][0]
        assert "/test_sets/ts1/download_logs" in url_called

    def test_html_response_raises(self, client):
        """Should raise if response is HTML (auth failure)."""
        client._web_login = MagicMock()
        client.session.get.return_value = _mock_response(
            content=b"<html>login page</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        with pytest.raises(RuntimeError, match="auth"):
            client.download_logs("ts1")

    def test_http_error(self, client):
        """Should propagate HTTP errors."""
        client._web_login = MagicMock()
        client.session.get.return_value = _mock_response(
            status_code=404,
            raise_for_status=requests.exceptions.HTTPError("404"),
        )
        with pytest.raises(requests.exceptions.HTTPError):
            client.download_logs("ts1")


# ---------------------------------------------------------------------------
# retest
# ---------------------------------------------------------------------------

class TestRetest:
    def test_success_flash(self, client):
        """Should return flash success message."""
        client._web_login = MagicMock(return_value=MagicMock())
        client._get_csrf_token = MagicMock(return_value="csrf-tok")

        web = client._web_login.return_value
        web.post.return_value = _mock_response(
            text='<div class="flash success">Retest queued successfully</div>'
        )

        result = client.retest("sid-123", option="single", bug_id="LU-19487")
        assert result == "Retest queued successfully"

        post_data = web.post.call_args[1]["data"]
        assert post_data["authenticity_token"] == "csrf-tok"
        assert post_data["retest_option"] == "single"
        assert post_data["bug_id"] == "LU-19487"

    def test_error_flash_raises(self, client):
        """Should raise RuntimeError on error flash."""
        client._web_login = MagicMock(return_value=MagicMock())
        client._get_csrf_token = MagicMock(return_value="csrf-tok")

        web = client._web_login.return_value
        web.post.return_value = _mock_response(
            text='<div class="flash error">Bug ID is required</div>'
        )

        with pytest.raises(RuntimeError, match="Bug ID is required"):
            client.retest("sid-123")

    def test_no_flash_returns_status(self, client):
        """When no flash message found, return HTTP status."""
        client._web_login = MagicMock(return_value=MagicMock())
        client._get_csrf_token = MagicMock(return_value="csrf-tok")

        web = client._web_login.return_value
        resp = _mock_response(text="<html>some page</html>")
        resp.status_code = 200
        web.post.return_value = resp

        result = client.retest("sid-123")
        assert result == "HTTP 200"

    def test_all_option(self, client):
        """Should pass 'all' as retest_option."""
        client._web_login = MagicMock(return_value=MagicMock())
        client._get_csrf_token = MagicMock(return_value="csrf-tok")
        web = client._web_login.return_value
        web.post.return_value = _mock_response(
            text='<div class="flash success">Queued</div>'
        )

        client.retest("sid-123", option="all", bug_id="LU-100")
        post_data = web.post.call_args[1]["data"]
        assert post_data["retest_option"] == "all"


# ---------------------------------------------------------------------------
# raise_bug
# ---------------------------------------------------------------------------

class TestRaiseBug:
    def _setup_raise_bug(self, client, form_html, result_html):
        """Helper to set up raise_bug mocks."""
        client._web_login = MagicMock(return_value=MagicMock())
        web = client._web_login.return_value
        web.get.side_effect = [
            _mock_response(text=form_html),   # GET form
            _mock_response(text=result_html),  # GET submit
        ]
        return web

    def test_success_with_ticket(self, client):
        form_html = (
            '<textarea name="description">Session: https://...</textarea>'
            '<textarea name="summary">sanity: </textarea>'
        )
        result_html = (
            '<div class="flash success">Created LU-99999</div>'
            '<a href="https://jira.whamcloud.com/browse/LU-99999">LU-99999</a>'
        )
        self._setup_raise_bug(client, form_html, result_html)

        result = client.raise_bug(buggable_id="ts1", project="LU")
        assert result["ticket"] == "LU-99999"
        assert "LU-99999" in result["url"]

    def test_uses_default_description(self, client):
        """When no description provided, should use form default."""
        form_html = (
            '<textarea name="description">Default desc &amp; stuff</textarea>'
            '<textarea name="summary">sanity: </textarea>'
        )
        result_html = '<div class="flash success">Created LU-100</div>'
        web = self._setup_raise_bug(client, form_html, result_html)

        client.raise_bug(buggable_id="ts1")
        # Second GET should include the extracted description
        second_call = web.get.call_args_list[1]
        params = second_call[1]["params"]
        assert "Default desc & stuff" in params["description"]

    def test_custom_description_overrides(self, client):
        form_html = '<textarea name="description">Default</textarea>'
        result_html = '<div class="flash success">Created LU-101</div>'
        web = self._setup_raise_bug(client, form_html, result_html)

        client.raise_bug(buggable_id="ts1", description="My custom desc")
        second_call = web.get.call_args_list[1]
        params = second_call[1]["params"]
        assert params["description"] == "My custom desc"

    def test_error_flash_raises(self, client):
        form_html = '<textarea name="description">x</textarea>'
        result_html = '<div class="flash error">JIRA connection failed</div>'
        self._setup_raise_bug(client, form_html, result_html)

        with pytest.raises(RuntimeError, match="JIRA connection failed"):
            client.raise_bug(buggable_id="ts1")

    def test_no_description_textarea(self, client):
        """When form lacks description textarea, use empty string."""
        form_html = '<html>no textarea</html>'
        result_html = '<div class="flash success">Created LU-102</div>'
        web = self._setup_raise_bug(client, form_html, result_html)

        client.raise_bug(buggable_id="ts1")
        params = web.get.call_args_list[1][1]["params"]
        assert params["description"] == ""


# ---------------------------------------------------------------------------
# get_test_history
# ---------------------------------------------------------------------------

class TestGetTestHistory:
    def test_basic_history(self, client):
        """Should find matching subtests across sessions."""
        sessions = [
            {"id": "s1", "submission": "2026-01-10T00:00:00Z", "test_host": "h1", "test_name": "test1"},
        ]
        test_sets = [{"id": "ts1", "test_set_script_id": "sc1"}]
        subtests = [
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "err", "duration": 30},
            {"sub_test_script_id": "st2", "status": "PASS", "error": "", "duration": 10},
        ]

        script_map = {
            "st1": {"id": "st1", "name": "test_39b"},
            "st2": {"id": "st2", "name": "test_40a"},
        }

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(return_value=test_sets)
        client.get_test_set_script = MagicMock(return_value={"id": "sc1", "name": "sanity"})
        client.get_subtests = MagicMock(return_value=subtests)
        client.get_sub_test_script = MagicMock(
            side_effect=lambda sid: script_map[sid]
        )

        history, resolved = client.get_test_history(
            test_name="test_39b",
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert len(history) == 1
        assert history[0]["status"] == "FAIL"
        assert history[0]["suite"] == "sanity"
        assert resolved == "sanity"

    def test_suite_filter(self, client):
        """Suite filter should narrow which test sets are examined."""
        sessions = [{"id": "s1", "submission": "2026-01-10", "test_host": "h1", "test_name": "t"}]
        test_sets = [
            {"id": "ts1", "test_set_script_id": "sc1"},
            {"id": "ts2", "test_set_script_id": "sc2"},
        ]

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(return_value=test_sets)
        # find_test_set_script_id resolves suite name to script ID
        client.find_test_set_script_id = MagicMock(return_value="sc1")
        client.get_test_set_script = MagicMock(return_value={"id": "sc1", "name": "sanity"})
        client.get_subtests = MagicMock(return_value=[
            {"sub_test_script_id": "st1", "status": "PASS", "error": "", "duration": 5},
        ])
        client.get_sub_test_script = MagicMock(return_value={"id": "st1", "name": "test_1a"})

        history, _ = client.get_test_history(
            test_name="test_1a",
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
            suite="sanity",
        )
        # Only ts1 (sc1=sanity) should have subtests fetched
        client.get_subtests.assert_called_once_with(test_set_id="ts1")

    def test_empty_sessions(self, client):
        """No sessions means empty history."""
        client.get_sessions = MagicMock(return_value=[])
        history, resolved = client.get_test_history(
            test_name="test_1a",
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert history == []
        assert resolved is None

    def test_history_sorted_by_submission(self, client):
        """Results should be sorted by submission date."""
        sessions = [
            {"id": "s1", "submission": "2026-01-15", "test_host": "h1", "test_name": "t"},
            {"id": "s2", "submission": "2026-01-10", "test_host": "h1", "test_name": "t"},
        ]
        # Each session gets its own test_sets call
        ts_s1 = [{"id": "ts1a", "test_set_script_id": "sc1"}]
        ts_s2 = [{"id": "ts2a", "test_set_script_id": "sc1"}]

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(side_effect=[ts_s1, ts_s2])
        client.get_test_set_script = MagicMock(return_value={"id": "sc1", "name": "sanity"})
        client.get_subtests = MagicMock(return_value=[
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "", "duration": 5},
        ])
        client.get_sub_test_script = MagicMock(return_value={"id": "st1", "name": "test_1a"})

        history, _ = client.get_test_history(
            test_name="test_1a",
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert len(history) == 2
        assert history[0]["submission"] == "2026-01-10"
        assert history[1]["submission"] == "2026-01-15"


# ---------------------------------------------------------------------------
# get_top_failures
# ---------------------------------------------------------------------------

class TestGetTopFailures:
    def test_aggregates_failures(self, client):
        """Should aggregate failures by (suite, test_name)."""
        sessions = [
            {"id": "s1"},
            {"id": "s2"},
        ]
        # Each session has one failed test set
        ts1 = [{"id": "ts1", "test_set_script_id": "sc1", "status": "FAIL"}]
        ts2 = [{"id": "ts2", "test_set_script_id": "sc1", "status": "FAIL"}]

        subtests1 = [
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "err1", "order": 1},
        ]
        subtests2 = [
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "err2", "order": 1},
        ]

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(side_effect=[ts1, ts2])
        client.resolve_test_set_names = MagicMock(return_value={"sc1": "sanity"})
        client.get_subtests = MagicMock(side_effect=[subtests1, subtests2])
        client.get_sub_test_script = MagicMock(return_value={"id": "st1", "name": "test_39b"})

        failures, examined, total = client.get_top_failures(
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert examined == 2
        assert total == 2
        assert len(failures) == 1
        assert failures[0]["test_name"] == "test_39b"
        assert failures[0]["suite"] == "sanity"
        assert failures[0]["count"] == 2
        assert failures[0]["session_count"] == 2

    def test_empty_sessions(self, client):
        client.get_sessions = MagicMock(return_value=[])
        failures, examined, total = client.get_top_failures(
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert failures == []
        assert examined == 0
        assert total == 0

    def test_sorted_by_count_descending(self, client):
        """Failures should be sorted by count descending."""
        sessions = [{"id": "s1"}]
        test_sets = [
            {"id": "ts1", "test_set_script_id": "sc1", "status": "FAIL"},
        ]
        subtests = [
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "", "order": 1},
            {"sub_test_script_id": "st1", "status": "FAIL", "error": "", "order": 2},
            {"sub_test_script_id": "st2", "status": "FAIL", "error": "", "order": 3},
        ]

        script_map = {
            "st1": {"id": "st1", "name": "test_39b"},
            "st2": {"id": "st2", "name": "test_1a"},
        }

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(return_value=test_sets)
        client.resolve_test_set_names = MagicMock(return_value={"sc1": "sanity"})
        client.get_subtests = MagicMock(return_value=subtests)
        client.get_sub_test_script = MagicMock(
            side_effect=lambda sid: script_map[sid]
        )

        failures, _, _ = client.get_top_failures(
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert failures[0]["test_name"] == "test_39b"
        assert failures[0]["count"] == 2
        assert failures[1]["test_name"] == "test_1a"
        assert failures[1]["count"] == 1

    def test_skips_passing_subtests(self, client):
        """Only FAIL/CRASH/ABORT/TIMEOUT subtests should be aggregated."""
        sessions = [{"id": "s1"}]
        test_sets = [{"id": "ts1", "test_set_script_id": "sc1", "status": "FAIL"}]
        subtests = [
            {"sub_test_script_id": "st1", "status": "PASS", "error": "", "order": 1},
            {"sub_test_script_id": "st2", "status": "SKIP", "error": "", "order": 2},
            {"sub_test_script_id": "st3", "status": "FAIL", "error": "err", "order": 3},
        ]

        script_map = {
            "st1": {"id": "st1", "name": "test_1a"},
            "st2": {"id": "st2", "name": "test_1b"},
            "st3": {"id": "st3", "name": "test_1c"},
        }

        client.get_sessions = MagicMock(return_value=sessions)
        client.get_test_sets = MagicMock(return_value=test_sets)
        client.resolve_test_set_names = MagicMock(return_value={"sc1": "sanity"})
        client.get_subtests = MagicMock(return_value=subtests)
        client.get_sub_test_script = MagicMock(
            side_effect=lambda sid: script_map[sid]
        )

        failures, _, _ = client.get_top_failures(
            trigger_job="lustre-master",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        assert len(failures) == 1
        assert failures[0]["test_name"] == "test_1c"
