"""The --human report of gc conflicts, rendered from compact results."""
import copy
import io
import json
from pathlib import Path

import pytest

from gerrit_cli.conflict_human import group_by_root, render, use_color

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def r70663():
    return load("conflicts_70663.json")


def test_groups_via_pairs_under_their_root(r70663):
    groups = group_by_root(r70663["conflicts"])
    assert [root["cn"] for root, _ in groups] == [69517, 64919]
    assert [c["cn"] for c in groups[0][1]] == [68361, 69703, 70067, 70571, 64441]
    assert groups[1][1] == []


def test_root_section_in_words(r70663):
    text = render(r70663, color=False)
    assert "69517" in text
    assert "in master-next" in text
    assert "conflicts in both landing orders" in text
    assert "who moves: this change (70663)" in text
    stacked = next(line for line in text.splitlines() if "stacked on it, same conflict:" in line)
    assert "68361" in stacked
    assert "64919" in text and "lustre/llite/file.c" in text


def test_header_names_next_blocker(r70663):
    text = render(r70663, color=False)
    assert "70663 PS3" in text
    assert "171 behind master" in text
    assert "master-next: CONFLICTS with 69517" in text


def test_via_root_missing_is_synthesized(r70663):
    r = copy.deepcopy(r70663)
    r["conflicts"] = [e for e in r["conflicts"] if e["cn"] != 69517]
    groups = group_by_root(r["conflicts"])
    root, children = groups[0]
    assert root["cn"] == 69517 and root["owner"] == "Keguang Xu"
    assert [c["cn"] for c in children] == [68361, 69703, 70067, 70571, 64441]
    text = render(r, color=False)
    assert "69517" in text and "Keguang Xu" in text


def test_own_via_named_as_parent(r70663):
    r = copy.deepcopy(r70663)
    r["conflicts"] = [{
        "cn": 222, "subject": "LU-1 x: y", "owner": "Other", "tier": "lands-first",
        "readiness": "ready", "fix_side": "candidate", "files": ["a.c"],
        "landing": "conflict/conflict", "own_via": 111,
        "own_via_change": {"cn": 111, "owner": "X", "mine": False, "tier": "lands-first"}}]
    groups = group_by_root(r["conflicts"])
    assert [root["cn"] for root, _ in groups] == [222]
    assert "in this change's parent 111 (X)" in render(r, color=False)


def test_needs_rebase_prints_rebase_advice(r70663):
    r = copy.deepcopy(r70663)
    r.update(status="needs-rebase", conflicts=None, rebase_cause="parent",
             rebase_onto={"cn": 5, "ps": 3})
    text = render(r, color=False)
    assert "needs a rebase onto 5 PS3" in text
    assert "COLLISIONS" not in text


def test_clean_result_says_no_collisions(r70663):
    r = copy.deepcopy(r70663)
    r.update(conflicts=[], silent_only=[], watch=[])
    text = render(r, color=False)
    assert "no collisions" in text
    assert "WATCH" not in text


def test_hunks_rendered_under_root():
    text = render(load("conflicts_70735_hunks.json"), color=False)
    assert "lustre/include/obd.h" in text
    assert "insert-insert" in text
    assert "<<<<<<< 70735 (this change)" in text
    assert ">>>>>>> 64919" in text
    assert "||||||| base" in text


def test_color_codes_only_when_asked(r70663):
    assert "\x1b[" in render(r70663, color=True)
    assert "\x1b[" not in render(r70663, color=False)


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_no_color_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert use_color(io.StringIO()) is False
    assert use_color(_Tty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color(_Tty()) is False
