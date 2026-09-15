import subprocess
import tempfile
import unittest
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path

from patch_watcher import bot_mail


class Recorder:
    """Stands in for sendmail and keeps what it was handed."""

    def __init__(self, returncode=0, stderr=b""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "input": kwargs.get("input", b"")})
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr)

    @property
    def message(self):
        return message_from_bytes(self.calls[-1]["input"], policy=default_policy)


class BotMailTests(unittest.TestCase):
    def limit(self, *, start=1_000_000.0):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [start]
        return bot_mail.RateLimit(
            Path(self.temp.name) / "bot-mail.json", clock=lambda: self.now[0]
        )

    def send(self, subject, body="body", runner=None, limit=None):
        runner = runner or Recorder()
        outcome = bot_mail.send(
            subject, body, runner=runner, limit=limit or self.limit()
        )
        return outcome, runner

    def test_it_goes_to_the_one_destination_named_on_the_command_line(self):
        """The recipient is a constant, and it is passed as an argument rather
        than left to `-t` to read out of the headers, so even a malformed
        message cannot redirect delivery."""

        outcome, runner = self.send("a ticket arrived")
        self.assertTrue(outcome.sent)
        self.assertEqual(runner.calls[0]["argv"][-1], bot_mail.RECIPIENT)
        self.assertNotIn("-t", runner.calls[0]["argv"])
        self.assertEqual(runner.message["To"], bot_mail.RECIPIENT)

    def test_a_caller_cannot_add_a_header(self):
        """The whole point of a tool for untrusted callers.  A newline in the
        subject is the classic way to append Bcc, and the body is set as
        content rather than assembled into the message text."""

        outcome, runner = self.send(
            "hello\nBcc: attacker@example.com\nX-Evil: yes",
            "line one\nBcc: also-attacker@example.com",
        )
        self.assertTrue(outcome.sent)
        message = runner.message
        self.assertIsNone(message["Bcc"])
        self.assertIsNone(message["X-Evil"])
        self.assertEqual(message.get_all("To"), [bot_mail.RECIPIENT])
        # The text survives, flattened, where a human can read it.
        self.assertIn("Bcc: attacker@example.com", message["Subject"])
        self.assertIn("\n", message.get_content())

    def test_control_characters_never_reach_a_header(self):
        _, runner = self.send("subject\r\vwith\x00controls\x7f")
        self.assertNotIn("\r", runner.message["Subject"])
        self.assertNotIn("\v", runner.message["Subject"])
        self.assertNotIn("\x00", runner.message["Subject"])

    def test_subject_and_body_are_bounded(self):
        _, runner = self.send("s" * 5000, "b" * 50_000)
        # Read the decoded header: a long subject is transfer-encoded and
        # folded on the wire, so the raw line is longer than the text in it.
        self.assertEqual(runner.message["Subject"].count("s"), bot_mail.MAX_SUBJECT_CHARS)
        self.assertLessEqual(
            runner.message.get_content().count("b"), bot_mail.MAX_BODY_CHARS
        )

    def test_a_mailbomb_is_refused(self):
        """A compromised caller's most obvious move."""

        limit = self.limit()
        runner = Recorder()
        first = bot_mail.send("one", "b", runner=runner, limit=limit)
        self.assertTrue(first.sent)
        second = bot_mail.send("two", "b", runner=runner, limit=limit)
        self.assertFalse(second.sent)
        self.assertIn("minimum gap", second.detail)
        self.assertEqual(len(runner.calls), 1, "the refused message was not sent")

        # Waiting is all it takes; the limit is a pace, not a lockout.
        self.now[0] += bot_mail.MIN_SECONDS_BETWEEN
        self.assertTrue(bot_mail.send("three", "b", runner=runner, limit=limit).sent)

    def test_the_daily_ceiling_bounds_slow_exfiltration(self):
        """A message a minute, all day, is the other shape of the same abuse."""

        limit = self.limit()
        runner = Recorder()
        for _ in range(bot_mail.MAX_PER_DAY):
            self.assertTrue(bot_mail.send("x", "y", runner=runner, limit=limit).sent)
            self.now[0] += bot_mail.MIN_SECONDS_BETWEEN
        refused = bot_mail.send("one too many", "y", runner=runner, limit=limit)
        self.assertFalse(refused.sent)
        self.assertIn("daily limit", refused.detail)
        self.assertEqual(len(runner.calls), bot_mail.MAX_PER_DAY)

    def test_an_unenforceable_budget_refuses_rather_than_sends(self):
        """An advisory limit against an untrusted caller is no limit."""

        unwritable = bot_mail.RateLimit(Path("/proc/version/nope/bot-mail.json"))
        runner = Recorder()
        outcome = bot_mail.send("hello", "body", runner=runner, limit=unwritable)
        self.assertFalse(outcome.sent)
        self.assertIn("budget", outcome.detail)
        self.assertEqual(runner.calls, [])

    def test_the_budget_is_spent_before_the_message_is_built(self):
        """Otherwise a caller that crashes the builder sends uncounted."""

        limit = self.limit()
        runner = Recorder()
        bot_mail.send("one", "b", runner=runner, limit=limit)
        self.assertTrue(limit.path.is_file())
        self.assertEqual(limit.path.stat().st_mode & 0o777, 0o600)

    def test_nothing_to_say_is_not_sent(self):
        outcome, runner = self.send("   ", "")
        self.assertFalse(outcome.sent)
        self.assertEqual(runner.calls, [])

    def test_a_sendmail_failure_is_reported_not_raised(self):
        runner = Recorder(returncode=75, stderr=b"deferred")
        outcome, _ = self.send("hello", "body", runner=runner)
        self.assertFalse(outcome.sent)
        self.assertIn("75", outcome.detail)
        self.assertIn("deferred", outcome.detail)

    def test_the_message_says_it_is_automatic(self):
        """So a human's filters and a mailing list both know what it is."""

        _, runner = self.send("hello")
        self.assertEqual(runner.message["Auto-Submitted"], "auto-generated")
        self.assertTrue(runner.message["Subject"].startswith(bot_mail.SUBJECT_PREFIX))


if __name__ == "__main__":
    unittest.main()
