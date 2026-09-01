import re
import unittest
from dataclasses import dataclass

import engineering_views


MIB = 1024 ** 2
GIB = 1024 ** 3


class EngineeringViewTests(unittest.TestCase):
    def sample_run(self, **changes):
        value = {
            "run_id": "eng-123",
            "version": 4,
            "state": "running",
            "subject": "LU-12345 repair race",
            "session_id": "session-123",
            "owner_id": "patch-watcher:session-123",
            "revision_sha": "a" * 40,
            "current_step": "Building",
            "started_at": "2026-09-01T12:00:00Z",
            "checkout": {
                "state": "ready",
                "remote": "https://user:secret@example.test/lustre.git?token=bad",
                "base_branch": "master",
                "revision_sha": "a" * 40,
                "logical_path": "/work/source",
                "dedicated": True,
                "initial_dirty": False,
                "cleanup_state": "not_started",
            },
            "manifest": {
                "schema_version": "engineering-manifest-v1",
                "digest": "sha256:manifest",
                "isolation_profile": "container-standard-v1",
                "network_profile": "restricted-egress",
                "build_steps": [{
                    "name": "Lustre build", "state": "succeeded",
                    "target": "owned VM", "command": "echo TOP_SECRET",
                    "environment": {"TOKEN": "never render this"},
                }],
                "test_steps": [{
                    "name": "sanity", "state": "running", "target": "vm-1",
                    "argv": ["malicious", "--secret"],
                }],
            },
            "artifacts": [{
                "artifact_id": "build/log 1", "name": "build.log",
                "state": "captured", "size_bytes": 2 * MIB,
                "digest": "sha256:build", "href": "javascript:alert(1)",
            }],
            "diffs": [{
                "artifact_id": "diff-1", "name": "proposed.patch",
                "state": "captured", "size_bytes": 1536,
                "digest": "sha256:diff", "url": "https://evil.test/",
            }],
            "test_results": [{
                "artifact_id": "test-1", "name": "sanity-dom",
                "outcome": "passed", "exit_status": 0,
                "duration_seconds": 61,
            }],
        }
        value.update(changes)
        return value

    def vm(self, **changes):
        value = {
            "name": "eng-123-co1-mds",
            "owner_id": "patch-watcher:session-123",
            "state": "running",
            "topology": "single / mds",
            "configured_guest_memory_bytes": 4 * GIB,
            "host_rss_bytes": 750 * MIB,
            "cleanup_state": "active",
        }
        value.update(changes)
        return value

    def validation_proposal(self, **changes):
        value = {
            "manifest": {
                "schema_version": 1,
                "manifest_id": "manifest-eng-123",
                "digest": "f" * 64,
                "revision_sha": "a" * 40,
                "commands": [{
                    "step_id": "validation-1",
                    "label": "Build and smoke test",
                    "argv": ["make", "check", "NAME=<unsafe>"],
                    "cwd": ".",
                    "timeout_seconds": 3661,
                    "execution_target": "rocky9-x86_64",
                    "env": {"TOKEN": "secret-must-not-render"},
                }],
            },
            "owner_id": "patch-watcher:session-123",
            "target": "rocky9-x86_64",
            "validation_eligible": True,
        }
        value.update(changes)
        return value

    def test_run_shows_checkout_revision_manifest_evidence_and_capability_boundary(self):
        rendered = engineering_views.render_engineering_run(self.sample_run())
        for expected in (
            "Isolated full checkout", "Exact pinned revision", "a" * 40,
            "Safe execution manifest summary", "engineering-manifest-v1",
            "Lustre build", "sanity-dom", "proposed.patch",
            "Source editing:</strong> active inside the isolated checkout",
            "Guest build/test execution:</strong> not verified active",
            "not approval of each individual command",
            "Host command execution:</strong> disabled",
            "Gerrit upload:</strong> disabled for this subphase",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("user:secret", rendered)
        self.assertNotIn("token=bad", rendered)

    def test_capability_banner_distinguishes_declared_active_and_expired(self):
        declared = engineering_views.render_engineering_dashboard([])
        self.assertIn(
            "declared; activated only after exact revision and owner binding are verified",
            declared,
        )
        active_run = self.sample_run(validation={
            **self.validation_proposal(),
            "state": "running",
            "approval_state": "approved",
            "validation_id": "attempt-active",
        })
        active = engineering_views.render_engineering_run(active_run)
        self.assertIn(
            "active as one open-ended audited capability, restricted to exact-owner",
            active,
        )
        expired = engineering_views.render_engineering_run(
            self.sample_run(state="failed", validation=active_run["validation"])
        )
        self.assertIn(
            "Guest build/test execution:</strong> expired; no guest command capability is active",
            expired,
        )
        self.assertIn("Effective guest capability</dt><dd>inactive", expired)

    def test_manifest_is_an_allowlisted_summary_not_a_shell_or_secret_dump(self):
        rendered = engineering_views.render_engineering_run(self.sample_run())
        for secret in ("TOP_SECRET", "never render this", "--secret"):
            self.assertNotIn(secret, rendered)
        self.assertIn("Raw commands, arguments, environment values, and secrets", rendered)

    def test_validation_proposal_is_exact_inert_and_not_a_per_command_allowlist(self):
        rendered = engineering_views.render_validation_proposal(
            self.sample_run(),
            self.validation_proposal(),
            base_url="/runs",
            csrf_token="csrf<&",
            idempotency_token="proposal-once",
        )
        for expected in (
            "Proposed session-owned LTVM validation",
            "No build or test has started",
            "manifest-eng-123",
            "f" * 64,
            "a" * 40,
            "patch-watcher:session-123",
            "rocky9-x86_64",
            "Build and smoke test",
            "Planned exec argv",
            "NAME=&lt;unsafe&gt;",
            "not constrain the approved session to only these commands",
            "Host execution and Gerrit upload remain disabled",
            "method='post' action='/runs/eng-123/validation/prepare'",
            "Review validation capability",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("secret-must-not-render", rendered)
        self.assertNotIn("/validation/execute'", rendered)
        self.assertNotRegex(rendered, r"(?i)method=['\"]get")
        self.assertNotIn("csrf<&", rendered)

    def test_validation_proposal_fails_closed_on_revision_owner_or_target_gap(self):
        cases = (
            self.validation_proposal(
                manifest={
                    **self.validation_proposal()["manifest"],
                    "revision_sha": "b" * 40,
                }
            ),
            self.validation_proposal(owner_id="patch-watcher:other"),
            self.validation_proposal(target=""),
            self.validation_proposal(validation_eligible=False, disabled_reason="policy denied"),
        )
        for proposal in cases:
            with self.subTest(proposal=proposal):
                rendered = engineering_views.render_validation_proposal(
                    self.sample_run(), proposal
                )
                self.assertIn("disabled aria-disabled='true'", rendered)
                self.assertIn("Validation approval unavailable", rendered)

    def test_validation_confirmation_repeats_boundary_and_only_final_post_executes(self):
        rendered = engineering_views.render_validation_confirmation(
            self.sample_run(),
            self.validation_proposal(),
            confirmation_token="signed<&",
            confirmation_expires_at="1788300000",
            csrf_token="csrf",
            idempotency_token="validation-once",
            base_url="/runs",
        )
        for expected in (
            "Confirm session-owned LTVM validation",
            "open-ended, brokered guest-command capability",
            "Immutable proposal being approved",
            "manifest-eng-123",
            "patch-watcher:session-123",
            "rocky9-x86_64",
            "Not granted:",
            "Gerrit upload remain disabled",
            "method='post' action='/runs/eng-123/validation/execute'",
            "name='confirmation_expires_at' value='1788300000'",
            "name='idempotency_token' value='validation-once'",
            "Approve validation capability",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("signed<&", rendered)
        self.assertNotRegex(rendered, r"(?i)method=['\"]get")

        retry = engineering_views.render_validation_confirmation(
            self.sample_run(), self.validation_proposal(),
            intent="retry", confirmation_token="retry-token", base_url="/runs",
        )
        self.assertIn("Confirm a new validation attempt", retry)
        self.assertIn("does not resume or rewrite", retry)
        self.assertIn("action='/runs/eng-123/validation/retry'", retry)
        with self.assertRaises(ValueError):
            engineering_views.render_validation_confirmation(
                self.sample_run(), self.validation_proposal(),
                confirmation_token="", base_url="/runs",
            )

    def test_validation_status_shows_guest_audit_results_capacity_and_cleanup(self):
        validation = {
            **self.validation_proposal(),
            "validation_id": "validation-attempt-7",
            "state": "resource_exhausted",
            "approval_state": "approved",
            "approved_by": "operator<&",
            "approved_at": "2026-09-01T15:00:00Z",
            "command_audits": [{
                "audit_id": "audit-1",
                "label": "Configure",
                "state": "succeeded",
                "guest": "eng-123-co1-mds",
                "owner_id": "patch-watcher:session-123",
                "argv": ["./configure", "--with-name=<unsafe>"],
                "started_at": "2026-09-01T15:01:00Z",
                "finished_at": "2026-09-01T15:02:01Z",
                "exit_code": 0,
                "duration_seconds": 61,
                "environment": {"SECRET": "do-not-render"},
            }],
            "results": [{
                "name": "smoke", "outcome": "passed", "exit_code": 0,
                "duration_seconds": 9, "artifact_id": "smoke-log",
            }],
            "artifacts": [{
                "artifact_id": "console/log", "name": "console.log",
                "state": "captured", "size_bytes": MIB,
                "sha256": "c" * 64,
                "href": "javascript:bad",
            }],
            "resource_exhaustion": {
                "error_code": "ltvm_resource_exhausted",
                "operation": "cluster create",
                "requested_resources": "4 guests",
                "evidence": "insufficient host memory",
            },
            "cooldown": {
                "state": "active",
                "retry_not_before": "2026-09-01T16:00:00Z",
                "remaining_seconds": 1800,
                "automation_suppressed": True,
                "exhaustion_count": 3,
            },
            "cleanup": {
                "state": "cleanup_failed",
                "error": "VM <stuck>",
                "owned_resources_remaining": 1,
            },
            "quarantine_state": "quarantined",
        }
        rendered = engineering_views.render_validation_status(
            self.sample_run(), validation, base_url="/runs"
        )
        for expected in (
            "Session-owned LTVM validation",
            "Validation: Resource exhausted",
            "validation-attempt-7",
            "manifest-eng-123",
            "patch-watcher:session-123",
            "rocky9-x86_64",
            "Guest command audit",
            "eng-123-co1-mds",
            "./configure",
            "--with-name=&lt;unsafe&gt;",
            "smoke",
            "/runs/eng-123/artifacts/smoke-log",
            "/runs/eng-123/artifacts/console%2Flog",
            "ltvm_resource_exhausted",
            "2026-09-01T16:00:00Z",
            "30m 0s",
            "Validation cleanup",
            "Cleanup failed",
            "VM &lt;stuck&gt;",
            "Quarantined",
            "Use the engineering run controls to review and confirm a new isolated run",
            "cannot silently retry itself",
            "Host execution and Gerrit upload are disabled",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("do-not-render", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotRegex(rendered, r"(?i)<form[^>]+method=['\"]get")

    def test_validation_status_fails_closed_on_identity_or_audit_owner_mismatch(self):
        validation = {
            **self.validation_proposal(owner_id="patch-watcher:wrong"),
            "revision_sha": "b" * 40,
            "state": "running",
            "approval_state": "approved",
            "validation_id": "attempt-untrusted",
            "command_audits": [{
                "step_id": "step-1", "state": "succeeded",
                "owner_id": "patch-watcher:wrong", "argv": ["true"],
            }],
        }
        rendered = engineering_views.render_validation_status(
            self.sample_run(), validation
        )
        self.assertIn(
            "Validation identity not verified; capability treated as inactive",
            rendered,
        )
        self.assertIn("revision matching the engineering run", rendered)
        self.assertIn("owner matching the engineering session", rendered)
        self.assertIn("Reported approval state", rendered)
        self.assertIn("Effective guest capability</dt><dd>inactive", rendered)
        self.assertIn("audit record is unverified", rendered)
        self.assertNotIn("one open-ended guest-command capability is brokered", rendered)

    def test_run_embeds_only_explicit_validation_proposal_or_status(self):
        proposal_run = self.sample_run(
            validation_proposal=self.validation_proposal()
        )
        proposal_html = engineering_views.render_engineering_run(
            proposal_run, base_url="/runs", csrf_token="csrf",
            idempotency_token="once",
        )
        self.assertIn("Proposed session-owned LTVM validation", proposal_html)
        self.assertIn("/runs/eng-123/validation/prepare", proposal_html)

        status_run = self.sample_run(validation_execution={
            **self.validation_proposal(),
            "validation_id": "attempt-1",
            "state": "running",
        })
        status_html = engineering_views.render_engineering_run(
            status_run, base_url="/runs"
        )
        self.assertIn("Session-owned LTVM validation", status_html)
        self.assertIn("attempt-1", status_html)
        self.assertNotIn("Review validation capability", status_html)

    def test_artifact_hrefs_are_internal_routes_derived_from_encoded_ids(self):
        rendered = engineering_views.render_engineering_run(self.sample_run())
        self.assertIn("/engineering-runs/eng-123/artifacts/build%2Flog%201", rendered)
        self.assertIn("/engineering-runs/eng-123/artifacts/diff-1", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("https://evil.test", rendered)

    def test_vms_are_nested_only_on_exact_owner_match_and_memory_is_separate(self):
        rendered = engineering_views.render_engineering_run(
            self.sample_run(),
            vms=[
                self.vm(),
                self.vm(name="similar", owner_id="prefix-patch-watcher:session-123"),
                self.vm(name="other", owner_id="patch-watcher:other"),
            ],
        )
        self.assertIn("Session-owned LTVM guests (1)", rendered)
        self.assertIn("eng-123-co1-mds", rendered)
        self.assertNotIn(">similar<", rendered)
        self.assertNotIn(">other<", rendered)
        self.assertIn("Configured guest memory", rendered)
        self.assertIn("Actual host RSS", rendered)
        self.assertIn("4 GiB", rendered)
        self.assertIn("750 MiB", rendered)
        self.assertNotIn("4.7 GiB", rendered)

    def test_resource_exhaustion_cooldown_and_no_retry_loop_are_visible(self):
        run = self.sample_run(
            state="resource_exhausted",
            resource_exhaustion={
                "error_code": "ltvm_resource_exhausted",
                "operation": "cluster create",
                "requested_resources": "3 nodes, 2 GiB each",
                "evidence": "host has insufficient memory <unsafe>",
            },
            cooldown={
                "state": "active", "retry_not_before": "2026-09-01T14:00:00Z",
                "remaining_seconds": 3599, "automation_suppressed": True,
                "exhaustion_count": 2,
            },
        )
        rendered = engineering_views.render_engineering_run(run)
        for expected in (
            "Resource exhaustion", "ltvm_resource_exhausted", "cluster create",
            "3 nodes, 2 GiB each", "No automatic retry is performed.",
            "2026-09-01T14:00:00Z", "59m 59s",
            "Automatic VM-backed runs suppressed</dt><dd>yes",
            "Retry now as a new run",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("<unsafe>", rendered)

    def test_cleanup_quarantine_and_orphan_warnings_are_prominent_and_escaped(self):
        run = self.sample_run(
            checkout={
                "revision_sha": "a" * 40,
                "cleanup_state": "cleanup_failed",
            },
            quarantine={"state": "quarantined", "reason": "bad <artifact>"},
            warnings=["operator <check>"],
        )
        vm = self.vm(cleanup_state="orphaned", name="vm<&")
        rendered = engineering_views.render_engineering_run(run, vms=[vm])
        for expected in (
            "Cleanup, quarantine, or orphan warning",
            "Checkout cleanup requires operator attention",
            "Quarantined run resource: bad &lt;artifact&gt;",
            "vm&lt;&amp; is orphaned", "operator &lt;check&gt;",
        ):
            self.assertIn(expected, rendered)

    def test_dashboard_keeps_unmatched_vms_outside_runs(self):
        rendered = engineering_views.render_engineering_dashboard(
            [self.sample_run()],
            vms=[self.vm(), self.vm(name="legacy", owner_id=None),
                 self.vm(name="orphan", owner_id="patch-watcher:gone")],
        )
        card = rendered.split("<article class='engineering-run'", 1)[1]
        self.assertIn("eng-123-co1-mds", card)
        self.assertNotIn(">legacy<", card)
        self.assertNotIn(">orphan<", card)
        orphan_section = rendered.split("<section class='orphan-vms'", 1)[1].split("</section>", 1)[0]
        self.assertIn("legacy", orphan_section)
        self.assertIn("orphan", orphan_section)
        self.assertIn("not adopted or made mutable", orphan_section)

    def test_operator_message_and_prod_are_explicit_post_buttons(self):
        rendered = engineering_views.render_engineering_run(
            self.sample_run(), csrf_token="csrf<&", idempotency_token="once'bad",
        )
        self.assertIn("method='post' action='/engineering-runs/eng-123/guidance'", rendered)
        self.assertIn("name='message' required", rendered)
        self.assertIn("value='safe_boundary'>Send message", rendered)
        self.assertIn("value='interrupt_and_send'>Prod now", rendered)
        self.assertIn("next safe turn boundary", rendered)
        self.assertNotIn("csrf<&", rendered)

    def test_cancel_kill_and_retry_detail_controls_only_open_confirmation(self):
        active = engineering_views.render_engineering_run(self.sample_run())
        self.assertIn("/confirm?intent=cancel", active)
        self.assertIn("/confirm?intent=kill", active)
        self.assertNotIn("action='/engineering-runs/eng-123/cancel'", active)
        self.assertNotIn("action='/engineering-runs/eng-123/kill'", active)
        terminal = engineering_views.render_engineering_run(self.sample_run(state="failed"))
        self.assertIn("/confirm?intent=retry", terminal)
        self.assertNotIn("action='/engineering-runs/eng-123/retry'", terminal)
        self.assertNotRegex(active + terminal, r"(?i)<form[^>]+method=['\"]get")

    def test_final_control_confirmation_is_token_bound_post(self):
        for intent in ("cancel", "kill", "retry"):
            with self.subTest(intent=intent):
                rendered = engineering_views.render_engineering_confirmation(
                    self.sample_run(), intent, confirmation_token="signed", csrf_token="csrf",
                )
                self.assertIn(
                    f"method='post' action='/engineering-runs/eng-123/{intent}'",
                    rendered,
                )
                self.assertIn("name='confirmation_token' value='signed'", rendered)
                self.assertIn("name='expected_version' value='4'", rendered)
                self.assertNotIn("method='get'", rendered.casefold())
        with self.assertRaises(ValueError):
            engineering_views.render_engineering_confirmation(
                self.sample_run(), "delete", confirmation_token="signed")
        with self.assertRaises(ValueError):
            engineering_views.render_engineering_confirmation(
                self.sample_run(), "kill", confirmation_token="")

    def test_start_flow_is_prepare_post_then_token_bound_final_post(self):
        patch = {
            "change_number": 68160, "patchset": 13,
            "revision_sha": "b" * 40, "engineering_eligible": True,
        }
        control = engineering_views.render_engineering_start_control(
            patch, csrf_token="csrf", idempotency_token="prepare-once")
        self.assertIn("method='post' action='/engineering-runs/prepare'", control)
        self.assertIn("Prepare engineering run", control)
        self.assertIn("display-only confirmation page", control)
        self.assertIn("b" * 40, control)
        self.assertNotIn("action='/engineering-runs/start'", control)

        confirmation = engineering_views.render_engineering_start_confirmation(
            patch, confirmation_token="signed-start", csrf_token="csrf",
            idempotency_token="start-once")
        self.assertIn("method='post' action='/engineering-runs/start'", confirmation)
        self.assertIn("name='confirmation_token' value='signed-start'", confirmation)
        self.assertIn("name='revision_sha' value='" + "b" * 40, confirmation)
        self.assertIn("Gerrit upload:</strong> disabled for this subphase", confirmation)
        self.assertNotIn("method='get'", confirmation.casefold())

    def test_start_controls_disable_without_eligibility_or_exact_revision(self):
        for patch in (
            {"revision_sha": "c" * 40, "engineering_eligible": False},
            {"engineering_eligible": True},
        ):
            with self.subTest(patch=patch):
                rendered = engineering_views.render_engineering_start_control(patch)
                self.assertIn("disabled aria-disabled='true'", rendered)
        with self.assertRaises(ValueError):
            engineering_views.render_engineering_start_confirmation(
                {"change_number": 1}, confirmation_token="signed")

    def test_dataclass_input_dynamic_text_and_route_segments_are_safe(self):
        @dataclass
        class Checkout:
            state: str = "ready<script>"
            revision_sha: str = "<&>"
            dedicated: bool = True
            initial_dirty: bool = False

        @dataclass
        class Run:
            run_id: str = "../x y/?"
            state: str = "waiting_human"
            subject: str = "<img src=x>"
            revision_sha: str = "<&>"
            owner_id: str = "patch-watcher:<owner>"
            checkout: Checkout = None

            def __post_init__(self):
                self.checkout = Checkout()

        rendered = engineering_views.render_engineering_run(
            Run(),
            base_url="javascript:alert(1)",
            messages=[{"author": "<admin>", "body": "<b>unsafe</b>"}],
        )
        for unsafe in ("<script>", "<img", "<admin>", "<b>", "javascript:"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("/engineering-runs/..%2Fx%20y%2F%3F", rendered)
        hrefs = re.findall(r"href='([^']+)'", rendered)
        self.assertTrue(hrefs)
        self.assertTrue(all(href.startswith("/engineering-runs/") for href in hrefs))


if __name__ == "__main__":
    unittest.main()
