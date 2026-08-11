"""Main build_graph orchestrator.

Pulls together:
- /related for the anchor's main series
- ALL_REVISIONS + ALL_COMMITS for edge reconstruction (incl. stale)
- commit-parent-based discovery of changes dropped from /related
- topic/hashtag expansion into separate-series trees
- cycle breaking on the final edge set

The returned dict is the shape expected by `render.generate_html`.

`build_graph` is a thin orchestrator: each numbered step is delegated
to a dedicated helper that operates on a shared `BuildContext`.
Helpers mutate `ctx` in place — this mirrors how the original 600-line
function passed state through locals, but with explicit boundaries and
each step now readable on its own."""

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..client import GerritCommentsClient
from .edges import _break_cycles, _collect_revisions
from .nodes import _make_node, _update_node_meta, subject_ticket
from .review import (
    _empty_review,
    _extract_ci_links,
    _extract_cr_history,
    _extract_unresolved_comments,
    _parse_labels,
)

_DEFAULT_PROJECT = "fs/lustre-release"
_BATCH_SIZE = 50
_BATCH_SIZE_WITH_COMMITS = 10
_DISCOVERY_BATCH_SIZE = 30
_MESSAGES_BATCH_SIZE = 20

# Hashtags that are conventional/lifecycle markers rather than
# series identifiers. Even when the anchor carries them, they're
# skipped by the auto-derived hashtag fanout — every patch queued
# for the next master merge has "master-next", and "mw" is the
# equivalent marker for the b_es backport queue. Using either as
# a series identifier would pull in hundreds of unrelated changes.
# Users can still force-include them with `--include-hashtag`.
_LIFECYCLE_HASHTAGS = frozenset({"master-next", "mw"})

# Number of pipeline phases printed by build_graph. Must match the
# number of `logger.start(...)` calls in build_graph so the [N/total]
# prefix counts correctly.
_TOTAL_PHASES = 8


# ─── Phase logger ───────────────────────────────────────────────────────


class PhaseLogger:
    """Pretty phase-by-phase progress printer.

    Usage:
        logger.header("gerrit-cli graph / 63677")
        logger.start("Fetching /related")
        ... work ...
        logger.done("35 changes")
        ...
        logger.summary("93 nodes · 88 edges · 6 separate series")

    On a tty: colored label, dot-filled alignment, and `\\r` overwrite
    so an "in-progress" line becomes the final line when done. On a
    non-tty: plain one-line-per-phase output that's safe to redirect
    or pipe. When `enabled=False`, all methods are no-ops."""

    LABEL_WIDTH = 42  # target column where the result column starts

    def __init__(self, total: int, *, enabled: bool = True) -> None:
        self.total = total
        self.enabled = enabled
        self.is_tty = enabled and sys.stderr.isatty()
        self.n = 0
        self.t_overall = time.monotonic()
        self.t_phase = 0.0
        self.label = ""
        # ANSI styles (only when color makes sense).
        if self.is_tty:
            self.c_cyan = "\033[36m"
            self.c_green = "\033[32m"
            self.c_yellow = "\033[33m"
            self.c_dim = "\033[2m"
            self.c_bold = "\033[1m"
            self.c_reset = "\033[0m"
        else:
            self.c_cyan = self.c_green = self.c_yellow = ""
            self.c_dim = self.c_bold = self.c_reset = ""

    # ── internals ────────────────────────────────────────────────────

    def _fmt_label(self) -> str:
        pad = max(3, self.LABEL_WIDTH - len(self.label))
        dots = self.c_dim + ("." * pad) + self.c_reset
        return f"{self.c_cyan}{self.label}{self.c_reset} {dots}"

    def _prefix(self) -> str:
        return f"[{self.n:>2}/{self.total}]"

    def _overwrite(self, text: str, newline: bool) -> None:
        if self.is_tty:
            sys.stderr.write("\r\033[K" + text + ("\n" if newline else ""))
            sys.stderr.flush()
        elif newline:
            sys.stderr.write(text + "\n")
            sys.stderr.flush()

    # ── public API ───────────────────────────────────────────────────

    def header(self, text: str) -> None:
        if not self.enabled:
            return
        sys.stderr.write(f"\n{self.c_bold}{text}{self.c_reset}\n\n")
        sys.stderr.flush()

    def start(self, label: str) -> None:
        if not self.enabled:
            return
        self.n += 1
        self.t_phase = time.monotonic()
        self.label = label
        self._overwrite(
            f"{self._prefix()}    ·    {self._fmt_label()} running",
            newline=False,
        )

    def done(self, result: str) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self.t_phase
        elapsed_str = f"{elapsed:>5.1f}s"
        self._overwrite(
            f"{self._prefix()} {elapsed_str} {self._fmt_label()} {result}",
            newline=True,
        )

    def note(self, text: str) -> None:
        """Print a sub-step note (e.g. batch-N/M) while a phase is
        running. On a tty the current "running" line is overwritten
        with the note, then restored by the next start/done call.
        On a non-tty these become plain dimmed lines."""
        if not self.enabled:
            return
        styled = f"{self.c_dim}    · {text}{self.c_reset}"
        if self.is_tty:
            # Overwrite the running line with the note, then re-draw
            # the running line so the phase is still visible.
            sys.stderr.write("\r\033[K" + styled + "\n")
            sys.stderr.write(
                f"{self._prefix()}    ·    {self._fmt_label()} running"
            )
            sys.stderr.flush()
        else:
            sys.stderr.write(styled + "\n")
            sys.stderr.flush()

    def warn(self, text: str) -> None:
        if not self.enabled:
            return
        styled = f"{self.c_yellow}    ⚠ {text}{self.c_reset}"
        if self.is_tty:
            sys.stderr.write("\r\033[K" + styled + "\n")
            sys.stderr.write(
                f"{self._prefix()}    ·    {self._fmt_label()} running"
            )
            sys.stderr.flush()
        else:
            sys.stderr.write(styled + "\n")
            sys.stderr.flush()

    def summary(self, text: str) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self.t_overall
        sys.stderr.write(
            f"\n{self.c_green}✓{self.c_reset} {text} "
            f"{self.c_dim}· {elapsed:.1f}s total{self.c_reset}\n"
        )
        sys.stderr.flush()


# ─── Build context ──────────────────────────────────────────────────────


@dataclass
class BuildContext:
    """Mutable state threaded through the build pipeline.

    Created in `build_graph` and progressively filled in by each
    helper. At the end of the pipeline, `_assemble_payload` reads
    everything here to produce the final JSON blob."""

    client: GerritCommentsClient
    change_number: int
    base_url: str
    progress: bool
    fetch_details: bool
    fetch_comments: bool
    include_topic: bool
    include_hashtag: bool
    extra_topics: list[str]
    extra_hashtags: list[str]
    # Ticket-based expansion (subject-leading JIRA ids, e.g.
    # LU-18222). Behaves like extra_hashtags — one search label per
    # ticket — but matches are filtered to changes whose SUBJECT
    # starts with the ticket, so mere mentions don't get pulled in.
    extra_tickets: list[str]
    # When False (the default), search-based expansion (topic/hashtag
    # and commit-parent discovery) is restricted to changes in the
    # same project AND branch as the anchor. When True, results from
    # any project/branch on the same Gerrit host are pulled in
    # (the original permissive behavior).
    cross_project_branch: bool = False
    logger: "PhaseLogger | None" = None

    # Resolved from the anchor change during step 1.
    project: str = _DEFAULT_PROJECT
    branch: str = "master"

    # Accumulated during the pipeline.
    nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    raw_entries: list[dict[str, Any]] = field(default_factory=list)
    commit_to_change_ps: dict[str, tuple[int, int]] = field(default_factory=dict)
    revision_parents: dict[str, str] = field(default_factory=dict)
    labels_by_cn: dict[int, dict[str, Any]] = field(default_factory=dict)
    comment_count_by_cn: dict[int, int] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    # Tracks every (from, to) pair already added to `edges` across all
    # stages (main + separate-series internal + cross-group). Makes
    # duplicate-edge suppression a global invariant instead of
    # something each helper has to reimplement locally.
    seen_edges: set[tuple[int, int]] = field(default_factory=set)
    separate_groups: list[dict[str, Any]] = field(default_factory=list)
    # `submitted` dates for merged changes discovered as ancestor
    # parent commits but dropped from the visible graph by
    # _filter_merged_ancestors. Kept so the trunk edge-redirect
    # step can still resolve an in-flight patch's true upload-time
    # master position (the parent commit is owned by one of these
    # off-tree merged changes; we need its date to pick the right
    # trunk node).
    external_merged_submitted: dict[int, str] = field(default_factory=dict)

    def log(self, msg: str, end: str = "\n") -> None:
        """Legacy plain-text logger retained for places that don't
        fit the phase model (e.g. per-batch error reports)."""
        if self.progress:
            if self.logger is not None:
                self.logger.note(msg)
            else:
                print(msg, end=end, file=sys.stderr, flush=True)


# ─── Step helpers ───────────────────────────────────────────────────────


