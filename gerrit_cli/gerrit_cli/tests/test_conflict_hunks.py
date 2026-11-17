"""Conflict regions and their kind (conflict_hunks.py)."""

from gerrit_cli.conflict_hunks import classify, conflict_regions, silent_collisions
import subprocess

from gerrit_cli.conflicts import Patch, Repo, change_ref, check_conflicts
from gerrit_cli.tests.test_graph_conflicts import _ENV, _check, _git, _text, gerrit, local  # noqa: F401


def test_both_append_after_the_same_line_is_insert_insert():
    assert classify([], ["x:1,", "a:1;"], ["x:1,", "b:1;"], {"a:1;"}) == "insert-insert"


def test_both_rewrite_the_last_bitfield_line_is_edit_edit():
    assert classify(["c:1;"], ["c:1,", "a:1;"], ["c:1,", "b:1;"], {"c:1,", "a:1;"}) == "edit-edit"


def test_neighbouring_lines_are_adjacent():
    assert classify(["l5", "l6"], ["L5", "l6"], ["l5", "L6"], {"L5"}) == "adjacent"


def test_a_pure_deletion_on_our_side_is_adjacent_not_context():
    assert classify(["A", "B"], ["A"], ["A2", "B"], set()) == "adjacent"


def test_branch_moved_under_a_stale_side_is_context():
    assert classify(["l5", "l6"], ["M5", "l6"], ["l5", "L6"], set()) == "context"


def _fetch(gerrit, local, *shas):
    _git(local, "fetch", "-q", str(gerrit.path), "+refs/heads/*:refs/remotes/o/*",
         "+refs/changes/*:refs/remotes/c/*")


