"""gc test-numbers: free numbers in gaps no open change touches.

The fixture script has tests 1, 2, 8, 10 and 13; 9 and 12 are except
listed.  Canned Gerrit diffs: change A adds 3 and change B adds 5, both
after test 2 (B on a base 10 lines longer, so only placing its hunk by the
test line above it puts it in the right gap); change C adds 14 at the end.
"""

import json
from pathlib import Path

import pytest

from gerrit_cli import test_numbers as tn

FIXTURE = (Path(__file__).parent / "fixtures" / "test_numbers"
           / "sanity-fixture.sh").read_text()
PATH = "lustre/tests/sanity.sh"


def _insert_after(text, run_line, new_lines, pad=0):
    """A full-context Gerrit diff inserting new_lines after a line of
    text, on a base with pad extra lines at the top."""
    lines = [f"# pad {i}" for i in range(pad)] + text.splitlines()
    at = run_line + pad
    return {"content": [{"ab": lines[:at]}, {"b": new_lines},
                        {"ab": lines[at:]}]}


def _test_lines(tid):
    return ["", f"test_{tid}() {{", "\ttrue", "}", f'run_test {tid} "new"']


DIFFS = {
    1001: _insert_after(FIXTURE, 15, _test_lines("3")),
    1002: _insert_after(FIXTURE, 15, _test_lines("5"), pad=10),
    1003: _insert_after(FIXTURE, 30, _test_lines("14")),
}


class FakeClient:
    def __init__(self, diffs=DIFFS, files=None, ex=None):
        self.diffs = diffs
        self.files = files or {"master": FIXTURE}
        self.ex = ex or {}
        self.queries = []

    def search_changes(self, query, limit=25, start=0, options=None):
        self.queries.append((query, start))
        if start:
            return []
        return [{"_number": n, "subject": f"change {n}", "branch": "master",
                 "owner": {"name": "someone"}, "updated": "2026-11-20 00:00",
                 "current_revision": "r",
                 "revisions": {"r": {"files": {PATH: {}}}}}
                for n in self.diffs]

    def get_text(self, endpoint):
        branch = endpoint.split("/branches/")[1].split("/")[0]
        if "except" in endpoint:
            if branch in self.ex:
                return self.ex[branch]
        elif branch in self.files:
            return self.files[branch]
        raise RuntimeError("404 Client Error: Not Found")

    def get_diff(self, change, path, base):
        return self.diffs[change]


def run(**kw):
    kw.setdefault("suggest_k", 3)
    return tn.gather(FakeClient(**{k: kw.pop(k) for k in ("diffs", "ex")
                                   if k in kw}),
                     ["sanity.sh"], **kw)[PATH]


def test_parse_tests_starts_at_the_comment():
    tests = {t["id"]: t for t in tn.parse_tests(FIXTURE)}
    assert tests["2"] == {"id": "2", "run_line": 15, "start_line": 11}
    assert tests["8"]["start_line"] == 17


def test_except_ids_from_script_and_ex_file():
    ex = "# comment\nclient  >  v2_16_54-54-ga2e3a2f5a3  LU-14590 0d 7\n"
    assert tn.except_ids(FIXTURE, [ex]) == {"9", "12", "0d", "7"}


def test_two_changes_in_one_gap_take_it():
    r = run()
    gap = next(s for s in r["slots"] if s["after"] == "2")
    assert gap["numbers"] == ["4", "6", "7"]
    assert gap["gap"] == [15, 17]
    assert gap["taken_by"] == [1001, 1002]
    assert not gap["usable"]


def test_change_on_an_older_base_lands_in_its_own_gap():
    # 1002's hunk is at base line 26, which on the tip would be the gap
    # after test 10; placed by run_test 2 above it, it is at line 16.
    r = run()
    ranges = {c["change"]: c["ranges"] for c in r["changes"]}
    assert ranges[1002] == [[16, 16]]
    gap = next(s for s in r["slots"] if s["after"] == "10")
    assert gap["numbers"] == ["11"] and gap["usable"]


