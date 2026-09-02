"""GitHub pull request resolution and posting helpers (stdlib only)."""
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

_PR = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")
API = "https://api.github.com"


def github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GitHub authentication missing: set GH_TOKEN or GITHUB_TOKEN")
    return token


def github_request(path: str, method="GET", data=None, token=None):
    token = token or github_token()
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(API + path, data=body, method=method,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lreview"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ValueError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc


@dataclass
class ResolvedGitHubPullRequest:
    owner: str; repo_name: str; number: int; subject: str; sha: str; base_sha: str
    head_ref: str; fetch_repo: str; url: str
    provider: str = "github"; patchset: None = None; base_url: str = ""
    @property
    def slug(self): return f"github_{self.owner}_{self.repo_name}_{self.number}_{self.sha[:7]}"
    @property
    def project(self): return f"{self.owner}/{self.repo_name}"
    @property
    def ref(self): return f"refs/pull/{self.number}/head"
    def fetch_url(self): return f"https://github.com/{self.owner}/{self.repo_name}.git"


def resolve_pull_request(url: str, request=github_request) -> ResolvedGitHubPullRequest:
    match = _PR.match(url)
    if not match:
        raise ValueError("--github must be a canonical https://github.com/OWNER/REPO/pull/NUMBER URL")
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    data = request(f"/repos/{owner}/{repo}/pulls/{number}")
    if not data.get("head", {}).get("sha") or not data.get("base", {}).get("sha"):
        raise ValueError("GitHub PR response did not include base/head SHAs")
    return ResolvedGitHubPullRequest(owner, repo, number, data.get("title", ""), data["head"]["sha"],
        data["base"]["sha"], data["head"].get("ref", ""),
        data["head"].get("repo", {}).get("full_name", f"{owner}/{repo}"), data.get("html_url", url))
