"""Compact diffs: Gerrit ignores ?context=, so review and diff cut it."""

import argparse
import json
from functools import partial
from unittest.mock import MagicMock, patch

import pytest

from gerrit_cli.client import GerritCommentsClient
from gerrit_cli.reviewer import (
    CodeReviewer,
    FileChange,
    content_to_hunks,
    patch_to_contents,
)

from .test_client import FILE_TEXT, file_answer


def _ctx(start, n):
    return [f"l{i}" for i in range(start, start + n)]


# A 1000 line file with one line changed at 101 and one at 501, as the
# Whamcloud Gerrit returns it whatever context was asked for.
WHOLE_FILE = [
    {"ab": _ctx(1, 100)},
    {"a": ["old101"], "b": ["new101"]},
    {"ab": _ctx(102, 399)},
    {"a": ["old501"], "b": ["new501"]},
    {"ab": _ctx(502, 499)},
]


class TestContentToHunks:

    def test_default_context_is_three_lines(self):
        hunks = content_to_hunks(WHOLE_FILE, 3)
        assert len(hunks) == 2
        first, second = hunks
        assert (first.old_start, first.old_count) == (98, 7)
        assert (first.new_start, first.new_count) == (98, 7)
        assert [ln.content for ln in first.lines] == [
            "l98", "l99", "l100", "old101", "new101",
            "l102", "l103", "l104"]
        assert (second.old_start, second.old_count) == (498, 7)
        assert [ln.content for ln in second.lines][-3:] == [
            "l502", "l503", "l504"]

    def test_line_numbers_follow_the_file(self):
        hunks = content_to_hunks(WHOLE_FILE, 3)
        added = [ln for h in hunks for ln in h.lines if ln.type == "added"]
        assert [ln.line_number_new for ln in added] == [101, 501]
        tail = hunks[1].lines[-1]
        assert (tail.line_number_old, tail.line_number_new) == (504, 504)

    def test_none_keeps_the_whole_file(self):
        hunks = content_to_hunks(WHOLE_FILE, None)
        assert len(hunks) == 1
        assert hunks[0].old_count == 1000

    def test_zero_is_changes_only(self):
        hunks = content_to_hunks(WHOLE_FILE, 0)
        assert [[ln.type for ln in h.lines] for h in hunks] == [
            ["deleted", "added"], ["deleted", "added"]]
        assert hunks[1].old_start == 501

    def test_close_changes_share_a_hunk(self):
        content = [
            {"ab": _ctx(1, 10)},
            {"b": ["new"]},
            {"ab": _ctx(11, 6)},
            {"a": ["gone"]},
            {"ab": _ctx(18, 10)},
        ]
        hunks = content_to_hunks(content, 3)
        assert len(hunks) == 1
        assert hunks[0].old_start == 8
        assert sum(ln.type == "context" for ln in hunks[0].lines) == 12

    def test_server_skip_sections(self):
        content = [
            {"skip": 50},
            {"ab": _ctx(51, 3)},
            {"b": ["new"]},
            {"ab": _ctx(54, 3)},
            {"skip": 40},
        ]
        hunks = content_to_hunks(content, 3)
        assert len(hunks) == 1
        assert hunks[0].old_start == 51
        assert hunks[0].new_count == 7

    def test_unchanged_file_has_no_hunks(self):
        assert content_to_hunks([{"ab": _ctx(1, 5)}], 3) == []

    def test_no_context_only_hunk_before_a_skip(self):
        # The tail of a unified diff hunk, then the lines it left out:
        # with less context than it carries, the tail must not open a
        # hunk of its own just because a change comes after the skip.
        content = [
            {"skip": 10}, {"ab": _ctx(11, 3)}, {"b": ["new"]},
            {"ab": _ctx(14, 3)}, {"skip": 50}, {"ab": _ctx(67, 3)},
            {"a": ["gone"]}, {"ab": _ctx(71, 3)},
        ]
        for n in (0, 1, 2):
            hunks = content_to_hunks(content, n)
            assert len(hunks) == 2, n
            assert all(any(ln.type != "context" for ln in h.lines)
                       for h in hunks)


