import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from patch_watcher import bot_inbox


def issue(key, summary="a ticket", status="Open"):
    return {"key": key, "summary": summary, "status": status, "updated": "now"}


class FakeJira:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def assigned_issues(self, **kwargs):
        self.calls += 1
        if not self.responses:
            return []
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Sender:
    def __init__(self, sent=True, detail="sent", raises=None):
        self.calls = []
        self.sent = sent
        self.detail = detail
        self.raises = raises

    def __call__(self, subject, body):
        self.calls.append({"subject": subject, "body": body})
        if self.raises:
            raise self.raises
        return SimpleNamespace(sent=self.sent, detail=self.detail)


class BotInboxTests(unittest.TestCase):
    def inbox(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1_000_000.0]
        return bot_inbox.BotInbox(
            Path(self.temp.name) / "bot-inbox.json", clock=lambda: self.now[0]
        )

    def test_a_new_assignment_is_announced_once(self):
        inbox, sender = self.inbox(), Sender()
        client = FakeJira([issue("LU-20724", "fsx burst mode")], [issue("LU-20724")])

        first = bot_inbox.poll(client, inbox=inbox, send=sender)
        self.assertEqual([change.key for change in first], ["LU-20724"])
        self.assertTrue(first[0].notified)
        self.assertIn("LU-20724", sender.calls[0]["subject"])
        self.assertIn("fsx burst mode", sender.calls[0]["body"])

        # Still assigned on the next poll; still only one announcement.
        self.assertEqual(bot_inbox.poll(client, inbox=inbox, send=sender), [])
        self.assertEqual(len(sender.calls), 1)

    def test_unassigning_and_reassigning_announces_again(self):
        """"Look at this again" is exactly what a reassignment means, and
        remembering the first one forever would silently deny it."""

        inbox, sender = self.inbox(), Sender()
        client = FakeJira([issue("LU-1")], [], [issue("LU-1")])
        bot_inbox.poll(client, inbox=inbox, send=sender)
        bot_inbox.poll(client, inbox=inbox, send=sender)   # gone
        again = bot_inbox.poll(client, inbox=inbox, send=sender)
        self.assertEqual([change.key for change in again], ["LU-1"])
        self.assertEqual(len(sender.calls), 2)

    def test_a_failing_notifier_does_not_stop_the_watching(self):
        """Mail is expected to fail outright until the bot has its own Unix
        account.  Trading the whole job for the announcement of it would be
        the wrong way round."""

        inbox = self.inbox()
        sender = Sender(sent=False, detail="sendmail exited 127")
        client = FakeJira(*([[issue("LU-1")]] * (bot_inbox.MAX_NOTIFY_ATTEMPTS + 2)))

        for _ in range(bot_inbox.MAX_NOTIFY_ATTEMPTS):
            announced = bot_inbox.poll(client, inbox=inbox, send=sender)
            self.assertEqual(len(announced), 1)
            self.assertFalse(announced[0].notified)
            self.assertIn("127", announced[0].detail)

        # Bounded: a notice nobody can deliver is not retried forever.
        self.assertEqual(bot_inbox.poll(client, inbox=inbox, send=sender), [])
        self.assertEqual(len(sender.calls), bot_inbox.MAX_NOTIFY_ATTEMPTS)

    def test_a_notifier_that_raises_is_caught(self):
        inbox = self.inbox()
        sender = Sender(raises=RuntimeError("no sendmail on this host"))
        announced = bot_inbox.poll(FakeJira([issue("LU-1")]), inbox=inbox, send=sender)
        self.assertEqual(len(announced), 1)
        self.assertFalse(announced[0].notified)
        self.assertIn("no sendmail", announced[0].detail)

    def test_a_tracker_that_is_down_is_not_an_event(self):
        inbox, sender = self.inbox(), Sender()
        client = FakeJira(RuntimeError("JIRA returned HTTP 503"), [issue("LU-1")])
        self.assertEqual(bot_inbox.poll(client, inbox=inbox, send=sender), [])
        self.assertEqual(sender.calls, [])
        # And the next poll still finds it.
        self.assertEqual(
            [change.key for change in bot_inbox.poll(client, inbox=inbox, send=sender)],
            ["LU-1"],
        )

    def test_unreadable_state_re_announces_rather_than_forgetting(self):
        """The noisy failure is the tolerable one; losing the ticket is not."""

        inbox, sender = self.inbox(), Sender()
        client = FakeJira([issue("LU-1")], [issue("LU-1")])
        bot_inbox.poll(client, inbox=inbox, send=sender)
        inbox.path.write_text("{ not json", encoding="utf-8")
        bot_inbox.poll(client, inbox=inbox, send=sender)
        self.assertEqual(len(sender.calls), 2)

    def test_the_state_file_is_private(self):
        inbox, sender = self.inbox(), Sender()
        bot_inbox.poll(FakeJira([issue("LU-1")]), inbox=inbox, send=sender)
        self.assertEqual(inbox.path.stat().st_mode & 0o777, 0o600)

    def test_several_assignments_are_each_announced(self):
        inbox, sender = self.inbox(), Sender()
        client = FakeJira([issue("LU-1"), issue("LU-2"), {"key": ""}])
        announced = bot_inbox.poll(client, inbox=inbox, send=sender)
        self.assertEqual([change.key for change in announced], ["LU-1", "LU-2"])
        self.assertEqual(len(sender.calls), 2)


if __name__ == "__main__":
    unittest.main()
