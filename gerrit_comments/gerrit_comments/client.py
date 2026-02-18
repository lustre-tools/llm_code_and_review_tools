"""Gerrit REST API client for comment operations."""

import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from pygerrit2 import GerritRestAPI, HTTPBasicAuth  # type: ignore[import-untyped]


# Load .env file from standard locations (in priority order)
def _load_env_file():
    """Load environment variables from .env file in standard locations.

    Priority order (highest to lowest):
    1. Current directory (.env) - allows project-specific overrides
    2. User config directory (~/.config/gerrit-comments/.env)
    3. System config directory (/etc/gerrit-comments/.env)
    4. Shared support files (/shared/support_files/.env)

    All matching files are loaded. Higher-priority files override
    values set by lower-priority ones.
    """
    env_locations = [
        Path("/shared/support_files/.env"),
        Path("/etc/gerrit-comments/.env"),
        Path.home() / ".config" / "gerrit-comments" / ".env",
        Path.cwd() / ".env",
    ]

    # Load in priority order (lowest first); each overrides the previous
    for env_path in env_locations:
        if env_path.exists():
            load_dotenv(env_path, override=True)


# Load .env file when module is imported
_load_env_file()


# Config file location for error messages
CONFIG_PATH = Path.home() / ".config" / "gerrit-comments" / ".env"

# Default Gerrit URL for when only a change number is provided
# Falls back to GERRIT_URL environment variable
DEFAULT_GERRIT_URL: str | None = os.environ.get("GERRIT_URL")


class GerritConfigError(Exception):
    """Raised when Gerrit credentials are not configured."""
    pass


