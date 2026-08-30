import threading
import unittest

from observer import BackgroundObserver


class BackgroundObserverTests(unittest.TestCase):
    def test_tick_refreshes_and_evaluates_each_successful_patch(self):
        patches = [{"id": "one"}, {"id": "two"}]
        refreshed = []
        evaluated = []
        observer = BackgroundObserver(
            lambda: patches,
            lambda patch: refreshed.append(patch["id"]),
            lambda patch: evaluated.append(patch["id"]),
            interval_seconds=60,
        )
        self.assertTrue(observer.tick())
        self.assertEqual(refreshed, ["one", "two"])
        self.assertEqual(evaluated, ["one", "two"])

    def test_refresh_error_result_suppresses_automation_for_that_patch(self):
        evaluated = []
        observer = BackgroundObserver(
            lambda: [{"id": "one"}, {"id": "two"}],
            lambda patch: "read failed" if patch["id"] == "one" else None,
            lambda patch: evaluated.append(patch["id"]),
            interval_seconds=60,
        )
        observer.tick()
        self.assertEqual(evaluated, ["two"])

    def test_one_patch_exception_is_reported_and_does_not_stop_others(self):
        errors = []
        evaluated = []

        def refresh(patch):
            if patch["id"] == "one":
                raise RuntimeError("boom")

        observer = BackgroundObserver(
            lambda: [{"id": "one"}, {"id": "two"}],
            refresh,
            lambda patch: evaluated.append(patch["id"]),
            interval_seconds=60,
            error_handler=lambda patch, error: errors.append((patch["id"], str(error))),
        )
        observer.tick()
        self.assertEqual(errors, [("one", "boom")])
        self.assertEqual(evaluated, ["two"])

    def test_concurrent_tick_is_coalesced(self):
        entered = threading.Event()
        release = threading.Event()

        def refresh(_patch):
            entered.set()
            release.wait(timeout=2)

        observer = BackgroundObserver(
            lambda: [{"id": "one"}],
            refresh,
            lambda patch: None,
            interval_seconds=60,
        )
        thread = threading.Thread(target=observer.tick)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(observer.tick())
        release.set()
        thread.join(timeout=2)

    def test_rejects_non_positive_interval(self):
        with self.assertRaises(ValueError):
            BackgroundObserver(lambda: [], lambda patch: None, lambda patch: None, interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
