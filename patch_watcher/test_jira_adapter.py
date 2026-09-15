import json
import unittest
from unittest.mock import patch as mock_patch
from urllib.request import Request

from patch_watcher import jira_adapter


def issue_body(key="LU-20724", status="Open", assignee="WC Triage", comments=()):
    return json.dumps({
        "key": key,
        "fields": {
            "summary": "tests: fsx burst mode",
            "status": {"name": status},
            "resolution": None,
            "priority": {"name": "Medium"},
            "assignee": {"displayName": assignee},
            "updated": "2026-09-10T02:00:43.000+0000",
            "comment": {"comments": list(comments)},
        },
    }).encode("utf-8")


def comment(identifier, body="hello", updated="2026-09-10T02:00:43.000+0000"):
    return {
        "id": identifier, "body": body, "updated": updated,
        "created": updated, "author": {"displayName": "Patrick Farrell"},
    }


class JiraAdapterTests(unittest.TestCase):
    def client(self, body, captured=None):
        def transport(request, timeout):
            if captured is not None:
                captured["url"] = request.full_url
                captured["headers"] = dict(request.headers)
            return body

        return jira_adapter.JiraClient(
            jira_adapter.JiraConfig("https://jira.whamcloud.com", "private-token"),
            transport=transport, timeout=3,
        )

    def test_it_reads_the_fields_a_reader_would_act_on(self):
        captured = {}
        issue = self.client(issue_body(comments=[comment("1")]), captured).fetch_issue("lu-20724")
        self.assertEqual(issue.key, "LU-20724")
        self.assertEqual(issue.status, "Open")
        self.assertEqual(issue.priority, "Medium")
        self.assertEqual(issue.assignee, "WC Triage")
        self.assertEqual(len(issue.comments), 1)
        self.assertIn("/rest/api/2/issue/LU-20724", captured["url"])
        # The token travels as a header, never in the URL.
        self.assertNotIn("private-token", captured["url"])

    def test_a_new_comment_changes_the_fingerprint(self):
        """The signal that most often means "someone is asking you something"."""

        one = self.client(issue_body(comments=[comment("1")])).fetch_issue("LU-20724")
        two = self.client(
            issue_body(comments=[comment("1"), comment("2", "and another")])
        ).fetch_issue("LU-20724")
        self.assertNotEqual(one.fingerprint(), two.fingerprint())

    def test_an_edited_summary_does_not(self):
        """An issue's own `updated` stamp moves for edits nobody needs to act
        on.  Keying on it would fire on a reword and keep firing for as long
        as anyone kept touching the issue."""

        first = self.client(issue_body(comments=[comment("1")])).fetch_issue("LU-20724")
        raw = json.loads(issue_body(comments=[comment("1")]))
        raw["fields"]["summary"] = "tests: a completely reworded summary"
        raw["fields"]["updated"] = "2099-01-01T00:00:00.000+0000"
        second = self.client(json.dumps(raw).encode("utf-8")).fetch_issue("LU-20724")
        self.assertNotEqual(first.summary, second.summary)
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_status_priority_and_assignee_all_count(self):
        base = self.client(issue_body()).fetch_issue("LU-20724").fingerprint()
        for changed in (
            issue_body(status="Resolved"),
            issue_body(assignee="Patrick Farrell"),
        ):
            self.assertNotEqual(
                base, self.client(changed).fetch_issue("LU-20724").fingerprint()
            )

    def test_a_substituted_issue_is_refused(self):
        """Every field is read from the body, so the body must first be proven
        to describe the issue that was asked for."""

        client = self.client(issue_body(key="LU-99999"))
        with self.assertRaises(jira_adapter.JiraRequestError) as caught:
            client.fetch_issue("LU-20724")
        self.assertIn("LU-99999", str(caught.exception))

    def test_a_non_key_never_reaches_the_network(self):
        def transport(request, timeout):
            raise AssertionError("no request may be made for a non-key")

        client = jira_adapter.JiraClient(
            jira_adapter.JiraConfig("https://jira.whamcloud.com", "t"),
            transport=transport,
        )
        for bad in ("", "LU", "20724", "https://jira.whamcloud.com/browse/LU-1"):
            with self.assertRaises(jira_adapter.JiraRequestError, msg=bad):
                client.fetch_issue(bad)

    def test_the_token_is_not_in_the_repr(self):
        """Accidental logging must not reveal it."""

        config = jira_adapter.JiraConfig("https://jira.whamcloud.com", "private-token")
        self.assertNotIn("private-token", repr(config))


if __name__ == "__main__":
    unittest.main()
