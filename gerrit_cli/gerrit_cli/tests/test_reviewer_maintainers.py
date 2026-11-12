"""reviewer-candidates reports who MAINTAINERS names, beside the ranking.

fb-019d54d2436c: on change 69015 the LFSCK maintainer, whom
get_maintainer.pl lists first, was not among the candidates (only git
history is ranked) and the run added him by hand.  MAINTAINERS is stale
(Patrick, 2026-11-12), so it is extra information: it changes no rank,
eligibility or hold, and a missing or odd file is an empty list.
"""

import json
import shutil
from pathlib import Path

import pytest

from gerrit_cli.commands.maintainers import (
    change_maintainers, file_match, maintainers_for, parse_sections,
)
from gerrit_cli.tests.test_reviewer_candidates import (  # noqa: F401
    _git, _run, repo,
)

FIXTURE = Path(__file__).parent / "fixtures" / "MAINTAINERS"


@pytest.fixture
def sections():
    return parse_sections(FIXTURE.read_text())


@pytest.fixture
def mrepo(repo):  # noqa: F811
    shutil.copy(FIXTURE, repo / "MAINTAINERS")
    _git(repo, "add", "MAINTAINERS")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "-m", "add MAINTAINERS")
    return repo


@pytest.mark.parametrize("path,pattern,hit", [
    ("lustre/llite/file.c", "lustre/llite/file.c", True),
    ("lustre/llite/file.c", "lustre/llite/", True),
    ("lustre/llite/sub/x.c", "lustre/llite/", True),
    ("lnet/autoconf/lnet.m4", "*/autoconf/", True),
    ("lustre/lfsck/lfsck_lib.c", "lustre/lfsck", True),   # a directory
    ("lustre/lfsck/lfsck_lib.c", "lustre/lfsck", True),
    ("lustre/osd-ldiskfs/osd_scrub.c", "lustre/osd-ldiskfs/osd_scrub.[ch]", True),
    ("lustre/osd-ldiskfs/osd_scrubXc", "lustre/osd-ldiskfs/osd_scrub.c", False),
    ("lustre/build/x", "build/", False),
    ("lustre/llite/sub/x.c", "lustre/llite/*", False),   # one level only
    ("lustre/x.c", "lustre/[", False),                   # bad regex
])
def test_patterns_match_as_get_maintainer_does(path, pattern, hit):
    assert file_match(path, pattern)[0] is hit
    assert file_match(path, pattern, {"lustre/lfsck"})[0] is hit


def test_a_tree_directory_gets_its_slash():
    # get_maintainer.pl's -d test: lustre/lfsck is a directory, so it
    # does not match lustre/lfsck_other.c by prefix.
    assert file_match("lustre/lfsck_other.c", "lustre/lfsck",
                      {"lustre", "lustre/lfsck"})[0] is False
    assert file_match("lustre/lfsck_other.c", "lustre/lfsck", set())[0]


def test_sections_people_and_exclusions(sections):
    names = [s["name"] for s in sections]
    assert "Lustre File System Check - LFSCK" in names
    assert "Lustre Orphan Area" not in names  # nobody to name
    people = maintainers_for(sections, ["lustre/lfsck/lfsck_lib.c",
                                        "lustre/llite/namei.c",
                                        "lustre/tests/sanity-lfsck.sh"])
    by = {p["name"]: p for p in people}
    # X: leaves the llite section out for namei.c.
    assert "Andreas Dilger" not in by
    assert by["Hongchao Zhang"]["role"] == "R"
    assert by["Hongchao Zhang"]["files"] == ["lustre/lfsck/lfsck_lib.c",
                                             "lustre/tests/sanity-lfsck.sh"]
    assert by["Tess Tester"]["sections"] == ["Lustre Tests"]  # N: regex


def test_missing_or_unparsable_file_is_an_empty_list(repo, tmp_path):  # noqa: F811
    assert change_maintainers(str(repo), "HEAD", ["x.c"]) == (None, [])
    assert change_maintainers(str(tmp_path), "HEAD", ["x.c"]) == (None, [])
    (repo / "MAINTAINERS").write_bytes(b"\xff\xfeF:\t[[[\nR: nobody\n\x00")
    _git(repo, "add", "MAINTAINERS")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "-m", "junk")
    assert change_maintainers(str(repo), "HEAD", ["x.c"])[1] == []


def test_maintainers_are_reported_beside_an_unchanged_ranking(
    monkeypatch, capsys, repo, mrepo,  # noqa: F811
):
    shutil.copy(FIXTURE, repo / "MAINTAINERS.keep")
    code, data = _run(monkeypatch, capsys, mrepo)
    assert code == 0
    assert data["maintainers_file"] == "HEAD:MAINTAINERS"
    by = {m["name"]: m for m in data["maintainers"]}
    assert set(by) == {"Andreas Dilger", "Neil Brown", "Carl Clio"}
    assert by["Andreas Dilger"]["role"] == "M"
    assert by["Andreas Dilger"]["on_change"] == "REVIEWER"
    assert by["Andreas Dilger"]["files"] == ["lustre/llite/file.c",
                                             "lustre/llite/namei2.c"]
    names = [c["name"] for c in data["candidates"]]
    assert by["Neil Brown"]["rank"] == names.index("Neil Brown") + 1
    # Not in the history: reported, not ranked, not made eligible.
    assert by["Carl Clio"]["rank"] is None
    assert "Carl Clio" not in names and "Carl Clio" not in data["eligible"]
    neil = data["candidates"][names.index("Neil Brown")]
    assert neil["maintainer"] == ["Lustre Client IO stack - CLIO"]
    assert "maintainer" not in data["candidates"][names.index("Jian Yu")]


def test_ranking_eligible_and_holds_do_not_move(
    monkeypatch, capsys, repo, tmp_path,  # noqa: F811
):
    asks = tmp_path / "asks.json"
    asks.write_text(json.dumps({"by": "test", "people": [
        {"name": "Neil Brown", "email": "neilb@suse.de",
         "asked": 20, "answered": 5, "pending": 10, "asked_14d": 10}]}))
    before = _run(monkeypatch, capsys, repo, asked=str(asks), compact=True)[1]
    shutil.copy(FIXTURE, repo / "MAINTAINERS")
    _git(repo, "add", "MAINTAINERS")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "-m", "add MAINTAINERS")
    after = _run(monkeypatch, capsys, repo, asked=str(asks), compact=True)[1]
    assert before["maintainers"] == [] and before["maintainers_file"] is None
    strip = [{k: v for k, v in c.items() if k != "maintainer"}
             for c in after["candidates"]]
    assert strip == before["candidates"]
    assert after["eligible"] == before["eligible"]
    neil = next(c for c in after["candidates"] if c["name"] == "Neil Brown")
    assert neil["maintainer"] is True and neil["hold"]
    assert all("files" not in m for m in after["maintainers"])


def test_table_marks_the_row_and_lists_maintainers(
    monkeypatch, capsys, mrepo,
):
    code, data = _run(monkeypatch, capsys, mrepo, table=True)
    assert code == 0
    neil = next(r for r in data["rows"] if " Neil Brown " in r)
    assert " maintainer " in neil
    assert "maintainer_columns" in data
    rows = {r.split(" <")[0]: r for r in data["maintainers"]}
    assert rows["Carl Clio"] == ("Carl Clio <carl@example.com> R not_listed "
                                 "[Lustre Client IO stack - CLIO]")
    assert rows["Andreas Dilger"].startswith(
        "Andreas Dilger <adilger@whamcloud.com> M on_change:REVIEWER ")
    assert rows["Neil Brown"].split()[4].startswith("rank:")
