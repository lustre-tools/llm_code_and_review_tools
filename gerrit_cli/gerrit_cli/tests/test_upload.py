"""Tests for `gerrit upload`, against local bare repositories.

Nothing here talks to a real Gerrit.  Most tests push to a bare
repository through a file:// GERRIT_PUSH_URL, with the REST lookups
answered by FakeGerrit.  The end-to-end test runs the real CLI against
a local HTTP server that fronts `git http-backend` with Basic auth, so
the askpass path, a password containing '/' and --user are exercised
the way a run uses them.
"""

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from gerrit_cli.errors import ErrorCode, ExitCode
from gerrit_cli.upload import (
    ASKPASS_PASS_VAR,
    PUSH_URL_VAR,
    REDACTED,
    UploadError,
    _transport_error,
    push_url,
    upload,
)

PROJECT = "fs/lustre-release"
OPERATOR = ("Patrick Farrell", "patrick@thelustrecollective.com")
BOT = ("patrick bot", "patrick-bot@mulberrytree.us")
OWNER = ("Patch Owner", "owner@example.com")
BOT_USER = "Patrickbot"
BOT_PASS = "bot/pass+w=rd"


def change_id(name: str) -> str:
    return "I" + hashlib.sha1(name.encode()).hexdigest()


CID_A = change_id("a")
CID_B = change_id("b")
CID_NEW = change_id("new")


def git(repo, *args, env=None, input=None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, input=input,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(
    repo, subject, cid=None, committer=OPERATOR, author=OWNER,
    filename=None, extra_trailer=None,
):
    path = Path(repo) / (filename or re.sub(r"\W+", "_", subject))
    path.write_text(subject + "\n")
    git(repo, "add", path.name)
    message = f"{subject}\n\nBody of {subject}.\n\nSigned-off-by: {author[0]} <{author[1]}>\n"
    if extra_trailer:
        message += extra_trailer + "\n"
    if cid:
        message += f"Change-Id: {cid}\n"
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_AUTHOR_DATE": "@1700000000 +0200",
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    })
    git(repo, "commit", "-q", "--cleanup=verbatim", "-F", "-",
        env=env, input=message)
    return git(repo, "rev-parse", "HEAD")


class FakeGerrit:
    """The REST calls upload makes, answered from a dict and a bare repo.

    A ref pushed under refs/for/ is absorbed the way Gerrit would: a new
    patchset of the open change with that Change-Id on that branch, or a
    new change.
    """

    def __init__(self, url, bare, username=BOT_USER, password=BOT_PASS):
        self.url = url
        self.bare = bare
        self.username = username
        self.password = password
        self.account = {
            "_account_id": 1000, "name": BOT[0], "email": BOT[1],
            "username": BOT_USER,
        }
        self.emails = [{"email": BOT[1], "preferred": True}]
        self.changes: dict[int, dict] = {}
        self.absorbed: set[str] = set()
        self.queries: list[str] = []

    @property
    def authenticated(self):
        return bool(self.username and self.password)

    def add_change(self, number, cid, branch="master", status="NEW",
                   revisions=None, subject=None, project=PROJECT):
        revisions = revisions or {"a" * 40: 1}
        self.changes[number] = {
            "_number": number, "project": project, "branch": branch,
            "change_id": cid, "status": status,
            "subject": subject or f"change {number}",
            "revisions": {sha: {"_number": n} for sha, n in revisions.items()},
            "current_revision": max(revisions, key=revisions.get),
        }

    def _absorb(self):
        refs = git(self.bare, "for-each-ref", "refs/for/",
                   "--format=%(objectname) %(refname)")
        for line in refs.splitlines():
            tip, ref = line.split(" ", 1)
            branch = ref[len("refs/for/"):].split("%")[0]
            shas = git(self.bare, "rev-list", "--reverse", tip,
                       "--not", "--branches").split()
            for sha in shas:
                if sha in self.absorbed:
                    continue
                self.absorbed.add(sha)
                if any(sha in c["revisions"] for c in self.changes.values()):
                    continue
                self._absorb_one(sha, branch)

    def _absorb_one(self, sha, branch):
        cid = git(self.bare, "log", "-1",
                  "--format=%(trailers:key=Change-Id,valueonly)", sha)
        for change in self.changes.values():
            if change["change_id"] == cid and change["branch"] == branch:
                ps = max(r["_number"] for r in change["revisions"].values())
                change["revisions"][sha] = {"_number": ps + 1}
                change["current_revision"] = sha
                return
        number = max(self.changes, default=90000) + 1
        self.add_change(number, cid, branch=branch, revisions={sha: 1})

    def _summary(self, change, with_revisions):
        out = {k: v for k, v in change.items()
               if k not in ("revisions", "current_revision")}
        if with_revisions:
            out["revisions"] = copy.deepcopy(change["revisions"])
            out["current_revision"] = change["current_revision"]
        return out

    def search_changes(self, query, limit=25, start=0, options=None):
        self._absorb()
        self.queries.append(query)
        with_revisions = "ALL_REVISIONS" in (options or [])
        shas = re.findall(r"commit:([0-9a-f]+)", query)
        if shas:
            return [
                self._summary(c, with_revisions)
                for c in self.changes.values()
                if set(shas) & set(c["revisions"])
            ]
        terms = dict(t.split(":", 1) for t in query.split())
        found = [
            c for c in self.changes.values()
            if c["change_id"].startswith(terms["change"])
            and terms.get("project", c["project"]) == c["project"]
            and terms.get("branch", c["branch"]) == c["branch"]
        ]
        return [self._summary(c, with_revisions) for c in found][:limit]

    def get_change(self, number, options=None):
        self._absorb()
        if number not in self.changes:
            raise RuntimeError(f"404 Not Found: change {number}")
        return self._summary(self.changes[number], True)

    def get_self_account(self):
        return dict(self.account)

    def get_self_emails(self):
        return copy.deepcopy(self.emails)


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Keep the host's git config and identity out of every test."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", PUSH_URL_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    return config


