"""`gerrit info` must never hide a negative vote, whoever cast it.

The bot filter exists to drop "Looks good to me" chatter from Maloo and
Jenkins.  It also dropped wc-checkpatch's standing -1 on a change that could
not be cherry-picked to master, so `gerrit info` reported a vetoed change as
carrying only two +1s.  The only way to see the veto was --show-bots, a flag
nobody following the documented workflow would think to pass.
"""

from gerrit_cli.commands.ci import _has_negative_vote, _info_for_change


class FakeClient:
    """Just enough of GerritCommentsClient for _info_for_change."""

    def get_change_detail(self, change_number):
        return {
            "current_revision": "abc",
            "revisions": {
                "abc": {
                    "_number": 4,
                    "created": "2019-06-27 12:00:00.000000000",
                    "uploader": {"name": "Patrick Farrell"},
                }
            },
        }

    def get_reviewers(self, change_number):
        return [
            # A bot veto: must always be shown.
            {"name": "wc-checkpatch", "approvals": {"Verified": " 0", "Code-Review": "-1"}},
            # Bot chatter: hidden by default, shown with --show-bots.
            {"name": "Maloo", "approvals": {"Verified": " 0", "Code-Review": " 0"}},
            {"name": "Jenkins", "approvals": {"Verified": "+1"}},
            # Humans are never filtered.
            {"name": "Andreas Dilger", "approvals": {"Verified": " 0", "Code-Review": "+1"}},
            # No approvals at all: dropped whatever the name.
            {"name": "Lurker", "approvals": {}},
        ]

    def get_messages(self, change_number):
        return []


def _names(show_bots):
    data = _info_for_change(FakeClient(), 35302, show_bots=show_bots)
    return [r["name"] for r in data["reviewers"]]


def test_bot_veto_is_shown_without_show_bots():
    names = _names(show_bots=False)
    assert "wc-checkpatch" in names
    assert "Andreas Dilger" in names


def test_bot_chatter_is_still_hidden_by_default():
    names = _names(show_bots=False)
    assert "Maloo" not in names
    assert "Jenkins" not in names
    assert "Lurker" not in names


def test_show_bots_reveals_everything_with_a_vote():
    names = _names(show_bots=True)
    assert {"wc-checkpatch", "Maloo", "Jenkins", "Andreas Dilger"} <= set(names)
    assert "Lurker" not in names


def test_has_negative_vote_reads_gerrit_string_values():
    assert not _has_negative_vote({})
    assert not _has_negative_vote({"Code-Review": " 0"})
    assert not _has_negative_vote({"Code-Review": "+1", "Verified": "+1"})
    assert _has_negative_vote({"Code-Review": "-1"})
    assert _has_negative_vote({"Verified": " 0", "Code-Review": "-2"})
    assert _has_negative_vote({"Verified": "-1", "Code-Review": "+2"})
