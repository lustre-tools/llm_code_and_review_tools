"""Durable, side-effect-free standing automation policies.

This module deliberately knows nothing about HTTP, Gerrit credentials, CI
clients, workers, or process launch.  It stores an operator's per-patch
intent and makes a pure trigger decision from already-normalized facts.  A
controller remains responsible for collecting fresh evidence and performing
any resulting action.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Collection, Mapping


DOCUMENT_SCHEMA = "patch-watcher-standing-policies/v1"
DECISION_SCHEMA = "patch-watcher-standing-trigger-decision/v1"

TEST_FAILURE_MODES = frozenset({"off", "deterministic", "investigate"})
BUILD_FAILURE_MODES = frozenset({"off", "repair"})
REVIEW_COMMENT_MODES = frozenset({"off", "simple", "all"})
TRIGGER_MODES = frozenset({"manual", "automatic"})
TRIGGER_KINDS = frozenset({"test_failure", "build_failure", "review_comments"})
TRIGGER_SOURCES = frozenset({"manual", "automatic"})
ACTIVE_RUN_STATES = frozenset(
    {"planned", "starting", "running", "waiting_external", "waiting_human"}
)

_PATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_FINGERPRINT_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_MAX_DOCUMENT_BYTES = 1_000_000
_MAX_POLICIES = 10_000


class StandingPolicyError(RuntimeError):
    """Base error for standing-policy persistence."""


class StandingPolicyConflict(StandingPolicyError):
    """Raised when a caller tries to overwrite a newer policy version."""


def _bounded_text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{name} must be a non-empty bounded printable string")
    return result


def _patch_id(value: Any) -> str:
    result = _bounded_text("patch_id", value, 128)
    if not _PATCH_ID_RE.fullmatch(result):
        raise ValueError("patch_id contains unsupported characters")
    return result


def _choice(name: str, value: Any, choices: Collection[str]) -> str:
    result = _bounded_text(name, value, 32).lower()
    if result not in choices:
        raise ValueError(f"unsupported {name}: {result}")
    return result


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise ValueError("version must be a non-negative integer")
    return value


def _strict_keys(value: Mapping[str, Any], allowed: Collection[str], name: str) -> None:
    extras = set(value) - set(allowed)
    if extras:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(sorted(extras))}")


@dataclasses.dataclass(frozen=True)
class PatchAutomationPolicy:
    """Operator-selected capabilities for one watched Gerrit change.

    Defaults are deliberately inert.  The policy follows the change across
    patchsets; every decision is separately bound to an exact patchset and
    revision by :class:`RevisionIdentity`.
    """

    patch_id: str
    test_failures: str = "off"
    build_failures: str = "off"
    review_comments: str = "off"
    trigger_mode: str = "manual"
    version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "patch_id", _patch_id(self.patch_id))
        object.__setattr__(
            self, "test_failures", _choice("test_failures", self.test_failures, TEST_FAILURE_MODES)
        )
        object.__setattr__(
            self, "build_failures", _choice("build_failures", self.build_failures, BUILD_FAILURE_MODES)
        )
        object.__setattr__(
            self,
            "review_comments",
            _choice("review_comments", self.review_comments, REVIEW_COMMENT_MODES),
        )
        object.__setattr__(self, "trigger_mode", _choice("trigger_mode", self.trigger_mode, TRIGGER_MODES))
        object.__setattr__(self, "version", _version(self.version))

    @classmethod
    def from_dict(cls, patch_id: str, value: Mapping[str, Any] | None) -> "PatchAutomationPolicy":
        """Decode a policy, defaulting safely when old fields are absent.

        A minimal compatibility shim accepts the old generic ``mode`` field.
        Old ``automatic`` means deterministic retest automation only; it does
        not silently authorize build repair or review editing.
        """

        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError("policy must be an object")
        allowed = {
            "patch_id", "test_failures", "build_failures", "review_comments",
            "trigger_mode", "version", "mode",
        }
        _strict_keys(value, allowed, "policy")
        embedded_id = value.get("patch_id", patch_id)
        if _patch_id(embedded_id) != _patch_id(patch_id):
            raise ValueError("policy patch_id does not match its document key")

        legacy_mode = value.get("mode")
        legacy_test = "off"
        legacy_trigger = "manual"
        if legacy_mode is not None:
            legacy_mode = _choice(
                "legacy mode", legacy_mode,
                frozenset({"disabled", "advise", "approval", "automatic"}),
            )
            if legacy_mode != "disabled":
                legacy_test = "deterministic"
            if legacy_mode == "automatic":
                legacy_trigger = "automatic"

        return cls(
            patch_id=patch_id,
            test_failures=value.get("test_failures", legacy_test),
            build_failures=value.get("build_failures", "off"),
            review_comments=value.get("review_comments", "off"),
            trigger_mode=value.get("trigger_mode", legacy_trigger),
            version=value.get("version", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "test_failures": self.test_failures,
            "build_failures": self.build_failures,
            "review_comments": self.review_comments,
            "trigger_mode": self.trigger_mode,
            "version": self.version,
        }

    def configured_action(self, kind: str) -> str:
        kind = _choice("trigger kind", kind, TRIGGER_KINDS)
        return {
            "test_failure": self.test_failures,
            "build_failure": self.build_failures,
            "review_comments": self.review_comments,
        }[kind]


@dataclasses.dataclass(frozen=True)
class RevisionIdentity:
    patch_id: str
    change_number: int
    patchset: int
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "patch_id", _patch_id(self.patch_id))
        object.__setattr__(self, "change_number", _positive_int("change_number", self.change_number))
        object.__setattr__(self, "patchset", _positive_int("patchset", self.patchset))
        revision = _bounded_text("revision", self.revision, 64).lower()
        if not _REVISION_RE.fullmatch(revision):
            raise ValueError("revision must be a full hexadecimal object ID")
        object.__setattr__(self, "revision", revision)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class TriggerObservation:
    kind: str
    identity: RevisionIdentity
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _choice("trigger kind", self.kind, TRIGGER_KINDS))
        if not isinstance(self.identity, RevisionIdentity):
            raise ValueError("identity must be a RevisionIdentity")
        fingerprint = _bounded_text("fingerprint", self.fingerprint, 71).lower()
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("fingerprint must be a SHA-256 digest")
        if not fingerprint.startswith("sha256:"):
            fingerprint = "sha256:" + fingerprint
        object.__setattr__(self, "fingerprint", fingerprint)


@dataclasses.dataclass(frozen=True)
class ActivePatchRun:
    run_id: str
    patch_id: str
    state: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _bounded_text("run_id", self.run_id, 256))
        object.__setattr__(self, "patch_id", _patch_id(self.patch_id))
        object.__setattr__(self, "state", _choice("run state", self.state, ACTIVE_RUN_STATES))


@dataclasses.dataclass(frozen=True)
class TriggerDecision:
    eligible: bool
    code: str
    explanation: str
    action: str
    source: str
    identity: RevisionIdentity
    fingerprint: str
    coalescing_key: str
    policy_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DECISION_SCHEMA,
            "eligible": self.eligible,
            "code": self.code,
            "explanation": self.explanation,
            "action": self.action,
            "source": self.source,
            "identity": self.identity.to_dict(),
            "fingerprint": self.fingerprint,
            "coalescing_key": self.coalescing_key,
            "policy_version": self.policy_version,
        }


def trigger_coalescing_key(
    policy: PatchAutomationPolicy,
    observation: TriggerObservation,
    *,
    source: str,
) -> str:
    """Return the stable exact-event key used by the durable run ledger."""

    _choice("trigger source", source, TRIGGER_SOURCES)
    if policy.patch_id != observation.identity.patch_id:
        raise ValueError("policy and observation refer to different patches")
    payload = {
        "schema": DECISION_SCHEMA,
        "kind": observation.kind,
        "action": policy.configured_action(observation.kind),
        "identity": observation.identity.to_dict(),
        "fingerprint": observation.fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "standing:" + hashlib.sha256(encoded).hexdigest()


def decide_trigger(
    policy: PatchAutomationPolicy,
    observation: TriggerObservation,
    current_identity: RevisionIdentity,
    *,
    source: str,
    active_run: ActivePatchRun | None = None,
    consumed_keys: Collection[str] = (),
) -> TriggerDecision:
    """Make an explainable decision without performing or persisting effects."""

    source = _choice("trigger source", source, TRIGGER_SOURCES)
    if not isinstance(policy, PatchAutomationPolicy):
        raise ValueError("policy must be a PatchAutomationPolicy")
    if not isinstance(observation, TriggerObservation):
        raise ValueError("observation must be a TriggerObservation")
    if not isinstance(current_identity, RevisionIdentity):
        raise ValueError("current_identity must be a RevisionIdentity")
    if policy.patch_id != observation.identity.patch_id:
        raise ValueError("policy and observation refer to different patches")
    if current_identity.patch_id != policy.patch_id:
        raise ValueError("current identity refers to a different patch")

    action = policy.configured_action(observation.kind)
    key = trigger_coalescing_key(policy, observation, source=source)
    consumed = frozenset(_bounded_text("consumed key", value, 128) for value in consumed_keys)

    def result(eligible: bool, code: str, explanation: str) -> TriggerDecision:
        return TriggerDecision(
            eligible=eligible,
            code=code,
            explanation=explanation,
            action=action,
            source=source,
            identity=observation.identity,
            fingerprint=observation.fingerprint,
            coalescing_key=key,
            policy_version=policy.version,
        )

    if observation.identity != current_identity:
        return result(False, "stale_revision", "The event is not for the current exact patchset revision.")
    if action == "off":
        return result(False, "capability_off", f"{observation.kind} handling is disabled for this patch.")
    if source == "automatic" and policy.trigger_mode != "automatic":
        return result(False, "manual_only", "The policy requires an explicit manual trigger.")
    if active_run is not None:
        if not isinstance(active_run, ActivePatchRun):
            raise ValueError("active_run must be an ActivePatchRun")
        if active_run.patch_id == policy.patch_id:
            return result(
                False,
                "active_run",
                f"Run {active_run.run_id} already owns this patch; the event was coalesced.",
            )
    if key in consumed:
        return result(False, "duplicate", "This exact event and action were already handled.")
    return result(True, "eligible", f"Start the configured {action} handler for this exact revision.")


class StandingPolicyStore:
    """A private, atomic JSON policy store with optimistic concurrency."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def get(self, patch_id: str) -> PatchAutomationPolicy:
        patch_id = _patch_id(patch_id)
        with self._locked(exclusive=False):
            return self._read().get(patch_id, PatchAutomationPolicy(patch_id))

    def list(self) -> tuple[PatchAutomationPolicy, ...]:
        with self._locked(exclusive=False):
            values = self._read().values()
            return tuple(sorted(values, key=lambda item: item.patch_id))

    def save(
        self,
        policy: PatchAutomationPolicy,
        *,
        expected_version: int | None = None,
    ) -> PatchAutomationPolicy:
        if not isinstance(policy, PatchAutomationPolicy):
            raise ValueError("policy must be a PatchAutomationPolicy")
        if expected_version is not None:
            expected_version = _version(expected_version)
        with self._locked(exclusive=True):
            policies = self._read()
            current = policies.get(policy.patch_id, PatchAutomationPolicy(policy.patch_id))
            expected = policy.version if expected_version is None else expected_version
            if current.version != expected:
                raise StandingPolicyConflict(
                    f"policy {policy.patch_id} is version {current.version}, expected {expected}"
                )
            saved = dataclasses.replace(policy, version=current.version + 1)
            policies[saved.patch_id] = saved
            self._write(policies)
            return saved

    def remove(self, patch_id: str, *, expected_version: int | None = None) -> bool:
        patch_id = _patch_id(patch_id)
        if expected_version is not None:
            expected_version = _version(expected_version)
        with self._locked(exclusive=True):
            policies = self._read()
            current = policies.get(patch_id)
            if current is None:
                return False
            if expected_version is not None and current.version != expected_version:
                raise StandingPolicyConflict(
                    f"policy {patch_id} is version {current.version}, expected {expected_version}"
                )
            del policies[patch_id]
            self._write(policies)
            return True

    def _locked(self, *, exclusive: bool):
        store = self

        class _Lock:
            def __enter__(self):
                store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    store.path.parent.chmod(0o700)
                except OSError:
                    pass
                self.fd = os.open(store.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                os.chmod(store.lock_path, 0o600)
                fcntl.flock(self.fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                return self

            def __exit__(self, exc_type, exc, traceback):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)

        return _Lock()

    def _read(self) -> dict[str, PatchAutomationPolicy]:
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > _MAX_DOCUMENT_BYTES:
            raise StandingPolicyError("standing-policy document is too large")
        try:
            document = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=self._unique_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise StandingPolicyError(f"invalid standing-policy document: {exc}") from exc
        if not isinstance(document, Mapping):
            raise StandingPolicyError("standing-policy document must be an object")

        # Legacy documents were a direct {patch_id: policy} mapping.
        if "schema" not in document and "policies" not in document:
            policies_value = document
        else:
            try:
                _strict_keys(document, {"schema", "policies"}, "policy document")
                if document.get("schema") != DOCUMENT_SCHEMA:
                    raise ValueError("unsupported standing-policy schema")
                policies_value = document.get("policies", {})
            except ValueError as exc:
                raise StandingPolicyError(str(exc)) from exc
        if not isinstance(policies_value, Mapping) or len(policies_value) > _MAX_POLICIES:
            raise StandingPolicyError("policies must be a bounded object")
        try:
            return {
                _patch_id(key): PatchAutomationPolicy.from_dict(key, value)
                for key, value in policies_value.items()
            }
        except ValueError as exc:
            raise StandingPolicyError(f"invalid policy: {exc}") from exc

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def _write(self, policies: Mapping[str, PatchAutomationPolicy]) -> None:
        document = {
            "schema": DOCUMENT_SCHEMA,
            "policies": {key: policies[key].to_dict() for key in sorted(policies)},
        }
        encoded = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(encoded) > _MAX_DOCUMENT_BYTES:
            raise StandingPolicyError("standing-policy document is too large")
        pending = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as stream:
                pending = Path(stream.name)
                os.chmod(pending, 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise StandingPolicyError(f"could not persist standing policies: {exc}") from exc
        finally:
            if pending is not None and pending.exists():
                pending.unlink()
