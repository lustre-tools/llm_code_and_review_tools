"""lreview run --since REV: review only what changed from an earlier
version of the commit."""

import json
import subprocess
from pathlib import Path

import pytest

from lreview.cli import build_parser, cmd_run
from lreview.gerrit import LocalChange
from lreview.runner import (CLOSING_NOTE, BatchConfig, ReviewResult,
                            STATUS_CLEAN, review_prompt, update_summary)
from lreview.since import SinceFocus, focus_prompt, resolve_since
from lreview.worktree import add_worktree, remove_worktree, rev_parse

OLD = "a" * 40
NEW = "b" * 40


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """base -> v1 (the patch as it was), plus amended/rebased versions."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "t")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "t@e")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.c").write_text("".join(f"{i}\n" for i in range(50)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.c").write_text("".join(f"{i}\n" for i in range(51)))
    _git(repo, "commit", "-qam",
         "LU-1 llite: the patch\n\nChange-Id: I" + "1" * 40)
    return repo


def _amend(repo, text="x\n", message=None):
    (repo / "b.c").write_text(text)
    _git(repo, "add", ".")
    args = ["commit", "-q", "--amend"]
    args += ["-m", message] if message else ["--no-edit"]
    _git(repo, *args)
    return rev_parse(repo, "HEAD")


def _rebase_onto_new_base(repo, v1):
    _git(repo, "checkout", "-q", "HEAD~1")
    (repo / "up.c").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    _git(repo, "cherry-pick", v1)
    return rev_parse(repo, "HEAD")


class TestResolveSince:

    def test_amended_same_base(self, repo):
        v1 = rev_parse(repo, "HEAD")
        v2 = _amend(repo)
        focus = resolve_since(repo, v1[:10], v2)
        assert focus.sha == v1
        assert focus.ref == v1[:10]
        assert focus.same_base
        assert not focus.unchanged

    def test_same_commit_is_unchanged(self, repo):
        v1 = rev_parse(repo, "HEAD")
        assert resolve_since(repo, "HEAD", v1).unchanged

    def test_rebased_without_change_is_unchanged(self, repo):
        v1 = rev_parse(repo, "HEAD")
        v1r = _rebase_onto_new_base(repo, v1)
        focus = resolve_since(repo, v1, v1r)
        assert not focus.same_base
        assert focus.unchanged

    def test_rebased_and_changed(self, repo):
        v1 = rev_parse(repo, "HEAD")
        _rebase_onto_new_base(repo, v1)
        v2 = _amend(repo)
        focus = resolve_since(repo, v1, v2)
        assert not focus.same_base
        assert not focus.unchanged

    def test_message_change_is_a_change(self, repo):
        v1 = rev_parse(repo, "HEAD")
        _git(repo, "commit", "-q", "--amend", "-m", "LU-1 llite: reworded")
        v2 = rev_parse(repo, "HEAD")
        assert not resolve_since(repo, v1, v2).unchanged

    def test_unknown_rev(self, repo):
        with pytest.raises(ValueError, match="does not name a commit"):
            resolve_since(repo, "deadbeef" * 5, rev_parse(repo, "HEAD"))

    def test_ancestor_is_not_an_earlier_version(self, repo):
        with pytest.raises(ValueError, match="is an ancestor"):
            resolve_since(repo, "HEAD~1", rev_parse(repo, "HEAD"))

    def test_descendant_is_not_an_earlier_version(self, repo):
        with pytest.raises(ValueError, match="is a descendant"):
            resolve_since(repo, "HEAD", rev_parse(repo, "HEAD~1"))

    def test_review_worktree_resolves_the_earlier_version(self, repo,
                                                          tmp_path):
        # The CLI resolves REV to a SHA in --repo; the review worktree
        # shares the object store, so that SHA is there even though a
        # ref such as HEAD@{1} would mean something else in it.
        v1 = rev_parse(repo, "HEAD")
        v2 = _amend(repo)
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
        dest = tmp_path / "wt"
        add_worktree(repo, dest, v2)
        try:
            assert rev_parse(dest, v1) == v1
            assert _git(dest, "range-diff", f"{v1}^!", f"{v2}^!")
        finally:
            remove_worktree(repo, dest)


class TestFocusPrompt:

    def test_same_base(self):
        text = focus_prompt(SinceFocus("HEAD@{1}", OLD, same_base=True), NEW)
        assert f"git range-diff --creation-factor=999 {OLD}^! {NEW}^!" \
            in text
        assert f"git diff {OLD} {NEW}" in text
        assert "exactly the interdiff" in text
        # The reviewer runs commands in its own worktree, where the
        # ref as given may mean something else: full SHAs only.
        assert "HEAD@{1}" not in text

    def test_rebased(self):
        text = focus_prompt(SinceFocus(OLD, OLD, same_base=False), NEW)
        assert "range-diff" in text
        assert "different parents" in text
        assert "unrelated" in text
        assert "exactly the interdiff" not in text

    def test_limits_findings_to_the_change(self):
        text = focus_prompt(SinceFocus(OLD, OLD, same_base=True), NEW)
        assert "Findings must be about the lines and hunks that changed" \
            in text
        assert "only if it is a real bug" in text
        assert "never style" in text

    def test_review_prompt_gains_the_section(self, tmp_path):
        config = BatchConfig(repo=tmp_path, results_dir=tmp_path / "r",
                             worktrees_dir=tmp_path / "w",
                             prompts_dir=Path("/p/kernel"))
        change = LocalChange(ref_name="HEAD", sha=NEW, subject="s")
        plain = review_prompt(config, change)
        assert "Focus:" not in plain
        change.since = SinceFocus(OLD, OLD, same_base=True)
        focused = review_prompt(config, change)
        bare = plain.removesuffix(".\n\n" + CLOSING_NOTE)
        assert focused.startswith(bare + ".\n\nFocus: ")
        assert focused.endswith(CLOSING_NOTE)
        assert "review-core.md" in focused

    def test_light_and_memory_compose(self, tmp_path):
        config = BatchConfig(repo=tmp_path, results_dir=tmp_path / "r",
                             worktrees_dir=tmp_path / "w", mode="light",
                             memory_db=tmp_path / "db")
        change = LocalChange(ref_name="HEAD", sha=NEW, subject="s",
                             change_id="I" + "e" * 39,
                             since=SinceFocus(OLD, OLD, same_base=True))
        prompt = review_prompt(config, change)
        assert "light regression review" in prompt
        assert "your review memory document" in prompt
        assert prompt.index("memory document") < prompt.index("Focus:")


class TestArtifacts:

    def _result(self):
        change = LocalChange(ref_name="HEAD", sha=NEW, subject="s",
                             since=SinceFocus("v1", OLD, same_base=True))
        return ReviewResult(change, STATUS_CLEAN)

    def test_summary_records_since(self, tmp_path):
        update_summary(tmp_path, [self._result()])
        entry = json.loads((tmp_path / "summary.json").read_text())
        (entry,) = entry.values()
        assert entry["since"] == {"ref": "v1", "sha": OLD}

    def test_summary_without_since(self, tmp_path):
        result = self._result()
        result.change.since = None
        update_summary(tmp_path, [result])
        (entry,) = json.loads(
            (tmp_path / "summary.json").read_text()).values()
        assert entry["since"] is None

    def test_text_dump_and_markdown(self):
        from lreview.markdown import review_markdown
        from lreview.text import result_text
        result = self._result()
        label = f"changes since v1 ({OLD[:12]})"
        assert f"focus:  {label}" in result_text(result)
        assert f"- **Focus:** {label}" in review_markdown(result.change,
                                                           None)


class TestCli:

    def _run(self, repo, tmp_path, monkeypatch, *argv):
        from lreview.prompts import PromptsStatus
        captured = {}

        def fake_run_batch(config, changes, in_place=False):
            captured["changes"] = changes
            captured["in_place"] = in_place
            return []

        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(available=True,
                                       prompts_dir=Path("/p/kernel"),
                                       source="test"))
        monkeypatch.setattr("lreview.cli.check_prompts_freshness",
                            lambda *a, **k: None)
        monkeypatch.setattr("lreview.cli.run_batch", fake_run_batch)
        args = build_parser().parse_args(
            ["run", "--repo", str(repo),
             "--results-dir", str(tmp_path / "results"), *argv])
        return cmd_run(args), captured

    def test_parses(self):
        args = build_parser().parse_args(["run", "--last", "1",
                                          "--since", "abc123"])
        assert args.since == "abc123"
        assert build_parser().parse_args(["run"]).since is None

    def test_last_one(self, repo, tmp_path, monkeypatch):
        v1 = rev_parse(repo, "HEAD")
        v2 = _amend(repo)
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--last", "1", "--since", v1)
        assert rc == 0
        (change,) = got["changes"]
        assert change.sha == v2
        assert change.since.sha == v1
        assert change.since.same_base

    def test_head_in_place_with_reflog_ref(self, repo, tmp_path,
                                           monkeypatch):
        v1 = rev_parse(repo, "HEAD")
        _amend(repo)
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--since", "HEAD@{1}")
        assert rc == 0
        assert got["in_place"]
        assert got["changes"][0].since.sha == v1

    def test_unchanged_skips_the_review(self, repo, tmp_path, monkeypatch,
                                        capsys):
        out = tmp_path / "dump.txt"
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--last", "1", "--since", "HEAD", "-o", str(out))
        assert rc == 0
        assert "changes" not in got
        assert "nothing changed" in capsys.readouterr().out
        assert "nothing changed, no review run" in out.read_text()

    def test_bad_rev(self, repo, tmp_path, monkeypatch, capsys):
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--last", "1", "--since", "nosuchref")
        assert rc == 1
        assert "changes" not in got
        assert "does not name a commit" in capsys.readouterr().out

    def test_one_commit_only(self, repo, tmp_path, monkeypatch, capsys):
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--last", "2", "--since", "HEAD")
        assert rc == 1
        assert "single commit" in capsys.readouterr().out

    def test_not_for_gerrit_changes(self, repo, tmp_path, monkeypatch,
                                    capsys):
        rc, _ = self._run(repo, tmp_path, monkeypatch,
                          "64086", "--since", "HEAD")
        assert rc == 1
        assert "local reviews only" in capsys.readouterr().out

    def test_change_id_mismatch_is_noted(self, repo, tmp_path, monkeypatch,
                                         capsys):
        v1 = rev_parse(repo, "HEAD")
        _amend(repo, message="LU-2 other\n\nChange-Id: I" + "2" * 40)
        rc, got = self._run(repo, tmp_path, monkeypatch,
                            "--last", "1", "--since", v1)
        assert rc == 0
        assert "really an earlier version" in capsys.readouterr().out
