import tempfile
import unittest
from pathlib import Path

from patch_watcher import run_history
from patch_watcher.session_state import SessionStateStore


class RunHistoryTests(unittest.TestCase):
    def store(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return SessionStateStore(Path(self.temp.name) / "s.sqlite3")

    def finish(self, store, run_id, patch_id, state, *, messages=(), result=None,
               failure_code=None, failure_summary=None):
        session_id = "session-" + run_id
        store.register_pinned_session(
            session_id, patch_id=patch_id, run_id=run_id, revision="a" * 40,
            patchset=2, profile="engineering", state="running",
        )
        for author, body in messages:
            store.record_message(session_id, author, body)
        store.finish_session(
            session_id, state, result=result,
            failure_code=failure_code, failure_summary=failure_summary,
        )

    def test_the_work_comes_first_and_the_failure_alongside(self):
        """"shell interpreters are not permitted in safe commands" is a fact
        about the machinery and tells the next agent nothing.  The same run's
        last message was "patchset 6 uploaded, replies posted", which tells it
        everything."""

        store = self.store()
        self.finish(
            store, "pw-review-35302-a", "35302", "failed",
            messages=[("agent", "Patchset 6 uploaded, replies posted.")],
            failure_code="worker_report_invalid",
            failure_summary="shell interpreters are not permitted in safe commands",
        )
        run = run_history.prior_runs(store, ["35302"])[0]
        self.assertEqual(run.summary, "Patchset 6 uploaded, replies posted.")
        self.assertEqual(run.source, "last message")
        self.assertIn("shell interpreters", run.failure)

        rendered = run_history.render_prior_runs([run])
        self.assertIn("Patchset 6 uploaded", rendered)
        self.assertIn("stopped by: shell interpreters", rendered)

    def test_a_report_beats_the_last_message(self):
        """The run's own account of what it did, when it wrote one."""

        store = self.store()
        self.finish(
            store, "pw-review-35302-b", "35302", "succeeded",
            messages=[("agent", "Let me check one more thing.")],
            result={"schema": "patch-watcher-engineering-report/v1",
                    "state": "complete", "summary": "Addressed all four threads.",
                    "changed_files": []},
        )
        run = run_history.prior_runs(store, ["35302"])[0]
        self.assertEqual(run.summary, "Addressed all four threads.")
        self.assertEqual(run.source, "report")

    def test_newest_first_and_the_current_run_is_not_its_own_history(self):
        store = self.store()
        for name in ("one", "two", "three"):
            self.finish(store, f"pw-r-{name}", "35302", "failed",
                        messages=[("agent", f"did {name}")],
                        failure_code="x", failure_summary="x")
        runs = run_history.prior_runs(store, ["35302"], exclude_run_id="pw-r-three")
        self.assertEqual([r.run_id for r in runs], ["pw-r-two", "pw-r-one"])

    def test_a_group_shares_one_history(self):
        """A run on one patch of a series is the previous run on the series;
        hiding it would repeat the mistake of handing an agent one change and
        asking it about the chain."""

        store = self.store()
        self.finish(store, "pw-r-68763", "68763", "succeeded",
                    result={"schema": "patch-watcher-engineering-report/v1",
                            "state": "complete", "summary": "Fixed the base patch.",
                            "changed_files": []})
        self.finish(store, "pw-r-68764", "68764", "succeeded",
                    result={"schema": "patch-watcher-engineering-report/v1",
                            "state": "complete", "summary": "Fixed the one above it.",
                            "changed_files": []})
        runs = run_history.prior_runs(store, ["68763", "68764"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(run_history.prior_runs(store, ["68763"])[0].patch_id, "68763")

    def test_nothing_to_say_renders_nothing(self):
        """A heading that says nothing is a heading the agent must read to
        discover it can be ignored."""

        self.assertEqual(run_history.render_prior_runs([]), "")
        self.assertEqual(run_history.prior_runs(self.store(), ["35302"]), [])
        self.assertEqual(run_history.prior_runs(None, ["35302"]), [])

    def test_a_running_run_is_not_history_yet(self):
        store = self.store()
        store.register_pinned_session(
            "s-live", patch_id="35302", run_id="pw-live", revision="a" * 40,
            patchset=2, profile="engineering", state="running",
        )
        self.assertEqual(run_history.prior_runs(store, ["35302"]), [])

    def test_the_transcript_names_the_run_and_what_it_concluded(self):
        store = self.store()
        self.finish(
            store, "pw-review-35302-c", "35302", "failed",
            messages=[("agent", "Read the threads."), ("agent", "Uploaded.")],
            failure_code="runner_lost", failure_summary="host_process_missing",
        )
        text = run_history.transcript(store, "pw-review-35302-c")
        self.assertIn("pw-review-35302-c", text)
        self.assertIn("Read the threads.", text)
        self.assertIn("Uploaded.", text)
        self.assertIn("host_process_missing", text)
        self.assertIn("No run named 'nope'.", run_history.transcript(store, "nope"))


if __name__ == "__main__":
    unittest.main()
