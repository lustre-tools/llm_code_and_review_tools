import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from claude_runner import ProcessIdentity, RunnerEvent, RunnerHandle, RunnerSnapshot
from run_controller import RunController, RunControllerError
from session_state import SessionStateStore
from worker_contract import load_profile


DEFAULT_REVISION = "d" * 40


def engineering_patch(revision=DEFAULT_REVISION, **updates):
    value = {
        "change_number": 68160,
        "project": "fs/lustre-release",
        "patchset": 4,
        "revision_sha": revision,
        "revision_ref": "refs/changes/60/68160/4",
        "lifecycle": "Open",
        "title": "LU-12345 exercise the engineering path",
    }
    value.update(updates)
    return value


def review_snapshot(revision=DEFAULT_REVISION):
    return {
        "schema": "patch-watcher-review-snapshot/v1",
        "change": {
            "change_number": 68160, "project": "fs/lustre-release",
            "branch": "master", "change_id": "I" + "a" * 40,
            "status": "NEW", "patchset": 4, "revision_sha": revision,
            "gerrit_updated_at": "now", "server": "https://review.whamcloud.com",
        },
        "reported_unresolved_count": 1, "complete": True,
        "incompleteness_reasons": [], "snapshot_sha256": "a" * 64,
        "captured_at": "2026-09-01T14:00:00+00:00",
        "threads": [{"thread_id": "thread-1", "comments": [{
            "comment_id": "comment-1", "thread_id": "thread-1",
            "message": "Rename this", "unresolved": True,
        }]}],
    }


def build_snapshot(revision=DEFAULT_REVISION, digest="b" * 64):
    return {
        "schema": "patch-watcher-jenkins-failure-snapshot/v1",
        "complete": True,
        "change": {
            "change_number": 68160, "patchset": 4,
            "revision_sha": revision, "revision_ref": "refs/changes/60/68160/4",
            "project": "fs/lustre-release", "branch": "master",
        },
        "build": {
            "job_name": "lustre-reviews", "build_number": 123,
            "url": "https://build.whamcloud.com/job/lustre-reviews/123/",
            "result": "FAILURE", "completed_at": "2026-09-01T14:00:00+00:00",
            "duration_ms": 1000,
        },
        "parent_console_tail": ["FAILURE"], "failed_runs": [],
        "captured_at": "2026-09-01T14:01:00+00:00",
        "snapshot_sha256": digest,
    }


class EngineeringRunner:
    def __init__(self):
        self.starts = []
        self.events_by_session = {}
        self.terminations = []
        self.alive = True
        self.start_error = None

    def start(self, spec):
        self.starts.append(spec)
        if self.start_error is not None:
            raise self.start_error
        handle = RunnerHandle(
            spec.run_id,
            spec.session_id,
            str(Path(spec.runtime_dir) / "claude.sock"),
            str(Path(spec.runtime_dir) / "events.jsonl"),
            str(Path(spec.runtime_dir) / "host-state.json"),
            ProcessIdentity(4242, "host-start", 4242),
            ProcessIdentity(4343, "claude-start", 4343),
        )
        return RunnerSnapshot(
            handle, "running", "running", 1_788_000_000.0,
            1_788_000_000.0, 0, "", None,
        )

    def probe(self, _handle):
        return SimpleNamespace(
            alive=self.alive,
            adoptable=self.alive,
            reason="ok" if self.alive else "host_process_missing",
        )

    def events(self, handle, *, after_cursor=0, limit=100):
        return [
            event
            for event in self.events_by_session.get(handle.session_id, [])
            if event.cursor > after_cursor
        ][:limit]

    def terminate(self, handle):
        self.terminations.append(handle.session_id)
        self.alive = False

    def kill(self, handle):
        self.terminations.append(handle.session_id)
        self.alive = False

    def adopt(self, handle):
        return RunnerSnapshot(
            handle, "running", "idle", 1_788_000_000.0,
            1_788_000_000.0, 0, "", None,
        )

    def queue_guidance(self, *_args):
        return SimpleNamespace(state="queued", duplicate=False)


