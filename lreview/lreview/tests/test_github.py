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
