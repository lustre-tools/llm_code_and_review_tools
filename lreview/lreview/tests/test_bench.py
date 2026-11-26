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
    monkeypatch.chdir(tmp_path)
    bench_dir = tmp_path / "bench"
    cases = [_case(sha)]
    repos = bench.prepare_repos(bench_dir, cases, src)
    label = bench_dir / "arm"
    prompts = tmp_path / "prompts"
    prompts.mkdir()

    def config_for(results_dir, repo):
        return BatchConfig(repo=repo, results_dir=results_dir,
                           worktrees_dir=bench_dir / "worktrees",
                           prompts_dir=prompts)
    bench.run_bench(config_for, cases, label, 2, repos, log=lambda *a: None)
    meta = json.loads((label / bench.RUN_FILE).read_text())
    assert [r["rep"] for r in meta["reps"]] == [1, 2]
    s = bench.summarize(bench.score(label, cases))
    assert (s["bugs_found"], s["bug_chances"]) == (2, 2)


def test_bundled_cases_are_well_formed():
    data = bench.load_cases()
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids))
    for case in data["cases"]:
        assert len(case["sha"]) == 40
        assert case["kind"] in ("bug", "control")
        assert set(case["sets"]) <= {"quick", "eval", "hard"}
        assert ("eval" in case["sets"]) != ("hard" in case["sets"])
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


def test_cursor_offline_rules():
    from lreview.bench import OFFLINE_COMMANDS, offline_rules
    allow, deny = offline_rules()
    assert allow == ["Shell(*)"]
    assert "Shell(curl:*)" in deny and "Shell(jira:*)" in deny
    assert len(deny) == len(OFFLINE_COMMANDS)


def test_agy_offline_rules():
    from lreview.bench import OFFLINE_COMMANDS, agy_offline_rules
    deny = agy_offline_rules()
    assert {"read_url(*)", "execute_url(*)", "download_url(*)",
            "mcp(*)"} <= set(deny)
    assert "command(curl)" in deny and "command(jira)" in deny
    assert len(deny) == 4 + len(OFFLINE_COMMANDS)


def test_agy_leak_is_reported(tmp_path):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["a is uninitialized"])
    log = next((label / "rep1").glob("kreview-*.log"))
    with open(log, "a") as handle:
        handle.write("\n" + json.dumps({
            "event": "step_update",
            "step_update": {"step_index": 2, "state": "DONE",
                            "step_type": "tool", "tool_name": "run_command",
                            "tool_info": {"name": "run_command",
                                          "output": "abcdef1234 LU-2 llite: the fix"}}}))
    case = _case("0" * 40)
    case["bugs"][0].update(fix_sha="abcdef1234567", fix_subject="LU-2 llite: the fix")
    scored = bench.score(label, [case])
    assert bench.summarize(scored)["leaked"] == 1


def test_offline_env_shadows_network_commands(tmp_path):
    env = bench.offline_env(tmp_path)
    shims = tmp_path / "offline-bin"
    assert env["PATH"].startswith(str(shims) + os.pathsep)
    result = subprocess.run(["curl", "https://example.invalid"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 7 and "disabled" in result.stderr


def test_render_bugs(tmp_path):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["a is uninitialized here"])
    _rep(label, 2, "c1", [])
    text = bench.render_bugs([bench.score(label, [_case("0" * 40)])])
    assert "c1/b1" in text and "1/2" in text


def test_fix_reusing_the_subject_is_no_leak(tmp_path):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["a is uninitialized"])
    log = next((label / "rep1").glob("kreview-*.log"))
    with open(log, "a") as handle:
        handle.write("\n" + json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": "commit 0000 LU-1 llite: the change"}]}}))
    case = _case("0" * 40)
    case["bugs"][0].update(fix_sha="abcdef1234567",
                           fix_subject="LU-1 llite: the change")
    assert bench.summarize(bench.score(label, [case]))["leaked"] == 0


def test_a_failed_verdict_is_asked_again_not_counted(tmp_path, monkeypatch):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["something else"])
    answers = [{"found": None, "finding": None, "why": "judge failed: "},
               {"found": True, "finding": 0, "why": "same bug"}]
    monkeypatch.setattr(bench, "llm_found",
                        lambda case, bug, findings, model=None: answers.pop(0))
    cases = [_case("0" * 40)]
    s = bench.summarize(bench.score(label, cases, judge=True), judge=True)
    assert (s["bugs_found"], s["bug_chances"], s["unjudged"]) == (0, 0, 1)
    assert json.loads((label / "rep1" / "judge.json").read_text()) == {}
    s = bench.summarize(bench.score(label, cases, judge=True), judge=True)
    assert (s["bugs_found"], s["bug_chances"], s["unjudged"]) == (1, 1, 0)


def test_the_judges_last_verdict_counts(monkeypatch):
    text = ('{"found": true, "finding": 3, "why": "first"}\n\nCorrection: '
            '{"found": false, "finding": null, "why": "second"}')

    class Done:
        stdout = json.dumps({"result": text})
        stderr = ""
    monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: Done())
    v = bench.llm_found(_case("0" * 40), _case("0" * 40)["bugs"][0], ["x"])
    assert v == {"found": False, "finding": None, "why": "second"}


