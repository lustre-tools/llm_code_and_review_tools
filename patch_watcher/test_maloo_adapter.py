import json
import shlex
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from patch_watcher import maloo_adapter

SID = "11111111-2222-3333-4444-555555555555"
SUITE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def envelope(command, data=None, *, ok=True, error=None):
    value = {"ok": ok, "meta": {"tool": "maloo", "command": command}}
    if ok:
        value["data"] = data or {}
    else:
        value["error"] = error or {"code": "API_ERROR", "message": "failed"}
    return json.dumps(value)


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def result(command, data, returncode=0, stderr=""):
    return maloo_adapter.CommandResult(returncode, envelope(command, data), stderr)


class MalooAdapterTests(unittest.TestCase):
    def session_payload(self, **changes):
        value = {
            "session_id": SID,
            "test_group": "review-dne-part-1",
            "test_name": "lustre-reviews--review-dne-part-1",
            "test_host": "vm-1",
            "submission": "2026-08-30T10:00:00Z",
            "enforcing": "true",
            "passed": 4,
            "failed": 1,
            "aborted": 0,
            "total": 5,
            "suites": [{
                "id": SUITE, "name": "sanity", "status": "FAIL",
                "passed": 49, "failed": 1, "skipped": 0, "total": 50,
            }],
        }
        value.update(changes)
        return value

    def test_session_uses_shell_free_envelope_argv_and_normalizes_enforcing(self):
        runner = FakeRunner([result("session", self.session_payload())])
        session = maloo_adapter.MalooAdapter(runner=runner).get_session(
            "https://testing.whamcloud.com/test_sessions/" + SID
        )
        self.assertEqual(runner.calls, [("maloo", "--envelope", "session", SID)])
        self.assertTrue(session.enforcing)
        self.assertEqual(session.suites[0].suite_id, SUITE)
        self.assertEqual(session.suites[0].failed, 1)
        self.assertIsNone(session.retest_pending)

    def test_session_preserves_forward_compatible_pending_retest_evidence(self):
        runner = FakeRunner([result("session", self.session_payload(
            retest_pending=True, retest_status="queued", retest_ticket="lu-12345"
        ))])
        session = maloo_adapter.MalooAdapter(runner=runner).get_session(SID)
        self.assertTrue(session.retest_pending)
        self.assertEqual(session.retest_status, "queued")
        self.assertEqual(session.retest_ticket, "LU-12345")

    def test_failures_normalize_suite_ids_and_redact_failure_text(self):
        data = {
            "session_id": SID,
            "test_group": "group",
            "test_name": "name",
            "failed_suites": [{
                "suite": "sanity", "suite_id": SUITE, "status": "FAIL",
                "failed_count": 1, "total_count": 50,
                "failed_subtests": [{
                    "name": "test_39b", "status": "FAIL",
                    "error": "MALOO_PASS=hunter2 token=abc123", "return_code": 1,
                }],
            }],
        }
        runner = FakeRunner([result("failures", data)])
        failures = maloo_adapter.MalooAdapter(runner=runner).get_failures(SID)
        self.assertEqual(failures.suite_ids, (SUITE,))
        self.assertEqual(failures.failed_suites[0].failed_subtests[0].name, "test_39b")
        serialized = json.dumps(failures.to_dict())
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("abc123", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_bug_links_distinguish_accepted_pending_and_default_accepted(self):
        data = {"buggable_id": SUITE, "bug_links": [
            {"bug_upstream_id": "LU-100", "state": "accepted", "buggable_id": SUITE},
            {"ticket": "LU-101", "state": "pending"},
            {"bug_id": "LU-102"},
        ]}
        runner = FakeRunner([result("bugs", data)])
        bugs = maloo_adapter.MalooAdapter(runner=runner).get_bug_links(SUITE)
        self.assertEqual([item.ticket for item in bugs.accepted], ["LU-100", "LU-102"])
        self.assertEqual([item.ticket for item in bugs.pending], ["LU-101"])
        self.assertEqual(bugs.links[2].state, "accepted")
        self.assertEqual(runner.calls[0][-1], "--related")

    def test_a_bug_read_about_a_different_suite_is_refused(self):
        """The write paths verify their echo; this read did not.

        `_link_state` matches only on ticket, so links belonging to another
        test set would be read as this one's: a caller then sees an existing
        link and skips writing one -- leaving the real suite unbugged -- or
        issues a retest on the strength of an association that does not exist
        for it.
        """

        runner = FakeRunner([result("bugs", {
            "buggable_id": "9" * 24,
            "bug_links": [{"ticket": "LU-100"}],
        })])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.get_bug_links(SUITE)
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_RESPONSE
        )
        self.assertIn("different buggable", caught.exception.message)

    def test_a_bug_read_about_the_requested_suite_is_returned(self):
        runner = FakeRunner([result("bugs", {
            "buggable_id": SUITE, "bug_links": [{"ticket": "LU-100"}],
        })])
        bugs = maloo_adapter.MalooAdapter(runner=runner).get_bug_links(SUITE)
        self.assertEqual([item.ticket for item in bugs.accepted], ["LU-100"])

    def test_bug_read_can_disable_related_flag(self):
        runner = FakeRunner([result("bugs", {"buggable_id": SUITE, "bug_links": []})])
        maloo_adapter.MalooAdapter(runner=runner).get_bug_links(SUITE, related=False)
        self.assertEqual(runner.calls[0], ("maloo", "--envelope", "bugs", SUITE))

    def test_review_and_queue_are_pinned_to_patchset_and_full_revision(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        review_data = {
            "review_id": 68160,
            "patch": 13,
            "sessions": [self.session_payload()],
        }
        queue_data = {
            "filters": {"review_id": revision},
            "queue_entries": [{
                "id": "queue-1", "review_id": revision,
                "test_group": "review-dne-part-1", "status": "Running",
                "review_patch": 13,
            }],
        }
        runner = FakeRunner([result("review", review_data), result("queue", queue_data)])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        review = adapter.get_review_sessions(68160, 13)
        queue = adapter.get_queue(revision)
        self.assertEqual(review.enforced_failed[0].session_id, SID)
        self.assertTrue(queue.entries[0].pending)
        self.assertEqual(runner.calls, [
            ("maloo", "--envelope", "review", "68160", "--patch", "13"),
            ("maloo", "--envelope", "queue", "--review", revision),
        ])

    def test_enforced_failure_collection_is_grouped_once_per_session_not_suite(self):
        second_suite = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        duplicate_session = self.session_payload()
        review_data = {
            "review_id": 68160,
            "patch": 13,
            "sessions": [self.session_payload(), duplicate_session,
                         self.session_payload(session_id="99999999-2222-3333-4444-555555555555",
                                              enforcing=False)],
        }
        failure_data = {
            "session_id": SID, "test_group": "review-dne-part-1", "test_name": "name",
            "failed_suites": [
                {"suite": "sanity", "suite_id": SUITE, "status": "FAIL",
                 "failed_subtests": []},
                {"suite": "replay", "suite_id": second_suite, "status": "FAIL",
                 "failed_subtests": []},
            ],
        }
        def empty_bugs(suite):
            return {"buggable_id": suite, "bug_links": []}
        runner = FakeRunner([
            result("review", review_data), result("failures", failure_data),
            result("bugs", empty_bugs(SUITE)), result("bugs", empty_bugs(second_suite)),
        ])
        grouped = maloo_adapter.MalooAdapter(runner=runner).get_enforced_failures(68160, 13)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].decision_key, (SID, "review-dne-part-1"))
        self.assertEqual(len(grouped[0].suite_bugs), 2)
        self.assertEqual([call[2] for call in runner.calls],
                         ["review", "failures", "bugs", "bugs"])

    def test_retest_is_one_shell_free_call_and_normalized(self):
        data = {"success": True, "session_id": SID, "retest_option": "single",
                "bug_id": "LU-19487", "response": "Retest requested"}
        runner = FakeRunner([result("retest", data)])
        retest = maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "lu-19487")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0],
                         ("maloo", "--envelope", "retest", SID, "LU-19487", "--option", "single"))
        self.assertTrue(retest.requested)
        self.assertEqual(retest.jira_ticket, "LU-19487")

    def test_link_bug_is_one_shell_free_exact_call_and_normalized(self):
        data = {
            "success": True,
            "buggable_class": "TestSet",
            "buggable_id": SUITE,
            "bug": "LU-19487",
            "state": "accepted",
            "response": "OK",
        }
        runner = FakeRunner([result("link-bug", data)])
        linked = maloo_adapter.MalooAdapter(runner=runner).link_bug(
            SUITE, "lu-19487"
        )
        self.assertEqual(
            runner.calls,
            [(
                "maloo", "--envelope", "link-bug", SUITE, "LU-19487",
                "--type", "TestSet", "--state", "accepted",
            )],
        )
        self.assertTrue(linked.linked)
        self.assertEqual(linked.state, "accepted")

    def test_retest_rejects_an_echo_that_differs_in_any_single_field(self):
        """Maloo confirming a *different* retest must never read as success.

        `retest` is a remote write with the same shape of proof as `link-bug`:
        the only evidence of what was queued is the echo.  The one success test
        echoed back exactly what it sent, so none of the three comparisons ran.
        An accepted mismatch settles the run as `waiting_external` believing a
        retest is queued for this session and ticket when Maloo queued one for
        something else -- and reconciliation then goes looking for evidence
        that will never appear, at the cost of the operator's one approval.
        """
        confirmed = {
            "success": True,
            "session_id": SID,
            "retest_option": "single",
            "bug_id": "LU-19487",
            "response": "Retest requested",
        }
        other_session = "99999999-8888-7777-6666-555555555555"
        divergences = (
            ("different session", {"session_id": other_session}),
            ("different ticket", {"bug_id": "LU-19999"}),
            ("different retest option", {"retest_option": "all"}),
        )
        for description, change in divergences:
            with self.subTest(divergence=description):
                runner = FakeRunner([result("retest", {**confirmed, **change})])
                adapter = maloo_adapter.MalooAdapter(runner=runner)
                with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
                    adapter.request_retest(SID, "lu-19487")
                self.assertEqual(
                    caught.exception.code,
                    maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION,
                )
                self.assertTrue(caught.exception.ambiguous)
                self.assertEqual(len(runner.calls), 1)

    def test_link_bug_rejects_an_echo_that_differs_in_any_single_field(self):
        """Maloo confirming a *different* association must never read as success.

        `link-bug` is a remote write, so the only proof of what was written is
        the echo Maloo sends back.  Each of the four echoed fields identifies a
        different thing -- which buggable, which Jira ticket, which buggable
        class, and whether the association is accepted or merely pending -- and
        the only successful-link test in this suite echoed back exactly what it
        sent, so every one of those comparisons was unexercised.  A bug link
        written against the wrong test set or the wrong ticket would then be
        recorded as a completed association: the real failure stays unbugged
        and a ticket gains a failure that is not its own.
        """
        confirmed = {
            "success": True,
            "buggable_class": "TestSet",
            "buggable_id": SUITE,
            "bug": "LU-19487",
            "state": "accepted",
            "response": "OK",
        }
        other_suite = "99999999-8888-7777-6666-555555555555"
        divergences = (
            ("different buggable", {"buggable_id": other_suite}),
            ("different ticket", {"bug": "LU-19999"}),
            ("different buggable class", {"buggable_class": "SubTest"}),
            ("different association state", {"state": "pending"}),
        )
        for description, change in divergences:
            with self.subTest(divergence=description):
                runner = FakeRunner([result("link-bug", {**confirmed, **change})])
                adapter = maloo_adapter.MalooAdapter(runner=runner)
                with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
                    adapter.link_bug(SUITE, "lu-19487")
                self.assertEqual(
                    caught.exception.code,
                    maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION,
                )
                self.assertTrue(caught.exception.ambiguous)
                self.assertEqual(len(runner.calls), 1)

    def test_structured_read_errors_carry_the_right_retryability(self):
        """Retryability decides whether a read is re-issued or abandoned.

        A structured envelope error takes a different path from a process-level
        timeout: the remote error code is looked up in the adapter's
        classification table, and only that table says whether the caller may
        try again.  Nothing exercised those entries, so a transient TIMEOUT or
        CONNECTION_ERROR could be classified permanent -- abandoning a read
        that would have succeeded on retry -- or a definitive rejection such as
        NOT_FOUND could be classified retryable and hammered forever.
        """
        cases = (
            ("TIMEOUT", maloo_adapter.MalooErrorCode.TIMEOUT, True),
            ("CONNECTION_ERROR", maloo_adapter.MalooErrorCode.CONNECTION, True),
            ("NOT_FOUND", maloo_adapter.MalooErrorCode.NOT_FOUND, False),
            ("AUTH_FAILED", maloo_adapter.MalooErrorCode.AUTHENTICATION, False),
            ("INVALID_INPUT", maloo_adapter.MalooErrorCode.INVALID_INPUT, False),
        )
        for remote_code, expected_code, expected_retryable in cases:
            with self.subTest(remote_code=remote_code):
                output = envelope("session", ok=False, error={
                    "code": remote_code, "message": "remote said no",
                })
                runner = FakeRunner([maloo_adapter.CommandResult(1, output)])
                adapter = maloo_adapter.MalooAdapter(runner=runner)
                with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
                    adapter.get_session(SID)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.retryable, expected_retryable)
                self.assertFalse(caught.exception.ambiguous)

    def test_every_mutation_transport_failure_is_ambiguous_and_issued_once(self):
        """A mutation whose outcome is unknown is never retried automatically.

        The failure mode differs; the required verdict does not.
        """
        structured_timeout = envelope(
            "link-bug",
            ok=False,
            error={"code": "TIMEOUT", "message": "remote response timed out"},
        )
        success_envelope_with_invalid_data = json.dumps({
            "ok": True, "data": [], "meta": {"tool": "maloo", "command": "retest"},
        })
        cases = (
            ("link_bug/process timeout", "link_bug", (SUITE, "LU-100"),
             subprocess.TimeoutExpired(["maloo"], 45),
             maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION),
            ("link_bug/structured transport error", "link_bug", (SUITE, "LU-100"),
             maloo_adapter.CommandResult(1, structured_timeout, ""),
             maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION),
            ("request_retest/process timeout", "request_retest", (SID, "LU-100"),
             subprocess.TimeoutExpired(["maloo"], 45),
             maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION),
            ("request_retest/success envelope with invalid data", "request_retest",
             (SID, "LU-100"),
             maloo_adapter.CommandResult(0, success_envelope_with_invalid_data),
             maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION),
        )
        for failure_mode, method, arguments, failure, expected_code in cases:
            with self.subTest(failure_mode=failure_mode):
                runner = FakeRunner([failure])
                adapter = maloo_adapter.MalooAdapter(runner=runner)
                with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
                    getattr(adapter, method)(*arguments)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(caught.exception.ambiguous)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(runner.calls), 1)

    def test_retest_invalid_json_is_ambiguous_and_not_retried(self):
        runner = FakeRunner([maloo_adapter.CommandResult(0, "not json", "password=secret")])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION)
        self.assertNotIn("secret", json.dumps(caught.exception.to_dict()))

    def test_explicit_auth_rejection_is_definitive_and_redacted(self):
        output = envelope("retest", ok=False, error={
            "code": "AUTH_FAILED", "message": "MALOO_PASS=hunter2 Authorization: Bearer token",
        })
        runner = FakeRunner([maloo_adapter.CommandResult(2, output)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AUTHENTICATION)
        self.assertFalse(caught.exception.ambiguous)
        serialized = json.dumps(caught.exception.to_dict())
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("Bearer token", serialized)

    def test_cli_credential_traceback_is_definitive_even_for_mutation(self):
        runner = FakeRunner([maloo_adapter.CommandResult(
            1,
            "",
            "ValueError: Maloo credentials required. Set MALOO_USER and MALOO_PASS",
        )])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AUTHENTICATION)
        self.assertFalse(caught.exception.ambiguous)
        self.assertIn("not configured", str(caught.exception))
        self.assertEqual(len(runner.calls), 1)

    def test_unknown_mutation_failure_is_ambiguous(self):
        output = envelope("retest", ok=False, error={"code": "API_ERROR", "message": "server died"})
        runner = FakeRunner([maloo_adapter.CommandResult(1, output)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION)

    def test_read_timeout_is_retryable_but_not_ambiguous(self):
        runner = FakeRunner([subprocess.TimeoutExpired(["maloo"], 45)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).get_session(SID)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.TIMEOUT)
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.ambiguous)

    def test_read_envelope_error_is_typed(self):
        output = envelope("session", ok=False, error={"code": "NOT_FOUND", "message": "gone"})
        runner = FakeRunner([maloo_adapter.CommandResult(3, output)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).get_session(SID)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.NOT_FOUND)

    def test_mismatched_envelope_metadata_is_rejected(self):
        runner = FakeRunner([result("bugs", self.session_payload())])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).get_session(SID)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_RESPONSE)

    def test_reconciliation_finds_pending_group_without_retesting(self):
        runner = FakeRunner([])
        evidence = {"enforced": {"tests": [{
            "test": "review-dne", "retest_pending": True,
            "failures": [{"url": "https://testing.whamcloud.com/test_sessions/" + SID}],
        }]}}
        outcome = maloo_adapter.MalooAdapter(runner=runner).reconcile_retest(
            SID, evidence=evidence
        )
        self.assertEqual(outcome.outcome, "pending")
        self.assertTrue(outcome.already_requested)
        self.assertEqual(runner.calls, [])

    def test_reconciliation_from_success_result_is_already_requested(self):
        evidence = maloo_adapter.MalooRetestResult(SID, "LU-100", "single", True, "ok")
        outcome = maloo_adapter.reconcile_retest_evidence(SID, evidence, jira_ticket="LU-100")
        self.assertEqual(outcome.outcome, "already_requested")
        self.assertIsNone(outcome.pending)
        self.assertEqual(outcome.ticket, "LU-100")

    def test_reconciliation_ignores_other_session_and_wrong_ticket(self):
        other = "99999999-2222-3333-4444-555555555555"
        evidence = [
            {"session_id": other, "retest_pending": True, "bug_id": "LU-100"},
            {"session_id": SID, "retest_pending": True, "bug_id": "LU-999"},
        ]
        outcome = maloo_adapter.reconcile_retest_evidence(SID, evidence, jira_ticket="LU-100")
        self.assertEqual(outcome.outcome, "not_observed")

    def test_reconciliation_without_evidence_performs_one_read_only_session_call(self):
        runner = FakeRunner([result("session", self.session_payload(retest_pending=True))])
        outcome = maloo_adapter.MalooAdapter(runner=runner).reconcile_retest(SID)
        self.assertEqual(outcome.outcome, "pending")
        self.assertEqual(runner.calls, [("maloo", "--envelope", "session", SID)])
        self.assertNotIn("retest", runner.calls[0])

    def test_remote_reconciliation_matches_exact_revision_and_group_queue(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        queue = maloo_adapter.MalooQueueEvidence(revision, (
            maloo_adapter.MalooQueueEntry("wrong-rev", "a" * 40,
                                          "review-dne-part-1", "Running"),
            maloo_adapter.MalooQueueEntry("wrong-group", revision,
                                          "other", "Running"),
            maloo_adapter.MalooQueueEntry("match", revision,
                                          "review-dne-part-1", "Queued"),
        ))
        review = maloo_adapter.MalooReviewSessions(68160, 13, (
            maloo_adapter.normalize_session(self.session_payload()),
        ))
        runner = FakeRunner([])
        outcome = maloo_adapter.MalooAdapter(runner=runner).reconcile_remote_retest(
            change_number=68160, patchset=13, revision_sha=revision,
            session_ref=SID, test_group="review-dne-part-1",
            queue_evidence=queue, review_evidence=review,
        )
        self.assertEqual(outcome.outcome, "pending")
        self.assertEqual(outcome.sources, ("queue:match",))
        self.assertEqual(runner.calls, [])

    def test_remote_reconciliation_recognizes_newer_same_group_session(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        original = maloo_adapter.normalize_session(self.session_payload(
            submission="2026-08-30T10:00:00Z"))
        newer = maloo_adapter.normalize_session(self.session_payload(
            session_id="99999999-2222-3333-4444-555555555555",
            submission="2026-08-30T12:00:00Z"))
        review = maloo_adapter.MalooReviewSessions(68160, 13, (original, newer))
        queue = maloo_adapter.MalooQueueEvidence(revision, ())
        outcome = maloo_adapter.MalooAdapter(runner=FakeRunner([])).reconcile_remote_retest(
            change_number=68160, patchset=13, revision_sha=revision,
            session_ref=SID, test_group="review-dne-part-1",
            queue_evidence=queue, review_evidence=review,
        )
        self.assertEqual(outcome.outcome, "already_requested")
        self.assertFalse(outcome.pending)
        self.assertIn(newer.session_id, outcome.sources[0])

    def test_remote_reconciliation_does_not_accept_other_revision_group_or_older_session(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        original = maloo_adapter.normalize_session(self.session_payload(
            submission="2026-08-30T10:00:00Z"))
        older = maloo_adapter.normalize_session(self.session_payload(
            session_id="99999999-2222-3333-4444-555555555555",
            submission="2026-08-30T09:00:00Z"))
        review = maloo_adapter.MalooReviewSessions(68160, 13, (original, older))
        queue = maloo_adapter.MalooQueueEvidence(revision, (
            maloo_adapter.MalooQueueEntry("x", "a" * 40, "review-dne-part-1", "Running"),
            maloo_adapter.MalooQueueEntry("y", revision, "other", "Running"),
        ))
        outcome = maloo_adapter.MalooAdapter(runner=FakeRunner([])).reconcile_remote_retest(
            change_number=68160, patchset=13, revision_sha=revision,
            session_ref=SID, test_group="review-dne-part-1",
            queue_evidence=queue, review_evidence=review,
        )
        self.assertEqual(outcome.outcome, "not_observed")

    def test_remote_reconciliation_does_not_guess_newer_without_baseline(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        unrelated = maloo_adapter.normalize_session(self.session_payload(
            session_id="99999999-2222-3333-4444-555555555555",
            submission="2026-08-30T12:00:00Z"))
        review = maloo_adapter.MalooReviewSessions(68160, 13, (unrelated,))
        queue = maloo_adapter.MalooQueueEvidence(revision, ())
        outcome = maloo_adapter.MalooAdapter(runner=FakeRunner([])).reconcile_remote_retest(
            change_number=68160, patchset=13, revision_sha=revision,
            session_ref=SID, test_group="review-dne-part-1",
            queue_evidence=queue, review_evidence=review,
        )
        self.assertEqual(outcome.outcome, "not_observed")

    def test_remote_reconciliation_fetches_only_queue_and_review_never_retest(self):
        revision = "7b77eeb0190d6d93880951533c2e1d1145780375"
        queue_data = {"filters": {"review_id": revision}, "queue_entries": []}
        review_data = {"review_id": 68160, "patch": 13,
                       "sessions": [self.session_payload()]}
        runner = FakeRunner([result("queue", queue_data), result("review", review_data)])
        outcome = maloo_adapter.MalooAdapter(runner=runner).reconcile_remote_retest(
            change_number=68160, patchset=13, revision_sha=revision,
            session_ref=SID, test_group="review-dne-part-1",
        )
        self.assertEqual(outcome.outcome, "not_observed")
        self.assertFalse(outcome.automatic_retry_allowed)
        self.assertEqual([call[2] for call in runner.calls], ["queue", "review"])
        self.assertFalse(any("retest" in call for call in runner.calls))

    def test_validation_rejects_flag_injection_and_bad_ticket_before_io(self):
        runner = FakeRunner([])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        with self.assertRaises(maloo_adapter.MalooAdapterError):
            adapter.get_bug_links("--pretty")
        with self.assertRaises(maloo_adapter.MalooAdapterError):
            adapter.request_retest(SID, "bad ticket")
        self.assertEqual(runner.calls, [])

    def test_response_retest_text_is_redacted_without_losing_the_result(self):
        data = {"success": True, "session_id": SID, "retest_option": "single",
                "bug_id": "LU-1", "response": "password=swordfish"}
        runner = FakeRunner([result("retest", data)])
        response = maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-1")
        # Only the credential is removed; the key that named it stays, and so
        # does every acknowledged field.  Discarding the whole response would
        # also hide "swordfish", so assert the exact redacted form.
        self.assertEqual(response.response, "password=[REDACTED]")
        self.assertEqual(response.session_id, SID)
        self.assertEqual(response.jira_ticket, "LU-1")
        self.assertEqual(response.option, "single")
        self.assertTrue(response.requested)


if __name__ == "__main__":
    unittest.main()


class ExternalDataTests(unittest.TestCase):
    """Maloo's bytes are untrusted input, including on the mutation paths."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fake_cli(self, body):
        """Write a real executable that emits ``body`` on a real pipe."""
        path = Path(self.temp.name) / "maloo"
        script = Path(self.temp.name) / "payload"
        script.write_bytes(body)
        path.write_text(
            "#!/bin/sh\nexec cat " + shlex.quote(str(script)) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return str(path)

    # ---------------------------------------------------------- decoding
    def test_non_utf8_bytes_from_the_real_cli_do_not_raise(self):
        binary = self.fake_cli(
            b'{"ok": true, "meta": {"tool": "maloo", "command": "retest"},'
            b' "data": {"session_id": "' + SID.encode() + b'", "bug_id": "LU-100",'
            b' "retest_option": "single", "success": true,'
            b' "response": "queued caf\xe9"}}'
        )
        adapter = maloo_adapter.MalooAdapter(binary=binary)
        outcome = adapter.request_retest(SID, "LU-100")
        self.assertTrue(outcome.requested)
        self.assertIn("caf", outcome.response)
        outcome.response.encode("utf-8")

    def test_undecodable_mutation_output_is_ambiguous_not_a_local_failure(self):
        # The command has already run when decoding fails, so the remote
        # outcome is unknown.  Recording it as failed/precondition_failed --
        # a definitive local failure -- excluded it from reconciliation.
        broken = UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte")
        adapter = maloo_adapter.MalooAdapter(runner=FakeRunner([broken]))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.request_retest(SID, "LU-100")
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION
        )
        self.assertTrue(caught.exception.ambiguous)

    def test_undecodable_read_output_is_not_ambiguous(self):
        broken = UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte")
        adapter = maloo_adapter.MalooAdapter(runner=FakeRunner([broken]))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.get_session(SID)
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_RESPONSE
        )
        self.assertFalse(caught.exception.ambiguous)

    def test_a_definitive_rejection_is_still_not_ambiguous(self):
        # Guard the other direction: calling a definite failure ambiguous
        # causes spurious reconciliation.
        rejected = maloo_adapter.CommandResult(
            2,
            json.dumps({
                "ok": False,
                "meta": {"tool": "maloo", "command": "retest"},
                "error": {"code": "INVALID_INPUT", "message": "no such session"},
            }),
            "",
        )
        adapter = maloo_adapter.MalooAdapter(runner=FakeRunner([rejected]))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.request_retest(SID, "LU-100")
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_INPUT
        )
        self.assertFalse(caught.exception.ambiguous)

    # ------------------------------------------------------ review identity
    def review_payload(self, change, patch):
        return {
            "review_id": change,
            "patch": patch,
            "sessions": [{
                "session_id": SID, "test_group": "review-dne-part-1",
                "test_name": "t", "test_host": "h", "submission": "2026-01-01",
                "enforcing": True, "passed": 1, "failed": 3, "aborted": 0,
                "total": 4,
            }],
        }

    def test_review_answer_about_another_change_is_rejected(self):
        runner = FakeRunner([result("review", self.review_payload(99999, 7))])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.get_review_sessions(101, 7)
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_RESPONSE
        )
        self.assertIn("different review", caught.exception.message)

    def test_review_answer_about_another_patchset_is_rejected(self):
        runner = FakeRunner([result("review", self.review_payload(101, 9))])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        with self.assertRaises(maloo_adapter.MalooAdapterError):
            adapter.get_review_sessions(101, 7)

    def test_matching_review_answer_is_accepted(self):
        runner = FakeRunner([result("review", self.review_payload(101, 7))])
        adapter = maloo_adapter.MalooAdapter(runner=runner)
        review = adapter.get_review_sessions(101, 7)
        self.assertEqual((review.change_number, review.patchset), (101, 7))

    # ---------------------------------------------------------- identifiers
    def test_identifier_refuses_nul_traversal_and_injection(self):
        for value in (
            "abc\x00def",
            "../../admin/destroy",
            "id;rm -rf /",
            "abc\ndef",
            "-flag",
            " ",
            "a" * 201,
        ):
            with self.subTest(value=value), self.assertRaises(
                maloo_adapter.MalooAdapterError
            ) as caught:
                maloo_adapter._identifier(value, "buggable ID")
            self.assertEqual(
                caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_INPUT
            )

    def test_identifier_still_accepts_real_maloo_values(self):
        for value in (SUITE, "9", "suite-7", "review-dne-part-1", "sanity"):
            self.assertEqual(maloo_adapter._identifier(value, "suite ID"), value)

    def test_nul_identifier_never_reaches_subprocess(self):
        adapter = maloo_adapter.MalooAdapter(binary=self.fake_cli(b"{}"))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.get_bug_links("abc\x00def")
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_INPUT
        )

    # ------------------------------------------------------------ size cap
    def test_oversized_read_is_rejected_before_parsing(self):
        oversized = maloo_adapter.CommandResult(
            0, "x" * (maloo_adapter.MAX_OUTPUT_BYTES + 1), ""
        )
        adapter = maloo_adapter.MalooAdapter(runner=FakeRunner([oversized]))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.get_failures(SID)
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.INVALID_RESPONSE
        )
        self.assertIn("more than", caught.exception.message)
        self.assertFalse(caught.exception.ambiguous)

    def test_oversized_mutation_response_is_ambiguous(self):
        oversized = maloo_adapter.CommandResult(
            0, "x" * (maloo_adapter.MAX_OUTPUT_BYTES + 1), ""
        )
        adapter = maloo_adapter.MalooAdapter(runner=FakeRunner([oversized]))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.request_retest(SID, "LU-100")
        self.assertTrue(caught.exception.ambiguous)

    def test_default_runner_stops_reading_past_the_bound(self):
        with unittest.mock.patch.object(maloo_adapter, "MAX_OUTPUT_BYTES", 4096):
            binary = self.fake_cli(b"y" * (1 << 20))
            outcome = maloo_adapter._default_runner((binary,))
            self.assertEqual(len(outcome.stdout), 4097)
            adapter = maloo_adapter.MalooAdapter(binary=binary)
            with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
                adapter.get_session(SID)
        self.assertIn("more than", caught.exception.message)

    def test_default_runner_still_enforces_a_timeout(self):
        path = Path(self.temp.name) / "slow"
        # ``exec`` so killing the child really kills the sleep.
        path.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        path.chmod(0o755)
        with unittest.mock.patch.object(maloo_adapter, "COMMAND_TIMEOUT", 0.3):
            with self.assertRaises(subprocess.TimeoutExpired):
                maloo_adapter._default_runner((str(path),))

    def test_default_runner_reports_stderr_and_status(self):
        path = Path(self.temp.name) / "failing"
        path.write_text(
            "#!/bin/sh\nprintf 'maloo credentials required' >&2\nexit 3\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        outcome = maloo_adapter._default_runner((str(path),))
        self.assertEqual(outcome.returncode, 3)
        self.assertIn("credentials required", outcome.stderr)
        adapter = maloo_adapter.MalooAdapter(binary=str(path))
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            adapter.request_retest(SID, "LU-100")
        self.assertEqual(
            caught.exception.code, maloo_adapter.MalooErrorCode.AUTHENTICATION
        )
        self.assertFalse(caught.exception.ambiguous)
