"""Review-state helpers: empty templates, label parsing, CI link
extraction, and unresolved-comment heuristics.

These all operate on raw Gerrit REST payloads and emit the compact
review-info shape that the graph nodes expect. They have no
dependency on the rest of the graph pipeline."""

import re
from typing import Any


def _empty_review() -> dict[str, Any]:
    """Return an empty review info structure."""
    return {
        "verified_votes": [],    # [{name, value}] — all non-zero Verified votes
        "verified_pass": False,  # at least one +1 and no -1
        "verified_fail": False,  # at least one -1
        "cr_votes": [],          # [{name, value}] — all non-zero CR votes
        "cr_approved": False,    # any +2
        "cr_rejected": False,    # Gerrit rejected flag
        "cr_rejected_by": "",
        "cr_veto": False,        # any -1 or -2
        "jenkins_url": "",
        "maloo_url": "",
        "unresolved_count": 0,
        "unresolved_comments": [],
    }


def _extract_ci_links(
    messages: list[dict[str, Any]], patchset: int
) -> dict[str, str]:
    """Extract Jenkins build URL and Maloo results URL from change messages.

    Only looks at messages for the given patchset number.
    """
    jenkins_url = ""
    maloo_url = ""

    for msg in messages:
        if msg.get("_revision_number", 0) != patchset:
            continue
        text = msg.get("message", "")

        # Jenkins: look for build.whamcloud.com URL
        if not jenkins_url:
            m = re.search(
                r"(https?://build\.whamcloud\.com/job/[^/]+/\d+/?)", text
            )
            if m:
                jenkins_url = m.group(1)

        # Maloo: look for "sessions will be run for Build NNNNN"
        # to construct the results overview link
        if not maloo_url:
            m = re.search(
                r"sessions will be run for Build (\d+)", text
            )
            if m:
                build_num = m.group(1)
                maloo_url = (
                    f"https://testing.whamcloud.com/test_sessions/related"
                    f"?jobs=lustre-reviews&builds={build_num}#redirect"
                )

    return {"jenkins_url": jenkins_url, "maloo_url": maloo_url}


