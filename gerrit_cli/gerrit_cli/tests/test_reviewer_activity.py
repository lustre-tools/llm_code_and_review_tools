"""`gerrit reviewers` says who is a reviewer and who a CC, and what each did.

Five reviewer searches asked for it: `reviewers` listed CCs with reviewers
and gave no activity, so runs were handed the Reviewer-role list and parsed
`info` and comments themselves to see who had voted or written lately.
"""

import json
from types import SimpleNamespace

import pytest

from gerrit_cli import cli
from gerrit_cli.commands.reviewers import (
    cmd_reviewers, message_votes, reviewer_activity,
)

LABELS = {"Code-Review": {}, "Verified": {}}

OWNER = {"_account_id": 5, "name": "Marc Vef", "username": "mvef"}
ANDREAS = {"_account_id": 1, "name": "Andreas Dilger", "username": "adilger"}
BOBIJAM = {"_account_id": 3, "name": "Zhenyu Xu", "username": "bobijam"}
AIREVIEW = {"_account_id": 7, "name": "AI review", "username": "aireview"}
JENKINS = {"_account_id": 8, "name": "jenkins", "username": "jenkins",
           "tags": ["SERVICE_USER"]}
OLEG = {"_account_id": 2, "name": "Oleg Drokin", "username": "green"}


def _message(author, when, patchset, text):
    return {"author": author, "date": f"2026-10-{when} 10:00:00.000000000",
            "_revision_number": patchset, "message": text}


CHANGE = {
    "owner": OWNER,
    "current_revision": "c",
    "revisions": {"b": {"_number": 3}, "c": {"_number": 4}},
    "attention_set": {"3": {"account": BOBIJAM}},
    "reviewers": {
        "REVIEWER": [OWNER, ANDREAS, BOBIJAM, JENKINS],
        "CC": [AIREVIEW],
        "REMOVED": [OLEG],
    },
    "labels": {
        "Code-Review": {"all": [
            dict(ANDREAS, value=1, date="2026-10-04 10:00:00.000000000"),
            dict(BOBIJAM, value=0),
            dict(OWNER, value=0),
        ]},
        "Verified": {"all": [dict(JENKINS, value=1)]},
    },
    "reviewer_updates": [
        {"updated": "2026-10-01 10:00:00.000000000", "updated_by": OWNER,
         "reviewer": ANDREAS, "state": "REVIEWER"},
        {"updated": "2026-10-01 10:00:00.000000000", "updated_by": OWNER,
         "reviewer": OLEG, "state": "REVIEWER"},
        {"updated": "2026-10-02 10:00:00.000000000", "updated_by": OWNER,
         "reviewer": OLEG, "state": "REMOVED"},
        {"updated": "2026-10-03 10:00:00.000000000", "updated_by": OWNER,
         "reviewer": BOBIJAM, "state": "REVIEWER"},
        {"updated": "2026-10-03 11:00:00.000000000", "updated_by": AIREVIEW,
         "reviewer": AIREVIEW, "state": "CC"},
    ],
    "messages": [
        _message(OWNER, "01", 1, "Uploaded patch set 1."),
        _message(OLEG, "02", 1, "Patch Set 1:\n\nNot me."),
        _message(ANDREAS, "02", 3, "Patch Set 3: Code-Review+1\n\nLooks right."),
        _message(JENKINS, "04", 4, "Patch Set 4: Verified+1\n\nBuild Successful"),
        _message(ANDREAS, "05", 4, "Patch Set 4:\n\n(1 comment)"),
        _message(AIREVIEW, "06", 4, "Patch Set 4: Code-Review-1"),
        _message(AIREVIEW, "07", 4, "Patch Set 4: -Code-Review"),
    ],
}

COMMENTS = {
    "lustre/llite/file.c": [
        {"author": ANDREAS, "updated": "2026-10-05 10:00:00.000000000",
         "patch_set": 4, "line": 120, "message": "Why   not\nll_foo()?"},
    ],
}


