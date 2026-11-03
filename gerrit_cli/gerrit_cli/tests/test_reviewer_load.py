"""reviewer-candidates --load and --asked: the same experts were asked for
every change in an area, and some were asked a dozen times without ever
answering."""

import json

import pytest

from gerrit_cli.commands import reviewer_load
from gerrit_cli.commands.reviewer_load import (
    AskedError,
    add_gerrit_load,
    balance,
    read_asked,
    resolve_account,
    weigh,
)

from .test_reviewer_candidates import _run, repo  # noqa: F401


class LoadClient:
    """Accounts by email or name, and a count per query."""

    def __init__(self, accounts, counts):
        self.accounts = accounts      # [{_account_id, name, email}]
        self.counts = counts          # {(account_id, "waiting"|"reviewed"): n}
        self.queries = []

    def search_accounts(self, name=None, email=None, limit=10):
        if email:
            return [{"_account_id": a["_account_id"]} for a in self.accounts
                    if a["email"] == email]
        wanted = name.strip('"').lower()
        return [a for a in self.accounts if wanted in a["name"].lower()]

    def search_all(self, query, max_results=500, page_size=100, options=None):
        self.queries.append(query)
        who = int(query.split("user=")[1].split()[0])
        kind = "waiting" if query.startswith("reviewer:") else "reviewed"
        return [{}] * min(max_results, self.counts.get((who, kind), 0))


def test_account_by_alias_email_then_by_exact_name():
    client = LoadClient(
        [{"_account_id": 117, "name": "Andreas Dilger",
          "email": "adilger@thelustrecollective.com"},
         {"_account_id": 5, "name": "Jian Yu", "email": "yujian@whamcloud.com"},
         {"_account_id": 6, "name": "Jian Yuan", "email": "jy@example.com"}],
        {})
    # The old address is not registered; the newer alias is.
    assert resolve_account(client, {
        "name": "Andreas Dilger", "email": "adilger@whamcloud.com",
        "aliases": {"emails": ["adilger@thelustrecollective.com",
                               "adilger@whamcloud.com"]}}) == 117
    # No email matches: one account whose whole name is theirs.
    assert resolve_account(client, {"name": "Jian Yu",
                                    "email": "jian.yu@other.org"}) == 5
    assert resolve_account(client, {"name": "Nobody", "email": ""}) is None


def test_gerrit_load_counts_queue_and_votes_and_survives_one_failure():
    client = LoadClient(
        [{"_account_id": 931, "name": "Timothy Day", "email": "t@x"}],
        {(931, "waiting"): 81, (931, "reviewed"): 177})
    people = [{"name": "Timothy Day", "email": "t@x", "score": 40},
              {"name": "Ghost", "email": "g@x", "score": 9}]
    add_gerrit_load(client, people, workers=1)
    assert people[0]["account_id"] == 931
    assert people[0]["load"] == {"window_days": 30, "waiting": 81,
                                 "reviewed": 177}
    waiting = next(q for q in client.queries if q.startswith("reviewer:"))
    assert "-owner:931" in waiting and "-is:wip" in waiting
    assert "-label:Code-Review=ANY,user=931" in waiting
    assert people[1]["load"] is None
    assert people[1]["load_error"] == "no unambiguous Gerrit account"


def test_counts_at_gerrits_cap_are_marked():
    client = LoadClient([{"_account_id": 117, "name": "A", "email": "a@x"}],
                        {(117, "waiting"): 900, (117, "reviewed"): 900})
    people = [{"name": "A", "email": "a@x", "score": 1}]
    add_gerrit_load(client, people, workers=1)
    assert people[0]["load"]["capped_at"] == 500


def test_the_expert_with_a_queue_of_asks_yields_to_a_free_one():
    busy = {"name": "Busy", "score": 100, "asked": {
        "asked": 20, "answered": 8, "pending": 5, "asked_14d": 8}}
    free = {"name": "Free", "score": 36, "asked": None}
    novice = {"name": "Novice", "score": 1, "asked": None}
    ordered = balance([busy, free, novice])
    assert [c["name"] for c in ordered] == ["Free", "Busy", "Novice"]
    assert busy["hold"] is None
    assert busy["factors"]["asks"] == round(1 / (1 + (5 + 4) / 3), 2)


def test_holds_go_last_with_their_reasons():
    silent = {"name": "Silent", "score": 400, "asked": {
        "asked": 17, "answered": 0, "pending": 2, "asked_14d": 1}}
    swamped = {"name": "Swamped", "score": 400, "asked": {
        "asked": 29, "answered": 4, "pending": 23, "asked_14d": 18}}
    absent = {"name": "Absent", "score": 400,
              "load": {"window_days": 30, "waiting": 3, "reviewed": 0}}
    fine = {"name": "Fine", "score": 4}
    ordered = balance([silent, swamped, absent, fine])
    assert ordered[0]["name"] == "Fine"
    assert silent["hold"] == ["asked 17 times, answered none"]
    assert swamped["hold"] == ["23 asks already waiting on them"]
    assert absent["hold"] == ["no Code-Review vote on Gerrit in 30 days"]


