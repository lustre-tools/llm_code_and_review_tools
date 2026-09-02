import unittest
from datetime import datetime, timedelta, timezone

from engineering_state import ExecutionManifest, SafeCommand, ValidationCommandAudit
from ltvm_guest_exec import (
    EngineeringExecutionAuthorization,
    GuestCapacityExhausted,
    GuestCommand,
    GuestExecutionPolicy,
    GuestExecutionRequest,
    GuestTarget,
    GuestTransportBoundary,
    GuestTransportCancelled,
    GuestTransportResult,
    LTVMGuestExecutionBroker,
)
from ltvm_resources import LTVMInventory, owner_id_for_session


SESSION = "session-guest-1"
RUN = "run-guest-1"
REVISION = "a" * 40
OWNER = owner_id_for_session(SESSION)
NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


def inventory(*vms, clusters=None):
    payload = {"vms": list(vms)}
    if clusters is not None:
        payload["clusters"] = list(clusters)
    return LTVMInventory.from_json(payload)


def vm(name="owned-vm", owner=OWNER, **updates):
    value = {
        "name": name,
        "owner_id": owner,
        "status": "running",
        "mem": 2048,
    }
    value.update(updates)
    return value


class FakeInventoryProvider:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def inventory(self):
        self.calls += 1
        if not self.values:
            raise AssertionError("unexpected inventory read")
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, BaseException):
            raise value
        return value


class FakeAuthorizer:
    def __init__(self, values=None):
        self.values = list(values or [
            EngineeringExecutionAuthorization(SESSION, RUN, REVISION)
        ])
        self.calls = []

    def authorization_for(self, **kwargs):
        self.calls.append(kwargs)
        if not self.values:
            return None
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class FakeTransport:
    def __init__(self, results=None, *, boundary=None, on_call=None):
        self.results = list(results or [GuestTransportResult(0)])
        self._boundary = boundary or GuestTransportBoundary()
        self.on_call = on_call
        self.calls = []

    def boundary(self):
        return self._boundary

    def execute_guest(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call(kwargs)
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(value, BaseException):
            raise value
        return value


class FakeTimes:
    def __init__(self, monotonic_values=None):
        self.monotonic_values = list(monotonic_values or [0.0])
        self.wall = NOW

    def monotonic(self):
        return (
            self.monotonic_values.pop(0)
            if len(self.monotonic_values) > 1
            else self.monotonic_values[0]
        )

    def clock(self):
        value = self.wall
        self.wall += timedelta(seconds=1)
        return value


def request(*commands, target=None):
    return GuestExecutionRequest(
        request_id="request-1",
        session_id=SESSION,
        run_id=RUN,
        revision_sha=REVISION,
        target=target or GuestTarget("owned-vm"),
        commands=tuple(commands),
    )


class OpenEndedGuestExecutionTests(unittest.TestCase):
    def test_arbitrary_argv_and_guest_text_are_dispatched_only_through_transport(self):
        provider = FakeInventoryProvider([inventory(vm()), inventory(vm())])
        authorizer = FakeAuthorizer()
        transport = FakeTransport(
            [GuestTransportResult(0, b"argv ok"), GuestTransportResult(0, b"text ok")]
        )
        broker = LTVMGuestExecutionBroker(
            inventory_provider=provider, authorizer=authorizer, transport=transport
        )
        commands = (
            GuestCommand(
                "arbitrary-argv",
                argv=("/bin/bash", "-lc", "make -j8 && custom-test --all"),
                cwd="/work/source",
                env=(("CUSTOM_FLAG", "anything"),),
            ),
            GuestCommand(
                "arbitrary-text",
                text="for t in tests/*; do custom-runner \"$t\"; done",
                cwd="/work/source",
            ),
        )

        result = broker.execute(request(*commands))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(authorizer.calls), 2)
        self.assertEqual([call["command"] for call in transport.calls], list(commands))
        self.assertTrue(all(call["expected_owner_id"] == OWNER for call in transport.calls))
        self.assertTrue(all(isinstance(call["command"].argv, (tuple, type(None))) for call in transport.calls))

    def test_manifest_is_a_plan_and_evidence_source_not_an_executable_allowlist(self):
        manifest = ExecutionManifest(
            "manifest-1",
            RUN,
            REVISION,
            (SafeCommand(
                "planned", ("make", "check"), label="planned validation",
                evidence_role="test",
            ),),
        )
        planned = GuestExecutionRequest.from_manifest(
            request_id="request-manifest",
            session_id=SESSION,
            target=GuestTarget("owned-vm"),
            manifest=manifest,
        )
        ad_hoc = request(GuestCommand("ad-hoc", text="run-any-new-diagnostic --verbose"))

        self.assertEqual(planned.source_manifest_digest, manifest.digest)
        self.assertEqual(planned.commands[0].argv, ("make", "check"))
        self.assertEqual(planned.commands[0].evidence_role, "test")
        self.assertEqual(
            ValidationCommandAudit.from_command(planned.commands[0]).evidence_role,
            "test",
        )
        self.assertIsNone(ad_hoc.source_manifest_id)
        self.assertNotEqual(planned.digest, ad_hoc.digest)