def _matches_anchor_scope(
    ctx: BuildContext, change: dict[str, Any],
) -> bool:
    """Return True if a Gerrit change payload is in the same project
    AND branch as the anchor — used to gate search-based expansion
    (topic / hashtag / commit-parent discovery) so untrusted callers
    don't pull in patches from other repos or branches that the user
    might not want to expose. Bypassed when ctx.cross_project_branch
    is True."""
    if ctx.cross_project_branch:
        return True
    if change.get("project") != ctx.project:
        return False
    if change.get("branch") != ctx.branch:
        return False
    return True


def _resolve_project(ctx: BuildContext) -> None:
    """Resolve the Gerrit project AND branch for the anchor change.
    The project drives URLs and git-fetch refs; the branch is used
    (together with the project) to scope search-based expansion to
    the same upstream as the anchor unless `cross_project_branch`
    is set."""
    try:
        anchor = ctx.client.rest.get(f"/changes/{ctx.change_number}")
        ctx.project = anchor.get("project", _DEFAULT_PROJECT)
        ctx.branch = anchor.get("branch", "master")
    except Exception:
        ctx.project = _DEFAULT_PROJECT
        ctx.branch = "master"


def _fetch_related(ctx: BuildContext) -> list[dict[str, Any]]:
    """Fetch the Gerrit /related entries for the anchor change."""
    response = ctx.client.rest.get(
        f"/changes/{ctx.change_number}/revisions/current/related"
    )
    return response.get("changes", [])


def _parse_related_entries(
    ctx: BuildContext, entries: list[dict[str, Any]],
) -> int:
    """Turn /related entries into nodes + raw_entries (the skeleton
    used later to build the core chain edges). Changes already in
    ctx.nodes are skipped so this helper is safe to call repeatedly
    with overlapping results (see _expand_via_related_fanout).
    Returns the number of genuinely new changes added."""
    added = 0
    for entry in entries:
        cn = entry.get("_change_number", 0)
        if not cn or cn in ctx.nodes:
            continue
        ci = entry.get("commit", {})
        commit_hash = ci.get("commit", "")
        parents = ci.get("parents", [])
        parent_hash = parents[0].get("commit", "") if parents else ""
        author_info = ci.get("author", {})
        ps = entry.get("_revision_number", 0)
        latest = entry.get("_current_revision_number", 0)
        status = entry.get("status", "UNKNOWN")
        subject = ci.get("subject", "")

        # /related entries don't include a branch field; assume the
        # anchor's branch (Gerrit's /related is commit-graph based,
        # so cross-branch entries here would be highly unusual). The
        # bulk revision fetch later will overwrite if Gerrit returns
        # something different.
        ctx.nodes[cn] = _make_node(
            cn, subject, status, latest,
            author_info.get("name", "Unknown"), ctx.base_url,
            project=ctx.project,
            branch=ctx.branch,
        )
        ctx.raw_entries.append({
            "cn": cn,
            "commit": commit_hash,
            "parent_commit": parent_hash,
            "ps": ps,
            "latest": latest,
        })
        added += 1
    return added


def _expand_via_related_fanout(ctx: BuildContext) -> int:
    """Gerrit's /related is asymmetric: the chain Gerrit returns when
    queried from the anchor may omit side branches that are visible
    only from other changes in the series (e.g. a descendant patch
    whose own /related includes the anchor, but whose change number
    never shows up in the anchor's /related).

    Fix by calling /related on every change in the initial set and
    merging any new members back in — a single pass, only over the
    anchor's own /related members. We deliberately do NOT recurse
    into newly-discovered nodes: each extra level of recursion would
    bridge into unrelated historical series via old shared commits
    (e.g. a change that shares an ancestor with one of our nodes
    would pull in its entire sibling series). The single pass is
    enough to patch Gerrit's asymmetry without leaking history.

    Returns the number of newly-added changes."""
    pending = sorted(ctx.nodes.keys())
    if not pending:
        return 0
    added_total = 0
    for cn in pending:
        try:
            resp = ctx.client.rest.get(
                f"/changes/{cn}/revisions/current/related"
            )
        except Exception:
            continue
        added_total += _parse_related_entries(
            ctx, resp.get("changes", [])
        )
    return added_total


def _fetch_revisions_batch(
    ctx: BuildContext, cns: list[int], *, collect_parents: bool = False,
) -> None:
    """Fetch ALL_REVISIONS for a batch of changes.

    If `collect_parents` is True, also request ALL_COMMITS so parent
    commit hashes are recorded in `ctx.revision_parents`. That's only
    used for the initial /related set (and later for discovered
    changes) to avoid unbounded history expansion."""
    opts = "&o=ALL_REVISIONS&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS"
    if collect_parents:
        opts += "&o=ALL_COMMITS"
    # ALL_COMMITS returns much more data per change, so use a smaller
    # batch size to avoid connection errors.
    bs = _BATCH_SIZE_WITH_COMMITS if collect_parents else _BATCH_SIZE
    batches = [cns[i:i + bs] for i in range(0, len(cns), bs)]
    for batch_idx, batch in enumerate(batches):
        query = " OR ".join(f"change:{cn}" for cn in batch)
        try:
            result = ctx.client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}{opts}&n=500"
            )
            for change in result:
                cn = change.get("_number", 0)
                _collect_revisions(
                    change, ctx.commit_to_change_ps,
                    ctx.revision_parents if collect_parents else None,
                )
                ctx.labels_by_cn[cn] = _parse_labels(
                    change.get("labels", {})
                )
                ctx.comment_count_by_cn[cn] = change.get(
                    "unresolved_comment_count", 0
                )
                if cn in ctx.nodes:
                    _update_node_meta(ctx.nodes[cn], change)
        except Exception as e:
            ctx.log(f" (batch {batch_idx} error: {e})", end="")


def _fetch_initial_revisions(ctx: BuildContext) -> None:
    """Fetch revision history for the initial /related set, with
    parent-commit collection enabled so stale branches can be
    reconstructed later."""
    all_cns = sorted(ctx.nodes.keys())
    _fetch_revisions_batch(ctx, all_cns, collect_parents=True)


def _discover_missing_nodes(ctx: BuildContext) -> int:
    """Find changes that an old-patchset parent commit refers to but
    that weren't returned by /related. Search Gerrit for each such
    commit, pull the owning change in, and fetch its revisions so
    one more level of connections can be resolved.

    Operates only on the parent commits already collected from the
    initial /related set, so it stays bounded. Returns the number of
    newly-discovered changes."""
    unresolved: set[str] = set()
    for _child_hash, parent_hash in ctx.revision_parents.items():
        if parent_hash and parent_hash not in ctx.commit_to_change_ps:
            unresolved.add(parent_hash)

    if not unresolved:
        return 0

    discovered_cns: set[int] = set()
    unresolved_list = sorted(unresolved)
    batches = [
        unresolved_list[i:i + _DISCOVERY_BATCH_SIZE]
        for i in range(0, len(unresolved_list), _DISCOVERY_BATCH_SIZE)
    ]
    for batch in batches:
        query = " OR ".join(f"commit:{h}" for h in batch)
        try:
            result = ctx.client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}&n=500"
            )
            for change in result:
                cn = change.get("_number", 0)
                if not cn or cn in ctx.nodes:
                    continue
                if not _matches_anchor_scope(ctx, change):
                    continue
                discovered_cns.add(cn)
                ctx.nodes[cn] = _make_node(
                    cn, change.get("subject", ""),
                    change.get("status", "UNKNOWN"),
                    change.get("_current_revision_number", 1),
                    change.get("owner", {}).get("name", "Unknown"),
                    ctx.base_url,
                    topic=change.get("topic", ""),
                    hashtags=change.get("hashtags", []),
                    updated=change.get("updated", ""),
                    is_wip=bool(change.get("work_in_progress", False)),
                    project=change.get("project", ctx.project),
                    branch=change.get("branch", ctx.branch),
                )
        except Exception:
            pass

    if not discovered_cns:
        return 0

    _fetch_revisions_batch(ctx, sorted(discovered_cns))
    return len(discovered_cns)


def _filter_merged_ancestors(ctx: BuildContext) -> int:
    """Drop discovered changes that are already MERGED — those are
    git ancestors on lustre-master, not part of the actual patch
    series we care about. Returns the number of removed changes.

    Before deletion, the submitted timestamp of each removed change
    is stashed in ctx.external_merged_submitted. The trunk edge-
    redirect step uses it to date an in-flight patch's true base
    commit when that base lives in one of these off-tree ancestors.
    """
    related_set = {e["cn"] for e in ctx.raw_entries}
    merged_discovered = [
        cn for cn in ctx.nodes
        if cn not in related_set and ctx.nodes[cn]["status"] == "MERGED"
    ]
    for cn in merged_discovered:
        sub = ctx.nodes[cn].get("submitted", "")
        if sub:
            ctx.external_merged_submitted[cn] = sub
        del ctx.nodes[cn]
    return len(merged_discovered)


def _attach_review_info(ctx: BuildContext) -> None:
    """Copy the parsed labels + comment count onto each node's
    ``review`` field."""
    for cn, node in ctx.nodes.items():
        review = ctx.labels_by_cn.get(cn, _empty_review())
        review["unresolved_count"] = ctx.comment_count_by_cn.get(cn, 0)
        node["review"] = review