def make_remote(root: Path) -> tuple[Path, Path]:
    """A bare repo at root/fs/lustre-release with master, and a clone."""
    bare = root / "remotes" / PROJECT
    bare.parent.mkdir(parents=True)
    git(root, "init", "-q", "--bare", "-b", "master", str(bare))
    seed = root / "seed"
    git(root, "init", "-q", "-b", "master", str(seed))
    commit(seed, "base", committer=("Upstream", "upstream@example.com"))
    git(seed, "push", "-q", str(bare), "master")
    work = root / "work"
    git(root, "clone", "-q", str(bare), str(work))
    return bare, work


@pytest.fixture
def gerrit(tmp_path, monkeypatch):
    bare, work = make_remote(tmp_path)
    monkeypatch.setenv(PUSH_URL_VAR, f"file://{tmp_path / 'remotes'}")
    fake = FakeGerrit("https://review.example.com", bare)
    fake.add_change(51164, CID_A, revisions={"a" * 40: 1, "b" * 40: 2},
                    subject="LU-1 llite: the change")
    return fake, bare, work


def pushed_refs(bare) -> dict[str, str]:
    out = git(bare, "for-each-ref", "refs/for/",
              "--format=%(refname) %(objectname)")
    return dict(line.split(" ") for line in out.splitlines())


# ---------------------------------------------------------------------------
# Resolution and the Change-Id guard
# ---------------------------------------------------------------------------

def test_uploads_the_next_patchset_of_the_named_change(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), change="51164")

    assert data["pushed"] is True
    assert data["sha"] == head
    assert data["change_number"] == 51164
    assert data["patchset"] == 3
    assert data["new_change"] is False
    assert data["url"] == (
        "https://review.example.com/c/fs/lustre-release/+/51164"
    )
    assert data["committer_amended"] == []
    assert data["account"] == BOT_USER
    assert pushed_refs(bare) == {"refs/for/master": head}


