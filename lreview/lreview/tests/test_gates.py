"""Version gate facts."""

import subprocess
from unittest.mock import patch

import pytest

from lreview import gates
from lreview.backport import Upstream
from lreview.gates import (NOTE_TAGS, added_gates, gate_facts, same_line,
                           tree_version, version_code)
from lreview.gerrit import LocalChange, ResolvedChange
from lreview.runner import attach_gates, review_prompt

from .test_backport import config_for

HOST = "https://review.example.com"
CID = "I" + "1" * 40
SUBJECT = "LU-1 osd: fix the broken frobnication"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def gate_line(version):
    return f"\t(( MDS1_VERSION >= $(version_code {version}) )) ||\n"


class Tree:
    """A clone with a vendor and an upstream remote, already fetched."""

    def __init__(self, path):
        self.path = path
        path.mkdir()
        git(path, "init", "-q", "-b", "work")
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "t")
        git(path, "remote", "add", "up", f"{HOST}/fs/lustre-release")
        git(path, "remote", "add", "own", f"{HOST}/vendor/lustre")

    def commit(self, message=None, tag=None):
        # unrelated root commits must not be one and the same
        self.commits = getattr(self, "commits", 0) + 1
        message = message or f"commit {self.commits}"
        git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        sha = git(self.path, "rev-parse", "HEAD")
        if tag:
            git(self.path, "tag", tag)
        return sha

    def branch(self, remote, name):
        """Publish HEAD as a remote's branch and start a new history."""
        git(self.path, "update-ref", f"refs/remotes/{remote}/{name}", "HEAD")

    def orphan(self, name):
        git(self.path, "checkout", "-q", "--orphan", name)
        git(self.path, "rm", "-rqf", "--ignore-unmatch", ".")

    def test_commit(self, *versions, subject=SUBJECT):
        path = self.path / "lustre/tests/sanity.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as out:
            out.write("".join(gate_line(v) for v in versions))
        git(self.path, "add", "lustre")
        return self.commit(f"{subject}\n\nChange-Id: {CID}")


@pytest.fixture(autouse=True)
def offline():
    with patch.object(gates, "refresh"), \
            patch.object(gates, "_find_open", return_value=None):
        yield


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path / "repo")


def change_of(sha, project="vendor/lustre", branch="b2"):
    return ResolvedChange(number=7, project=project, subject=SUBJECT,
                          sha=sha, patchset=1, ref="", base_url=HOST,
                          change_id=CID, branch=branch)


def facts(tree, sha, upstreams=None, **kwargs):
    text = gate_facts(tree.path, change_of(sha, **kwargs), upstreams)
    return text.split("\n\n")[0]


class TestVersions:

    def test_version_code_keeps_four_numbers(self):
        assert version_code("2.16.0-abc62") == (2, 16, 0, 62)
        assert version_code("v2_17_62") == (2, 17, 62, 0)
        assert version_code("2.17.58.39.7") == (2, 17, 58, 39)
        assert version_code("2.16.58-105") == (2, 16, 58, 105)

    def test_three_number_tags_count_commits(self, tree):
        tree.commit(tag="2.17.62")
        tree.commit()
        assert tree_version(tree.path, tree.commit()) == \
            ((2, 17, 62, 2), "2.17.62")

    def test_four_number_tags_do_not(self, tree):
        tree.commit(tag="2.16.0-abc62")
        assert tree_version(tree.path, tree.commit()) == \
            ((2, 16, 0, 62), "2.16.0-abc62")

    def test_release_lines(self):
        assert same_line((2, 16, 0, 61), (2, 16, 0, 62), "2.16.0-abc62")
        assert not same_line((2, 14, 0, 61), (2, 16, 0, 62), "2.16.0-abc62")
        assert same_line((2, 16, 58, 0), (2, 17, 62, 0), "2.17.62")
        assert not same_line((2, 17, 1, 0), (2, 17, 62, 0), "2.17.62")
        assert same_line((2, 15, 1, 0), (2, 15, 8, 0), "2.15.8")