def test_the_gatekeeper_is_held():
    oleg = {"name": "Oleg Drokin", "score": 422, "reviewed": 207,
            "review_share": 0.96}
    few = {"name": "Few", "score": 6, "reviewed": 3, "review_share": 1.0}
    balance([oleg, few])
    assert oleg["hold"] == ["on nearly every commit: the gatekeeper who lands them"]
    assert few["hold"] is None


def test_someone_who_reviews_a_lot_carries_more_asks_unheld():
    asks = {"asked": 36, "answered": 20, "pending": 13, "asked_14d": 13}
    prolific = {"name": "Andreas", "score": 286, "asked": dict(asks),
                "load": {"window_days": 30, "waiting": 255, "reviewed": 355}}
    sparse = {"name": "Sparse", "score": 286, "asked": dict(asks),
              "load": {"window_days": 30, "waiting": 10, "reviewed": 12}}
    balance([prolific, sparse])
    assert prolific["hold"] is None
    assert prolific["factors"]["asks"] == round(1 / (1 + 19.5 / 35.5), 2)
    assert sparse["hold"] == ["13 asks already waiting on them"]
    # A week of a prolific reviewer's votes does hold them.
    flooded = dict(prolific, asked=dict(asks, pending=90))
    balance([flooded])
    assert flooded["hold"] == ["90 asks already waiting on them"]


def test_never_answering_holds_only_someone_who_reviews_little():
    asks = {"asked": 17, "answered": 0, "pending": 2, "asked_14d": 1}
    busy = {"name": "Busy", "score": 9, "asked": dict(asks),
            "load": {"window_days": 30, "waiting": 30, "reviewed": 150}}
    idle = {"name": "Idle", "score": 9, "asked": dict(asks),
            "load": {"window_days": 30, "waiting": 51, "reviewed": 3}}
    balance([busy, idle])
    assert busy["hold"] is None and busy["factors"]["answers"] == 0.11
    assert idle["hold"] == ["asked 17 times, answered none"]


def test_queue_against_rate():
    _, factors, _ = weigh({"score": 4, "load": {
        "window_days": 30, "waiting": 51, "reviewed": 3}})
    assert factors["queue"] == round(4 / 55, 2)


def test_asked_matched_by_account_email_then_name():
    asked = {"by": "Patrickbot", "people": [
        {"account_id": 117, "name": "A D", "asked": 3, "answered": 3,
         "pending": 0, "asked_14d": 1},
        {"email": "Yujian@Whamcloud.com", "name": "x", "asked": 2,
         "answered": 1, "pending": 1, "asked_14d": 0},
        {"name": "Mikhail Pershin", "asked": 1, "answered": 0,
         "pending": 1, "asked_14d": 1}]}
    a = {"name": "Andreas", "account_id": 117, "score": 9}
    j = {"name": "Jian", "email": "yujian@whamcloud.com", "score": 9}
    m = {"name": "mikhail  pershin", "score": 9}
    n = {"name": "Neil", "score": 9}
    balance([a, j, m, n], asked)
    assert a["asked"]["asked"] == 3
    assert j["asked"]["pending"] == 1
    assert m["asked"]["asked"] == 1
    assert n["asked"] is None


def test_read_asked_refuses_a_bad_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(AskedError):
        read_asked(str(bad))
    with pytest.raises(AskedError):
        read_asked(str(tmp_path / "missing.json"))


def test_command_orders_by_balanced_with_asked(
    monkeypatch, capsys, repo, tmp_path,  # noqa: F811
):
    code, plain = _run(monkeypatch, capsys, repo)
    assert code == 0 and plain["order"] == "score"
    top = plain["candidates"][0]
    asks = tmp_path / "asks.json"
    asks.write_text(json.dumps({"by": "Patrickbot", "generated_at": "t",
                                "people": [{
                                    "email": top["email"], "name": top["name"],
                                    "asked": 9, "answered": 0, "pending": 7,
                                    "asked_14d": 3}]}))
    code, data = _run(monkeypatch, capsys, repo, asked=str(asks))
    assert code == 0
    assert data["order"] == "balanced" and data["asked_by"] == "Patrickbot"
    assert data["candidates"][-1]["name"] == top["name"]
    assert data["candidates"][-1]["hold"]
    code, data = _run(monkeypatch, capsys, repo,
                      asked=str(tmp_path / "none.json"))
    assert code == 4


def test_command_reads_load_through_the_client(monkeypatch, capsys, repo):  # noqa: F811
    seen = []
    monkeypatch.setattr(reviewer_load, "add_gerrit_load",
                        lambda client, people: seen.append(len(people)))
    code, data = _run(monkeypatch, capsys, repo, load=True)
    assert code == 0 and data["order"] == "balanced"
    assert seen == [data["count"]]
