"""Unit tests for the Markdown to ADF converter."""

import json

import pytest
import responses
from click.testing import CliRunner

from jira_tool.adf import markdown_to_adf
from jira_tool.cli import main
from jira_tool.client import JiraClient
from jira_tool.config import JiraConfig


def types(doc):
    return [n["type"] for n in doc["content"]]


class TestPlainText:
    def test_paragraphs_and_hard_breaks(self):
        doc = markdown_to_adf("one\ntwo\n\nthree")
        assert types(doc) == ["paragraph", "paragraph"]
        assert doc["content"][0]["content"] == [
            {"type": "text", "text": "one"},
            {"type": "hardBreak"},
            {"type": "text", "text": "two"},
        ]

    def test_empty(self):
        assert types(markdown_to_adf("")) == ["paragraph"]

    def test_globs_and_identifiers_untouched(self):
        text = "lctl get_param osc.*.stats and file_dirty_bytes * 2"
        doc = markdown_to_adf(text)
        assert doc["content"][0]["content"] == [{"type": "text", "text": text}]


class TestBlocks:
    def test_code_fence(self):
        doc = markdown_to_adf("before\n```bash\nls -l\n**not bold**\n```\nafter")
        assert types(doc) == ["paragraph", "codeBlock", "paragraph"]
        code = doc["content"][1]
        assert code["attrs"] == {"language": "bash"}
        assert code["content"][0]["text"] == "ls -l\n**not bold**"

    def test_blockquote(self):
        doc = markdown_to_adf("> quoted line\n> second\n\nanswer")
        assert types(doc) == ["blockquote", "paragraph"]
        assert types(doc["content"][0]) == ["paragraph"]

    def test_ordered_list_with_continuation(self):
        doc = markdown_to_adf("3. first\n   more\n4. second")
        lst = doc["content"][0]
        assert lst["type"] == "orderedList"
        assert lst["attrs"] == {"order": 3}
        assert len(lst["content"]) == 2
        first = lst["content"][0]["content"][0]["content"]
        assert [n["type"] for n in first] == ["text", "hardBreak", "text"]

    def test_bullet_list(self):
        doc = markdown_to_adf("- a\n- b")
        assert doc["content"][0]["type"] == "bulletList"

    def test_heading_and_rule(self):
        doc = markdown_to_adf("## Title\n---\ntext")
        assert types(doc) == ["heading", "rule", "paragraph"]
        assert doc["content"][0]["attrs"] == {"level": 2}

    def test_table(self):
        doc = markdown_to_adf("| a | b |\n|---|---|\n| 1 | 2 |")
        table = doc["content"][0]
        assert table["type"] == "table"
        assert table["content"][0]["content"][0]["type"] == "tableHeader"
        assert table["content"][1]["content"][1]["type"] == "tableCell"


class TestInline:
    def nodes(self, text, resolve=None):
        return markdown_to_adf(text, resolve)["content"][0]["content"]

    def test_bold_code_link(self):
        nodes = self.nodes("**bold** and `osc.*.stats` see [patch](https://x.io/1)")
        assert nodes[0] == {"type": "text", "text": "bold", "marks": [{"type": "strong"}]}
        assert nodes[2] == {"type": "text", "text": "osc.*.stats", "marks": [{"type": "code"}]}
        assert nodes[4]["marks"] == [{"type": "link", "attrs": {"href": "https://x.io/1"}}]

    def test_italic(self):
        nodes = self.nodes("an *emphasised* word")
        assert nodes[1] == {"type": "text", "text": "emphasised", "marks": [{"type": "em"}]}

    def test_bare_url_without_trailing_period(self):
        nodes = self.nodes("see https://review.whamcloud.com/c/1.")
        assert nodes[1]["text"] == "https://review.whamcloud.com/c/1"
        assert nodes[2]["text"] == "."

    def test_mention_resolved(self):
        nodes = self.nodes("hi @[Steve Crusan]", lambda name: "acct-1")
        assert nodes[1] == {"type": "mention", "attrs": {"id": "acct-1", "text": "@Steve Crusan"}}

    def test_mention_left_as_text_without_resolver(self):
        assert self.nodes("hi @[Steve]") == [{"type": "text", "text": "hi @[Steve]"}]


