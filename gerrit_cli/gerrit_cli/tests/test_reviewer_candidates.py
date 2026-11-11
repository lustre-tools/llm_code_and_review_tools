"""`gerrit reviewer-candidates` ranks who reviewed and wrote a change's files.

Three reviewer-finding runs (fb-3fb03b6dff8f, fb-7627123a9c30,
fb-7fe33960a745) tallied Reviewed-by trailers and authors per touched file
by hand, over several git log passes each time.
"""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from gerrit_cli import cli
from gerrit_cli.commands.reviewer_candidates import (
    Identities, cmd_reviewer_candidates, git_history, normalize_name,
    rank_candidates,
)


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, env=env)


def _commit(repo, path, author, when, reviewers=(), extra=""):
    name, email = author
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as f:
        f.write(f"{when} {name}\n")
    _git(repo, "add", path)
    body = "LU-1 test: change\n\nText.\n\n" + extra
    body += "".join(f"Reviewed-by: {n} <{e}>\n" for n, e in reviewers)
    env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME="Oleg Drokin",
               GIT_COMMITTER_EMAIL="green@whamcloud.com",
               GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    _git(repo, "commit", "-q", "-m", body, env=env)


ANDREAS = ("Andreas Dilger", "adilger@whamcloud.com")
ANDREAS_NEW = ("Andreas Dilger", "adilger@thelustrecollective.com")
JIAN = ("Jian Yu", "yujian@whamcloud.com")
JIAN_ALT = ("jian yu", "jian.yu@example.com")  # another spelling and email
NEIL = ("Neil Brown", "neilb@suse.de")
OWNER = ("Marc Vef", "mvef@example.com")
MAINT = ("Maloo", "maloo@whamcloud.com")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "lustre"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    # Old enough to fall outside a 12 month window.
    _commit(repo, "lustre/llite/file.c", NEIL, "2015-01-01T00:00:00",
            reviewers=[JIAN, JIAN, JIAN])
    _commit(repo, "lustre/llite/file.c", NEIL, "2026-09-01T00:00:00",
            reviewers=[ANDREAS, JIAN, MAINT])
    _commit(repo, "lustre/llite/file.c", OWNER, "2026-09-02T00:00:00",
            reviewers=[ANDREAS_NEW, NEIL],
            extra="Tested-by: jenkins <devops@whamcloud.com>\n")
    _commit(repo, "lustre/llite/namei.c", JIAN_ALT, "2026-09-03T00:00:00",
            reviewers=[ANDREAS])
    _commit(repo, "lustre/tests/sanity.sh", NEIL, "2026-09-04T00:00:00",
            reviewers=[JIAN])
    _commit(repo, "lustre/other.c", JIAN, "2026-09-05T00:00:00",
            reviewers=[NEIL])
    return repo


def test_normalize_name_ignores_case_accents_and_punctuation():
    assert normalize_name("Sébastien  Buisson") == "sebastien buisson"
    assert normalize_name("o'Neil, J.") == "o neil j"


def test_identities_merge_by_email_and_by_name():
    ids = Identities()
    ids.add("Andreas Dilger", "adilger@whamcloud.com")
    ids.add("Andreas Dilger", "adilger@dilger.ca")
    ids.add("A. Dilger", "ADILGER@dilger.ca")
    root = ids.lookup("", "adilger@whamcloud.com")
    assert ids.lookup("A. Dilger", "") == root
    assert ids.lookup("andreas dilger", "") == root
    assert ids.lookup("Someone Else", "x@y") is None


def test_history_reads_trailers_and_files_within_the_window(repo):
    commits = git_history(str(repo), "HEAD",
                          ["lustre/llite/file.c", "lustre/llite/namei.c"], 12)
    assert len(commits) == 3  # the 2015 one is outside, other.c not asked
    first = commits[-1]
    assert first["name"] == "Neil Brown"
    assert first["files"] == ["lustre/llite/file.c"]
    assert ("Andreas Dilger", "adilger@whamcloud.com") in first["reviewers"]
    # Tested-by is not a review.
    assert all(n != "jenkins" for c in commits for n, _e in c["reviewers"])
    assert len(git_history(str(repo), "HEAD", ["lustre/llite/file.c"], 240)) == 3


