"""Tests for lreview bench."""

import json
import os
import stat
import subprocess

import pytest

from lreview import bench
from lreview.runner import BatchConfig


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    """A repository with a case commit and a later 'fix' on top."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.c").write_text("int a;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "LU-1 llite: the change\n\nChange-Id: I1")
    case_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.c").write_text("int a = 0;\n")
    _git(repo, "commit", "-qam", "LU-2 llite: the fix\n\nFixes: x")
    return repo, case_sha


def _case(sha, **extra):
    return {"id": "c1", "change": 1, "patchset": 1, "sha": sha,
            "subject": "LU-1 llite: the change", "lines": 1, "kind": "bug",
            "bugs": [{"id": "b1", "summary": "a is not initialized",
                      "match": [["uninitiali[sz]ed|not initiali[sz]ed",
                                 r"\ba\b"]]}], **extra}


def test_select():
    cases = [{"id": "x"}, {"id": "y"}]
    assert bench.select(cases, None) == cases
    assert bench.select(cases, "y") == [{"id": "y"}]
    with pytest.raises(ValueError, match="no such case"):
        bench.select(cases, "z")


def test_regex_found():
    bug = {"match": [["swabber", "rmf_size"], ["never swabbed"]]}
    assert bench.regex_found(bug, ["nothing", "the swabber passes rmf_size"]) == 1
    assert bench.regex_found(bug, ["dv1 is never swabbed"]) == 0
    assert bench.regex_found(bug, ["swabber alone"]) is None


def test_repos_hold_only_the_case_history(tmp_path, source):
    src, sha = source
    later = _git(src, "rev-parse", "HEAD")
    cases = [_case(sha), dict(_case(later), id="c2")]
    repos = bench.prepare_repos(tmp_path / "bench", cases, src)
    assert _git(repos["c1"], "rev-parse", f"{sha}^{{commit}}") == sha
    # Each case repository reaches nothing from git log --all, not even
    # the other case's newer commit, though its objects are shared.
    for repo in repos.values():
        assert _git(repo, "log", "--all", "--oneline") == ""
    _git(repos["c1"], "worktree", "add", "-q", "--detach",
         str(tmp_path / "wt"), sha)
    assert later not in _git(repos["c1"], "log", "--all", "--format=%H")
    assert not (tmp_path / "bench" / "store.git" / "FETCH_HEAD").exists()
    # A second prepare is a no-op
    assert bench.prepare_repos(tmp_path / "bench", cases, src) == repos


def _rep(label_dir, rep, case_id, findings, cost=0.5):
    d = label_dir / f"rep{rep}"
    d.mkdir(parents=True)
    slug = f"bench-{case_id}_abcdef0"
    (d / f"review-metadata-{slug}.json").write_text("{}")
    if findings:
        (d / f"gerrit-review-{slug}.json").write_text(json.dumps(
            {"message": "overall", "comments": {"a.c": [
                {"line": 1, "message": m} for m in findings]}}))
    events = [
        {"type": "assistant", "timestamp": "2026-10-09T10:00:00Z",
         "message": {"id": "m1", "model": "claude-opus-5-5",
                     "usage": {"input_tokens": 1,
                               "cache_creation_input_tokens": 100,
                               "cache_read_input_tokens": 0,
                               "output_tokens": 10}, "content": []}},
        {"type": "result", "subtype": "success", "total_cost_usd": cost,
         "duration_ms": 60000, "usage": {"output_tokens": 10}}]
    (d / f"kreview-{slug}-20261009-1.1.log").write_text(
        "\n".join(json.dumps(e) for e in events))


def test_score_and_render(tmp_path):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["a is uninitialized here"])
    _rep(label, 2, "c1", [], cost=0.3)
    cases = [_case("0" * 40)]
    scored = bench.score(label, cases)
    s = bench.summarize(scored)
    assert s["reviews"] == 2 and s["incomplete"] == 0
    assert (s["bugs_found"], s["bug_chances"]) == (1, 2)
    assert (s["bugs_found_any_rep"], s["bugs_known"]) == (1, 1)
    assert s["cost_mean"] == pytest.approx(0.4)
    assert s["findings_mean"] == 0.5
    text = bench.render([scored])
    assert "rep1: known bugs 1/1" in text and "rep2: known bugs 0/1" in text
    assert "b1+" in text and "b1-" in text


def test_judge_is_cached(tmp_path, monkeypatch):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["something else"])
    calls = []

    def fake(case, bug, findings, model=bench.JUDGE_MODEL):
        calls.append(1)
        return {"found": True, "finding": 0, "why": "same bug"}
    monkeypatch.setattr(bench, "llm_found", fake)
    cases = [_case("0" * 40)]
    assert bench.summarize(bench.score(label, cases, judge=True),
                           judge=True)["bugs_found"] == 1
    bench.score(label, cases, judge=True)
    assert len(calls) == 1
    assert (label / "rep1" / "judge.json").exists()


STUB = """#!/bin/sh
echo '{"type":"system","subtype":"init","model":"claude-opus-5-5"}'
printf '{"comments":{"a.c":[{"line":1,"message":"a is not initialized"}]}}' > gerrit-review.json
printf '{"issue-severity-score":"low"}' > review-metadata.json
echo '{"type":"result","subtype":"success","total_cost_usd":0.1,"duration_ms":1000,"usage":{"output_tokens":5}}'
"""


def test_run_bench_end_to_end(tmp_path, source, monkeypatch):
    src, sha = source
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    bench_dir = tmp_path / "bench"
    cases = [_case(sha)]
    repos = bench.prepare_repos(bench_dir, cases, src)
    label = bench_dir / "arm"
    prompts = tmp_path / "prompts"
    prompts.mkdir()

    def config_for(results_dir, repo):
        return BatchConfig(repo=repo, results_dir=results_dir,
                           worktrees_dir=bench_dir / "worktrees",
                           prompts_dir=prompts, memory_db=label / "db")
    bench.run_bench(config_for, cases, label, 2, repos, log=lambda *a: None)
    meta = json.loads((label / bench.RUN_FILE).read_text())
    assert [r["rep"] for r in meta["reps"]] == [1, 2]
    assert meta["memory"] is True and meta["environment"]["memory"] is True
    s = bench.summarize(bench.score(label, cases))
    assert (s["bugs_found"], s["bug_chances"]) == (2, 2)


def test_bundled_cases_are_well_formed():
    data = bench.load_cases()
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids))
    for case in data["cases"]:
        assert len(case["sha"]) == 40
        assert case["kind"] in ("bug", "control")
        assert "eval" in case["sets"]
        assert bool(case.get("bugs")) == (case["kind"] == "bug")
        for bug in case.get("bugs") or []:
            assert bug["summary"] and bug["match"]
            for patterns in bug["match"]:
                for pattern in patterns:
                    __import__("re").compile(pattern)


def test_leak_is_reported(tmp_path):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["a is uninitialized"])
    log = next((label / "rep1").glob("kreview-*.log"))
    with open(log, "a") as handle:
        handle.write("\n" + json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": "abcdef1234 LU-2 llite: the fix"}]}}))
    case = _case("0" * 40)
    case["bugs"][0].update(fix_sha="abcdef1234567", fix_subject="LU-2 llite: the fix")
    scored = bench.score(label, [case])
    summary = bench.summarize(scored)
    assert summary["leaked"] == 1
    assert summary["bug_chances"] == 0     # the leaked review is not counted
    assert "LEAKED" in bench.render([scored])


def test_select_by_set():
    cases = [{"id": "x", "sets": ["quick", "eval"]}, {"id": "y", "sets": ["eval"]}]
    assert [c["id"] for c in bench.select(cases, None, "quick")] == ["x"]
    assert len(bench.select(cases, None, "eval")) == 2
    assert len(bench.select(cases, None, "all")) == 2
    assert [c["id"] for c in bench.select(cases, "y", "quick")] == ["y"]
    with pytest.raises(ValueError, match="no cases in set"):
        bench.select(cases, None, "nope")


def test_offline_env_shadows_network_commands(tmp_path):
    env = bench.offline_env(tmp_path)
    shims = tmp_path / "offline-bin"
    assert env["PATH"].startswith(str(shims) + os.pathsep)
    result = subprocess.run(["curl", "https://example.invalid"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 7 and "disabled" in result.stderr