def test_message_votes_reads_only_label_votes_from_the_first_line():
    assert message_votes("Patch Set 4: Code-Review+1 Verified-1\n\nok",
                         LABELS) == {"Code-Review": 1, "Verified": -1}
    assert message_votes("Patch Set 4: -Code-Review", LABELS) == {"Code-Review": 0}
    assert message_votes("Uploaded patch set 2: Verified+1.", LABELS) == {"Verified": 1}
    assert message_votes("Patch Set 2: Commit message was updated.", LABELS) == {}
    assert message_votes("Patch Set 2:\n\nCode-Review+1 would be nice", LABELS) == {}
    assert message_votes("Patch Set 2: Something+1", LABELS) == {}


def test_activity_tells_a_copied_vote_from_one_cast_on_this_patchset():
    current, people = reviewer_activity(CHANGE, COMMENTS)
    assert current == 4
    andreas = people[1]
    assert andreas["role"] == "REVIEWER"
    assert andreas["votes"] == {"Code-Review": 1}
    # Cast on patchset 3 and carried to 4 by Gerrit.
    assert andreas["copied_votes"] == ["Code-Review"]
    assert andreas["has_current_vote"] is True
    assert andreas["voted_on_current_patchset"] is False
    assert andreas["last_vote"] == {"date": "2026-10-02 10:00:00.000000000",
                                    "patchset": 3, "labels": {"Code-Review": 1}}
    jenkins = people[8]
    assert jenkins["votes"] == {"Verified": 1}
    assert jenkins["voted_on_current_patchset"] is True
    assert jenkins["copied_votes"] == []


def test_last_activity_is_the_latest_message_or_inline_comment():
    _current, people = reviewer_activity(CHANGE, COMMENTS)
    andreas = people[1]
    # The inline comment and its "(1 comment)" message carry the same date;
    # the comment says more.
    assert andreas["last_activity"] == {
        "kind": "comment", "date": "2026-10-05 10:00:00.000000000",
        "patchset": 4, "file": "lustre/llite/file.c", "line": 120,
        "text": "Why not ll_foo()?",
    }
    assert andreas["message_count"] == 2
    assert andreas["comment_count"] == 1
    # A removed vote is still the latest vote, and leaves none standing.
    ai = people[7]
    assert ai["role"] == "CC"
    assert ai["last_vote"]["labels"] == {"Code-Review": 0}
    assert ai["votes"] == {}
    assert ai["has_current_vote"] is False
    # Asked, never answered.
    bobijam = people[3]
    assert bobijam["last_activity"] is None
    assert bobijam["in_attention_set"] is True
    assert people[5]["owner"] is True


class FakeClient:
    def __init__(self, change=CHANGE, comments_fail=False):
        self.change = change
        self.comments_fail = comments_fail

    @staticmethod
    def parse_gerrit_url(url):
        return "https://review.whamcloud.com", 68747

    def get_reviewers(self, change_number):
        return [
            dict(OWNER, approvals={"Code-Review": " 0"}),
            dict(ANDREAS, approvals={"Code-Review": "+1"}),
            dict(BOBIJAM, approvals={"Code-Review": " 0"}),
            dict(JENKINS, approvals={"Verified": "+1"}),
            dict(AIREVIEW, approvals={"Code-Review": " 0"}),
        ]

    def get_change(self, change_number, options=None):
        assert "REVIEWER_UPDATES" in options and "MESSAGES" in options
        return self.change

    def get_comments(self, change_number):
        if self.comments_fail:
            raise RuntimeError("no comments")
        return COMMENTS


def _run(monkeypatch, capsys, client):
    monkeypatch.setattr(cli, "GerritCommentsClient", lambda *a, **k: client)
    monkeypatch.setattr(cli.GerritCommentsClient, "parse_gerrit_url",
                        FakeClient.parse_gerrit_url, raising=False)
    with pytest.raises(SystemExit) as done:
        cmd_reviewers(SimpleNamespace(url="68747", pretty=False))
    assert done.value.code == 0
    out = json.loads(capsys.readouterr().out)
    return out.get("data", out)


