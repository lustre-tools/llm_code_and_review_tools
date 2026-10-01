"""JIRA REST API client."""

import random
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import requests
from urllib3.exceptions import ConnectTimeoutError

from .adf import MentionResolver, adf_to_markdown, markdown_to_adf
from .config import JiraConfig
from .errors import (
    AuthError,
    ErrorCode,
    InvalidInputError,
    JiraToolError,
    NetworkError,
    NotFoundError,
)


def _text_to_adf(text: str, resolve_mention: MentionResolver | None = None) -> dict[str, Any]:
    """Convert comment or description text to Atlassian Document Format (ADF).

    Cloud v3 API requires ADF for description and comment body fields
    instead of plain text strings.  Markdown markup is converted; see
    jira_tool.adf for what is supported.
    """
    return markdown_to_adf(text, resolve_mention)


def _adf_to_text(adf: Any) -> Any:
    """Convert Atlassian Document Format (ADF) to Markdown text.

    Cloud v3 API returns ADF dicts for description and comment body
    fields.  Returns the input unchanged if it's already a string or None.
    """
    return adf_to_markdown(adf)


def _retry_after_seconds(value: str | None) -> float | None:
    """The wait a Retry-After header asks for, or None if it is unusable."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 1.0  # Base delay in seconds
DEFAULT_RETRY_MAX_DELAY = 30.0  # Maximum delay between retries


class JiraClient:
    """
    JIRA REST API client.

    Provides low-level access to JIRA REST API endpoints with proper
    error handling and response normalization.
    """

    def __init__(
        self,
        config: JiraConfig,
        timeout: int = 30,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
        debug: bool = False,
    ):
        """
        Initialize JIRA client.

        Args:
            config: JIRA configuration with server URL and credentials
            timeout: Request timeout in seconds (default: 30)
            max_retries: Maximum number of retries for transient failures (default: 3)
            retry_backoff: Base delay for exponential backoff in seconds (default: 1.0)
            retry_max_delay: Maximum delay between retries in seconds (default: 30.0)
            debug: Enable debug output to stderr
        """
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.retry_max_delay = retry_max_delay
        self.debug = debug
        self._session = requests.Session()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Send no Authorization header when there is no token: an empty
        # one is rejected, where its absence is served anonymously.
        auth_header = config.get_auth_header()
        if auth_header:
            headers["Authorization"] = auth_header
        self._session.headers.update(headers)

    # Jira's search API is a POST that only reads, so the HTTP method
    # alone cannot decide whether an operation writes.
    READ_ONLY_POSTS = frozenset({"search", "search/jql"})

    def _require_auth(self, method: str, endpoint: str = "") -> None:
        """Refuse a write before making it when there is no token.

        Reads are served anonymously by a public Jira; a write without a
        token fails at the server with a 401 whose body does not say
        that a token is the fix.  _request, _raw_request_with_retry and
        upload_attachment each call it before sending.
        """
        if method.upper() in ("GET", "HEAD", "OPTIONS"):
            return
        if (
            method.upper() == "POST"
            and endpoint.strip("/") in self.READ_ONLY_POSTS
        ):
            return
        if self.config.authenticated:
            return
        raise AuthError(
            f"A Jira token is required to {method.upper()} "
            f"({self.config.server}). Reading works without one. "
            "Set JIRA_TOKEN, or run `install.sh --configure --only jira`."
        )

    def _debug(self, msg: str) -> None:
        """Print debug message to stderr if debug mode is enabled."""
        if self.debug:
            print(f"[DEBUG] {msg}", file=sys.stderr)

    def _debug_request(self, method: str, url: str,
                       params: dict | None = None,
                       json_data: dict | None = None) -> None:
        """Log request details to stderr."""
        if not self.debug:
            return
        self._debug(f"{method} {url}")
        if params:
            self._debug(f"  params: {params}")
        if json_data:
            self._debug(f"  body: {json_data}")

    def _debug_response(self, response: requests.Response) -> None:
        """Log response details to stderr."""
        if not self.debug:
            return
        self._debug(f"  -> {response.status_code} "
                     f"({len(response.content)} bytes)")

    def _build_url(self, endpoint: str) -> str:
        """Build full URL for API endpoint."""
        api_version = "3" if self.config.is_cloud else "2"
        base = f"{self.config.server}/rest/api/{api_version}/"
        return urljoin(base, endpoint.lstrip("/"))

    def _calculate_retry_delay(
        self, attempt: int, response: requests.Response | None = None
    ) -> float:
        """
        Calculate delay before next retry using exponential backoff with jitter.

        A 429 or 503 with a usable Retry-After waits that long instead.
        Both are capped at retry_max_delay.

        Args:
            attempt: Current attempt number (0-indexed)
            response: The failed attempt's response, if it got one

        Returns:
            Delay in seconds before next retry
        """
        if response is not None and response.status_code in (429, 503):
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after is not None:
                return min(retry_after, self.retry_max_delay)

        # Exponential backoff: base * 2^attempt
        delay = self.retry_backoff * (2**attempt)
        # Add jitter (±25%) to prevent thundering herd
        jitter = delay * 0.25 * (2 * random.random() - 1)
        delay += jitter
        # Cap at max delay
        return min(delay, self.retry_max_delay)

    def _is_retryable_error(self, error: Exception) -> bool:
        """
        Check if an error is retryable (transient).

        Args:
            error: The exception to check

        Returns:
            True if the error is transient and should be retried
        """
        # Network errors are always retryable
        if isinstance(error, NetworkError):
            return True

        # JiraToolError with certain codes are retryable
        if isinstance(error, JiraToolError):
            # Rate limiting
            if error.code == ErrorCode.RATE_LIMITED:
                return True
            # Server errors (5xx)
            if error.code == ErrorCode.SERVER_ERROR and error.http_status is not None:
                return error.http_status >= 500

        return False

    def _may_resend(self, error: Exception, method: str, endpoint: str = "") -> bool:
        """Whether a request that failed with *error* may be sent again.

        A POST that creates something may already have been applied when
        the reply timed out, the connection dropped after sending, or the
        server answered 5xx, so it is resent only when the server cannot
        have acted on it: a 429, or a failure to connect.
        """
        if method.upper() != "POST" or endpoint.strip("/") in self.READ_ONLY_POSTS:
            return True
        if isinstance(error, JiraToolError) and error.code == ErrorCode.RATE_LIMITED:
            return True
        cause = error.__cause__
        if isinstance(cause, requests.exceptions.ConnectTimeout):
            return True
        # requests wraps a failure to connect in MaxRetryError; a failure
        # after sending is a bare ProtocolError or ReadTimeoutError.
        if isinstance(cause, requests.exceptions.ConnectionError) and cause.args:
            reason = getattr(cause.args[0], "reason", None)
            return isinstance(reason, ConnectTimeoutError)
        return False

    @staticmethod
    def _not_found_code(endpoint: str) -> str:
        """The error code for a 404 from *endpoint*.

        Below issue/<KEY>/<collection>, as in issue/<KEY>/comment/<ID>, the
        404 may be for the item rather than the issue.
        """
        parts = endpoint.strip("/").split("/")
        if parts[0] == "issue" and len(parts) in (2, 3):
            return ErrorCode.ISSUE_NOT_FOUND
        if parts[0] == "project" and len(parts) >= 2:
            return ErrorCode.PROJECT_NOT_FOUND
        return ErrorCode.NOT_FOUND

    def _handle_response(
        self,
        response: requests.Response,
        context: str = "",
        not_found_code: str = ErrorCode.NOT_FOUND,
    ) -> Any:
        """
        Handle API response, raising appropriate errors for non-success statuses.

        Args:
            response: The requests Response object
            context: Optional context string for error messages
            not_found_code: Error code to report for a 404

        Returns:
            Parsed JSON response data

        Raises:
            AuthError: For 401/403 responses
            NotFoundError: For 404 responses
            InvalidInputError: For 400 responses
            JiraToolError: For other error responses
        """
        # Try to parse JSON body for error details
        try:
            body = response.json() if response.text else {}
        except ValueError:
            body = {"raw": response.text[:500] if response.text else ""}

        # Extract JIRA error messages if present
        jira_errors = []
        if isinstance(body, dict):
            if "errorMessages" in body:
                jira_errors.extend(body["errorMessages"])
            if "errors" in body and isinstance(body["errors"], dict):
                for field, msg in body["errors"].items():
                    jira_errors.append(f"{field}: {msg}")

        error_detail = "; ".join(jira_errors) if jira_errors else ""
        # Fallback: include raw body when no structured errors extracted
        if not error_detail and isinstance(body, dict) and body:
            raw = str(body)
            if len(raw) > 500:
                raw = raw[:500] + "..."
            error_detail = raw

        if response.status_code == 401:
            raise AuthError(
                message=f"Authentication failed{': ' + error_detail if error_detail else ''}",
                http_status=401,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        if response.status_code == 403:
            raise AuthError(
                message=f"Permission denied{': ' + error_detail if error_detail else ''}",
                http_status=403,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        if response.status_code == 404:
            raise NotFoundError(
                code=not_found_code,
                message=f"Resource not found{': ' + context if context else ''}{': ' + error_detail if error_detail else ''}",
                http_status=404,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        if response.status_code == 400:
            raise InvalidInputError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid request{': ' + error_detail if error_detail else ''}",
                http_status=400,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        if response.status_code == 429:
            raise JiraToolError(
                code=ErrorCode.RATE_LIMITED,
                message="Rate limited by JIRA server",
                http_status=429,
            )

        if response.status_code >= 500:
            raise JiraToolError(
                code=ErrorCode.SERVER_ERROR,
                message=f"JIRA server error{': ' + error_detail if error_detail else ''}",
                http_status=response.status_code,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        if not response.ok:
            raise JiraToolError(
                code=ErrorCode.SERVER_ERROR,
                message=f"Unexpected error (HTTP {response.status_code}){': ' + error_detail if error_detail else ''}",
                http_status=response.status_code,
                details={"jira_errors": jira_errors} if jira_errors else None,
            )

        # Success - return parsed JSON or empty dict
        if response.status_code == 204:
            return {}
        return body

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        context: str = "",
    ) -> Any:
        """
        Make an API request with error handling and automatic retry.

        Automatically retries on transient failures (5xx, 429 rate limit,
        timeouts, connection errors) with exponential backoff; see
        _may_resend for the POSTs that are not retried.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON body for POST/PUT requests
            context: Context string for error messages

        Returns:
            Parsed JSON response

        Raises:
            NetworkError: For connection/timeout issues (after retries exhausted)
            Various JiraToolError subclasses for API errors
        """
        self._require_auth(method, endpoint)
        url = self._build_url(endpoint)
        last_error: Exception | None = None
        self._debug_request(method, url, params, json_data)

        for attempt in range(self.max_retries + 1):
            response: requests.Response | None = None
            try:
                response = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=self.timeout,
                )
                self._debug_response(response)
                return self._handle_response(
                    response, context, self._not_found_code(endpoint)
                )

            except requests.exceptions.Timeout as e:
                last_error = NetworkError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Request timed out after {self.timeout}s",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.ConnectionError as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Connection failed: {str(e)}",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.RequestException as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Request failed: {str(e)}",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except (AuthError, NotFoundError, InvalidInputError):
                # Non-retryable errors - raise immediately
                raise
            except JiraToolError as e:
                last_error = e
                if not self._is_retryable_error(e):
                    raise

            if last_error is not None and not self._may_resend(last_error, method, endpoint):
                raise last_error

            # If we have retries left, wait and try again
            if attempt < self.max_retries:
                delay = self._calculate_retry_delay(attempt, response)
                time.sleep(delay)

        # All retries exhausted, raise the last error
        if last_error is not None:
            raise last_error
        # Should never reach here, but satisfy type checker
        raise RuntimeError("Unexpected state in retry loop")

    def _raw_request_with_retry(
        self,
        method: str,
        url: str,
        context: str = "",
        **kwargs: Any,
    ) -> requests.Response:
        """
        Make a raw session request with retry logic.

        Used for special cases like attachment downloads that need direct
        session access but still want retry behavior.

        Args:
            method: HTTP method
            url: Full URL to request
            context: Context for error messages
            **kwargs: Additional arguments to pass to session.request

        Returns:
            Response object

        Raises:
            NetworkError: For connection/timeout issues
            JiraToolError: For HTTP errors
        """
        self._require_auth(method)
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        last_error: Exception | None = None
        self._debug(f"{method} {url} (raw)")

        for attempt in range(self.max_retries + 1):
            response: requests.Response | None = None
            try:
                response = self._session.request(method, url, **kwargs)
                self._debug_response(response)

                # Check for retryable HTTP errors
                if response.status_code == 429:
                    last_error = JiraToolError(
                        code=ErrorCode.RATE_LIMITED,
                        message="Rate limited by JIRA server",
                        http_status=429,
                    )
                elif response.status_code >= 500:
                    last_error = JiraToolError(
                        code=ErrorCode.SERVER_ERROR,
                        message=f"Server error: HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                else:
                    # Success or non-retryable error
                    return response

            except requests.exceptions.Timeout as e:
                last_error = NetworkError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Request timed out after {kwargs.get('timeout', self.timeout)}s",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.ConnectionError as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Connection failed: {str(e)}",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.RequestException as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Request failed: {str(e)}",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e

            if last_error is not None and not self._may_resend(last_error, method):
                raise last_error

            # If we have retries left, wait and try again
            if attempt < self.max_retries:
                delay = self._calculate_retry_delay(attempt, response)
                time.sleep(delay)

        # All retries exhausted
        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected state in retry loop")

    # =========================================================================
    # Issue Operations
    # =========================================================================

    def get_issue(
        self,
        key: str,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Get issue by key.

        Args:
            key: Issue key (e.g., "PROJ-123")
            fields: Optional list of fields to return (default: all)
            expand: Optional list of expansions (e.g., ["changelog"])

        Returns:
            Issue data dictionary
        """
        params: dict[str, str] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = ",".join(expand)

        return self._request("GET", f"issue/{key}", params=params, context=key)

    def search_issues(
        self,
        jql: str,
        fields: list[str] | None = None,
        start_at: int = 0,
        max_results: int = 50,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for issues using JQL.

        Args:
            jql: JQL query string
            fields: Optional list of fields to return
            start_at: Starting index for pagination (v2 only)
            max_results: Maximum results to return (default: 50, max: 1000)
            next_page_token: Pagination token (Cloud v3 only)

        Returns:
            Search results with issues and pagination info
        """
        if self.config.is_cloud:
            # Cloud v3 search/jql returns only issue IDs by default;
            # request standard fields so callers get usable results.
            _CLOUD_DEFAULT_FIELDS = [
                "summary", "status", "priority", "issuetype", "project",
                "assignee", "reporter", "resolution", "created", "updated",
                "labels", "description", "components", "fixVersions",
            ]
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": min(max_results, 1000),
                "fields": fields if fields else _CLOUD_DEFAULT_FIELDS,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            result = self._request("POST", "search/jql", json_data=body,
                                   context=f"JQL: {jql[:50]}")
            # Normalize Cloud v3 response to match v2 shape for callers
            result.setdefault("startAt", 0)
            result.setdefault("maxResults", max_results)
            result.setdefault("total", len(result.get("issues", [])))
            return result
        else:
            body = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": min(max_results, 1000),
            }
            if fields:
                body["fields"] = fields
            return self._request("POST", "search", json_data=body,
                                 context=f"JQL: {jql[:50]}")

    # =========================================================================
    # Comment Operations
    # =========================================================================

    def get_comments(
        self,
        key: str,
        start_at: int = 0,
        max_results: int = 50,
        order_by: str = "created",
    ) -> dict[str, Any]:
        """
        Get comments for an issue.

        Args:
            key: Issue key
            start_at: Starting index for pagination
            max_results: Maximum comments to return
            order_by: Sort order (default: "created" for oldest first, use "-created" for newest first)

        Returns:
            Comments data with pagination info
        """
        params = {
            "startAt": str(start_at),
            "maxResults": str(max_results),
            "orderBy": order_by,
        }
        return self._request("GET", f"issue/{key}/comment", params=params, context=key)

    def get_all_comments(
        self,
        key: str,
        order_by: str = "created",
        page_size: int = 100,
    ) -> dict[str, Any]:
        """
        Get ALL comments for an issue, paginating through the full set.

        Args:
            key: Issue key
            order_by: Sort order (default: "created" for oldest first, use "-created" for newest first)
            page_size: Number of comments per API request (max 100 for Cloud)

        Returns:
            Comments data with all comments collected
        """
        all_comments: list[dict[str, Any]] = []
        start_at = 0
        total = None

        while True:
            page = self.get_comments(key, start_at=start_at, max_results=page_size, order_by=order_by)
            comments = page.get("comments", [])
            all_comments.extend(comments)

            if total is None:
                total = page.get("total", len(comments))

            start_at += len(comments)
            if not comments or start_at >= total:
                break

        return {
            "maxResults": len(all_comments),
            "total": total if total is not None else len(all_comments),
            "startAt": 0,
            "comments": all_comments,
        }

    def get_comment(self, key: str, comment_id: str) -> dict[str, Any]:
        """Get one comment of an issue."""
        return self._request(
            "GET",
            f"issue/{key}/comment/{comment_id}",
            context=f"{key}/comment/{comment_id}",
        )

    def _resolve_mention(self, name: str) -> str:
        from .commands._helpers import resolve_cloud_user

        return resolve_cloud_user(self, name)

    def add_comment(
        self,
        key: str,
        body: str,
        visibility: dict[str, str] | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Add a comment to an issue.

        Args:
            key: Issue key
            body: Comment body text
            visibility: Optional visibility restriction dict with "type" ("role" or "group")
                        and "value" (role/group name), e.g. {"type": "role", "value": "Developers"}
            parent_id: Cloud only: the root comment of the thread to reply in

        Returns:
            Created comment data
        """
        comment_body = _text_to_adf(body, self._resolve_mention) if self.config.is_cloud else body
        json_data: dict[str, Any] = {"body": comment_body}
        if visibility:
            json_data["visibility"] = visibility
        if parent_id:
            json_data["parentId"] = int(parent_id)
        return self._request(
            "POST",
            f"issue/{key}/comment",
            json_data=json_data,
            context=key,
        )

    def edit_comment(
        self,
        key: str,
        comment_id: str,
        body: str,
        visibility: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Edit an existing comment on an issue.

        Args:
            key: Issue key
            comment_id: Comment ID to edit
            body: New comment body text
            visibility: Optional visibility restriction dict with "type" ("role" or "group")
                        and "value" (role/group name)

        Returns:
            Updated comment data
        """
        comment_body = _text_to_adf(body, self._resolve_mention) if self.config.is_cloud else body
        json_data: dict[str, Any] = {"body": comment_body}
        if visibility:
            json_data["visibility"] = visibility
        return self._request(
            "PUT",
            f"issue/{key}/comment/{comment_id}",
            json_data=json_data,
            context=f"{key}/comment/{comment_id}",
        )

    def delete_comment(self, key: str, comment_id: str) -> None:
        """
        Delete a comment from an issue.

        Args:
            key: Issue key
            comment_id: Comment ID to delete
        """
        self._request(
            "DELETE",
            f"issue/{key}/comment/{comment_id}",
            context=f"{key}/comment/{comment_id}",
        )

    # =========================================================================
    # Issue Link Operations
    # =========================================================================

    def create_link(self, inward_key: str, outward_key: str, link_type: str = "Related") -> None:
        """
        Create a link between two issues.

        Args:
            inward_key: Inward issue key (e.g., "is related to")
            outward_key: Outward issue key (e.g., "relates to")
            link_type: Link type name (e.g., Related, Blocks, Duplicate)
        """
        self._request(
            "POST",
            "issueLink",
            json_data={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
            context=f"link {inward_key} -> {outward_key}",
        )

    def delete_link(self, link_id: str) -> None:
        """
        Delete an issue link.

        Args:
            link_id: Link ID (from 'links' command output)
        """
        self._request(
            "DELETE",
            f"issueLink/{link_id}",
            context=f"issueLink/{link_id}",
        )

    def get_link_types(self) -> dict[str, Any]:
        """
        Get available issue link types.

        Returns:
            Dict with issueLinkTypes list
        """
        return self._request("GET", "issueLinkType")

    # =========================================================================
    # Label Operations
    # =========================================================================

    def add_labels(self, key: str, labels: list[str]) -> dict[str, Any]:
        """
        Add labels to an issue without replacing existing ones.

        Args:
            key: Issue key
            labels: Labels to add
        """
        body = {
            "update": {
                "labels": [{"add": label} for label in labels],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def remove_labels(self, key: str, labels: list[str]) -> dict[str, Any]:
        """
        Remove labels from an issue.

        Args:
            key: Issue key
            labels: Labels to remove
        """
        body = {
            "update": {
                "labels": [{"remove": label} for label in labels],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    # =========================================================================
    # Project Role Operations
    # =========================================================================

    def get_project_roles(self, project_key: str) -> dict[str, str]:
        """
        Get available roles for a project.

        Args:
            project_key: Project key (e.g., "LU")

        Returns:
            Dict mapping role name to role URL
        """
        return self._request("GET", f"project/{project_key}/role", context=project_key)

    # =========================================================================
    # Component and Version Operations
    # =========================================================================

    def get_project_components(self, project_key: str) -> list[dict[str, Any]]:
        """
        Get components for a project.

        Args:
            project_key: Project key (e.g., "LU")

        Returns:
            List of component dicts
        """
        return self._request("GET", f"project/{project_key}/components", context=project_key)

    def get_project_versions(self, project_key: str) -> list[dict[str, Any]]:
        """
        Get versions for a project.

        Args:
            project_key: Project key (e.g., "LU")

        Returns:
            List of version dicts
        """
        return self._request("GET", f"project/{project_key}/versions", context=project_key)

    def set_components(self, key: str, components: list[str]) -> dict[str, Any]:
        """
        Set components on an issue (replaces existing).

        Args:
            key: Issue key
            components: Component names to set
        """
        body = {
            "fields": {
                "components": [{"name": c} for c in components],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def add_components(self, key: str, components: list[str]) -> dict[str, Any]:
        """
        Add components to an issue without replacing existing ones.

        Args:
            key: Issue key
            components: Component names to add
        """
        body = {
            "update": {
                "components": [{"add": {"name": c}} for c in components],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def remove_components(self, key: str, components: list[str]) -> dict[str, Any]:
        """
        Remove components from an issue.

        Args:
            key: Issue key
            components: Component names to remove
        """
        body = {
            "update": {
                "components": [{"remove": {"name": c}} for c in components],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def set_fix_versions(self, key: str, versions: list[str]) -> dict[str, Any]:
        """
        Set fix versions on an issue (replaces existing).

        Args:
            key: Issue key
            versions: Version names to set
        """
        body = {
            "fields": {
                "fixVersions": [{"name": v} for v in versions],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def add_fix_versions(self, key: str, versions: list[str]) -> dict[str, Any]:
        """
        Add fix versions to an issue without replacing existing ones.

        Args:
            key: Issue key
            versions: Version names to add
        """
        body = {
            "update": {
                "fixVersions": [{"add": {"name": v}} for v in versions],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    def remove_fix_versions(self, key: str, versions: list[str]) -> dict[str, Any]:
        """
        Remove fix versions from an issue.

        Args:
            key: Issue key
            versions: Version names to remove
        """
        body = {
            "update": {
                "fixVersions": [{"remove": {"name": v}} for v in versions],
            }
        }
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    # =========================================================================
    # Worklog Operations
    # =========================================================================

    def get_worklogs(
        self,
        key: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """
        Get worklogs for an issue.

        Args:
            key: Issue key
            start_at: Starting index for pagination
            max_results: Maximum worklogs to return

        Returns:
            Worklogs data with pagination info
        """
        params = {
            "startAt": str(start_at),
            "maxResults": str(max_results),
        }
        return self._request("GET", f"issue/{key}/worklog", params=params, context=key)

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        comment: str | None = None,
        started: str | None = None,
    ) -> dict[str, Any]:
        """
        Add a worklog entry to an issue.

        Args:
            key: Issue key
            time_spent: Time spent in JIRA duration format (e.g., "2h 30m", "1d")
            comment: Optional comment for the worklog
            started: Optional start time in ISO format (defaults to now)

        Returns:
            Created worklog data
        """
        body: dict[str, Any] = {"timeSpent": time_spent}
        if comment:
            body["comment"] = (
                _text_to_adf(comment) if self.config.is_cloud else comment
            )
        if started:
            body["started"] = started

        return self._request(
            "POST",
            f"issue/{key}/worklog",
            json_data=body,
            context=key,
        )

    # =========================================================================
    # Transition Operations
    # =========================================================================

    def get_transitions(self, key: str) -> dict[str, Any]:
        """
        Get available transitions for an issue.

        Args:
            key: Issue key

        Returns:
            Available transitions data
        """
        return self._request("GET", f"issue/{key}/transitions", context=key)

    def do_transition(
        self,
        key: str,
        transition_id: str,
        comment: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Perform a transition on an issue.

        Args:
            key: Issue key
            transition_id: ID of the transition to perform
            comment: Optional comment to add with transition
            fields: Optional fields to update during transition

        Returns:
            Empty dict on success (JIRA returns 204)
        """
        body: dict[str, Any] = {
            "transition": {"id": transition_id},
        }

        if comment:
            comment_body = _text_to_adf(comment, self._resolve_mention) if self.config.is_cloud else comment
            body["update"] = {"comment": [{"add": {"body": comment_body}}]}

        if fields:
            body["fields"] = fields

        return self._request(
            "POST",
            f"issue/{key}/transitions",
            json_data=body,
            context=f"{key} -> transition {transition_id}",
        )

    # =========================================================================
    # Create Operations
    # =========================================================================

    def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create a new issue.

        Args:
            project_key: Project key (e.g., "PROJ")
            issue_type: Issue type name (e.g., "Bug", "Task")
            summary: Issue summary
            description: Optional description
            fields: Optional additional fields

        Returns:
            Created issue data with key and id
        """
        body: dict[str, Any] = {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type},
                "summary": summary,
                **(fields or {}),
            }
        }

        if description:
            body["fields"]["description"] = (
                _text_to_adf(description, self._resolve_mention) if self.config.is_cloud else description
            )

        return self._request("POST", "issue", json_data=body)

    def update_issue(
        self,
        key: str,
        summary: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing issue.

        Args:
            key: Issue key (e.g., "PROJ-123")
            summary: New summary (optional)
            description: New description (optional)
            assignee: New assignee username (optional, use "" to unassign)
            priority: New priority name (optional)
            labels: New labels list (optional, replaces existing)
            fields: Additional fields to update (optional)

        Returns:
            Empty dict on success (JIRA returns 204)
        """
        update_fields: dict[str, Any] = {}

        if summary is not None:
            update_fields["summary"] = summary
        if description is not None:
            update_fields["description"] = (
                _text_to_adf(description, self._resolve_mention) if self.config.is_cloud else description
            )
        if assignee is not None:
            if self.config.is_cloud:
                # Cloud uses accountId, not name
                update_fields["assignee"] = {"accountId": assignee} if assignee else None
            else:
                update_fields["assignee"] = {"name": assignee} if assignee else None
        if priority is not None:
            update_fields["priority"] = {"name": priority}
        if labels is not None:
            update_fields["labels"] = labels
        if fields:
            update_fields.update(fields)

        if not update_fields:
            return {}  # Nothing to update

        body = {"fields": update_fields}
        return self._request("PUT", f"issue/{key}", json_data=body, context=key)

    # =========================================================================
    # Server Info (for connectivity tests)
    # =========================================================================

    def get_server_info(self) -> dict[str, Any]:
        """
        Get JIRA server information.

        Useful for testing connectivity and authentication.

        Returns:
            Server info including version, base URL, etc.
        """
        return self._request("GET", "serverInfo")

    # =========================================================================
    # Attachment Operations
    # =========================================================================

    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        """
        Get attachment metadata by ID.

        Args:
            attachment_id: The attachment ID

        Returns:
            Attachment metadata (filename, size, mimeType, content URL, etc.)
        """
        return self._request("GET", f"attachment/{attachment_id}", context=f"attachment {attachment_id}")

    def get_attachment_content(
        self,
        attachment_id: str,
        max_size: int = 1024 * 1024,  # 1MB default limit
    ) -> tuple[bytes, dict[str, Any]]:
        """
        Download attachment content.

        Args:
            attachment_id: The attachment ID
            max_size: Maximum size in bytes to download (default: 1MB).
                      Set to 0 for no limit (use with caution).

        Returns:
            Tuple of (content_bytes, metadata_dict)

        Raises:
            InvalidInputError: If attachment exceeds max_size
        """
        # First get metadata to check size
        metadata = self.get_attachment(attachment_id)
        size = metadata.get("size", 0)

        if max_size > 0 and size > max_size:
            raise InvalidInputError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Attachment too large: {size} bytes exceeds limit of {max_size} bytes",
                details={"size": size, "max_size": max_size, "filename": metadata.get("filename")},
            )

        # Download content from the content URL
        content_url = metadata.get("content")
        if not content_url:
            raise JiraToolError(
                code=ErrorCode.SERVER_ERROR,
                message="Attachment metadata missing content URL",
            )

        response = self._raw_request_with_retry(
            "GET",
            content_url,
            context=f"download attachment {attachment_id}",
        )

        if not response.ok:
            raise JiraToolError(
                code=ErrorCode.SERVER_ERROR,
                message=f"Failed to download attachment: HTTP {response.status_code}",
                http_status=response.status_code,
            )
        return response.content, metadata

    def delete_attachment(self, attachment_id: str) -> None:
        """
        Delete an attachment.

        Args:
            attachment_id: The attachment ID to delete
        """
        self._request(
            "DELETE",
            f"attachment/{attachment_id}",
            context=f"attachment {attachment_id}",
        )

    # =========================================================================
    # User Operations
    # =========================================================================

    def search_users(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Search for users by username, display name, or email.

        Args:
            query: Search string (matches username, display name, email)
            max_results: Maximum results to return (default: 10)

        Returns:
            List of user dicts
        """
        # Cloud GDPR strict mode rejects 'username'; use 'query' instead
        query_param = "query" if self.config.is_cloud else "username"
        params = {
            query_param: query,
            "maxResults": str(max_results),
        }
        return self._request("GET", "user/search", params=params, context=f"user search: {query}")

    def get_user(self, username: str) -> dict[str, Any]:
        """
        Look up one Server user by exact username.

        Works where user search returns nothing because the account lacks
        the Browse Users permission.

        Raises:
            NotFoundError: USER_NOT_FOUND if there is no such user
        """
        try:
            return self._request(
                "GET", "user", params={"username": username}, context=f"user {username}"
            )
        except NotFoundError:
            raise NotFoundError(
                code=ErrorCode.USER_NOT_FOUND,
                message=f"No JIRA user with username '{username}'.",
            ) from None

    # =========================================================================
    # Issue Type Operations
    # =========================================================================

    def get_issue_types(self, project_key: str | None = None) -> list[dict[str, Any]]:
        """
        Get available issue types.

        Args:
            project_key: Optional project key to get project-specific types.
                         If None, returns all issue types on the server.

        Returns:
            List of issue type dicts
        """
        if project_key:
            result = self._request(
                "GET",
                f"project/{project_key}",
                params={"expand": "issueTypes"},
                context=project_key,
            )
            return result.get("issueTypes", [])
        return self._request("GET", "issuetype")

    # =========================================================================
    # Issue Delete Operations
    # =========================================================================

    def delete_issue(self, key: str, delete_subtasks: bool = False) -> None:
        """
        Delete an issue.

        Args:
            key: Issue key
            delete_subtasks: If True, also delete subtasks
        """
        params = {}
        if delete_subtasks:
            params["deleteSubtasks"] = "true"
        self._request(
            "DELETE",
            f"issue/{key}",
            params=params if params else None,
            context=key,
        )

    # =========================================================================
    # Watcher Operations
    # =========================================================================

    def get_watchers(self, key: str) -> dict[str, Any]:
        """
        Get watchers for an issue.

        Args:
            key: Issue key (e.g., "PROJ-123")

        Returns:
            Watchers data including count and list of watchers
        """
        return self._request("GET", f"issue/{key}/watchers", context=key)

    def add_watcher(self, key: str, username: str) -> dict[str, Any]:
        """
        Add a watcher to an issue.

        Args:
            key: Issue key (e.g., "PROJ-123")
            username: Username or accountId to add as watcher

        Returns:
            Empty dict on success (JIRA returns 204)
        """
        import json

        # JIRA expects the username/accountId as a raw JSON string, not an object
        endpoint = f"issue/{key}/watchers"
        url = self._build_url(endpoint)
        response = self._raw_request_with_retry(
            "POST",
            url,
            data=json.dumps(username),
            context=f"add watcher {username} to {key}",
        )
        return self._handle_response(
            response,
            f"add watcher {username} to {key}",
            self._not_found_code(endpoint),
        )

    def remove_watcher(self, key: str, username: str) -> dict[str, Any]:
        """
        Remove a watcher from an issue.

        Args:
            key: Issue key (e.g., "PROJ-123")
            username: Username or accountId to remove as watcher

        Returns:
            Empty dict on success (JIRA returns 204)
        """
        # Cloud GDPR mode uses accountId param instead of username
        param_name = "accountId" if self.config.is_cloud else "username"
        return self._request(
            "DELETE",
            f"issue/{key}/watchers",
            params={param_name: username},
            context=f"remove watcher {username} from {key}",
        )

    def upload_attachment(
        self,
        key: str,
        file_path: str,
        filename: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Upload an attachment to an issue.

        Args:
            key: Issue key (e.g., "PROJ-123")
            file_path: Path to the file to upload
            filename: Optional filename (defaults to basename of file_path)

        Returns:
            List of created attachment data (JIRA returns a list)

        Raises:
            InvalidInputError: If file doesn't exist or can't be read
            NetworkError: For upload failures
        """
        import os

        if not os.path.exists(file_path):
            raise InvalidInputError(
                code=ErrorCode.INVALID_INPUT,
                message=f"File not found: {file_path}",
            )

        if filename is None:
            filename = os.path.basename(file_path)

        endpoint = f"issue/{key}/attachments"
        self._require_auth("POST", endpoint)
        url = self._build_url(endpoint)
        headers = {"X-Atlassian-Token": "no-check"}
        last_error: Exception | None = None
        self._debug(f"POST {url} (upload: {filename})")

        for attempt in range(self.max_retries + 1):
            response: requests.Response | None = None
            try:
                # Re-open file for each attempt (file handle is consumed after POST)
                with open(file_path, "rb") as f:
                    # Must remove Content-Type so requests can set
                    # the multipart/form-data boundary automatically.
                    # The session default is application/json which
                    # causes HTTP 415 on file uploads.
                    post_headers = {**headers}
                    post_headers["Content-Type"] = None
                    response = self._session.post(
                        url,
                        files={"file": (filename, f,
                                        "application/octet-stream")},
                        headers=post_headers,
                        timeout=self.timeout,
                    )
                    self._debug_response(response)

                # Check for retryable HTTP errors
                if response.status_code == 429:
                    last_error = JiraToolError(
                        code=ErrorCode.RATE_LIMITED,
                        message="Rate limited by JIRA server",
                        http_status=429,
                    )
                elif response.status_code >= 500:
                    last_error = JiraToolError(
                        code=ErrorCode.SERVER_ERROR,
                        message=f"Server error: HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                else:
                    return self._handle_response(
                        response,
                        f"upload to {key}",
                        self._not_found_code(endpoint),
                    )

            except requests.exceptions.Timeout as e:
                last_error = NetworkError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Attachment upload timed out after {self.timeout}s",
                    details={"file": file_path, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.RequestException as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Attachment upload failed: {str(e)}",
                    details={"file": file_path, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except OSError as e:
                # File read errors are not retryable
                raise InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Cannot read file: {e}",
                    details={"file": file_path},
                ) from e

            if last_error is not None and not self._may_resend(last_error, "POST", endpoint):
                raise last_error

            # If we have retries left, wait and try again
            if attempt < self.max_retries:
                delay = self._calculate_retry_delay(attempt, response)
                time.sleep(delay)

        # All retries exhausted
        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected state in retry loop")

    # ── Filter operations ──────────────────────────────────────────

    def get_filter(self, filter_id: str | int) -> dict[str, Any]:
        """Get a single saved filter by ID."""
        return self._request("GET", f"filter/{filter_id}", context=f"filter {filter_id}")

    def get_favourite_filters(self) -> list[dict[str, Any]]:
        """Get the current user's favourite (starred) filters."""
        return self._request("GET", "filter/favourite", context="favourite filters")

    def search_filters(
        self,
        filter_name: str | None = None,
        owner: str | None = None,
        max_results: int = 100,
        start_at: int = 0,
    ) -> dict[str, Any]:
        """Search for filters by name and/or owner.

        On JIRA Server, 'owner' is the username.
        On JIRA Cloud, use 'accountId' instead — pass it as owner.

        Args:
            filter_name: Optional filter name substring to match
            owner: Optional owner username (Server) or accountId (Cloud)
            max_results: Maximum results to return
            start_at: Pagination offset

        Returns:
            Dict with 'values' list and pagination info
        """
        params: dict[str, Any] = {
            "maxResults": max_results,
            "startAt": start_at,
            "expand": "description,owner,jql,sharePermissions",
        }
        if filter_name:
            params["filterName"] = filter_name
        if owner:
            params["accountId"] = owner
        return self._request("GET", "filter/search", params=params, context="filter search")

    def get_my_filters(self, max_results: int = 100) -> list[dict[str, Any]]:
        """Get all filters owned by the current user.

        Paginates automatically to collect all results.
        """
        all_filters: list[dict[str, Any]] = []
        start_at = 0

        while True:
            result = self.search_filters(max_results=max_results, start_at=start_at)
            values = result.get("values", [])
            all_filters.extend(values)

            total = result.get("total", len(all_filters))
            if len(all_filters) >= total or not values:
                break
            start_at += len(values)

        return all_filters

    def create_filter(
        self,
        name: str,
        jql: str,
        description: str = "",
        favourite: bool = False,
    ) -> dict[str, Any]:
        """Create a new saved filter.

        Args:
            name: Filter name
            jql: JQL query string
            description: Optional description
            favourite: Whether to mark as favourite

        Returns:
            Created filter data
        """
        data: dict[str, Any] = {
            "name": name,
            "jql": jql,
            "favourite": favourite,
        }
        if description:
            data["description"] = description
        return self._request("POST", "filter", json_data=data, context=f"create filter '{name}'")
