"""Private changes stay off the graph unless --include-private: the page
is served to people the credentials' owner did not share them with."""

from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest

from gerrit_cli.graph.build import (
    BuildContext,
    _assemble_payload,
    _make_edge,
    _public,
    resolve_search_anchor,
    resolve_ticket_anchor,
)
from gerrit_cli.graph.nodes import _make_node, _update_node_meta


def _ctx(include_private=False):
    """10 merged; 101 <- 102 (private) <- 103 in the series; a separate
    group of 104 (private) and one of 105."""
    client = MagicMock()
    client.rest.get.return_value = []
    ctx = BuildContext(
        client=client, change_number=101, base_url="https://gerrit.invalid",
        progress=False, fetch_details=False, fetch_comments=False,
        include_topic=True, include_hashtag=True, extra_topics=[],
        extra_hashtags=[], extra_tickets=[], include_private=include_private,
    )
    for cn, status in ((10, "MERGED"), (101, "NEW"), (102, "NEW"), (103, "NEW"),
                       (104, "NEW"), (105, "NEW")):
        node = _make_node(cn, f"LU-1 change {cn}", status, 1, "A", ctx.base_url)
        node["is_private"] = cn in (102, 104)
        node["submitted"] = "2026-01-01 00:00:00.000000000" if status == "MERGED" else ""
        node["review"] = {}
        ctx.nodes[cn] = node
    ctx.raw_entries = [{"cn": cn, "commit": f"{cn:040x}", "parent_commit": "", "ps": 1,
                        "latest": 1} for cn in (10, 101, 102, 103)]
    for a, b in ((10, 101), (101, 102), (102, 103)):
        ctx.edges.append(_make_edge(a, 1, 1, b, 1, 1))
        ctx.seen_edges.add((a, b))
    ctx.separate_groups = [{"id": 1, "label": "topic x", "node_ids": [104]},
                           {"id": 2, "label": "topic y", "node_ids": [105]}]
    return ctx


def test_private_changes_are_left_out():
    payload = _assemble_payload(_ctx())
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {10, 101, 103, 105}
    assert all(e["from"] not in (102, 104) and e["to"] not in (102, 104)
               for e in payload["edges"])
    assert [g["node_ids"] for g in payload["separate_groups"]] == [[105]]
    assert payload["stats"]["status_counts"] == {"MERGED": 1, "NEW": 3}


def test_include_private_keeps_them():
    payload = _assemble_payload(_ctx(include_private=True))
    assert {n["id"] for n in payload["nodes"]} == {10, 101, 102, 103, 104, 105}
    assert {(e["from"], e["to"]) for e in payload["edges"]} >= {(101, 102), (102, 103)}


def test_a_private_anchor_is_an_error():
    ctx = _ctx()
    ctx.nodes[101]["is_private"] = True
    with pytest.raises(ValueError, match="101 is private; --include-private"):
        _assemble_payload(ctx)


def test_the_flag_comes_from_the_change():
    node = _make_node(1, "S", "NEW", 1, "A", "https://x")
    _update_node_meta(node, {"is_private": True})
    assert node["is_private"] is True
    _update_node_meta(node, {})
    assert node["is_private"] is False


def test_searches_leave_private_changes_out():
    assert _public("topic:x OR hashtag:y", False) == "(topic:x OR hashtag:y) -is:private"
    assert _public("topic:x", True) == "topic:x"
    for resolve, args in ((resolve_ticket_anchor, ("LU-1",)),
                          (resolve_search_anchor, ("hashtag", "x"))):
        for include, want in ((False, True), (True, False)):
            client = MagicMock()
            client.rest.get.return_value = []
            with pytest.raises(ValueError):
                resolve(client, *args, include_private=include)
            url = unquote(client.rest.get.call_args[0][0])
            assert ("-is:private" in url) is want, url
