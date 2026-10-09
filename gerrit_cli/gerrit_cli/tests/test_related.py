"""`gerrit related`: each entry's subject, status and commits.

Gerrit's /related endpoint nests the subject and sha under "commit", so
reading a top-level "subject" left every entry's subject empty.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from gerrit_cli.client import GerritCommentsClient


def _entry(number, status, sha, subject, ps=1, current=1):
    return {
        "project": "fs/lustre-release",
        "change_id": f"I{number:040d}",
        "commit": {
            "commit": sha,
            "parents": [{"commit": "0" * 40}],
            "author": {"name": "A", "email": "a@example.com"},
            "subject": subject,
        },
        "_change_number": number,
        "_revision_number": ps,
        "_current_revision_number": current,
        "status": status,
    }


RELATED = [
    _entry(300, "NEW", "c" * 40, "LU-3 llite: tip", ps=2, current=2),
    _entry(200, "MERGED", "b" * 40, "LU-2 osc: middle", ps=1, current=2),
    _entry(150, "ABANDONED", "e" * 40, "LU-5 mdc: dropped"),
    _entry(100, "MERGED", "a" * 40, "LU-1 lov: root"),
]


def _run(capsys, client):
    from gerrit_cli.cli import cmd_related
    args = argparse.Namespace(url="300", pretty=False)
    with patch("gerrit_cli.cli.GerritCommentsClient") as MockClient, \
         pytest.raises(SystemExit) as exc_info:
        MockClient.parse_gerrit_url.return_value = (
            "https://example.com", 300)
        MockClient.return_value = client
        cmd_related(args)
    assert exc_info.value.code == 0
    return json.loads(capsys.readouterr().out)


def test_related_entries_carry_subject_status_and_commits(capsys):
    client = MagicMock()
    client.get_related_changes.return_value = RELATED
    client.get_current_revisions.return_value = {
        200: "d" * 40, 100: "a" * 40,
    }

    data = _run(capsys, client)

    client.get_current_revisions.assert_called_once_with([200, 100])
    series = {e["change_number"]: e for e in data["series"]}
    assert [e["change_number"] for e in data["series"]] == [300, 200, 150, 100]
    assert series[300] == {
        "change_number": 300, "patchset": 2, "current_patchset": 2,
        "status": "NEW", "subject": "LU-3 llite: tip", "is_current": True,
        "change_id": "I" + "300".zfill(40), "commit": "c" * 40,
        "merged_commit": None,
    }
    # A merged change's landed commit is its current revision, not the
    # older patchset the relation chain names.
    assert series[200]["subject"] == "LU-2 osc: middle"
    assert series[200]["commit"] == "b" * 40
    assert series[200]["merged_commit"] == "d" * 40
    assert series[100]["merged_commit"] == "a" * 40
    assert series[150]["status"] == "ABANDONED"
    assert series[150]["merged_commit"] is None


def test_related_without_merged_changes_makes_no_extra_query(capsys):
    client = MagicMock()
    client.get_related_changes.return_value = [RELATED[0], RELATED[2]]

    data = _run(capsys, client)

    client.get_current_revisions.assert_not_called()
    assert [e["subject"] for e in data["series"]] == [
        "LU-3 llite: tip", "LU-5 mdc: dropped",
    ]


@patch("gerrit_cli.client.GerritRestAPI")
@patch("gerrit_cli.client.HTTPBasicAuth")
def test_current_revisions_is_one_query(mock_auth, mock_api):
    rest = MagicMock()
    rest.get.return_value = [
        {"_number": 200, "current_revision": "d" * 40},
        {"_number": 100, "current_revision": "a" * 40},
    ]
    mock_api.return_value = rest

    client = GerritCommentsClient()
    result = client.get_current_revisions([200, 100])

    rest.get.assert_called_once()
    url = rest.get.call_args[0][0]
    assert "change%3A200%20OR%20change%3A100" in url
    assert "n=2" in url
    assert "o=CURRENT_REVISION" in url
    assert result == {200: "d" * 40, 100: "a" * 40}
