"""JIRA REST API client."""

from typing import Any
from urllib.parse import urljoin

import requests

from .config import JiraConfig
from .errors import (
    AuthError,
    ErrorCode,
    InvalidInputError,
    JiraToolError,
    NetworkError,
    NotFoundError,
)


class JiraClient:
    """
    JIRA REST API client.

    Provides low-level access to JIRA REST API endpoints with proper
    error handling and response normalization.
    """

    def __init__(self, config: JiraConfig, timeout: int = 30):
        """
        Initialize JIRA client.

        Args:
            config: JIRA configuration with server URL and credentials
            timeout: Request timeout in seconds (default: 30)
        """
        self.config = config
        self.timeout = timeout
        self._session = requests.Session()

        # Set up authentication header
        # JIRA uses Bearer token auth for API tokens
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _build_url(self, endpoint: str) -> str:
        """Build full URL for API endpoint."""
        base = f"{self.config.server}/rest/api/2/"
        return urljoin(base, endpoint.lstrip("/"))

    def _handle_response(self, response: requests.Response, context: str = "") -> Any:
        """
        Handle API response, raising appropriate errors for non-success statuses.

        Args:
            response: The requests Response object
            context: Optional context string for error messages

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
                code=ErrorCode.ISSUE_NOT_FOUND,
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
        Make an API request with error handling.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON body for POST/PUT requests
            context: Context string for error messages

        Returns:
            Parsed JSON response

        Raises:
            NetworkError: For connection/timeout issues
            Various JiraToolError subclasses for API errors
        """
        url = self._build_url(endpoint)

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                timeout=self.timeout,
            )
            return self._handle_response(response, context)

        except requests.exceptions.Timeout as e:
            raise NetworkError(
                code=ErrorCode.TIMEOUT,
                message=f"Request timed out after {self.timeout}s",
                details={"url": url},
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise NetworkError(
                code=ErrorCode.CONNECTION_ERROR,
                message=f"Connection failed: {str(e)}",
                details={"url": url},
            ) from e
        except requests.exceptions.RequestException as e:
            raise NetworkError(
                code=ErrorCode.CONNECTION_ERROR,
                message=f"Request failed: {str(e)}",
                details={"url": url},
            ) from e

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
    ) -> dict[str, Any]:
        """
        Search for issues using JQL.

        Args:
            jql: JQL query string
            fields: Optional list of fields to return
            start_at: Starting index for pagination
            max_results: Maximum results to return (default: 50, max: 1000)

        Returns:
            Search results with issues and pagination info
        """
        body: dict[str, Any] = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": min(max_results, 1000),
        }
        if fields:
            body["fields"] = fields

        return self._request("POST", "search", json_data=body, context=f"JQL: {jql[:50]}")

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

    def add_comment(self, key: str, body: str) -> dict[str, Any]:
        """
        Add a comment to an issue.

        Args:
            key: Issue key
            body: Comment body text

        Returns:
            Created comment data
        """
        return self._request(
            "POST",
            f"issue/{key}/comment",
            json_data={"body": body},
            context=key,
        )

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
            body["comment"] = comment
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
            body["update"] = {"comment": [{"add": {"body": comment}}]}

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
            body["fields"]["description"] = description

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
            update_fields["description"] = description
        if assignee is not None:
            # Empty string means unassign, otherwise set to username
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

        try:
            response = self._session.get(
                content_url,
                timeout=self.timeout,
            )
            if not response.ok:
                raise JiraToolError(
                    code=ErrorCode.SERVER_ERROR,
                    message=f"Failed to download attachment: HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            return response.content, metadata

        except requests.exceptions.Timeout as e:
            raise NetworkError(
                code=ErrorCode.TIMEOUT,
                message=f"Attachment download timed out after {self.timeout}s",
                details={"url": content_url},
            ) from e
        except requests.exceptions.RequestException as e:
            raise NetworkError(
                code=ErrorCode.CONNECTION_ERROR,
                message=f"Attachment download failed: {str(e)}",
                details={"url": content_url},
            ) from e

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

        url = self._build_url(f"issue/{key}/attachments")

        try:
            with open(file_path, "rb") as f:
                # JIRA requires X-Atlassian-Token header for attachment uploads
                headers = {
                    "X-Atlassian-Token": "no-check",
                }
                response = self._session.post(
                    url,
                    files={"file": (filename, f)},
                    headers=headers,
                    timeout=self.timeout,
                )

            return self._handle_response(response, f"upload to {key}")

        except requests.exceptions.Timeout as e:
            raise NetworkError(
                code=ErrorCode.TIMEOUT,
                message=f"Attachment upload timed out after {self.timeout}s",
                details={"file": file_path},
            ) from e
        except requests.exceptions.RequestException as e:
            raise NetworkError(
                code=ErrorCode.CONNECTION_ERROR,
                message=f"Attachment upload failed: {str(e)}",
                details={"file": file_path},
            ) from e
        except OSError as e:
            raise InvalidInputError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Cannot read file: {e}",
                details={"file": file_path},
            ) from e