def test_the_change_can_be_named_by_url_or_change_id(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    by_url = upload(
        fake, repo=str(work), dry_run=True,
        change="https://review.example.com/c/fs/lustre-release/+/51164",
    )
    by_cid = upload(fake, repo=str(work), change=CID_A, dry_run=True)

    assert by_url["change_number"] == by_cid["change_number"] == 51164


def test_refuses_a_head_carrying_another_changes_change_id(gerrit):
    """The guard for runs whose HEAD picked up a stray Change-Id."""
    fake, bare, work = gerrit
    fake.add_change(60000, CID_B, subject="LU-2 osc: someone else's")
    head = commit(work, "LU-1 llite: fix", cid=CID_B, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    e = err.value
    assert e.code == ErrorCode.CHANGE_ID_MISMATCH
    assert CID_A in e.message and CID_B in e.message
    assert "60000" in e.message
    assert "Nothing was pushed" in e.message
    assert e.details["head_change_id"] == CID_B
    assert pushed_refs(bare) == {}
    assert git(work, "rev-parse", "HEAD") == head


def test_refuses_a_head_without_a_change_id_when_a_change_is_named(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    assert err.value.code == ErrorCode.CHANGE_ID_MISMATCH
    assert "no Change-Id" in err.value.message
    assert pushed_refs(bare) == {}


def test_without_change_head_change_id_finds_the_change(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work))

    assert data["change_number"] == 51164
    assert data["patchset"] == 3


def test_without_change_head_needs_a_change_id(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work))

    assert err.value.code == ErrorCode.NO_CHANGE_ID


def test_an_unknown_change_id_needs_project_and_branch(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-3 mdt: new work", cid=CID_NEW, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), branch="master")

    assert err.value.code == ErrorCode.CHANGE_NOT_FOUND
    assert "--project and --branch" in err.value.message
    assert pushed_refs(bare) == {}


def test_uploads_a_new_change_with_project_and_branch(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-3 mdt: new work", cid=CID_NEW, committer=BOT)

    data = upload(fake, repo=str(work), project=PROJECT, branch="master")

    assert data["new_change"] is True
    assert data["change_number"] > 51164
    assert data["commits"][-1]["action"] == "create"
    assert data["patchset"] == 1
    assert data["sha"] == head
    assert pushed_refs(bare) == {"refs/for/master": head}


def test_several_open_changes_with_one_change_id_are_ambiguous(gerrit):
    fake, bare, work = gerrit
    fake.add_change(51165, CID_A, branch="b2_15")
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work))
    assert err.value.code == ErrorCode.AMBIGUOUS_CHANGE
    assert "51164" in err.value.message and "51165" in err.value.message

    data = upload(fake, repo=str(work), branch="master", dry_run=True)
    assert data["change_number"] == 51164


def test_branch_that_contradicts_the_named_change_is_refused(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", branch="b2_15")

    assert err.value.exit_code == ExitCode.INVALID_INPUT
    assert "name no change" in err.value.message


@pytest.mark.parametrize("status", ["MERGED", "ABANDONED"])
def test_a_closed_change_is_refused(gerrit, status):
    fake, bare, work = gerrit
    fake.changes[51164]["status"] = status
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    assert err.value.code == ErrorCode.CHANGE_CLOSED
    assert status in err.value.message


def test_head_that_is_already_a_patchset_is_not_reuploaded(gerrit):
    """Otherwise the committer amend would mint a duplicate patchset."""
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A)
    fake.changes[51164]["revisions"][head] = {"_number": 3}

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    assert err.value.code == ErrorCode.NO_NEW_CHANGES
    assert git(work, "rev-parse", "HEAD") == head


def test_two_change_id_trailers_are_refused(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT,
           extra_trailer=f"Change-Id: {CID_B}")

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    assert err.value.code == ErrorCode.MULTIPLE_CHANGE_IDS


# ---------------------------------------------------------------------------
# Committer
# ---------------------------------------------------------------------------

def commit_fields(repo, rev="HEAD"):
    fmt = "%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%T%x00%P%x00%B"
    out = git(repo, "log", "-1", "--date=raw", f"--format={fmt}", rev)
    keys = ["an", "ae", "ad", "cn", "ce", "tree", "parents", "body"]
    return dict(zip(keys, out.split("\x00")))


def test_amends_an_unregistered_committer_on_head_only(gerrit):
    fake, bare, work = gerrit
    old = commit(work, "LU-1 llite: fix", cid=CID_A, committer=OPERATOR)
    before = commit_fields(work)
    (Path(work) / "staged").write_text("not part of the upload\n")
    git(work, "add", "staged")

    data = upload(fake, repo=str(work), change="51164")

    new = git(work, "rev-parse", "HEAD")
    after = commit_fields(work)
    assert new != old
    assert data["sha"] == new
    assert data["committer_amended"] == [{
        "old_sha": old,
        "new_sha": new,
        "subject": "LU-1 llite: fix",
        "from": f"{OPERATOR[0]} <{OPERATOR[1]}>",
        "to": f"{BOT[0]} <{BOT[1]}>",
    }]
    assert (after["cn"], after["ce"]) == BOT
    for key in ("an", "ae", "ad", "tree", "parents", "body"):
        assert after[key] == before[key], key
    # The staged file stayed staged, out of the commit.
    assert "staged" not in git(work, "show", "--name-only", "--format=", "HEAD")
    assert git(work, "diff", "--cached", "--name-only") == "staged"
    assert data["warnings"]
    assert pushed_refs(bare) == {"refs/for/master": new}
    assert git(work, "reflog", "-1", "--format=%gs").startswith(
        "gerrit upload: committer"
    )


def test_no_amend_refuses_instead(gerrit):
    fake, bare, work = gerrit
    old = commit(work, "LU-1 llite: fix", cid=CID_A, committer=OPERATOR)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", amend=False)

    assert err.value.code == ErrorCode.COMMITTER_NOT_REGISTERED
    assert OPERATOR[1] in err.value.message
    assert git(work, "rev-parse", "HEAD") == old
    assert pushed_refs(bare) == {}


def test_a_parent_gerrit_already_has_is_not_checked(gerrit):
    """A series parent already uploaded is not validated again."""
    fake, bare, work = gerrit
    parent = commit(work, "LU-2 osc: parent", cid=CID_B, committer=OPERATOR)
    fake.add_change(60000, CID_B, revisions={parent: 4})
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=OPERATOR)

    data = upload(fake, repo=str(work), change="51164")

    assert data["pushed"] is True
    assert [a["new_sha"] for a in data["committer_amended"]] == [data["sha"]]
    assert [c["action"] for c in data["commits"]] == ["none", "update"]
    assert git(work, "rev-parse", "HEAD~1") == parent


