import re
import unittest
from dataclasses import dataclass, field

from patch_watcher import resource_views

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


def metric_values(rendered):
    """Map each metric label in a rendered <dl> to the value shown for it.

    The tests care that "4 GiB" is what the guest-memory row displays, not
    that the row happens to be a <dt>/<dd> pair, so the markup is parsed once
    here.  A trailing explanatory <small> note is a separate concern and is
    dropped; keeping it inline is what made the old assertions prefix matches
    that would have accepted "200" for "20".
    """
    return {
        label: re.sub(r"<small>.*?</small>", "", value).strip()
        for label, value in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", rendered)
    }


def owned_vms_by_session(rendered):
    """Map each owning run to the guests its rows claim.

    One flat table now, so ownership is read off each guest's own row rather
    than out of a nested block.  Structural markers only, so display copy can
    change freely.
    """
    owned = {}
    for row in re.findall(r"<tr>(?:(?!</tr>).)*</tr>", rendered, flags=re.S):
        name = re.search(r"<th scope='row'>(.*?)</th>", row)
        if not name:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
        if not cells:
            continue
        owner = cells[-1].strip()
        if owner and "no owner" not in owner:
            owned.setdefault(owner, []).append(name.group(1))
    return owned


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
        self.assertEqual(metric_values(rendered), {
            "Total physical memory": "16 GiB",
            "Used physical memory": "unknown",
            "Available physical memory": "5 GiB",
            "Swap": "unknown",
            "Cache / reclaimable": "unknown",
            "Managed-session process-tree RSS": "unknown",
            "LTVM process RSS": "unknown",
            "Configured LTVM guest memory": "unknown",
        })
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
        self.assertEqual(metric_values(rendered), {
            "Total physical memory": "16 GiB",
            "Used physical memory": "10 GiB",
            "Available physical memory": "6 GiB",
            "Swap": "512 MiB used / 2 GiB total",
            "Cache / reclaimable": "unknown",
            "Managed-session process-tree RSS": "unknown",
            "LTVM process RSS": "750 MiB",
            "Configured LTVM guest memory": "4 GiB",
        })
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
        values = metric_values(rendered)
        self.assertEqual(values["Total physical memory"], "8 GiB")
        self.assertEqual(values["Configured LTVM guest memory"], "2 GiB")
        self.assertIn(">projected-vm<", rendered)

    def test_a_guests_owner_label_is_escaped(self):
        """Run ids and session ids reach the page as text.

        This replaced three tests of the session-row card: two covered the
        recent-message list, which now lives on the run's own page and is
        tested there, and the third covered escaping in a row that no longer
        exists.  The escaping invariant does still apply, to the one place a
        session's own text now reaches the guest table.
        """

        sessions = [{"id": "s1 <unsafe>", "owner_id": "patch-watcher:s1"}]
        vms = [{"name": "co1-<script>", "owner_id": "patch-watcher:s1"}]
        rendered = resource_views.render_resource_dashboard({}, sessions, vms)
        self.assertIn("s1 &lt;unsafe&gt;", rendered)
        self.assertIn("co1-&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)

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
        self.assertEqual(
            owned_vms_by_session(rendered),
            {"s1": ["owned-one"], "s2": ["owned-two"]},
        )
        # Every guest is in the one table, owned or not; the unmatched ones
        # say so rather than living in a second card whose heading only made
        # sense once you had read the first.
        self.assertIn("legacy", rendered)
        self.assertIn("similar-but-external", rendered)
        self.assertEqual(rendered.count("class='vm-unowned'"), 2)
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
        # Neither candidate adopts it.  In one flat table that reads as the
        # guest saying it has no owner, which is the fact worth surfacing:
        # nothing will clean it up automatically.
        self.assertEqual(owned_vms_by_session(rendered), {})
        self.assertIn("vm-unowned", rendered)

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



    def test_vm_table_renders_only_fields_the_sampler_produces(self):
        """Every column must be able to carry a value for a real guest.

        This is one guest exactly as ``LTVMVMStatus.to_dict()`` emits it.
        `ltvm list --json` reports no topology, role, age, CPU share or
        cleanup state, so columns for those printed "unknown" for every VM
        that has ever been rendered.
        """
        vm = {
            "name": "co1-diotests",
            "state": "running",
            "owner_id": "pid:2520851",
            "patch_watcher_session_id": None,
            "configured_guest_memory_bytes": 4 * GIB,
            "host_rss_bytes": 2604929024,
            "process_id": 2520875,
            "vcpus": 2,
            "ip": "192.168.100.204",
            "host_memory_source": "/proc/2520875/status VmRSS",
            "quality": "good",
            "errors": [],
        }
        rendered = resource_views.render_ltvm_guests((), [vm])
        body = rendered.split("<tbody>", 1)[1]
        for expected in (
            ">co1-diotests<", "State: Running", ">2<", ">192.168.100.204<",
            ">4 GiB<", "2.4 GiB", "/proc/2520875/status VmRSS", ">2520875<",
            "class='vm-unowned'",  # no running run matches, so it says so
        ):
            self.assertIn(expected, body)
        self.assertNotIn("unknown", body)
        for dead_column in (
            "Topology / role", "<th scope='col'>Age</th>",
            "<th scope='col'>CPU</th>", "<th scope='col'>Cleanup</th>",
        ):
            self.assertNotIn(dead_column, rendered)

    def test_stopped_guest_reports_absence_rather_than_a_fabricated_sample(self):
        """A stopped guest has no QEMU RSS; say so without inventing an age."""
        vm = {
            "name": "co1-perf1", "state": "stopped", "owner_id": None,
            "configured_guest_memory_bytes": 3 * GIB, "host_rss_bytes": None,
            "process_id": 47338, "vcpus": 2, "ip": "192.168.100.32",
            "host_memory_source": None, "quality": "good", "errors": [],
        }
        rendered = resource_views.render_ltvm_guests((), [vm])
        self.assertIn("Sample quality: good", rendered)
        self.assertNotIn("Sample age unknown", rendered)

    def test_default_empty_dashboard_has_explicit_unknowns_and_empty_states(self):
        rendered = resource_views.render_resource_dashboard(None)
        self.assertIn("Host:</strong> unknown", rendered)
        self.assertIn("Sample age unknown", rendered)
        self.assertNotIn("Running now", rendered)
        self.assertIn("LTVM guests (0)", rendered)
        self.assertNotIn("None", rendered)


if __name__ == "__main__":
    unittest.main()
