import re
import unittest
from dataclasses import dataclass

from patch_watcher import engineering_views

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
        """One guest exactly as ``LTVMVMStatus.to_dict()`` produces it.

        This is the whole record the sampler can supply: `ltvm list --json`
        has no topology, role, age, CPU share, or cleanup field, and stamps no
        patch-watcher owner id.
        """
        value = {
            "name": "eng-123-co1-mds",
            "owner_id": "patch-watcher:session-123",
            "patch_watcher_session_id": "session-123",
            "state": "running",
            "configured_guest_memory_bytes": 4 * GIB,
            "host_rss_bytes": 750 * MIB,
            "process_id": 4242,
            "vcpus": 2,
            "ip": "192.168.100.11",
            "host_memory_source": "/proc/4242/status VmRSS",
            "quality": "good",
            "errors": [],
        }
        value.update(changes)
        return value

    def validation_record(self, **changes):
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
            "Host command execution:</strong> available; the run has a host shell",
            "Gerrit upload:</strong> available with real credentials",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("user:secret", rendered)
        self.assertNotIn("token=bad", rendered)

    def test_a_read_only_run_claims_no_host_shell_and_no_credentials(self):
        """The run card sat on its own page and could say what it liked.  It
        now renders under `render_run_detail`'s boundary statement, which
        follows the capability profile, so an unconditional "host shell, real
        credentials" made one page contradict itself."""

        restricted = engineering_views.render_engineering_run(
            self.sample_run(capability_profile="read_only")
        )
        self.assertIn("Host command execution:</strong> restricted", restricted)
        self.assertIn("Gerrit upload:</strong> unavailable", restricted)
        self.assertNotIn("available with real credentials", restricted)

        full = engineering_views.render_engineering_run(
            self.sample_run(capability_profile="full")
        )
        self.assertIn(
            "Host command execution:</strong> available; the run has a host shell",
            full,
        )
        self.assertIn("Gerrit upload:</strong> available with real credentials", full)

    def test_capability_banner_distinguishes_declared_active_and_expired(self):
        declared = engineering_views.render_capability_status()
        self.assertIn(
            "declared; activated only after exact revision and owner binding are verified",
            declared,
        )
        # A confirmation page states plainly what is about to be authorised.
        self.assertIn("available with real credentials", declared)
        # The index card lists runs rather than authorising one, so there the
        # same capabilities are declarations, not a grant that is live now.
        standing = engineering_views.render_capability_status(standing=True)
        self.assertNotIn("available with real credentials", standing)
        self.assertNotIn("available; the run has a host shell", standing)
        self.assertIn("an engineering run carries real service credentials", standing)
        active_run = self.sample_run(validation={
            **self.validation_record(),
            "state": "running",
            "approval_state": "approved",
            "validation_id": "attempt-active",
        })
        active = engineering_views.render_engineering_run(active_run)
        self.assertIn(
            "active as one open-ended capability, recorded for exact-owner",
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

    def test_validation_status_shows_identity_artifacts_capacity_and_cleanup(self):
        validation = {
            **self.validation_record(),
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
            "The run's host shell and service credentials are unaffected by that",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("do-not-render", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotRegex(rendered, r"(?i)<form[^>]+method=['\"]get")

    def test_validation_status_fails_closed_on_identity_mismatch(self):
        validation = {
            **self.validation_record(owner_id="patch-watcher:wrong"),
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
        self.assertNotIn("one open-ended guest-command capability is recorded", rendered)

    def test_run_embeds_validation_status_only_when_the_run_carries_one(self):
        without = engineering_views.render_engineering_run(
            self.sample_run(), base_url="/runs", csrf_token="csrf",
            idempotency_token="once",
        )
        self.assertNotIn("Session-owned LTVM validation", without)

        status_run = self.sample_run(validation_execution={
            **self.validation_record(),
            "validation_id": "attempt-1",
            "state": "running",
        })
        status_html = engineering_views.render_engineering_run(
            status_run, base_url="/runs"
        )
        self.assertIn("Session-owned LTVM validation", status_html)
        self.assertIn("attempt-1", status_html)

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

    def test_the_run_page_claims_no_isolation_it_does_not_have(self):
        """Two hardcoded constants asserted a mediation layer that was removed.

        The manifest panel rendered "Isolation profile: session-owned-ltvm"
        and "Network profile: controller-mediated" from string literals in the
        projection, not from anything measured. An engineering run has neither:
        it runs on this host under bypassPermissions with the ambient
        environment and the operator's real credentials. A label the operator
        reads as a boundary has to correspond to one.
        """

        run = self.sample_run(
            manifest={
                "schema_version": "safe-execution-manifest/v1",
                "digest": "sha256:" + "a" * 64,
                "build_steps": [],
                "test_steps": [],
            },
        )
        rendered = engineering_views.render_engineering_run(run)

        self.assertIn("Manifest digest", rendered)
        for claim in (
            "Isolation profile", "Network profile",
            "session-owned-ltvm", "controller-mediated",
        ):
            self.assertNotIn(claim, rendered)

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
            # A deadline and a countdown read as though there were a control
            # to shorten them. There is not -- the retry-grant machinery that
            # would have overridden a cooldown had no issuer -- so the panel
            # says so rather than leaving the operator hunting for it.
            "There is no override",
            "free VM slots, disk, or memory",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("<unsafe>", rendered)

    def test_cleanup_quarantine_and_orphan_warnings_are_prominent_and_escaped(self):
        """Warn on the states the producers can actually reach.

        ``pw_checkout_allocation.state`` is constrained to planned, allocated,
        active, cleanup_pending, released and quarantined, and a guest's
        cleanup state lives on its ``pw_owned_resource`` row, never on the
        LTVM sample.
        """
        run = self.sample_run(
            checkout={
                "revision_sha": "a" * 40,
                "cleanup_state": "quarantined",
            },
            vm_prefix="co3-",
            owned_resources=[{
                "resource_type": "ltvm_vm",
                "external_id": "co3-vm<&",
                "state": "cleanup_failed",
                "cleanup_failure": "ltvm destroy <timed out>",
            }],
            quarantine={"state": "quarantined", "reason": "bad <artifact>"},
            warnings=["operator <check>"],
        )
        vm = self.vm(name="co3-vm<&", owner_id=None)
        rendered = engineering_views.render_engineering_run(run, vms=[vm])
        for expected in (
            "Cleanup, quarantine, or orphan warning",
            "Checkout is quarantined",
            "Quarantined run resource: bad &lt;artifact&gt;",
            "co3-vm&lt;&amp; is still in the LTVM inventory",
            "operator &lt;check&gt;",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("vm<&", rendered)

    def test_guests_are_nested_by_the_checkouts_reserved_name_prefix(self):
        """A run's guests carry no patch-watcher owner id; the prefix finds them.

        ``ltvm list --json`` has no owner field for Patch Watcher to stamp, so
        every sampled guest arrives with ``owner_id`` of None (or LTVM's own
        ``pid:<n>``). Matching on the run's owner id therefore nested nothing
        and flagged the run's own guest as an orphan.
        """
        run = self.sample_run(checkout_index=3)
        mine = self.vm(name="co3-sanity", owner_id=None)
        ltvm_owned = self.vm(name="co3-build", owner_id="pid:2520851")
        neighbour = self.vm(name="co31-sanity", owner_id=None)
        elsewhere = self.vm(name="co4-sanity", owner_id=None)
        stray = self.vm(name="co5-stray", owner_id="patch-watcher:gone")
        vms = [mine, ltvm_owned, neighbour, elsewhere, stray]
        card = engineering_views.render_engineering_run(run, vms=vms)
        orphans = engineering_views.render_unmatched_resources([run], vms)
        self.assertIn("Session-owned LTVM guests (1)", card)
        self.assertIn(">co3-sanity<", card)
        # A trailing dash is the whole point: co31 belongs to checkout 31.
        self.assertNotIn(">co31-sanity<", card)
        self.assertNotIn(">co4-sanity<", card)
        # A guest declaring some other owner is never claimed by name alone.
        self.assertNotIn(">co3-build<", card)
        self.assertNotIn("co3-sanity", orphans)
        # Only a guest claiming a Patch Watcher owner is our orphan. The rest
        # belong to whoever made them and are listed, calmly, by the resource
        # card; alerting on them here just duplicated that list in red.
        self.assertIn("co5-stray", orphans)
        for stranger in ("co3-build", "co31-sanity", "co4-sanity"):
            self.assertNotIn(stranger, orphans)

    def test_run_without_a_pool_checkout_claims_no_guest(self):
        """No checkout means no reserved prefix, so the run owns nothing."""
        run = self.sample_run(owner_id="")
        rendered = engineering_views.render_engineering_run(
            run, vms=[self.vm(name="co3-sanity", owner_id=None)]
        )
        self.assertIn("Session-owned LTVM guests (0)", rendered)
        self.assertIn("reserves no guest name prefix", rendered)

    def test_guest_table_renders_only_fields_the_sampler_produces(self):
        """No column may be structurally incapable of carrying a value."""
        run = self.sample_run(
            checkout_index=3,
            owned_resources=[{
                "resource_type": "ltvm_vm",
                "external_id": "co3-sanity",
                "state": "cleanup_pending",
            }],
        )
        rendered = engineering_views._render_vms(
            run, supplied_vms=[self.vm(name="co3-sanity", owner_id=None)],
            suffix="s",
        )
        body = rendered.split("<tbody>", 1)[1]
        for expected in (
            ">2<", ">192.168.100.11<", "4 GiB", "750 MiB",
            "/proc/4242/status VmRSS", ">Cleanup pending<",
        ):
            self.assertIn(expected, body)
        self.assertNotIn("Topology / role", rendered)
        self.assertNotIn(">unknown<", body)
        self.assertNotIn(">Unknown<", body)

    def test_quarantined_checkout_is_the_state_that_warns(self):
        """quarantined is the only allocation state needing an operator."""
        warned = engineering_views.render_engineering_run(
            self.sample_run(checkout={"cleanup_state": "quarantined"})
        )
        self.assertIn("Checkout is quarantined", warned)
        self.assertIn("needs operator review", warned)
        for benign in ("planned", "allocated", "active", "cleanup_pending",
                       "released"):
            quiet = engineering_views.render_engineering_run(
                self.sample_run(checkout={"cleanup_state": benign})
            )
            self.assertIn(
                "Cleanup, quarantine, and orphan warnings: none reported", quiet
            )

    def test_unmatched_vms_are_reported_outside_the_run_card(self):
        run = self.sample_run()
        vms = [self.vm(), self.vm(name="legacy", owner_id=None),
               self.vm(name="orphan", owner_id="patch-watcher:gone")]
        card = engineering_views.render_engineering_run(run, vms=vms)
        orphan_section = engineering_views.render_unmatched_resources([run], vms)
        self.assertIn("eng-123-co1-mds", card)
        self.assertNotIn(">legacy<", card)
        self.assertNotIn(">orphan<", card)
        # "orphan" declares a Patch Watcher owner with no live run, so it is
        # ours to chase; "legacy" declares no owner at all and is not.
        self.assertIn("orphan", orphan_section)
        self.assertNotIn("legacy", orphan_section)
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

    def test_terminal_run_offers_no_message_or_prod_control(self):
        """Clicking either on a terminal run only produced a controller error
        page saying "cannot guide terminal session"."""

        rendered = engineering_views.render_engineering_run(
            self.sample_run(state="failed"), csrf_token="csrf",
        )
        self.assertNotIn(">Send message<", rendered)
        self.assertNotIn(">Prod now<", rendered)
        self.assertNotIn("action='/engineering-runs/eng-123/guidance'", rendered)
        self.assertIn("no live worker to message or prod", rendered)

    def test_recorded_resource_cleanup_failure_is_warned_with_its_reason(self):
        """Warnings were derived only from the live LTVM inventory, so a
        resource row marked cleanup_failed reported "none reported"."""

        rendered = engineering_views.render_engineering_run(self.sample_run(
            state="failed",
            owned_resources=[{
                "resource_id": "resource-1",
                "resource_type": "ltvm_vm",
                "external_id": "eng-123-co1-oss",
                "owner_id": "patch-watcher:session-123",
                "state": "cleanup_failed",
                "cleanup_failure": "ltvm destroy timed out after 3 attempts",
            }],
        ))
        self.assertNotIn(
            "Cleanup, quarantine, and orphan warnings: none reported", rendered
        )
        self.assertIn("Cleanup, quarantine, or orphan warning", rendered)
        self.assertIn("eng-123-co1-oss", rendered)
        self.assertIn("ltvm destroy timed out after 3 attempts", rendered)

    def test_clean_recorded_resources_still_report_no_warning(self):
        rendered = engineering_views.render_engineering_run(self.sample_run(
            owned_resources=[{
                "resource_id": "resource-1",
                "resource_type": "ltvm_vm",
                "external_id": "eng-123-co1-mds",
                "state": "cleaned",
            }],
        ))
        self.assertIn(
            "Cleanup, quarantine, and orphan warnings: none reported", rendered
        )

    def test_abandoned_resource_absent_from_inventory_is_still_an_orphan(self):
        """`ltvm list` only knows about guests that still exist, so an
        abandoned cleanup vanished from the orphan list entirely."""

        orphan_section = engineering_views.render_unmatched_resources(
            [self.sample_run(owned_resources=[{
                "resource_id": "resource-9",
                "resource_type": "ltvm_cluster",
                "external_id": "eng-123-cluster",
                "owner_id": "patch-watcher:session-123",
                "state": "cleanup_failed",
                "cleanup_failure": "cleanup abandoned; ltvm never returned",
            }])],
            [self.vm()],
        )
        self.assertNotIn(
            "Unmatched or orphan LTVM resources: none reported", orphan_section
        )
        self.assertIn("eng-123-cluster", orphan_section)
        self.assertIn("cleanup abandoned; ltvm never returned", orphan_section)

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
        self.assertIn("Gerrit upload:</strong> available with real credentials", confirmation)
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

    def test_compact_start_preserves_prepare_post_without_capability_essay(self):
        patch = {
            "change_number": 68160,
            "patchset": 13,
            "revision_sha": "b" * 40,
            "engineering_eligible": True,
        }
        rendered = engineering_views.render_engineering_start_control(
            patch, csrf_token="csrf", idempotency_token="once", compact=True,
        )
        self.assertIn("class='quick-action'", rendered)
        self.assertIn("action='/engineering-runs/prepare'", rendered)
        self.assertIn("name='revision_sha'", rendered)
        self.assertIn("Engineering run", rendered)
        self.assertNotIn("Capability status", rendered)

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