class TestAddedGates:

    def test_line_numbers_and_text(self, tree):
        tree.commit(tag="2.17.60")
        tree.test_commit("2.17.50")
        found = added_gates(tree.path, tree.test_commit("2.17.60", "2.17.61"))
        assert [(g.path, g.line, g.version) for g in found] == [
            ("lustre/tests/sanity.sh", 2, "2.17.60"),
            ("lustre/tests/sanity.sh", 3, "2.17.61")]

    def test_moved_line_is_not_added(self, tree):
        tree.commit(tag="2.17.60")
        tree.test_commit("2.17.50")
        path = tree.path / "lustre/tests/sanity.sh"
        path.write_text("# new\n\t" + path.read_text())
        git(tree.path, "commit", "-qam", "move")
        assert added_gates(tree.path, "HEAD") == []

    def test_other_files_do_not_count(self, tree):
        path = tree.path / "lustre/a.sh"
        path.parent.mkdir()
        path.write_text(gate_line("2.17.60"))
        git(tree.path, "add", "lustre")
        assert gate_facts(tree.path, change_of(tree.commit())) == ""


class TestOwnBranch:

    def tagged(self, tree, gate, ahead, tag="2.16.0-abc{}", base=60):
        """A test commit on tag base; its branch is ahead tags further."""
        tree.commit(tag=tag.format(base))
        sha = tree.test_commit(tag.format(gate))
        git(tree.path, "checkout", "-q", "HEAD^")
        for i in range(ahead):
            tree.commit(tag=tag.format(base + 1 + i))
        tree.branch("own", "b2")
        return facts(tree, sha)

    def test_at_the_tree_is_fine(self, tree):
        text = self.tagged(tree, 60, 0)
        assert "at the tree's version 2.16.0-abc60: fine" in text
        assert "NOTE" not in text

    def test_unreleased_tag_is_above(self, tree):
        assert "ABOVE the tree's version 2.16.0-abc60" in \
            self.tagged(tree, 61, 1)

    def test_one_tag_behind_is_silent(self, tree):
        assert NOTE_TAGS == 1
        assert "NOTE" not in self.tagged(tree, 60, 1)

    def test_old_base(self, tree):
        text = self.tagged(tree, 60, 2)
        assert "2 tags past the gate: the patch has an old base" in text
        assert "older than its base" not in text

    def test_gate_not_updated(self, tree):
        for tag in ("2.16.0-abc57", "2.16.0-abc58"):
            tree.commit(tag=tag)
        text = self.tagged(tree, 57, 0)
        assert "below the tree's version" in text
        assert "2 tags past the gate: the gate is older" in text
        assert "old base" not in text

    def test_master_counts_commits(self, tree):
        tree.commit(tag="2.17.62")
        tree.branch("up", "master")
        sha = tree.test_commit("2.17.62.1", "2.17.62.2")
        text = facts(tree, sha, project="fs/lustre-release", branch="master")
        assert "at the tree's version 2.17.62.1: fine" in text
        assert "ABOVE the tree's version 2.17.62.1" in text

    def test_local_change_knows_its_tree(self, tree):
        tree.commit(tag="2.17.62")
        sha = tree.test_commit("2.17.62", "2.17.63")
        text = gate_facts(tree.path, LocalChange("HEAD", sha, SUBJECT))
        assert "below the tree's version 2.17.62.1: fine" in text
        assert "ABOVE" in text

    def test_no_tags_no_facts(self, tree):
        assert facts(tree, tree.test_commit("2.17.62")) == ""