def test_prepare_repos_takes_a_lock(tmp_path, source):
    repo, sha = source
    repos = bench.prepare_repos(tmp_path / "bench", [_case(sha)], repo)
    assert (tmp_path / "bench" / ".prepare.lock").exists()
    assert set(repos) == {"c1"}


def test_render_says_when_verdicts_failed(tmp_path, monkeypatch):
    label = tmp_path / "arm"
    _rep(label, 1, "c1", ["something else"])
    monkeypatch.setattr(bench, "llm_found", lambda *a, **k: {
        "found": None, "finding": None, "why": "judge failed: "})
    text = bench.render([bench.score(label, [_case("0" * 40)], judge=True)],
                        judge=True)
    assert "1 verdict(s) failed" in text


def _scored(label, verdicts, cost=1.0):
    """verdicts: {case: {bug: [found per rep]}}"""
    reps = max(len(v) for bugs in verdicts.values() for v in bugs.values())
    return {"label": label, "reps": [
        {"rep": f"rep{i + 1}", "rows": [
            {"case": case, "complete": True, "findings": 1, "cost": cost,
             "bugs": {b: {"regex": v[i]} for b, v in bugs.items()
                      if i < len(v)}}
            for case, bugs in verdicts.items()
            if any(i < len(v) for v in bugs.values())]}
        for i in range(reps)]}


def test_classify_bug():
    from lreview.bench import classify_bug
    assert classify_bug([2, 2], [0, 1]) == "lost"
    assert classify_bug([2, 2], [1, 2]) == "weaker"
    assert classify_bug([2, 2], [1, 1]) == "same"
    assert classify_bug([0, 2], [1, 2]) == "gained"
    assert classify_bug([0, 2], [0, 2]) == "same"
    assert classify_bug([1, 2], [0, 2]) == "flips"
    assert classify_bug([1, 1], [0, 1]) == "thin"
    assert classify_bug([1, 1], [2, 2]) == "same"


def test_compare_pools_the_baseline_and_pairs_by_bug():
    from lreview.bench import compare, render_compare
    base = [_scored("b1", {"a": {"x": [True], "y": [True]},
                           "c": {"z": [False]}, "only": {"q": [True]}}, 2.0),
            _scored("b2", {"a": {"x": [True], "y": [False]},
                           "c": {"z": [False]}}, 2.0)]
    variant = [_scored("v", {"a": {"x": [False], "y": [True]},
                             "c": {"z": [True]}}, 1.0)]
    c = compare(base, variant)
    assert {(r["bug"], r["verdict"]) for r in c["bugs"]} == {
        ("x", "lost"), ("y", "flips"), ("z", "gained")}
    assert c["baseline_rate"] == 1.5 and c["variant_rate"] == 2.0
    assert c["gain"] == 0.5 and c["gain_se"] > 0
    assert c["repeat"] == ["a", "c"] and c["not_compared"] == 1
    assert c["cost"] == (2.0, 1.0) and c["cheaper"] == 2
    text = render_compare(c, {"effort": (['"high"'], ['"medium"'])})
    assert "a/x  2/2 -> 0/1" in text and 'effort: "high" -> "medium"' in text
    assert "--cases a,c" in text
    left = compare(base, variant, without=("x", "q"))
    assert {r["bug"] for r in left["bugs"]} == {"y", "z"}
    assert left["not_compared"] == 0


def test_compare_without_shared_bugs():
    from lreview.bench import compare, render_compare
    c = compare([_scored("b", {"a": {"x": [True]}})],
                [_scored("v", {"c": {"z": [True]}})])
    assert c["bugs"] == [] and c["cost"] is None
    assert "no known bug has reviews in both" in render_compare(c, {})


def test_environment_differences(tmp_path):
    from lreview.bench import environment_differences, environments
    for name, env in (("b", {"model": "sonnet", "cases_file": "/x"}),
                      ("v", {"model": "sonnet", "cases_file": "/y",
                             "agent_version": "2"})):
        (tmp_path / name).mkdir()
        (tmp_path / name / "bench-run.json").write_text(
            json.dumps({"environment": env}))
    b, v = environments([tmp_path / "b"]), environments([tmp_path / "v"])
    assert environment_differences(b, v) == {
        "agent_version": (["null"], ['"2"'])}
    assert environments([tmp_path / "none"]) == [{}]


def test_compare_command(tmp_path, capsys):
    from lreview.cli import build_parser, cmd_bench
    parse = build_parser().parse_args
    args = parse(["bench", "compare", "v", "--baseline", "b1", "b2",
                  "--bench-dir", str(tmp_path)])
    assert args.labels == ["v"] and args.baseline == ["b1", "b2"]
    assert cmd_bench(args) == 1
    assert "no such arm" in capsys.readouterr().out
    (tmp_path / "v").mkdir()
    args = parse(["bench", "compare", "v", "--baseline", "v",
                  "--bench-dir", str(tmp_path)])
    assert cmd_bench(args) == 1
    assert "both sides" in capsys.readouterr().out
