import threading
import unittest

from patch_watcher.observer import BackgroundObserver


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
            BackgroundObserver(list, lambda patch: None, lambda patch: None, interval_seconds=0)


if __name__ == "__main__":
    unittest.main()


class ObserverSurvivalTests(unittest.TestCase):
    """The polling thread must outlive any single failure.

    `patches_provider()` was called outside the per-patch guard, so one
    exception from it escaped `tick` (which had only a `finally`) and ended the
    thread permanently. Nothing surfaced that: the dashboard kept serving,
    patches simply stopped being refreshed.
    """

    def test_a_failing_patches_provider_does_not_escape_tick(self):
        seen = []

        def explode():
            raise RuntimeError("store is locked")

        observer = BackgroundObserver(
            explode, lambda patch: None, lambda patch: None,
            interval_seconds=60,
            error_handler=lambda patch, exc: seen.append(exc),
        )
        self.assertFalse(observer.tick())
        self.assertEqual([type(exc) for exc in seen], [RuntimeError])

    def test_a_later_tick_still_works_after_a_failure(self):
        state = {"fail": True}

        def provider():
            if state["fail"]:
                raise RuntimeError("transient")
            return [{"url": "u"}]

        refreshed = []
        observer = BackgroundObserver(
            provider, lambda patch: refreshed.append(patch) or None,
            lambda patch: None, interval_seconds=60,
            error_handler=lambda patch, exc: None,
        )
        self.assertFalse(observer.tick())
        state["fail"] = False
        self.assertTrue(observer.tick())
        self.assertEqual(len(refreshed), 1)

    def test_stop_does_not_return_while_the_poller_is_still_running(self):
        """`stop()` must join, not merely ask.

        Only the self-join guard path was ever exercised -- `stop()` called
        from inside a tick, where joining would deadlock -- so the join itself
        was never run by any test. Without it `stop()` returns while the
        polling thread is still mid-tick against a store the caller is about
        to tear down, and the next thing that store sees is a refresh from a
        thread its owner believes is gone.
        """

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def provider():
            entered.set()
            release.wait(timeout=5)
            finished.set()
            return []

        observer = BackgroundObserver(
            provider, lambda patch: None, lambda patch: None,
            interval_seconds=0.01, error_handler=lambda patch, exc: None,
        )
        observer.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=5))

        # The tick can only end once this fires, so a `stop()` that returns
        # before it has plainly not waited for the thread.
        timer = threading.Timer(0.3, release.set)
        timer.start()
        try:
            observer.stop(timeout=5)
        finally:
            timer.cancel()

        self.assertTrue(
            finished.is_set(),
            "stop() returned while the poller was still inside a tick",
        )
        self.assertFalse(
            observer._thread.is_alive(),
            "stop() returned while the poller thread was still running",
        )

    def test_stop_called_from_inside_a_tick_does_not_deadlock(self):
        """The self-join guard: a tick that stops its own observer.

        `Thread.join()` on the current thread raises RuntimeError, so the
        guard is what lets automation shut the poller down from inside a
        refresh callback.
        """

        stopped = []
        errors = []

        class SelfStopping(BackgroundObserver):
            def tick(self_inner):
                stopped.append(1)
                self_inner.stop(timeout=5)
                return True

        observer = SelfStopping(
            list, lambda patch: None, lambda patch: None,
            interval_seconds=0.01,
            error_handler=lambda patch, exc: errors.append(exc),
        )
        observer.start()
        observer._thread.join(timeout=5)
        self.assertFalse(observer._thread.is_alive())
        self.assertEqual(len(stopped), 1)
        self.assertEqual(
            errors, [],
            "stop() from inside a tick tried to join its own thread",
        )

    def test_the_loop_survives_a_tick_that_raises(self):
        calls = []

        class Exploding(BackgroundObserver):
            def tick(self_inner):
                calls.append(1)
                if len(calls) < 3:
                    raise RuntimeError("bookkeeping failed")
                self_inner.stop()
                return True

        observer = Exploding(
            list, lambda patch: None, lambda patch: None,
            interval_seconds=0.01, error_handler=lambda patch, exc: None,
        )
        observer.start()
        observer._thread.join(timeout=5)
        self.assertGreaterEqual(
            len(calls), 3, "the observer thread died on its first failure"
        )