class TestOtherBranch:

    def build(self, tree, landed=True, gate="2.14.0-abc30", tip=32):
        """b1 (2.14.0-abc) at tip, with the patch after abc30."""
        tree.commit(tag="2.14.0-abc29")
        for n in range(30, tip + 1):
            tree.commit(tag=f"2.14.0-abc{n}")
            if n == 30 and landed:
                tree.commit(f"LU-9 {SUBJECT[5:]}\n\nChange-Id: {CID}")
        tree.branch("own", "b1")
        tree.orphan("second")
        tree.commit(tag="2.16.0-abc60")
        return tree.test_commit(gate)

    def test_landed_there(self, tree):
        text = facts(tree, self.build(tree))
        assert "for b1, now at 2.14.0-abc32: the patch is there" in text
        assert "landed at 2.14.0-abc30): fine" in text

    def test_found_by_subject(self, tree):
        sha = self.build(tree)
        change = change_of(sha)
        change.change_id = None
        assert "the patch is there" in gate_facts(tree.path, change)

    def test_drift_is_a_note(self, tree):
        text = facts(tree, self.build(tree, gate="2.14.0-abc32"))
        assert "NOTE: the gate is 2 tags above where it landed" in text

    def test_missing_there(self, tree):
        assert "MISSING there" in facts(tree, self.build(tree, landed=False))

    def test_open_there_is_not_missing(self, tree):
        sha = self.build(tree, landed=False)
        with patch.object(gates, "_find_open", return_value=99):
            text = facts(tree, sha)
        assert "open as change 99" in text and "MISSING" not in text

    def test_failed_query_claims_nothing(self, tree):
        sha = self.build(tree, landed=False)
        with patch.object(gates, "_find_open", side_effect=OSError):
            text = facts(tree, sha)
        assert "MISSING" not in text and "NOTE" not in text

    def test_reverted_there_is_missing(self, tree):
        tree.commit(tag="2.14.0-abc30")
        sha = tree.commit(f"{SUBJECT}\n\nChange-Id: {CID}")
        tree.commit(f'Revert "{SUBJECT}"\n\nThis reverts commit {sha}.')
        tree.branch("own", "b1")
        tree.orphan("second")
        tree.commit(tag="2.16.0-abc60")
        assert "MISSING there" in facts(tree, tree.test_commit("2.14.0-abc30"))

    def test_gate_keeps_it_out(self, tree):
        sha = self.build(tree, landed=False, gate="2.14.0-abc40")
        assert "the gate keeps it out" in facts(tree, sha)

    def test_port_finds_the_upstream_branch(self, tree):
        tree.commit(tag="2.17.60")
        tree.commit("LU-9 master: subject that differs there\n\n"
                    f"Change-Id: I{'2' * 40}")
        tree.branch("up", "master")
        tree.orphan("old")
        tree.commit(tag="2.15.90")
        tree.branch("own", "master")
        tree.orphan("second")
        tree.commit(tag="2.16.0-abc60")
        sha = tree.test_commit("2.17.60.1")
        upstream = Upstream(name="u", sha="x", landed=True,
                            change_id="I" + "2" * 40)
        assert "for master, now at 2.17.60.1: the patch is there" in \
            facts(tree, sha, [upstream])

    def test_public_change_sees_no_other_project(self, tree):
        self.build(tree)
        tree.orphan("third")
        tree.commit(tag="2.17.62")
        tree.branch("up", "master")
        text = facts(tree, tree.test_commit("2.14.0-abc30"),
                     project="fs/lustre-release", branch="master")
        assert "not checked: nothing to report" in text
        assert "b1" not in text


class TestPrompt:

    def test_rules_reach_the_prompt_only_with_gates(self, tree, tmp_path):
        tree.commit(tag="2.17.62")
        config = config_for(tmp_path, tree.path)
        plain = LocalChange("HEAD", tree.commit(), SUBJECT)
        attach_gates(config, plain)
        assert "version_code" not in review_prompt(config, plain)
        change = LocalChange("HEAD", tree.test_commit("2.17.62"), SUBJECT)
        attach_gates(config, change)
        prompt = review_prompt(config, change)
        assert "lustre/tests/sanity.sh:1 2.17.62: for this branch" in prompt
        assert "# Version gates in tests" in prompt

    def test_failure_leaves_the_review(self, tree, tmp_path, capsys):
        config = config_for(tmp_path, tree.path)
        change = LocalChange("HEAD", tree.test_commit("2.17.62"), SUBJECT)
        with patch.object(gates, "gate_facts", side_effect=OSError("no")):
            attach_gates(config, change)
        assert change.gates == ""
        assert "version gate check failed" in "".join(capsys.readouterr())
