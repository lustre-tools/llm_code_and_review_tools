import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gerrit_status as status


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

    def test_upload_kill_switch_defaults_off_and_requires_git_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            base = (
                "GERRIT_URL=https://review.whamcloud.com\n"
                "GERRIT_USER=test-user\nGERRIT_PASS=secret\n"
            )
            path.write_text(base, encoding="utf-8")
            path.chmod(0o600)
            self.assertFalse(status.GerritConfig.load(path).upload_enabled)
            path.write_text(base + "GERRIT_UPLOAD_ENABLED=true\n", encoding="utf-8")
            with self.assertRaisesRegex(status.GerritConfigError, "GERRIT_GIT_NAME"):
                status.GerritConfig.load(path)
            path.write_text(
                base + "GERRIT_UPLOAD_ENABLED=true\n"
                "GERRIT_GIT_NAME=Patch Watcher\n"
                "GERRIT_GIT_EMAIL=patch-watcher@example.test\n",
                encoding="utf-8",
            )
            config = status.GerritConfig.load(path)
            self.assertTrue(config.upload_enabled)
            self.assertEqual(config.git_name, "Patch Watcher")

    def test_external_write_kill_switches_are_independent_and_default_off(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            base = (
                "GERRIT_URL=https://review.whamcloud.com\n"
                "GERRIT_USER=test-user\nGERRIT_PASS=secret\n"
            )
            path.write_text(base, encoding="utf-8")
            path.chmod(0o600)
            config = status.GerritConfig.load(path)
            self.assertFalse(config.reply_enabled)
            self.assertFalse(config.jenkins_retrigger_enabled)
            path.write_text(
                base + "GERRIT_REPLY_ENABLED=true\n"
                "JENKINS_RETRIGGER_ENABLED=yes\n",
                encoding="utf-8",
            )
            config = status.GerritConfig.load(path)
            self.assertTrue(config.reply_enabled)
            self.assertTrue(config.jenkins_retrigger_enabled)


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

        with patch("reporting.log_structured_error"):
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
