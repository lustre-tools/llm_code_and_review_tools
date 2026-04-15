"""Node construction and metadata-copy helpers.

A "node" is the dict shape consumed by the HTML template — the graph
builder creates one per change and progressively enriches it with
topic/hashtag/status/review info as more Gerrit data arrives."""

import re
from typing import Any


def _make_node(
    cn: int, subject: str, status: str, latest: int,
    author: str, base_url: str, ticket: str = "",
    topic: str = "", hashtags: list[str] | None = None,
    updated: str = "", is_wip: bool = False,
    project: str = "fs/lustre-release",
) -> dict[str, Any]:
    """Create a node dict for the graph."""
    if not ticket:
        m = re.match(r"(LU-\d+)", subject)
        ticket = m.group(1) if m else ""
    ref = f"refs/changes/{cn % 100:02d}/{cn}/{latest}"
    fetch_cmd = f"git fetch {base_url}/{project} {ref}"
    return {
        "id": cn,
        "subject": subject,
        "status": status,
        "current_patchset": latest,
        "author": author,
        "url": f"{base_url}/c/{project}/+/{cn}",
        "ticket": ticket,
        "topic": topic,
        "hashtags": hashtags or [],
        "checkout_cmd": f"{fetch_cmd} && git checkout FETCH_HEAD",
        "cherrypick_cmd": f"{fetch_cmd} && git cherry-pick FETCH_HEAD",
        "updated": updated,
        "is_wip": is_wip,
    }


def _update_node_meta(node: dict[str, Any], change: dict[str, Any]) -> None:
    """Copy topic/hashtags/updated/wip-flag from a change payload onto a node."""
    node["topic"] = change.get("topic", "")
    node["hashtags"] = change.get("hashtags", [])
    node["updated"] = change.get("updated", "")
    node["is_wip"] = bool(change.get("work_in_progress", False))