# WHOLE_FILE as git format-patch writes it: three lines of context.
PATCH = r"""From abc Mon Sep 17 00:00:00 2001
Subject: [PATCH] LU-1 test: a change

A commit message line like --- or +++ is not a file header.
---

diff --git a/big.sh b/big.sh
index 1..2 100644
--- a/big.sh
+++ b/big.sh
@@ -98,7 +98,7 @@
 l98
 l99
 l100
-old101
+new101
 l102
 l103
 l104
@@ -498,7 +498,7 @@
 l498
 l499
 l500
-old501
+new501
 l502
 l503
 l504
diff --git a/new.c b/new.c
new file mode 100644
--- /dev/null
+++ b/new.c
@@ -0,0 +1,2 @@
+one
+two
diff --git a/gone.c b/gone.c
deleted file mode 100644
--- a/gone.c
+++ /dev/null
@@ -1 +0,0 @@
-only
\ No newline at end of file
--
2.43.0
"""


class TestPatchToContents:

    def test_same_hunks_as_the_whole_file(self):
        content = patch_to_contents(PATCH)["big.sh"]
        for n in (0, 1, 2, 3):
            assert content_to_hunks(content, n) == content_to_hunks(
                WHOLE_FILE, n), n

    def test_added_and_deleted_files(self):
        contents = patch_to_contents(PATCH)
        assert contents["new.c"] == [{"b": ["one", "two"]}]
        assert contents["gone.c"] == [{"a": ["only"]}]
        assert set(contents) == {"big.sh", "new.c", "gone.c"}


def _review_client(patch_text, files, encoding="json"):
    client = MagicMock()
    client.format_change_url.return_value = "https://example.com/123"
    client.get_text.side_effect = partial(GerritCommentsClient.get_text, client)

    def get(url, return_response=False):
        if url.endswith("/files"):
            return files
        if url.endswith("/patch"):
            return file_answer(patch_text, encoding)
        if url.endswith("/content"):
            return file_answer(FILE_TEXT, encoding)
        if "/diff" in url:
            return {"content": WHOLE_FILE}
        return {"id": "x", "current_revision": "abc",
                "revisions": {"abc": {"commit": {"message": "m",
                                                 "parents": []}}}}
    client.rest.get.side_effect = get
    return client


class TestReviewFromPatch:

    @pytest.mark.parametrize("encoding", ["json", "base64"])
    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_one_request_for_every_file(self, mock_client_class, encoding):
        files = {"big.sh": {"status": "M", "lines_inserted": 2,
                            "lines_deleted": 2},
                 "new.c": {"status": "A", "lines_inserted": 2}}
        client = _review_client(PATCH, files, encoding)
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (client.url, 123)

        data = CodeReviewer().get_review_data("https://example.com/123")

        urls = [c.args[0] for c in client.rest.get.call_args_list]
        assert not [u for u in urls if "/diff" in u]
        big = next(f for f in data.files if f.path == "big.sh")
        assert big.hunks == content_to_hunks(WHOLE_FILE, 3)

    @pytest.mark.parametrize("encoding", ["json", "base64"])
    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_full_content_is_the_file_text(self, mock_client_class, encoding):
        files = {"new.c": {"status": "A", "lines_inserted": 2}}
        client = _review_client(PATCH, files, encoding)
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (client.url, 123)

        data = CodeReviewer().get_review_data(
            "https://example.com/123", include_file_content=True)

        assert data.files[0].new_content == FILE_TEXT

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_file_whose_counts_disagree_is_fetched(self, mock_client_class):
        files = {"big.sh": {"status": "M", "lines_inserted": 5,
                            "lines_deleted": 1}}
        client = _review_client(PATCH, files)
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (client.url, 123)

        CodeReviewer().get_review_data("https://example.com/123")

        urls = [c.args[0] for c in client.rest.get.call_args_list]
        assert [u for u in urls if "/diff" in u] == [
            "/changes/123/revisions/current/files/big.sh/diff"]

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_full_context_uses_the_file_diff(self, mock_client_class):
        files = {"big.sh": {"status": "M", "lines_inserted": 1,
                            "lines_deleted": 1}}
        client = _review_client(PATCH, files)
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (client.url, 123)

        CodeReviewer().get_review_data("https://example.com/123",
                                       context_lines=None)

        urls = [c.args[0] for c in client.rest.get.call_args_list]
        assert not [u for u in urls if u.endswith("/patch")]