def test_reviewers_output_keeps_the_old_fields_and_adds_role_and_activity(
    monkeypatch, capsys,
):
    data = _run(monkeypatch, capsys, FakeClient())
    assert data["count"] == 5
    assert data["current_patchset"] == 4
    assert data["reviewer_count"] == 4
    assert data["cc_count"] == 1
    by_name = {r["username"]: r for r in data["reviewers"]}
    andreas = by_name["adilger"]
    for old in ("account_id", "name", "email", "username", "approvals",
                "added_at", "added_by"):
        assert old in andreas
    assert andreas["approvals"] == {"Code-Review": "+1"}
    assert andreas["added_at"] == "2026-10-01 10:00:00.000000000"
    assert andreas["role"] == "REVIEWER"
    assert andreas["last_activity"]["kind"] == "comment"
    assert by_name["aireview"]["role"] == "CC"
    assert by_name["jenkins"]["service_user"] is True
    assert by_name["adilger"]["service_user"] is False
    # Oleg was removed: not among the reviewers, listed apart.
    assert "green" not in by_name
    assert data["removed"] == [{
        "account_id": 2, "name": "Oleg Drokin", "email": "",
        "username": "green", "removed_at": "2026-10-02 10:00:00.000000000",
        "removed_by": "mvef",
        "last_activity": {"kind": "message",
                          "date": "2026-10-02 10:00:00.000000000",
                          "patchset": 1, "tag": None,
                          "text": "Patch Set 1: Not me."},
    }]
    assert "inline_comments_unavailable" not in data


def test_reviewers_still_answers_without_inline_comments(monkeypatch, capsys):
    data = _run(monkeypatch, capsys, FakeClient(comments_fail=True))
    assert data["inline_comments_unavailable"] is True
    andreas = {r["username"]: r for r in data["reviewers"]}["adilger"]
    assert andreas["last_activity"]["kind"] == "message"
    assert andreas["comment_count"] == 0


BOT_OWNER = {"_account_id": 20, "name": "Patrick Farrell Bot",
             "username": "patrickbot", "email": "bot@example.com"}
PATRICK = {"_account_id": 21, "name": "Patrick Farrell",
           "username": "pfarrell", "email": "Patrick@Example.com"}
RISCV = {"_account_id": 22, "name": "Lustre RISC-V Builder",
         "username": "lustre-riscv-builder"}
RISCV_BY_USERNAME = {"_account_id": 23, "name": "riscv",
                     "username": "lustre-riscv-builder"}


class AuthorIsACcClient(FakeClient):
    """A bot owns the change; the commit's author is only a CC."""

    def __init__(self):
        super().__init__(change={
            "owner": BOT_OWNER,
            "current_revision": "c",
            "revisions": {"c": {"_number": 2, "commit": {"author": {
                "name": "Patrick Farrell", "email": "patrick@example.com"}}}},
            "reviewers": {"REVIEWER": [BOT_OWNER, ANDREAS, RISCV,
                                       RISCV_BY_USERNAME],
                          "CC": [PATRICK, AIREVIEW]},
            "labels": {}, "reviewer_updates": [], "messages": [],
        })

    def get_change(self, change_number, options=None):
        assert "CURRENT_COMMIT" in options
        return super().get_change(change_number, options)

    def get_reviewers(self, change_number):
        return [dict(person, approvals={}) for person in
                (BOT_OWNER, ANDREAS, RISCV, RISCV_BY_USERNAME, PATRICK,
                 AIREVIEW)]

    def get_comments(self, change_number):
        return {}


def test_reviewers_mark_owner_commit_author_and_bots(monkeypatch, capsys):
    """An agent took the commit author, listed as a CC with owner false, for
    someone not yet on the change, and had to classify aireview and the
    RISC-V builder (service_user false) as bots by hand."""
    data = _run(monkeypatch, capsys, AuthorIsACcClient())
    by_name = {r["username"]: r for r in data["reviewers"]}

    patrick = by_name["pfarrell"]
    assert patrick["role"] == "CC"
    assert patrick["commit_author"] is True
    assert patrick["owner"] is False
    assert by_name["patrickbot"]["owner"] is True
    assert by_name["patrickbot"]["commit_author"] is False
    assert data["commit_author"] == {"name": "Patrick Farrell",
                                     "email": "patrick@example.com"}
    assert data["owner"]["username"] == "patrickbot"

    assert by_name["aireview"]["service_user"] is False
    assert by_name["aireview"]["is_bot"] is True
    assert {r["account_id"] for r in data["reviewers"] if r["is_bot"]} == {
        7, 20, 22, 23}
    assert by_name["adilger"]["is_bot"] is False
    # Andreas and Patrick.  Patrickbot owns the change but is an agent,
    # not a person: counting it made 63315 look full at 5 with 4 people.
    assert by_name["patrickbot"]["is_bot"] is True
    assert data["human_participant_count"] == 2


