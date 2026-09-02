"""Shared fixtures: change-dict builders using real message shapes
captured from review.whamcloud.com during the design exploration.

Default dates are RELATIVE to now — the classifier has freshness
boundaries (7d action-recent, 100d stalled), so absolute fixture dates
rot and start failing days after they are written."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime(
        "%Y-%m-%d %H:%M:%S.000000000")

ME = {"_account_id": 1055, "name": "Marc Vef", "email": "mvef@whamcloud.com", "username": "mvef"}
ADILGER = {"_account_id": 117, "name": "Andreas Dilger", "email": "adilger@whamcloud.com", "username": "adilger"}
JENKINS = {"_account_id": 683, "name": "jenkins", "username": "jenkins", "tags": ["SERVICE_USER"]}
MALOO = {"_account_id": 147, "name": "Maloo", "username": "maloo", "tags": ["SERVICE_USER"]}
AUTOTEST = {"_account_id": 403, "name": "Autotest", "username": "autotest", "tags": ["SERVICE_USER"]}
CHECKPATCH = {"_account_id": 377, "name": "wc-checkpatch", "username": "hpdd-checkpatch", "tags": ["SERVICE_USER"]}
JANITOR = {"_account_id": 799, "name": "Lustre Gerrit Janitor", "username": "lgerritjanitor"}


def msg(author, text, ps=2, date=None, tag=None):
    return {"author": author, "message": text, "_revision_number": ps,
            "date": date or days_ago(2), "tag": tag}


def vote(account, value, date=None):
    entry = dict(account)
    entry["value"] = value
    entry["date"] = date or days_ago(2)
    return entry


def mk_change(number=67221, ps=2, owner=ME, uploader=None, messages=None,
              verified=None, code_review=None, submit_records=None,
              status="NEW", subject="LU-1 test: subject", branch="master",
              project="fs/lustre-release", hashtags=None, wip=False,
              commit_message=None, unresolved=0, updated=None,
              kind="REWORK"):
    updated = updated or days_ago(2)
    rev_sha = "deadbeef"
    change = {
        "_number": number,
        "id": f"{project}~{branch}~I{number}",
        "change_id": f"I{number:07d}",
        "project": project,
        "branch": branch,
        "subject": subject,
        "status": status,
        "updated": updated,
        "created": days_ago(30),
        "meta_rev_id": f"meta{number}",
        "current_revision": rev_sha,
        "current_revision_number": ps,
        "unresolved_comment_count": unresolved,
        "hashtags": hashtags or [],
        "owner": owner,
        "insertions": 10,
        "deletions": 2,
        "messages": messages or [],
        "labels": {
            "Verified": {"all": verified or []},
            "Code-Review": {"all": code_review or []},
        },
        "revisions": {
            rev_sha: {
                "_number": ps,
                "kind": kind,
                "created": days_ago(4),
                "uploader": uploader or owner,
                "commit": {
                    "message": commit_message or f"{subject}\n\nSigned-off-by: X\nChange-Id: I{number}\n",
                },
            }
        },
    }
    if wip:
        change["work_in_progress"] = True
    if submit_records is not None:
        change["submit_records"] = submit_records
    return change


def mk_bundle(changes_roles, threads=None, watchlist=None, starred=None, next_queues=None):
    """changes_roles: list of (change, roles-set)."""
    return {
        "self": {"account_id": 1055, "username": "mvef", "name": "Marc Vef"},
        "changes": {c["_number"]: c for c, _ in changes_roles},
        "roles": {c["_number"]: set(roles) for c, roles in changes_roles},
        "starred": starred or set(),
        "watchlist": watchlist or [],
        "threads": threads or {},
        "next_queues": next_queues or {},
        "errors": [],
        "fetched_at": 1784300000.0,
    }


@pytest.fixture
def accounts():
    return {"me": ME, "adilger": ADILGER, "jenkins": JENKINS, "maloo": MALOO,
            "autotest": AUTOTEST, "checkpatch": CHECKPATCH, "janitor": JANITOR}