# ---------------------------------------------------------------------------
# More than one commit, and --series
# ---------------------------------------------------------------------------

def test_more_than_one_new_commit_is_refused_and_names_series(gerrit):
    fake, bare, work = gerrit
    parent = commit(work, "LU-2 osc: parent", cid=CID_B, committer=BOT)
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    e = err.value
    assert e.code == ErrorCode.MULTIPLE_COMMITS
    assert "--series" in e.message
    assert "2 commits ahead" in e.message
    for sha, subject, cid in (
        (parent, "LU-2 osc: parent", CID_B),
        (head, "LU-1 llite: fix", CID_A),
    ):
        assert sha[:12] in e.message
        assert subject in e.message
        assert cid in e.message
    assert [c["sha"] for c in e.details["commits"]] == [parent, head]
    assert pushed_refs(bare) == {}


def test_named_change_below_head_points_at_series(gerrit):
    fake, bare, work = gerrit
    fake.add_change(60000, CID_B, subject="LU-2 osc: parent")
    commit(work, "LU-2 osc: parent", cid=CID_B, committer=BOT)
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="60000")

    assert err.value.code == ErrorCode.CHANGE_ID_MISMATCH
    assert "HEAD~1" in err.value.message
    assert "--series" in err.value.message


def test_series_uploads_each_commit_to_its_own_change(gerrit):
    fake, bare, work = gerrit
    parent = commit(work, "LU-2 osc: parent", cid=CID_B, committer=BOT)
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), series=True)

    assert data["pushed"] is True and data["series"] is True
    assert data["committer_amended"] == []
    assert pushed_refs(bare) == {"refs/for/master": head}
    first, second = data["commits"]
    assert (first["sha"], first["action"], first["patchset"]) == (
        parent, "create", 1
    )
    assert first["change_number"] == fake.search_changes(
        f"change:{CID_B}")[0]["_number"]
    assert (second["sha"], second["action"], second["change_number"],
            second["patchset"]) == (head, "update", 51164, 3)
    assert (data["change_number"], data["patchset"]) == (51164, 3)