def ready_doctor(_profile, _envelope, **_kwargs):
    return {
        "status": "ready",
        "failure_codes": [],
        "worker_host": {"host_id": "test-worker"},
        "isolation_mode": "container",
        "network_mode": "restricted",
    }


class EngineeringRunControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStateStore(self.root / "sessions.sqlite3")
        self.profile = load_profile("host-unsandboxed-mac-v1")
        self.engineering_profile = load_profile("host-unsandboxed-mac-engineering-v1")
        self.runner = EngineeringRunner()
        self.now = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def fake_full_clone(destination, _revision):
        destination = Path(destination)
        (destination / ".git").mkdir()
        (destination / "README").write_text("pinned source\n", encoding="utf-8")
        return destination

    def controller(self, checkout=None):
        return RunController(
            self.store,
            self.profile,
            runs_directory=self.root / "runs",
            runner=self.runner,
            checkout=checkout or self.fake_full_clone,
            doctor_fn=ready_doctor,
            clock=lambda: self.now,
            engineering_profile=self.engineering_profile,
        )

    def create_seed_repository(self):
        seed = self.root / "seed"
        seed.mkdir()
        subprocess.run(["git", "init", "--quiet", str(seed)], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(seed), "config", "user.name", "Patch Watcher Test"],
            check=True,
        )
        (seed / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "commit", "--quiet", "-m", "seed"],
            check=True,
        )
        revision = subprocess.run(
            ["git", "-C", str(seed), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return seed, revision

    def test_request_is_bound_to_one_exact_open_gerrit_revision(self):
        controller = self.controller()
        for patch in (
            engineering_patch(revision=""),
            engineering_patch(revision_ref="refs/changes/60/68160/3"),
            engineering_patch(lifecycle="Merged"),
        ):
            with self.subTest(patch=patch), self.assertRaises(RunControllerError):
                controller.request_engineering(patch)

        session = controller.request_engineering(
            engineering_patch(), request_id="durable-request-1"
        )
        replay = controller.request_engineering(
            engineering_patch(), request_id="durable-request-1"
        )
        self.assertEqual(replay.session_id, session.session_id)
        with self.assertRaisesRegex(RunControllerError, "different revision"):
            controller.request_engineering(
                engineering_patch(revision="c" * 40),
                request_id="durable-request-1",
            )
        self.assertEqual((session.patch_id, session.patchset, session.revision), (
            "68160", 4, DEFAULT_REVISION,
        ))
        request = self.store.list_events(session.session_id)[0]
        self.assertEqual(request.payload["revision"], DEFAULT_REVISION)
        self.assertEqual(request.payload["revision_ref"], "refs/changes/60/68160/4")

        changed = engineering_patch(
            revision="e" * 40,
            patchset=5,
            revision_ref="refs/changes/60/68160/5",
        )
        self.assertEqual(controller.reconcile_patch_revision(changed), [session.run_id])
        controller.tick()
        self.assertEqual(self.runner.starts, [])
        self.assertIsNone(controller.engineering_store.get_allocation_by_run(session.run_id))

    def test_review_request_binds_mode_snapshot_and_auto_upload_policy(self):
        controller = self.controller()
        session = controller.request_review_comments(
            engineering_patch(), review_snapshot(), mode="simple",
            request_id="review-request-1",
        )
        replay = controller.request_review_comments(
            engineering_patch(), review_snapshot(), mode="simple",
            request_id="review-request-1",
        )
        self.assertEqual(replay.session_id, session.session_id)
        request = controller._request_payload(session)
        self.assertEqual(request["request_kind"], "review_comments")
        self.assertEqual(request["review_mode"], "simple")
        self.assertEqual(request["target_comment_ids"], ["comment-1"])
        self.assertTrue(request["auto_upload_patchset"])

        controller.tick()
        spec = self.runner.starts[0]
        self.assertIn("review-comments.json", spec.prompt)
        self.assertIn("reply", spec.prompt.lower())
        snapshot_path = self.root / "runs" / session.run_id / "work" / "input" / "review-comments.json"
        self.assertTrue(snapshot_path.is_file())
        self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o400)

    def test_build_failure_request_binds_snapshot_and_rejects_reused_identity(self):
        controller = self.controller()
        session = controller.request_build_failure(
            engineering_patch(), build_snapshot(), request_id="build-request-1",
        )
        replay = controller.request_build_failure(
            engineering_patch(), build_snapshot(), request_id="build-request-1",
        )
        self.assertEqual(replay.session_id, session.session_id)
        with self.assertRaisesRegex(RunControllerError, "reused"):
            controller.request_build_failure(
                engineering_patch(), build_snapshot(digest="c" * 64),
                request_id="build-request-1",
            )
        request = controller._request_payload(session)
        self.assertEqual(request["request_kind"], "build_failure")
        self.assertEqual(request["build_id"], "lustre-reviews/123")
        self.assertTrue(request["auto_upload_patchset"])

        controller.tick()
        spec = self.runner.starts[0]
        self.assertIn("jenkins-failure.json", spec.prompt)
        self.assertIn("open-ended", spec.prompt)
        snapshot_path = (
            self.root / "runs" / session.run_id / "work" / "input"
            / "jenkins-failure.json"
        )
        self.assertTrue(snapshot_path.is_file())
        self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o400)

    def test_restart_reconnects_checkout_planned_before_resource_registration(self):
        controller = self.controller()
        session = controller.request_engineering(
            engineering_patch(), request_id="restart-allocation-gap"
        )
        self.store.set_state(session.session_id, "preparing", changed_at=self.now)
        checkout_path = controller.engineering_checkout_root / session.run_id
        allocation = controller.engineering_store.plan_checkout(
            run_id=session.run_id,
            session_id=session.session_id,
            patch_id=session.patch_id,
            patchset=session.patchset,
            revision_sha=session.revision,
            repository_url="https://review.whamcloud.com/fs/lustre-release",
            base_branch="refs/changes/60/68160/4",
            checkout_path=checkout_path,
            owner_id=f"patch-watcher:{session.session_id}",
            now=self.now,
        )

        restarted = self.controller()

        resources = self.store.list_owned_resources(session_id=session.session_id)
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].resource_type, "engineering_checkout")
        self.assertEqual(resources[0].external_id, str(checkout_path))
        self.assertEqual(resources[0].metadata["allocation_id"], allocation.allocation_id)
        self.assertEqual(
            restarted.engineering_store.get_checkout(allocation.allocation_id).state,
            "planned",
        )

    def test_dispatch_allocates_writable_full_clone_and_source_edit_spec(self):
        checkouts = []

        def checkout(destination, revision):
            checkouts.append((Path(destination), revision))
            return self.fake_full_clone(destination, revision)

        controller = self.controller(checkout)
        requested = controller.request_engineering(engineering_patch())
        controller.tick()

        session = self.store.get_session(requested.session_id)
        self.assertEqual(session.state, "running")
        self.assertEqual(len(checkouts), 1)
        checkout_path, revision = checkouts[0]
        self.assertEqual(revision.revision_sha, DEFAULT_REVISION)
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        self.assertEqual(allocation.state, "active")
        self.assertEqual(allocation.checkout_kind, "full_clone")
        self.assertEqual(allocation.checkout_path, checkout_path)
        self.assertTrue((checkout_path / ".git").is_dir())
        self.assertTrue(checkout_path.stat().st_mode & 0o200)

        spec = self.runner.starts[0]
        self.assertEqual(Path(spec.cwd), checkout_path)
        self.assertEqual(spec.capability_profile, "source_edit_ltvm")
        self.assertEqual(spec.report_kind, "engineering")
        mcp_config = json.loads(spec.mcp_config_json)
        self.assertEqual(set(mcp_config["mcpServers"]), {"pw_ltvm"})
        context_path = Path(mcp_config["mcpServers"]["pw_ltvm"]["args"][-1])
        self.assertEqual(context_path.stat().st_mode & 0o777, 0o600)
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(context["owner_id"], allocation.owner_id)
        self.assertEqual(context["revision_sha"], DEFAULT_REVISION)
        capability = controller.engineering_store.get_active_validation_capability(
            session_id=session.session_id,
            run_id=session.run_id,
            revision_sha=session.revision,
        )
        self.assertIsNotNone(capability)
        resources = self.store.list_owned_resources(session_id=session.session_id)
        checkout_resource = next(
            resource for resource in resources
            if resource.resource_type == "engineering_checkout"
        )
        self.assertEqual(checkout_resource.owner_id, allocation.owner_id)
        envelope_path = next(
            (self.root / "runs" / session.run_id).rglob("run-envelope.json")
        )
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["checkout_mode"], "writable")
        self.assertIn("edit_source", envelope["capabilities"])
        self.assertNotIn("upload_patchset", envelope["capabilities"])

    def test_runner_start_failure_closes_open_guest_capability(self):
        controller = self.controller()
        requested = controller.request_engineering(engineering_patch())
        self.runner.start_error = RuntimeError("transport did not start")

        controller.tick()

        session = self.store.get_session(requested.session_id)
        execution = controller.engineering_store.get_validation_execution_by_run(
            session.run_id
        )
        attempts = controller.engineering_store.list_validation_attempts(
            execution.execution_id
        )
        self.assertEqual(attempts[0].state, "failed")
        self.assertEqual(attempts[0].failure_code, "runner_start_failed")
        self.assertNotEqual(session.state, "running")

    def test_unexpected_terminal_path_revokes_guest_capability_first(self):
        controller = self.controller()
        requested = controller.request_engineering(engineering_patch())
        controller.tick()
        running = self.store.get_session(requested.session_id)
        self.assertIsNotNone(
            controller.engineering_store.get_active_validation_capability(
                session_id=running.session_id,
                run_id=running.run_id,
                revision_sha=running.revision,
            )
        )
        self.runner.events_by_session[running.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "process_exit", {"returncode": 1}
        )]

        controller.tick()

        terminal = self.store.get_session(running.session_id)
        execution = controller.engineering_store.get_validation_execution_by_run(
            running.run_id
        )
        attempt = controller.engineering_store.list_validation_attempts(
            execution.execution_id
        )[0]
        self.assertEqual(terminal.state, "failed")
        self.assertEqual(execution.admission_state, "disabled")
        self.assertEqual(attempt.state, "failed")
        self.assertIsNone(
            controller.engineering_store.get_active_validation_capability(
                session_id=running.session_id,
                run_id=running.run_id,
                revision_sha=running.revision,
            )
        )

    def test_restart_makes_unadoptable_running_guest_attempt_ambiguous(self):
        controller = self.controller()
        requested = controller.request_engineering(engineering_patch())
        controller.tick()
        running = self.store.get_session(requested.session_id)
        execution = controller.engineering_store.get_validation_execution_by_run(
            running.run_id
        )
        self.runner.alive = False

        restarted = self.controller()

        attempt = restarted.engineering_store.list_validation_attempts(
            execution.execution_id
        )[0]
        self.assertEqual(attempt.state, "ambiguous")
        self.assertEqual(
            restarted.engineering_store.get_validation_execution(
                execution.execution_id
            ).state,
            "ambiguous",
        )
        self.assertIsNone(
            restarted.engineering_store.get_active_validation_capability(
                session_id=running.session_id,
                run_id=running.run_id,
                revision_sha=running.revision,
            )
        )
        restarted.tick()
        self.assertEqual(
            self.store.get_session(running.session_id).state, "failed"
        )
        self.assertEqual(
            restarted.engineering_store.get_validation_execution(
                execution.execution_id
            ).admission_state,
            "disabled",
        )

    def test_restart_retains_only_exact_adoptable_guest_attempt(self):
        controller = self.controller()
        requested = controller.request_engineering(engineering_patch())
        controller.tick()
        running = self.store.get_session(requested.session_id)

        restarted = self.controller()

        execution = restarted.engineering_store.get_active_validation_capability(
            session_id=running.session_id,
            run_id=running.run_id,
            revision_sha=running.revision,
        )
        self.assertIsNotNone(execution)
        attempt = restarted.engineering_store.list_validation_attempts(
            execution.execution_id
        )[0]
        self.assertEqual(attempt.state, "running")

    def test_new_patchset_revokes_running_guest_capability_immediately(self):
        controller = self.controller()
        requested = controller.request_engineering(engineering_patch())
        controller.tick()
        running = self.store.get_session(requested.session_id)

        stale = controller.reconcile_patch_revision(engineering_patch(
            revision="e" * 40,
            patchset=5,
            revision_ref="refs/changes/60/68160/5",
        ))

        execution = controller.engineering_store.get_validation_execution_by_run(
            running.run_id
        )
        attempt = controller.engineering_store.list_validation_attempts(
            execution.execution_id
        )[0]
        self.assertEqual(stale, [running.run_id])
        self.assertEqual(self.store.get_session(running.session_id).state, "stale")
        self.assertEqual(execution.admission_state, "disabled")
        self.assertEqual(attempt.state, "stale")
        self.assertIsNone(
            controller.engineering_store.get_active_validation_capability(
                session_id=running.session_id,
                run_id=running.run_id,
                revision_sha=running.revision,
            )
        )

    def test_terminal_report_captures_actual_tracked_and_untracked_diff_and_manifest(self):
        seed, revision = self.create_seed_repository()

        def checkout(destination, requested):
            self.assertEqual(requested.revision_sha, revision)
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(seed), str(destination)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
                check=True,
            )
            return Path(destination)

        controller = self.controller(checkout)
        patch = engineering_patch(revision=revision)
        session = controller.request_engineering(patch)
        controller.tick()
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        (allocation.checkout_path / "tracked.txt").write_text("after\n", encoding="utf-8")
        (allocation.checkout_path / "new.txt").write_text("new content\n", encoding="utf-8")
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1,
            self.now.timestamp(),
            "worker_report",
            {
                "schema": "patch-watcher-engineering-report/v1",
                "state": "complete",
                "summary": "Prepared a small fix for review.",
                "changed_files": ["tracked.txt", "new.txt"],
                "validation_requests": [
                    {
                        "name": "unit", "target": "ltvm", "argv": ["make", "test"],
                        "evidence_role": "test",
                    }
                ],
            },
        )]
        controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "succeeded")
        artifacts = controller.engineering_store.list_artifacts(session.run_id)
        self.assertEqual({artifact.kind for artifact in artifacts}, {"diff", "status"})
        diff_path = self.root / "runs" / "engineering-artifacts" / session.run_id / "proposed.patch"
        captured = diff_path.read_bytes()
        self.assertIn(b"+after", captured)
        self.assertIn(b"new file mode", captured)
        self.assertIn(b"+new content", captured)
        self.assertNotIn(str(self.root).encode(), captured)
        manifest = controller.engineering_store.get_manifest(session.run_id)
        self.assertEqual(manifest.commands[0].argv, ("make", "test"))
        self.assertEqual(manifest.commands[0].cwd, ".")
        self.assertEqual(manifest.commands[0].label, "unit")
        self.assertEqual(manifest.commands[0].execution_target, "ltvm")
        self.assertEqual(manifest.commands[0].evidence_role, "test")
        events = self.store.list_events(session.session_id)
        captured_event = next(
            event for event in events if event.event_type == "engineering_evidence_captured"
        )
        self.assertEqual(captured_event.payload["validation_request_count"], 1)
        self.assertEqual(captured_event.payload["diff_bytes"], len(captured))

    def test_review_terminal_report_maps_exact_comment_and_resolution_artifact(self):
        seed, revision = self.create_seed_repository()

        def checkout(destination, requested):
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(seed), str(destination)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
                check=True,
            )
            return Path(destination)

        controller = self.controller(checkout)
        patch = engineering_patch(revision=revision)
        snapshot = review_snapshot(revision)
        snapshot["change"]["revision_sha"] = revision
        session = controller.request_review_comments(
            patch, snapshot, mode="all", request_id="review-terminal",
        )
        controller.tick()
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        (allocation.checkout_path / "tracked.txt").write_text("after\n", encoding="utf-8")
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-engineering-report/v1",
                "state": "complete", "summary": "Addressed the review comment.",
                "changed_files": ["tracked.txt"], "validation_requests": [],
                "review_mode": "all", "review_snapshot_sha256": "a" * 64,
                "comment_results": [{
                    "comment_id": "comment-1", "assessment": "simple",
                    "disposition": "addressed",
                    "summary": "Updated the requested text.",
                    "reply_draft": "Addressed in the next patchset.",
                    "changed_files": ["tracked.txt"],
                }],
            },
        )]
        controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "succeeded")
        artifacts = controller.engineering_store.list_artifacts(session.run_id)
        self.assertEqual(
            {artifact.kind for artifact in artifacts},
            {"diff", "status", "review_resolution"},
        )
        plan = self.root / "runs" / "engineering-artifacts" / session.run_id / "review-resolution-plan.json"
        value = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(value["comment_results"][0]["comment_id"], "comment-1")
        self.assertEqual(value["review_mode"], "all")

    def test_build_terminal_report_maps_exact_failure_and_resolution_artifact(self):
        seed, revision = self.create_seed_repository()

        def checkout(destination, _requested):
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(seed), str(destination)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
                check=True,
            )
            return Path(destination)

        controller = self.controller(checkout)
        patch = engineering_patch(revision=revision)
        snapshot = build_snapshot(revision)
        snapshot["change"]["revision_sha"] = revision
        session = controller.request_build_failure(
            patch, snapshot, request_id="build-terminal",
        )
        controller.tick()
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        (allocation.checkout_path / "tracked.txt").write_text("after\n", encoding="utf-8")
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-engineering-report/v1",
                "state": "complete", "summary": "Fixed the Jenkins compile failure.",
                "changed_files": ["tracked.txt"], "validation_requests": [],
                "jenkins_snapshot_sha256": "b" * 64,
                "jenkins_resolution": {
                    "build_id": "lustre-reviews/123",
                    "classification": "patch_caused_fixed",
                    "diagnosis": "A missing declaration caused the compile failure.",
                },
            },
        )]
        controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "succeeded")
        artifacts = controller.engineering_store.list_artifacts(session.run_id)
        self.assertEqual(
            {artifact.kind for artifact in artifacts},
            {"diff", "status", "jenkins_resolution"},
        )
        plan = (
            self.root / "runs" / "engineering-artifacts" / session.run_id
            / "jenkins-resolution.json"
        )
        value = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(value["build_id"], "lustre-reviews/123")
        self.assertEqual(value["resolution"]["classification"], "patch_caused_fixed")

    def test_simple_review_rejects_nontrivial_comment_assessment(self):
        seed, revision = self.create_seed_repository()

        def checkout(destination, requested):
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(seed), str(destination)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
                check=True,
            )
            return Path(destination)

        controller = self.controller(checkout)
        patch = engineering_patch(revision=revision)
        snapshot = review_snapshot(revision)
        snapshot["change"]["revision_sha"] = revision
        session = controller.request_review_comments(
            patch, snapshot, mode="simple", request_id="review-nontrivial",
        )
        controller.tick()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-engineering-report/v1",
                "state": "complete", "summary": "Attempted a larger change.",
                "changed_files": [], "validation_requests": [],
                "review_mode": "simple", "review_snapshot_sha256": "a" * 64,
                "comment_results": [{
                    "comment_id": "comment-1", "assessment": "nontrivial",
                    "disposition": "addressed", "summary": "Too broad for simple mode.",
                    "reply_draft": "", "changed_files": [],
                }],
            },
        )]
        controller.tick()

        failed = self.store.get_session(session.session_id)
        self.assertEqual(failed.state, "failed")
        terminal = self.store.get_terminal_result(session.session_id)
        self.assertIn("simple mode", terminal.failure_summary)

    def test_cleanup_refuses_cross_run_path_and_releases_only_owned_checkout(self):
        controller = self.controller()
        first = controller.request_engineering(engineering_patch())
        second = controller.request_engineering(engineering_patch(
            change_number=68161,
            revision_ref="refs/changes/61/68161/4",
        ))
        controller.tick()
        first_allocation = controller.engineering_store.get_allocation_by_run(first.run_id)
        second_allocation = controller.engineering_store.get_allocation_by_run(second.run_id)
        rogue = self.store.register_owned_resource(
            first.session_id,
            owner_id=first_allocation.owner_id,
            resource_type="engineering_checkout",
            external_id=str(second_allocation.checkout_path),
            metadata={"run_id": first.run_id, "allocation_id": first_allocation.allocation_id},
            at=self.now,
        )
        self.store.finish_session(
            first.session_id,
            "cancelled",
            result={"reason": "operator_cancelled"},
            finished_at=self.now,
        )
        self.runner.alive = False
        controller._cleanup_session(self.store.get_session(first.session_id))

        self.assertFalse(first_allocation.checkout_path.exists())
        self.assertEqual(
            controller.engineering_store.get_allocation_by_run(first.run_id).state,
            "released",
        )
        self.assertTrue(second_allocation.checkout_path.exists())
        self.assertEqual(
            controller.engineering_store.get_allocation_by_run(second.run_id).state,
            "active",
        )
        rogue_after = next(
            resource for resource in self.store.list_owned_resources(session_id=first.session_id)
            if resource.resource_id == rogue.resource_id
        )
        self.assertEqual(rogue_after.state, "cleanup_failed")

    def test_partial_clone_failure_is_owned_and_cleaned_on_next_restart_pass(self):
        def fail_after_partial_write(destination, _revision):
            (Path(destination) / "partial-object").write_text("partial\n", encoding="utf-8")
            raise OSError("simulated clone interruption")

        controller = self.controller(fail_after_partial_write)
        session = controller.request_engineering(
            engineering_patch(), request_id="partial-clone"
        )
        controller.tick()
        self.assertEqual(self.store.get_session(session.session_id).state, "failed")
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        self.assertEqual(allocation.state, "planned")
        self.assertTrue(allocation.checkout_path.exists())
        resources = self.store.list_owned_resources(session_id=session.session_id)
        checkout_resource = next(
            resource for resource in resources
            if resource.resource_type == "engineering_checkout"
        )
        self.assertEqual(checkout_resource.state, "cleanup_pending")

        controller.tick()
        allocation = controller.engineering_store.get_allocation_by_run(session.run_id)
        self.assertEqual(allocation.state, "released")
        self.assertFalse(allocation.checkout_path.exists())

    def test_needs_input_checkpoint_does_not_consume_final_artifact_ids(self):
        controller = self.controller()
        session = controller.request_engineering(
            engineering_patch(), request_id="needs-input"
        )
        controller.tick()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1,
            self.now.timestamp(),
            "worker_report",
            {
                "schema": "patch-watcher-engineering-report/v1",
                "state": "needs_input",
                "summary": "A design choice is required before editing.",
                "changed_files": [],
                "validation_requests": [],
                "question": "Should compatibility behavior be preserved?",
            },
        )]
        controller.tick()

        self.assertEqual(
            self.store.get_session(session.session_id).state, "waiting_human"
        )
        self.assertEqual(controller.engineering_store.list_artifacts(session.run_id), ())
        self.assertIsNone(controller.engineering_store.get_manifest(session.run_id))


if __name__ == "__main__":
    unittest.main()
