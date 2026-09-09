import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patch_watcher import gerrit_status as status
from patch_watcher import reporting

_ERROR_LOG_SANDBOX = {}


def setUpModule():
    """Keep refresh-failure logging out of the operator's real error log.

    `refresh_patch` records every failed refresh through
    `reporting.log_structured_error`, and these tests drive that path
    deliberately -- more of them since malformed Gerrit payloads started
    landing there as typed failures instead of escaping. Without this the
    suite appends to ~/.local/state/patch-watcher/errors.jsonl.
    """

    sandbox = tempfile.TemporaryDirectory()
    _ERROR_LOG_SANDBOX["directory"] = sandbox
    _ERROR_LOG_SANDBOX["previous"] = reporting.DEFAULT_ERROR_LOG
    reporting.DEFAULT_ERROR_LOG = Path(sandbox.name) / "errors.jsonl"


def tearDownModule():
    if "previous" in _ERROR_LOG_SANDBOX:
        reporting.DEFAULT_ERROR_LOG = _ERROR_LOG_SANDBOX.pop("previous")
    sandbox = _ERROR_LOG_SANDBOX.pop("directory", None)
    if sandbox is not None:
        sandbox.cleanup()


def sample_change(*, raw_status="NEW", backport=False):
    message = "LU-12345: fix compressed pages\n\nBody\n"
    if backport:
        message += "\nLustre-change: https://review.whamcloud.com/123\n"
    revision = "d" * 40
    return {
        "_number": 61965,
        "project": "fs/lustre-release",
        "subject": "LU-12345: fix compressed pages",
        "status": raw_status,
        "updated": "2026-08-29 12:34:56.000000000",
        "work_in_progress": False,
        "owner": {"name": "Owner"},
        "current_revision": revision,
        "revisions": {
            revision: {
                "_number": 4,
                "ref": "refs/changes/65/61965/4",
                "created": "2026-08-29 12:00:00.000000000",
                "uploader": {"name": "Uploader"},
                "commit": {
                    "message": message,
                    "author": {"name": "Author"},
                },
            }
        },
        "labels": {
            "Verified": {"all": [
                {"name": "jenkins", "value": 1},
                {"name": "Maloo", "value": 1},
            ]},
            "Code-Review": {
                "all": [
                    {"name": "Owner", "value": 2},
                    {"name": "Reviewer A", "value": 1},
                    {"name": "Reviewer B", "value": 1},
                ],
                "approved": {"name": "Owner"},
            },
        },
        "unresolved_comment_count": 2,
        "messages": [
            {
                "_revision_number": 3,
                "date": "2026-08-28 10:00:00.000000000",
                "author": {"name": "jenkins"},
                "message": "Build Failed https://build.whamcloud.com/job/old/1/",
            },
            {
                "_revision_number": 4,
                "date": "2026-08-29 12:20:00.000000000",
                "author": {"name": "jenkins"},
                "message": "Build Successful https://build.whamcloud.com/job/lustre-reviews/42/",
            },
            {
                "_revision_number": 4,
                "date": "2026-08-29 12:30:00.000000000",
                "author": {"name": "Maloo"},
                "message": "Test sessions will be run for Build 42",
            },
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_loads_private_config_without_using_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            path.write_text(
                "GERRIT_URL=https://review.whamcloud.com\n"
                "GERRIT_USER=test-user\n"
                "GERRIT_PASS='secret value'\n"
                "REFRESH_INTERVAL_SECONDS=120\n"
                "EMAIL_ENABLED=yes\n"
                "EMAIL_TO=paf@mulberrytree.us\n"
                "SENDMAIL_PATH=/usr/sbin/sendmail\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            os.environ["GERRIT_USER"] = "wrong-environment-user"
            try:
                config = status.GerritConfig.load(path)
            finally:
                os.environ.pop("GERRIT_USER", None)
        self.assertEqual(config.username, "test-user")
        self.assertEqual(config.password, "secret value")
        self.assertEqual(config.refresh_interval, 120)
        self.assertTrue(config.email_enabled)
        self.assertEqual(config.email_to, "paf@mulberrytree.us")
        self.assertNotIn("secret", repr(config))

    def test_rejects_group_readable_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            path.write_text("GERRIT_URL=x\n", encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaisesRegex(status.GerritConfigError, "unsafe"):
                status.GerritConfig.load(path)

    def test_reports_missing_required_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            path.write_text("GERRIT_URL=https://review.whamcloud.com\n", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(status.GerritConfigError, "GERRIT_USER"):
                status.GerritConfig.load(path)




class StatusTests(unittest.TestCase):
    def test_parses_supported_change_urls(self):
        self.assertEqual(status.parse_change_number(
            "https://review.whamcloud.com/c/fs/lustre-release/+/61965/3"
        ), 61965)
        self.assertEqual(status.parse_change_number(
            "https://review.whamcloud.com/c/61965"
        ), 61965)
        self.assertEqual(status.parse_change_number(
            "https://review.whamcloud.com/61965"
        ), 61965)

    def test_review_snapshot_is_revision_pinned_and_deterministic(self):
        revision = "d" * 40
        identity = {
            "change_number": 61965, "project": "fs/lustre-release",
            "branch": "master", "change_id": "I" + "a" * 40,
            "status": "NEW", "revision_sha": revision, "patchset": 4,
            "revision_numbers": {revision: 4}, "updated": "now",
            "unresolved_comment_count": 1,
        }
        comment = {
            "id": "abc", "patch_set": 4, "commit_id": revision,
            "author": {"_account_id": 7, "name": "Reviewer"},
            "message": "Please rename this", "updated": "2026-01-01",
            "unresolved": True, "line": 12,
            "range": {"start_line": 12, "start_character": 1,
                      "end_line": 12, "end_character": 8},
        }
        first = status.normalize_review_snapshot(
            identity, {"file.c": [comment]}, {}
        )
        second = status.normalize_review_snapshot(
            identity, {"file.c": [comment]}, {}
        )
        self.assertTrue(first["complete"])
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        normalized = first["threads"][0]["comments"][0]
        self.assertEqual(normalized["comment_id"], "abc")
        self.assertEqual(normalized["location"]["range"]["end_character"], 8)
        self.assertEqual(normalized["author_key"], "account:7")

    def test_review_snapshot_fails_closed_on_orphan_or_count_mismatch(self):
        revision = "d" * 40
        identity = {
            "change_number": 61965, "project": "fs/lustre-release",
            "branch": "master", "change_id": "I" + "a" * 40,
            "status": "NEW", "revision_sha": revision, "patchset": 4,
            "revision_numbers": {revision: 4}, "updated": "now",
            "unresolved_comment_count": 2,
        }
        result = status.normalize_review_snapshot(identity, {"file.c": [{
            "id": "reply", "in_reply_to": "missing", "patch_set": 4,
            "commit_id": revision, "message": "orphan", "updated": "now",
            "unresolved": True,
        }]}, {})
        self.assertFalse(result["complete"])
        self.assertTrue(any("missing parent" in item for item in result["incompleteness_reasons"]))
        self.assertTrue(any("does not match" in item for item in result["incompleteness_reasons"]))

    def test_a_comment_without_an_unresolved_flag_is_treated_as_resolved(self):
        """Gerrit omits `unresolved` on resolved comments, and that must stay resolved.

        Every snapshot test sent the flag explicitly, so the default for a
        payload that leaves it out was never exercised.  That default decides
        whether a comment opens an unresolved thread, and the snapshot then
        cross-checks its own thread count against Gerrit's -- so defaulting the
        wrong way both invents review blockers and makes the snapshot report
        itself incomplete, which is what gates whole-review work from starting.
        """
        revision = "d" * 40
        identity = {
            "change_number": 61965, "project": "fs/lustre-release",
            "branch": "master", "change_id": "I" + "a" * 40,
            "status": "NEW", "revision_sha": revision, "patchset": 4,
            "revision_numbers": {revision: 4}, "updated": "now",
            "unresolved_comment_count": 0,
        }
        comment = {
            "id": "abc", "patch_set": 4, "commit_id": revision,
            "author": {"_account_id": 7, "name": "Reviewer"},
            "message": "Looks good to me", "updated": "2026-01-01",
        }

        result = status.normalize_review_snapshot(
            identity, {"file.c": [comment]}, {}
        )

        self.assertEqual(result["threads"], [])
        self.assertEqual(result["reported_unresolved_count"], 0)
        self.assertEqual(result["incompleteness_reasons"], [])
        self.assertTrue(result["complete"])

    def test_a_change_without_an_unresolved_count_is_not_treated_as_blocked(self):
        """A ChangeInfo that omits the count must read as zero, not as blocked.

        Gerrit leaves `unresolved_comment_count` out when there is nothing
        unresolved, and every fixture in this suite supplied it, so the default
        was never exercised.  It feeds the watch classification directly: a
        non-zero default turns every such change into `needs-attention` and
        suppresses review runs that should start, while the count itself is
        what an operator reads to decide there is feedback to answer.
        """
        change = sample_change()
        del change["unresolved_comment_count"]

        result = status.summarize_change(change)

        self.assertEqual(result["unresolved"], 0)
        self.assertEqual(result["review"], "Ready")
        self.assertEqual(result["watch_state"], "ready")
        self.assertEqual(result["recommendation"], "Ready for maintainer action")

    def test_ready_requires_both_ci_and_two_non_owner_reviews(self):
        result = status.summarize_change(sample_change())
        self.assertEqual(result["review"], "Ready")
        self.assertEqual(result["lifecycle"], "Open")
        self.assertEqual(result["patchset"], 4)
        self.assertEqual(result["change_number"], 61965)
        self.assertEqual(result["project"], "fs/lustre-release")
        self.assertEqual(result["revision_sha"], "d" * 40)
        self.assertEqual(result["revision_ref"], "refs/changes/65/61965/4")
        self.assertEqual(result["unresolved"], 2)
        self.assertEqual(result["jenkins"], "PASS")
        self.assertEqual(result["maloo"], "PASS")
        self.assertEqual(result["jenkins_url"],
                         "https://build.whamcloud.com/job/lustre-reviews/42/")
        self.assertIn("builds=42", result["maloo_url"])
        self.assertIn("Maloo posted on patchset 4", result["change_summary"])

    def test_owner_vote_does_not_count_as_external_review(self):
        change = sample_change()
        change["labels"]["Code-Review"]["all"] = [
            {"name": "Owner", "value": 2},
            {"name": "Reviewer A", "value": 1},
        ]
        result = status.summarize_change(change)
        self.assertEqual(result["review"], "Pending")
        self.assertEqual(result["watch_state"], "needs-attention")

    def test_backport_only_needs_one_external_review(self):
        change = sample_change(backport=True)
        change["unresolved_comment_count"] = 0
        change["labels"]["Code-Review"]["all"] = [
            {"name": "Owner", "value": 2},
            {"name": "Reviewer A", "value": 1},
        ]
        result = status.summarize_change(change)
        self.assertTrue(result["is_backport"])
        self.assertEqual(result["review"], "Ready")
        self.assertEqual(result["watch_state"], "ready")

    def test_veto_takes_priority_over_ci_failure(self):
        change = sample_change()
        change["messages"].append({
            "_revision_number": 4,
            "date": "2026-08-29 12:31:00.000000000",
            "author": {"name": "Reviewer C"},
            "message": "Patch Set 4:\n\nCode-Review-1 This needs human attention",
        })
        change["labels"]["Code-Review"]["all"].append(
            {"name": "Reviewer C", "value": -1}
        )
        change["labels"]["Verified"]["all"][0]["value"] = -1
        result = status.summarize_change(change)
        self.assertEqual(result["review"], "Veto")
        self.assertTrue(result["test_flow_blocked"])
        self.assertEqual(result["review_blockers"][0]["name"], "Reviewer C")
        self.assertEqual(result["review_blockers"][0]["patchset"], 4)
        self.assertIn("Code-Review-1", result["review_blockers"][0]["message"])

    def test_maloo_minus_one_remains_a_ci_signal_not_review_gate(self):
        change = sample_change()
        change["labels"]["Verified"]["all"][1]["value"] = -1
        result = status.summarize_change(change)
        self.assertFalse(result["test_flow_blocked"])
        self.assertEqual(result["review"], "Maloo failed")
        self.assertEqual(result["watch_state"], "ci-failed")

    def test_ci_failure_is_attributed_by_voter(self):
        change = sample_change()
        change["labels"]["Verified"]["all"][1]["value"] = -1
        result = status.summarize_change(change)
        self.assertEqual(result["review"], "Maloo failed")
        self.assertEqual(result["maloo"], "FAIL")
        self.assertEqual(result["watch_state"], "ci-failed")

    def test_merged_change_folds_lifecycle_into_watch_state(self):
        result = status.summarize_change(sample_change(raw_status="MERGED"))
        self.assertEqual(result["lifecycle"], "Merged")
        self.assertEqual(result["review"], "—")
        self.assertEqual(result["watch_state"], "merged")

    def test_abandoned_change_folds_lifecycle_into_watch_state(self):
        result = status.summarize_change(sample_change(raw_status="ABANDONED"))
        self.assertEqual(result["lifecycle"], "Abandoned")
        self.assertEqual(result["review"], "—")
        self.assertEqual(result["watch_state"], "abandoned")

    def test_client_uses_basic_auth_and_strips_gerrit_xssi_prefix(self):
        change = sample_change()
        captured = {}

        def transport(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return (")]}'\n" + json.dumps(change)).encode("utf-8")

        config = status.GerritConfig(
            "https://review.whamcloud.com", "reader", "private-password"
        )
        client = status.GerritStatusClient(config, transport=transport, timeout=3)
        result = client.fetch("https://review.whamcloud.com/c/61965")
        expected = base64.b64encode(b"reader:private-password").decode("ascii")
        self.assertEqual(captured["request"].get_header("Authorization"),
                         f"Basic {expected}")
        self.assertEqual(captured["request"].method, "GET")
        self.assertEqual(captured["timeout"], 3)
        self.assertEqual(result["title"], change["subject"])

    def test_identity_fetch_includes_all_revision_numbers_for_reconciliation(self):
        change = sample_change()
        change["status"] = "NEW"
        change["branch"] = "master"
        change["change_id"] = "I" + "1" * 40
        change["revisions"]["older"] = {"_number": 3}

        def transport(_request, _timeout):
            return (")]}'\n" + json.dumps(change)).encode("utf-8")

        client = status.GerritStatusClient(
            status.GerritConfig(
                "https://review.whamcloud.com", "reader", "private-password"
            ),
            transport=transport,
        )
        identity = client.fetch_identity("https://review.whamcloud.com/c/61965")
        self.assertEqual(identity["patchset"], 4)
        self.assertEqual(identity["revision_sha"], "d" * 40)
        self.assertEqual(identity["revision_numbers"]["older"], 3)
        self.assertIn("d" * 40, identity["revision_shas"])

    def test_review_capture_brackets_exact_sha_endpoints_with_identity(self):
        change = sample_change()
        change.update(
            status="NEW", branch="master", change_id="I" + "1" * 40,
            unresolved_comment_count=1,
        )
        revision = change["current_revision"]
        calls = []
        comment = {
            "id": "c1", "patch_set": 4, "commit_id": revision,
            "message": "Rename it", "updated": "now", "unresolved": True,
        }

        def transport(request, _timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/comments"):
                value = {"file.c": [comment]}
            elif request.full_url.endswith("/ported_comments"):
                value = {}
            else:
                value = change
            return (")]}'\n" + json.dumps(value)).encode("utf-8")

        client = status.GerritStatusClient(
            status.GerritConfig(
                "https://review.whamcloud.com", "reader", "private-password"
            ), transport=transport,
        )
        snapshot = client.fetch_review_snapshot(
            "https://review.whamcloud.com/c/61965", expected_revision=revision
        )
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(calls), 4)
        self.assertIn(f"/revisions/{revision}/comments", calls[1])
        self.assertIn(f"/revisions/{revision}/ported_comments", calls[2])
        self.assertIn("ALL_REVISIONS", calls[0])
        self.assertIn("ALL_REVISIONS", calls[3])

    def test_review_capture_can_target_a_historical_revision_explicitly(self):
        change = sample_change()
        current = "d" * 40
        historical = "a" * 40
        change.update(
            status="NEW", branch="master", change_id="I" + "1" * 40,
            current_revision=current, unresolved_comment_count=1,
        )
        change["revisions"] = {
            historical: {"_number": 3}, current: {"_number": 4},
        }
        comment = {
            "id": "c1", "patch_set": 3, "commit_id": historical,
            "message": "Rename it", "updated": "now", "unresolved": True,
        }
        calls = []

        def transport(request, _timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/comments"):
                value = {"file.c": [comment]}
            elif request.full_url.endswith("/ported_comments"):
                value = {}
            else:
                value = change
            return (")]}'\n" + json.dumps(value)).encode("utf-8")

        client = status.GerritStatusClient(
            status.GerritConfig(
                "https://review.whamcloud.com", "reader", "private-password"
            ), transport=transport,
        )
        snapshot = client.fetch_review_snapshot(
            "https://review.whamcloud.com/c/61965",
            expected_revision=historical, require_current=False,
        )
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["change"]["revision_sha"], historical)
        self.assertEqual(snapshot["change"]["patchset"], 3)
        self.assertIn(f"/revisions/{historical}/comments", calls[1])

    def test_refresh_preserves_last_known_status_on_error(self):
        patch_record = {
            "url": "https://review.whamcloud.com/c/61965",
            "review": "Ready",
        }

        class FailingClient:
            def fetch(self, _url):
                raise status.GerritRequestError("temporary failure")

        with patch("patch_watcher.reporting.log_structured_error"):
            error = status.refresh_patch(patch_record, FailingClient())
        self.assertEqual(error, "temporary failure")
        self.assertEqual(patch_record["review"], "Ready")
        self.assertEqual(patch_record["status_error"], "temporary failure")
        self.assertNotEqual(patch_record["last_checked"], "—")

    def test_refresh_records_bounded_history_and_state_transition(self):
        patch_record = {
            "url": "https://review.whamcloud.com/c/61965",
            "watch_state": "awaiting-ci",
            "history": [],
        }

        class ReadyClient:
            def fetch(self, _url):
                result = status.summarize_change(sample_change(backport=True))
                result["unresolved"] = 0
                result["watch_state"] = "ready"
                return result

        self.assertIsNone(status.refresh_patch(patch_record, ReadyClient()))
        self.assertEqual(patch_record["check_count"], 1)
        self.assertEqual(patch_record["state_transition"], "awaiting-ci → ready")
        self.assertEqual(len(patch_record["history"]), 1)


if __name__ == "__main__":
    unittest.main()


CHANGE_URL = "https://review.whamcloud.com/c/fs/lustre-release/+/61965"


def _client(change, *, transport=None):
    config = status.GerritConfig("https://review.whamcloud.com", "user", "pass")
    if transport is None:
        body = b")]}'\n" + json.dumps(change).encode()

        def transport(_request, _timeout):
            return body

    return status.GerritStatusClient(config, transport=transport)


class ExternalDataTests(unittest.TestCase):
    """Gerrit answers are untrusted data, not a description of the request."""

    def test_fetch_rejects_a_body_about_a_different_change(self):
        other = sample_change()
        other["_number"] = 101
        other["project"] = "other/project"
        other["subject"] = "someone else's patch"
        with self.assertRaises(status.GerritRequestError) as caught:
            _client(other).fetch(CHANGE_URL)
        self.assertIn("different change", str(caught.exception))

    def test_fetch_identity_rejects_a_body_about_a_different_change(self):
        other = sample_change()
        other["_number"] = 101
        with self.assertRaises(status.GerritRequestError) as caught:
            _client(other).fetch_identity(CHANGE_URL)
        self.assertIn("different change", str(caught.exception))

    def test_fetch_identity_is_pinned_to_the_requested_change(self):
        identity = _client(sample_change()).fetch_identity(CHANGE_URL)
        self.assertEqual(identity["change_number"], 61965)

    def test_summarize_change_checks_the_change_it_was_asked_about(self):
        change = sample_change()
        self.assertEqual(
            status.summarize_change(change, expected_change_number=61965)["change_number"],
            61965,
        )
        with self.assertRaises(status.GerritRequestError):
            status.summarize_change(change, expected_change_number=68160)

    def test_missing_change_number_is_a_typed_failure(self):
        change = sample_change()
        change.pop("_number")
        with self.assertRaises(status.GerritRequestError):
            _client(change).fetch(CHANGE_URL)

    def wrong_typed_changes(self):
        revision = "d" * 40
        shapes = {
            "revisions_list": {"revisions": [{"_number": 4}]},
            "labels_list": {"labels": [{"Verified": 1}]},
            "labels_verified_str": {"labels": {"Verified": "nope"}},
            "labels_all_str": {"labels": {"Verified": {"all": "nope"}}},
            "labels_vote_str": {"labels": {"Verified": {"all": ["jenkins"]}}},
            "messages_dict": {"messages": {"a": 1}},
            "message_str": {"messages": ["a message"]},
            "message_author_str": {"messages": [
                {"_revision_number": 4, "date": "2026-08-29", "author": "bob"}
            ]},
            "owner_list": {"owner": [{"name": "x"}]},
            "commit_object_list": None,
            "commit_message_int": None,
            "uploader_str": None,
            "current_revision_object": {"current_revision": {"sha": revision}},
            "status_int": {"status": 7},
            "unresolved_list": {"unresolved_comment_count": [1, 2]},
        }
        for name, overlay in shapes.items():
            change = sample_change()
            if name == "commit_object_list":
                change["revisions"][revision]["commit"] = [{"message": "x"}]
            elif name == "commit_message_int":
                change["revisions"][revision]["commit"]["message"] = 5
            elif name == "uploader_str":
                change["revisions"][revision]["uploader"] = "someone"
            else:
                change.update(overlay)
            yield name, change

    def test_wrong_typed_fields_raise_the_error_callers_already_handle(self):
        for name, change in self.wrong_typed_changes():
            with self.subTest(shape=name):
                with self.assertRaises(status.GerritRequestError):
                    _client(change).fetch(CHANGE_URL)

    def test_wrong_typed_fields_reach_refresh_patch_bookkeeping(self):
        # A malformed shape used to escape as AttributeError/TypeError past
        # refresh_patch's handler: no status_error, no last_checked stamp, a
        # frozen row, and a health banner claiming success.
        for name, change in self.wrong_typed_changes():
            with self.subTest(shape=name):
                patch = {
                    "url": CHANGE_URL,
                    "title": "previous title",
                    "last_checked": "stale",
                    "status_error": "",
                }
                message = status.refresh_patch(patch, _client(change))
                self.assertTrue(message)
                self.assertTrue(patch["status_error"])
                self.assertNotEqual(patch["last_checked"], "stale")
                self.assertEqual(patch["title"], "previous title")

    def test_mixed_type_message_dates_do_not_break_the_comparison(self):
        # ``max`` over mixed str/int dates used to raise TypeError.
        change = sample_change()
        change["messages"] = [
            {"_revision_number": 4, "date": 5, "author": {"name": "a"}, "message": "x"},
            {"_revision_number": "4", "date": "2026-08-29 13:00:00.000000000",
             "author": {"name": "b"}, "message": "y"},
        ]
        result = _client(change).fetch(CHANGE_URL)
        # Both dates are normalized to text, so the newest is chosen by a
        # string comparison that cannot raise, and the string-typed
        # ``_revision_number`` still resolves to patchset 4.
        self.assertIn("posted on patchset 4", result["change_summary"])

    def test_lone_surrogate_text_can_always_be_encoded(self):
        change = sample_change()
        change["subject"] = "LU-1 \ud800 broken subject"
        change["project"] = "fs/\ud800"
        result = _client(change).fetch(CHANGE_URL)
        for value in (result["title"], result["project"], result["change_summary"]):
            value.encode("utf-8")
        self.assertNotIn("\ud800", result["title"])

    def test_multi_line_subject_is_flattened_and_bounded(self):
        change = sample_change()
        change["subject"] = "first line\nsecond line" + "z" * 900
        result = _client(change).fetch(CHANGE_URL)
        self.assertNotIn("\n", result["title"])
        self.assertLessEqual(len(result["title"]), 500)

    def test_oversized_gerrit_response_is_rejected(self):
        oversized = b"x" * (status.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(status.GerritRequestError) as caught:
            _client(None, transport=lambda _r, _t: oversized).fetch(CHANGE_URL)
        self.assertIn("more than", str(caught.exception))

    def test_default_transport_reads_with_a_bound(self):
        reads = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=None):
                reads.append(size)
                return b"{}"

        with patch("patch_watcher.gerrit_status.urlopen", lambda *a, **k: FakeResponse()):
            status._default_transport(object(), 1.0)
        self.assertEqual(reads, [status.MAX_RESPONSE_BYTES + 1])

    def test_non_bytes_transport_result_is_a_typed_failure(self):
        with self.assertRaises(status.GerritRequestError):
            _client(None, transport=lambda _r, _t: "not bytes").fetch(CHANGE_URL)

    def test_a_surrogate_in_a_review_comment_does_not_break_the_snapshot(self):
        # The byte-length measurement inside _bounded_comment_text encodes,
        # so an unsanitised surrogate raised UnicodeEncodeError there too.
        text = status._bounded_comment_text("hi \ud800 there", limit=100)
        text.encode("utf-8")
        self.assertNotIn("\ud800", text)

    def test_an_unforeseen_shape_still_becomes_a_typed_failure(self):
        # Defence in depth for a thirteenth malformed shape: whatever raises,
        # the caller sees the error its handler already understands.
        def explode(_labels):
            raise TypeError("unforeseen Gerrit shape")

        with patch("patch_watcher.gerrit_status._parse_labels", explode):
            with self.assertRaises(status.GerritRequestError):
                status.summarize_change(sample_change())