def test_series_named_change_may_be_below_head(gerrit):
    fake, bare, work = gerrit
    fake.add_change(60000, CID_B, subject="LU-2 osc: parent")
    parent = commit(work, "LU-2 osc: parent", cid=CID_B, committer=BOT)
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), change="60000", series=True)

    assert pushed_refs(bare) == {"refs/for/master": head}
    assert [(c["sha"], c["change_number"], c["patchset"])
            for c in data["commits"]] == [
        (parent, 60000, 2), (head, 51164, 3),
    ]


def test_series_refuses_a_named_change_that_is_not_in_it(gerrit):
    fake, bare, work = gerrit
    fake.add_change(60000, CID_B, subject="LU-2 osc: elsewhere")
    commit(work, "LU-3 mdt: parent", cid=CID_NEW, committer=BOT)
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="60000", series=True)

    assert err.value.code == ErrorCode.CHANGE_ID_MISMATCH
    assert CID_B in err.value.message
    assert CID_NEW in err.value.message and CID_A in err.value.message
    assert pushed_refs(bare) == {}


def test_series_refuses_a_commit_without_a_change_id(gerrit):
    fake, bare, work = gerrit
    parent = commit(work, "LU-2 osc: no trailer", committer=BOT)
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), series=True)

    assert err.value.code == ErrorCode.NO_CHANGE_ID
    assert parent[:12] in err.value.message
    assert "LU-2 osc: no trailer" in err.value.message
    assert pushed_refs(bare) == {}


def test_series_rewrites_the_chain_above_a_foreign_committer(gerrit):
    fake, bare, work = gerrit
    base = git(work, "rev-parse", "HEAD")
    parent = commit(work, "LU-2 osc: parent", cid=CID_B, committer=OPERATOR)
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)
    before = {sha: commit_fields(work, sha) for sha in (parent, head)}

    data = upload(fake, repo=str(work), series=True)

    new_head = git(work, "rev-parse", "HEAD")
    new_parent = git(work, "rev-parse", "HEAD~1")
    assert [(a["old_sha"], a["new_sha"]) for a in data["committer_amended"]] == [
        (parent, new_parent), (head, new_head),
    ]
    assert commit_fields(work, new_parent)["parents"] == base
    assert commit_fields(work, new_head)["parents"] == new_parent
    for old, new in ((parent, new_parent), (head, new_head)):
        after = commit_fields(work, new)
        assert (after["cn"], after["ce"]) == BOT
        for key in ("an", "ae", "ad", "tree", "body"):
            assert after[key] == before[old][key], key
    assert [c["old_sha"] for c in data["commits"]] == [parent, head]
    assert pushed_refs(bare) == {"refs/for/master": new_head}


def test_series_dry_run_says_what_each_commit_would_do(gerrit):
    fake, bare, work = gerrit
    known = commit(work, "LU-4 lov: already up", cid=change_id("known"),
                   committer=OPERATOR)
    fake.add_change(70000, change_id("known"), revisions={known: 1})
    commit(work, "LU-3 mdt: new", cid=CID_NEW, committer=OPERATOR)
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), series=True, dry_run=True)

    assert [(c["action"], c.get("change_number")) for c in data["commits"]] == [
        ("none", None), ("create", None), ("update", 51164),
    ]
    assert [a["subject"] for a in data["committer_amended"]] == [
        "LU-3 mdt: new", "LU-1 llite: fix",
    ]
    assert git(work, "rev-parse", "HEAD") == head
    assert pushed_refs(bare) == {}


def test_series_refuses_a_commit_whose_change_is_merged(gerrit):
    fake, bare, work = gerrit
    fake.add_change(60000, CID_B, status="MERGED")
    commit(work, "LU-2 osc: parent", cid=CID_B, committer=BOT)
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), series=True)

    assert err.value.code == ErrorCode.CHANGE_CLOSED
    assert "LU-2 osc: parent" in err.value.message


# ---------------------------------------------------------------------------
# Dry run, push options, failures
# ---------------------------------------------------------------------------

