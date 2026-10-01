import pytest

from lreview.github import resolve_pull_request


def test_resolve_github_pr_records_exact_range():
    def request(path):
        assert path == "/repos/acme/widget/pulls/42"
        return {"title": "Fix", "html_url": "https://github.com/acme/widget/pull/42",
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40, "ref": "fix", "repo": {"full_name": "fork/widget"}}}
    pr = resolve_pull_request("https://github.com/acme/widget/pull/42", request)
    assert (pr.base_sha, pr.sha, pr.project, pr.ref) == ("a" * 40, "b" * 40, "acme/widget", "refs/pull/42/head")


def test_rejects_noncanonical_pr_url():
    with pytest.raises(ValueError):
        resolve_pull_request("acme/widget#42")


@pytest.fixture
def pr_range(tmp_path):
    """(repo, base, head) where head adds src.c, lines 1-2."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "src.c").write_text("int a;\nint b;\n")
    git("add", ".")
    git("commit", "-q", "-m", "head")
    return tmp_path, base, git("rev-parse", "HEAD")


def _inline(**extra):
    return {"version": 1, "message": "m", "findings": [
        {"path": "src.c", "line": 2, "message": "b is unused", **extra}]}


@pytest.mark.parametrize("extra", [{}, {"side": "RIGHT"}, {"side": "right"}])
def test_inline_finding_on_the_added_side(pr_range, extra):
    from lreview.artifacts import validate_review_result
    validate_review_result(_inline(**extra), *pr_range)


@pytest.mark.parametrize("side", ["LEFT", "middle", None, 1])
def test_inline_finding_side_must_be_right(pr_range, side):
    """Inline findings name added lines, which exist only on the RIGHT
    side; anything else fails the whole GitHub review at post time."""
    from lreview.artifacts import validate_review_result
    with pytest.raises(ValueError, match="side"):
        validate_review_result(_inline(side=side), *pr_range)