def test_rank_merges_people_leaves_out_owner_and_bots_marks_reviewers(repo):
    commits = git_history(str(repo), "HEAD",
                          ["lustre/llite/file.c", "lustre/llite/namei.c"], 12)
    candidates, excluded = rank_candidates(
        commits,
        exclude=[("Marc Vef", "", "owner")],
        on_change=[("Neil Brown", "", "CC")],
    )
    by_name = {c["name"]: c for c in candidates}
    andreas = by_name["Andreas Dilger"]
    # Two emails, one person: three reviews across both files.
    assert andreas["reviewed"] == 3 and andreas["authored"] == 0
    assert andreas["score"] == 6
    assert andreas["file_count"] == 2
    assert sorted(andreas["aliases"]["emails"]) == [
        "adilger@thelustrecollective.com", "adilger@whamcloud.com"]
    assert andreas["last_reviewed"] == "2026-09-03"
    assert andreas["on_change"] is None
    # "jian yu" with another email is Jian Yu.
    jian = by_name["Jian Yu"]
    assert jian["reviewed"] == 1 and jian["authored"] == 1
    assert {f["path"] for f in jian["files"]} == {
        "lustre/llite/file.c", "lustre/llite/namei.c"}
    neil = by_name["Neil Brown"]
    assert (neil["reviewed"], neil["authored"]) == (1, 1)
    assert neil["on_change"] == "CC"
    assert candidates[0]["name"] == "Andreas Dilger"
    reasons = {e["name"]: e["reason"] for e in excluded}
    assert reasons == {"Marc Vef": "owner", "Maloo": "bot"}
    assert "Maloo" not in by_name


class FakeClient:
    """Gerrit's answer for a change touching file.c and renaming namei.c."""

    @staticmethod
    def parse_gerrit_url(url):
        return "https://review.whamcloud.com", 69999

    def get_change(self, change_number, options=None):
        assert "CURRENT_FILES" in options and "DETAILED_LABELS" in options
        return {
            "project": "fs/lustre-release", "branch": "master",
            "subject": "LU-1 llite: thing",
            "owner": {"_account_id": 9, "name": "patrick bot"},
            "current_revision": "abc",
            "revisions": {"abc": {
                "_number": 2,
                "commit": {"author": {"name": "Marc Vef",
                                      "email": "mvef@example.com"}},
                "files": {
                    "/COMMIT_MSG": {},
                    "lustre/llite/file.c": {"lines_inserted": 3},
                    "lustre/llite/namei2.c": {"status": "R",
                                              "old_path": "lustre/llite/namei.c"},
                },
            }},
            "reviewers": {
                "REVIEWER": [{"_account_id": 9, "name": "patrick bot"},
                             {"_account_id": 1, "name": "Andreas Dilger"}],
                "CC": [{"_account_id": 2, "name": "Jian Yu"}],
            },
        }


def _run(monkeypatch, capsys, repo, **overrides):
    client = FakeClient()
    monkeypatch.setattr(cli, "GerritCommentsClient", lambda *a, **k: client)
    monkeypatch.setattr(cli.GerritCommentsClient, "parse_gerrit_url",
                        FakeClient.parse_gerrit_url, raising=False)
    args = dict(url="69999", paths=None, repo=str(repo), ref="HEAD",
                months=12, limit=10, exclude=None, brief=False, pretty=False)
    args.update(overrides)
    with pytest.raises(SystemExit) as done:
        cmd_reviewer_candidates(SimpleNamespace(**args))
    out = json.loads(capsys.readouterr().out)
    return done.value.code, out.get("data", out)