def test_dry_run_checks_everything_and_changes_nothing(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=OPERATOR)

    data = upload(fake, repo=str(work), change="51164", dry_run=True)

    assert data["dry_run"] is True and data["pushed"] is False
    assert data["current_patchset"] == 2
    assert data["committer_amended"][0]["new_sha"] is None
    assert data["push"]["command"].startswith(
        "git -c credential.helper= push --porcelain file://"
    )
    assert data["push"]["command"].endswith(
        "'<HEAD after committer amend>:refs/for/master'"
    )
    assert data["push"]["env"][ASKPASS_PASS_VAR] == REDACTED
    assert BOT_PASS not in json.dumps(data)
    assert git(work, "rev-parse", "HEAD") == head
    assert pushed_refs(bare) == {}


def test_dry_run_prints_the_sha_it_would_push(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), change="51164", dry_run=True,
                  topic="lu-1-fix")

    assert data["push"]["command"].endswith(
        f"{head}:refs/for/master%topic=lu-1-fix"
    )


def test_topic_is_sent_as_a_push_option(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), change="51164", topic="lu-1-fix")

    assert data["ref"] == "refs/for/master%topic=lu-1-fix"
    assert pushed_refs(bare) == {"refs/for/master%topic=lu-1-fix": head}


def test_a_topic_that_cannot_be_a_push_option_is_refused(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", topic="a,b")

    assert err.value.exit_code == ExitCode.INVALID_INPUT


def test_a_rejected_push_carries_gerrits_message(gerrit):
    fake, bare, work = gerrit
    hook = Path(bare) / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'ERROR: commit 1234567: missing subsystem in subject' >&2\n"
        "exit 1\n"
    )
    hook.chmod(0o755)
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    e = err.value
    assert e.code == ErrorCode.PUSH_REJECTED
    assert "missing subsystem in subject" in e.message
    assert e.details["rejection"].startswith("[remote rejected]")
    assert any("missing subsystem" in m for m in e.details["remote_messages"])


def test_a_url_rewrite_that_would_bypass_https_is_refused(
    gerrit, isolated_git
):
    fake, bare, work = gerrit
    isolated_git.write_text(
        '[url "ssh://pfarrell2@review.example.com:29418/"]\n'
        "\tpushInsteadOf = file://\n"
    )
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", dry_run=True)

    assert err.value.code == ErrorCode.CONFIG_ERROR
    assert "pushinsteadof" in err.value.message.lower()


def test_needs_credentials(gerrit):
    fake, bare, work = gerrit
    fake.password = None
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164")

    assert err.value.exit_code == ExitCode.AUTH_ERROR


def test_push_url_names_the_account_and_never_a_password(monkeypatch):
    monkeypatch.delenv(PUSH_URL_VAR, raising=False)
    assert push_url("https://review.whamcloud.com", PROJECT, "Patrickbot") == (
        "https://Patrickbot@review.whamcloud.com/a/fs/lustre-release"
    )
    monkeypatch.setenv(PUSH_URL_VAR, "https://u:pw@example.com/a")
    with pytest.raises(UploadError):
        push_url("https://review.whamcloud.com", PROJECT, "u")


def test_git_auth_failures_are_reported_as_auth_errors():
    error = _transport_error(
        "fatal: Authentication failed for 'https://x/a/p/'", "https://x",
        "Patrickbot",
    )
    assert error.code == ErrorCode.AUTH_FAILED
    assert error.exit_code == ExitCode.AUTH_ERROR