class OwnerAndCapabilitySafetyTests(unittest.TestCase):
    def test_owner_and_capability_are_revalidated_immediately_before_every_command(self):
        provider = FakeInventoryProvider(
            [
                inventory(vm()),
                inventory(vm(owner="patch-watcher:somebody-else")),
            ]
        )
        authorizer = FakeAuthorizer(
            [
                EngineeringExecutionAuthorization(SESSION, RUN, REVISION),
                EngineeringExecutionAuthorization(SESSION, RUN, REVISION),
            ]
        )
        transport = FakeTransport([GuestTransportResult(0)])
        broker = LTVMGuestExecutionBroker(
            inventory_provider=provider, authorizer=authorizer, transport=transport
        )

        result = broker.execute(
            request(
                GuestCommand("one", argv=("first",)),
                GuestCommand("two", argv=("second",)),
            )
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.code, "owner_mismatch")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(authorizer.calls), 2)

    def test_missing_wrong_or_inactive_capability_never_dispatches(self):
        wrong_values = (
            None,
            EngineeringExecutionAuthorization(SESSION, RUN, REVISION, active=False),
            EngineeringExecutionAuthorization(SESSION, RUN, REVISION, capability="other"),
            EngineeringExecutionAuthorization(SESSION, "different-run", REVISION),
        )
        for authorization in wrong_values:
            with self.subTest(authorization=authorization):
                transport = FakeTransport()
                provider = FakeInventoryProvider([inventory(vm())])
                broker = LTVMGuestExecutionBroker(
                    inventory_provider=provider,
                    authorizer=FakeAuthorizer([authorization]),
                    transport=transport,
                )
                result = broker.execute(request(GuestCommand("one", argv=("test",))))
                self.assertEqual(result.code, "capability_denied")
                self.assertEqual(provider.calls, 0)
                self.assertEqual(transport.calls, [])

    def test_unowned_unrelated_missing_and_duplicate_vms_never_dispatch(self):
        candidates = (
            (inventory(vm(owner=None)), "owner_mismatch"),
            (inventory(vm(owner="patch-watcher:other")), "owner_mismatch"),
            (inventory(vm("unrelated")), "ambiguous_target"),
            (inventory(vm(), vm(owner="patch-watcher:other")), "ambiguous_target"),
        )
        for observed, expected_code in candidates:
            with self.subTest(code=expected_code):
                transport = FakeTransport()
                broker = LTVMGuestExecutionBroker(
                    inventory_provider=FakeInventoryProvider([observed]),
                    authorizer=FakeAuthorizer(),
                    transport=transport,
                )
                result = broker.execute(request(GuestCommand("one", argv=("test",))))
                self.assertEqual(result.code, expected_code)
                self.assertEqual(transport.calls, [])

    def test_cluster_member_requires_authoritative_exact_owner_cluster_and_members(self):
        target = GuestTarget("co1-mds", "co1")
        complete = inventory(
            vm("co1-mds", cluster_name="co1"),
            vm("co1-oss", cluster_name="co1"),
            clusters=[{"name": "co1", "owner_id": OWNER, "members": ["co1-mds", "co1-oss"]}],
        )
        incomplete = inventory(
            vm("co1-mds", cluster_name="co1"),
            vm("co1-oss", owner=None, cluster_name="co1"),
            clusters=[{"name": "co1", "owner_id": OWNER, "members": ["co1-mds", "co1-oss"]}],
        )
        for observed, expected_code, calls in (
            (inventory(vm("co1-mds", cluster_name="co1")), "cluster_inventory_unavailable", 0),
            (incomplete, "partial_cluster", 0),
            (complete, "completed", 1),
        ):
            with self.subTest(code=expected_code):
                transport = FakeTransport()
                broker = LTVMGuestExecutionBroker(
                    inventory_provider=FakeInventoryProvider([observed]),
                    authorizer=FakeAuthorizer(),
                    transport=transport,
                )
                result = broker.execute(
                    request(GuestCommand("cluster-test", text="run cluster test"), target=target)
                )
                self.assertEqual(result.code, expected_code)
                self.assertEqual(len(transport.calls), calls)

    def test_transport_must_prove_no_host_fallback_credentials_or_gerrit_writes(self):
        unsafe_boundaries = (
            GuestTransportBoundary(host_fallback=True),
            GuestTransportBoundary(guest_exec_only=False),
            GuestTransportBoundary(owner_checked_at_dispatch=False),
            GuestTransportBoundary(service_credentials_absent=False),
            GuestTransportBoundary(gerrit_writes_blocked=False),
        )
        for boundary in unsafe_boundaries:
            with self.subTest(boundary=boundary):
                transport = FakeTransport(boundary=boundary)
                broker = LTVMGuestExecutionBroker(
                    inventory_provider=FakeInventoryProvider([inventory(vm())]),
                    authorizer=FakeAuthorizer(),
                    transport=transport,
                )
                result = broker.execute(request(GuestCommand("one", argv=("anything",))))
                self.assertEqual(result.code, "unsafe_transport")
                self.assertEqual(transport.calls, [])


