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
    # cloud_id="site": these tests are not about routing, and a scoped-
    # token probe of /myself would be the first call they inspect.
    return JiraClient(JiraConfig(server="https://example.atlassian.net", token="t",
                                 auth_type="basic", email="a@example.com",
                                 cloud_id="site"))


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
        # Set both, as the user's .env can: JIRA_CLOUD_EMAIL makes the
        # login basic auth, which probes /myself first unless cloud_id
        # is "site", and the probe would be calls[0].
        monkeypatch.setenv("JIRA_CLOUD_EMAIL", "a@example.com")
        monkeypatch.setenv("JIRA_CLOUD_ID", "site")
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


ANDREAS = "712020:a3f0d19b-732a-41e2-9e73-9c88f74d83bb"
PATRICK = "712020:52347719-9693-4a4c-96c0-a66b12299969"


def _no_lookup(name):
    raise AssertionError(f"looked up {name}")


class TestMentionAccountIds:
    """@[Name|accountId] carries the accountId through a read and a write."""

    @pytest.mark.parametrize("inner,expected", [
        ("Andreas Dilger", ("Andreas Dilger", None)),
        (f"Andreas Dilger|{ANDREAS}", ("Andreas Dilger", ANDREAS)),
        (f" Andreas Dilger | {ANDREAS} ", ("Andreas Dilger", ANDREAS)),
        ("Andreas Dilger|", ("Andreas Dilger|", None)),
        (f"|{ANDREAS}", (f"|{ANDREAS}", None)),
        ("Andreas|Dilger Smith", ("Andreas|Dilger Smith", None)),
    ])
    def test_split_mention(self, inner, expected):
        from jira_tool.adf import split_mention

        assert split_mention(inner) == expected

    def test_mention_with_account_id_needs_no_lookup(self):
        doc = markdown_to_adf(f"cc @[Andreas Dilger|{ANDREAS}] please", _no_lookup)
        assert doc["content"][0]["content"][1] == {
            "type": "mention", "attrs": {"id": ANDREAS, "text": "@Andreas Dilger"}}

    def test_mention_with_account_id_without_resolver(self):
        nodes = markdown_to_adf(f"@[Andreas Dilger|{ANDREAS}]")["content"][0]["content"]
        assert nodes == [{"type": "mention", "attrs": {"id": ANDREAS, "text": "@Andreas Dilger"}}]

    def test_read_emits_account_id(self):
        from jira_tool.adf import adf_to_markdown

        doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": ANDREAS, "text": "@Andreas Dilger",
                                          "accessLevel": ""}},
            {"type": "text", "text": " see this"}]}]}
        assert adf_to_markdown(doc) == f"@[Andreas Dilger|{ANDREAS}] see this"

    def test_read_without_text_or_id(self):
        from jira_tool.adf import adf_to_markdown

        doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": ANDREAS}},
            {"type": "mention", "attrs": {"text": "@Someone"}}]}]}
        assert adf_to_markdown(doc) == f"@[unknown|{ANDREAS}]@[Someone]"

    def test_read_write_round_trip_keeps_account_ids(self):
        from jira_tool.adf import adf_to_markdown

        doc = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [
                {"type": "mention", "attrs": {"id": ANDREAS, "text": "@Andreas Dilger"}},
                {"type": "text", "text": " and "},
                {"type": "mention", "attrs": {"id": PATRICK, "text": "@Patrick Farrell"}},
                {"type": "text", "text": ": "},
                {"type": "text", "text": "look", "marks": [{"type": "strong"}]}]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "mention", "attrs": {"id": PATRICK, "text": "@Patrick Farrell"}}]}]}]}]}
        assert markdown_to_adf(adf_to_markdown(doc), _no_lookup) == doc

    def test_adf_mentions(self):
        from jira_tool.adf import adf_mentions

        doc = markdown_to_adf(f"> @[Andreas Dilger|{ANDREAS}]\n\n- @[Patrick Farrell|{PATRICK}]")
        assert adf_mentions(doc) == {"andreas dilger": ANDREAS, "patrick farrell": PATRICK}
        assert adf_mentions(None) == {}

    def test_markdown_mentions(self):
        from jira_tool.adf import markdown_mentions

        text = f"@[Andreas Dilger|{ANDREAS}] and @[Patrick Farrell] and `@[Code|x]`"
        assert markdown_mentions(text) == {"andreas dilger": ANDREAS}


class TestLooksLikeAccountId:
    @pytest.mark.parametrize("value,expected", [
        (ANDREAS, True),
        ("5b10ac8d82e05b22cc7d4ef5", True),
        ("qm:a3f0d19b-732a-41e2-9e73-9c88f74d83bb:289ca33b-1090-47b9-a6f1-991e175b9add", True),
        ("a3f0d19b-732a-41e2-9e73-9c88f74d83bb", True),
        ("Ada-Lovelace", False),
        ("Abe:Smith", False),
        ("Andreas Dilger", False),
    ])
    def test_account_ids(self, value, expected):
        from jira_tool.commands._helpers import looks_like_account_id

        assert looks_like_account_id(value) is expected


def _project_page(*keys):
    return {"values": [{"key": k} for k in keys], "isLast": True, "startAt": 0}


