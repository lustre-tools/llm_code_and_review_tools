"""`gerrit reviewers` says when each reviewer was added.

A run deciding whether a reviewer asked 14 days ago has gone quiet had no
dates from `reviewers` or `info`, and read Gerrit's REST API by hand.
"""

import json
from types import SimpleNamespace

import pytest

from gerrit_cli import cli
from gerrit_cli.commands.reviewers import cmd_reviewers, reviewer_since

ANDREAS = {"_account_id": 1, "name": "Andreas Dilger", "username": "adilger"}
OLEG = {"_account_id": 2, "name": "Oleg Drokin", "username": "green"}
BOT = {"_account_id": 9, "name": "Patrickbot", "username": "patrickbot"}

UPDATES = [
    {"updated": "2026-09-01 10:00:00.000000000", "updated_by": BOT,
     "reviewer": ANDREAS, "state": "CC"},
    {"updated": "2026-09-03 10:00:00.000000000", "updated_by": BOT,
     "reviewer": ANDREAS, "state": "REVIEWER"},
    {"updated": "2026-09-04 10:00:00.000000000", "updated_by": ANDREAS,
     "reviewer": ANDREAS, "state": "REVIEWER"},
    {"updated": "2026-09-02 10:00:00.000000000", "updated_by": BOT,
     "reviewer": OLEG, "state": "REVIEWER"},
    {"updated": "2026-09-05 10:00:00.000000000", "updated_by": BOT,
     "reviewer": OLEG, "state": "REMOVED"},
    {"updated": "2026-09-20 10:00:00.000000000", "updated_by": OLEG,
     "reviewer": OLEG, "state": "REVIEWER"},
]


def test_the_date_is_when_the_current_state_began():
    since = reviewer_since(UPDATES)
    # CC first, then asked to review: the request to review is what counts,
    # and voting later (the same state again) does not move it.
    assert since[1] == ("2026-09-03 10:00:00.000000000", "patrickbot")
    # Removed and added back: the clock starts again.
    assert since[2] == ("2026-09-20 10:00:00.000000000", "green")


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail

    @staticmethod
    def parse_gerrit_url(url):
        return "https://review.whamcloud.com", 62577

    def get_reviewers(self, change_number):
        return [dict(ANDREAS, approvals={"Code-Review": "0"}), dict(OLEG, approvals={})]

    def get_reviewer_updates(self, change_number):
        if self.fail:
            raise RuntimeError("no reviewer updates")
        return UPDATES


@pytest.mark.parametrize("fail", [False, True])
def test_reviewers_carries_the_dates_and_still_answers_without_them(
    monkeypatch, capsys, fail,
):
    client = FakeClient(fail)
    monkeypatch.setattr(cli, "GerritCommentsClient", lambda *a, **k: client)
    monkeypatch.setattr(cli.GerritCommentsClient, "parse_gerrit_url",
                        FakeClient.parse_gerrit_url, raising=False)
    with pytest.raises(SystemExit) as done:
        cmd_reviewers(SimpleNamespace(url="62577", pretty=False))
    assert done.value.code == 0
    out = json.loads(capsys.readouterr().out)
    data = out.get("data", out)
    first = data["reviewers"][0]
    if fail:
        assert "added_at" not in first
    else:
        assert first["added_at"] == "2026-09-03 10:00:00.000000000"
        assert first["added_by"] == "patrickbot"
