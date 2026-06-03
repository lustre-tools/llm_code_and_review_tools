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
from .nodes import _make_node, _update_node_meta
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
# for the next master merge has "master-next", so using it as a
# series identifier would pull in hundreds of unrelated changes.
# Users can still force-include them with `--include-hashtag`.
_LIFECYCLE_HASHTAGS = frozenset({"master-next"})

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
    series we care about. Returns the number of removed changes."""
    related_set = {e["cn"] for e in ctx.raw_entries}
    merged_discovered = [
        cn for cn in ctx.nodes
        if cn not in related_set and ctx.nodes[cn]["status"] == "MERGED"
    ]
    for cn in merged_discovered:
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


def _assemble_payload(ctx: BuildContext) -> dict[str, Any]:
    """Flatten the accumulated build state into the final dict shape
    consumed by `render.generate_html`."""
    status_counts: dict[str, int] = {}
    for n in ctx.nodes.values():
        s = n["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    stale_edges = sum(1 for e in ctx.edges if e["is_stale"])
    tickets = sorted(
        set(n["ticket"] for n in ctx.nodes.values() if n["ticket"])
    )
    generated_at = datetime.now().astimezone().strftime(
        "%Y-%m-%d %I:%M:%S %p %Z"
    )

    chains = _build_separate_chains(ctx)
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
        "generated_at": generated_at,
        "stats": {
            "node_count": len(ctx.nodes),
            "edge_count": len(ctx.edges),
            "status_counts": status_counts,
            "stale_edge_count": stale_edges,
            "tickets": tickets,
            "separate_group_count": len(ctx.separate_groups),
            "separate_chain_count": len(chains),
            "generated_at": generated_at,
        },
    }


# ─── Public entry point ─────────────────────────────────────────────────


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