class GerritCommentsClient:
    """Client for interacting with Gerrit comments via REST API."""

    def __init__(
        self,
        url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        """Initialize the Gerrit client.

        Args:
            url: Gerrit server URL. Defaults to GERRIT_URL env var.
            username: Gerrit username. Defaults to GERRIT_USER env var.
            password: Gerrit HTTP password. Defaults to GERRIT_PASS env var.

        Raises:
            GerritConfigError: If required credentials are not configured.
        """
        self.url = url or os.environ.get("GERRIT_URL")
        self.username = username or os.environ.get("GERRIT_USER")
        self.password = password or os.environ.get("GERRIT_PASS")

        # Check for missing configuration
        missing = []
        if not self.url:
            missing.append("GERRIT_URL")
        if not self.username:
            missing.append("GERRIT_USER")
        if not self.password:
            missing.append("GERRIT_PASS")

        if missing:
            raise GerritConfigError(
                f"Missing configuration: {', '.join(missing)}. "
                f"Set environment variables or create config file at {CONFIG_PATH}"
            )

        auth = HTTPBasicAuth(self.username, self.password)
        self.rest = GerritRestAPI(url=self.url, auth=auth)
        self.rest.kwargs["timeout"] = 60

    @staticmethod
    def parse_gerrit_url(url: str, default_base_url: str | None = None) -> tuple[str, int]:
        """Parse a Gerrit URL or change number to extract base URL and change number.

        Supports:
        - https://review.whamcloud.com/c/fs/lustre-release/+/61965
        - https://review.whamcloud.com/61965
        - https://review.whamcloud.com/c/fs/lustre-release/+/61965/3 (with patchset)
        - 61965 (just the change number, uses default base URL)

        Args:
            url: Gerrit URL or change number
            default_base_url: Base URL to use if only a change number is provided.
                              Defaults to DEFAULT_GERRIT_URL.

        Returns:
            Tuple of (base_url, change_number)
        """
        # Check if it's just a number
        if url.isdigit():
            base = default_base_url or DEFAULT_GERRIT_URL
            if not base:
                raise ValueError(
                    f"Could not parse Gerrit URL or change number: {url}. "
                    "Set GERRIT_URL environment variable to use change numbers directly."
                )
            return base, int(url)

        # Pattern for /c/project/+/number style URLs
        match = re.match(r"(https?://[^/]+)/c/[^/]+(?:/[^/]+)*/\+/(\d+)(?:/\d+)?", url)
        if match:
            return match.group(1), int(match.group(2))

        # Pattern for simple /number style URLs
        match = re.match(r"(https?://[^/]+)/(\d+)(?:/\d+)?", url)
        if match:
            return match.group(1), int(match.group(2))

        raise ValueError(f"Could not parse Gerrit URL or change number: {url}")

    def get_change_detail(self, change_number: int) -> dict[str, Any]:
        """Get detailed information about a change.

        Args:
            change_number: The change number

        Returns:
            Dict with change details including revisions
        """
        return self.rest.get(
            f"/changes/{change_number}?"
            "o=ALL_REVISIONS&o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_ACCOUNTS"
        )

    def get_comments(self, change_number: int) -> dict[str, list[dict[str, Any]]]:
        """Get all published comments for a change.

        Args:
            change_number: The change number

        Returns:
            Dict mapping file paths to lists of comments
        """
        return self.rest.get(f"/changes/{change_number}/comments")

    def get_messages(self, change_number: int) -> list[dict[str, Any]]:
        """Get all review messages (top-level comments) for a change.

        These are the overall review comments posted when someone submits a review,
        as opposed to inline comments on specific files/lines.

        Args:
            change_number: The change number

        Returns:
            List of message dicts with keys: id, author, date, message, _revision_number
        """
        return self.rest.get(f"/changes/{change_number}/messages")

    def get_file_diff(
        self,
        change_number: int,
        revision_id: str,
        file_path: str,
        context: str = "ALL",
    ) -> Optional[dict[str, Any]]:
        """Get the diff for a specific file.

        Args:
            change_number: The change number
            revision_id: The revision SHA or patch set number
            file_path: Path to the file
            context: Amount of context ("ALL" for full file)

        Returns:
            Dict with diff information or None on error
        """
        encoded_path = quote(file_path, safe="")
        try:
            return self.rest.get(
                f"/changes/{change_number}/revisions/{revision_id}/files/{encoded_path}/diff?context={context}"
            )
        except Exception as e:
            print(f"Error getting diff for {file_path}: {e}")
            return None

    def get_revision_for_patchset(
        self, change_number: int, patch_set: int
    ) -> Optional[str]:
        """Get the revision ID for a specific patch set number.

        Args:
            change_number: The change number
            patch_set: The patch set number

        Returns:
            Revision SHA or None if not found
        """
        change = self.get_change_detail(change_number)
        revisions = change.get("revisions", {})

        for rev_id, rev_data in revisions.items():
            if rev_data.get("_number") == patch_set:
                return rev_id

        return None

    def post_review(
        self,
        change_number: int,
        revision_id: str,
        message: Optional[str] = None,
        comments: Optional[dict[str, list[dict[str, Any]]]] = None,
        labels: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        """Post a review with optional comments and labels.

        Args:
            change_number: The change number
            revision_id: The revision SHA or "current"
            message: Optional overall review message
            comments: Optional dict mapping file paths to comment lists
            labels: Optional dict mapping label names to values

        Returns:
            Response from the API
        """
        review_input: dict[str, Any] = {}

        if message:
            review_input["message"] = message

        if comments:
            review_input["comments"] = comments

        if labels:
            review_input["labels"] = labels

        return self.rest.post(
            f"/changes/{change_number}/revisions/{revision_id}/review",
            json=review_input,
        )

    def reply_to_comment(
        self,
        change_number: int,
        revision_id: str,
        file_path: str,
        comment_id: str,
        message: str,
        line: Optional[int] = None,
        mark_resolved: bool = False,
    ) -> dict[str, Any]:
        """Reply to a specific comment.

        Args:
            change_number: The change number
            revision_id: The revision SHA or "current"
            file_path: Path to the file containing the comment
            comment_id: ID of the comment to reply to
            message: Reply message
            line: Line number (should match original comment)
            mark_resolved: Whether to mark the thread as resolved

        Returns:
            Response from the API
        """
        comment_input = {
            "in_reply_to": comment_id,
            "message": message,
            "unresolved": not mark_resolved,
        }

        if line is not None:
            comment_input["line"] = line

        comments = {file_path: [comment_input]}

        return self.post_review(
            change_number=change_number,
            revision_id=revision_id,
            comments=comments,
        )

    def mark_comment_done(
        self,
        change_number: int,
        revision_id: str,
        file_path: str,
        comment_id: str,
        line: Optional[int] = None,
        message: str = "Done",
    ) -> dict[str, Any]:
        """Mark a comment as done with an optional message.

        Args:
            change_number: The change number
            revision_id: The revision SHA or "current"
            file_path: Path to the file containing the comment
            comment_id: ID of the comment to mark done
            line: Line number (should match original comment)
            message: Message to include (default: "Done")

        Returns:
            Response from the API
        """
        return self.reply_to_comment(
            change_number=change_number,
            revision_id=revision_id,
            file_path=file_path,
            comment_id=comment_id,
            message=message,
            line=line,
            mark_resolved=True,
        )

    def format_change_url(self, project: str, change_number: int) -> str:
        """Format a web URL for a change.

        Args:
            project: Project name
            change_number: Change number

        Returns:
            Full URL to the change
        """
        return f"{self.url}/c/{project}/+/{change_number}"

    def get_reviewers(self, change_number: int) -> list[dict[str, Any]]:
        """Get all reviewers for a change.

        Args:
            change_number: The change number

        Returns:
            List of reviewer account dicts
        """
        return self.rest.get(f"/changes/{change_number}/reviewers")

    def add_reviewer(
        self,
        change_number: int,
        reviewer: str,
        state: str = "REVIEWER",
    ) -> dict[str, Any]:
        """Add a reviewer to a change.

        Args:
            change_number: The change number
            reviewer: Account ID, email, or username of the reviewer
            state: "REVIEWER" (default) or "CC"

        Returns:
            Response with added reviewer info
        """
        return self.rest.post(
            f"/changes/{change_number}/reviewers",
            json={"reviewer": reviewer, "state": state},
        )

    def remove_reviewer(self, change_number: int, reviewer: str) -> None:
        """Remove a reviewer from a change.

        Args:
            change_number: The change number
            reviewer: Account ID, email, or username of the reviewer
        """
        self.rest.delete(f"/changes/{change_number}/reviewers/{reviewer}")

    def abandon_change(
        self,
        change_number: int,
        message: str = "",
    ) -> dict[str, Any]:
        """Abandon a Gerrit change.

        Tries REST API first, falls back to SSH if REST returns 401.

        Args:
            change_number: The change number
            message: Optional message explaining why

        Returns:
            Response from the API (change info)
        """
        body: dict[str, Any] = {}
        if message:
            body["message"] = message
        try:
            return self.rest.post(
                f"/changes/{change_number}/abandon",
                json=body,
            )
        except Exception as e:
            if "401" not in str(e):
                raise
            # REST 401 — fall back to SSH
            return self._abandon_via_ssh(change_number, message)

    def _abandon_via_ssh(
        self,
        change_number: int,
        message: str = "",
    ) -> dict[str, Any]:
        """Abandon a change via Gerrit SSH interface.

        Used as fallback when REST API returns 401.
        Discovers SSH user from GERRIT_SSH_USER env var,
        git remote URL, or GERRIT_USER.

        Args:
            change_number: The change number
            message: Optional message explaining why

        Returns:
            Dict with change_number and status
        """
        import subprocess
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        host = parsed.hostname
        ssh_port = os.environ.get("GERRIT_SSH_PORT", "29418")
        ssh_user = os.environ.get("GERRIT_SSH_USER", "")

        if not ssh_user:
            ssh_user = self._discover_ssh_user(host)

        if not ssh_user:
            raise Exception(
                "Cannot determine SSH user for Gerrit. "
                "Set GERRIT_SSH_USER env var or configure "
                "a git remote pointing to Gerrit."
            )

        cmd = [
            "ssh", "-p", ssh_port,
            f"{ssh_user}@{host}",
            "gerrit", "review", "--abandon",
        ]
        if message:
            cmd.extend(["--message", f'"{message}"'])
        cmd.append(f"{change_number},1")

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise Exception(
                f"SSH abandon failed for {change_number}: {stderr}"
            )
        return {
            "status": "ABANDONED",
            "_number": change_number,
        }

    @staticmethod
    def _discover_ssh_user(host: str) -> str:
        """Try to find SSH username for Gerrit.

        Checks in order:
        1. Git remote URLs with ssh://user@host
        2. Shell alias definitions (gitpush* aliases)
        3. User config .env file (not cwd .env which
           may be a template)

        Args:
            host: Gerrit hostname to match against

        Returns:
            Username string, or empty string if not found
        """
        import subprocess

        # 1. Check git remotes
        try:
            result = subprocess.run(
                ["git", "remote", "-v"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.decode().splitlines():
                    if host in line and "ssh://" in line:
                        match = re.match(
                            r".*ssh://([^@]+)@"
                            + re.escape(host),
                            line,
                        )
                        if match:
                            return match.group(1)
        except Exception:
            pass

        # 2. Check shell aliases (bash)
        try:
            result = subprocess.run(
                ["bash", "-ic", "alias"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.decode().splitlines():
                    if host in line and "ssh://" in line:
                        match = re.search(
                            r"ssh://([^@]+)@"
                            + re.escape(host),
                            line,
                        )
                        if match:
                            return match.group(1)
        except Exception:
            pass

        # 3. Check user config .env directly
        user_env = (
            Path.home() / ".config"
            / "gerrit-comments" / ".env"
        )
        if user_env.exists():
            try:
                for line in user_env.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("GERRIT_USER="):
                        val = line.split("=", 1)[1].strip()
                        if val and val != "your-username":
                            return val
            except Exception:
                pass

        return ""

    def suggest_accounts(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Suggest accounts matching a query string (fuzzy search).

        Args:
            query: Search string (name, email, or username)
            limit: Maximum number of results

        Returns:
            List of matching account dicts with _account_id, name, email, username
        """
        encoded_query = quote(query, safe="")
        return self.rest.get(f"/accounts/?suggest&q={encoded_query}&n={limit}")

    def search_accounts(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        username: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for accounts by name, email, or username.

        Args:
            name: Search by display name
            email: Search by email
            username: Search by username
            limit: Maximum number of results

        Returns:
            List of matching account dicts
        """
        parts = []
        if name:
            parts.append(f"name:{name}")
        if email:
            parts.append(f"email:{email}")
        if username:
            parts.append(f"username:{username}")

        if not parts:
            return []

        query = " OR ".join(parts)
        encoded_query = quote(query, safe="")
        return self.rest.get(f"/accounts/?q={encoded_query}&n={limit}")
