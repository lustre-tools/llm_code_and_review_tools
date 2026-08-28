"""Gerrit change resolution for lreview.

Thin wrapper over gerrit-cli's client: resolve a change URL/number to
the current patchset's revision SHA and fetch ref. A URL that pins an
explicit patchset (.../+/61965/3) resolves to that patchset instead of
silently reviewing the current one.
"""

import re
from dataclasses import dataclass
from typing import Any, Optional

_URL_PATCHSET_RE = re.compile(r"/\+/\d+/(\d+)/?$")


@dataclass
class ResolvedChange:
    """A Gerrit change pinned to its current patchset."""
    number: int
    project: str
    subject: str
    sha: str
    patchset: int
    ref: str
    base_url: str
    change_id: Optional[str] = None

    @property
    def slug(self) -> str:
        """Stable per-review identifier used in file and dir names."""
        return f"{self.number}_ps{self.patchset}"

    def fetch_url(self) -> str:
        """Anonymous-fetch URL of the project on the Gerrit server."""
        return f"{self.base_url.rstrip('/')}/{self.project}"


def change_ref(number: int, patchset: int) -> str:
    """Construct the standard Gerrit ref for a change's patchset."""
    return f"refs/changes/{number % 100:02d}/{number}/{patchset}"


@dataclass
class LocalChange:
    """A local git ref to review — no Gerrit change behind it.

    Duck-typed against ResolvedChange for the runner (slug/sha/
    subject); number is None, which marks the result as local and
    not postable.
    """
    ref_name: str
    sha: str
    subject: str
    number: Optional[int] = None
    patchset: Optional[int] = None
    project: str = ""
    base_url: str = ""
    ref: str = ""
    change_id: Optional[str] = None

    @property
    def slug(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.ref_name).strip("_")
        return f"{safe[:40]}_{self.sha[:7]}"

    def fetch_url(self) -> str:
        return ""


def resolve_change(url_or_number: str, client: Optional[Any] = None) -> ResolvedChange:
    """Resolve a Gerrit URL or bare change number to a patchset.

    Resolves to the current patchset unless the URL pins an explicit
    one (.../+/<change>/<patchset>).
    """
    from gerrit_cli.client import GerritCommentsClient

    spec = str(url_or_number)
    base_url, number = GerritCommentsClient.parse_gerrit_url(spec)
    client = client or GerritCommentsClient(url=base_url)

    pinned = _URL_PATCHSET_RE.search(spec)
    detail = client.get_change_detail(number)

    if pinned:
        wanted_ps = int(pinned.group(1))
        for sha, revision in detail["revisions"].items():
            if revision.get("_number") == wanted_ps:
                break
        else:
            raise ValueError(
                f"change {number} has no patchset {wanted_ps}")
    else:
        sha = detail["current_revision"]
        revision = detail["revisions"][sha]

    patchset = revision["_number"]
    ref = revision.get("ref") or change_ref(number, patchset)

    return ResolvedChange(
        number=number,
        project=detail["project"],
        subject=detail.get("subject", ""),
        sha=sha,
        patchset=patchset,
        ref=ref,
        base_url=base_url,
        change_id=detail.get("change_id"),
    )
