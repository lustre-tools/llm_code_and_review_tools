import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from gerrit_reply import (
    GerritReplyAmbiguousError,
    GerritReplyConflict,
    GerritReplyController,
    GerritReplyDefiniteFailure,
    GerritReviewWriter,
    ReviewReplyStateStore,
    review_snapshot_digest,
)
from gerrit_status import GerritConfig


REVISION = "a" * 40


def snapshot(*, revision=REVISION, patchset=3):
    value = {
        "schema": "patch-watcher-review-snapshot/v1",
        "change": {
            "server": "https://review.whamcloud.com",
            "change_number": 68541,
            "project": "fs/lustre-release",
            "branch": "master",
            "change_id": "I" + "1" * 40,
            "status": "NEW",
            "patchset": patchset,
            "revision_sha": revision,
            "gerrit_updated_at": "2026-09-04 10:00:00.000000000",
        },
        "reported_unresolved_count": 2,
        "complete": True,
        "incompleteness_reasons": [],
        "threads": [
            {"thread_id": "root-1", "comments": [{
                "comment_id": "comment-1", "thread_id": "root-1",
                "unresolved": True,
                "location": {
                    "path": "lustre/file.c", "side": "REVISION", "line": 17,
                    "range": None,
                },
            }]},
            {"thread_id": "root-2", "comments": [{
                "comment_id": "comment-2", "thread_id": "root-2",
                "unresolved": True,
                "location": {
                    "path": "/PATCHSET_LEVEL", "side": "REVISION", "line": None,
                    "range": None,
                },
            }]},
        ],
    }
    value["snapshot_sha256"] = review_snapshot_digest(value)
    value["captured_at"] = "2026-09-04T10:00:01+00:00"
    return value


def identity(*, revision=REVISION, patchset=3):
    return {
        "change_number": 68541, "project": "fs/lustre-release",
        "patchset": patchset, "revision_sha": revision, "status": "NEW",
    }


class FakeWriter:
    def __init__(self, outcome="success"):
        self.outcome = outcome
        self.posts = []

    def review_url(self, change_number, revision_sha):
        return (
            "https://review.whamcloud.com/a/changes/"
            f"{change_number}/revisions/{revision_sha}/review"
        )

    def post_review(self, url, payload):
        self.posts.append((url, payload))
        if self.outcome == "ambiguous":
            raise GerritReplyAmbiguousError("timeout")
        if self.outcome == "failed":
            raise GerritReplyDefiniteFailure("rejected")
        return {}


class GerritReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ReviewReplyStateStore(self.root / "replies.sqlite3")
        self.snapshot = snapshot()
        self.resolution = self.root / "review-resolution-plan.json"
        self.write_resolution()

    def tearDown(self):
        self.temp.cleanup()

    def write_resolution(self, **changes):
        value = {
            "schema": "patch-watcher-review-resolution/v1",
            "run_id": "run-1",
            "revision_sha": REVISION,
            "review_mode": "all",
            "review_snapshot_sha256": self.snapshot["snapshot_sha256"],
            "comment_results": [
                {
                    "comment_id": "comment-1", "assessment": "simple",
                    "disposition": "addressed", "summary": "Renamed it.",
                    "reply_draft": "Done in the new patchset.",
                    "changed_files": ["lustre/file.c"],
                },
                {
                    "comment_id": "comment-2", "assessment": "simple",
                    "disposition": "reply_draft", "summary": "Answered it.",
                    "reply_draft": "This behavior is intentional.",
                    "changed_files": [],
                },
            ],
            "controller_observed_status_sha256": "b" * 64,
            "controller_observed_diff_sha256": "c" * 64,
        }
        value.update(changes)
        self.resolution.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )

    @property
    def resolution_sha(self):
        return hashlib.sha256(self.resolution.read_bytes()).hexdigest()

    def controller(
        self, *, writer=None, identities=None, snapshots=None, comments=None,
        enabled=True,
    ):
        identity_values = iter(identities or [identity()])
        snapshot_values = iter(snapshots or [self.snapshot, self.snapshot])
        comment_values = iter(comments or [{}])
        return GerritReplyController(
            self.store, writer or FakeWriter(),
            identity_fetcher=lambda _url: next(identity_values),
            snapshot_fetcher=lambda _url, _revision: next(snapshot_values),
            comments_fetcher=lambda _change, _revision: next(comment_values),
            enabled=enabled,
        )

    def prepare(self, controller, **changes):
        values = {
            "run_id": "run-1", "session_id": "session-1",
            "review_snapshot": self.snapshot,
            "resolution_path": self.resolution,
            "resolution_artifact_id": "review-resolution-run-1",
            "resolution_sha256": self.resolution_sha,
            "requested_by": "review-policy:abc",
            "idempotency_key": "reply-once-run-1",
        }
        values.update(changes)
        return controller.prepare(**values)

    def test_prepare_binds_exact_drafts_and_is_idempotent(self):
        controller = self.controller(identities=[identity(), identity()])
        first = self.prepare(controller)
        second = self.prepare(controller)
        self.assertEqual(first.reply_id, second.reply_id)
        self.assertEqual(first.state, "prepared")
        self.assertEqual(first.classification, "pending")
        self.assertEqual(first.api_url, (
            "https://review.whamcloud.com/a/changes/68541/revisions/"
            + REVISION + "/review"
        ))
        self.assertEqual(first.payload["comments"]["lustre/file.c"][0], {
            "in_reply_to": "comment-1", "line": 17,
            "message": "Done in the new patchset.", "side": "REVISION",
            "unresolved": False,
        })

    def test_addressed_comment_without_reply_remains_valid_snapshot_member(self):
        self.write_resolution(comment_results=[
            {
                "comment_id": "comment-1", "assessment": "simple",
                "disposition": "addressed", "summary": "Changed the code.",
                "reply_draft": "Done in the new patchset.",
                "changed_files": ["lustre/file.c"],
            },
            {
                "comment_id": "comment-2", "assessment": "simple",
                "disposition": "addressed", "summary": "No reply needed.",
                "reply_draft": "", "changed_files": [],
            },
        ])
        writer = FakeWriter()
        controller = self.controller(writer=writer, identities=[identity(), identity()])
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(writer.posts), 1)
        self.assertEqual(set(plan.payload["comments"]), {"lustre/file.c"})

    def test_identity_reuse_with_changed_immutable_draft_conflicts(self):
        controller = self.controller(identities=[identity(), identity()])
        self.prepare(controller)
        self.write_resolution(comment_results=[
            {
                "comment_id": "comment-1", "disposition": "addressed",
                "reply_draft": "Different", "changed_files": [],
            },
            {
                "comment_id": "comment-2", "disposition": "addressed",
                "reply_draft": "Different", "changed_files": [],
            },
        ])
        with self.assertRaisesRegex(GerritReplyConflict, "identity was reused"):
            self.prepare(controller)

    def test_tampered_snapshot_resolution_and_deferred_results_fail_closed(self):
        controller = self.controller()
        broken = dict(self.snapshot)
        broken["reported_unresolved_count"] = 99
        with self.assertRaisesRegex(GerritReplyConflict, "snapshot digest"):
            self.prepare(controller, review_snapshot=broken)

        original_sha = self.resolution_sha
        self.resolution.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(GerritReplyConflict, "digest"):
            self.prepare(controller, resolution_sha256=original_sha)

        self.write_resolution(comment_results=[
            {
                "comment_id": "comment-1", "disposition": "needs_human",
                "reply_draft": "Do not post", "changed_files": [],
            },
            {
                "comment_id": "comment-2", "disposition": "addressed",
                "reply_draft": "Done", "changed_files": [],
            },
        ])
        with self.assertRaisesRegex(GerritReplyConflict, "deferred"):
            self.prepare(controller)

    def test_stale_prepare_never_posts(self):
        writer = FakeWriter()
        controller = self.controller(
            writer=writer, identities=[identity(patchset=4, revision="d" * 40)],
        )
        plan = self.prepare(controller)
        self.assertEqual(plan.state, "stale")
        self.assertEqual(plan.classification, "stale")
        self.assertEqual(writer.posts, [])

    def test_success_posts_once_and_repeated_execute_is_read_only(self):
        writer = FakeWriter()
        controller = self.controller(writer=writer, identities=[identity(), identity()])
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.classification, "success")
        self.assertEqual(len(writer.posts), 1)

    def test_reply_to_original_revision_survives_its_own_new_patchset(self):
        writer = FakeWriter()
        advanced = identity(patchset=4, revision="d" * 40)
        advanced["revision_numbers"] = {REVISION: 3, "d" * 40: 4}
        controller = self.controller(
            writer=writer, identities=[advanced, advanced],
            snapshots=[self.snapshot, self.snapshot],
        )
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertIn(f"/revisions/{REVISION}/review", writer.posts[0][0])
        again = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(again.state, "succeeded")
        self.assertEqual(len(writer.posts), 1)

    def test_comment_snapshot_change_before_dispatch_is_stale(self):
        writer = FakeWriter()
        changed = dict(self.snapshot)
        changed["snapshot_sha256"] = "d" * 64
        controller = self.controller(
            writer=writer, identities=[identity(), identity()],
            snapshots=[self.snapshot, changed],
        )
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "stale")
        self.assertEqual(writer.posts, [])

    def test_ambiguous_write_is_reconciled_without_retry(self):
        writer = FakeWriter("ambiguous")
        controller = self.controller(
            writer=writer, identities=[identity(), identity(), identity()],
            comments=[{}, {}],
        )
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(result.classification, "ambiguous")
        again = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(again.state, "ambiguous")
        self.assertEqual(len(writer.posts), 1)

    def test_timeout_after_accept_reconciles_exact_tagged_replies(self):
        writer = FakeWriter("ambiguous")
        controller = self.controller(
            writer=writer, identities=[identity(), identity()], comments=[{}],
        )
        plan = self.prepare(controller)
        observed = {}
        for path, items in plan.payload["comments"].items():
            observed[path] = [{**item, "tag": plan.tag} for item in items]
        controller.comments_fetcher = lambda _change, _revision: observed
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(writer.posts), 1)

    def test_ambiguous_write_then_revision_advance_is_stale(self):
        writer = FakeWriter("ambiguous")
        controller = self.controller(
            writer=writer,
            identities=[identity(), identity(), identity(patchset=4, revision="d" * 40)],
            comments=[{}],
        )
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "stale")
        self.assertEqual(result.classification, "stale")
        self.assertEqual(len(writer.posts), 1)

    def test_definite_rejection_is_failed(self):
        writer = FakeWriter("failed")
        controller = self.controller(writer=writer, identities=[identity(), identity()])
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.classification, "failed")

    def test_writer_uses_only_allowlisted_exact_revision_post(self):
        captured = []

        def transport(request, timeout):
            captured.append((request, timeout))
            return b")]}'\n{}"

        config = GerritConfig(
            "https://review.whamcloud.com", "writer", "top-secret",
        )
        writer = GerritReviewWriter(config, transport=transport)
        url = writer.review_url(68541, REVISION)
        writer.post_review(url, {"tag": "autogenerated:test", "comments": {}})
        request, timeout = captured[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, url)
        self.assertEqual(timeout, 30)
        self.assertNotIn("top-secret", request.data.decode("utf-8"))
        with self.assertRaisesRegex(GerritReplyConflict, "allowlisted"):
            writer.post_review(
                "https://evil.example/a/changes/68541/revisions/" + REVISION + "/review",
                {"tag": "x"},
            )
        with self.assertRaises(GerritReplyConflict):
            GerritReviewWriter(GerritConfig(
                "https://review.whamcloud.com/evil", "writer", "top-secret",
            ))

    def test_writer_fetches_only_exact_revision_comments(self):
        captured = []

        def transport(request, _timeout):
            captured.append(request)
            return b")]}'\n{\"file.c\": []}"

        writer = GerritReviewWriter(
            GerritConfig("https://review.whamcloud.com", "writer", "secret"),
            transport=transport,
        )
        self.assertEqual(writer.fetch_comments(68541, REVISION), {"file.c": []})
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(captured[0].full_url, (
            "https://review.whamcloud.com/a/changes/68541/revisions/"
            + REVISION + "/comments"
        ))

    def test_invalid_reconciliation_data_settles_ambiguous(self):
        writer = FakeWriter("ambiguous")
        controller = self.controller(
            writer=writer, identities=[identity(), identity()],
            comments=[{"file.c": "not-a-list"}],
        )
        plan = self.prepare(controller)
        result = controller.execute(
            plan.reply_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(len(writer.posts), 1)

    def test_transport_timeout_is_explicitly_ambiguous(self):
        writer = GerritReviewWriter(
            GerritConfig("https://review.whamcloud.com", "writer", "secret"),
            transport=lambda _request, _timeout: (_ for _ in ()).throw(URLError("timeout")),
        )
        with self.assertRaises(GerritReplyAmbiguousError):
            writer.post_review(
                writer.review_url(68541, REVISION),
                {"tag": "autogenerated:test", "comments": {}},
            )


if __name__ == "__main__":
    unittest.main()
