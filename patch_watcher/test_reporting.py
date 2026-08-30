import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import reporting


def watched_patch():
    return {
        "url": "https://review.whamcloud.com/c/1",
        "title": "LU-1 fix pages",
        "lifecycle": "Open",
        "watch_state": "needs-review",
        "review": "Pending",
        "jenkins": "PASS",
        "maloo": "PASS",
        "change_summary": "Alice uploaded patchset 2",
        "recommendation": "Request the missing Code-Review votes",
        "check_count": 4,
        "history": [{
            "checked_at": "2026-08-29T12:00:00+00:00",
            "changed_at": "2026-08-29 11:00:00",
            "summary": "Alice uploaded patchset 2",
            "watch_state": "needs-review",
        }],
    }


class ReportingTests(unittest.TestCase):
    def test_session_alert_is_bounded_and_kill_link_is_confirmation_only(self):
        body = reporting.compose_session_alert(
            session_id="session-1",
            patch_id="68160",
            state="failed",
            reason="agent inactivity timeout",
            messages=[
                {"author": "agent", "body": f"message {index}"}
                for index in range(12)
            ],
            confirmation_url="http://127.0.0.1:8080/runs/session-1/kill?token=opaque",
        )
        self.assertNotIn("message 0", body)
        self.assertIn("message 11", body)
        self.assertIn("opens a confirmation page", body)
        self.assertIn("agent inactivity timeout", body)

    def test_disabled_session_alert_sends_nothing(self):
        calls = []
        result = reporting.send_session_alert(
            SimpleNamespace(
                email_enabled=False,
                email_to="paf@mulberrytree.us",
                sendmail_path="/usr/sbin/sendmail",
            ),
            session_id="session-1",
            patch_id="68160",
            state="failed",
            reason="timeout",
            messages=[],
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        self.assertFalse(result.sent)
        self.assertEqual(calls, [])

    def test_private_structured_log_and_bounded_reader(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state" / "errors.jsonl"
            reporting.log_structured_error(
                "gerrit_refresh", "timeout", "https://review.whamcloud.com/c/1",
                path=path,
            )
            reporting.log_structured_error("email", "sendmail failed", path=path)
            events = reporting.recent_error_events(path=path, limit=1)
            mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "email")

    def test_summary_covers_checks_changes_and_errors(self):
        body = reporting.compose_daily_summary(
            [watched_patch()],
            day=date(2026, 8, 29),
            errors=[{"timestamp": "now", "kind": "fetch", "message": "bad"}],
        )
        self.assertIn("Checks performed in this process: 4", body)
        self.assertIn("Changes noticed today: 1", body)
        self.assertIn("Alice uploaded patchset 2", body)
        self.assertIn("fetch: bad", body)

    def test_disabled_email_is_a_dry_run(self):
        calls = []
        config = SimpleNamespace(
            email_enabled=False,
            email_to="paf@mulberrytree.us",
            sendmail_path="/usr/sbin/sendmail",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = reporting.send_daily_summary(
                [watched_patch()], config,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                error_log=Path(temp_dir) / "none",
            )
        self.assertFalse(result.sent)
        self.assertIn("disabled", result.message)
        self.assertEqual(calls, [])

    def test_enabled_email_uses_sendmail_without_shell(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, b"", b"")

        config = SimpleNamespace(
            email_enabled=True,
            email_to="paf@mulberrytree.us",
            sendmail_path="/custom/sendmail",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = reporting.send_daily_summary(
                [watched_patch()], config, runner=runner,
                error_log=Path(temp_dir) / "none",
            )
        self.assertTrue(result.sent)
        self.assertEqual(captured["command"], ["/custom/sendmail", "-t", "-oi"])
        self.assertNotIn("shell", captured["kwargs"])
        message = captured["kwargs"]["input"].decode("utf-8")
        self.assertIn("To: paf@mulberrytree.us", message)
        self.assertIn("Patch Watcher daily status", message)

    def test_sendmail_failure_is_logged_and_returned(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 75, b"", b"queue unavailable")

        with patch("reporting.log_structured_error") as logger:
            result = reporting.SendmailMailer("/usr/sbin/sendmail", runner=runner).send(
                "paf@mulberrytree.us", "subject", "body"
            )
        self.assertFalse(result.sent)
        self.assertIn("status 75", result.message)
        logger.assert_called_once()

    def test_app_email_helper_uses_current_patch_results(self):
        app.PATCHES[:] = [watched_patch()]
        captured = {}

        def runner(command, **kwargs):
            captured["message"] = kwargs["input"].decode("utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        config = SimpleNamespace(
            email_enabled=True,
            email_to="paf@mulberrytree.us",
            sendmail_path="/usr/sbin/sendmail",
        )
        result = app.send_status_email(config, runner=runner)
        self.assertTrue(result.sent)
        self.assertIn("LU-1 fix pages", captured["message"])


if __name__ == "__main__":
    unittest.main()
