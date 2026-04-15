"""Gerrit patch series DAG visualizer.

Builds a full DAG of all related changes for a Gerrit patch, resolving
stale patchset dependencies to show which patches need rebasing. Generates
an interactive HTML visualization with:

- Vertical tree layout growing upward from the anchor change
- Edge labels showing which patchset each dependency goes through
- Stale edges highlighted (child depends on old patchset of parent)
- Click-to-re-anchor: click any node to make it the new starting point
- Filter controls for abandoned/stale changes

The key insight: Gerrit's /related endpoint shows one patchset per change
in the commit chain. When a change is rebased, its old patchset's children
become "orphans" — their parent commit no longer matches anything in the
current chain. By fetching ALL_REVISIONS for each change, we can reconnect
these orphans to the correct parent change at the correct (stale) patchset.
"""

from .build import build_graph
from .render import generate_html, save_and_open

__all__ = ["build_graph", "generate_html", "save_and_open"]
