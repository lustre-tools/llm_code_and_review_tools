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
        # CR actions by human reviewers on patchsets BEFORE the
        # current one, in chronological order. Each entry is
        # {name, ps, value}; value 0 means the user reset their
        # earlier vote on that ps. The JS panel picks the latest
        # per voter and discards anyone whose latest action was a
        # reset.
        "cr_history": [],
        "unresolved_count": 0,
        "unresolved_comments": [],
    }


_JENKINS_URL_RE = re.compile(
    r"https?://build\.whamcloud\.com/job/([^/]+)/(\d+)/?"
)

# CR-vote action patterns on a message's "Patch Set N:" header line.
# `Code-Review[+-]M` is a vote; `-Code-Review` (no score) is a reset
# back to 0. The reset regex uses a negative lookahead to avoid
# eating "-Code-Review+1" / "-Code-Review-1" forms.
_CR_VOTE_RE = re.compile(r"Code-Review([+\-]\d+)")
_CR_RESET_RE = re.compile(r"-Code-Review(?![+\-\d])")

# Account names that issue automated CR votes (style/CI bots).
# Excluded from the human-reviewer history.
_CR_BOT_VOTERS = frozenset({
    "Lustre Gerrit Janitor",
    "wc-checkpatch",
    "Maloo",
    "jenkins",
    "Autotest",
    "Misc Code Checks Robot (Gatekeeper helper)",
})
_MALOO_DIRECT_RE = re.compile(
    r"https?://testing\.whamcloud\.com/test_sessions/related"
    r"\?jobs=[^&\s]+&builds=\d+#redirect"
)
_MALOO_BUILD_RE = re.compile(r"sessions will be run for Build (\d+)")


def _extract_ci_links(
    messages: list[dict[str, Any]], patchset: int
) -> dict[str, str]:
    """Extract Jenkins build URL and Maloo results URL from change messages.

    Only looks at messages for the given patchset number. The Maloo URL
    must reflect whichever Jenkins job ran the build (e.g.
    `lustre-reviews` for fs/lustre-release, `lustre-b_es-reviews` for
    ex/lustre-release on b_es branches). The Maloo bot's own message
    contains the fully-formed URL with the right `jobs=` parameter, so
    we prefer to extract it verbatim. If only the "Build NNNNN" line is
    present (older message format), we reconstruct the URL using the
    Jenkins job name captured from a Jenkins build link on the same
    patchset, falling back to `lustre-reviews` only when no Jenkins URL
    was seen at all.

    Reviewers can request a retest mid-patchset, which produces a
    second pair of Jenkins+Maloo bot messages on the SAME patchset.
    The newer run is what people actually care about, so we keep
    overwriting and end on whichever match comes last in chronological
    order (Gerrit returns messages oldest-first).
    """
    jenkins_url = ""
    maloo_url = ""
    jenkins_job = ""
    pending_maloo_build = ""

    for msg in messages:
        if msg.get("_revision_number", 0) != patchset:
            continue
        text = msg.get("message", "")

        m = _JENKINS_URL_RE.search(text)
        if m:
            jenkins_url = m.group(0)
            jenkins_job = m.group(1)

        # Maloo state is reset to whichever signal this message
        # carries, so an older direct URL or build-num doesn't bleed
        # through into a newer retest's run.
        m = _MALOO_DIRECT_RE.search(text)
        if m:
            maloo_url = m.group(0)
            pending_maloo_build = ""
        else:
            m = _MALOO_BUILD_RE.search(text)
            if m:
                pending_maloo_build = m.group(1)
                maloo_url = ""

    if not maloo_url and pending_maloo_build:
        job = jenkins_job or "lustre-reviews"
        maloo_url = (
            f"https://testing.whamcloud.com/test_sessions/related"
            f"?jobs={job}&builds={pending_maloo_build}#redirect"
        )

    return {"jenkins_url": jenkins_url, "maloo_url": maloo_url}


def _extract_cr_history(
    messages: list[dict[str, Any]], current_ps: int,
) -> list[dict[str, Any]]:
    """Extract per-patchset Code-Review actions by human reviewers
    from change messages, scoped to patchsets BEFORE current_ps.

    Returns chronologically-ordered entries: {name, ps, value}.
    A positive/negative value is a vote; 0 is a reset (the user
    cleared their prior CR vote). Bot voters and autogenerated
    patchset-upload messages are filtered out. The JS panel
    derives the latest action per voter and discards any voter
    whose latest action was a reset (they retracted, so they
    shouldn't appear as a still-standing previous review).

    The Maloo / Jenkins / Verified votes live on a different
    label and are not extracted here.
    """
    history: list[dict[str, Any]] = []
    for msg in messages:
        ps = msg.get("_revision_number", 0)
        if ps <= 0 or ps >= current_ps:
            continue
        # Autogenerated upload messages carry no vote info and
        # the "Outdated Votes" line they sometimes contain isn't
        # a fresh action — skip them.
        if msg.get("tag") == "autogenerated:gerrit:newPatchSet":
            continue
        author = msg.get("author", {}).get("name", "")
        if not author or author in _CR_BOT_VOTERS:
            continue
        first_line = msg.get("message", "").split("\n", 1)[0]
        if not first_line.startswith("Patch Set "):
            continue

        vote = _CR_VOTE_RE.search(first_line)
        if vote:
            history.append({
                "name": author, "ps": ps,
                "value": int(vote.group(1)),
            })
        elif _CR_RESET_RE.search(first_line):
            history.append({"name": author, "ps": ps, "value": 0})

    return history


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