# ---------------------------------------------------------------------------
# End to end: the CLI, over HTTP, with Basic auth
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Gerrit's /a/ REST endpoints plus git smart HTTP via http-backend."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        self.server.users_seen.append(user)
        return (user, password) == (BOT_USER, BOT_PASS)

    def _handle(self):
        path, _, query = self.path.partition("?")
        if not path.startswith("/a/"):
            return self._send(404)
        if not self._authorized():
            return self._send(
                401, headers=[("WWW-Authenticate", 'Basic realm="Gerrit"')]
            )
        path = path[len("/a"):]
        if re.search(r"/(info/refs|git-upload-pack|git-receive-pack)$", path):
            return self._git(path, query)
        self._rest(path, query)

    def _rest(self, path, query):
        fake = self.server.fake
        params = parse_qs(query)
        try:
            if path == "/accounts/self":
                body = fake.get_self_account()
            elif path == "/accounts/self/emails":
                body = fake.get_self_emails()
            elif path == "/changes/":
                body = fake.search_changes(
                    params["q"][0], options=params.get("o", [])
                )
            elif re.fullmatch(r"/changes/\d+", path):
                body = fake.get_change(int(path.rsplit("/", 1)[1]))
            else:
                return self._send(404)
        except RuntimeError:
            return self._send(404)
        payload = b")]}'\n" + json.dumps(body).encode()
        self._send(200, payload, [("Content-Type", "application/json")])

    def _git(self, path, query):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PROJECT_ROOT": str(self.server.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": BOT_USER,
            "REMOTE_ADDR": "127.0.0.1",
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
        }
        out = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True,
        ).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode().split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            elif key:
                headers.append((key, value.strip()))
        self._send(status, payload, headers)


@pytest.fixture
def http_gerrit(tmp_path):
    bare, work = make_remote(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    fake = FakeGerrit(url, bare)
    fake.add_change(51164, CID_A, revisions={"a" * 40: 1})
    server.fake = fake
    server.root = tmp_path / "remotes"
    server.users_seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, url, bare, work
    server.shutdown()
    server.server_close()


_RUN_CLI = (
    "import sys\n"
    "sys.argv = ['gerrit'] + sys.argv[1:]\n"
    "from gerrit_cli.cli import main\n"
    "main()\n"
)


def run_cli(tmp_path, url, argv, isolated_git):
    """Run the gerrit CLI as a run would, with a logging git on PATH."""
    env_file = tmp_path / "gerrit.env"
    env_file.write_text(
        f"GERRIT_URL={url}\n"
        "GERRIT_USER=pfarrell2\n"
        "GERRIT_PASS=operator-pass\n"
        "\n[patrickbot]\n"
        f"GERRIT_USER={BOT_USER}\n"
        f"GERRIT_PASS={BOT_PASS}\n"
    )
    # The operator's stored login, which must not be what the push uses.
    isolated_git.write_text(
        "[credential]\n"
        "\thelper = \"!f() { echo username=pfarrell2; "
        "echo password=operator-pass; }; f\"\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "git-argv.log"
    (bindir / "git").write_text(
        "#!/bin/sh\n"
        f"printf '%s ' \"$@\" >> '{argv_log}'\n"
        f"printf '\\n' >> '{argv_log}'\n"
        f"exec '{shutil.which('git')}' \"$@\"\n"
    )
    (bindir / "git").chmod(0o755)

    import gerrit_cli

    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GERRIT_")
    }
    env.update({
        "GERRIT_CLI_ENV_FILE": str(env_file),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(Path(gerrit_cli.__file__).parent.parent),
    })
    result = subprocess.run(
        [sys.executable, "-c", _RUN_CLI, *argv],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return result, argv_log.read_text() if argv_log.exists() else ""


def test_cli_uploads_as_the_selected_account_over_http(
    http_gerrit, tmp_path, isolated_git
):
    server, url, bare, work = http_gerrit
    hook = Path(bare) / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'SUCCESS' >&2\n"
        f"echo '  {url}/c/{PROJECT}/+/51164 LU-1 llite: fix' >&2\n"
    )
    hook.chmod(0o755)
    old = commit(work, "LU-1 llite: fix", cid=CID_A, committer=OPERATOR)

    result, argv_log = run_cli(
        tmp_path, url,
        ["upload", "51164", "--repo", str(work), "--user", "patrickbot"],
        isolated_git,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    new = git(work, "rev-parse", "HEAD")
    assert data["pushed"] is True
    assert data["sha"] == new != old
    assert data["patchset"] == 2
    assert data["committer_amended"][0]["to"] == f"{BOT[0]} <{BOT[1]}>"
    assert "SUCCESS" in data["remote_messages"]
    assert pushed_refs(bare) == {"refs/for/master": new}
    # Every request the server saw was the bot's; the operator's stored
    # login was never offered.
    assert set(server.users_seen) == {BOT_USER}
    # The password never reached a command line or the output.
    assert "push" in argv_log
    assert BOT_PASS not in argv_log
    assert BOT_PASS not in result.stdout + result.stderr


def test_cli_refuses_a_mismatched_change_with_a_json_error(
    http_gerrit, tmp_path, isolated_git
):
    server, url, bare, work = http_gerrit
    commit(work, "LU-2 osc: other", cid=CID_B, committer=BOT)

    result, _ = run_cli(
        tmp_path, url,
        ["--user", "patrickbot", "upload", "51164", "--repo", str(work)],
        isolated_git,
    )

    assert result.returncode == ExitCode.GENERAL_ERROR
    error = json.loads(result.stdout)
    assert error["code"] == ErrorCode.CHANGE_ID_MISMATCH
    assert pushed_refs(bare) == {}


def test_cli_reports_a_wrong_password_as_an_auth_error(
    http_gerrit, tmp_path, isolated_git
):
    server, url, bare, work = http_gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    result, _ = run_cli(
        tmp_path, url,
        ["upload", "51164", "--repo", str(work), "--dry-run"],
        isolated_git,
    )

    # The default set is the operator's, which this server refuses.
    assert result.returncode == ExitCode.AUTH_ERROR, result.stdout
    assert json.loads(result.stdout)["code"] == ErrorCode.AUTH_FAILED

# ---------------------------------------------------------------------------
# --expect-patchset: the change moved on while HEAD was being written
# ---------------------------------------------------------------------------

def test_expect_patchset_uploads_when_the_change_has_not_moved(gerrit):
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    data = upload(fake, repo=str(work), change="51164", expect_patchset=2)

    assert data["pushed"] is True
    assert data["patchset"] == 3
    assert pushed_refs(bare) == {"refs/for/master": head}


def test_expect_patchset_refuses_when_someone_else_uploaded(gerrit):
    """The guard for two uploaders racing on one change."""
    fake, bare, work = gerrit
    head = commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", expect_patchset=1)

    e = err.value
    assert e.code == ErrorCode.STALE_PATCHSET
    assert e.details["expected_patchset"] == 1
    assert e.details["current_patchset"] == 2
    assert e.details["change_number"] == 51164
    assert pushed_refs(bare) == {}
    assert git(work, "rev-parse", "HEAD") == head


def test_expect_patchset_refuses_a_dry_run_too(gerrit):
    """A dry run reports what would happen, and a refusal is what would."""
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), change="51164", expect_patchset=1,
               dry_run=True)

    assert err.value.code == ErrorCode.STALE_PATCHSET


