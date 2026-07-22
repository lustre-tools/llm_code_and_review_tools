"""Confluence REST API client (read-only).

Works against both Confluence Cloud (``/wiki/rest/api``, Basic auth) and
Confluence Server/DC (``/rest/api``; the public whamcloud wiki is reached
anonymously). The ``/wiki`` prefix, when needed, is part of the configured
server URL, so a single ``{server}/rest/api/`` base works for both.

Only GET endpoints are implemented — this tool never writes.
"""

import random
import sys
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import requests

from .config import ConfluenceConfig
from .errors import (
    AuthError,
    ConfluenceToolError,
    ErrorCode,
    InvalidInputError,
    NetworkError,
    NotFoundError,
)

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0


class _HTMLToText(HTMLParser):
    """Convert Confluence storage/view HTML to readable plain text.

    Block elements become newlines, list items get a "- " prefix,
    headings a "# " prefix, and table cells are joined with " | ".
    """

    _BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0  # depth inside <script>/<style>

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("li",):
            self.parts.append("\n- ")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n# ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        # Collapse excess whitespace and blank lines
        lines = [line.strip() for line in raw.splitlines()]
        out: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if not blank and out:
                    out.append("")
                blank = True
            else:
                out.append(line)
                blank = False
        return "\n".join(out).strip()


def html_to_text(html: str | None) -> str:
    """Convert a Confluence HTML body to plain text."""
    if not html:
        return ""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.text()


class ConfluenceClient:
    """Read-only Confluence REST API client."""

    def __init__(
        self,
        config: ConfluenceConfig,
        timeout: int = 30,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
        debug: bool = False,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.retry_max_delay = retry_max_delay
        self.debug = debug
        self._session = requests.Session()

        headers = {"Accept": "application/json", "User-Agent": "confluence-tool/1.0"}
        auth = config.get_auth_header()
        if auth:
            headers["Authorization"] = auth
        self._session.headers.update(headers)

    def _debug(self, msg: str) -> None:
        if self.debug:
            print(f"[DEBUG] {msg}", file=sys.stderr)

    def _build_url(self, endpoint: str) -> str:
        base = f"{self.config.server}/rest/api/"
        return urljoin(base, endpoint.lstrip("/"))

    def _calculate_retry_delay(self, attempt: int) -> float:
        delay = self.retry_backoff * (2**attempt)
        jitter = delay * 0.25 * (2 * random.random() - 1)
        return min(delay + jitter, self.retry_max_delay)

    def _handle_response(self, response: requests.Response, context: str = "") -> Any:
        try:
            body = response.json() if response.text else {}
        except ValueError:
            body = {"raw": response.text[:500] if response.text else ""}

        detail = ""
        if isinstance(body, dict):
            detail = body.get("message") or ""
        if not detail and isinstance(body, dict) and body:
            raw = str(body)
            detail = raw[:500] + ("..." if len(raw) > 500 else "")

        if response.status_code in (401, 403):
            raise AuthError(
                message=(
                    ("Authentication failed" if response.status_code == 401 else "Permission denied")
                    + (f": {detail}" if detail else "")
                ),
                http_status=response.status_code,
            )
        if response.status_code == 404:
            raise NotFoundError(
                code=ErrorCode.PAGE_NOT_FOUND,
                message=f"Resource not found{': ' + context if context else ''}"
                + (f": {detail}" if detail else ""),
                http_status=404,
            )
        if response.status_code == 400:
            raise InvalidInputError(
                code=ErrorCode.INVALID_CQL,
                message=f"Invalid request{': ' + detail if detail else ''}",
                http_status=400,
            )
        if response.status_code == 429:
            raise ConfluenceToolError(
                code=ErrorCode.RATE_LIMITED,
                message="Rate limited by Confluence server",
                http_status=429,
            )
        if response.status_code >= 500:
            raise ConfluenceToolError(
                code=ErrorCode.SERVER_ERROR,
                message=f"Confluence server error{': ' + detail if detail else ''}",
                http_status=response.status_code,
            )
        if not response.ok:
            raise ConfluenceToolError(
                code=ErrorCode.SERVER_ERROR,
                message=f"Unexpected error (HTTP {response.status_code})"
                + (f": {detail}" if detail else ""),
                http_status=response.status_code,
            )
        if response.status_code == 204:
            return {}
        return body

    def _is_retryable(self, error: Exception) -> bool:
        if isinstance(error, NetworkError):
            return True
        if isinstance(error, ConfluenceToolError):
            if error.code == ErrorCode.RATE_LIMITED:
                return True
            if error.code == ErrorCode.SERVER_ERROR and error.http_status is not None:
                return error.http_status >= 500
        return False

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        context: str = "",
    ) -> Any:
        url = self._build_url(endpoint)
        last_error: Exception | None = None
        self._debug(f"{method} {url} params={params}")

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.request(
                    method=method, url=url, params=params, timeout=self.timeout,
                )
                self._debug(f"  -> {response.status_code} ({len(response.content)} bytes)")
                return self._handle_response(response, context)
            except requests.exceptions.Timeout as e:
                last_error = NetworkError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Request timed out after {self.timeout}s",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except requests.exceptions.RequestException as e:
                last_error = NetworkError(
                    code=ErrorCode.CONNECTION_ERROR,
                    message=f"Request failed: {e}",
                    details={"url": url, "attempt": attempt + 1},
                )
                last_error.__cause__ = e
            except (AuthError, NotFoundError, InvalidInputError):
                raise
            except ConfluenceToolError as e:
                last_error = e
                if not self._is_retryable(e):
                    raise

            if attempt < self.max_retries:
                time.sleep(self._calculate_retry_delay(attempt))

        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected state in retry loop")

    # ── Read operations ─────────────────────────────────────────────

    def search(
        self,
        cql: str,
        limit: int = 25,
        expand: str | None = None,
    ) -> dict[str, Any]:
        """Search content with CQL. Returns the raw search response."""
        params: dict[str, Any] = {"cql": cql, "limit": limit}
        if expand:
            params["expand"] = expand
        return self._request("GET", "content/search", params=params, context="search")

    def get_page(
        self,
        page_id: str,
        expand: str = "body.view,version,space",
    ) -> dict[str, Any]:
        """Get a single page (content) by id."""
        return self._request(
            "GET", f"content/{page_id}",
            params={"expand": expand},
            context=f"page {page_id}",
        )

    def list_spaces(self, limit: int = 100, start: int = 0) -> dict[str, Any]:
        """List spaces (used for discovery and connectivity checks)."""
        return self._request(
            "GET", "space",
            params={"limit": limit, "start": start},
            context="spaces",
        )