def test_regions_come_from_the_merge_with_line_numbers(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    _fetch(gerrit, local)
    with Repo(local) as repo:
        regs = conflict_regions(repo, t.commit, c.commit, gerrit.master, ["a.c"],
                                {"a.c": {"T"}})
    [r] = regs["a.c"]
    assert (r["line"], r["kind"], r["truncated"]) == (5, "edit-edit", False)
    assert "|||||||" in r["text"] and "line 5" in r["text"]
    assert "line 2" in r["text"]


def test_a_deleted_file_conflict_has_no_regions(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    listing = _git(gerrit.path, "ls-tree", gerrit.master).splitlines()
    tree = _git(gerrit.path, "mktree",
                input="\n".join(x for x in listing if not x.endswith("\ta.c")) + "\n")
    d = _git(gerrit.path, "commit-tree", tree, "-p", gerrit.master, "-m", "del")
    _git(gerrit.path, "update-ref", "refs/changes/02/2/1", d)
    _fetch(gerrit, local)
    with Repo(local) as repo:
        regs = conflict_regions(repo, t.commit, d, gerrit.master, ["a.c"], {})
    assert regs == {"a.c": []}


def test_text_is_capped(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    _fetch(gerrit, local)
    with Repo(local) as repo:
        [r] = conflict_regions(repo, t.commit, c.commit, gerrit.master, ["a.c"], {},
                               max_lines=4)["a.c"]
    assert r["truncated"] and len(r["text"].splitlines()) == 4


def _hunks(r, other):
    return next(p for p in r["focus_pairs"] if p["other"] == other)


def test_focus_hunks_are_attached_to_conflicting_pairs_only(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    d = gerrit.change(3, gerrit.master, {"a.c": _text({15: "D"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c, d],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    h = _hunks(r, 2)["hunks"]
    assert [x["kind"] for x in h["landing"]["a.c"]] == ["edit-edit", "edit-edit"]
    assert {x["order"] for x in h["landing"]["a.c"]} == {"target_first", "candidate_first"}
    assert _hunks(r, 3)["hunks"] == {"landing": {}}
    r = _check(gerrit, local, [t, c, d], focus={1})
    assert all("hunks" not in p for p in r["focus_pairs"])


def test_branch_moved_under_the_candidate_is_context(gerrit, local):
    """The target only deletes line 5; what meets the candidate's line 4
    is master's line 6, which moved since the candidate's base."""
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    c = gerrit.change(2, old, {"a.c": _text({4: "C"})})
    deleted = [line for i, line in enumerate(_text({6: "M"}).splitlines()) if i != 4]
    t = gerrit.change(1, gerrit.master, {"a.c": "\n".join(deleted) + "\n"})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    kinds = {x["order"]: x["kind"] for x in _hunks(r, 2)["hunks"]["landing"]["a.c"]}
    assert kinds == {"target_first": "context", "candidate_first": "adjacent"}


def _raw_change(gerrit, cn, parent, content: bytes):
    blob = subprocess.run(["git", "-C", str(gerrit.path), "hash-object", "-w", "--stdin"],
                          input=content, capture_output=True, check=True).stdout.decode().strip()
    env = dict(_ENV, GIT_INDEX_FILE=str(gerrit.index))
    _git(gerrit.path, "read-tree", parent, env=env)
    _git(gerrit.path, "update-index", "--add", "--cacheinfo", f"100644,{blob},a.c", env=env)
    tree = _git(gerrit.path, "write-tree", env=env)
    sha = _git(gerrit.path, "commit-tree", tree, "-p", parent, "-m",
               f"change {cn}\n\nChange-Id: I{cn:040x}")
    _git(gerrit.path, "update-ref", change_ref(cn, 1), sha)
    gerrit.owner[sha] = (cn, "NEW")
    return Patch(cn, 1, sha)


def test_binary_conflict_is_noted_not_raised(gerrit, local):
    t = _raw_change(gerrit, 1, gerrit.master, b"\x00\xff\xfe T\n")
    c = _raw_change(gerrit, 2, gerrit.master, b"\x00\xff\xfe C\n")
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    h = _hunks(r, 2)["hunks"]
    assert h["landing"]["a.c"] == [] and h["notes"] == {"a.c": "no text regions"}


def test_non_utf8_text_conflict_still_has_regions(gerrit, local):
    base = _text({}).encode()
    t = _raw_change(gerrit, 1, gerrit.master, base.replace(b"line 5", b"T\xe9"))
    c = _raw_change(gerrit, 2, gerrit.master, base.replace(b"line 5", b"C\xe9"))
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    h = _hunks(r, 2)["hunks"]
    assert h["landing"]["a.c"] and "notes" not in h


def test_deleted_file_pair_carries_the_note(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    listing = _git(gerrit.path, "ls-tree", gerrit.master).splitlines()
    tree = _git(gerrit.path, "mktree",
                input="\n".join(x for x in listing if not x.endswith("\ta.c")) + "\n")
    d = _git(gerrit.path, "commit-tree", tree, "-p", gerrit.master, "-m",
             "del\n\nChange-Id: I" + "2".zfill(40))
    _git(gerrit.path, "update-ref", change_ref(2, 1), d)
    gerrit.owner[d] = (2, "NEW")
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, Patch(2, 1, d)],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    assert _hunks(r, 2)["hunks"]["notes"] == {"a.c": "no text regions"}


_SH = "lustre/tests/sanity.sh"


def test_both_adding_test_312_is_a_duplicate():
    one = "test_311() {\n}\n"
    t = one + "test_312() {\n}\n"
    got = silent_collisions({_SH: (t, t, one + "test_312() {\n}\n" + "test_312() {\n}\n")})
    assert got == [{"check": "duplicate-test", "file": _SH, "what": "test_312"}]


def test_a_test_both_already_had_is_not_reported():
    t = "test_5() {\n}\nrun_test 5 \"x\"\n"
    assert silent_collisions({_SH: (t, t, t)}) == []


def test_two_fail_locs_with_one_value():
    h = "include/obd_support.h"
    t = "#define OBD_FAIL_A 0x1439\n#define OBD_FAIL_B 0x143A\n"
    c = "#define OBD_FAIL_A 0x1439\n#define OBD_FAIL_C 0x143a\n"
    m = t + "#define OBD_FAIL_C 0x143a\n"
    assert silent_collisions({h: (t, c, m)}) == [
        {"check": "duplicate-value", "file": h, "what": "0x143a"}]


def test_a_test_defined_with_a_space_before_the_parens_counts():
    one = "test_311 () {\n}\n"
    t = one + "test_312 () {\n}\n"
    got = silent_collisions({_SH: (t, t, t + "test_312 () {\n}\n")})
    assert got == [{"check": "duplicate-test", "file": _SH, "what": "test_312"}]


def test_fail_loc_values_compare_as_numbers():
    h = "include/obd_support.h"
    t = "#define OBD_FAIL_B 0x0143a\n"
    c = "#define OBD_FAIL_C 0x143A\n"
    assert silent_collisions({h: (t, c, t + c)}) == [
        {"check": "duplicate-value", "file": h, "what": "0x143a"}]


def test_a_fail_loc_quoted_in_a_comment_is_not_a_define():
    h = "include/obd_support.h"
    t = "#define OBD_FAIL_B 0x143a\n"
    for c in ("/* #define OBD_FAIL_OLD 0x143a removed */\n",
              " * #define OBD_FAIL_OLD 0x143a\n"):
        assert silent_collisions({h: (t, c, t + c)}) == []


def test_unmatched_paths_run_nothing():
    t = "test_312() {\n}\n"
    assert silent_collisions({"lustre/foo.c": (t, t, t + t)}) == []


def test_clean_merge_with_duplicate_test_is_reported_by_the_engine(gerrit, local):
    gerrit.advance({"lustre/tests/x.sh": "a\n" + "\n" * 30 + "z\n"})
    top = "a\ntest_312() {\n}\n" + "\n" * 30 + "z\n"
    bot = "a\n" + "\n" * 30 + "z\ntest_312() {\n}\n"
    t = gerrit.change(1, gerrit.master, {"lustre/tests/x.sh": top})
    c = gerrit.change(2, gerrit.master, {"lustre/tests/x.sh": bot})
    r = _check(gerrit, local, [t, c], focus={1})
    pair = next(p for p in r["focus_pairs"] if p["other"] == 2)
    assert pair["landing"]["target_first"] == "clean"
    assert pair["silent"] == [{"check": "duplicate-test", "file": "lustre/tests/x.sh",
                               "what": "test_312"}]


def test_pairs_without_collisions_carry_an_empty_silent_list(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({2: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({15: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert r["focus_pairs"][0]["silent"] == []


def test_own_added_survives_a_git_timeout(local, monkeypatch):
    repo = Repo(local)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1)

    monkeypatch.setattr(subprocess, "run", slow)
    assert repo.own_added("a" * 40, "b" * 40, ["x.c"]) == {"x.c": set()}


def test_a_form_feed_does_not_shift_line_numbers(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({2: "x\fy\x1cz w", 5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({2: "x\fy\x1cz w", 5: "C"})})
    _fetch(gerrit, local)
    with Repo(local) as repo:
        [r] = conflict_regions(repo, t.commit, c.commit, gerrit.master, ["a.c"], {})["a.c"]
    assert r["line"] == 5
    assert r["text"].split("\n")[0] == "x\fy\x1cz w"


def test_crlf_regions_split_like_lf():
    from gerrit_cli.conflict_hunks import _regions

    blob = "a\r\n<<<<<<< x\r\nA\r\n||||||| b\r\nB\r\n=======\r\nC\r\n>>>>>>> y\r\n"
    [r] = _regions(blob)
    assert (r["start"], r["end"], r["ours"], r["base"], r["theirs"]) == (1, 7, ["A"], ["B"],
                                                                         ["C"])


_IDL = "include/uapi/linux/lustre/lustre_idl.h"


def test_two_connect_flags_with_one_value():
    t = "#define OBD_CONNECT_A\t\t0x1ULL /* a */\n#define OBD_CONNECT_B\t\t0x2ULL\n"
    c = "#define OBD_CONNECT_A\t\t0x1ULL /* a */\n#define OBD_CONNECT_C\t\t0x02ULL\n"
    m = t + "#define OBD_CONNECT_C\t\t0x02ULL\n"
    assert silent_collisions({_IDL: (t, c, m)}) == [
        {"check": "duplicate-value", "file": _IDL, "what": "OBD_CONNECT 0x2"}]


def test_connect_and_connect2_are_separate_namespaces():
    t = "#define OBD_CONNECT_B 0x4ULL\n"
    c = "#define OBD_CONNECT2_B 0x4ULL\n"
    assert silent_collisions({_IDL: (t, c, t + c)}) == []
    c2 = "#define OBD_CONNECT2_C 0x4ULL\n"
    assert silent_collisions({_IDL: (c, c2, c + c2)}) == [
        {"check": "duplicate-value", "file": _IDL, "what": "OBD_CONNECT2 0x4"}]


def test_a_connect_flag_composite_is_not_a_value():
    t = "#define OBD_CONNECT_B 0x4ULL\n"
    c = "#define OBD_CONNECT_MASK (OBD_CONNECT_B | 0x4ULL)\n"
    assert silent_collisions({_IDL: (t, c, t + c)}) == []


def test_both_skipping_the_same_test_is_a_duplicate_exception():
    base = "ALWAYS_EXCEPT=\"$SANITY_EXCEPT \"\nalways_except LU-1 42a 42b\n"
    t = base + "always_except LU-2 56\n"
    c = base + "always_except LU-3\t56 # flaky\n"
    m = base + "always_except LU-2 56\nalways_except LU-3\t56 # flaky\n"
    assert silent_collisions({_SH: (t, c, m)}) == [
        {"check": "duplicate-exception", "file": _SH, "what": "ALWAYS_EXCEPT 56"}]


def test_exceptions_in_assignments_and_continued_lines():
    from gerrit_cli.conflict_hunks import _exceptions

    text = ('ALWAYS_EXCEPT="$X_EXCEPT 1a"\n'
            'ALWAYS_EXCEPT+=" 14b  21b"\n'
            '\tALWAYS_EXCEPT="$ALWAYS_EXCEPT 9"\n'
            "export ALWAYS_EXCEPT=$Y_EXCEPT\n"
            "\talways_except LU-4 smoke \\\n\t\tteardown\n"
            "# always_except LU-5 77\n")
    assert _exceptions(text) == ["ALWAYS_EXCEPT " + t
                                 for t in ("1a", "14b", "21b", "9", "smoke", "teardown")]


def test_an_exception_one_side_adds_again_is_not_a_duplicate():
    base = "always_except LU-1 56\n"
    t = base + "always_except LU-2 56\n"
    assert silent_collisions({_SH: (t, base, t)}) == []


def test_own_added_ignores_the_users_diff_configuration(gerrit, local):
    """An external diff driver or a textconv must not change what is read;
    an added line starting "++" is still an added line."""
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T", 6: "++x"})})
    _fetch(gerrit, local)
    _git(local, "config", "diff.external", "true")
    _git(local, "config", "diff.noprefix", "true")
    with Repo(local) as repo:
        assert repo.own_added(t.commit, gerrit.master, ["a.c"]) == {"a.c": {"T", "++x"}}