def test_reviewers_is_bot_follows_the_service_user_tag(monkeypatch, capsys):
    data = _run(monkeypatch, capsys, FakeClient())
    by_name = {r["username"]: r for r in data["reviewers"]}
    assert by_name["jenkins"]["is_bot"] is True
    assert by_name["aireview"]["is_bot"] is True
    # mvef, adilger, bobijam
    assert data["human_participant_count"] == 3
    # No commit in the change detail: no author to name.
    assert data["commit_author"] is None
    assert not any(r["commit_author"] for r in data["reviewers"])


def test_find_user_marks_bots(monkeypatch, capsys):
    """find-user says which matches are bots, by the same list as reviewers."""
    from gerrit_cli.commands.reviewers import cmd_find_user

    class SuggestClient:
        def suggest_accounts(self, query, limit=10):
            return [
                {"_account_id": 7, "name": "Gerrit AI review for Lustre",
                 "username": "aireview"},
                {"_account_id": 23, "name": "Lustre RISC-V Builder",
                 "username": "lustre-riscv-builder"},
                {"_account_id": 3, "name": "Andreas Dilger",
                 "username": "adilger", "email": "adilger@example.com"},
            ]

    monkeypatch.setattr(cli, "GerritCommentsClient", lambda *a, **k: SuggestClient())
    with pytest.raises(SystemExit) as done:
        cmd_find_user(SimpleNamespace(query="a", limit=10, pretty=False))
    assert done.value.code == 0
    out = json.loads(capsys.readouterr().out)
    users = {u["username"]: u for u in out.get("data", out)["users"]}
    assert users["aireview"]["is_bot"] is True
    assert users["lustre-riscv-builder"]["is_bot"] is True
    assert users["adilger"]["is_bot"] is False


def test_agent_accounts_are_participants_not_ci():
    """Patrickbot is no person for a reviewer count, but its messages are
    real replies, so is_bot (CI noise) still leaves it alone."""
    from gerrit_cli.ci_parse import is_bot, is_bot_participant
    bot = {"name": "Patrick Farrell Bot", "username": "patrickbot"}
    assert is_bot_participant(bot) is True
    assert is_bot(bot) is False
    assert is_bot_participant({"name": "Patrick Farrell",
                               "username": "pfarrell"}) is False


class PatrickbotAsRegisteredClient(FakeClient):
    """Gerrit gives Patrickbot's username as registered, capitalised, and
    it is a CC on someone else's change (67507)."""

    PATRICKBOT = {"_account_id": 30, "name": "patrick bot",
                  "username": "Patrickbot"}

    def __init__(self):
        super().__init__(change={
            "owner": ANDREAS,
            "current_revision": "c",
            "revisions": {"c": {"_number": 2, "commit": {"author": {
                "name": "Andreas Dilger", "email": "adilger@example.com"}}}},
            "reviewers": {"REVIEWER": [ANDREAS, BOBIJAM, JENKINS],
                          "CC": [self.PATRICKBOT]},
            "labels": {}, "reviewer_updates": [], "messages": [],
        })

    def get_reviewers(self, change_number):
        return [dict(person, approvals={}) for person in
                (ANDREAS, BOBIJAM, JENKINS, self.PATRICKBOT)]

    def get_comments(self, change_number):
        return {}


def test_reviewers_patrickbot_is_no_person_whatever_its_case(monkeypatch, capsys):
    """67507: Patrickbot came back is_bot false, and the count a reviewer
    cap uses included it and the owner, so the run counted by hand."""
    data = _run(monkeypatch, capsys, PatrickbotAsRegisteredClient())
    by_name = {r["username"]: r for r in data["reviewers"]}
    assert by_name["Patrickbot"]["is_bot"] is True
    # Andreas and bobijam.
    assert data["human_participant_count"] == 2
    # bobijam: Andreas owns it.
    assert data["people_besides_owner"] == 1


def _add_reviewer_client(matches, added):
    class AddClient:
        @staticmethod
        def parse_gerrit_url(url):
            return "https://review.whamcloud.com", int(url)

        def suggest_accounts(self, query, limit=5):
            return matches

        def search_accounts(self, name=None, limit=5):
            return []

        def add_reviewer(self, change, reviewer, state="REVIEWER"):
            added.append((change, reviewer, state))
            return {}
    return AddClient


