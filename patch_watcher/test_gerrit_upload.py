import tempfile
import unittest
import subprocess
from pathlib import Path

from gerrit_status import GerritConfig
from gerrit_upload import (
    GerritUploadConflict,
    GerritUploadController,
    GerritUploadError,
    GitGerritUploader,
    PreparedCommit,
    UploadStateStore,
)


REVISION = "a" * 40
NEW_REVISION = "b" * 40


class FakeUploader:
    def __init__(self, root, *, commit=NEW_REVISION, fail_push=False):
        self.config = GerritConfig(
            "https://review.whamcloud.com", "writer", "secret",
            upload_enabled=True, git_name="Writer", git_email="writer@example.test",
        )
        self.root = Path(root)
        self.commit = commit
        self.fail_push = fail_push
        self.prepare_count = 0
        self.push_count = 0
        self.cleaned = 0

    def prepare_commit(self, plan):
        self.prepare_count += 1
        workspace = self.root / plan.upload_id
        workspace.mkdir(parents=True)
        return PreparedCommit(plan.upload_id, self.commit, workspace)

    def push(self, _plan, _prepared):
        self.push_count += 1
        if self.fail_push:
            raise GerritUploadError("redacted push failure")

    def prepared_commit(self, plan):
        return PreparedCommit(plan.upload_id, plan.local_commit_sha, self.root / plan.upload_id)

    def cleanup(self, prepared):
        if prepared:
            self.cleaned += 1


class GerritUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.diff = self.root / "proposed.patch"
        self.diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
        import hashlib
        self.diff_sha = hashlib.sha256(self.diff.read_bytes()).hexdigest()
        self.store = UploadStateStore(self.root / "uploads.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def values(self, **changes):
        value = {
            "idempotency_key": "prepare-once", "run_id": "run-1",
            "session_id": "session-1", "change_number": 68541,
            "project": "fs/lustre-release", "branch": "master", "patchset": 3,
            "change_id": "I" + "1" * 40,
            "revision_sha": REVISION, "revision_ref": "refs/changes/41/68541/3",
            "diff_path": str(self.diff), "diff_artifact_id": "diff-run-1",
            "diff_sha256": self.diff_sha, "evidence_sha256": "c" * 64,
            "requested_by": "operator",
        }
        value.update(changes)
        return value

    @staticmethod
    def status(*, patchset=3, revision=REVISION):
        return {
            "change_number": 68541,
            "project": "fs/lustre-release",
            "branch": "master",
            "change_id": "I" + "1" * 40,
            "status": "NEW",
            "patchset": patchset,
            "revision_sha": revision,
        }

    def controller(self, statuses, **uploader_options):
        values = iter(statuses)
        uploader = FakeUploader(self.root / "work", **uploader_options)
        controller = GerritUploadController(
            self.store, uploader, status_fetcher=lambda _url: next(values), enabled=True,
        )
        return controller, uploader

    def test_store_prepare_is_idempotent_but_identity_reuse_conflicts(self):
        first = self.store.prepare(**self.values())
        second = self.store.prepare(**self.values())
        self.assertEqual(first.upload_id, second.upload_id)
        with self.assertRaises(GerritUploadConflict):
            self.store.prepare(**self.values(diff_sha256="d" * 64))

    def test_disabled_and_tampered_diff_fail_before_state(self):
        uploader = FakeUploader(self.root / "work")
        disabled = GerritUploadController(
            self.store, uploader, status_fetcher=lambda _url: self.status(), enabled=False,
        )
        with self.assertRaises(GerritUploadConflict):
            disabled.prepare(**self.values())
        self.diff.write_text("changed", encoding="utf-8")
        enabled = GerritUploadController(
            self.store, uploader, status_fetcher=lambda _url: self.status(), enabled=True,
        )
        with self.assertRaises(GerritUploadConflict):
            enabled.prepare(**self.values())

    def test_stale_precheck_dispatches_no_git(self):
        controller, uploader = self.controller([self.status(patchset=4, revision="d" * 40)])
        plan = controller.prepare(**self.values())
        self.assertEqual(plan.state, "stale")
        self.assertEqual(uploader.prepare_count, 0)
        self.assertEqual(uploader.push_count, 0)

    def test_success_is_proved_by_new_current_revision(self):
        controller, uploader = self.controller([
            self.status(), self.status(),
            self.status(patchset=4, revision=NEW_REVISION),
        ])
        plan = controller.prepare(**self.values())
        result = controller.execute(plan.upload_id, expected_binding_digest=plan.binding_digest)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.new_patchset, 4)
        self.assertEqual(result.new_revision_sha, NEW_REVISION)
        self.assertEqual(uploader.push_count, 1)
        again = controller.execute(plan.upload_id, expected_binding_digest=plan.binding_digest)
        self.assertEqual(again.state, "succeeded")
        self.assertEqual(uploader.push_count, 1)

    def test_uncertain_outcome_never_pushes_twice(self):
        controller, uploader = self.controller([
            self.status(), self.status(), self.status(), self.status(),
        ])
        plan = controller.prepare(**self.values())
        result = controller.execute(plan.upload_id, expected_binding_digest=plan.binding_digest)
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(uploader.push_count, 1)
        result = controller.execute(plan.upload_id, expected_binding_digest=plan.binding_digest)
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(uploader.push_count, 1)

    def test_timeout_after_accept_reconciles_as_success(self):
        controller, uploader = self.controller([
            self.status(), self.status(),
            self.status(patchset=4, revision=NEW_REVISION),
        ], fail_push=True)
        plan = controller.prepare(**self.values())
        result = controller.execute(plan.upload_id, expected_binding_digest=plan.binding_digest)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(uploader.push_count, 1)

    def test_confirmation_binding_rejects_tampering(self):
        controller, uploader = self.controller([self.status()])
        plan = controller.prepare(**self.values())
        with self.assertRaises(GerritUploadConflict):
            controller.execute(plan.upload_id, expected_binding_digest="0" * 64)
        self.assertEqual(uploader.prepare_count, 1)
        self.assertEqual(uploader.push_count, 0)

    def test_exact_precheck_rejects_incomplete_identity(self):
        controller, uploader = self.controller([{
            "change_number": 68541, "patchset": 3, "revision_sha": REVISION,
        }])
        plan = controller.prepare(**self.values())
        self.assertEqual(plan.state, "stale")
        self.assertEqual(uploader.prepare_count, 0)

    def test_lost_prepared_workspace_fails_without_push(self):
        controller, uploader = self.controller([self.status(), self.status()])
        plan = controller.prepare(**self.values())
        uploader.prepared_commit = lambda _plan: (_ for _ in ()).throw(
            GerritUploadConflict("workspace missing")
        )
        result = controller.execute(
            plan.upload_id, expected_binding_digest=plan.binding_digest
        )
        self.assertEqual(result.state, "failed")
        self.assertEqual(uploader.push_count, 0)

    def test_real_uploader_removes_partial_workspace_on_prepare_failure(self):
        config = GerritConfig(
            "https://review.whamcloud.com", "writer", "not-serialized",
            upload_enabled=True, git_name="Writer", git_email="writer@example.test",
        )
        uploader = GitGerritUploader(config, self.root / "real-work")
        uploader._git = lambda _workspace, args, **_kwargs: subprocess.CompletedProcess(
            args, 1, b"", b"redacted"
        )
        plan = self.store.prepare(**self.values())
        with self.assertRaises(GerritUploadError):
            uploader.prepare_commit(plan)
        self.assertFalse((self.root / "real-work" / plan.upload_id).exists())


if __name__ == "__main__":
    unittest.main()
