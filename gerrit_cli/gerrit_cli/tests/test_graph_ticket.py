"""Tests for ticket-mode graph support: anchor resolution and the
ticket expansion label plumbing."""

from types import SimpleNamespace
from typing import Any

import pytest

from gerrit_cli.graph.build import (
    _collect_search_labels,
    resolve_ticket_anchor,
)
from gerrit_cli.graph.nodes import subject_ticket


class _FakeRest:
    """Canned-response REST stub: maps a substring of the request
    path to a response."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append(path)
        for frag, resp in self.routes.items():
            if frag in path:
                return resp
        return []


def _client(routes: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(rest=_FakeRest(routes))


def _ch(cn: int, subject: str, status: str, updated: str = "",
        submitted: str = "") -> dict[str, Any]:
    return {
        "_number": cn, "subject": subject, "status": status,
        "updated": updated, "submitted": submitted,
    }


def _rel(cn: int, subject: str, status: str) -> dict[str, Any]:
    return {
        "_change_number": cn, "status": status,
        "commit": {"subject": subject},
    }


class TestSubjectTicket:
    def test_leading_ticket(self):
        assert subject_ticket("LU-18222 quota: x") == "LU-18222"
        assert subject_ticket("EX-14659 ofd: y") == "EX-14659"

    def test_mention_only_is_not_a_match(self):
        assert subject_ticket("quota: fix fallout of LU-18222") == ""
        assert subject_ticket("") == ""


class TestResolveTicketAnchor:
    def test_prefers_biggest_inflight_series(self):
        # Two NEW candidates: 200 sits in a 3-in-flight series,
        # 100 is standalone. 200 wins despite being older.
        client = _client({
            "/changes/100/revisions/current/related": {"changes": []},
            "/changes/200/revisions/current/related": {"changes": [
                _rel(201, "LU-1 c", "NEW"),
                _rel(200, "LU-1 b", "NEW"),
                _rel(202, "LU-2 other", "NEW"),
            ]},
            "/changes/?q=": [
                _ch(100, "LU-1 a", "NEW", updated="2026-08-10"),
                _ch(200, "LU-1 b", "NEW", updated="2026-08-01"),
            ],
        })
        assert resolve_ticket_anchor(client, "LU-1") == 200

    def test_mentions_do_not_count_as_candidates(self):
        client = _client({
            "related": {"changes": []},
            "/changes/?q=": [
                _ch(100, "misc: mentions LU-1 in passing", "NEW"),
                _ch(200, "LU-1 real patch", "NEW", updated="2026-08-01"),
            ],
        })
        assert resolve_ticket_anchor(client, "LU-1") == 200

    def test_falls_back_to_newest_merged(self):
        client = _client({
            "/changes/?q=": [
                _ch(10, "LU-1 old", "MERGED",
                    submitted="2026-01-01 00:00:00"),
                _ch(20, "LU-1 new", "MERGED",
                    submitted="2026-03-01 00:00:00"),
            ],
        })
        assert resolve_ticket_anchor(client, "LU-1") == 20

    def test_raises_when_no_subject_matches(self):
        client = _client({
            "/changes/?q=": [
                _ch(100, "misc: mentions LU-1 only", "NEW"),
            ],
        })
        with pytest.raises(ValueError):
            resolve_ticket_anchor(client, "LU-1")


class TestTicketSearchLabels:
    def test_extra_tickets_produce_message_query_labels(self):
        ctx = SimpleNamespace(
            change_number=1,
            nodes={1: {"topic": "", "hashtags": []}},
            include_topic=True,
            include_hashtag=True,
            extra_topics=[],
            extra_hashtags=[],
            extra_tickets=["LU-18222"],
        )
        labels = _collect_search_labels(ctx)
        assert ('message:"LU-18222"', "ticket LU-18222") in labels