class TestCloudUserLookup:
    """Without Browse users and groups, Cloud user/search finds nobody."""

    @responses.activate
    def test_search_falls_back_to_assignable_users(self, cloud_client):
        responses.add(responses.GET, f"{CLOUD}/user/search", json=[], status=200)
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json=_project_page("TLU", "TLC"), status=200)
        responses.add(responses.GET, f"{CLOUD}/user/assignable/multiProjectSearch",
                      json=[{"accountId": ANDREAS, "displayName": "Andreas Dilger"}],
                      status=200)
        users = cloud_client.search_users("Andreas", max_results=5)
        assert users == [{"accountId": ANDREAS, "displayName": "Andreas Dilger"}]
        params = responses.calls[2].request.params
        assert params["projectKeys"] == "TLU,TLC"
        assert params["query"] == "Andreas"
        assert params["maxResults"] == "5"

    @responses.activate
    def test_project_list_is_cached(self, cloud_client):
        responses.add(responses.GET, f"{CLOUD}/user/search", json=[], status=200)
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json=_project_page("TLU"), status=200)
        responses.add(responses.GET, f"{CLOUD}/user/assignable/multiProjectSearch",
                      json=[], status=200)
        cloud_client.search_users("a")
        cloud_client.search_users("b")
        assert sum("project/search" in c.request.url for c in responses.calls) == 1

    @responses.activate
    def test_project_list_follows_pages(self, cloud_client):
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json={"values": [{"key": "A"}], "isLast": False}, status=200)
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json={"values": [{"key": "B"}], "isLast": True}, status=200)
        assert cloud_client.visible_project_keys() == ["A", "B"]
        assert responses.calls[1].request.params["startAt"] == "1"

    @responses.activate
    def test_search_with_results_does_not_fall_back(self, cloud_client):
        responses.add(responses.GET, f"{CLOUD}/user/search",
                      json=[{"accountId": ANDREAS, "displayName": "Andreas Dilger"}],
                      status=200)
        assert len(cloud_client.search_users("Andreas")) == 1
        assert len(responses.calls) == 1

    @responses.activate
    def test_cli_users_with_project(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://example.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "t")
        monkeypatch.setenv("JIRA_CLOUD_EMAIL", "a@example.com")
        monkeypatch.setenv("JIRA_CLOUD_ID", "site")
        responses.add(responses.GET, f"{CLOUD}/user/search", json=[], status=200)
        responses.add(responses.GET, f"{CLOUD}/user/assignable/multiProjectSearch",
                      json=[{"accountId": ANDREAS, "displayName": "Andreas Dilger",
                             "active": True}], status=200)
        result = CliRunner().invoke(main, ["users", "Dilger", "--project", "TLU"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["total"] == 1
        assert data["users"][0]["account_id"] == ANDREAS
        assert responses.calls[1].request.params["projectKeys"] == "TLU"

    @responses.activate
    def test_comment_mention_resolves_through_fallback(self, cloud_client):
        responses.add(responses.GET, f"{CLOUD}/user/search", json=[], status=200)
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json=_project_page("TLU"), status=200)
        responses.add(responses.GET, f"{CLOUD}/user/assignable/multiProjectSearch",
                      json=[{"accountId": PATRICK, "displayName": "Patrick Farrell"}],
                      status=200)
        responses.add(responses.POST, f"{CLOUD}/issue/TLU-1/comment",
                      json={"id": "1"}, status=201)
        cloud_client.add_comment("TLU-1", "hi @[Patrick Farrell]")
        sent = json.loads(responses.calls[-1].request.body)
        assert sent["body"]["content"][0]["content"][1]["attrs"]["id"] == PATRICK

    @responses.activate
    def test_comment_mention_with_account_id_makes_no_lookup(self, cloud_client):
        responses.add(responses.POST, f"{CLOUD}/issue/TLU-1/comment",
                      json={"id": "1"}, status=201)
        cloud_client.add_comment(
            "TLU-1", f"@[Patrick Farrell|{PATRICK}] and again @[Patrick Farrell]")
        assert len(responses.calls) == 1
        nodes = json.loads(responses.calls[0].request.body)["body"]["content"][0]["content"]
        assert [n["attrs"]["id"] for n in nodes if n["type"] == "mention"] == [PATRICK, PATRICK]

    @responses.activate
    def test_edit_reuses_mentions_of_the_comment(self, cloud_client):
        existing = markdown_to_adf(f"old @[Andreas Dilger|{ANDREAS}] text")
        responses.add(responses.GET, f"{CLOUD}/issue/TLU-1/comment/7",
                      json={"id": "7", "body": existing}, status=200)
        responses.add(responses.PUT, f"{CLOUD}/issue/TLU-1/comment/7",
                      json={"id": "7"}, status=200)
        cloud_client.edit_comment("TLU-1", "7", "new @[andreas dilger] text")
        assert [c.request.method for c in responses.calls] == ["GET", "PUT"]
        node = json.loads(responses.calls[1].request.body)["body"]["content"][0]["content"][1]
        assert node == {"type": "mention", "attrs": {"id": ANDREAS, "text": "@andreas dilger"}}

    @responses.activate
    def test_edit_without_mentions_does_not_fetch(self, cloud_client):
        responses.add(responses.PUT, f"{CLOUD}/issue/TLU-1/comment/7",
                      json={"id": "7"}, status=200)
        cloud_client.edit_comment("TLU-1", "7", "no mentions here")
        assert len(responses.calls) == 1

    @responses.activate
    def test_unresolved_mention_names_the_syntax(self, cloud_client):
        from jira_tool.errors import InvalidInputError

        responses.add(responses.GET, f"{CLOUD}/issue/TLU-1/comment/7",
                      json={"id": "7", "body": markdown_to_adf("old")}, status=200)
        responses.add(responses.GET, f"{CLOUD}/user/search", json=[], status=200)
        responses.add(responses.GET, f"{CLOUD}/project/search",
                      json=_project_page("TLU"), status=200)
        responses.add(responses.GET, f"{CLOUD}/user/assignable/multiProjectSearch",
                      json=[], status=200)
        with pytest.raises(InvalidInputError, match=r"@\[Display Name\|accountId\]"):
            cloud_client.edit_comment("TLU-1", "7", "hi @[Nobody Here]")