def _fetch_ci_and_comments(
    ctx: BuildContext, cns: list[int] | None = None,
) -> int:
    """Attach CI links (from change messages) and, when requested,
    detailed unresolved comments. Only non-abandoned changes are
    queried — abandoned patches carry no useful extra detail.

    When `cns` is None, every active node is processed (the main
    pass). Pass an explicit cn list to backfill a subset — used for
    separate-group nodes, which are added to ctx.nodes after the
    main pass and would otherwise have no Jenkins/Maloo links.
    Returns the number of active changes that were processed."""
    if not ctx.fetch_details:
        return 0
    candidates = ctx.nodes.keys() if cns is None else cns
    active_cns = sorted(
        cn for cn in candidates
        if cn in ctx.nodes and ctx.nodes[cn]["status"] != "ABANDONED"
    )
    if not active_cns:
        return 0

    # Batch-fetch messages for CI links and prior-patchset
    # Code-Review history. DETAILED_ACCOUNTS ensures message
    # authors carry a name we can match against owner/author and
    # the bot exclusion list.
    msg_batches = [
        active_cns[i:i + _MESSAGES_BATCH_SIZE]
        for i in range(0, len(active_cns), _MESSAGES_BATCH_SIZE)
    ]
    for batch in msg_batches:
        query = " OR ".join(f"change:{cn}" for cn in batch)
        try:
            result = ctx.client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}"
                "&o=MESSAGES&o=DETAILED_ACCOUNTS&n=500"
            )
            for change in result:
                cn = change.get("_number", 0)
                if cn not in ctx.nodes:
                    continue
                latest_ps = ctx.nodes[cn]["current_patchset"]
                msgs = change.get("messages", [])
                links = _extract_ci_links(msgs, latest_ps)
                ctx.nodes[cn]["review"]["jenkins_url"] = links.get(
                    "jenkins_url", ""
                )
                ctx.nodes[cn]["review"]["maloo_url"] = links.get(
                    "maloo_url", ""
                )
                ctx.nodes[cn]["review"]["cr_history"] = (
                    _extract_cr_history(msgs, latest_ps)
                )
        except Exception:
            pass

    # Fetch inline comments per change — slow, opt-in. Uses
    # confidence-ranked thread analysis capped at
    # unresolved_comment_count.
    if ctx.fetch_comments:
        for cn in active_cns:
            try:
                expected = ctx.nodes[cn]["review"].get("unresolved_count", -1)
                ctx.nodes[cn]["review"]["unresolved_comments"] = (
                    _extract_unresolved_comments(ctx.client, cn, expected)
                )
            except Exception:
                pass

    return len(active_cns)


def _build_main_edges(ctx: BuildContext) -> int:
    """Produce edges for the main series.

    For each node, the primary edge to its parent is derived from
    the CURRENT patchset's parent commit (via revision_parents),
    not from /related's view. /related is anchor-dependent: when
    queried from a merged anchor, it returns the patchset
    relationship that was current at merge time, so an in-flight
    descendant that has since been reordered shows a stale parent.
    Using the current patchset's parent makes the chain
    anchor-independent — the layout is identical regardless of
    which change in the series is the anchor.

    The /related view is kept as a fallback for nodes whose
    revisions weren't fetched. revision_parents (all patchsets'
    parent commits) still drives discovered-node and stale-edge
    detection in the second pass. Cycles get removed as a final
    step. Returns the number of cycle edges removed."""

    # Reverse-index commit_to_change_ps to find each node's current
    # commit hash. We need this to look up the current patchset's
    # parent in revision_parents.
    cn_current_commit: dict[int, str] = {}
    for h, (cn, ps) in ctx.commit_to_change_ps.items():
        if cn in ctx.nodes and ps == ctx.nodes[cn].get("current_patchset"):
            cn_current_commit[cn] = h

    def add_edge(parent_cn: int, child_cn: int, parent_ps: int) -> None:
        if parent_cn == child_cn:
            return
        if parent_cn not in ctx.nodes or child_cn not in ctx.nodes:
            return
        key = (parent_cn, child_cn)
        if key in ctx.seen_edges:
            return
        ctx.seen_edges.add(key)
        parent_latest = ctx.nodes[parent_cn]["current_patchset"]
        ctx.edges.append({
            "from": parent_cn,
            "to": child_cn,
            "parent_patchset": parent_ps,
            "parent_latest": parent_latest,
            "is_stale": parent_ps < parent_latest,
        })

    # One primary parent edge per node, derived from the node's
    # current patchset. raw_entries is iterated only to enumerate
    # the relevant cns. When the current-patchset parent maps to a
    # change that isn't in our node pool — typically an in-flight
    # node rebased onto a master commit owned by a filtered-out
    # merged-ancestor change — fall back to the /related historical
    # view so the boundary link to a merged ancestor that IS in
    # the pool is preserved as a stale edge. Without the fallback,
    # a merged change in the chain (head OR mid-series) whose
    # in-flight follower has since rebased away would be silently
    # orphaned from its own series.
    def _resolve(commit_hash: str) -> tuple[int, int] | None:
        info = ctx.commit_to_change_ps.get(commit_hash)
        if not info or info[0] not in ctx.nodes:
            return None
        return info

    processed: set[int] = set()
    for entry in ctx.raw_entries:
        cn = entry["cn"]
        if cn in processed:
            continue
        processed.add(cn)
        current_h = cn_current_commit.get(cn)
        parent_commit = (
            ctx.revision_parents.get(current_h, "") if current_h else ""
        )
        resolved = _resolve(parent_commit)
        if resolved is None:
            resolved = _resolve(entry["parent_commit"])
        if resolved is None:
            continue
        parent_cn, parent_ps = resolved
        add_edge(parent_cn, cn, parent_ps)

    # Edges from revision parents — only where at least one endpoint
    # is a discovered change (not in the /related set). This hooks
    # discovered nodes back onto the graph without adding cross-
    # connections between /related changes from old patchset history.
    related_cns = {e["cn"] for e in ctx.raw_entries}
    for child_hash, parent_hash in ctx.revision_parents.items():
        if not parent_hash:
            continue
        if child_hash not in ctx.commit_to_change_ps:
            continue
        if parent_hash not in ctx.commit_to_change_ps:
            continue
        child_cn, _child_ps = ctx.commit_to_change_ps[child_hash]
        parent_cn, parent_ps = ctx.commit_to_change_ps[parent_hash]
        if child_cn in related_cns and parent_cn in related_cns:
            continue
        add_edge(parent_cn, child_cn, parent_ps)

    return _break_cycles(ctx.edges)


def _tag_main_group(ctx: BuildContext) -> None:
    """Mark every main-series node with series_group 0 so separate-
    series expansion can leave it alone."""
    for n in ctx.nodes.values():
        n["series_group"] = 0


# ─── Separate-series expansion ──────────────────────────────────────────


def _collect_search_labels(
    ctx: BuildContext,
) -> list[tuple[str, str]]:
    """Build the list of (query, label) pairs that drive separate-
    series expansion — the anchor's own topic/hashtag plus any extras
    the caller asked for, deduplicated while preserving order."""
    anchor_topic = ctx.nodes.get(ctx.change_number, {}).get("topic", "")
    anchor_hashtags = (
        ctx.nodes.get(ctx.change_number, {}).get("hashtags", []) or []
    )

    topics: list[str] = []
    if ctx.include_topic and anchor_topic:
        topics.append(anchor_topic)
    topics.extend(ctx.extra_topics)

    hashtags: list[str] = []
    if ctx.include_hashtag:
        # Drop lifecycle markers (e.g. "master-next") from the auto-
        # derived list — they're carried by every patch queued for
        # the next merge and would pull in unrelated series.
        hashtags.extend(
            h for h in anchor_hashtags if h not in _LIFECYCLE_HASHTAGS
        )
    # User-supplied --include-hashtag still wins; opting in to
    # "master-next" explicitly is allowed.
    hashtags.extend(ctx.extra_hashtags)

    search_labels: list[tuple[str, str]] = []
    seen_t: set[str] = set()
    for t in topics:
        if t and t not in seen_t:
            seen_t.add(t)
            search_labels.append((f"topic:{t}", f"topic {t}"))
    seen_h: set[str] = set()
    for h in hashtags:
        if h and h not in seen_h:
            seen_h.add(h)
            search_labels.append((f"hashtag:{h}", f"hashtag {h}"))
    # Ticket expansion: Gerrit has no first-class ticket operator,
    # so search the commit message and rely on the subject-ticket
    # filter in _expand_separate_series to drop mere mentions.
    seen_k: set[str] = set()
    for t in ctx.extra_tickets:
        if t and t not in seen_k:
            seen_k.add(t)
            search_labels.append((f'message:"{t}"', f"ticket {t}"))
    return search_labels