def test_add_reviewer_takes_the_exact_username_among_fuzzy_matches(
        monkeypatch, capsys):
    """add-reviewer LiX picks account LiX, not the other Li* suggestions."""
    from gerrit_cli.commands.reviewers import cmd_add_reviewer

    added = []
    AddClient = _add_reviewer_client([
        {"_account_id": 1, "name": "Lixin Liu", "username": "lixinliu"},
        {"_account_id": 2, "name": "Xi Li", "username": "LiX",
         "email": "lixi@example.com"},
        {"_account_id": 3, "name": "Li Xiao", "username": "lixiao"},
    ], added)
    monkeypatch.setattr(cli, "GerritCommentsClient", AddClient)
    with pytest.raises(SystemExit) as done:
        cmd_add_reviewer(SimpleNamespace(url="70550", names=["lix"], cc=False,
                                         dry_run=False, pretty=False))
    assert done.value.code == 0
    assert added == [(70550, "LiX", "REVIEWER")]


def test_add_reviewer_still_asks_when_nothing_matches_exactly(
        monkeypatch, capsys):
    from gerrit_cli.commands.reviewers import cmd_add_reviewer

    added = []
    AddClient = _add_reviewer_client([
        {"_account_id": 1, "name": "Lixin Liu", "username": "lixinliu"},
        {"_account_id": 3, "name": "Li Xiao", "username": "lixiao"},
    ], added)
    monkeypatch.setattr(cli, "GerritCommentsClient", AddClient)
    with pytest.raises(SystemExit) as done:
        cmd_add_reviewer(SimpleNamespace(url="70550", names=["Li"], cc=False,
                                         dry_run=False, pretty=False))
    assert done.value.code != 0
    assert added == []
    out = json.loads(capsys.readouterr().out)
    assert out.get("data", out)["error"] == "multiple_matches"


def test_add_reviewer_adds_several_and_reports_gerrits_answer(
        monkeypatch, capsys):
    from gerrit_cli.commands.reviewers import cmd_add_reviewer

    people = {
        "ann": {"_account_id": 1, "name": "Ann", "username": "ann"},
        "bob": {"_account_id": 2, "name": "Bob", "username": "bob"},
    }
    added = []

    class AddClient:
        parse_gerrit_url = staticmethod(lambda url: (None, int(url)))

        def suggest_accounts(self, name, limit=5):
            return [people[name]]

        def add_reviewer(self, change, reviewer, state="REVIEWER"):
            added.append(reviewer)
            # Gerrit answers an add of someone already there with nothing.
            return ({"reviewers": [people["ann"]]} if reviewer == "ann"
                    else {"input": reviewer})

    monkeypatch.setattr(cli, "GerritCommentsClient", AddClient)
    with pytest.raises(SystemExit) as done:
        cmd_add_reviewer(SimpleNamespace(url="70550", names=["ann", "bob"],
                                         cc=False, dry_run=False, pretty=False))
    assert done.value.code == 0
    assert sorted(added) == ["ann", "bob"]
    out = json.loads(capsys.readouterr().out)
    results = {a["username"]: a["result"] for a in out.get("data", out)["added"]}
    assert results == {"ann": "added_reviewer", "bob": "already_on_change"}


def test_add_reviewer_adds_nobody_when_one_name_is_ambiguous(
        monkeypatch, capsys):
    from gerrit_cli.commands.reviewers import cmd_add_reviewer

    added = []

    class AddClient:
        parse_gerrit_url = staticmethod(lambda url: (None, int(url)))

        def suggest_accounts(self, name, limit=5):
            if name == "ann":
                return [{"_account_id": 1, "name": "Ann", "username": "ann"}]
            return [{"_account_id": 2, "name": "Li A", "username": "lia"},
                    {"_account_id": 3, "name": "Li B", "username": "lib"}]

        def add_reviewer(self, change, reviewer, state="REVIEWER"):
            added.append(reviewer)
            return {}

    monkeypatch.setattr(cli, "GerritCommentsClient", AddClient)
    with pytest.raises(SystemExit) as done:
        cmd_add_reviewer(SimpleNamespace(url="70550", names=["ann", "Li"],
                                         cc=False, dry_run=False, pretty=False))
    assert done.value.code != 0
    assert added == []
