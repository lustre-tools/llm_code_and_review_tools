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
    branch: str = "",
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
        # Change owner (uploader) name. Distinct from `author` (the
        # git commit author): Gerrit's self-approval rule keys off
        # the owner, so the review-health logic must too. Backfilled
        # from the change payload in _update_node_meta; "" until then.
        "owner": "",
        # Latest patchset's commit SHA. Backfilled from the change
        # payload's `current_revision` field during the bulk
        # revision fetch — until then, "".
        "current_commit": "",
        "url": f"{base_url}/c/{project}/+/{cn}",
        "ticket": ticket,
        "topic": topic,
        "hashtags": hashtags or [],
        "checkout_cmd": f"{fetch_cmd} && git checkout FETCH_HEAD",
        "cherrypick_cmd": f"{fetch_cmd} && git cherry-pick FETCH_HEAD",
        "updated": updated,
        "is_wip": is_wip,
        "project": project,
        "branch": branch,
    }


def _update_node_meta(node: dict[str, Any], change: dict[str, Any]) -> None:
    """Copy topic/hashtags/updated/wip/project/branch from a change
    payload onto a node. /related entries don't carry branch, so the
    bulk revision fetch is where this info lands."""
    node["topic"] = change.get("topic", "")
    node["hashtags"] = change.get("hashtags", [])
    node["updated"] = change.get("updated", "")
    node["is_wip"] = bool(change.get("work_in_progress", False))
    owner_name = change.get("owner", {}).get("name", "")
    if owner_name:
        node["owner"] = owner_name
    current_commit = change.get("current_revision", "")
    if current_commit:
        node["current_commit"] = current_commit
    if change.get("project"):
        node["project"] = change["project"]
    if change.get("branch"):
        node["branch"] = change["branch"]