def _extract_unresolved_comments(
    client: Any,
    cn: int,
    expected_count: int = -1,
) -> list[dict[str, Any]]:
    """Extract unresolved comments using multi-source heuristics.

    Gerrit's unresolved_comment_count is authoritative but opaque — its
    resolution logic (code-change-based, porting) isn't fully exposed via
    any single API, and the per-comment `unresolved` field is unreliable
    (especially for PATCHSET_LEVEL comments posted with votes).

    Strategy:
    1. Raw thread analysis: threads where last comment has unresolved=True
    2. Subtract threads that ported_comments confirms as resolved
    3. If still short of expected_count, supplement with recent human
       comments on the current patchset (Gerrit may track these as
       unresolved despite the API field saying False)

    Results are capped at expected_count (from unresolved_comment_count).
    """
    try:
        raw = client.rest.get(f"/changes/{cn}/comments")
    except Exception:
        return []

    # Get current patchset number
    current_ps = 0
    try:
        detail = client.rest.get(f"/changes/{cn}?o=CURRENT_REVISION")
        for rev_info in detail.get("revisions", {}).values():
            current_ps = rev_info.get("_number", 0)
    except Exception:
        pass

    # Flatten all comments with file path
    all_comments: list[dict[str, Any]] = []
    for filepath, file_comments in raw.items():
        for c in file_comments:
            c["_file"] = filepath
            all_comments.append(c)

    by_id = {c.get("id", ""): c for c in all_comments}

    # Build threads: group by root comment
    threads: dict[str, list[dict[str, Any]]] = {}
    for c in all_comments:
        root = c
        visited: set[str] = set()
        while root.get("in_reply_to") and root["in_reply_to"] in by_id:
            if root["in_reply_to"] in visited:
                break
            visited.add(root.get("id", ""))
            root = by_id[root["in_reply_to"]]
        threads.setdefault(root.get("id", ""), []).append(c)

    bot_names = {"wc-checkpatch", "Lustre Gerrit Janitor", "jenkins",
                 "Maloo", "Autotest",
                 "Misc Code Checks Robot (Gatekeeper helper)"}

    def _make_item(root: dict[str, Any]) -> dict[str, Any]:
        return {
            "file": root.get("_file", ""),
            "line": root.get("line", 0),
            "author": root.get("author", {}).get("name", "?"),
            "message": root.get("message", "")[:200],
            "patch_set": root.get("patch_set", 0),
            "id": root.get("id", ""),
        }

    # Primary: raw thread analysis — threads where last comment has
    # unresolved=True. Ranked: current-patchset first, then older.
    primary: list[tuple[int, dict[str, Any]]] = []
    seen_root_ids: set[str] = set()

    for root_id, thread_comments in threads.items():
        thread_comments.sort(key=lambda x: x.get("updated", ""))
        last = thread_comments[-1]
        if not last.get("unresolved", False):
            continue

        root = by_id.get(root_id, thread_comments[0])
        seen_root_ids.add(root_id)
        max_ps = max(c.get("patch_set", 0) for c in thread_comments)
        rank = 0 if max_ps == current_ps else 1
        primary.append((rank, _make_item(root)))

    primary.sort(key=lambda x: (x[0], x[1]["file"], x[1]["line"]))
    items = [p[1] for p in primary]

    # When raw analysis finds MORE candidates than expected_count,
    # use ported_comments to identify which old-patchset threads
    # Gerrit considers resolved (via code changes). Remove those
    # to get closer to the true set. Only applied when we have
    # excess — when raw matches or undershoots expected_count,
    # ported is too unreliable (it sometimes resolves threads
    # that Gerrit still counts as unresolved).
    if expected_count >= 0 and len(items) > expected_count:
        ported_resolved_ids: set[str] = set()
        try:
            ported = client.rest.get(
                f"/changes/{cn}/revisions/current/ported_comments"
            )
            ported_flat: list[dict[str, Any]] = []
            for filepath, file_comments in ported.items():
                for c in file_comments:
                    c["_file"] = filepath
                    ported_flat.append(c)

            ported_by_id = {c.get("id", ""): c for c in ported_flat}
            ported_threads: dict[str, list[dict[str, Any]]] = {}
            for c in ported_flat:
                root = c
                visited: set[str] = set()
                while (root.get("in_reply_to")
                       and root["in_reply_to"] in ported_by_id):
                    if root["in_reply_to"] in visited:
                        break
                    visited.add(root.get("id", ""))
                    root = ported_by_id[root["in_reply_to"]]
                ported_threads.setdefault(
                    root.get("id", ""), []
                ).append(c)

            for root_id, thread in ported_threads.items():
                thread.sort(key=lambda x: x.get("updated", ""))
                if not thread[-1].get("unresolved", False):
                    ported_resolved_ids.add(root_id)
        except Exception:
            pass

        if ported_resolved_ids:
            items = [it for it in items
                     if it["id"] not in ported_resolved_ids]

    # Fallback: when raw analysis (after optional ported filtering)
    # finds ZERO candidates but expected_count > 0, supplement with
    # recent current-patchset human comments. Handles a Gerrit API
    # bug where PATCHSET_LEVEL comments posted with votes have
    # unresolved=False in the API but are counted as unresolved.
    if expected_count > 0 and len(items) == 0:
        for root_id, thread_comments in threads.items():
            if root_id in seen_root_ids:
                continue
            root = by_id.get(root_id, thread_comments[0])
            if root.get("patch_set", 0) != current_ps:
                continue
            author = root.get("author", {}).get("name", "")
            if author in bot_names:
                continue
            items.append(_make_item(root))
        items.sort(key=lambda x: x.get("id", ""), reverse=True)

    # Cap at expected_count if provided
    if expected_count >= 0:
        items = items[:expected_count]

    return items


def _parse_labels(labels: dict[str, Any]) -> dict[str, Any]:
    """Parse Gerrit DETAILED_LABELS into compact review info."""
    result = _empty_review()

    # Verified label — track ALL voters, not just Jenkins/Maloo
    verified = labels.get("Verified", {})
    has_plus = False
    has_minus = False
    for vote in verified.get("all", []):
        val = vote.get("value", 0)
        if val == 0:
            continue
        name = vote.get("name", f"account:{vote.get('_account_id', '?')}")
        result["verified_votes"].append({"name": name, "value": val})
        if val > 0:
            has_plus = True
        if val < 0:
            has_minus = True

    result["verified_pass"] = has_plus and not has_minus
    result["verified_fail"] = has_minus

    # Code-Review label
    cr = labels.get("Code-Review", {})
    for vote in cr.get("all", []):
        val = vote.get("value", 0)
        if val == 0:
            continue
        name = vote.get("name", f"account:{vote.get('_account_id', '?')}")
        result["cr_votes"].append({"name": name, "value": val})
        if val <= -1:
            result["cr_veto"] = True

    if cr.get("approved"):
        result["cr_approved"] = True
    if cr.get("rejected"):
        result["cr_rejected"] = True
        result["cr_rejected_by"] = cr["rejected"].get("name", "")

    # Sort CR votes: negative first (most concerning), then positive
    result["cr_votes"].sort(key=lambda v: (v["value"] > 0, abs(v["value"])))

    return result