def test_expect_patchset_is_refused_with_series(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-1 llite: fix", cid=CID_A, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), series=True, expect_patchset=2)

    assert err.value.code == ErrorCode.INVALID_INPUT
    assert "--series" in err.value.message
    assert pushed_refs(bare) == {}


def test_expect_patchset_is_refused_for_a_new_change(gerrit):
    fake, bare, work = gerrit
    commit(work, "LU-9 lnet: brand new", cid=CID_B, committer=BOT)

    with pytest.raises(UploadError) as err:
        upload(fake, repo=str(work), project=PROJECT, branch="master",
               expect_patchset=1)

    assert err.value.code == ErrorCode.INVALID_INPUT
    assert "new change" in err.value.message
    assert pushed_refs(bare) == {}



def test_upload_parser_defines_what_the_handler_reads():
    import argparse

    from gerrit_cli import cli
    from gerrit_cli.parsers import setup_parsers

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    setup_parsers(subparsers, cli.build_handlers())

    bare = parser.parse_args(["upload"])
    assert bare.func is cli.cmd_upload
    assert (bare.change, bare.series, bare.dry_run, bare.no_amend,
            bare.repo, bare.expect_patchset) == (
        None, False, False, False, ".", None,
    )

    full = parser.parse_args([
        "upload", "51164", "--series", "--dry-run", "--no-amend",
        "--topic", "t", "--repo", "/r", "--branch", "b", "--project", "p",
        "--expect-patchset", "3",
    ])
    assert (full.change, full.series, full.dry_run, full.no_amend,
            full.topic, full.repo, full.branch, full.project,
            full.expect_patchset) == (
        "51164", True, True, True, "t", "/r", "b", "p", 3,
    )
