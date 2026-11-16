"""gc conflicts end to end: canned REST payloads plus the bare-repo Gerrit."""

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest

from gerrit_cli.commands._helpers import error_code_for
from gerrit_cli.commands.conflicts import run_conflicts
from gerrit_cli.conflicts import ConflictCheckError, ConflictRepoError, Patch, Repo
from gerrit_cli.errors import ErrorCode
from gerrit_cli.tests.test_graph_conflicts import (  # noqa: F401
    _ENV, _git, _queue, _text, change_id, gerrit, local,
)

NOW = datetime(2026, 11, 11, 12, 0, 0)
OWNER = {"_account_id": 1, "name": "Olive Owner", "username": "olive"}


def vote(value, name, aid):
    return {"value": value, "name": name, "username": name.lower(), "_account_id": aid}


READY = {"Verified": {"all": [vote(1, "jenkins", 90), vote(1, "Maloo", 91)]},
         "Code-Review": {"all": [vote(1, "Ann", 2), vote(1, "Bob", 3)]}}
PENDING = {"Verified": {"all": [vote(1, "jenkins", 90)]}, "Code-Review": {"all": []}}
VETO = {"Verified": {"all": []},
        "Code-Review": {"all": [vote(-1, "Rita Reviewer", 4)]}}


class FakeRest:
    """Enough of Gerrit's REST API for gc conflicts."""

    def __init__(self, gerrit):
        self.gerrit = gerrit
        self.changes: dict[int, dict] = {}
        self.files: dict[int, set[str]] = {}
        self.calls: list[str] = []

    def add(self, cn, patches, files, labels=PENDING, hashtags=(), topic=None,
            age_days=1, status="NEW", owner=OWNER):
        revs = {}
        for p in patches:
            parent = _git(self.gerrit.path, "rev-parse", f"{p.commit}^")
            revs[p.commit] = {"_number": p.ps, "commit": {
                "parents": [{"commit": parent}], "subject": f"change {cn}",
                "message": f"change {cn}\n\nChange-Id: {change_id(cn)}\n"}}
        cur = patches[-1]
        self.changes[cn] = {
            "_number": cn, "change_id": change_id(cn), "project": "fs/lustre-release",
            "branch": "master", "status": status, "subject": f"change {cn}",
            "owner": owner, "hashtags": list(hashtags), "topic": topic,
            "updated": (NOW - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S.000000000"),
            "labels": labels, "current_revision": cur.commit, "revisions": revs,
        }
        self.files[cn] = set(files)

    def get(self, path):
        self.calls.append(path)
        if m := re.fullmatch(r"/changes/(\d+)/revisions/current/related", path):
            return self._related(int(m[1]))
        if m := re.fullmatch(r"/changes/(\d+)\?.*", path):
            return self._out(self.changes[int(m[1])], all_revs=False)
        if path.startswith("/changes/?"):
            q = unquote(re.search(r"[?&]q=([^&]*)", path)[1])
            return [self._out(c, "ALL_REVISIONS" in path)
                    for c in self.changes.values() if self._match(c, q)]
        raise AssertionError(f"unexpected REST call {path}")

    @staticmethod
    def _out(c, all_revs):
        out = dict(c)
        if not all_revs:
            out["revisions"] = {c["current_revision"]: c["revisions"][c["current_revision"]]}
        return out

    def _match(self, c, q):
        if "status:open" in q and c["status"] != "NEW":
            return False
        for key in ("project", "branch"):
            m = re.search(rf'\b{key}:(?:"([^"]*)"|(\S+))', q)
            if m and c[key] != (m[1] if m[1] is not None else m[2]):
                return False
        if m := re.search(r"(-?)age:(\d+)d", q):
            updated = datetime.strptime(c["updated"][:19], "%Y-%m-%d %H:%M:%S")
            if (NOW - updated > timedelta(days=int(m[2]))) == bool(m[1]):
                return False
        # one backslash each for the query parser and the regex
        files = {re.sub(r"\\(.)", r"\1", re.sub(r"\\(.)", r"\1", f))
                 for f in re.findall(r'file:"\^((?:\\.|[^"\\])*)\$"', q)}
        if files and not files & self.files[c["_number"]]:
            return False
        commits = set(re.findall(r"commit:([0-9a-f]+)", q))
        if commits and not commits & set(c["revisions"]):
            return False
        ids = set(re.findall(r"change:(\w+)", q))
        if ids and not ids & {str(c["_number"]), c["change_id"]}:
            return False
        return bool(files or commits or ids)

    def _entry(self, c, sha):
        rev = c["revisions"][sha]
        return {"_change_number": c["_number"], "_revision_number": rev["_number"],
                "_current_revision_number": c["revisions"][c["current_revision"]]["_number"],
                "status": c["status"], "change_id": c["change_id"],
                "commit": {"commit": sha, "parents": rev["commit"]["parents"]}}

    def _related(self, cn):
        owner = {sha: c for c in self.changes.values() for sha in c["revisions"]}
        c = self.changes[cn]
        below, sha = [], c["revisions"][c["current_revision"]]["commit"]["parents"][0]["commit"]
        while sha in owner:
            below.append(self._entry(owner[sha], sha))
            sha = owner[sha]["revisions"][sha]["commit"]["parents"][0]["commit"]
        above, mine = [], set(c["revisions"])
        while True:
            kids = [k for k in self.changes.values()
                    if k["revisions"][k["current_revision"]]["commit"]["parents"][0]["commit"]
                    in mine]
            if not kids:
                break
            above[:0] = [self._entry(k, k["current_revision"]) for k in kids]
            mine = {k["current_revision"] for k in kids}
        chain = above + [self._entry(c, c["current_revision"])] + below
        return {"changes": chain if len(chain) > 1 else []}


class FakeClient:
    def __init__(self, gerrit):
        self.rest = FakeRest(gerrit)
        self.url = "https://gerrit.invalid"
        self.branch_reads: list[str] = []

    def get_branch_revision(self, project, branch):
        assert project == "fs/lustre-release"
        self.branch_reads.append(branch)
        r = subprocess.run(["git", "-C", str(self.rest.gerrit.path), "rev-parse", "--verify",
                            "--quiet", f"refs/heads/{branch}"], capture_output=True, text=True)
        return r.stdout.strip() or None


@pytest.fixture
def setup(gerrit, local):
    _git(local, "remote", "add", "origin", str(gerrit.path))
    return gerrit, local, FakeClient(gerrit)


def run(client, local, change=100, commit=None, with_=(), all_=False, hunks=False):
    return run_conflicts(client, str(local), str(change), commit, 30, list(with_),
                         all_, hunks, NOW)


def _local_commit(local: Path, parent: str, files: dict[str, str], msg: str) -> str:
    _git(local, "fetch", "-q", "origin", "refs/heads/master")
    env = dict(_ENV, GIT_INDEX_FILE=str(local / ".git" / "scratch-index"))
    _git(local, "read-tree", parent, env=env)
    for path, content in files.items():
        blob = _git(local, "hash-object", "-w", "--stdin", input=content)
        _git(local, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
    tree = _git(local, "write-tree", env=env)
    return _git(local, "commit-tree", tree, "-p", parent, "-m", msg)


def _target(gerrit, rest, edits, **kw):
    t = gerrit.change(100, gerrit.master, {"a.c": _text(edits)})
    rest.add(100, [t], ["a.c"], **kw)
    return t


def test_end_to_end_reports_the_ready_collision_as_target_side(setup):
    gerrit, local, client = setup
    rest = client.rest
    t = _target(gerrit, rest, {5: "T"}, hashtags=["ec_work"])
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    out = run(client, local, hunks=True)
    assert out["status"] == "ok"
    assert out["change"] == 100 and out["patchset"] == 1
    assert out["commit"] == out["tested_commit"] == t.commit
    assert out["tier_of_target"] == "pending" and out["readiness"] == "pending"
    assert out["me"] == "test-user" and out["owner"] == "Olive Owner"
    assert out["on_branch"]["status"] == "clean"
    [c] = out["conflicts"]
    assert c["cn"] == 200 and c["tier"] == "lands-first" and c["readiness"] == "ready"
    assert c["fix_side"] == "target"
    assert c["files"] == ["a.c"]
    assert c["landing"] == {"target_first": "conflict", "candidate_first": "conflict"}
    assert c["as_uploaded"] == {"target_on_theirs": "conflict", "theirs_on_target": "conflict"}
    assert {reg["kind"] for reg in c["hunks"]["landing"]["a.c"]} == {"edit-edit"}
    assert out["counts"] == {"candidates": 1, "after_filters": 1, "tried": 1,
                             "conflicting": 1, "as_uploaded_only": 0, "watch": 0}
    assert out["base"]["on_branch"] is True and out["base"]["rebase"] == "possible"


def test_needs_rebase_gives_null_conflicts(setup):
    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(100, base, {"a.c": _text({5: "T"})})
    rest.add(100, [t], ["a.c"])
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "master", 6: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    out = run(client, local)
    assert out["status"] == "needs-rebase"
    assert out["conflicts"] is None
    assert out["on_branch"]["status"] == "conflict"
    [p] = out["as_uploaded_only"]
    assert p["cn"] == 200 and p["fix_side"] == "target"
    assert p["as_uploaded"]["target_on_theirs"] == "conflict"
    assert "landing" not in p


def test_commit_option_tests_the_local_rearrangement(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    assert [c["cn"] for c in run(client, local)["conflicts"]] == [200]
    moved = _local_commit(local, gerrit.master, {"a.c": _text({15: "T"})},
                          f"change 100 moved\n\nChange-Id: {change_id(100)}")
    out = run(client, local, commit=moved)
    assert out["tested_commit"] == moved
    assert out["conflicts"] == []
    assert out["clean"] == [200]


def test_commit_option_keeps_unknown_commits_in_the_targets_diff(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {9: "T"})
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    first = _local_commit(local, gerrit.master, {"a.c": _text({5: "X"})}, "no change id")
    top = _local_commit(local, first, {"a.c": _text({5: "X", 9: "T"})},
                        f"change 100\n\nChange-Id: {change_id(100)}")
    out = run(client, local, commit=top)
    assert [c["cn"] for c in out["conflicts"]] == [200]


def test_commit_not_in_repo_is_invalid_input(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    with pytest.raises(ConflictRepoError, match="1234567") as exc:
        run(client, local, commit="1234567" * 5 + "1" * 5)
    assert error_code_for(exc.value) == ErrorCode.INVALID_INPUT


def test_commit_stack_without_the_change_is_invalid_input(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    other = _local_commit(local, gerrit.master, {"a.c": _text({5: "O"})},
                          f"other\n\nChange-Id: {change_id(555)}")
    with pytest.raises(ConflictRepoError, match="100"):
        run(client, local, commit=other)


def test_repo_that_is_not_a_clone_of_the_project_is_invalid_input(gerrit, local):
    client = FakeClient(gerrit)
    _target(gerrit, client.rest, {5: "T"})
    with pytest.raises(ConflictRepoError, match="--repo"):
        run(client, local)


def test_no_overlapping_changes_is_empty_ok(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    o = gerrit.change(200, gerrit.master, {"b.c": _text({5: "O"})})
    rest.add(200, [o], ["b.c"], labels=READY)
    out = run(client, local)
    assert out["status"] == "ok"
    assert out["conflicts"] == [] and out["skipped"] == [] and out["clean"] == []
    assert out["counts"]["candidates"] == 0


def test_no_next_branch_means_no_frozen_tier(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    q = gerrit.change(200, gerrit.master, {"a.c": _text({5: "Q"})})
    rest.add(200, [q], ["a.c"], labels=READY)
    out = run(client, local)
    assert out["on_next"] == {"status": "no-next", "branch": "master-next"}
    assert out["conflicts"][0]["tier"] == "lands-first"


def test_queued_change_is_frozen(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    q = gerrit.change(200, gerrit.master, {"a.c": _text({5: "Q"})})
    rest.add(200, [q], ["a.c"], age_days=90)
    _queue(gerrit, [(200, {"a.c": _text({5: "Q"})})])
    out = run(client, local)
    [c] = out["conflicts"]
    assert c["cn"] == 200 and c["tier"] == "frozen" and c["fix_side"] == "target"
    assert out["on_next"]["status"] == "conflict"
    assert out["on_next"]["with"]["cn"] == 200


def test_skipped_lists_reasons(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    v = gerrit.change(200, gerrit.master, {"a.c": _text({5: "V"})})
    rest.add(200, [v], ["a.c"], labels=VETO)
    below = gerrit.change(400, gerrit.master, {"b.c": _text({2: "B"})})
    rest.add(400, [below], ["b.c"], labels=VETO)
    above = gerrit.change(410, below.commit, {"a.c": _text({5: "A"}), "b.c": _text({2: "B"})})
    rest.add(410, [above], ["a.c", "b.c"])
    out = run(client, local)
    assert out["skipped"] == [
        {"cn": 200, "reason": "Code-Review -1 (Rita Reviewer)"},
        {"cn": 410, "reason": "stacked on 400 (Code-Review -1 (Rita Reviewer))"},
    ]
    assert out["conflicts"] == []
    assert out["counts"] == {"candidates": 2, "after_filters": 0, "tried": 0,
                             "conflicting": 0, "as_uploaded_only": 0, "watch": 0}


def test_an_old_unready_change_is_not_a_candidate(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    s = gerrit.change(300, gerrit.master, {"a.c": _text({5: "S"})})
    rest.add(300, [s], ["a.c"], age_days=60)
    out = run(client, local, all_=True)
    assert out["counts"]["candidates"] == 0 and out["conflicts"] == []


def test_other_tier_is_tried_only_with_all(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    o = gerrit.change(200, gerrit.master, {"a.c": _text({5: "O"})})
    rest.add(200, [o], ["a.c"])
    out = run(client, local)
    assert out["not_tried"] == {"other": [200]} and out["conflicts"] == []
    out = run(client, local, all_=True)
    assert out["not_tried"] == {"other": []}
    [c] = out["conflicts"]
    assert c["tier"] == "other" and c["fix_side"] == "candidate"


def test_with_forces_a_skipped_change_in(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    v = gerrit.change(200, gerrit.master, {"a.c": _text({5: "V"})})
    rest.add(200, [v], ["a.c"], labels=VETO)
    out = run(client, local, with_=[200])
    assert out["skipped"] == []
    [c] = out["conflicts"]
    assert c["cn"] == 200 and c["forced"] is True


def test_same_effort_and_candidate_stack_land_together(setup):
    """A candidate stacked on an in-flight change the query does not
    return (too old) lands on top of it."""
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"}, hashtags=["ec_work", "pw_watch"])
    below = gerrit.change(300, gerrit.master, {"a.c": _text({5: "B"}), "b.c": _text({1: "B"})})
    rest.add(300, [below], ["a.c", "b.c"], age_days=60)
    top = gerrit.change(310, below.commit, {"a.c": _text({5: "B", 15: "U"}),
                                            "b.c": _text({1: "B"})})
    rest.add(310, [top], ["a.c", "b.c"], hashtags=["ec_work"])
    out = run(client, local)
    [c] = out["conflicts"]
    assert c["cn"] == 310 and c["tier"] == "same-effort" and c["via"] == 300
    # the collision is 300's, which is not part of the effort
    assert c["fix_side"] == "candidate"
    assert c["hashtags"] == ["ec_work"]


def test_a_collision_in_a_targets_parent_names_it_in_own_via(setup, capsys, monkeypatch):
    """69268 vs 69771: the colliding line is in parent 69267."""
    gerrit, local, client = setup
    rest = client.rest
    p = gerrit.change(90, gerrit.master, {"a.c": _text({5: "P"})})
    rest.add(90, [p], ["a.c"])
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "P"}), "b.c": _text({9: "T"})})
    rest.add(100, [t], ["b.c"])
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    [c] = run(client, local)["conflicts"]
    assert c["cn"] == 200 and c["own_via"] == 90 and c["via"] is None
    assert c["fix_side"] == "target"
    [c] = _cmd(client, local, monkeypatch, capsys)["conflicts"]
    assert c["own_via"] == 90 and "via" not in c


def test_fix_side_of_a_collision_at_via_follows_the_via_change(setup):
    """A ready change on a pending parent: the parent's lines collide,
    so the parent's tier decides who moves."""
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    below = gerrit.change(300, gerrit.master, {"a.c": _text({5: "B"})})
    rest.add(300, [below], ["a.c"])
    top = gerrit.change(310, below.commit, {"a.c": _text({5: "B"}), "b.c": _text({1: "U"})})
    rest.add(310, [top], ["b.c"], labels=READY)
    out = run(client, local, with_=[310])
    c = next(e for e in out["conflicts"] if e["cn"] == 310)
    assert c["tier"] == "lands-first" and c["via"] == 300
    assert c["fix_side"] == "candidate"


def test_a_change_on_the_same_ticket_is_same_effort(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    rest.changes[100]["subject"] = "LU-23456 lnet: fix discovery"
    same = gerrit.change(200, gerrit.master, {"a.c": _text({5: "S"})})
    rest.add(200, [same], ["a.c"])
    rest.changes[200]["subject"] = "LU-23456 tests: discovery"
    other = gerrit.change(300, gerrit.master, {"a.c": _text({15: "O"})})
    rest.add(300, [other], ["a.c"])
    rest.changes[300]["subject"] = "LU-23457 lnet: other"
    out = run(client, local)
    [c] = out["conflicts"]
    assert c["cn"] == 200 and c["tier"] == "same-effort" and c["fix_side"] == "coordinate"
    assert out["not_tried"] == {"other": [300]}


def test_children_are_reported_in_own_stack(setup):
    gerrit, local, client = setup
    rest = client.rest
    t = _target(gerrit, rest, {5: "T"})
    child = gerrit.change(110, t.commit, {"a.c": _text({5: "T", 6: "child"})})
    rest.add(110, [child], ["a.c"])
    out = run(client, local)
    assert out["own_stack"]["children"] == [{"cn": 110, "status": "clean"}]
    assert out["counts"]["candidates"] == 0
    moved = _local_commit(local, gerrit.master, {"a.c": _text({6: "T"})},
                          f"change 100 moved\n\nChange-Id: {change_id(100)}")
    out = run(client, local, commit=moved)
    assert out["own_stack"]["children"] == [
        {"cn": 110, "status": "conflict", "parent": {"cn": 100, "files": ["a.c"]}}]


def test_target_parents_carry_their_tier(setup):
    gerrit, local, client = setup
    rest = client.rest
    p = gerrit.change(90, gerrit.master, {"b.c": _text({2: "P"})})
    rest.add(90, [p], ["b.c"], labels=READY)
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["base"]["parents"] == [{"cn": 90, "tier": "lands-first"}]
    assert out["base"]["rebase"] == "would move ready/queued parents"
    assert out["own_stack"]["parents"] == [{"cn": 90, "status": "clean"}]


def test_output_has_no_user_specific_strings(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    text = json.dumps(run(client, local, hunks=True))
    assert "Marc" not in text and "Bot]" not in text


def test_parser_defaults():
    from gerrit_cli.cli import build_parser
    args = build_parser().parse_args(["conflicts", "100"])
    assert (args.repo, args.commit, args.hunks, args.max_age, args.with_, args.all,
            args.full, args.blocker_tag, args.priority_tag) == \
        (".", None, False, 30, None, False, False, None, None)
    args = build_parser().parse_args(["conflicts", "100", "--blocker-tag", "a_*",
                                      "--blocker-tag", "b"])
    assert args.blocker_tag == ["a_*", "b"]
    args = build_parser().parse_args(["conflicts", "100", "--with", "5", "--with", "6"])
    assert args.with_ == [5, 6]


def test_the_change_argument_is_parsed_by_the_cli_client(setup, monkeypatch):
    import gerrit_cli.cli as cli

    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    seen = []

    class Client:
        @staticmethod
        def parse_gerrit_url(url):
            seen.append(url)
            return "https://gerrit.invalid", 100

    monkeypatch.setattr(cli, "GerritCommentsClient", Client)
    out = run(client, local, change="Iabc")
    assert seen == ["Iabc"] and out["change"] == 100


def test_cmd_prints_invalid_input_for_a_bad_repo(setup, tmp_path, capsys, monkeypatch):
    from gerrit_cli.cli import build_parser
    from gerrit_cli.commands import conflicts as mod

    gerrit, _local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    monkeypatch.setattr(mod, "_client", lambda: client)
    args = build_parser().parse_args(["conflicts", "100", "--repo", str(tmp_path / "x")])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 4
    assert "INVALID_INPUT" in capsys.readouterr().out


def test_cmd_prints_the_result(setup, capsys, monkeypatch):
    from gerrit_cli.cli import build_parser
    from gerrit_cli.commands import conflicts as mod

    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    monkeypatch.setattr(mod, "_client", lambda: client)
    args = build_parser().parse_args(["conflicts", "100", "--repo", str(local)])
    args.func(args)
    assert json.loads(capsys.readouterr().out)["change"] == 100


def test_a_child_on_an_older_patchset_is_still_the_targets_child(setup, monkeypatch):
    from gerrit_cli.commands import conflicts as mod

    gerrit, local, client = setup
    rest = client.rest
    t1 = gerrit.change(100, gerrit.master, {"a.c": _text({5: "T"})})
    t2 = gerrit.change(100, gerrit.master, {"a.c": _text({5: "T2"})}, ps=2)
    rest.add(100, [t1, t2], ["a.c"])
    t3 = gerrit.change(105, t1.commit, {"a.c": _text({5: "T", 9: "sib"})})
    rest.add(105, [t3], ["a.c"])
    child = gerrit.change(110, t1.commit, {"a.c": _text({5: "T", 15: "child"})})
    rest.add(110, [child], ["a.c"])
    seen = {}

    def spy(*a, **k):
        seen.update(mod.check_conflicts.__wrapped__(*a, **k))
        return dict(seen)

    spy.__wrapped__ = mod.check_conflicts
    monkeypatch.setattr(mod, "check_conflicts", spy)
    out = run(client, local)
    assert seen["results"]["110"]["under"] == 100
    assert seen["results"]["105"]["under"] == 100
    assert sorted(out["own_stack"]["children"], key=lambda c: c["cn"]) == [
        {"cn": 105, "status": "clean", "on_old_patchset": {"of": 100, "ps": 1, "current": 2}},
        {"cn": 110, "status": "clean", "on_old_patchset": {"of": 100, "ps": 1, "current": 2}}]
    assert out["counts"]["candidates"] == 0 and out["clean"] == []


def test_a_queued_change_on_a_vetoed_parent_is_never_dropped(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    below = gerrit.change(400, gerrit.master, {"b.c": _text({2: "B"})})
    rest.add(400, [below], ["b.c"], labels=VETO)
    queued = gerrit.change(410, below.commit, {"a.c": _text({5: "Q"}), "b.c": _text({2: "B"})})
    rest.add(410, [queued], ["a.c", "b.c"])
    _queue(gerrit, [(410, {"a.c": _text({5: "Q"})})])
    out = run(client, local)
    assert out["skipped"] == []
    [c] = out["conflicts"]
    assert c["cn"] == 410 and c["tier"] == "frozen"


def test_a_candidate_the_engine_skips_is_listed_in_skipped(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    side = gerrit.commit(gerrit.master, {"b.c": _text({3: "side"})}, "side")
    tree = _git(gerrit.path, "rev-parse", f"{side}^{{tree}}")
    merge = _git(gerrit.path, "commit-tree", tree, "-p", gerrit.master, "-p", side,
                 "-m", f"change 200\n\nChange-Id: {change_id(200)}")
    _git(gerrit.path, "update-ref", "refs/changes/00/200/1", merge)
    rest.add(200, [Patch(200, 1, merge)], ["a.c"], labels=READY)
    out = run(client, local)
    assert out["skipped"] == [{"cn": 200, "reason": "merge commit"}]
    assert out["conflicts"] == [] and out["clean"] == [] and out["errors"] == []
    assert out["already_conflicting"] == []


def test_with_names_a_change_of_the_own_stack(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    out = run(client, local, with_=[100])
    assert out["skipped"] == [{"cn": 100, "reason": "own stack"}]


def test_hunks_for_extracts_only_the_named_candidates_hunks(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    for n in (200, 300):
        p = gerrit.change(n, gerrit.master, {"a.c": _text({5: f"R{n}"})})
        rest.add(n, [p], ["a.c"], labels=READY)
    out = run(client, local, hunks={300})
    by_cn = {c["cn"]: c for c in out["conflicts"]}
    assert set(by_cn) == {200, 300}
    assert "hunks" not in by_cn[200]
    assert by_cn[300]["hunks"]["landing"]["a.c"]


def test_hunks_for_implies_hunks_on_the_command_line(setup, capsys, monkeypatch):
    from gerrit_cli.cli import build_parser
    from gerrit_cli.commands import conflicts as mod

    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    for n in (200, 300):
        p = gerrit.change(n, gerrit.master, {"a.c": _text({5: f"R{n}"})})
        rest.add(n, [p], ["a.c"], labels=READY)
    monkeypatch.setattr(mod, "_client", lambda: client)
    args = build_parser().parse_args(["conflicts", "100", "--repo", str(local),
                                      "--hunks-for", "200"])
    args.func(args)
    by_cn = {c["cn"]: c for c in json.loads(capsys.readouterr().out)["conflicts"]}
    assert "hunks" in by_cn[200] and "hunks" not in by_cn[300]


def test_a_stale_target_reports_base_drift_not_a_collision(setup):
    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(100, base, {"a.c": _text({5: "T"}), "b.c": _text({2: "T"})})
    rest.add(100, [t], ["a.c", "b.c"])
    c = gerrit.change(200, gerrit.master, {"a.c": _text({5: "master"}),
                                           "b.c": _text({15: "C"})})
    rest.add(200, [c], ["b.c"], labels=READY)
    out = run(client, local)
    assert out["status"] == "needs-rebase"
    assert out["as_uploaded_only"] == [] and out["clean"] == [200]
    assert out["counts"]["conflicting"] == 0


def test_a_landed_pair_conflicting_only_as_uploaded_is_in_as_uploaded_only(setup):
    """70145: 16 of its 19 conflicts landed cleanly in both orders."""
    gerrit, local, client = setup
    rest = client.rest
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    stale = gerrit.change(200, old, {"a.c": _text({4: "S"})})
    rest.add(200, [stale], ["a.c"], labels=READY)
    hit = gerrit.change(300, gerrit.master, {"a.c": _text({6: "M", 7: "H"})})
    rest.add(300, [hit], ["a.c"], labels=READY)
    t = gerrit.change(100, gerrit.master, {"a.c": _text({6: "M", 7: "T"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "ok"
    assert [e["cn"] for e in out["conflicts"]] == [300]
    [e] = out["as_uploaded_only"]
    assert e["cn"] == 200 and e["landing"] == {"target_first": "clean",
                                               "candidate_first": "clean"}
    assert e["as_uploaded"]["target_on_theirs"] == "conflict"
    assert out["counts"]["conflicting"] == 1 and out["counts"]["as_uploaded_only"] == 1


def _stale_and_target(gerrit, rest, stale_h, target_h):
    """A ready candidate on an old base that conflicts with the target only
    as uploaded; each side also adds `*_h` to obd_support.h."""
    gerrit.advance({"inc/obd_support.h": _text({})})
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    stale = gerrit.change(200, old, {"a.c": _text({4: "S"}), "inc/obd_support.h": stale_h})
    rest.add(200, [stale], ["a.c", "inc/obd_support.h"], labels=READY)
    t = gerrit.change(100, gerrit.master, {"a.c": _text({6: "M", 7: "T"}),
                                           "inc/obd_support.h": target_h})
    rest.add(100, [t], ["a.c", "inc/obd_support.h"])
    return stale, t


def test_a_silent_collision_is_not_hidden_by_an_as_uploaded_conflict(setup):
    """A duplicate fail_loc on a stale ready candidate is a landing problem."""
    gerrit, local, client = setup
    _stale_and_target(gerrit, client.rest, _text({17: "#define OBD_FAIL_Y 0x1234"}),
                      _text({3: "#define OBD_FAIL_X 0x1234"}))
    out = run(client, local)
    assert out["as_uploaded_only"] == []
    [e] = out["silent_only"]
    assert e["cn"] == 200 and e["silent"][0]["check"] == "duplicate-value"
    assert e["as_uploaded"] == {"target_on_theirs": "conflict", "theirs_on_target": "clean"}
    assert out["counts"]["conflicting"] == 1 and out["counts"]["as_uploaded_only"] == 0


def test_a_landing_error_is_an_error_whatever_as_uploaded_says(setup, monkeypatch):
    from gerrit_cli import conflicts as engine

    gerrit, local, client = setup
    stale, t = _stale_and_target(gerrit, client.rest, _text({17: "S"}), _text({3: "T"}))
    real = engine.Repo.pick

    def pick(self, onto, commit, parent, **kw):
        if (commit, onto) not in ((stale.commit, t.commit), (t.commit, stale.commit)) \
                and commit in (stale.commit, t.commit) and onto != gerrit.master:
            return engine._Pick("error", detail="boom")
        return real(self, onto, commit, parent, **kw)

    monkeypatch.setattr(engine.Repo, "pick", pick)
    out = run(client, local)
    assert out["errors"] == [{"cn": 200, "reason": "trial merge failed"}]
    assert out["as_uploaded_only"] == [] and out["conflicts"] == []


def test_needs_rebase_lists_clean_pairs_in_clean(setup):
    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(100, base, {"a.c": _text({5: "T"})})
    rest.add(100, [t], ["a.c"])
    hit = gerrit.change(200, gerrit.master, {"a.c": _text({5: "master", 6: "R"})})
    rest.add(200, [hit], ["a.c"], labels=READY)
    far = gerrit.change(300, base, {"a.c": _text({15: "F"})})
    rest.add(300, [far], ["a.c"], labels=READY)
    out = run(client, local)
    assert out["status"] == "needs-rebase"
    assert [e["cn"] for e in out["as_uploaded_only"]] == [200]
    assert out["clean"] == [300]
    assert out["counts"]["conflicting"] == 0 and out["counts"]["as_uploaded_only"] == 1


def test_needs_rebase_surfaces_as_uploaded_errors(setup, monkeypatch):
    from gerrit_cli import conflicts as engine

    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(100, base, {"a.c": _text({5: "T"})})
    rest.add(100, [t], ["a.c"])
    r = gerrit.change(200, base, {"a.c": _text({15: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    real = engine.Repo.pick

    def pick(self, onto, commit, parent, **kw):
        if onto == r.commit:
            return engine._Pick("error", detail="boom")
        return real(self, onto, commit, parent, **kw)

    monkeypatch.setattr(engine.Repo, "pick", pick)
    out = run(client, local)
    assert out["errors"] == [{"cn": 200, "reason": "trial merge failed"}]
    assert out["clean"] == [] and out["as_uploaded_only"] == []


def test_a_merged_change_is_invalid_input(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"}, status="MERGED")
    with pytest.raises(ConflictRepoError, match="MERGED") as exc:
        run(client, local)
    assert error_code_for(exc.value) == ErrorCode.INVALID_INPUT
    assert not any("/related" in c for c in client.rest.calls)


def test_hunks_for_a_change_not_tried_is_noted(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY)
    out = run(client, local, hunks={200, 999})
    assert out["notes"] == ["hunks-for 999: not a tried candidate"]
    assert "hunks" in out["conflicts"][0]
    assert "notes" not in run(client, local, hunks={200})


def _old_parent(gerrit, rest, current_edits):
    """Change 90 with PS1 (b.c) and PS2 (`current_edits`); the target on PS1."""
    p1 = gerrit.change(90, gerrit.master, {"b.c": _text({2: "P1"})})
    p2 = gerrit.change(90, gerrit.master, current_edits, ps=2)
    rest.add(90, [p1, p2], sorted(current_edits))
    return p1, p2


def test_target_on_an_older_parent_patchset_that_still_applies(setup):
    gerrit, local, client = setup
    rest = client.rest
    p1, _p2 = _old_parent(gerrit, rest, {"b.c": _text({2: "P2"})})
    t = gerrit.change(100, p1.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P1"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "ok"
    assert out["outdated_parent"] == {"cn": 90, "ps": 1, "current": 2, "status": "clean"}
    assert "rebase_cause" not in out
    assert out["own_stack"]["parents"] == [{"cn": 90, "status": "clean"}]


def test_target_conflicting_with_its_parents_newer_patchset(setup):
    gerrit, local, client = setup
    rest = client.rest
    p1, _p2 = _old_parent(gerrit, rest, {"a.c": _text({5: "P2"})})
    t = gerrit.change(100, p1.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P1"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "needs-rebase" and out["conflicts"] is None
    assert out["rebase_cause"] == "parent"
    assert out["rebase_onto"] == {"cn": 90, "ps": 2}
    assert out["outdated_parent"] == {"cn": 90, "ps": 1, "current": 2,
                                      "status": "conflict", "files": ["a.c"]}


def test_target_conflicting_with_the_branch_names_the_branch(setup):
    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(100, base, {"a.c": _text({5: "T"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "needs-rebase"
    assert out["rebase_cause"] == "branch"
    assert "rebase_onto" not in out and "outdated_parent" not in out


def test_a_parent_on_an_older_patchset_of_the_one_below(setup):
    gerrit, local, client = setup
    rest = client.rest
    g1 = gerrit.change(80, gerrit.master, {"b.c": _text({1: "G1"})})
    g2 = gerrit.change(80, gerrit.master, {"b.c": _text({1: "G2"})}, ps=2)
    rest.add(80, [g1, g2], ["b.c"])
    p = gerrit.change(90, g1.commit, {"b.c": _text({1: "G1", 9: "P"})})
    rest.add(90, [p], ["b.c"])
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "T"}), "b.c": _text({1: "G1", 9: "P"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "ok" and "outdated_parent" not in out
    assert out["own_stack"]["parents"] == [
        {"cn": 80, "status": "clean"},
        {"cn": 90, "status": "clean", "on_old_patchset": {"of": 80, "ps": 1, "current": 2}}]


def test_a_child_on_an_older_patchset_conflicting_with_the_current_one(setup):
    gerrit, local, client = setup
    rest = client.rest
    t1 = gerrit.change(100, gerrit.master, {"a.c": _text({5: "T"})})
    t2 = gerrit.change(100, gerrit.master, {"a.c": _text({5: "T", 6: "T2"})}, ps=2)
    rest.add(100, [t1, t2], ["a.c"])
    child = gerrit.change(110, t1.commit, {"a.c": _text({5: "T", 6: "child"})})
    rest.add(110, [child], ["a.c"])
    out = run(client, local)
    assert out["own_stack"]["children"] == [
        {"cn": 110, "status": "conflict", "parent": {"cn": 100, "files": ["a.c"]},
         "on_old_patchset": {"of": 100, "ps": 1, "current": 2}}]


def test_commit_on_an_older_parent_patchset_is_tried_on_the_current_one(setup):
    gerrit, local, client = setup
    rest = client.rest
    p1, p2 = _old_parent(gerrit, rest, {"a.c": _text({5: "P2"})})
    t = gerrit.change(100, p1.commit, {"a.c": _text({9: "T"}), "b.c": _text({2: "P1"})})
    rest.add(100, [t], ["a.c"])
    _git(local, "fetch", "-q", "origin", "refs/changes/90/90/1")
    mine = _local_commit(local, p1.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P1"})},
                         f"change 100 edited\n\nChange-Id: {change_id(100)}")
    out = run(client, local, commit=mine)
    assert out["tested_commit"] == mine
    assert out["status"] == "needs-rebase"
    assert out["rebase_cause"] == "parent" and out["rebase_onto"] == {"cn": 90, "ps": 2}
    assert out["outdated_parent"] == {"cn": 90, "ps": 1, "current": 2,
                                      "status": "conflict", "files": ["a.c"]}


def test_commit_on_a_locally_edited_parent_keeps_the_local_parent(setup):
    gerrit, local, client = setup
    rest = client.rest
    p1, _p2 = _old_parent(gerrit, rest, {"a.c": _text({5: "P2"})})
    t = gerrit.change(100, p1.commit, {"a.c": _text({9: "T"}), "b.c": _text({2: "P1"})})
    rest.add(100, [t], ["a.c"])
    edited = _local_commit(local, gerrit.master, {"b.c": _text({2: "P1 edited"})},
                           f"change 90 edited\n\nChange-Id: {change_id(90)}")
    mine = _local_commit(local, edited, {"a.c": _text({5: "T"}), "b.c": _text({2: "P1 edited"})},
                         f"change 100 edited\n\nChange-Id: {change_id(100)}")
    out = run(client, local, commit=mine)
    assert out["status"] == "ok"
    assert "outdated_parent" not in out and "rebase_cause" not in out
    assert out["own_stack"]["parents"] == [{"cn": 90, "status": "clean"}]


def test_target_blocked_by_a_parent_on_an_older_patchset(setup):
    gerrit, local, client = setup
    rest = client.rest
    g1 = gerrit.change(80, gerrit.master, {"b.c": _text({1: "G1"})})
    g2 = gerrit.change(80, gerrit.master, {"b.c": _text({1: "G2", 9: "G2"})}, ps=2)
    rest.add(80, [g1, g2], ["b.c"])
    p = gerrit.change(90, g1.commit, {"b.c": _text({1: "G1", 9: "P"})})
    rest.add(90, [p], ["b.c"])
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "T"}), "b.c": _text({1: "G1", 9: "P"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["status"] == "needs-rebase" and out["on_branch"]["status"] == "blocked"
    assert out["rebase_cause"] == "parent"
    assert out["rebase_onto"] == {"cn": 80, "ps": 2, "for": 90}
    assert "outdated_parent" not in out
    assert out["own_stack"]["parents"][1] == {
        "cn": 90, "status": "conflict", "parent": {"cn": 80, "files": ["b.c"]},
        "on_old_patchset": {"of": 80, "ps": 1, "current": 2}}


def test_target_blocked_by_a_parent_conflicting_with_the_branch(setup):
    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.master
    gerrit.advance({"b.c": _text({2: "master"})})
    p = gerrit.change(90, base, {"b.c": _text({2: "P"})})
    rest.add(90, [p], ["b.c"])
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P"})})
    rest.add(100, [t], ["a.c"])
    out = run(client, local)
    assert out["on_branch"]["status"] == "blocked"
    assert out["rebase_cause"] == "branch" and "rebase_onto" not in out


def _cmd(client, local, monkeypatch, capsys, *extra, env=None):
    env = env or {}
    from gerrit_cli.cli import build_parser
    from gerrit_cli.commands import conflicts as mod

    monkeypatch.setattr(mod, "_client", lambda: client)
    for name in ("BLOCKER", "PRIORITY", "IGNORE"):
        monkeypatch.delenv(f"GERRIT_CONFLICTS_{name}_TAGS", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    args = build_parser().parse_args(["conflicts", "100", "--repo", str(local), *extra])
    args.func(args)
    return json.loads(capsys.readouterr().out)


def _busy(gerrit, rest):
    _target(gerrit, rest, {5: "T"})
    long = "LU-1 llite: " + "x" * 100
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], labels=READY,
             hashtags=["pw_shepherd", "218_review_priority", "ec"])
    rest.changes[200]["subject"] = long
    far = gerrit.change(300, gerrit.master, {"a.c": _text({15: "F"})})
    rest.add(300, [far], ["a.c"], labels=READY)
    other = gerrit.change(400, gerrit.master, {"a.c": _text({18: "O"})})
    rest.add(400, [other], ["a.c"])
    return long


def test_default_output_is_compact(setup, capsys, monkeypatch):
    gerrit, local, client = setup
    long = _busy(gerrit, client.rest)
    out = _cmd(client, local, monkeypatch, capsys)
    [c] = out["conflicts"]
    assert c == {"cn": 200, "subject": long[:67] + "...", "owner": "Olive Owner",
                 "tier": "lands-first", "readiness": "ready", "fix_side": "target",
                 "files": ["a.c"], "release_hashtags": ["218_review_priority"],
                 "landing": "conflict/conflict", "as_uploaded": "conflict/conflict"}
    assert out["clean"] == 1 and out["not_tried"] == 1 and out["already_conflicting"] == 0
    assert out["orders"] == {"landing": "target_first/candidate_first",
                             "as_uploaded": "target_on_theirs/theirs_on_target"}
    for key in ("commit", "tested_commit", "tip", "project"):
        assert key not in out
    for key in ("status", "base", "on_branch", "on_next", "own_stack", "skipped",
                "errors", "counts", "silent_only", "branch"):
        assert key in out


def test_full_is_the_report_unchanged(setup, capsys, monkeypatch):
    gerrit, local, client = setup
    _busy(gerrit, client.rest)
    out = _cmd(client, local, monkeypatch, capsys, "--full")
    assert out == json.loads(json.dumps(run(client, local)))
    assert out["clean"] == [300] and out["not_tried"] == {"other": [400]}
    assert out["conflicts"][0]["landing"] == {"target_first": "conflict",
                                              "candidate_first": "conflict"}


def test_full_names_each_pairs_commit_and_patchset(setup, capsys, monkeypatch):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    r1 = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    r2 = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R2"})}, ps=2)
    rest.add(200, [r1, r2], ["a.c"], labels=READY)
    full = _cmd(client, local, monkeypatch, capsys, "--full")
    [c] = full["conflicts"]
    assert c["commit"] == r2.commit and c["ps"] == 2
    [c] = _cmd(client, local, monkeypatch, capsys)["conflicts"]
    assert "commit" not in c and "ps" not in c


def test_compact_keeps_asked_hunks_and_counts_base_drift():
    from gerrit_cli.conflict_report import compact_entry

    e = {"cn": 1, "subject": "s", "owner": "o", "tier": "other", "readiness": "pending",
         "hashtags": ["x_review_blocker", "pw_halt"], "fix_side": "candidate",
         "files": ["a.c"],
         "as_uploaded": {"target_on_theirs": "clean", "theirs_on_target": "conflict"},
         "via": 7, "silent": [], "base_drift": ["b.c", "c.c"], "hunks": {"as_uploaded": {}}}
    assert compact_entry(e) == {
        "cn": 1, "subject": "s", "owner": "o", "tier": "other", "readiness": "pending",
        "fix_side": "candidate", "files": ["a.c"], "release_hashtags": ["x_review_blocker"],
        "as_uploaded": "clean/conflict", "via": 7, "hunks": {"as_uploaded": {}},
        "base_drift": 2}


def test_a_clean_pair_editing_the_same_function_is_in_watch(setup, capsys, monkeypatch):
    from gerrit_cli.tests.test_conflict_functions import C_FILE, _edit

    gerrit, local, client = setup
    rest = client.rest
    base = gerrit.advance({"f.c": C_FILE})
    t = gerrit.change(100, base, {"f.c": _edit({3: "\tint x = 9;"})})
    rest.add(100, [t], ["f.c"])
    w = gerrit.change(200, base, {"f.c": _edit({14: "\treturn -x;"})})
    rest.add(200, [w], ["f.c"], labels=READY)
    far = gerrit.change(300, base, {"f.c": _edit({19: "\treturn 1;"})})
    rest.add(300, [far], ["f.c"], labels=READY)
    out = run(client, local)
    assert out["clean"] == [300] and out["conflicts"] == []
    [e] = out["watch"]
    assert e["cn"] == 200 and e["landing"]["target_first"] == "clean"
    assert e["same_function"] == [{"file": "f.c", "functions": ["foo"]}]
    assert out["counts"]["watch"] == 1 and out["counts"]["conflicting"] == 0
    compact = _cmd(client, local, monkeypatch, capsys)
    assert compact["watch"][0]["same_function"] == e["same_function"]
    assert compact["watch"][0]["landing"] == "clean/clean"
    assert "files" not in compact["watch"][0]  # nothing conflicts


def test_compact_drops_same_function_only_where_the_pair_conflicts():
    from gerrit_cli.conflict_report import compact

    same = [{"file": "a.c", "functions": ["foo"]}]
    pair = {"cn": 1, "tier": "other", "fix_side": "candidate", "same_function": same,
            "as_uploaded": {"target_on_theirs": "clean", "theirs_on_target": "clean"}}
    full = {"conflicts": [dict(pair)], "watch": [dict(pair)],
            "silent_only": [{"cn": 1, "tier": "other", "fix_side": "candidate",
                             "silent": [{"check": "duplicate-test"}], "same_function": same}],
            "commit": "c", "tested_commit": "c"}
    out = compact(full)
    assert "same_function" not in out["conflicts"][0]
    assert out["watch"][0]["same_function"] == same
    assert out["silent_only"][0]["same_function"] == same


def test_release_tags_come_from_the_environment_and_the_options(setup, capsys, monkeypatch):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    r = gerrit.change(200, gerrit.master, {"a.c": _text({5: "R"})})
    rest.add(200, [r], ["a.c"], hashtags=["rel_hot", "218_review_priority"], age_days=60)
    out = _cmd(client, local, monkeypatch, capsys)
    assert out["ranks"]["blocker"] == ["*_review_blocker"]
    [c] = out["conflicts"]
    assert c["tier"] == "lands-first" and c["release_hashtags"] == ["218_review_priority"]
    out = _cmd(client, local, monkeypatch, capsys,
               env={"GERRIT_CONFLICTS_PRIORITY_TAGS": "rel_*"})
    assert out["ranks"]["priority"] == ["rel_*"]
    assert out["conflicts"][0]["release_hashtags"] == ["rel_hot"]
    # an option replaces the environment; the change is then neither
    # release-tagged nor recent, so the age-limited query leaves it out
    out = _cmd(client, local, monkeypatch, capsys, "--priority-tag", "other_*",
               "--blocker-tag", "none", env={"GERRIT_CONFLICTS_PRIORITY_TAGS": "rel_*"})
    assert out["ranks"]["priority"] == ["other_*"] and out["ranks"]["blocker"] == ["none"]
    assert out["conflicts"] == [] and out["counts"]["candidates"] == 0


def test_branch_tips_are_read_once_by_rest(setup, monkeypatch):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    q = gerrit.change(200, gerrit.master, {"a.c": _text({5: "Q"})})
    rest.add(200, [q], ["a.c"], age_days=90)
    _queue(gerrit, [(200, {"a.c": _text({5: "Q"})})])

    def no_ls_remote(*_a, **_k):
        raise AssertionError("ls-remote used")

    monkeypatch.setattr("gerrit_cli.conflicts.Repo.branch_tips", no_ls_remote)
    out = run(client, local)
    assert sorted(client.branch_reads) == ["master", "master-next"]
    assert out["conflicts"][0]["tier"] == "frozen"


def test_branch_tips_fall_back_to_ls_remote(setup):
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    q = gerrit.change(200, gerrit.master, {"a.c": _text({5: "Q"})})
    rest.add(200, [q], ["a.c"], age_days=90)
    _queue(gerrit, [(200, {"a.c": _text({5: "Q"})})])

    def broken(project, branch):
        raise OSError("REST down")

    client.get_branch_revision = broken
    out = run(client, local)
    assert out["conflicts"][0]["tier"] == "frozen"
    assert out["on_next"]["with"]["cn"] == 200


def test_the_branch_and_the_target_stack_come_in_one_fetch(setup, monkeypatch):
    gerrit, local, client = setup
    rest = client.rest
    p = gerrit.change(90, gerrit.master, {"b.c": _text({2: "P"})})
    rest.add(90, [p], ["b.c"])
    t = gerrit.change(100, p.commit, {"a.c": _text({5: "T"}), "b.c": _text({2: "P"})})
    rest.add(100, [t], ["a.c", "b.c"])
    fetches = []
    real = Repo.fetch_refs

    def fetch_refs(self, remote, refs):
        fetches.append(sorted(refs))
        return real(self, remote, refs)

    monkeypatch.setattr(Repo, "fetch_refs", fetch_refs)
    out = run(client, local)
    assert out["on_branch"]["status"] == "clean"
    assert fetches == [["refs/changes/00/100/1", "refs/changes/90/90/1", "refs/heads/master"]]


def test_a_tip_the_remote_does_not_have_is_read_from_the_remote(setup):
    """Gerrit's tip is ahead of what the clone's remote delivers (a
    mirror behind Gerrit): the remote's own tip is used."""
    gerrit, local, client = setup
    rest = client.rest
    _target(gerrit, rest, {5: "T"})
    q = gerrit.change(200, gerrit.master, {"a.c": _text({5: "Q"})})
    rest.add(200, [q], ["a.c"], age_days=90)
    _queue(gerrit, [(200, {"a.c": _text({5: "Q"})})])
    ahead = gerrit.change(999, gerrit.master, {"z.c": "z\n"}).commit
    _git(gerrit.path, "update-ref", "-d", "refs/changes/99/999/1")
    real = client.get_branch_revision

    def lagging(project, branch):
        return ahead if branch == "master" else real(project, branch)

    client.get_branch_revision = lagging
    out = run(client, local)
    assert out["on_branch"]["status"] == "clean"
    [c] = out["conflicts"]
    assert c["cn"] == 200 and c["tier"] == "frozen"


def test_an_undecodable_tips_answer_falls_back_to_ls_remote(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})

    def garbled(project, branch):
        raise ValueError("Expecting value: line 1 column 1")

    client.get_branch_revision = garbled
    assert run(client, local)["on_branch"]["status"] == "clean"


def test_a_tip_the_remote_never_delivers_is_an_error(setup, monkeypatch):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})
    base = gerrit.master
    gerrit.advance({"z.c": "z\n"})
    ahead = gerrit.change(999, gerrit.master, {"y.c": "y\n"}).commit
    _git(gerrit.path, "update-ref", "-d", "refs/changes/99/999/1")
    client.get_branch_revision = lambda project, branch: ahead if branch == "master" else None
    real = Repo.fetch_refs

    def no_branches(self, remote, refs):
        return real(self, remote, [r for r in refs if not r.startswith("refs/heads/")])

    monkeypatch.setattr(Repo, "fetch_refs", no_branches)
    with pytest.raises(ConflictCheckError, match="could not fetch master"):
        run(client, local)
    # with --commit the local stack is walked from the tip before the engine runs
    # built by hand: _local_commit would fetch master into the clone
    blob = _git(local, "hash-object", "-w", "--stdin", input=_text({15: "T"}))
    env = dict(_ENV, GIT_INDEX_FILE=str(local / ".git" / "scratch-index"))
    _git(local, "read-tree", base, env=env)
    _git(local, "update-index", "--add", "--cacheinfo", f"100644,{blob},a.c", env=env)
    local_commit = _git(local, "commit-tree", _git(local, "write-tree", env=env), "-p", base,
                        "-m", f"change 100 moved\n\nChange-Id: {change_id(100)}")
    with pytest.raises(ConflictCheckError, match="could not fetch master"):
        run(client, local, commit=local_commit)


def test_a_bug_reading_the_tips_is_not_hidden_by_the_fallback(setup):
    gerrit, local, client = setup
    _target(gerrit, client.rest, {5: "T"})

    def broken(project, branch):
        raise KeyError("bug")

    client.get_branch_revision = broken
    with pytest.raises(KeyError):
        run(client, local)
