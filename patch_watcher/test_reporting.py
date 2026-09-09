import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from patch_watcher import app, reporting


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

    def test_error_log_reader_only_touches_the_tail(self):
        """The dashboard reads ten lines per render from an unbounded log.

        Slicing the tail off `read_text()` costs the whole file every time:
        on a 138 MB log that measured 259 ms and a 278 MB transient
        allocation, against 0.2 ms and 0.2 MB for a tail read.
        """

        class CountingReader:
            def __init__(self, stream):
                self.stream = stream
                self.bytes_read = 0

            def read(self, *args):
                data = self.stream.read(*args)
                self.bytes_read += len(data)
                return data

            def seek(self, *args):
                return self.stream.seek(*args)

            def tell(self):
                return self.stream.tell()

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

        readers = []
        real_open = Path.open

        def counting_open(self, *args, **kwargs):
            stream = real_open(self, *args, **kwargs)
            if "b" in (args[0] if args else kwargs.get("mode", "r")):
                reader = CountingReader(stream)
                readers.append(reader)
                return reader
            return stream

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "errors.jsonl"
            for index in range(2000):
                reporting.log_structured_error(
                    "refresh", f"message-{index}", path=path
                )
            size = path.stat().st_size
            with patch.object(Path, "open", counting_open):
                events = reporting.recent_error_events(path=path, limit=10)

        self.assertGreater(size, 1 << 16)
        self.assertEqual(len(events), 10)
        self.assertEqual(events[0]["message"], "message-1990")
        self.assertEqual(events[-1]["message"], "message-1999")
        self.assertEqual(len(readers), 1)
        self.assertLessEqual(readers[0].bytes_read, 1 << 16)
        self.assertLess(readers[0].bytes_read, size)

    def test_error_log_reader_handles_partial_and_undecodable_tails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "errors.jsonl"
            # No trailing newline, and a leading line that is not valid UTF-8.
            path.write_bytes(
                b"\xff\xfe not utf-8\n"
                b'{"kind": "first", "message": "a"}\n'
                b'{"kind": "second", "message": "b"}'
            )
            events = reporting.recent_error_events(path=path, limit=10)
        self.assertEqual([event["kind"] for event in events], ["first", "second"])

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

    def test_summary_includes_bounded_retest_automation_events(self):
        events = [
            {
                "created_at": f"2026-08-29T12:{index:02d}:00Z",
                "patch_id": "68160",
                "event_type": "decision_recorded",
                "summary": f"decision {index}",
            }
            for index in range(30)
        ]
        body = reporting.compose_daily_summary(
            [watched_patch()],
            day=date(2026, 8, 29),
            automation_events=events,
        )
        self.assertIn("Retest automation events included: 25", body)
        self.assertNotIn("decision 0\n", body)
        self.assertIn("decision 29", body)

    def test_automation_alert_is_bounded(self):
        body = reporting.compose_automation_alert(
            patch_id="68160",
            revision="a" * 40,
            state="ambiguous",
            summary="Remote outcome must be reconciled",
            timeline=[
                {"event_type": "event", "summary": f"item {index}"}
                for index in range(12)
            ],
        )
        self.assertNotIn("item 0", body)
        self.assertIn("item 11", body)
        self.assertIn("ambiguous", body)

    def test_disabled_automation_alert_sends_nothing(self):
        calls = []
        result = reporting.send_automation_alert(
            SimpleNamespace(
                email_enabled=False,
                email_to="paf@mulberrytree.us",
                sendmail_path="/usr/sbin/sendmail",
            ),
            patch_id="68160",
            revision="a" * 40,
            state="failed",
            summary="read failed",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        self.assertFalse(result.sent)
        self.assertEqual(calls, [])

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

        with patch("patch_watcher.reporting.log_structured_error") as logger:
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


class ExternalTextTests(unittest.TestCase):
    """Gerrit subjects are free text and reach the operator's mailbox."""

    def test_a_multi_line_title_cannot_forge_report_sections(self):
        hostile = (
            "innocent subject\n\nRetest automation\n-----------------\n"
            "- 2026-01-01 change 68160: retest_submitted - fabricated"
        )
        body = reporting.compose_daily_summary([{"url": "u", "title": hostile}])
        title_lines = [line for line in body.splitlines() if "innocent subject" in line]
        self.assertEqual(len(title_lines), 1)
        self.assertIn("fabricated", title_lines[0])
        # Exactly one real section heading, and the forged one is not it.
        headings = [line for line in body.splitlines() if line == "-----------------"]
        self.assertEqual(len(headings), 1)

    def test_a_title_is_bounded(self):
        body = reporting.compose_daily_summary([{"url": "u", "title": "z" * 5000}])
        self.assertLess(len(body), 2000)

    def test_a_lone_surrogate_never_breaks_the_summary_or_the_mail(self):
        # A surrogate is legal in Gerrit JSON, is persisted in the watch list,
        # and made every later run of the nightly summary exit non-zero.
        patch = {"url": "u", "title": "LU-1 \ud800 subject",
                 "change_summary": "\ud800", "recommendation": "\ud800"}
        body = reporting.compose_daily_summary([patch])
        body.encode("utf-8")
        self.assertNotIn("\ud800", body)

    def test_sendmail_encodes_a_surrogate_body_instead_of_raising(self):
        sent = []

        def runner(argv, **kwargs):
            sent.append(kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        outcome = reporting.SendmailMailer("/bin/true", runner=runner).send(
            "paf@mulberrytree.us", "subject \ud800", "body \ud800 text"
        )
        self.assertTrue(outcome.sent)
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(sent[0], bytes)

    def test_error_log_lines_are_bounded_in_the_summary(self):
        body = reporting.compose_daily_summary(
            [],
            errors=[{"timestamp": "t", "kind": "k", "message": "m" * 4000,
                     "patch_url": "u"}],
        )
        self.assertLess(len(body), 1500)