class BoundsCancellationAndResultsTests(unittest.TestCase):
    def test_output_is_defensively_bounded_and_audited_with_digests(self):
        transport = FakeTransport(
            [
                GuestTransportResult(
                    0,
                    b"A" * 8,
                    b"B" * 8,
                    stdout_observed_bytes=80,
                    stderr_observed_bytes=90,
                )
            ]
        )
        broker = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=transport,
            policy=GuestExecutionPolicy(max_output_bytes=10),
        )

        result = broker.execute(request(GuestCommand("output", argv=("emit",))))

        self.assertEqual(result.status, "succeeded")
        artifacts = result.commands[0].artifacts
        self.assertEqual(sum(item.captured_bytes for item in artifacts), 10)
        self.assertEqual([item.observed_bytes for item in artifacts], [80, 90])
        self.assertTrue(all(item.truncated for item in artifacts))
        self.assertEqual(len(result.commands[0].command_digest), 64)
        self.assertEqual(len(result.request_digest), 64)
        self.assertEqual(transport.calls[0]["max_output_bytes"], 10)

    def test_step_and_total_time_are_bounded(self):
        times = FakeTimes([0.0, 0.0, 90.0])
        transport = FakeTransport([GuestTransportResult(0)])
        broker = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm()), inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=transport,
            policy=GuestExecutionPolicy(max_step_seconds=30, max_total_seconds=100),
            monotonic=times.monotonic,
            clock=times.clock,
        )
        result = broker.execute(
            request(
                GuestCommand("one", argv=("one",), timeout_seconds=80),
                GuestCommand("two", argv=("two",), timeout_seconds=80),
            )
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual([call["timeout_seconds"] for call in transport.calls], [30, 10])

        expired_times = FakeTimes([0.0, 101.0])
        expired_transport = FakeTransport()
        expired = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=expired_transport,
            policy=GuestExecutionPolicy(max_total_seconds=100),
            monotonic=expired_times.monotonic,
        ).execute(request(GuestCommand("one", argv=("one",))))
        self.assertEqual(expired.code, "total_timeout")
        self.assertEqual(expired_transport.calls, [])

    def test_typed_capacity_exhaustion_is_structured_but_text_is_not_guessed(self):
        capacity = GuestCapacityExhausted(
            "memory", "hypervisor machine error: insufficient memory", requested={"memory_mb": 4096}
        )
        broker = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=FakeTransport([capacity]),
        )
        result = broker.execute(request(GuestCommand("capacity", argv=("test",))))
        self.assertEqual(result.status, "resource_exhausted")
        self.assertEqual(result.code, "ltvm_resource_exhausted")
        self.assertEqual(result.capacity.category, "memory")
        self.assertEqual(result.capacity.requested, {"memory_mb": 4096})

        ordinary = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=FakeTransport(
                [GuestTransportResult(1, stderr=b"resource exhausted, probably")]
            ),
        ).execute(request(GuestCommand("ordinary", argv=("test",))))
        self.assertEqual(ordinary.status, "failed")
        self.assertEqual(ordinary.code, "unexpected_exit")
        self.assertIsNone(ordinary.capacity)

    def test_cancellation_before_and_during_guest_operation_stops_later_work(self):
        transport = FakeTransport()
        broker = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=transport,
        )
        before = broker.execute(
            request(GuestCommand("one", argv=("test",))), cancelled=lambda: True
        )
        self.assertEqual(before.status, "cancelled")
        self.assertEqual(transport.calls, [])

        during_transport = FakeTransport([GuestTransportCancelled("stopped")])
        during = LTVMGuestExecutionBroker(
            inventory_provider=FakeInventoryProvider([inventory(vm())]),
            authorizer=FakeAuthorizer(),
            transport=during_transport,
        ).execute(
            request(
                GuestCommand("one", argv=("long-test",)),
                GuestCommand("two", argv=("must-not-run",)),
            )
        )
        self.assertEqual(during.status, "cancelled")
        self.assertEqual(len(during_transport.calls), 1)
        self.assertEqual(len(during.commands), 1)

    def test_inventory_failure_and_nonrunning_target_fail_closed(self):
        for observed, expected in (
            (RuntimeError("ltvm list failed with private details"), "inventory_unavailable"),
            (inventory(vm(status="stopped")), "target_not_running"),
        ):
            with self.subTest(expected=expected):
                transport = FakeTransport()
                result = LTVMGuestExecutionBroker(
                    inventory_provider=FakeInventoryProvider([observed]),
                    authorizer=FakeAuthorizer(),
                    transport=transport,
                ).execute(request(GuestCommand("one", argv=("test",))))
                self.assertEqual(result.code, expected)
                self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