def test_change_files_come_from_gerrit_and_the_author_is_left_out(
    monkeypatch, capsys, repo,
):
    code, data = _run(monkeypatch, capsys, repo)
    assert code == 0
    assert data["change_number"] == 69999
    # The rename's old path carries the history.
    assert data["paths"] == ["lustre/llite/file.c", "lustre/llite/namei2.c",
                             "lustre/llite/namei.c"]
    assert data["paths_without_history"] == ["lustre/llite/namei2.c"]
    assert data["commits_examined"] == 3
    names = [c["name"] for c in data["candidates"]]
    assert names[0] == "Andreas Dilger"
    assert "Marc Vef" not in names
    roles = {c["name"]: c["on_change"] for c in data["candidates"]}
    assert roles["Andreas Dilger"] == "REVIEWER"
    assert roles["Jian Yu"] == "CC"
    assert roles["Neil Brown"] is None
    reasons = {e["name"]: e["reason"] for e in data["excluded"]}
    assert reasons["Marc Vef"] == "commit_author"


def test_paths_without_a_change_brief_limit_and_exclude(monkeypatch, capsys, repo):
    code, data = _run(monkeypatch, capsys, repo, url=None,
                      paths=["lustre/llite"], limit=1, brief=True,
                      exclude=["adilger@whamcloud.com"])
    assert code == 0
    assert "change_number" not in data
    assert data["count"] == 1 and data["total_candidates"] >= 2
    top = data["candidates"][0]
    assert top["name"] != "Andreas Dilger"
    assert all(isinstance(f, str) for f in top["files"])
    assert {e["reason"] for e in data["excluded"]} >= {"excluded"}


def test_not_a_checkout_is_invalid_input(monkeypatch, capsys, tmp_path):
    code, data = _run(monkeypatch, capsys, tmp_path, url=None,
                      paths=["x.c"])
    assert code == 4
    assert "not a git checkout" in json.dumps(data)


def test_limit_applies_after_holds_and_the_order_is_stated(
    monkeypatch, capsys, repo, tmp_path,
):
    """fb-8a9735ce7588, fb-1d0066201ba2, fb-cfb93b5c8d57: --limit cut the
    list by score before holds were weighed, so held people filled it and
    pushed out people who could be asked; the order was not stated."""
    asked = tmp_path / "asks.json"
    asked.write_text(json.dumps({"by": "test", "people": [
        {"name": "Andreas Dilger", "email": "adilger@whamcloud.com",
         "asked": 20, "answered": 5, "pending": 10, "asked_14d": 10}]}))
    code, data = _run(monkeypatch, capsys, repo, url=None,
                      paths=["lustre/llite"], limit=1, asked=str(asked))
    assert code == 0
    assert data["count"] == 1
    top = data["candidates"][0]
    assert top["name"] != "Andreas Dilger" and not top["hold"]
    assert data["eligible"] == [top["name"]]
    assert data["order"] == "balanced"
    assert "held people" in data["order_rule"]
    # fb-704a1fad3491, fb-3affa57c47a6: the held person the limit cut
    # is named, not dropped silently.
    assert [h["name"] for h in data["held_omitted"]] == ["Andreas Dilger"]
    assert data["held_omitted"][0]["hold"] == ["10 asks already waiting on them"]


def test_too_few_people_widens_the_window_and_says_so(
    monkeypatch, capsys, repo,
):
    """fb-df2ca5951165, fb-794498c5ba24, fb-388d68bf72d7: a file with no
    history in 12 months ranked nobody, and the run had to think of
    widening --months itself."""
    _commit(repo, "lustre/tests/mmap_sanity.c", NEIL, "2025-03-01T00:00:00",
            reviewers=[ANDREAS, JIAN])
    code, data = _run(monkeypatch, capsys, repo, url=None,
                      paths=["lustre/tests/mmap_sanity.c"], compact=True)
    assert code == 0
    assert data["widened_from"] == 12 and data["window_months"] == 36
    names = {c["name"] for c in data["candidates"]}
    assert names == {"Neil Brown", "Andreas Dilger", "Jian Yu"}
    code, data = _run(monkeypatch, capsys, repo, url=None,
                      paths=["lustre/tests/mmap_sanity.c"], widen_to=0)
    assert "widened_from" not in data and data["candidates"] == []
