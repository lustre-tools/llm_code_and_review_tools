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
    2. User config directory (~/.config/gerrit-cli/.env)
    3. System config directory (/etc/gerrit-cli/.env)
    4. Shared support files (/shared/support_files/.env)

    All matching files are loaded. Higher-priority files override
    values set by lower-priority ones.
    """
    env_locations = [
        Path("/shared/support_files/.env"),
        Path("/etc/gerrit-cli/.env"),
        Path.home() / ".config" / "gerrit-cli" / ".env",
        Path.cwd() / ".env",
    ]

    # Load in priority order (lowest first); each overrides the previous
    for env_path in env_locations:
        if env_path.exists():
            load_dotenv(env_path, override=True)


# Load .env file when module is imported
_load_env_file()


# Config file location for error messages
CONFIG_PATH = Path.home() / ".config" / "gerrit-cli" / ".env"

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

    def set_topic(
        self,
        change_number: int,
        topic: str,
    ) -> dict[str, Any]:
        """Set the topic on a Gerrit change.

        Tries REST API first, falls back to SSH if REST returns 401.

        Args:
            change_number: The change number
            topic: The topic name to set

        Returns:
            Dict with change_number and topic
        """
        try:
            self.rest.put(
                f"/changes/{change_number}/topic",
                json={"topic": topic},
            )
            return {"_number": change_number, "topic": topic}
        except Exception as e:
            if "401" not in str(e):
                raise
            return self._set_topic_via_ssh(change_number, topic)

    def _set_topic_via_ssh(
        self,
        change_number: int,
        topic: str,
    ) -> dict[str, Any]:
        """Set topic via Gerrit SSH interface.

        Used as fallback when REST API returns 401.
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
            "gerrit", "set-topic",
            str(change_number),
            topic,
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise Exception(
                f"SSH set-topic failed for {change_number}: {stderr}"
            )
        return {
            "_number": change_number,
            "topic": topic,
        }

    def get_hashtags(
        self,
        change_number: int,
    ) -> list[str]:
        """Get the hashtags on a Gerrit change.

        Args:
            change_number: The change number

        Returns:
            List of hashtag strings
        """
        return self.rest.get(f"/changes/{change_number}/hashtags")

    def get_related_changes(
        self,
        change_number: int,
    ) -> list[dict]:
        """Get the relation chain for a change (git parent/child series).

        Returns changes in order from series root (ancestor) to tip
        (descendant). The current change is included in the list.

        Args:
            change_number: The change number

        Returns:
            List of dicts with keys: _change_number, _revision_number,
            _current_revision_number, status, subject, change_id
        """
        result = self.rest.get(
            f"/changes/{change_number}/revisions/current/related"
        )
        return result.get("changes", [])

    def add_hashtags(
        self,
        change_number: int,
        add: list[str],
        remove: list[str] | None = None,
    ) -> list[str]:
        """Add (and optionally remove) hashtags on a Gerrit change.

        Args:
            change_number: The change number
            add: Hashtags to add
            remove: Hashtags to remove (optional)

        Returns:
            Updated list of hashtags on the change
        """
        body: dict[str, Any] = {"add": add}
        if remove:
            body["remove"] = remove
        return self.rest.post(
            f"/changes/{change_number}/hashtags",
            json=body,
        )

    def restore_change(
        self,
        change_number: int,
        message: str = "",
    ) -> dict[str, Any]:
        """Restore an abandoned Gerrit change.

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
                f"/changes/{change_number}/restore",
                json=body,
            )
        except Exception as e:
            if "401" not in str(e):
                raise
            return self._restore_via_ssh(change_number, message)

    def _restore_via_ssh(
        self,
        change_number: int,
        message: str = "",
    ) -> dict[str, Any]:
        """Restore a change via Gerrit SSH interface."""
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
            "gerrit", "review", "--restore",
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
                f"SSH restore failed for {change_number}: {stderr}"
            )
        return {
            "status": "NEW",
            "_number": change_number,
        }

    def rebase_change(
        self,
        change_number: int,
    ) -> dict[str, Any]:
        """Trigger a server-side rebase of a Gerrit change.

        Tries REST API first, falls back to SSH if REST returns 401.

        Args:
            change_number: The change number

        Returns:
            Response from the API (change info)
        """
        try:
            return self.rest.post(
                f"/changes/{change_number}/rebase",
                json={},
            )
        except Exception as e:
            if "401" not in str(e):
                raise
            return self._rebase_via_ssh(change_number)

    def _rebase_via_ssh(
        self,
        change_number: int,
    ) -> dict[str, Any]:
        """Rebase a change via Gerrit SSH interface."""
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

        # Get current patchset number
        change = self.get_change_detail(change_number)
        revisions = change.get("revisions", {})
        current_ps = 1
        for rev_data in revisions.values():
            ps = rev_data.get("_number", 1)
            if ps > current_ps:
                current_ps = ps

        cmd = [
            "ssh", "-p", ssh_port,
            f"{ssh_user}@{host}",
            "gerrit", "review", "--rebase",
            f"{change_number},{current_ps}",
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise Exception(
                f"SSH rebase failed for {change_number}: {stderr}"
            )
        return {
            "status": "NEW",
            "_number": change_number,
        }

    def get_diff(
        self,
        change_number: int,
        file_path: str,
        revision_a: str,
        revision_b: str = "current",
    ) -> dict[str, Any]:
        """Get the diff for a file between two revisions.

        Args:
            change_number: The change number
            file_path: Path to the file
            revision_a: Base revision (patchset number or SHA)
            revision_b: Target revision (default "current")

        Returns:
            Diff info from the API
        """
        encoded_path = quote(file_path, safe="")
        return self.rest.get(
            f"/changes/{change_number}/revisions/{revision_b}"
            f"/files/{encoded_path}/diff?base={revision_a}"
        )

    def get_files_between_patchsets(
        self,
        change_number: int,
        patchset_a: int,
        patchset_b: int,
    ) -> dict[str, dict[str, Any]]:
        """Get list of files changed between two patchsets.

        Args:
            change_number: The change number
            patchset_a: Base patchset number
            patchset_b: Target patchset number

        Returns:
            Dict mapping file paths to file info
        """
        return self.rest.get(
            f"/changes/{change_number}/revisions/{patchset_b}"
            f"/files?base={patchset_a}"
        )

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
            / "gerrit-cli" / ".env"
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

    def search_changes(
        self,
        query: str,
        limit: int = 25,
        start: int = 0,
        options: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for changes using a Gerrit query string.

        Args:
            query: Gerrit search query (e.g. "owner:self status:open",
                   "project:fs/lustre-release topic:LU-12345")
            limit: Maximum number of results (default 25)
            start: Offset for pagination (default 0)
            options: Additional output options (e.g. ["CURRENT_REVISION",
                     "DETAILED_ACCOUNTS"]). If None, includes
                     CURRENT_REVISION and DETAILED_ACCOUNTS.

        Returns:
            List of change dicts matching the query
        """
        if options is None:
            options = ["CURRENT_REVISION", "DETAILED_ACCOUNTS"]

        encoded_query = quote(query, safe="")
        params = f"/changes/?q={encoded_query}&n={limit}&S={start}"
        for opt in options:
            params += f"&o={opt}"

        return self.rest.get(params)
