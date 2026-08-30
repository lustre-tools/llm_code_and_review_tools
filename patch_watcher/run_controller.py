"""Phase 0C dispatcher and supervisor for manual read-only investigations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_runner import (
    ClaudeRunner,
    ClaudeRunnerError,
    ReadOnlyRunSpec,
    RunnerHandle,
    RunnerSnapshot,
    validate_read_only_report,
)
from session_state import (
    ENGINEERING_PROFILE,
    TERMINAL_STATES,
    InvalidSessionOperation,
    ManagedSession,
    SessionStateStore,
)
from source_checkout import GerritRevision, prepare_revision_checkout
from worker_contract import (
    WorkerProfile,
    build_run_envelope,
    create_run_directories,
    generate_worker_instructions,
    hash_text,
    write_run_snapshot,
)
from worker_doctor import DoctorProbes, doctor


READ_ONLY_CAPABILITIES = (
    "read_evidence",
    "read_source",
    "report_status",
)
DEFAULT_RUNS_DIRECTORY = (
    Path.home() / ".local" / "state" / "patch-watcher" / "runs"
)
RUNNER_EVENT_PREFIX = "runner-event:"
RUNNER_HANDLE_EVENT = "runner_attached"


class RunControllerError(RuntimeError):
    """A run request could not safely be admitted or supervised."""


AlertSender = Callable[[ManagedSession, str, list[Any], str], bool]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _handle_fingerprint(handle: RunnerHandle) -> str:
    payload = json.dumps(
        handle.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _assistant_text(raw: Mapping[str, Any]) -> str:
    if raw.get("type") != "assistant":
        return ""
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )[:8_192]


class RunController:
    """Durable dispatcher; browser requests only enqueue controller intent."""

    def __init__(
        self,
        store: SessionStateStore,
        profile: WorkerProfile,
        *,
        runs_directory: Path = DEFAULT_RUNS_DIRECTORY,
        runner: ClaudeRunner | None = None,
        checkout: Callable[..., Path] = prepare_revision_checkout,
        doctor_fn: Callable[..., Mapping[str, Any]] = doctor,
        clock: Callable[[], datetime] = _utc_now,
        alert_sender: AlertSender | None = None,
        public_base_url: str = "http://127.0.0.1:8080",
        poll_seconds: float = 1.0,
        model: str = "",
        effort: str = "high",
    ) -> None:
        self.store = store
        self.profile = profile
        self.runs_directory = Path(runs_directory).expanduser().resolve()
        self.runner = runner or ClaudeRunner()
        self.checkout = checkout
        self.doctor_fn = doctor_fn
        self.clock = clock
        self.alert_sender = alert_sender
        self.public_base_url = public_base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.model = model
        self.effort = effort
        self.consumer_id = "controller:" + platform.node() + ":" + str(os.getpid())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise,
            name="patch-watcher-run-controller",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)

    def request_investigation(self, patch: Mapping[str, Any]) -> ManagedSession:
        """Atomically reserve one exact patch revision for manual investigation."""

        lifecycle = str(patch.get("lifecycle", "")).casefold()
        if lifecycle not in {"open", "new"}:
            raise RunControllerError("only an open Gerrit change can be investigated")
        try:
            change_number = int(patch["change_number"])
            patchset = int(patch["patchset"])
            revision = str(patch["revision_sha"])
            revision_ref = str(patch["revision_ref"])
            project = str(patch["project"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RunControllerError(
                "refresh the patch before investigating; exact revision data is missing"
            ) from exc
        # Validation happens before reserving the DB row, so an invalid Gerrit
        # identity cannot leave a permanently active shell session record.
        try:
            GerritRevision(change_number, project, patchset, revision, revision_ref)
        except ValueError as exc:
            raise RunControllerError(
                "refresh the patch before investigating; exact revision data is invalid"
            ) from exc
        session_id = str(uuid.uuid4())
        run_id = f"pw-{change_number}-ps{patchset}-{uuid.uuid4().hex[:10]}"
        session = self.store.register_pinned_session(
            session_id,
            patch_id=str(change_number),
            run_id=run_id,
            revision=revision,
            patchset=patchset,
            profile=ENGINEERING_PROFILE,
            state="queued",
            started_at=self.clock(),
        )
        self.store.append_event(
            session_id,
            "investigation_requested",
            {
                "change_number": change_number,
                "patchset": patchset,
                "revision": revision,
                "project": project,
                "revision_ref": revision_ref,
            },
            idempotency_key="investigation-request:" + run_id,
            at=self.clock(),
        )
        return session

    def reconcile_patch_revision(self, patch: Mapping[str, Any]) -> list[str]:
        """Mark active work stale when a successful refresh moves the revision."""
        try:
            change = str(int(patch["change_number"]))
            patchset = int(patch["patchset"])
            revision = str(patch["revision_sha"])
        except (KeyError, TypeError, ValueError):
            return []
        if not revision:
            return []
        stale = []
        for session in self.store.list_sessions(include_terminal=False):
            if session.patch_id != change:
                continue
            result = self.store.mark_stale_for_revision(
                session.session_id,
                observed_revision=revision,
                observed_patchset=patchset,
                at=self.clock(),
            )
            if result is not None:
                stale.append(session.run_id)
        return stale

    def tick(self) -> None:
        """Perform one bounded reconciliation/dispatch pass."""

        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            for session in self.store.list_sessions(include_terminal=True):
                try:
                    if session.state == "queued":
                        self._prepare_and_start(session)
                    elif session.state not in TERMINAL_STATES:
                        self._supervise_session(session)
                    else:
                        self._cleanup_session(session)
                except Exception as exc:  # keep other independent runs observable
                    self._record_controller_failure(session, exc)
        finally:
            self._tick_lock.release()

    def _request_payload(self, session: ManagedSession) -> Mapping[str, Any]:
        events = self.store.list_events(session.session_id)
        for event in reversed(events):
            if event.event_type == "investigation_requested":
                return event.payload
        raise RunControllerError("run is missing its immutable investigation request")

    def _run_root(self, session: ManagedSession) -> Path:
        return (self.runs_directory / session.run_id).resolve()

    def _prepare_and_start(self, session: ManagedSession) -> None:
        payload = self._request_payload(session)
        self.store.set_state(session.session_id, "preparing", changed_at=self.clock())
        self.store.register_owned_resource(
            session.session_id,
            owner_id="patch-watcher:" + session.session_id,
            resource_type="run_directory",
            external_id=str(self._run_root(session)),
            metadata={"run_id": session.run_id},
            at=self.clock(),
        )
        layout = create_run_directories(self.runs_directory, session.run_id)
        revision = GerritRevision(
            int(payload["change_number"]),
            str(payload["project"]),
            int(payload["patchset"]),
            str(payload["revision"]),
            str(payload["revision_ref"]),
        )
        self.checkout(layout.resolve("/work/source"), revision)
        task = (
            "Investigate the pinned Gerrit revision using only the local source tree. "
            "Explain findings with precise file references. Do not modify files, run "
            "commands, contact services, or propose that an external action was taken."
        )
        instructions = generate_worker_instructions(
            self.profile,
            run_id=session.run_id,
            task=task,
            revision_sha=str(session.revision),
            capabilities=READ_ONLY_CAPABILITIES,
            organization_policy=(
                "This is a manual Phase 0C read-only investigation. Gerrit, CI, JIRA, "
                "VM, shell, and file-write actions are not granted."
            ),
            reporting_instructions=(
                "Return the controller-required structured read-only report through the "
                "Claude stream. Do not attempt to execute a reporting command or write a report file."
            ),
        )
        envelope = build_run_envelope(
            run_id=session.run_id,
            change_id=session.patch_id,
            patchset=int(session.patchset or 0),
            revision_sha=str(session.revision),
            profile=self.profile,
            task=task,
            capabilities=READ_ONLY_CAPABILITIES,
            instructions_hash=hash_text(instructions),
            created_at=self.clock().isoformat(),
        )
        paths = write_run_snapshot(layout, envelope, instructions)
        os.chmod(layout.resolve("/work/source"), 0o500)
        os.chmod(layout.resolve("/work/input"), 0o500)
        probes = DoctorProbes()
        system_which = probes.which
        local_worker = Path(__file__).with_name("pw-worker").resolve()
        probes.which = lambda command: (
            str(local_worker) if command == "pw-worker" else system_which(command)
        )
        probes.endpoint = lambda endpoint: str(endpoint).startswith(
            "local://patch-watcher"
        )
        attestation = dict(self.doctor_fn(
            self.profile,
            envelope,
            probes=probes,
            envelope_path=paths["run_envelope"],
        ))
        status = str(attestation.get("status", "blocked"))
        failure_codes = list(attestation.get("failure_codes") or [])
        self.store.record_worker_admission(
            session.session_id,
            profile_id=self.profile.profile_id,
            profile_hash=self.profile.content_hash,
            environment_instance_id=str(
                (attestation.get("worker_host") or {}).get("host_id", platform.node())
            ),
            status=status,
            isolation_profile=str(attestation.get("isolation_mode", "host_unsandboxed")),
            network_profile=str(attestation.get("network_mode", "host_ambient")),
            attestation=attestation,
            instruction_hash=hash_text(instructions),
            failure_code=failure_codes[0] if status == "blocked" and failure_codes else (
                "worker_admission_blocked" if status == "blocked" else None
            ),
            failure_summary=(
                "Worker admission blocked: " + ", ".join(failure_codes[:8])
                if status == "blocked" else None
            ),
            checked_at=self.clock(),
        )
        self.store.append_event(
            session.session_id,
            "worker_admission",
            {"status": status, "failure_codes": failure_codes},
            idempotency_key="worker-admission:" + session.run_id,
            at=self.clock(),
        )
        if status == "blocked":
            self.store.finish_session(
                session.session_id,
                "failed",
                failure_code=failure_codes[0] if failure_codes else "worker_admission_blocked",
                failure_summary="Worker environment did not pass preflight admission",
                finished_at=self.clock(),
            )
            return
        prompt = instructions + (
            "\nReturn the required structured read-only report. If a material human "
            "decision is required, return needs_input with one precise question."
        )
        snapshot = self.runner.start(ReadOnlyRunSpec(
            run_id=session.run_id,
            session_id=session.session_id,
            cwd=str(layout.resolve("/work/source")),
            runtime_dir=str(layout.resolve("/run/patch-watcher") / "claude"),
            prompt=prompt,
            name=f"patch-watcher-{session.patch_id}-ps{session.patchset}",
            model=self.model,
            effort=self.effort,
        ))
        self._persist_handle(session, snapshot)
        self.store.set_state(session.session_id, "running", changed_at=self.clock())

    def _persist_handle(self, session: ManagedSession, snapshot: RunnerSnapshot) -> None:
        handle = snapshot.handle
        fingerprint = _handle_fingerprint(handle)
        self.store.append_event(
            session.session_id,
            RUNNER_HANDLE_EVENT,
            {"handle": handle.to_dict(), "process_fingerprint": fingerprint},
            idempotency_key="runner-attached:" + session.run_id,
            at=self.clock(),
        )
        self.store.attach_runner_transport(
            session.session_id,
            transport="claude-stream-json-v1",
            transport_session_id=handle.session_id,
            pid=handle.host_identity.pid,
            process_started_at=datetime.fromtimestamp(snapshot.started_at, timezone.utc),
            process_fingerprint=fingerprint,
            attached_at=self.clock(),
        )

    def _load_handle(self, session: ManagedSession) -> RunnerHandle | None:
        for event in reversed(self.store.list_events(session.session_id)):
            if event.event_type == RUNNER_HANDLE_EVENT:
                raw = event.payload.get("handle")
                if isinstance(raw, Mapping):
                    return RunnerHandle.from_dict(raw)
        state_path = self._run_root(session) / "run" / "patch-watcher" / "claude" / "host-state.json"
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
            return RunnerSnapshot.from_dict(value).handle
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _last_runner_cursor(self, session: ManagedSession) -> int:
        cursor = 0
        for event in self.store.list_events(session.session_id):
            if event.event_type == "runner_event":
                cursor = max(cursor, int(event.payload.get("runner_cursor", 0)))
        return cursor

    def _supervise_session(self, session: ManagedSession) -> None:
        decision = self.store.evaluate_policy(session.session_id, now=self.clock())
        handle = self._load_handle(session)
        if decision.timeout is not None:
            if handle is not None:
                self._stop_runner_once(session, handle)
            self.store.finish_session(
                session.session_id,
                "failed",
                failure_code=decision.timeout.code,
                failure_summary=f"Session exceeded policy deadline {decision.timeout.deadline_at.isoformat()}",
                finished_at=self.clock(),
            )
            self._send_alert_once(session, decision.timeout.code)
            return
        if decision.reminder is not None:
            if self._send_alert_once(
                session,
                f"engineering session has run for {decision.reminder.interval_index * 2} hours",
                key=decision.reminder.idempotency_key,
            ):
                self.store.mark_reminder_delivered(
                    session.session_id,
                    decision.reminder.interval_index,
                    delivered_at=self.clock(),
                    idempotency_key=decision.reminder.idempotency_key,
                )
        if handle is None:
            if session.state == "preparing":
                self.store.finish_session(
                    session.session_id,
                    "failed",
                    failure_code="runner_start_interrupted",
                    failure_summary="Preparation was interrupted before a runner identity was persisted",
                    finished_at=self.clock(),
                )
            return
        probe = self.runner.probe(handle)
        if not probe.adoptable:
            self.store.finish_session(
                session.session_id,
                "failed",
                failure_code="runner_lost",
                failure_summary=probe.reason,
                finished_at=self.clock(),
            )
            self._send_alert_once(session, "runner_lost")
            return
        transport = self.store.get_runner_transport(session.session_id)
        if transport is not None and transport.adoption_state != "adopted":
            self.runner.adopt(handle)
            self.store.adopt_runner_transport(
                session.session_id,
                process_fingerprint=transport.process_fingerprint,
                at=self.clock(),
            )
        self._execute_controls(session, handle)
        refreshed = self.store.get_session(session.session_id)
        if refreshed.state in TERMINAL_STATES:
            return
        if refreshed.state != "paused":
            self._deliver_guidance(refreshed, handle)
        self._ingest_runner_events(refreshed, handle)

    def _deliver_guidance(self, session: ManagedSession, handle: RunnerHandle) -> None:
        guidance = self.store.claim_next_guidance(
            session.session_id, self.consumer_id, at=self.clock()
        )
        if guidance is None:
            return
        try:
            self.runner.queue_guidance(handle, guidance.guidance_id, guidance.body)
            self.store.finish_guidance_delivery(
                guidance.guidance_id,
                self.consumer_id,
                delivered=True,
                at=self.clock(),
            )
        except Exception as exc:
            self.store.finish_guidance_delivery(
                guidance.guidance_id,
                self.consumer_id,
                delivered=False,
                at=self.clock(),
                failure_summary=type(exc).__name__,
            )

    def _execute_controls(self, session: ManagedSession, handle: RunnerHandle) -> None:
        for intent in self.store.list_control_intents(session.session_id):
            if intent.status in {"executed", "failed"}:
                continue
            if intent.action == "pause" and intent.status == "recorded":
                self.store.set_state(session.session_id, "paused", changed_at=self.clock())
            elif intent.action == "interrupt" and intent.status == "recorded":
                self.runner.interrupt(handle)
            elif intent.action in {"cancel", "kill"} and intent.status == "confirmed":
                self._stop_runner_once(
                    session, handle, force=(intent.action == "kill")
                )
                self.store.finish_session(
                    session.session_id,
                    "cancelled",
                    result={"operator_action": intent.action},
                    finished_at=self.clock(),
                )
            else:
                continue
            self.store.finish_control_intent(
                session.session_id,
                intent.request_id,
                succeeded=True,
                executed_at=self.clock(),
            )

    def _ingest_runner_events(self, session: ManagedSession, handle: RunnerHandle) -> None:
        after = self._last_runner_cursor(session)
        while True:
            events = self.runner.events(handle, after_cursor=after, limit=100)
            if not events:
                break
            for event in events:
                after = max(after, event.cursor)
                self.store.append_event(
                    session.session_id,
                    "runner_event",
                    {
                        "runner_cursor": event.cursor,
                        "runner_type": event.type,
                        "runner_payload": dict(event.payload),
                    },
                    idempotency_key=f"{RUNNER_EVENT_PREFIX}{session.run_id}:{event.cursor}",
                    at=datetime.fromtimestamp(event.timestamp, timezone.utc),
                )
                if event.type == "claude_event":
                    raw = event.payload
                    text = _assistant_text(raw)
                    if text:
                        self.store.record_message(
                            session.session_id, "agent", text,
                            at=datetime.fromtimestamp(event.timestamp, timezone.utc),
                        )
                        self.store.record_activity(
                            session.session_id,
                            at=datetime.fromtimestamp(event.timestamp, timezone.utc),
                        )
                    # The transport validates result.structured_output and
                    # emits a separate worker_report event. Workflow state is
                    # never driven directly by an unvalidated Claude event.
                if event.type == "worker_report":
                    self._apply_report(session, handle, event.payload)
                    return
                if event.type == "worker_report_invalid":
                    self._stop_runner_once(session, handle)
                    self.store.finish_session(
                        session.session_id,
                        "failed",
                        failure_code="worker_report_invalid",
                        failure_summary=str(event.payload.get("reason", "invalid worker report"))[:500],
                        finished_at=self.clock(),
                    )
                    return
                if event.type == "process_exit":
                    current = self.store.get_session(session.session_id)
                    if current.state not in TERMINAL_STATES:
                        self.store.finish_session(
                            session.session_id,
                            "failed",
                            failure_code="runner_exited_without_report",
                            failure_summary="Claude runner exited without a valid terminal report",
                            finished_at=self.clock(),
                        )
                        self._send_alert_once(session, "runner_exited_without_report")
                    return
            if len(events) < 100:
                break

    def _apply_report(
        self, session: ManagedSession, handle: RunnerHandle, value: Any
    ) -> None:
        try:
            report = dict(validate_read_only_report(value))
        except Exception as exc:
            self._stop_runner_once(session, handle)
            self.store.finish_session(
                session.session_id,
                "failed",
                failure_code="worker_report_invalid",
                failure_summary=str(exc)[:500],
                finished_at=self.clock(),
            )
            return
        self.store.record_message(
            session.session_id, "agent-report", report["summary"], at=self.clock()
        )
        if report["state"] == "needs_input":
            self.store.ask_human(
                session.session_id, report["question"], at=self.clock()
            )
            return
        self._stop_runner_once(session, handle)
        if report["state"] == "complete":
            self.store.finish_session(
                session.session_id,
                "succeeded",
                result=report,
                finished_at=self.clock(),
            )
        else:
            self.store.finish_session(
                session.session_id,
                "failed",
                result=report,
                failure_code="worker_report_failed",
                failure_summary=report["summary"],
                finished_at=self.clock(),
            )

    def _send_alert_once(
        self, session: ManagedSession, reason: str, *, key: str | None = None
    ) -> bool:
        delivery_key = key or f"session-alert:{session.session_id}:{reason}"
        delivery = self.store.ensure_delivery(
            session.session_id,
            kind="session_alert",
            idempotency_key=delivery_key,
            payload={"reason": reason},
            at=self.clock(),
        )
        if delivery.status != "pending":
            return delivery.status == "delivered"
        messages = self.store.recent_messages(session.session_id, limit=8)
        url = f"{self.public_base_url}/runs/{session.run_id}/confirm?intent=kill"
        sent = bool(
            self.alert_sender(session, reason, messages, url)
            if self.alert_sender is not None else False
        )
        self.store.finish_delivery(
            delivery_key,
            delivered=sent,
            at=self.clock(),
            failure_summary=None if sent else "session alert delivery unavailable",
        )
        return sent

    def _cleanup_session(self, session: ManagedSession) -> None:
        handle = self._load_handle(session)
        if handle is not None and self.runner.probe(handle).alive:
            self._stop_runner_once(session, handle)
            return
        for resource in self.store.list_owned_resources(session_id=session.session_id):
            if resource.state != "cleanup_pending":
                continue
            if resource.resource_type != "run_directory":
                self.store.mark_resource_cleanup(
                    resource.resource_id,
                    succeeded=False,
                    failure_summary="controller cannot clean this resource type",
                    at=self.clock(),
                )
                continue
            target = Path(resource.external_id).resolve()
            expected = self._run_root(session)
            if target != expected or target.parent != self.runs_directory:
                self.store.mark_resource_cleanup(
                    resource.resource_id,
                    succeeded=False,
                    failure_summary="resource path failed owner-scope validation",
                    at=self.clock(),
                )
                continue
            if target.exists():
                shutil.rmtree(target)
            self.store.mark_resource_cleanup(
                resource.resource_id,
                succeeded=True,
                at=self.clock(),
            )

    def _stop_runner_once(
        self, session: ManagedSession, handle: RunnerHandle, *, force: bool = False
    ) -> None:
        stop_key = "runner-force-stop" if force else "runner-stop"
        if any(
            event.event_type == stop_key
            for event in self.store.list_events(session.session_id)
        ):
            return
        if force:
            self.runner.kill(handle)
        else:
            self.runner.terminate(handle)
        self.store.append_event(
            session.session_id,
            stop_key,
            {"force": force},
            idempotency_key=f"{stop_key}:{session.run_id}",
            at=self.clock(),
        )

    def _record_controller_failure(
        self, session: ManagedSession, exc: Exception
    ) -> None:
        try:
            current = self.store.get_session(session.session_id)
            self.store.append_event(
                session.session_id,
                "controller_error",
                {"error_type": type(exc).__name__},
                at=self.clock(),
            )
            if current.state not in TERMINAL_STATES:
                self.store.finish_session(
                    session.session_id,
                    "failed",
                    failure_code="controller_error",
                    failure_summary=type(exc).__name__,
                    finished_at=self.clock(),
                )
        except Exception:
            pass


__all__ = [
    "DEFAULT_RUNS_DIRECTORY", "READ_ONLY_CAPABILITIES", "RunController",
    "RunControllerError",
]
