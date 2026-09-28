"""Tests for change search: paging and the 'search' command's output."""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from gerrit_cli.client import GerritCommentsClient


def _client(pages):
    """A client whose search_changes serves ``pages`` in order, recording
    each call's (limit, start)."""
    client = GerritCommentsClient.__new__(GerritCommentsClient)
    calls = []

    def fake_search(query, limit=25, start=0, options=None):
        calls.append((limit, start))
        return pages.pop(0) if pages else []

    client.search_changes = fake_search
    return client, calls


def _page(first, count, more):
    page = [{"_number": n} for n in range(first, first + count)]
    if more and page:
        page[-1]["_more_changes"] = True
    return page


class TestSearchAll:
    def test_follows_more_changes(self):
        client, calls = _client([_page(1, 3, True), _page(4, 2, False)])
        got = client.search_all("q", page_size=3)
        assert [c["_number"] for c in got] == [1, 2, 3, 4, 5]
        assert calls == [(3, 0), (3, 3)]

    def test_short_page_with_more_is_not_the_end(self):
        # A server may cap a page below what was asked for; only the flag
        # says whether there is more.
        client, calls = _client([_page(1, 2, True), _page(3, 1, False)])
        got = client.search_all("q", page_size=100)
        assert len(got) == 3
        assert calls == [(100, 0), (100, 2)]

    def test_full_page_without_flag_is_the_end(self):
        client, calls = _client([_page(1, 3, False), _page(4, 3, False)])
        assert len(client.search_all("q", page_size=3)) == 3
        assert len(calls) == 1

    def test_stops_at_max_results_and_keeps_the_flag(self):
        client, calls = _client([_page(1, 4, True), _page(5, 2, True)])
        got = client.search_all("q", max_results=6, page_size=4)
        assert len(got) == 6
        assert calls == [(4, 0), (2, 4)]
        assert got[-1].get("_more_changes")

    def test_empty(self):
        client, _ = _client([[]])
        assert client.search_all("q") == []


class TestCmdSearch:
    def _run(self, capsys, results, **kw):
        from gerrit_cli.commands.meta import cmd_search

        args = argparse.Namespace(
            query="message:LU-1", limit=25, start=0, all=False, max=500, pretty=False
        )
        for k, v in kw.items():
            setattr(args, k, v)
        client = MagicMock()
        client.search_changes.return_value = results
        client.search_all.return_value = results
        client.format_change_url.side_effect = lambda p, n: f"https://g/c/{p}/+/{n}"
        cli = MagicMock()
        cli.GerritCommentsClient.return_value = client
        with patch("gerrit_cli.commands.meta._cli", return_value=cli):
            with pytest.raises(SystemExit) as exc:
                cmd_search(args)
        assert exc.value.code == 0
        return json.loads(capsys.readouterr().out), client

    def test_more_results_comes_from_gerrit(self, capsys):
        page = [
            {"_number": 1, "created": "2026-01-01 00:00:00.000000000"},
            {"_number": 2, "_more_changes": True},
        ]
        out, _ = self._run(capsys, page, limit=2, start=10)
        assert out["more_results"] is True
        assert out["next_start"] == 12
        assert out["changes"][0]["created"].startswith("2026-01-01")

    def test_full_page_is_not_more(self, capsys):
        out, _ = self._run(capsys, [{"_number": 1}, {"_number": 2}], limit=2)
        assert "more_results" not in out

    def test_all_pages(self, capsys):
        out, client = self._run(capsys, [{"_number": 1, "hashtags": ["x"]}], all=True, max=40)
        client.search_all.assert_called_once_with("message:LU-1", max_results=40)
        client.search_changes.assert_not_called()
        assert out["changes"][0]["hashtags"] == ["x"]
