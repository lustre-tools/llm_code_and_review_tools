import re
import unittest
from dataclasses import dataclass, field

import worker_admission_views


class WorkerAdmissionViewTests(unittest.TestCase):
    def test_missing_data_is_unknown_not_zero_not_ready(self):
        rendered = worker_admission_views.render_worker_admission()
        self.assertIn("Admission: Unknown", rendered)
        self.assertIn("Worker profile</dt><dd>unknown", rendered)
        self.assertIn("Profile hash</dt><dd><code>unknown", rendered)
        self.assertIn("Tool resolution is unknown.", rendered)
        self.assertIn("Warning collection is unknown.", rendered)
        self.assertIn("Preflight failure collection is unknown.", rendered)
        self.assertNotIn("Admission: Ready", rendered)
        self.assertNotIn("none reported", rendered)

    def test_not_checked_is_explicit_and_distinct_from_missing_status(self):
        rendered = worker_admission_views.render_worker_admission(
            {"admission_status": "not_checked"}
        )
        self.assertIn("Admission: Not checked", rendered)
        self.assertNotIn("Admission: Unknown", rendered)

    def test_all_supported_admission_states_include_text_and_tone(self):
        expectations = {
            "not_checked": ("Not checked", "neutral"),
            "checking": ("Checking", "info"),
            "ready": ("Ready", "good"),
            "degraded": ("Degraded", "warn"),
            "blocked": ("Blocked", "bad"),
        }
        for state, (label, tone) in expectations.items():
            with self.subTest(state=state):
                rendered = worker_admission_views.render_worker_admission(
                    {"status": state}
                )
                self.assertIn(f"Admission: {label}", rendered)
                self.assertIn(f"tone-{tone}", rendered)

    def test_ready_with_explicit_empty_collections_preserves_real_zero(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "ready",
            "tools": [],
            "warnings": [],
            "preflight_failures": [],
        })
        self.assertIn("Admission: Ready", rendered)
        self.assertIn("Resolved tools (0)", rendered)
        self.assertIn("Resolved tools: 0 (none reported).", rendered)
        self.assertIn("Warnings (0)", rendered)
        self.assertIn("Warnings: 0 (none reported).", rendered)
        self.assertIn("Failed preflight checks (0)", rendered)
        self.assertIn("Failed preflight checks: 0 (none reported).", rendered)

    def test_unsandboxed_and_general_network_boundaries_are_truthful(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "ready",
            "isolation_profile": "host-unsandboxed-mac-v1",
            "network_profile": "network_general",
        })
        self.assertIn("Attested isolation: Unsandboxed host worker", rendered)
        self.assertIn("tone-bad", rendered)
        self.assertIn("Attested network: General network access", rendered)
        self.assertNotIn("Sandboxed worker", rendered)
        self.assertNotIn("No network access", rendered)

    def test_offline_tools_badge_does_not_overclaim_full_network_denial(self):
        rendered = worker_admission_views.render_worker_admission({
            "isolation_profile": "container-standard-v1",
            "network_profile": "container-offline-tools",
        })
        self.assertIn("Attested isolation: Container standard v1", rendered)
        self.assertIn("Attested network: Worker-tool network disabled", rendered)
        self.assertNotIn("fully offline", rendered.casefold())
        self.assertNotIn("all network disabled", rendered.casefold())

    def test_host_ambient_network_is_labeled_as_general_access(self):
        rendered = worker_admission_views.render_worker_admission(
            {"status": "not_checked"},
            profile={"network_profile": "host_ambient"},
        )
        self.assertIn(
            "Declared only network: General network access",
            rendered,
        )

    def test_profile_only_boundaries_are_labelled_declared_not_attested(self):
        rendered = worker_admission_views.render_worker_admission(
            {"status": "not_checked"},
            profile={
                "profile_id": "triage",
                "isolation_profile": "unsandboxed",
                "network_profile": "general",
            },
        )
        self.assertIn("Declared only isolation: Unsandboxed host worker", rendered)
        self.assertIn("Declared only network: General network access", rendered)
        self.assertNotIn("Attested isolation", rendered)

    def test_attestation_wins_and_profile_boundary_mismatch_is_visible(self):
        rendered = worker_admission_views.render_worker_admission(
            {"status": "degraded"},
            profile={
                "isolation_profile": "container-standard-v1",
                "network_profile": "restricted",
            },
            attestation={
                "isolation_profile": "unsandboxed",
                "network_profile": "general",
            },
        )
        self.assertIn("Attested isolation: Unsandboxed host worker", rendered)
        self.assertIn("Attested network: General network access", rendered)
        self.assertIn("Declaration/attestation differences", rendered)
        self.assertIn("Profile-declared isolation: Container standard v1", rendered)
        self.assertIn("Profile-declared network: Restricted", rendered)

    def test_all_dynamic_profile_tool_warning_and_failure_text_is_escaped(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "blocked<script>",
            "profile_id": "profile<&>",
            "profile_version": "v'\"<1>",
            "profile_hash": "hash<&>",
            "environment_id": "env<script>",
            "host_id": "host&<one>",
            "attested_at": "time<&>",
            "isolation": "custom<isolation>",
            "network": "custom<network>",
            "tools": [{
                "name": "tool<script>",
                "status": "bad<state>",
                "version": "1<&>",
                "path": "/tmp/<tool>",
                "required": True,
            }],
            "warnings": [{"code": "warn<x>", "message": "look & <here>"}],
            "failures": [{
                "code": "tool_missing<x>",
                "message": "missing <binary>",
                "check": "runtime<&>",
                "tool": "rg<script>",
                "expected": ">=1<&",
                "actual": "none<script>",
            }],
        })
        for unsafe in (
            "<script>", "<1>", "<isolation>", "<network>", "<tool>",
            "<here>", "<binary>", "<x>",
        ):
            self.assertNotIn(unsafe, rendered)
        for escaped in (
            "profile&lt;&amp;&gt;", "env&lt;script&gt;", "host&amp;&lt;one&gt;",
            "tool&lt;script&gt;", "/tmp/&lt;tool&gt;", "look &amp; &lt;here&gt;",
            "missing &lt;binary&gt;", "rg&lt;script&gt;",
        ):
            self.assertIn(escaped, rendered)

    def test_mapping_tools_render_status_version_path_and_requirement(self):
        rendered = worker_admission_views.render_worker_admission({
            "resolved_tools": {
                "python": {
                    "available": True,
                    "version": "3.12.13",
                    "resolved_path": "/opt/pw/bin/python",
                    "required": True,
                },
                "ltvm": {
                    "available": False,
                    "version": None,
                    "path": None,
                    "required": False,
                },
            },
        })
        self.assertIn("Resolved tools (2)", rendered)
        self.assertIn(">python<", rendered)
        self.assertIn("Available", rendered)
        self.assertIn("3.12.13", rendered)
        self.assertIn("/opt/pw/bin/python", rendered)
        self.assertIn("Required", rendered)
        self.assertIn(">ltvm<", rendered)
        self.assertIn("Unavailable", rendered)
        self.assertIn("Optional", rendered)

    def test_failure_reasons_preserve_precise_code_and_evidence(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "blocked",
            "preflight_failures": [{
                "code": "tool_version_mismatch",
                "message": "Python does not satisfy the profile constraint",
                "check_name": "runtime_versions",
                "tool_name": "python",
                "expected": ">=3.12,<3.13",
                "actual": "3.9.6",
            }],
        })
        self.assertIn("Admission: Blocked", rendered)
        self.assertIn("tool_version_mismatch", rendered)
        self.assertIn("Python does not satisfy", rendered)
        self.assertIn("runtime_versions", rendered)
        self.assertIn("python", rendered)
        self.assertIn("&gt;=3.12,&lt;3.13", rendered)
        self.assertIn("3.9.6", rendered)

    def test_blocked_without_failure_collection_does_not_invent_reason(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "blocked",
        })
        self.assertIn("Admission is blocked, but precise failure details are unavailable", rendered)
        self.assertNotIn("tool_missing", rendered)

    def test_blocked_with_explicit_empty_failure_list_reports_missing_reason(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "blocked",
            "failure_codes": [],
        })
        self.assertIn("Failed preflight checks: 0 reported", rendered)
        self.assertIn("blocked reason is unavailable", rendered)
        self.assertNotIn("none reported", rendered)

    def test_canonical_attestation_projection_fields_are_supported(self):
        attestation = {
            "status": "blocked",
            "admitted": False,
            "created_at": "2026-08-30T14:00:00Z",
            "worker_host": {
                "host_id": "host-01",
                "host_build_id": "build-77",
                "image_digest": "",
            },
            "worker_profile_id": "host-unsandboxed-mac-v1",
            "worker_profile_hash": "sha256:profile",
            "isolation_mode": "host-unsandboxed-mac-v1",
            "network_mode": "network_general",
            "executables": [{
                "name": "python",
                "path": "/usr/bin/python3",
                "version": "3.9.6",
                "required": True,
                "available": True,
                "api_version": "",
            }],
            "warnings": ["legacy_host"],
            "deviations": ["python_version_drift"],
            "unavailable_optional_capabilities": ["ltvm"],
            "failure_codes": ["tool_version_mismatch"],
        }
        rendered = worker_admission_views.render_worker_admission(attestation)
        self.assertIn("Admission: Blocked", rendered)
        self.assertIn("host-unsandboxed-mac-v1", rendered)
        self.assertIn("sha256:profile", rendered)
        self.assertIn("host-01", rendered)
        self.assertIn("build-77", rendered)
        self.assertIn("2026-08-30T14:00:00Z", rendered)
        self.assertIn("Resolved tools (1)", rendered)
        self.assertIn("Warnings (3)", rendered)
        self.assertIn("legacy_host", rendered)
        self.assertIn("python_version_drift", rendered)
        self.assertIn("ltvm", rendered)
        self.assertIn("<code>tool_version_mismatch</code>", rendered)

    def test_admitted_compatibility_projection_with_warning_is_degraded(self):
        rendered = worker_admission_views.render_worker_admission({
            "admitted": True,
            "warnings": ["legacy environment"],
            "failure_codes": [],
        })
        self.assertIn("Admission: Degraded", rendered)
        self.assertNotIn("Admission: Ready", rendered)

    def test_dataclasses_and_to_dict_objects_are_supported(self):
        @dataclass
        class Profile:
            profile_id: str
            version: int
            content_hash: str
            isolation_profile: str
            network_profile: str

        @dataclass
        class Attestation:
            worker_host_id: str
            environment_instance_id: str
            attested_at: str
            isolation_profile: str
            network_profile: str
            resolved_tools: list = field(default_factory=list)
            warnings: list = field(default_factory=list)
            failures: list = field(default_factory=list)

        class Admission:
            def to_dict(self):
                return {"admission_status": "ready"}

        rendered = worker_admission_views.render_worker_admission(
            Admission(),
            profile=Profile("native-triage", 7, "sha256:abc", "container", "restricted"),
            attestation=Attestation(
                "worker-01", "env-07", "2026-08-30T13:00:00Z", "container",
                "restricted",
            ),
        )
        self.assertIn("Admission: Ready", rendered)
        self.assertIn("native-triage", rendered)
        self.assertIn("Profile version</dt><dd>7", rendered)
        self.assertIn("sha256:abc", rendered)
        self.assertIn("worker-01", rendered)
        self.assertIn("env-07", rendered)
        self.assertIn("2026-08-30T13:00:00Z", rendered)

    def test_persisted_admission_dataclass_fields_and_failure_summary_render(self):
        @dataclass
        class StoredAdmission:
            profile_id: str
            profile_hash: str
            environment_instance_id: str
            status: str
            isolation_profile: str
            network_profile: str
            attestation: dict
            failure_code: str
            failure_summary: str
            checked_at: str

        admission = StoredAdmission(
            "host-profile",
            "sha256:stored",
            "environment-1",
            "blocked",
            "unsandboxed",
            "general",
            {"failure_codes": ["tool_missing", "broker_unreachable"]},
            "tool_missing",
            "Required command rg was not found",
            "2026-08-30T15:00:00Z",
        )
        rendered = worker_admission_views.render_worker_admission(admission)
        self.assertIn("host-profile", rendered)
        self.assertIn("environment-1", rendered)
        self.assertIn("2026-08-30T15:00:00Z", rendered)
        self.assertEqual(rendered.count("<code>tool_missing</code>"), 1)
        self.assertIn("Required command rg was not found", rendered)
        self.assertIn("<code>broker_unreachable</code>", rendered)

    def test_renderer_has_no_links_forms_or_mutating_controls(self):
        rendered = worker_admission_views.render_worker_admission({
            "status": "blocked",
            "failures": ["preflight failed"],
        })
        self.assertNotRegex(rendered, r"(?i)<a\b")
        self.assertNotRegex(rendered, r"(?i)<form\b")
        self.assertNotRegex(rendered, r"(?i)<button\b")
        self.assertNotRegex(rendered, r"(?i)method=['\"]get['\"]")
        self.assertNotIn("Retry", rendered)
        self.assertNotIn("Override", rendered)


if __name__ == "__main__":
    unittest.main()