class TestReviewCompactByDefault:

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_review_data_cuts_context(self, mock_client_class):
        client = MagicMock()
        client.format_change_url.return_value = "https://example.com/123"
        client.rest.get.side_effect = [
            {"id": "x", "current_revision": "abc",
             "revisions": {"abc": {"commit": {"message": "m",
                                              "parents": []}}}},
            {"big.sh": {"status": "M", "lines_inserted": 2,
                        "lines_deleted": 2}},
            {"content": WHOLE_FILE},
        ]
        client.get_text.side_effect = Exception(
            "no /patch: the per-file diff is used")
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (
            client.url, 123)

        data = CodeReviewer().get_review_data("https://example.com/123")

        diff = data.files[0].to_dict()["diff"]
        assert diff.count("\n@@ ") == 2
        assert "l50" not in diff.split("\n")
        assert "new_content" not in data.files[0].to_dict()

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_base_patchset_reaches_gerrit(self, mock_client_class):
        client = MagicMock()
        client.rest.get.side_effect = [
            {"id": "x", "current_revision": "abc", "revisions": {}},
            {"f.c": {"status": "M"}},
            {"content": []},
        ]
        mock_client_class.return_value = client
        mock_client_class.parse_gerrit_url.return_value = (
            client.url, 123)

        CodeReviewer().get_review_data("https://example.com/123", base=2)

        urls = [c.args[0] for c in client.rest.get.call_args_list]
        assert urls[1].endswith("/files?base=2")
        assert urls[2].endswith("/diff?base=2")

    def test_new_content_kept_when_fetched(self):
        fc = FileChange(path="a", status="M", old_path=None, lines_added=0,
                        lines_deleted=0, size_delta=0, new_content="x")
        assert fc.to_dict()["new_content"] == "x"


def _parse_diff(*argv):
    from gerrit_cli import cli
    from gerrit_cli.parsers import setup_parsers

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(
        dest="command", parser_class=cli._JsonErrorParser)
    setup_parsers(subparsers, cli.build_handlers())
    return parser.parse_args(["diff", *argv])


def _run_diff(capsys, client, *argv):
    from gerrit_cli.commands.ci import cmd_diff

    args = _parse_diff(*argv)
    with patch("gerrit_cli.cli.GerritCommentsClient") as MockClient:
        MockClient.return_value = client
        MockClient.parse_gerrit_url.return_value = ("https://x", 123)
        with pytest.raises(SystemExit) as exc:
            cmd_diff(args)
    return exc.value.code, json.loads(capsys.readouterr().out)


def _client(patchsets):
    client = MagicMock()
    client.get_change_detail.return_value = {
        "revisions": {f"r{n}": {"_number": n} for n in patchsets}}
    client.get_files_between_patchsets.return_value = {
        "/COMMIT_MSG": {}, "big.sh": {"status": "M"}}
    client.get_diff.return_value = {"content": WHOLE_FILE}
    return client


class TestDiffDefaults:

    def test_patchsets_are_optional(self):
        args = _parse_diff("123")
        assert (args.patchset_a, args.patchset_b, args.unified) == (
            None, None, 3)

    def test_newest_against_previous(self, capsys):
        client = _client([1, 2, 3])
        code, out = _run_diff(capsys, client, "123")
        assert code == 0
        assert (out["patchset_a"], out["patchset_b"]) == (2, 3)
        client.get_files_between_patchsets.assert_called_once_with(123, 2, 3)
        assert out["files"][0]["diff"].count("@@ -") == 2
        assert "note" not in out

    def test_one_patchset_is_against_parent(self, capsys):
        client = _client([1])
        code, out = _run_diff(capsys, client, "123")
        assert code == 0
        assert out["patchset_a"] is None
        assert "parent" in out["note"]
        client.get_files_between_patchsets.assert_called_once_with(
            123, None, 1)
        assert client.get_diff.call_args.args[2] is None

    def test_base_given(self, capsys):
        client = _client([1, 2, 3, 4])
        code, out = _run_diff(capsys, client, "123", "2")
        assert (out["patchset_a"], out["patchset_b"]) == (2, 4)

    def test_base_not_below_target(self, capsys):
        code, out = _run_diff(capsys, _client([1, 2]), "123", "2", "1")
        assert code != 0
        assert out["code"] == "INVALID_INPUT"


class TestDiffExplained:

    def test_explain_diff(self, capsys):
        from gerrit_cli.commands.meta import COMMAND_EXPLANATIONS
        assert "diff" in COMMAND_EXPLANATIONS