def _build_separate_group(
    ctx: BuildContext, seed_cns: list[int], label: str,
) -> None:
    """Build one separate-series group from a set of seed change
    numbers. Seeds already in the main series are skipped; the rest
    get their own /related fetch, edges, and (optionally) cross-
    group stale links back to main."""
    main_cns = set(ctx.nodes.keys())
    seeds_new = [cn for cn in seed_cns if cn not in main_cns]
    if not seeds_new:
        return

    placed: set[int] = set()
    for seed in seeds_new:
        if seed in placed:
            continue

        group_nodes, group_raw = _fetch_group_seed_related(
            ctx, seed, main_cns
        )

        if not group_nodes:
            # Seed had no related or they were all in main — create
            # a single-node group for just this seed.
            single = _fetch_single_change(ctx, seed)
            if single is None:
                continue
            group_nodes[seed] = single

        group_ctps, group_rev_parents = _fetch_group_revisions(
            ctx, group_nodes
        )

        group_edges = _group_internal_edges(
            ctx, group_raw, group_ctps, group_nodes
        )
        group_edges.extend(
            _group_cross_edges(ctx, group_ctps, group_rev_parents, group_nodes)
        )

        group_id = len(ctx.separate_groups) + 1
        group_label = f"{label}: {min(group_nodes.keys())}"
        for cn, node in group_nodes.items():
            node["series_group"] = group_id
            node["review"] = node.get("review") or _empty_review()
            ctx.nodes[cn] = node
            placed.add(cn)
        ctx.edges.extend(group_edges)
        ctx.separate_groups.append({
            "id": group_id,
            "label": group_label,
            "node_ids": sorted(group_nodes.keys()),
        })