def test_change_at_the_append_point_contests_it():
    r = run()
    assert r["append"]["numbers"] == ["15"]
    assert r["append"]["after"] == "13"
    assert r["append"]["taken_by"] == [1003]
    assert not r["append"]["usable"]


def test_suggest_prefers_a_free_gap_inside_the_range():
    r = run()
    assert [s["number"] for s in r["suggest"]] == ["11"]
    assert r["suggest"][0]["gap"] == [25, 27]


def test_suggest_falls_back_to_a_free_append_point():
    r = run(diffs={1001: DIFFS[1001]})
    assert [s["number"] for s in r["suggest"]] == ["11", "14"]
    assert r["append"]["usable"] and r["append"]["numbers"] == ["14"]


def test_except_listed_numbers_are_skipped():
    r = run(ex={"b2_15": "client > v LU-2 11\n"})
    assert "11" in r["except_listed"]
    assert all("11" not in s["numbers"] for s in r["slots"])
    assert r["suggest"] == []


def test_collision_and_shared_number():
    diffs = dict(DIFFS)
    diffs[1004] = _insert_after(FIXTURE, 25, _test_lines("3"))
    r = run(diffs=diffs)
    assert r["collisions"] == {"3": [1001, 1004]}
    assert [1001, 1002] in r["near_conflicts"]


FAMILY = """test_5() { true; }
run_test 5 "five"

test_5a() { true; }
run_test 5a "a"

# about 5b
test_5b() { true; }
run_test 5b "b"

test_6() { true; }
run_test 6 "six"
"""


def test_family_next_suffix_and_rival():
    rival = _insert_after(FAMILY, 9, _test_lines("5c"))
    info = tn.slots(FAMILY, [{"change": 7, "added": ["5c"], "ranges":
                              tn.map_hunks(*tn.diff_hunks(rival)[:2],
                                           tn.anchors(FAMILY))}],
                    family="5")
    fam = info["family"]
    assert fam["numbers"] == ["5d"] and fam["after"] == "5b"
    assert fam["taken_by"] == [7] and not fam["usable"]
    free = tn.slots(FAMILY, [], family="5")["family"]
    assert free["numbers"] == ["5c"] and free["usable"]
    assert free["gap"] == [9, 11]


def test_next_suffix():
    assert tn.next_suffix("x") == "y"
    assert tn.next_suffix("xb") == "xc"
    assert tn.next_suffix("z") == "A"
    assert tn.next_suffix("Z") == "Za"


def test_maintained_branch_missing_file_is_skipped():
    # b2_15 etc. answer 404 for the file in FakeClient: no error.
    assert run()["tip_max"] == 13


def test_cli_json(monkeypatch, capsys):
    from gerrit_cli import cli
    monkeypatch.setattr(cli, "GerritCommentsClient", lambda: FakeClient())
    monkeypatch.setattr("sys.argv", ["gerrit", "test-numbers", "sanity.sh",
                                     "--suggest", "2"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["files"][PATH]["suggest"][0]["number"] == "11"


def test_cli_human(monkeypatch, capsys):
    from gerrit_cli import cli
    monkeypatch.setattr(cli, "GerritCommentsClient", lambda: FakeClient())
    monkeypatch.setattr("sys.argv", ["gerrit", "test-numbers", "sanity.sh",
                                     "--suggest", "3", "--human"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    assert "TAKEN 4, 6-7 (after 2, lines 15-17): change(s) 1001, 1002" in out
    assert "append point 15 (after 13, line 30): CONTESTED by change(s) 1003" in out
    assert "suggest: 11 (after 10, lines 25-27, no open change touches it)" in out


def test_append_ignores_far_numbers():
    """A change adding 99p must not move the append point to 100."""
    tip = FIXTURE
    far = {"change": 1009, "added": ["99p"], "ranges": [[29, 29]]}
    info = tn.slots(tip, [far])
    assert info["append"]["numbers"] == ["14"]
    assert info["append"]["taken_by"] == [1009]
