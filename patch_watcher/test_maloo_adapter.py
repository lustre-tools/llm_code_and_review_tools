import json
import subprocess
import unittest

import maloo_adapter


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
        empty_bugs = lambda suite: {"buggable_id": suite, "bug_links": []}
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

    def test_link_bug_timeout_is_ambiguous_and_never_retried(self):
        runner = FakeRunner([subprocess.TimeoutExpired(["maloo"], 45)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).link_bug(SUITE, "LU-100")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            caught.exception.code,
            maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION,
        )
        self.assertTrue(caught.exception.ambiguous)

    def test_link_bug_structured_transport_failure_is_also_ambiguous(self):
        failure = envelope(
            "link-bug",
            ok=False,
            error={"code": "TIMEOUT", "message": "remote response timed out"},
        )
        runner = FakeRunner([maloo_adapter.CommandResult(1, failure, "")])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).link_bug(SUITE, "LU-100")
        self.assertEqual(
            caught.exception.code,
            maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION,
        )
        self.assertTrue(caught.exception.ambiguous)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(runner.calls), 1)

    def test_retest_never_retries_timeout_and_marks_outcome_ambiguous(self):
        runner = FakeRunner([subprocess.TimeoutExpired(["maloo"], 45)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION)
        self.assertTrue(caught.exception.ambiguous)
        self.assertFalse(caught.exception.retryable)

    def test_retest_invalid_json_is_ambiguous_and_not_retried(self):
        runner = FakeRunner([maloo_adapter.CommandResult(0, "not json", "password=secret")])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(caught.exception.code, maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION)
        self.assertNotIn("secret", json.dumps(caught.exception.to_dict()))

    def test_retest_success_envelope_with_invalid_data_is_ambiguous(self):
        output = json.dumps({"ok": True, "data": [],
                             "meta": {"tool": "maloo", "command": "retest"}})
        runner = FakeRunner([maloo_adapter.CommandResult(0, output)])
        with self.assertRaises(maloo_adapter.MalooAdapterError) as caught:
            maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-100")
        self.assertEqual(caught.exception.code,
                         maloo_adapter.MalooErrorCode.AMBIGUOUS_MUTATION)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(runner.calls), 1)

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

    def test_response_retest_text_is_redacted(self):
        data = {"success": True, "session_id": SID, "retest_option": "single",
                "bug_id": "LU-1", "response": "password=swordfish"}
        runner = FakeRunner([result("retest", data)])
        response = maloo_adapter.MalooAdapter(runner=runner).request_retest(SID, "LU-1")
        self.assertNotIn("swordfish", response.response)


if __name__ == "__main__":
    unittest.main()