CLOUD = "https://example.atlassian.net/rest/api/3"


@pytest.fixture
def cloud_client():
    return JiraClient(JiraConfig(server="https://example.atlassian.net", token="t",
                                 auth_type="basic", email="a@example.com"))


class TestReplies:
    @responses.activate
    def test_add_comment_parent_id(self, cloud_client):
        responses.add(responses.POST, f"{CLOUD}/issue/TLC-1/comment",
                      json={"id": "11", "parentId": 10}, status=201)
        cloud_client.add_comment("TLC-1", "**hi**", parent_id="10")
        sent = json.loads(responses.calls[0].request.body)
        assert sent["parentId"] == 10
        assert sent["body"]["content"][0]["content"][0]["marks"] == [{"type": "strong"}]

    @responses.activate
    def test_cli_reply_to_reply_uses_thread_root(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://example.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "t")
        monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
        monkeypatch.setenv("JIRA_AUTH_TYPE", "basic")
        responses.add(responses.GET, f"{CLOUD}/issue/TLC-1/comment/12",
                      json={"id": "12", "parentId": 10}, status=200)
        responses.add(responses.POST, f"{CLOUD}/issue/TLC-1/comment",
                      json={"id": "13", "parentId": 10}, status=201)
        result = CliRunner().invoke(main, ["comment", "TLC-1", "reply", "--reply-to", "12"])
        assert result.exit_code == 0, result.output
        assert json.loads(responses.calls[1].request.body)["parentId"] == 10
        assert json.loads(result.output)["comment"]["parent_id"] == "10"


class TestRoundTrip:
    """Markdown read back from ADF must convert to the same document."""

    CASES = [
        "**bold** *em* ***both*** `code` [link](https://x.io/a)",
        "1. first\n2. second\n   - nested a\n   - nested b\n3. third",
        "- item\n  continued\n- next",
        "> quote\n>\n> second paragraph",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "```c\nint x = *p;\n```",
        "plain 1. not a list\n1. but this line looks like one",
        "literal \\*\\*stars\\*\\* and osc.*.stats and llite.*.max_cached_mb",
        "@[Steve Crusan] hello",
    ]

    @pytest.mark.parametrize("text", CASES)
    def test_round_trip(self, text):
        from jira_tool.adf import adf_to_markdown

        doc = markdown_to_adf(text, lambda name: "acct")
        assert markdown_to_adf(adf_to_markdown(doc), lambda name: "acct") == doc

    def test_paragraph_that_looks_like_list_is_escaped(self):
        from jira_tool.adf import adf_to_markdown

        doc = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "1. not a list"}]}]}
        assert adf_to_markdown(doc) == "1\\. not a list"

    def test_globs_are_not_emphasis(self):
        nodes = markdown_to_adf("osc.*.max_dirty_mb, llite.*.max_cached_mb")["content"][0]["content"]
        assert nodes == [{"type": "text", "text": "osc.*.max_dirty_mb, llite.*.max_cached_mb"}]

    def test_marks_with_edge_spaces(self):
        from jira_tool.adf import adf_to_markdown

        doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "so "},
            {"type": "text", "text": "much ", "marks": [{"type": "em"}]},
            {"type": "text", "text": "better"}]}]}
        assert adf_to_markdown(doc) == "so *much* better"

    def test_strings_pass_through(self):
        from jira_tool.adf import adf_to_markdown

        assert adf_to_markdown("server text") == "server text"
        assert adf_to_markdown(None) is None


class TestTransitionComment:
    @responses.activate
    def test_cloud_transition_comment_is_adf(self, cloud_client):
        responses.add(responses.POST, f"{CLOUD}/issue/TLC-1/transitions", status=204)
        cloud_client.do_transition("TLC-1", "21", comment="moving **on**")
        sent = json.loads(responses.calls[0].request.body)
        body = sent["update"]["comment"][0]["add"]["body"]
        assert body["type"] == "doc"