def _fetch_group_seed_related(
    ctx: BuildContext, seed: int, main_cns: set[int],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Fetch /related for a seed, parse the entries into group-local
    nodes and raw_entries, skipping anything already in main."""
    try:
        resp = ctx.client.rest.get(
            f"/changes/{seed}/revisions/current/related"
        )
        rel_entries = resp.get("changes", [])
    except Exception:
        rel_entries = []

    group_nodes: dict[int, dict[str, Any]] = {}
    group_raw: list[dict[str, Any]] = []
    for entry in rel_entries:
        ci = entry.get("commit", {})
        commit_hash = ci.get("commit", "")
        parents = ci.get("parents", [])
        parent_hash = parents[0].get("commit", "") if parents else ""
        cn = entry.get("_change_number", 0)
        if not cn or cn in main_cns:
            continue
        latest = entry.get("_current_revision_number", 0) or 1
        status = entry.get("status", "UNKNOWN")
        subject = ci.get("subject", "")
        author = ci.get("author", {}).get("name", "Unknown")
        group_nodes[cn] = _make_node(
            cn, subject, status, latest, author, ctx.base_url,
            project=ctx.project,
            branch=ctx.branch,
        )
        group_raw.append({
            "cn": cn,
            "parent_commit": parent_hash,
            "commit": commit_hash,
        })
    return group_nodes, group_raw


def _fetch_single_change(
    ctx: BuildContext, seed: int,
) -> dict[str, Any] | None:
    """Fallback when a seed has no /related entries: build a node
    from a single CURRENT_REVISION/CURRENT_COMMIT fetch."""
    if seed in ctx.nodes:
        return None
    try:
        result = ctx.client.rest.get(
            f"/changes/?q=change:{seed}"
            "&o=CURRENT_REVISION&o=CURRENT_COMMIT"
        )
    except Exception:
        return None
    if not result:
        return None
    ch = result[0]
    return _make_node(
        seed,
        ch.get("subject", ""),
        ch.get("status", "UNKNOWN"),
        ch.get("_current_revision_number", 1),
        ch.get("owner", {}).get("name", "Unknown"),
        ctx.base_url,
        topic=ch.get("topic", ""),
        hashtags=ch.get("hashtags", []),
        updated=ch.get("updated", ""),
        is_wip=bool(ch.get("work_in_progress", False)),
        project=ch.get("project", ctx.project),
        branch=ch.get("branch", ctx.branch),
    )


def _fetch_group_revisions(
    ctx: BuildContext, group_nodes: dict[int, dict[str, Any]],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """Fetch revisions + commits for a group so per-group commit
    maps can be built for internal and cross-group edge detection.

    Also contributes to the global ctx.commit_to_change_ps and
    ctx.revision_parents so the chain-builder for separate groups
    (which runs after all groups are processed) can look up commit
    relationships across the entire separate-group pool."""
    group_ctps: dict[str, tuple[int, int]] = {}
    group_rev_parents: dict[str, str] = {}
    try:
        q = " OR ".join(f"change:{c}" for c in group_nodes)
        result = ctx.client.rest.get(
            f"/changes/?q={quote(q, safe=':+ ')}"
            "&o=ALL_REVISIONS&o=ALL_COMMITS"
            "&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS&n=500"
        )
        for change in result:
            cn = change.get("_number", 0)
            _collect_revisions(change, group_ctps, group_rev_parents)
            if cn in group_nodes:
                _update_node_meta(group_nodes[cn], change)
                lbl = _parse_labels(change.get("labels", {}))
                lbl["unresolved_count"] = change.get(
                    "unresolved_comment_count", 0
                )
                group_nodes[cn]["review"] = lbl
        # Merge into the global maps so the chain-builder for
        # separate groups (running after all groups are processed)
        # can resolve commit hashes across the whole pool.
        ctx.commit_to_change_ps.update(group_ctps)
        ctx.revision_parents.update(group_rev_parents)
    except Exception:
        pass
    return group_ctps, group_rev_parents


def _group_internal_edges(
    ctx: BuildContext,
    group_raw: list[dict[str, Any]],
    group_ctps: dict[str, tuple[int, int]],
    group_nodes: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build edges between members of a single separate group from
    its own raw_entries. Uses the global ctx.seen_edges set so a
    (from, to) pair never gets added twice across all stages."""
    out: list[dict[str, Any]] = []
    for entry in group_raw:
        pc = entry["parent_commit"]
        child_cn = entry["cn"]
        if not pc or pc not in group_ctps:
            continue
        parent_cn, parent_ps = group_ctps[pc]
        if parent_cn not in group_nodes:
            continue
        if parent_cn == child_cn:
            continue
        key = (parent_cn, child_cn)
        if key in ctx.seen_edges:
            continue
        ctx.seen_edges.add(key)
        parent_latest = group_nodes[parent_cn]["current_patchset"]
        out.append({
            "from": parent_cn,
            "to": child_cn,
            "parent_patchset": parent_ps,
            "parent_latest": parent_latest,
            "is_stale": parent_ps < parent_latest,
        })
    return out


def _group_cross_edges(
    ctx: BuildContext,
    group_ctps: dict[str, tuple[int, int]],
    group_rev_parents: dict[str, str],
    group_nodes: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build stale edges main_series → group_node, linking this
    separate series back to its historical base in main. Uses the
    global ctx.seen_edges set for dedupe."""
    out: list[dict[str, Any]] = []
    for child_hash, parent_hash in group_rev_parents.items():
        if not parent_hash:
            continue
        if child_hash not in group_ctps:
            continue
        child_cn, _ = group_ctps[child_hash]
        if child_cn not in group_nodes:
            continue
        if parent_hash not in ctx.commit_to_change_ps:
            continue
        parent_cn, parent_ps = ctx.commit_to_change_ps[parent_hash]
        if parent_cn not in ctx.nodes:
            continue
        if ctx.nodes[parent_cn].get("series_group", 0) != 0:
            # Parent must itself be in the main series; otherwise we'd
            # be crossing between two separate groups, which is not a
            # useful edge.
            continue
        key = (parent_cn, child_cn)
        if key in ctx.seen_edges:
            continue
        ctx.seen_edges.add(key)
        parent_latest = ctx.nodes[parent_cn]["current_patchset"]
        out.append({
            "from": parent_cn,
            "to": child_cn,
            "parent_patchset": parent_ps,
            "parent_latest": parent_latest,
            "is_stale": parent_ps < parent_latest,
        })
    return out


def _build_separate_chains(ctx: BuildContext) -> list[list[int]]:
    """Build forward-walked chains across ALL separate-group nodes.

    Algorithm (per user specification):

    1. Pool every separate-group member (series_group > 0) into a
       single set of candidates.
    2. Start a chain with the OLDEST unused candidate. Oldest =
       lowest change_number.
    3. From the cursor, find the next patch: any unused candidate
       whose patchsets — examined NEWEST-FIRST — has a parent
       commit hash that resolves to one of the cursor's commits
       (across any of the cursor's patchsets). Preferring newer
       patchsets means in-flight relationships are picked up first;
       falling back to older patchsets recovers merged-to-merged
       chains where the latest patchset is a cherry-pick to master
       (whose parent is a master commit not in our pool) but
       earlier patchsets still point at the in-pool predecessor.
    4. Append the next patch to the chain, advance the cursor,
       repeat step 3 until no candidate matches.
    5. Start a new chain from the next-oldest unused candidate.
       Continue until every candidate is in some chain (possibly
       as a singleton).

    Returns a list of chains; each chain is a list of cns ordered
    oldest → newest. Singletons appear as 1-element chains."""
    sep_nodes = {
        cn: node for cn, node in ctx.nodes.items()
        if (node.get("series_group") or 0) > 0
    }
    if not sep_nodes:
        return []

    # Per-member commit set (every patchset commit hash this member
    # has ever produced) and per-member patchsets sorted newest →
    # oldest as (ps, commit_hash, parent_commit_hash) tuples.
    member_commits: dict[int, set[str]] = {cn: set() for cn in sep_nodes}
    member_revs: dict[int, list[tuple[int, str, str]]] = {
        cn: [] for cn in sep_nodes
    }
    for h, (cn, ps) in ctx.commit_to_change_ps.items():
        if cn not in member_commits:
            continue
        member_commits[cn].add(h)
        parent_h = ctx.revision_parents.get(h, "")
        member_revs[cn].append((ps, h, parent_h))
    for cn in member_revs:
        member_revs[cn].sort(reverse=True)  # newest patchset first

    chains: list[list[int]] = []
    used: set[int] = set()
    sorted_cns = sorted(sep_nodes.keys())  # oldest (lowest cn) first

    while True:
        root = next((c for c in sorted_cns if c not in used), None)
        if root is None:
            break
        chain = [root]
        used.add(root)
        cursor = root
        while True:
            cursor_commits = member_commits[cursor]
            next_patch = None
            for cand in sorted_cns:
                if cand in used:
                    continue
                # Examine candidate's patchsets newest-first; if any
                # has a parent commit in cursor's commit set, this
                # candidate follows the cursor in the chain.
                for _ps, _h, parent_h in member_revs[cand]:
                    if parent_h and parent_h in cursor_commits:
                        next_patch = cand
                        break
                if next_patch is not None:
                    break
            if next_patch is None:
                break
            chain.append(next_patch)
            used.add(next_patch)
            cursor = next_patch
        chains.append(chain)
    return chains


def _expand_separate_series(ctx: BuildContext) -> None:
    """Run topic/hashtag expansion and build one separate group per
    matching series. Search results are filtered to the anchor's
    project + branch unless cross_project_branch is set; this keeps
    sensitive patches from other repos / branches out of a graph
    built from a public-facing change."""
    search_labels = _collect_search_labels(ctx)
    for query, label in search_labels:
        try:
            result = ctx.client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}&n=500"
            )
        except Exception:
            result = []
        in_scope = [ch for ch in result if _matches_anchor_scope(ctx, ch)]
        # Ticket labels search the whole commit message (no better
        # Gerrit operator) — keep only changes whose SUBJECT starts
        # with the ticket so a patch that merely mentions it (e.g.
        # in a Fixes: line) isn't pulled into the graph.
        if label.startswith("ticket "):
            want = label[len("ticket "):]
            in_scope = [
                ch for ch in in_scope
                if subject_ticket(ch.get("subject", "")) == want
            ]
        seed_cns = [
            ch.get("_number", 0) for ch in in_scope
            if ch.get("_number")
        ]
        if result and ctx.logger is not None:
            total = sum(1 for ch in result if ch.get("_number"))
            n_new = sum(1 for c in seed_cns if c not in ctx.nodes)
            dropped = total - len(seed_cns)
            scope_note = ""
            if dropped:
                scope_note = (
                    f", {dropped} dropped (other project/branch — pass"
                    f" --cross-project to include)"
                )
            ctx.logger.note(
                f"{label}: {total} matches"
                f" ({n_new} outside main series{scope_note})"
            )
        _build_separate_group(ctx, seed_cns, label)


# ─── Output assembly ────────────────────────────────────────────────────


def _promote_merged_to_main(ctx: BuildContext) -> int:
    """Move every merged node out of its separate group into the
    main series (series_group=0). The JS layout places merged
    nodes as a single chronological trunk regardless of which
    topic/hashtag pulled them in, so they no longer belong in any
    separate group. Empty groups are pruned. Returns the number
    of nodes promoted.
    """
    promoted = 0
    for cn, n in ctx.nodes.items():
        if n.get("status") == "MERGED" and (n.get("series_group") or 0) > 0:
            n["series_group"] = 0
            promoted += 1
    if promoted == 0:
        return 0
    remaining_groups: list[dict[str, Any]] = []
    for group in ctx.separate_groups:
        group["node_ids"] = [
            cn for cn in group["node_ids"]
            if ctx.nodes.get(cn, {}).get("status") != "MERGED"
        ]
        if group["node_ids"]:
            remaining_groups.append(group)
    ctx.separate_groups = remaining_groups
    return promoted


def _build_merged_trunk(ctx: BuildContext) -> list[int]:
    """Return every merged node's cn, sorted by `submitted` ASC
    (oldest first). This is the chronological trunk the JS layout
    arranges as a single vertical column. Ties on `submitted`
    break on cn ascending so the order is fully deterministic.
    Nodes missing a `submitted` value fall back to `updated`; if
    both are missing they sort to the very end (newest).
    """
    merged = [
        n for n in ctx.nodes.values() if n.get("status") == "MERGED"
    ]
    def key(n):
        ts = n.get("submitted") or n.get("updated") or ""
        # Empty timestamp → sort last (treat as "newest unknown").
        return (ts == "", ts, n["id"])
    merged.sort(key=key)
    return [n["id"] for n in merged]


def _trunk_timestamp(node: dict[str, Any]) -> str:
    """The timestamp we treat as the trunk node's position on master.
    Prefer `submitted`; fall back to `updated` so very old merged
    nodes lacking the submitted field still sort sensibly."""
    return node.get("submitted") or node.get("updated") or ""


def _commit_is_on_master(
    ctx: BuildContext, commit_hash: str,
) -> tuple[bool, str]:
    """True iff `commit_hash` is the merged revision of some change
    we know about — i.e., that commit really lives on master.

    Returns (True, submitted_date) when it does, (False, "") when
    it doesn't (or when we can't tell). Two ways a commit qualifies:

    1. It's the current_commit of a MERGED change still in
       ctx.nodes — that's a trunk node, the commit is on master,
       and we trust its `submitted` field.
    2. Its owning change was filtered out as an off-tree merged
       ancestor (ctx.external_merged_submitted). Filtered-out
       changes are MERGED by definition (that's what the filter
       selects on), and only their merged revision ends up in
       commit_to_change_ps for use here — those commits are on
       master too, and the side table holds the submitted date.
    """
    info = ctx.commit_to_change_ps.get(commit_hash)
    if not info:
        return False, ""
    owner_cn, _ = info
    node = ctx.nodes.get(owner_cn)
    if node is not None:
        if (node.get("status") == "MERGED"
                and node.get("current_commit") == commit_hash):
            return True, node.get("submitted") or node.get("updated") or ""
        return False, ""
    # Owner was filtered out; check the external table.
    ts = ctx.external_merged_submitted.get(owner_cn, "")
    if ts:
        return True, ts
    return False, ""


def _inflight_base_date(ctx: BuildContext, child_id: int) -> str:
    """Walk the in-flight node's git ancestry to find the first
    commit that's actually on master, and return that commit's
    submitted timestamp. This is the patch's "logical base on
    master" — anything merged after this date wasn't there when
    the patch was based, so the trunk node we visually attach it
    to has to be at or before this point.

    Walks via ctx.revision_parents (which holds parents for every
    patchset commit we fetched). At each step we test whether the
    current commit is on master via _commit_is_on_master; the
    first hit wins. If the walk leaves our fetched-commit space
    before finding a master commit, we fall back to the in-flight
    patch's own upload-time fields (a coarser proxy).
    """
    child = ctx.nodes.get(child_id)
    if not child:
        return ""

    # Find the commit hash of the child's current patchset.
    current_ps = child.get("current_patchset", 0)
    cur = None
    for h, (cn, ps) in ctx.commit_to_change_ps.items():
        if cn == child_id and ps == current_ps:
            cur = h
            break

    if cur is None:
        return (
            child.get("current_ps_created", "")
            or child.get("updated", "")
            or ""
        )

    visited: set[str] = set()
    # 50 hops is plenty — a typical in-flight patch's PS is at most
    # 1-2 hops from a master commit through its rebase-base.
    for _ in range(50):
        parent = ctx.revision_parents.get(cur, "")
        if not parent or parent in visited:
            break
        visited.add(parent)
        on_master, ts = _commit_is_on_master(ctx, parent)
        if on_master:
            return ts
        cur = parent

    # The walk left our fetched-commit space (or hit a cycle). Use
    # the patch's own upload-time fields as a coarse approximation.
    return (
        child.get("current_ps_created", "")
        or child.get("updated", "")
        or ""
    )


def _most_recent_merged_at_or_before(
    ctx: BuildContext, merged_trunk: list[int], cutoff: str,
) -> int | None:
    """Return the cn of the most recent merged trunk node whose
    `submitted` is at or before `cutoff`. `cutoff` must be an ISO
    timestamp string (Gerrit's format compares correctly as plain
    strings). Returns None when no trunk node qualifies (e.g. the
    cutoff predates the earliest known merge).
    """
    if not cutoff:
        return None
    best_cn: int | None = None
    best_ts = ""
    for cn in merged_trunk:
        n = ctx.nodes.get(cn)
        if n is None:
            continue
        ts = _trunk_timestamp(n)
        if not ts or ts > cutoff:
            continue
        if ts > best_ts:
            best_ts = ts
            best_cn = cn
    return best_cn


def _child_ancestry_contains(
    ctx: BuildContext, child_id: int, parent_id: int,
    max_hops: int = 50,
) -> bool:
    """True iff the in-flight child's current-patchset git ancestry
    (walked via ctx.revision_parents) passes through any commit
    owned by parent_id.

    Used to tell apart "literal /related parent IS the series-
    relative git parent" (keep the edge, even when stale) from
    "patch was rebased away from the literal /related parent onto
    an unrelated base" (let the redirect retarget to the cutoff
    pick). Shares the walk shape of _inflight_base_date — start at
    the child's current-PS commit, follow revision_parents one hop
    at a time, bounded by max_hops and a visited-set against any
    cycles the fetched-commit space might contain.
    """
    child = ctx.nodes.get(child_id)
    if not child:
        return False
    current_ps = child.get("current_patchset", 0)
    cur: str | None = None
    for h, (cn, ps) in ctx.commit_to_change_ps.items():
        if cn == child_id and ps == current_ps:
            cur = h
            break
    if cur is None:
        return False
    visited: set[str] = set()
    for _ in range(max_hops):
        parent_h = ctx.revision_parents.get(cur, "")
        if not parent_h or parent_h in visited:
            return False
        visited.add(parent_h)
        info = ctx.commit_to_change_ps.get(parent_h)
        if info and info[0] == parent_id:
            return True
        cur = parent_h
    return False


def _redirect_inflight_to_recent_merged(
    ctx: BuildContext, merged_trunk: list[int],
) -> int:
    """Rewire each in-flight node's merged-ancestor edge to point at
    the merged trunk node that best represents its place on master.

    Why: /related preserves the historical parent relationship at
    upload time, which can leave an in-flight patch literally
    attached to an old merged ancestor whose final-merged patchset
    isn't actually where the in-flight branched off. The user's
    mental model is "from the in-flight patch, look back through
    git history — the first merged patch you hit is the right
    visual parent".

    We don't have full git ancestry locally, so we approximate using
    the in-flight node's CURRENT patchset creation time
    (`current_ps_created`). The target is the most recent merged
    trunk node whose `submitted` <= that timestamp: that's the
    latest landed change the in-flight node could plausibly be
    based on, because anything merged after the patchset was
    uploaded didn't exist yet from the patch's perspective.

    Edges from newer-than-anchor trunk nodes are left alone — those
    are real in-flight-descendant chains above the anchor. Edges
    that already point at the right target are no-ops.

    Returns the number of edges redirected. Modifies ctx.edges in
    place.
    """
    if not merged_trunk:
        return 0
    submitted_map = {
        cn: _trunk_timestamp(ctx.nodes[cn])
        for cn in merged_trunk if cn in ctx.nodes
    }
    anchor_node = ctx.nodes.get(ctx.change_number)
    anchor_submitted = (
        _trunk_timestamp(anchor_node) if anchor_node else ""
    )

    def is_redirect_candidate(e: dict) -> tuple[bool, int | None]:
        child = ctx.nodes.get(e["to"])
        parent = ctx.nodes.get(e["from"])
        if not child or child.get("status") != "NEW":
            return False, None
        if not parent or parent.get("status") != "MERGED":
            return False, None
        if e["from"] not in submitted_map:
            return False, None
        # Series-relative parent precedence (dual gate). Keep the
        # /related edge as-is when BOTH:
        #   (1) the parent merged AFTER the child's last upload
        #       (parent.submitted > child.current_ps_created), AND
        #   (2) the child's current-PS git ancestry actually passes
        #       through a commit owned by the parent.
        # That combination is the "series parent merged ahead of
        # the in-flight child" shape (54050/54051 — 54050 was
        # rebased to merge in May 2026, five months after 54051's
        # last upload, and 54051 ps6's git parent IS 54050 ps5; we
        # want 54050 -> 54051 kept).
        # Gate (1) rules out the 64441 shape (parent merged BEFORE
        # the in-flight was last uploaded — the in-flight was
        # actively left behind on a stale ps and the date-based
        # redirect should fire).
        # Gate (2) rules out the 62852 shape (in-flight was rebased
        # onto an off-tree base, /related parent isn't in the git
        # ancestry — 99442c1's redirect to the correct trunk node
        # should fire).
        parent_ts = submitted_map[e["from"]]
        child_ps_created = child.get("current_ps_created", "")
        if (parent_ts and child_ps_created
                and parent_ts > child_ps_created
                and _child_ancestry_contains(
                    ctx, e["to"], e["from"])):
            return False, None
        # The "where did this patch branch off master?" cutoff.
        # See _inflight_base_date — walks the in-flight patch's git
        # ancestry until it hits a commit that's actually on master,
        # returning that change's submitted timestamp. We don't
        # short-circuit on "parent is newer than anchor" because the
        # literal /related parent can still be the wrong trunk node:
        # an in-flight patch can be uploaded against a master commit
        # that lives further up the trunk than the merged change
        # /related happens to point at (hit on 62852 — current parent
        # commit is owned by an off-tree merged change submitted
        # AFTER the trunk node /related names).
        cutoff = _inflight_base_date(ctx, e["to"])
        if not cutoff:
            return False, None
        target = _most_recent_merged_at_or_before(
            ctx, merged_trunk, cutoff,
        )
        if target is None or target == e["from"]:
            return False, None
        return True, target

    # Two-pass to support deduplication: walk the existing edges
    # once to separate kept vs. candidates, then add redirects that
    # don't collide with edges we're already keeping AND don't close
    # a cycle by attaching target -> child when target is already
    # reachable from child via the kept-edge set.
    #
    # Cycle case (hit on 54459): an in-flight series 54469..54486 had
    # 54486 at the top before it merged. Chain edges 54469 -> 54470
    # -> ... -> 54485 -> 54486 are still surfaced by /related as
    # parent->child (Gerrit preserves the historical relationship,
    # is_stale=False). When 54486 merges ahead and 54469 rebases onto
    # it, the redirect would emit 54486 -> 54469 and close the loop.
    # Graph.js's recursive layout (_subtreeWidth, _subtreeHeight,
    # _layoutTree) walks childrenOf without a visited set and stack-
    # overflows on cycles -> blank canvas. We skip the redirect in
    # that case and keep the original (literal /related) edge so the
    # in-flight node isn't orphaned from its series view.
    keep_edges: list[dict[str, Any]] = []
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for e in ctx.edges:
        ok, target = is_redirect_candidate(e)
        if ok and target is not None:
            candidates.append((target, e["to"], e))
        else:
            keep_edges.append(e)
    # Pin candidate processing order — dict-iteration order over the
    # Gerrit JSON is not guaranteed stable across runs/sessions, and
    # eager adj updates below make later candidates dependent on the
    # state earlier ones leave behind.
    candidates.sort(key=lambda c: (c[0], c[1]))

    adj: dict[int, set[int]] = {}
    for e in keep_edges:
        adj.setdefault(e["from"], set()).add(e["to"])

    def _reachable(src: int, dst: int) -> bool:
        """True iff dst is reachable from src via `adj`. `adj` is kept
        in lockstep with keep_edges + existing_pairs in the loop below
        so reachability reflects every edge that will actually ship."""
        if src == dst:
            return True
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            for v in adj.get(u, ()):
                if v == dst:
                    return True
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        return False

    existing_pairs: set[tuple[int, int]] = {
        (e["from"], e["to"]) for e in keep_edges
    }
    for target, child, orig in candidates:
        if (target, child) in existing_pairs:
            continue  # would be a duplicate of a kept edge
        if _reachable(child, target):
            # target -> child would close a cycle. Fall back to the
            # original /related parent edge so the in-flight node
            # still has a merged-ancestor link — but only if the
            # fallback itself doesn't close a cycle through edges
            # other redirects have added in this loop.
            if (orig["from"], orig["to"]) not in existing_pairs \
                    and not _reachable(orig["to"], orig["from"]):
                keep_edges.append(orig)
                existing_pairs.add((orig["from"], orig["to"]))
                adj.setdefault(orig["from"], set()).add(orig["to"])
            continue
        target_ps = ctx.nodes[target].get("current_patchset", 0)
        keep_edges.append({
            "from": target,
            "to": child,
            "parent_patchset": target_ps,
            "parent_latest": target_ps,
            "is_stale": False,
        })
        existing_pairs.add((target, child))
        adj.setdefault(target, set()).add(child)
    ctx.edges = keep_edges
    return len(candidates)


def _hook_orphan_main_chains(
    ctx: BuildContext, merged_trunk: list[int],
) -> int:
    """Attach orphan chain roots to the merged trunk.

    A chain root is a NEW node with no incoming edge from any
    already-connected node — either a main-group patch that was
    rebased outside the tracked series (67067 -> 66436 in the
    61965 graph) or a separate-group patch whose git base sits on
    master rather than on another main-group patch (62140, 66444,
    66882 in 61965). Without hooking these, the JS layout dumps
    them into a far-right floating column and the user can't tell
    where the chain branches off master.

    Reuses the same "where did this branch off master" logic that
    _redirect_inflight_to_recent_merged uses: walk the root's
    current-PS ancestry via _inflight_base_date, pick the most
    recent trunk node at-or-before that cutoff via
    _most_recent_merged_at_or_before, and emit a stale edge to it.

    Roots whose cutoff falls BEFORE the oldest trunk node (chain is
    based on master history older than anything we track) or that
    have no cutoff at all are left unhooked — they still lay out in
    the far-right column so the user sees the chain isn't connected
    to any tracked merged patch. Roots whose cutoff falls AFTER the
    newest trunk node hook to the trunk top (base was merged just
    after our visible range — visually "just above trunk top").

    Returns the number of synthesized edges. Idempotent w.r.t.
    ctx.seen_edges — a re-hook for the same (target, root) pair is
    silently skipped.
    """
    if not merged_trunk:
        return 0

    trunk_dates = [
        _trunk_timestamp(ctx.nodes[cn]) for cn in merged_trunk
        if cn in ctx.nodes and _trunk_timestamp(ctx.nodes[cn])
    ]
    trunk_max = max(trunk_dates) if trunk_dates else ""

    trunk_set = set(merged_trunk)
    # A node is "already connected" if it has ANY incoming edge
    # from another visible node, or an outgoing edge to a trunk
    # node (in-flight parent of a trunk-column child — 62887's
    # shape, already anchored via _layoutTrunkSideBranches).
    has_incoming: set[int] = set()
    has_trunk_child: set[int] = set()
    for e in ctx.edges:
        has_incoming.add(e["to"])
        if e["to"] in trunk_set:
            has_trunk_child.add(e["from"])

    roots: list[int] = []
    for cn, node in sorted(ctx.nodes.items()):
        if cn in trunk_set:
            continue
        if cn in has_incoming:
            continue
        if cn in has_trunk_child:
            continue
        if cn == ctx.change_number:
            continue
        if node.get("status") != "NEW":
            continue
        roots.append(cn)

    if not roots:
        return 0

    adj: dict[int, set[int]] = {}
    for e in ctx.edges:
        adj.setdefault(e["from"], set()).add(e["to"])

    def _reachable(src: int, dst: int) -> bool:
        if src == dst:
            return True
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            for v in adj.get(u, ()):
                if v == dst:
                    return True
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        return False

    added = 0
    for root in roots:
        cutoff = _inflight_base_date(ctx, root)
        if not cutoff:
            continue
        target = _most_recent_merged_at_or_before(
            ctx, merged_trunk, cutoff,
        )
        # target is None only when cutoff predates EVERY trunk
        # timestamp — the chain root is based on master history
        # older than anything we track. Leave it unhooked; hooking
        # to any trunk node would misrepresent it as newer than
        # its actual base. (The "cutoff after every trunk" case is
        # already handled naturally: every trunk node qualifies as
        # at-or-before, so the helper returns the trunk top.)
        if target is None:
            continue
        if _reachable(root, target):
            continue
        key = (target, root)
        if key in ctx.seen_edges:
            continue
        ctx.seen_edges.add(key)
        target_ps = ctx.nodes[target].get("current_patchset", 0)
        ctx.edges.append({
            "from": target,
            "to": root,
            "parent_patchset": target_ps,
            "parent_latest": target_ps,
            # Stale so the JS layout renders the hookup with a
            # dashed connector — we're inferring the relationship
            # from a date walk, not from a concrete /related edge.
            "is_stale": True,
        })
        adj.setdefault(target, set()).add(root)
        added += 1
    return added


def _prune_unrelated_merged(ctx: BuildContext) -> tuple[int, int]:
    """Drop merged patches that have nothing to do with the series.

    Gerrit /related returns the anchor's whole git chain, including
    patches that merely happened to be stacked above/below the
    series at upload time. Once merged they'd inflate the trunk and
    the Merged counter (hit on 65282: 64499/64529/64534 are
    nodemap/mdt work with no LU-18222 relation, stacked on 64560).

    A merged node is RELEVANT when it carries any series signal:
      - its ticket matches a ticket of any non-merged node (the
        series' in-flight and abandoned members) or the anchor's, or
      - its topic is the anchor's topic (or a --topic extra), or
      - its hashtags intersect the anchor's non-lifecycle hashtags
        (or an --include-hashtag extra).
    The anchor itself is always relevant.

    An irrelevant merged node with a DIRECT edge to or from a
    non-merged node is a branch point: something in-flight hangs
    off it (or an in-flight git-parent points into it), so it
    stays in the trunk column — flagged trunk_structural=True and
    excluded from the Merged counter. Every other irrelevant
    merged node is deleted: its submitted timestamp is stashed in
    external_merged_submitted so ancestry dating (redirect/hookup
    cutoffs) still resolves through its merged commit, its
    non-current revision hashes are purged from commit_to_change_ps
    so _commit_is_on_master can't misdate old pre-merge patchsets
    as on-master, and its edges + seen_edges pairs are dropped.

    No-op when the relevance signal set is empty (ticketless anchor
    with no topic/hashtags) — pruning without signals would gut the
    trunk.

    Must run AFTER _promote_merged_to_main (separate_groups
    node_ids are scrubbed of merged cns there) and BEFORE
    status_counts / _build_separate_chains / _build_merged_trunk.

    Returns (deleted_count, structural_count).
    """
    anchor_cn = ctx.change_number
    anchor = ctx.nodes.get(anchor_cn) or {}
    # Recorded for stats so a vanished trunk row is diagnosable
    # from the build output ("where did change X go").
    ctx.pruned_merged_cns = []
    ctx.structural_merged_cns = []

    tickets = {
        n["ticket"] for n in ctx.nodes.values()
        if n.get("status") != "MERGED" and n.get("ticket")
    }
    if anchor.get("ticket"):
        tickets.add(anchor["ticket"])
    topics = {t for t in [anchor.get("topic", "")] if t}
    topics.update(t for t in ctx.extra_topics if t)
    hashtags = {
        h for h in (anchor.get("hashtags") or [])
        if h not in _LIFECYCLE_HASHTAGS
    }
    hashtags.update(h for h in ctx.extra_hashtags if h)
    tickets.update(t for t in ctx.extra_tickets if t)

    if not tickets and not topics and not hashtags:
        return 0, 0

    def relevant(n: dict[str, Any]) -> bool:
        if n["id"] == anchor_cn:
            return True
        if n.get("ticket") and n["ticket"] in tickets:
            return True
        if n.get("topic") and n["topic"] in topics:
            return True
        return any(h in hashtags for h in (n.get("hashtags") or []))

    irrelevant = [
        cn for cn, n in ctx.nodes.items()
        if n.get("status") == "MERGED" and not relevant(n)
    ]
    if not irrelevant:
        return 0, 0

    branch_point: set[int] = set()
    for e in ctx.edges:
        src = ctx.nodes.get(e["from"])
        dst = ctx.nodes.get(e["to"])
        if not src or not dst:
            continue
        if (src.get("status") == "MERGED"
                and dst.get("status") != "MERGED"):
            branch_point.add(e["from"])
        if (dst.get("status") == "MERGED"
                and src.get("status") != "MERGED"):
            branch_point.add(e["to"])

    structural = 0
    deleted: set[int] = set()
    for cn in irrelevant:
        node = ctx.nodes[cn]
        if cn in branch_point:
            node["trunk_structural"] = True
            ctx.structural_merged_cns.append(cn)
            structural += 1
            continue
        sub = node.get("submitted", "")
        if sub:
            ctx.external_merged_submitted[cn] = sub
        current = node.get("current_commit", "")
        stale_hashes = [
            h for h, (owner, _ps) in ctx.commit_to_change_ps.items()
            if owner == cn and h != current
        ]
        for h in stale_hashes:
            del ctx.commit_to_change_ps[h]
        del ctx.nodes[cn]
        deleted.add(cn)

    if deleted:
        ctx.edges = [
            e for e in ctx.edges
            if e["from"] not in deleted and e["to"] not in deleted
        ]
        ctx.seen_edges = {
            k for k in ctx.seen_edges
            if k[0] not in deleted and k[1] not in deleted
        }
    ctx.pruned_merged_cns = sorted(deleted)
    ctx.structural_merged_cns.sort()
    return len(deleted), structural


def _assemble_payload(ctx: BuildContext) -> dict[str, Any]:
    """Flatten the accumulated build state into the final dict shape
    consumed by `render.generate_html`."""
    # Merged-trunk promotion runs BEFORE the separate-chain builder
    # so chains are constructed over the in-flight pool only —
    # nodes promoted into the main series no longer appear as
    # separate-group seeds, and previously-empty groups are pruned.
    _promote_merged_to_main(ctx)
    pruned_merged, structural_merged = _prune_unrelated_merged(ctx)

    status_counts: dict[str, int] = {}
    for n in ctx.nodes.values():
        s = n["status"]
        # Structural merged nodes are branch-point parents of the
        # series, not series members — visible in the trunk but
        # excluded from the Merged counter.
        if s == "MERGED" and n.get("trunk_structural"):
            continue
        status_counts[s] = status_counts.get(s, 0) + 1

    stale_edges = sum(1 for e in ctx.edges if e["is_stale"])
    tickets = sorted(
        set(n["ticket"] for n in ctx.nodes.values() if n["ticket"])
    )
    generated_at = datetime.now().astimezone().strftime(
        "%Y-%m-%d %I:%M:%S %p %Z"
    )

    chains = _build_separate_chains(ctx)
    merged_trunk = _build_merged_trunk(ctx)
    # Rewrite in-flight → old-merged edges to attach to the most
    # recent merged trunk node the in-flight patch could plausibly
    # have branched off (using its current patchset's creation
    # time). Done AFTER chains are built so the chain detector
    # still uses the historical edges for parent-commit matching;
    # only the rendered edges get the redirect treatment.
    _redirect_inflight_to_recent_merged(ctx, merged_trunk)
    # Hook orphan main-group chains (roots with no incoming
    # main-group edge) into the trunk column by date. Runs AFTER
    # the redirect step because redirect only rewires existing
    # in-flight -> merged edges — the orphan set is unchanged by
    # it, and running after avoids any risk of the two phases
    # picking competing targets for the same node.
    _hook_orphan_main_chains(ctx, merged_trunk)
    return {
        "anchor": ctx.change_number,
        "base_url": ctx.base_url,
        "nodes": list(ctx.nodes.values()),
        "edges": ctx.edges,
        "separate_groups": ctx.separate_groups,
        # Forward-walked chains across all separate-group nodes
        # (oldest → newest). Used by the JS layout to render
        # separate groups as vertical columns rooted at their
        # oldest member.
        "separate_chains": chains,
        # Every merged node in chronological order (oldest first).
        # The JS layout positions these as a single vertical
        # column at x=0, with the anchor pinned at y=0 — older
        # merged below, newer merged (and in-flight descendants)
        # above.
        "merged_trunk": merged_trunk,
        "generated_at": generated_at,
        "stats": {
            "node_count": len(ctx.nodes),
            "edge_count": len(ctx.edges),
            "status_counts": status_counts,
            "stale_edge_count": stale_edges,
            "tickets": tickets,
            "separate_group_count": len(ctx.separate_groups),
            "separate_chain_count": len(chains),
            "merged_trunk_count": len(merged_trunk),
            # Unrelated merged patches removed from / kept-but-
            # uncounted in the trunk (see _prune_unrelated_merged).
            "pruned_merged_count": pruned_merged,
            "structural_merged_count": structural_merged,
            "pruned_merged_cns": getattr(ctx, "pruned_merged_cns", []),
            "structural_merged_cns": getattr(
                ctx, "structural_merged_cns", []),
            "generated_at": generated_at,
        },
    }


# ─── Public entry point ─────────────────────────────────────────────────


def resolve_ticket_anchor(
    client: GerritCommentsClient,
    ticket: str,
    branch: str = "master",
) -> int:
    """Pick the anchor change for a ticket-mode graph.

    Candidates are changes on `branch` whose SUBJECT starts with
    the ticket (a mention elsewhere in the message doesn't count).
    Among in-flight (NEW) candidates, prefer the one embedded in
    the series with the most in-flight members — its /related
    chain is the richest context for the ticket. Ties break on
    ticket-member count within the series, then most recently
    updated. Only the 8 most recently updated NEW candidates get
    a /related probe to bound the API cost.

    A ticket with no in-flight patches anchors on the newest
    merged one (pure trunk view of the landing order). Raises
    ValueError when the ticket has no matching changes at all.

    SECURITY GATE: the anchor is only ever taken from `branch`
    (default master). The branch is constrained in the Gerrit
    query AND re-verified client-side on every candidate — the
    tool is hosted on the web, and an auto-selected anchor from a
    non-requested branch would leak that branch's patches into a
    publicly served graph (the anchor's branch scopes the whole
    expansion). Any other branch requires the caller to pass it
    explicitly via --branch.
    """
    q = f'message:"{ticket}" branch:{branch}'
    try:
        result = client.rest.get(
            f"/changes/?q={quote(q, safe=':+ ')}&n=200"
        )
    except Exception as e:
        raise ValueError(f"ticket search failed for {ticket}: {e}")
    cands = [
        ch for ch in result
        if subject_ticket(ch.get("subject", "")) == ticket
        and ch.get("_number")
        and ch.get("branch") == branch
    ]
    if not cands:
        raise ValueError(
            f"no changes on branch '{branch}' with a subject "
            f"starting with {ticket}"
        )

    new_cands = [ch for ch in cands if ch.get("status") == "NEW"]
    if not new_cands:
        merged = [ch for ch in cands if ch.get("status") == "MERGED"]
        pool = merged or cands
        pool.sort(
            key=lambda ch: ch.get("submitted")
            or ch.get("updated") or "",
            reverse=True,
        )
        return pool[0]["_number"]

    new_cands.sort(key=lambda ch: ch.get("updated", ""), reverse=True)
    best_cn = None
    best_key: tuple[int, int, str] | None = None
    for ch in new_cands[:8]:
        cn = ch["_number"]
        try:
            rel = client.rest.get(
                f"/changes/{cn}/revisions/current/related"
            ).get("changes", [])
        except Exception:
            rel = []
        n_inflight = sum(1 for e in rel if e.get("status") == "NEW")
        n_ticket = sum(
            1 for e in rel
            if subject_ticket(
                (e.get("commit") or {}).get("subject", "")
            ) == ticket
        )
        key = (n_inflight, n_ticket, ch.get("updated", ""))
        if best_key is None or key > best_key:
            best_key = key
            best_cn = cn
    return best_cn


def build_graph(
    client: GerritCommentsClient,
    change_number: int,
    base_url: str,
    progress: bool = True,
    fetch_details: bool = True,
    fetch_comments: bool = False,
    include_topic: bool = True,
    include_hashtag: bool = True,
    extra_topics: list[str] | None = None,
    extra_hashtags: list[str] | None = None,
    extra_tickets: list[str] | None = None,
    cross_project_branch: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Build the full series graph with stale branch information.

    Args:
        fetch_details: If True, fetch CI links from change messages
            (slower, requires extra API calls). If False, skip message
            fetching for faster graph generation.
        fetch_comments: If True, fetch detailed inline comments per
            change (requires individual API calls, can be slow for
            large series). Implies fetch_details.
        include_topic: If True (default), include series sharing the
            anchor's topic as SEPARATE trees alongside the main one.
        include_hashtag: Same as include_topic but for hashtags.
        extra_topics: Additional topic names to search for and include.
        extra_hashtags: Additional hashtag names to search for and include.
        extra_tickets: JIRA tickets (e.g. LU-18222) whose patches are
            searched for and included; only changes whose subject
            STARTS with the ticket count (mentions are ignored).
        cross_project_branch: When False (the default), search-based
            expansion (topic/hashtag/commit-parent discovery) is
            scoped to the anchor's project AND branch. Set True to
            include results from any project/branch on the same host.

    Returns a dict ready to be embedded as JSON in the HTML template.
    """
    logger = PhaseLogger(total=_TOTAL_PHASES, enabled=progress)
    ctx = BuildContext(
        client=client,
        change_number=change_number,
        base_url=base_url,
        progress=progress,
        fetch_details=fetch_details or fetch_comments,
        fetch_comments=fetch_comments,
        include_topic=include_topic,
        include_hashtag=include_hashtag,
        extra_topics=list(extra_topics or []),
        extra_hashtags=list(extra_hashtags or []),
        extra_tickets=list(extra_tickets or []),
        cross_project_branch=cross_project_branch,
        logger=logger,
    )

    logger.header(f"gerrit-cli graph / #{change_number}")

    logger.start("Resolving project")
    _resolve_project(ctx)
    logger.done(ctx.project)

    logger.start(f"/related(#{change_number})")
    entries = _fetch_related(ctx)
    _parse_related_entries(ctx, entries)
    logger.done(f"{len(entries)} changes")

    logger.start("Fan-out over initial set")
    fanout_added = _expand_via_related_fanout(ctx)
    logger.done(
        f"+{fanout_added} new (total {len(ctx.nodes)})"
        if fanout_added
        else f"no new changes (total {len(ctx.nodes)})"
    )

    logger.start(f"Revision history ({len(ctx.nodes)} changes)")
    _fetch_initial_revisions(ctx)
    logger.done(f"{len(ctx.commit_to_change_ps)} commits mapped")

    logger.start("Discovering missing parent commits")
    discovered = _discover_missing_nodes(ctx)
    filtered = _filter_merged_ancestors(ctx)
    parts = []
    if discovered:
        parts.append(f"+{discovered} discovered")
    if filtered:
        parts.append(f"{filtered} ancestors filtered")
    logger.done(", ".join(parts) if parts else "nothing new")

    _attach_review_info(ctx)

    logger.start("Fetching CI details")
    active = _fetch_ci_and_comments(ctx)
    if not ctx.fetch_details:
        logger.done("skipped (--skip-ci-details)")
    elif active == 0:
        logger.done("no active changes")
    elif ctx.fetch_comments:
        logger.done(f"{active} changes (with inline comments)")
    else:
        logger.done(f"{active} active changes")

    logger.start("Building main edges")
    cycles_removed = _build_main_edges(ctx)
    _tag_main_group(ctx)
    cycle_note = f", {cycles_removed} cycle edges removed" if cycles_removed else ""
    logger.done(f"{len(ctx.edges)} edges{cycle_note}")

    logger.start("Topic/hashtag expansion")
    _expand_separate_series(ctx)
    if ctx.separate_groups:
        sep_total = sum(len(g["node_ids"]) for g in ctx.separate_groups)
        logger.done(
            f"{len(ctx.separate_groups)} groups, {sep_total} nodes"
        )
        # Separate-group nodes are added after the main CI pass, so
        # backfill their Jenkins/Maloo links (and inline comments
        # when requested) the same way main-series nodes get them.
        sep_cns = [
            cn for g in ctx.separate_groups for cn in g["node_ids"]
        ]
        _fetch_ci_and_comments(ctx, sep_cns)
    else:
        logger.done("none")

    payload = _assemble_payload(ctx)
    if name:
        payload["name"] = name
    stats = payload["stats"]
    logger.summary(
        f"{stats['node_count']} nodes · "
        f"{stats['edge_count']} edges · "
        f"{stats['separate_group_count']} separate groups"
    )
    return payload
