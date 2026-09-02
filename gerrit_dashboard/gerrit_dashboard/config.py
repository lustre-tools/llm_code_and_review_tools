"""Configuration for the dashboard.

Gerrit credentials come from the gerrit_cli env layering
(GERRIT_URL/GERRIT_USER/GERRIT_PASS, e.g. from a shell profile or
~/.config/gerrit-cli/.env).  Everything dashboard-specific uses GD_*
environment variables, all optional.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent

COMMUNITY_PROJECTS = ["fs/lustre-release"]

# Landing queues: work lands on <branch>-next first (by direct push — no
# Gerrit changes exist there) and is merged to <branch> later.  Add the
# ones your deployment cares about via GD_NEXT_QUEUES=proj:branch,...
DEFAULT_NEXT_QUEUES = [("fs/lustre-release", "master")]

# Colour tokens a branch may be tagged with in GD_BRANCH_COLORS; they map
# to the stylesheet's semantic colours, so anything else is ignored.
BRANCH_COLOR_TOKENS = ("tag", "run", "ok", "bad", "warn", "muted")


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@dataclass
class Config:
    # Localhost-only by default: put a TLS-terminating reverse proxy in
    # front (it may mount the app under a path prefix — the app honours
    # X-Forwarded-Prefix).  Set GD_HOST=0.0.0.0 for direct exposure.
    host: str = "127.0.0.1"
    port: int = 1055
    refresh_seconds: int = 0
    data_dir: Path = field(default_factory=lambda: _PKG_ROOT / "data")
    # Days of inactivity after which review-requested changes are shown
    # collapsed as dormant instead of classified in detail.
    review_dormant_days: int = 60
    # Age (days) of the current patchset before a CI-green unreviewed
    # patch of mine counts as "needs reviewer nudge".
    nudge_days: int = 3
    # Days without any update before a change gets a "stalled" badge.
    # Generous on purpose: green patches legitimately sit in line for
    # weeks — only truly forgotten ones deserve the marker.
    stale_days: int = 100
    # P0/P1 signals older than this many days move to the collapsed
    # "longstanding" part of the action section.
    action_recent_days: int = 7
    gerrit_base_url: str = ""
    # User whose board / (the bare URL) redirects to; also inherits the
    # legacy single-user data files on first multi-user start.  Defaults
    # to the credential's own account (GERRIT_USER).
    default_user: str = ""
    # Landing queues to read, as (project, branch) pairs; the dashboard
    # marks a change as queued when its Change-Id sits on <branch>-next.
    next_queues: list[tuple[str, str]] = field(
        default_factory=lambda: list(DEFAULT_NEXT_QUEUES))
    # Emails whose Verified +1 substitutes for a MISSING test-bot vote
    # (a release manager vouching for a patch the bots did not cover).
    verified_override_emails: list[str] = field(default_factory=list)
    # branch name -> colour token, for telling release branches apart at
    # a glance, e.g. GD_BRANCH_COLORS=b2_15:tag,b2_16:run
    branch_colors: dict[str, str] = field(default_factory=dict)
    # Distinct non-owner +1s a change needs; backports need fewer.
    review_threshold: int = 2
    backport_review_threshold: int = 1
    # Project allowlist. Empty = everything the credentials can see.
    # The credentials may be able to read projects this instance should
    # not serve — set GD_COMMUNITY=1 (fs/lustre-release only) or
    # GD_PROJECTS=a,b so those are never even fetched, let alone
    # rendered.
    projects: list[str] = field(default_factory=list)
    # Bound on concurrently live per-user contexts (LRU-evicted beyond
    # this; the default user is never evicted).
    max_users: int = 20
    # Optional board allowlist: when set, ONLY these usernames resolve to
    # a board and every other token 404s before any Gerrit lookup.
    # EMPTY (the default) means any Gerrit user can open their own board
    # by username — which is the point of a shared deployment.  Set
    # GD_BOARDS only for a deliberately closed instance.
    boards: list[str] = field(default_factory=list)
    # Floor between two refreshes of the SAME board, however many are
    # requested.  A refresh is ~11 bulk Gerrit queries plus comment
    # fetches, so this is what stops the refresh button (or a script
    # hitting the endpoint) from being an amplifier against Gerrit.
    # Requests inside the window are not dropped, just deferred.
    min_refresh_seconds: int = 30
    # Caps on the per-user lists.  The watchlist matters most: every
    # entry not already covered by the role queries costs one extra
    # Gerrit fetch on EVERY refresh, so an unbounded list is a permanent
    # load multiplier.  Both are far above real use.
    max_watchlist: int = 200
    max_hidden: int = 2000
    # Deployment web chrome (env-only; empty/dark by default so the tool's
    # own look and the CLI/static exports are unchanged).
    # First-visit theme when the viewer has no saved preference — the picker
    # still lets anyone switch. "dark" keeps the built-in default.
    default_theme: str = "dark"
    # Optional "back to the parent site" link shown in the header, for when
    # the dashboard is mounted inside a larger tools site.
    site_name: str = ""
    site_home: str = ""

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls()
        cfg.host = os.environ.get("GD_HOST", cfg.host)
        cfg.port = int(os.environ.get("GD_PORT", cfg.port))
        cfg.refresh_seconds = int(os.environ.get("GD_REFRESH_SECONDS", cfg.refresh_seconds))
        if os.environ.get("GD_DATA_DIR"):
            cfg.data_dir = Path(os.environ["GD_DATA_DIR"])
        cfg.review_dormant_days = int(os.environ.get("GD_REVIEW_DORMANT_DAYS", cfg.review_dormant_days))
        cfg.nudge_days = int(os.environ.get("GD_NUDGE_DAYS", cfg.nudge_days))
        cfg.stale_days = int(os.environ.get("GD_STALE_DAYS", cfg.stale_days))
        cfg.action_recent_days = int(os.environ.get("GD_ACTION_RECENT_DAYS", cfg.action_recent_days))
        cfg.gerrit_base_url = os.environ.get("GERRIT_URL", "https://review.whamcloud.com").rstrip("/")
        cfg.default_user = os.environ.get("GD_DEFAULT_USER") or os.environ.get("GERRIT_USER") or cfg.default_user
        if os.environ.get("GD_PROJECTS"):
            cfg.projects = _split_csv(os.environ["GD_PROJECTS"])
        elif os.environ.get("GD_COMMUNITY", "").lower() in ("1", "true", "yes"):
            cfg.projects = list(COMMUNITY_PROJECTS)
        if os.environ.get("GD_NEXT_QUEUES"):
            queues = []
            for item in _split_csv(os.environ["GD_NEXT_QUEUES"]):
                project, _, branch = item.rpartition(":")
                if project and branch:
                    queues.append((project, branch))
            cfg.next_queues = queues
        if os.environ.get("GD_VERIFIED_OVERRIDE"):
            cfg.verified_override_emails = [
                e.lower() for e in _split_csv(os.environ["GD_VERIFIED_OVERRIDE"])]
        if os.environ.get("GD_BRANCH_COLORS"):
            colors = {}
            for item in _split_csv(os.environ["GD_BRANCH_COLORS"]):
                branch, _, token = item.rpartition(":")
                if branch and token in BRANCH_COLOR_TOKENS:
                    colors[branch] = token
            cfg.branch_colors = colors
        cfg.review_threshold = int(
            os.environ.get("GD_REVIEW_THRESHOLD", cfg.review_threshold))
        cfg.backport_review_threshold = int(
            os.environ.get("GD_BACKPORT_REVIEW_THRESHOLD", cfg.backport_review_threshold))
        cfg.max_users = int(os.environ.get("GD_MAX_USERS", cfg.max_users))
        cfg.min_refresh_seconds = int(
            os.environ.get("GD_MIN_REFRESH_SECONDS", cfg.min_refresh_seconds))
        cfg.max_watchlist = int(os.environ.get("GD_MAX_WATCHLIST", cfg.max_watchlist))
        cfg.max_hidden = int(os.environ.get("GD_MAX_HIDDEN", cfg.max_hidden))
        theme = os.environ.get("GD_DEFAULT_THEME", "").strip().lower()
        if theme in ("dark", "light", "gruvbox-light"):
            cfg.default_theme = theme
        cfg.site_name = os.environ.get("GD_SITE_NAME", cfg.site_name)
        cfg.site_home = os.environ.get("GD_SITE_HOME", cfg.site_home)
        if os.environ.get("GD_BOARDS"):
            cfg.boards = _split_csv(os.environ["GD_BOARDS"])
        # The bare-URL default board must be reachable within its own
        # allowlist.
        if cfg.boards and cfg.default_user not in cfg.boards:
            cfg.boards.append(cfg.default_user)
        return cfg
