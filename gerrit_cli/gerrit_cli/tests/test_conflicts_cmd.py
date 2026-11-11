"""gc conflicts end to end: canned REST payloads plus the bare-repo Gerrit."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest

from gerrit_cli.commands._helpers import error_code_for
from gerrit_cli.commands.conflicts import run_conflicts
from gerrit_cli.conflicts import ConflictRepoError, Patch
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
            m = re.search(rf"\b{key}:(\S+)", q)
            if m and c[key] != m[1]:
                return False
        if m := re.search(r"-age:(\d+)d", q):
            updated = datetime.strptime(c["updated"][:19], "%Y-%m-%d %H:%M:%S")
            if NOW - updated > timedelta(days=int(m[1])):
                return False
        files = {re.sub(r"\\(.)", r"\1", f) for f in re.findall(r'file:"\^(.*?)\$"', q)}
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
                             "conflicting": 1}
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
                             "conflicting": 0}


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
    assert c["fix_side"] == "coordinate"
    assert c["hashtags"] == ["ec_work"]


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
    assert out["own_stack"]["children"] == [{"cn": 110, "status": "conflict"}]


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
    assert (args.repo, args.commit, args.hunks, args.max_age, args.with_, args.all) == \
        (".", None, False, 30, None, False)
    args = build_parser().parse_args(["conflicts", "100", "--with", "5", "--with", "6"])
    assert args.with_ == [5, 6]


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
        {"cn": 105, "status": "clean"}, {"cn": 110, "status": "clean"}]
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
