import re
import unittest
from dataclasses import dataclass, field

import resource_views


MIB = 1024 ** 2
GIB = 1024 ** 3


@dataclass
class HostSample:
    name: str
    total_bytes: int = None
    used_bytes: int = None
    available_bytes: int = None
    sampled_at: str = None
    sample_age_seconds: int = None
    stale: bool = False
    pressure: str = None
    quality: str = None
    errors: list = field(default_factory=list)


@dataclass
class Session:
    id: str
    patch: object
    run_id: str
    profile: str
    state: str
    elapsed_seconds: int
    current_step: str
    process_tree_rss_bytes: int
    messages: list = field(default_factory=list)


class ResourceViewTests(unittest.TestCase):
    def test_format_bytes_is_iec_and_does_not_coerce_unknowns(self):
        self.assertEqual(resource_views.format_bytes(0), "0 B")
        self.assertEqual(resource_views.format_bytes(1024), "1 KiB")
        self.assertEqual(resource_views.format_bytes(1536), "1.5 KiB")
        self.assertEqual(resource_views.format_bytes(2 * GIB), "2 GiB")
        for value in (None, True, -1, float("nan"), float("inf"), "1024"):
            self.assertEqual(resource_views.format_bytes(value), "unknown")

    def test_host_summary_marks_stale_unknown_data_and_escapes_errors(self):
        host = HostSample(
            name="worker<script>",
            total_bytes=16 * GIB,
            available_bytes=5 * GIB,
            sampled_at="2026-08-30T12:00:00Z",
            sample_age_seconds=91,
            stale=True,
            pressure="warning & rising",
            quality="estimated",
            errors=["collector <failed>", {"message": "bad & stale"}],
        )
        rendered = resource_views.render_host_memory_summary(host)
        self.assertIn("Worker host memory", rendered)
        self.assertIn("worker&lt;script&gt;", rendered)
        self.assertNotIn("worker<script>", rendered)
        self.assertIn("16 GiB", rendered)
        self.assertIn("Used physical memory</dt><dd>unknown", rendered)
        self.assertIn("Stale sample · 1m 31s old", rendered)
        self.assertIn("warning &amp; rising", rendered)
        self.assertIn("estimated", rendered)
        self.assertIn("collector &lt;failed&gt;", rendered)
        self.assertIn("bad &amp; stale", rendered)

    def test_resource_snapshot_shape_is_unwrapped_without_backend_import(self):
        snapshot = {
            "sampled_at": "2026-08-30T12:00:00Z",
            "quality": "measured",
            "host_memory": {
                "total_bytes": 16 * GIB,
                "used_bytes": 10 * GIB,
                "available_bytes": 6 * GIB,
                "swap_total_bytes": 2 * GIB,
                "swap_used_bytes": 512 * MIB,
            },
            "ltvm": {
                "configured_guest_memory_bytes": 4 * GIB,
                "measured_host_rss_bytes": 750 * MIB,
                "vms": [{"name": "snapshot-vm", "owner_id": None}],
            },
        }
        rendered = resource_views.render_resource_dashboard(snapshot)
        self.assertIn("16 GiB", rendered)
        self.assertIn("10 GiB", rendered)
        self.assertIn("6 GiB", rendered)
        self.assertIn("512 MiB used / 2 GiB total", rendered)
        self.assertIn("Configured LTVM guest memory</dt><dd>4 GiB", rendered)
        self.assertIn("LTVM process RSS</dt><dd>750 MiB", rendered)
        self.assertIn(">snapshot-vm<", rendered)

    def test_snapshot_object_mapping_projection_is_supported(self):
        class Snapshot:
            def to_dict(self):
                return {
                    "host_memory": {"total_bytes": 8 * GIB},
                    "ltvm": {
                        "configured_guest_memory_bytes": 2 * GIB,
                        "vms": [{"name": "projected-vm", "owner_id": None}],
                    },
                }

        rendered = resource_views.render_resource_dashboard(Snapshot())
        self.assertIn("Total physical memory</dt><dd>8 GiB", rendered)
        self.assertIn("Configured LTVM guest memory</dt><dd>2 GiB", rendered)
        self.assertIn(">projected-vm<", rendered)

    def test_dataclass_session_row_is_labelled_and_all_dynamic_text_is_escaped(self):
        session = Session(
            id="session<&'\"",
            patch={"title": "LU-1 <unsafe>"},
            run_id="run&1",
            profile="engineering<script>",
            state="waiting_human",
            elapsed_seconds=3661,
            current_step="test <suite>",
            process_tree_rss_bytes=512 * MIB,
            messages=[{"role": "agent", "content": "last & <message>"}],
        )
        rendered = resource_views.render_resource_dashboard({}, [session], [])
        for unsafe in ("<unsafe>", "<script>", "<suite>", "<message>"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("LU-1 &lt;unsafe&gt;", rendered)
        self.assertIn("run&amp;1", rendered)
        self.assertIn("State: Waiting human", rendered)
        self.assertIn("1h 1m 1s", rendered)
        self.assertIn("512 MiB", rendered)
        self.assertIn("last &amp; &lt;message&gt;", rendered)
        self.assertIn("tone-warn", rendered)

    def test_recent_messages_are_tail_bounded(self):
        session = {
            "id": "s1",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "agent", "content": "second"},
                {"role": "agent", "content": "third"},
                {"role": "agent", "content": "fourth"},
            ],
        }
        rendered = resource_views.render_resource_dashboard(
            {}, [session], [], max_messages=2
        )
        recent = rendered.split("<section class='recent-messages'", 1)[1].split(
            "</section>", 1
        )[0]
        self.assertNotIn("first", recent)
        self.assertNotIn("second", recent)
        self.assertIn("third", recent)
        self.assertIn("fourth", recent)
        self.assertIn("2 older message(s) omitted", recent)
        self.assertEqual(recent.count("<li>"), 2)

    def test_messages_mapping_supports_session_and_message_dataclasses(self):
        @dataclass
        class StoredSession:
            session_id: str
            patch_id: str
            run_id: str
            profile: str
            state: str
            started_at: str
            last_qualifying_activity_at: str

        @dataclass
        class StoredMessage:
            author: str
            body: str
            created_at: str

        session = StoredSession(
            session_id="stored-1",
            patch_id="LU-77",
            run_id="run-77",
            profile="triage",
            state="running",
            started_at="2026-08-30T12:00:00+00:00",
            last_qualifying_activity_at="2026-08-30T12:05:00+00:00",
        )
        messages = {
            "stored-1": [StoredMessage("human", "please <check>", "12:05")]
        }
        rendered = resource_views.render_resource_dashboard(
            {}, [session], [], messages_by_session=messages
        )
        self.assertIn("LU-77", rendered)
        self.assertIn("human", rendered)
        self.assertIn("please &lt;check&gt;", rendered)
        self.assertIn("2026-08-30T12:05:00+00:00", rendered)

    def test_vms_associate_by_exact_owner_and_unmatched_vms_stay_other(self):
        sessions = [
            {"id": "s1", "owner_id": "patch-watcher:s1"},
            {"id": "s2", "owner_id": "patch-watcher:s2"},
        ]
        vms = [
            {"name": "owned-one", "owner_id": "patch-watcher:s1"},
            {"name": "owned-two", "owner_id": "patch-watcher:s2"},
            {"name": "legacy", "owner_id": None},
            {"name": "similar-but-external", "owner_id": "prefix-patch-watcher:s1"},
        ]
        rendered = resource_views.render_resource_dashboard({}, sessions, vms)
        first_detail = rendered.split("Session details · 1 owned VM(s)", 1)[1].split(
            "</details>", 1
        )[0]
        second_detail = rendered.split("Session details · 1 owned VM(s)", 2)[2].split(
            "</details>", 1
        )[0]
        other = rendered.split("<section class='other-vms", 1)[1]
        self.assertIn("owned-one", first_detail)
        self.assertNotIn("owned-two", first_detail)
        self.assertIn("owned-two", second_detail)
        self.assertIn("legacy", other)
        self.assertIn("similar-but-external", other)
        for name in ("owned-one", "owned-two", "legacy", "similar-but-external"):
            self.assertEqual(rendered.count(f">{name}<"), 1, name)

    def test_ambiguous_owner_is_not_double_counted_or_adopted(self):
        sessions = [
            {"id": "s1", "owner_id": "duplicate"},
            {"id": "s2", "owner_id": "duplicate"},
        ]
        rendered = resource_views.render_resource_dashboard(
            {}, sessions, [{"name": "ambiguous", "owner_id": "duplicate"}]
        )
        self.assertEqual(rendered.count(">ambiguous<"), 1)
        other = rendered.split("<section class='other-vms", 1)[1]
        self.assertIn(">ambiguous<", other)
        self.assertEqual(rendered.count("Session details · 0 owned VM(s)"), 2)

    def test_guest_capacity_and_actual_host_rss_are_separate(self):
        vm = {
            "name": "vm-1",
            "owner_id": None,
            "configured_guest_memory_bytes": 4 * GIB,
            "host_rss_bytes": 750 * MIB,
            "sample_age_seconds": 7,
        }
        host = {
            "used_bytes": 12 * GIB,
            "configured_guest_memory_bytes": 4 * GIB,
            "vm_process_rss_bytes": 750 * MIB,
        }
        rendered = resource_views.render_resource_dashboard(host, [], [vm])
        self.assertIn("Configured LTVM guest memory", rendered)
        self.assertIn("Guest capacity only; not physical host usage.", rendered)
        self.assertIn("Configured guest memory</th>", rendered)
        self.assertIn("Actual host RSS</th>", rendered)
        self.assertIn("4 GiB", rendered)
        self.assertIn("750 MiB", rendered)
        # No synthetic 12 GiB + 4 GiB or 750 MiB + 4 GiB total is rendered.
        self.assertNotIn("16 GiB", rendered)
        self.assertNotIn("4.7 GiB", rendered)

    def test_session_controls_are_labelled_confirmed_post_forms_not_get_links(self):
        rendered = resource_views.render_resource_dashboard(
            {},
            [{"id": "s1", "state": "running"}],
            [],
            guidance_action="/ops/guidance?next=<unsafe>",
            kill_action="/ops/kill",
            csrf_token="token<&'\"",
        )
        self.assertIn("method='post' action='/ops/guidance?next=&lt;unsafe&gt;'", rendered)
        self.assertIn("method='post' action='/ops/kill'", rendered)
        self.assertIn("Send guidance to this session", rendered)
        self.assertRegex(rendered, r"name='guidance' required")
        self.assertIn("name='confirm' value='yes' required", rendered)
        self.assertIn("I confirm this session should be killed.", rendered)
        self.assertIn("name='csrf_token'", rendered)
        self.assertNotRegex(rendered, r"(?i)<a[^>]+(?:kill|guidance)")
        self.assertNotRegex(rendered, r"(?i)method=['\"]get['\"]")
        self.assertNotIn("token<&", rendered)

    def test_unknown_session_identifier_disables_mutating_controls(self):
        rendered = resource_views.render_resource_dashboard(
            {}, [{"state": "running"}], []
        )
        self.assertIn("Session controls unavailable", rendered)
        self.assertNotIn("action='/sessions/kill'", rendered)
        self.assertNotIn("action='/sessions/guidance'", rendered)

    def test_default_empty_dashboard_has_explicit_unknowns_and_empty_states(self):
        rendered = resource_views.render_resource_dashboard(None)
        self.assertIn("Host:</strong> unknown", rendered)
        self.assertIn("Sample age unknown", rendered)
        self.assertIn("No active managed sessions.", rendered)
        self.assertIn("Other LTVM VMs (0)", rendered)
        self.assertNotIn("None", rendered)


if __name__ == "__main__":
    unittest.main()
