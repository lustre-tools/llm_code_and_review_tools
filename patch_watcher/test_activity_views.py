import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from patch_watcher import activity_views


def usage(when, cost=1.0, tokens=1000, turns=3):
    return SimpleNamespace(
        recorded_at=when, cost_usd=cost, total_tokens=tokens, turns=turns
    )


def session(when, state):
    return SimpleNamespace(state=state, state_changed_at=when)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).astimezone()

    def buckets(self, usage_rows=(), sessions=(), days=5):
        return activity_views.bucket_by_day(
            usage_rows, sessions, days=days, now=self.now
        )

    def test_quiet_days_are_kept(self):
        """A chart that drops them draws a busy fortnight and a quiet one the
        same way, which is the one comparison it exists to make."""

        buckets = self.buckets([usage(self.now)], days=5)
        self.assertEqual(len(buckets), 5)
        self.assertEqual([b.runs for b in buckets], [0, 0, 0, 0, 1])
        self.assertEqual(buckets[-1].day, self.now.strftime("%Y-%m-%d"))

    def test_runs_and_cost_land_on_their_own_day(self):
        yesterday = self.now - timedelta(days=1)
        buckets = self.buckets(
            [usage(self.now, cost=2.0), usage(yesterday, cost=5.0),
             usage(yesterday, cost=1.0)],
            days=3,
        )
        self.assertEqual([b.runs for b in buckets], [0, 2, 1])
        self.assertAlmostEqual(buckets[1].cost_usd, 6.0)
        self.assertAlmostEqual(buckets[2].cost_usd, 2.0)

    def test_only_unsuccessful_runs_count_as_failures(self):
        buckets = self.buckets(
            sessions=[
                session(self.now, "failed"),
                session(self.now, "resource_exhausted"),
                session(self.now, "succeeded"),
                session(self.now, "running"),
            ],
            days=2,
        )
        self.assertEqual(buckets[-1].failures, 2)

    def test_anything_outside_the_window_is_ignored(self):
        old = self.now - timedelta(days=90)
        self.assertEqual(sum(b.runs for b in self.buckets([usage(old)], days=5)), 0)

    def test_every_chart_is_also_a_table(self):
        """A bar answers "was yesterday unusual" and cannot answer "how much
        did Thursday cost"; both get asked of this page."""

        rendered = activity_views.render_activity(
            self.buckets([usage(self.now, cost=3.7, tokens=2_500_000)]), divisor=37.0
        )
        # Four charts have data; the fifth says there were no failures rather
        # than drawing an empty axis.
        self.assertEqual(rendered.count("<svg"), 4)
        self.assertIn("No runs that did not succeed in this period", rendered)
        self.assertIn("$3.70", rendered)        # list, in the table
        self.assertIn("$0.10", rendered)        # on subscription
        self.assertIn("2.5M", rendered)
        self.assertIn("<table>", rendered)

    def test_a_chart_with_nothing_in_it_says_so_rather_than_drawing_nothing(self):
        rendered = activity_views.render_activity(self.buckets(days=4))
        self.assertIn("No runs in this period", rendered)
        self.assertNotIn("<svg", rendered)

    def test_a_day_too_small_to_see_still_gets_a_mark(self):
        """A bar of zero height reads as "nothing happened", which is a
        different fact from "a little happened"."""

        buckets = self.buckets(
            [usage(self.now, cost=1000.0), usage(self.now - timedelta(days=1), cost=0.01)],
            days=3,
        )
        rendered = activity_views.render_activity(buckets)
        self.assertNotIn("height='0.0'", rendered)

    def test_dynamic_text_is_escaped(self):
        rendered = activity_views.render_activity(
            [activity_views.DayBucket(day="<script>", runs=1, cost_usd=1.0)]
        )
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
