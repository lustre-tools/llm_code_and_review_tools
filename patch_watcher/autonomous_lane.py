"""Side-effect-free policy model for narrow autonomous lanes.

The module answers one question: given a durable operator configuration and
an exact, normalized observation, may a controller perform a bounded action?
It never contacts Gerrit, Maloo, a worker, or any other external system.

The first built-in lane intentionally does only one thing: request one retest
for an exact Maloo failure snapshot whose failures were all classified as
deterministic.  Every enable switch defaults off.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CONTROL_SCHEMA = "patch-watcher-autonomous-lanes/v1"
DECISION_SCHEMA = "patch-watcher-autonomous-lane-decision/v1"
AUDIT_SCHEMA = "patch-watcher-autonomous-lane-audit/v1"

DETERMINISTIC_RETEST_LANE = "deterministic-test-retest"
DETERMINISTIC_RETEST_VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_LANE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_FINGERPRINT_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_MAX_CONTROL_BYTES = 1_000_000
_MAX_AUDIT_LINE_BYTES = 1_000_000
_MAX_CONTROLS = 10_000


class AutonomousLaneError(RuntimeError):
    """Base error for autonomous-lane state."""


class AutonomousLaneConflict(AutonomousLaneError):
    """Raised when optimistic concurrency detects a lost update."""


def _text(name: str, value: Any, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{name} must be a bounded printable string")
    return result


def _identifier(name: str, value: Any) -> str:
    result = _text(name, value, 200)
    if not _ID_RE.fullmatch(result):
        raise ValueError(f"{name} contains unsupported characters")
    return result


def _lane_name(value: Any) -> str:
    result = _text("lane name", value, 64).lower()
    if not _LANE_RE.fullmatch(result):
        raise ValueError("lane name contains unsupported characters")
    return result


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _fingerprint(value: Any) -> str:
    result = _text("fingerprint", value, 71).lower()
    if not _FINGERPRINT_RE.fullmatch(result):
        raise ValueError("fingerprint must be a SHA-256 digest")
    return result if result.startswith("sha256:") else "sha256:" + result


def _strict_keys(value: Mapping[str, Any], allowed: Sequence[str], name: str) -> None:
    extras = set(value) - set(allowed)
    if extras:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(sorted(extras))}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_loads_strict(raw: str) -> Any:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=reject_duplicate)


@dataclasses.dataclass(frozen=True, order=True)
class LaneRef:
    name: str
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _lane_name(self.name))
        object.__setattr__(self, "version", _positive_int("lane version", self.version))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LaneRef":
        if not isinstance(value, Mapping):
            raise ValueError("lane must be an object")
        _strict_keys(value, ("name", "version"), "lane")
        return cls(value.get("name"), value.get("version"))


@dataclasses.dataclass(frozen=True)
class ActionBudgets:
    actions: int
    remote_writes: int
    agent_runs: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", _nonnegative_int("actions", self.actions))
        object.__setattr__(
            self, "remote_writes", _nonnegative_int("remote_writes", self.remote_writes)
        )
        object.__setattr__(self, "agent_runs", _nonnegative_int("agent_runs", self.agent_runs))

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionBudgets":
        if not isinstance(value, Mapping):
            raise ValueError("budgets must be an object")
        _strict_keys(value, ("actions", "remote_writes", "agent_runs"), "budgets")
        return cls(value.get("actions"), value.get("remote_writes"), value.get("agent_runs"))


@dataclasses.dataclass(frozen=True)
class AutonomousLaneDefinition:
    ref: LaneRef
    title: str
    evidence_kind: str
    capabilities: tuple[str, ...]
    budgets: ActionBudgets

    def __post_init__(self) -> None:
        if not isinstance(self.ref, LaneRef):
            raise ValueError("ref must be a LaneRef")
        object.__setattr__(self, "title", _text("title", self.title, 200))
        object.__setattr__(self, "evidence_kind", _identifier("evidence kind", self.evidence_kind))
        capabilities = tuple(sorted({_identifier("capability", item) for item in self.capabilities}))
        if not capabilities:
            raise ValueError("lane must grant at least one capability")
        object.__setattr__(self, "capabilities", capabilities)
        if not isinstance(self.budgets, ActionBudgets):
            raise ValueError("budgets must be ActionBudgets")

    @property
    def definition_digest(self) -> str:
        payload = {
            "ref": self.ref.to_dict(),
            "title": self.title,
            "evidence_kind": self.evidence_kind,
            "capabilities": list(self.capabilities),
            "budgets": self.budgets.to_dict(),
        }
        return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


BUILTIN_LANES: tuple[AutonomousLaneDefinition, ...] = (
    AutonomousLaneDefinition(
        ref=LaneRef(DETERMINISTIC_RETEST_LANE, DETERMINISTIC_RETEST_VERSION),
        title="Deterministic test-failure retest",
        evidence_kind="test_failure",
        capabilities=("request_retest",),
        budgets=ActionBudgets(actions=1, remote_writes=1, agent_runs=0),
    ),
)


@dataclasses.dataclass(frozen=True)
class RevisionIdentity:
    project: str
    patch_id: str
    change_number: int
    patchset: int
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _identifier("project", self.project))
        object.__setattr__(self, "patch_id", _identifier("patch_id", self.patch_id))
        object.__setattr__(self, "change_number", _positive_int("change_number", self.change_number))
        object.__setattr__(self, "patchset", _positive_int("patchset", self.patchset))
        revision = _text("revision", self.revision, 64).lower()
        if not _REVISION_RE.fullmatch(revision):
            raise ValueError("revision must be a full hexadecimal object ID")
        object.__setattr__(self, "revision", revision)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RevisionIdentity":
        if not isinstance(value, Mapping):
            raise ValueError("identity must be an object")
        fields = ("project", "patch_id", "change_number", "patchset", "revision")
        _strict_keys(value, fields, "identity")
        return cls(*(value.get(field) for field in fields))


@dataclasses.dataclass(frozen=True, order=True)
class NormalizedTestFailure:
    suite: str
    test: str
    fingerprint: str
    classification: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "suite", _identifier("suite", self.suite))
        object.__setattr__(self, "test", _identifier("test", self.test))
        object.__setattr__(self, "fingerprint", _fingerprint(self.fingerprint))
        classification = _text("classification", self.classification, 32).lower()
        if classification not in {"deterministic", "unknown", "product_bug"}:
            raise ValueError("unsupported failure classification")
        object.__setattr__(self, "classification", classification)

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NormalizedTestFailure":
        if not isinstance(value, Mapping):
            raise ValueError("failure must be an object")
        fields = ("suite", "test", "fingerprint", "classification")
        _strict_keys(value, fields, "failure")
        return cls(*(value.get(field) for field in fields))


@dataclasses.dataclass(frozen=True)
class LaneObservation:
    """Normalized facts bound to the observed and current exact revisions."""

    identity: RevisionIdentity
    current_identity: RevisionIdentity
    evidence_kind: str
    evidence_id: str
    evidence_fingerprint: str
    source: str
    change_state: str
    failures: tuple[NormalizedTestFailure, ...]
    standing_policy_authorized: bool = False
    primary_global_enabled: bool = False
    base_evaluation_permits: bool = False
    actions_used: int = 0
    non_maloo_minus_one: bool = False
    active_run_id: str | None = None
    consumed_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RevisionIdentity) or not isinstance(
            self.current_identity, RevisionIdentity
        ):
            raise ValueError("identity and current_identity must be RevisionIdentity values")
        if (self.identity.project, self.identity.patch_id, self.identity.change_number) != (
            self.current_identity.project,
            self.current_identity.patch_id,
            self.current_identity.change_number,
        ):
            raise ValueError("observed and current identities must refer to the same change")
        object.__setattr__(self, "evidence_kind", _identifier("evidence kind", self.evidence_kind))
        object.__setattr__(self, "evidence_id", _identifier("evidence_id", self.evidence_id))
        object.__setattr__(self, "evidence_fingerprint", _fingerprint(self.evidence_fingerprint))
        object.__setattr__(self, "source", _identifier("source", self.source).lower())
        state = _text("change_state", self.change_state, 32).lower()
        if state not in {"open", "merged", "abandoned"}:
            raise ValueError("unsupported change_state")
        object.__setattr__(self, "change_state", state)
        failures = tuple(sorted(self.failures))
        if any(not isinstance(item, NormalizedTestFailure) for item in failures):
            raise ValueError("failures must contain NormalizedTestFailure values")
        object.__setattr__(self, "failures", failures)
        object.__setattr__(
            self,
            "standing_policy_authorized",
            _bool("standing_policy_authorized", self.standing_policy_authorized),
        )
        object.__setattr__(
            self,
            "primary_global_enabled",
            _bool("primary_global_enabled", self.primary_global_enabled),
        )
        object.__setattr__(
            self,
            "base_evaluation_permits",
            _bool("base_evaluation_permits", self.base_evaluation_permits),
        )
        object.__setattr__(self, "actions_used", _nonnegative_int("actions_used", self.actions_used))
        object.__setattr__(
            self, "non_maloo_minus_one", _bool("non_maloo_minus_one", self.non_maloo_minus_one)
        )
        if self.active_run_id is not None:
            object.__setattr__(self, "active_run_id", _identifier("active_run_id", self.active_run_id))
        consumed = tuple(sorted({_text("consumed key", key, 80) for key in self.consumed_keys}))
        object.__setattr__(self, "consumed_keys", consumed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "current_identity": self.current_identity.to_dict(),
            "evidence_kind": self.evidence_kind,
            "evidence_id": self.evidence_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "source": self.source,
            "change_state": self.change_state,
            "failures": [item.to_dict() for item in self.failures],
            "standing_policy_authorized": self.standing_policy_authorized,
            "primary_global_enabled": self.primary_global_enabled,
            "base_evaluation_permits": self.base_evaluation_permits,
            "actions_used": self.actions_used,
            "non_maloo_minus_one": self.non_maloo_minus_one,
            "active_run_id": self.active_run_id,
            "consumed_keys": list(self.consumed_keys),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LaneObservation":
        if not isinstance(value, Mapping):
            raise ValueError("observation must be an object")
        fields = (
            "identity", "current_identity", "evidence_kind", "evidence_id",
            "evidence_fingerprint", "source", "change_state", "failures",
            "standing_policy_authorized", "primary_global_enabled",
            "base_evaluation_permits", "actions_used",
            "non_maloo_minus_one", "active_run_id", "consumed_keys",
        )
        _strict_keys(value, fields, "observation")
        failures = value.get("failures")
        if not isinstance(failures, list):
            raise ValueError("failures must be an array")
        consumed = value.get("consumed_keys", [])
        if not isinstance(consumed, list):
            raise ValueError("consumed_keys must be an array")
        return cls(
            identity=RevisionIdentity.from_dict(value.get("identity")),
            current_identity=RevisionIdentity.from_dict(value.get("current_identity")),
            evidence_kind=value.get("evidence_kind"),
            evidence_id=value.get("evidence_id"),
            evidence_fingerprint=value.get("evidence_fingerprint"),
            source=value.get("source"),
            change_state=value.get("change_state"),
            failures=tuple(NormalizedTestFailure.from_dict(item) for item in failures),
            standing_policy_authorized=value.get("standing_policy_authorized", False),
            primary_global_enabled=value.get("primary_global_enabled", False),
            base_evaluation_permits=value.get("base_evaluation_permits", False),
            actions_used=value.get("actions_used", 0),
            non_maloo_minus_one=value.get("non_maloo_minus_one", False),
            active_run_id=value.get("active_run_id"),
            consumed_keys=tuple(consumed),
        )


@dataclasses.dataclass(frozen=True, order=True)
class ProjectControl:
    project: str
    enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _identifier("project", self.project))
        object.__setattr__(self, "enabled", _bool("enabled", self.enabled))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectControl":
        if not isinstance(value, Mapping):
            raise ValueError("project control must be an object")
        _strict_keys(value, ("project", "enabled"), "project control")
        return cls(value.get("project"), value.get("enabled"))


@dataclasses.dataclass(frozen=True, order=True)
class PatchLaneControl:
    project: str
    patch_id: str
    lane: LaneRef
    enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _identifier("project", self.project))
        object.__setattr__(self, "patch_id", _identifier("patch_id", self.patch_id))
        if not isinstance(self.lane, LaneRef):
            raise ValueError("lane must be a LaneRef")
        object.__setattr__(self, "enabled", _bool("enabled", self.enabled))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "patch_id": self.patch_id,
            "lane": self.lane.to_dict(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PatchLaneControl":
        if not isinstance(value, Mapping):
            raise ValueError("patch control must be an object")
        _strict_keys(value, ("project", "patch_id", "lane", "enabled"), "patch control")
        return cls(
            project=value.get("project"),
            patch_id=value.get("patch_id"),
            lane=LaneRef.from_dict(value.get("lane")),
            enabled=value.get("enabled"),
        )


@dataclasses.dataclass(frozen=True)
class LaneControlSnapshot:
    generation: int = 0
    global_enabled: bool = False
    projects: tuple[ProjectControl, ...] = ()
    patches: tuple[PatchLaneControl, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _nonnegative_int("generation", self.generation))
        object.__setattr__(self, "global_enabled", _bool("global_enabled", self.global_enabled))
        projects = tuple(sorted(self.projects))
        patches = tuple(sorted(self.patches))
        if len(projects) > _MAX_CONTROLS or len(patches) > _MAX_CONTROLS:
            raise ValueError("too many autonomous-lane controls")
        if any(not isinstance(item, ProjectControl) for item in projects):
            raise ValueError("projects must contain ProjectControl values")
        if any(not isinstance(item, PatchLaneControl) for item in patches):
            raise ValueError("patches must contain PatchLaneControl values")
        if len({item.project for item in projects}) != len(projects):
            raise ValueError("duplicate project controls")
        if len({(item.project, item.patch_id) for item in patches}) != len(patches):
            raise ValueError("duplicate patch controls")
        object.__setattr__(self, "projects", projects)
        object.__setattr__(self, "patches", patches)

    def project_control(self, project: str) -> ProjectControl | None:
        project = _identifier("project", project)
        return next((item for item in self.projects if item.project == project), None)

    def patch_control(self, project: str, patch_id: str) -> PatchLaneControl | None:
        key = (_identifier("project", project), _identifier("patch_id", patch_id))
        return next((item for item in self.patches if (item.project, item.patch_id) == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROL_SCHEMA,
            "generation": self.generation,
            "global_enabled": self.global_enabled,
            "projects": [item.to_dict() for item in self.projects],
            "patches": [item.to_dict() for item in self.patches],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LaneControlSnapshot":
        if not isinstance(value, Mapping):
            raise ValueError("control document must be an object")
        fields = ("schema", "generation", "global_enabled", "projects", "patches")
        _strict_keys(value, fields, "control document")
        if value.get("schema") != CONTROL_SCHEMA:
            raise ValueError("unsupported autonomous-lane control schema")
        projects = value.get("projects")
        patches = value.get("patches")
        if not isinstance(projects, list) or not isinstance(patches, list):
            raise ValueError("projects and patches must be arrays")
        return cls(
            generation=value.get("generation"),
            global_enabled=value.get("global_enabled"),
            projects=tuple(ProjectControl.from_dict(item) for item in projects),
            patches=tuple(PatchLaneControl.from_dict(item) for item in patches),
        )


@dataclasses.dataclass(frozen=True)
class LaneDecision:
    eligible: bool
    code: str
    explanation: str
    lane: LaneRef | None
    definition_digest: str
    identity: RevisionIdentity
    evidence_id: str
    evidence_fingerprint: str
    decision_key: str
    capabilities: tuple[str, ...]
    budgets: ActionBudgets
    actions: tuple[str, ...]
    control_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DECISION_SCHEMA,
            "eligible": self.eligible,
            "code": self.code,
            "explanation": self.explanation,
            "lane": None if self.lane is None else self.lane.to_dict(),
            "definition_digest": self.definition_digest,
            "identity": self.identity.to_dict(),
            "evidence_id": self.evidence_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "decision_key": self.decision_key,
            "capabilities": list(self.capabilities),
            "budgets": self.budgets.to_dict(),
            "actions": list(self.actions),
            "control_generation": self.control_generation,
        }


def _definition(ref: LaneRef, definitions: Sequence[AutonomousLaneDefinition]) -> AutonomousLaneDefinition | None:
    return next((item for item in definitions if item.ref == ref), None)


def _decision_key(ref: LaneRef | None, observation: LaneObservation) -> str:
    payload = {
        "schema": DECISION_SCHEMA,
        "lane": None if ref is None else ref.to_dict(),
        "identity": observation.identity.to_dict(),
        "evidence_kind": observation.evidence_kind,
        "evidence_id": observation.evidence_id,
        "evidence_fingerprint": observation.evidence_fingerprint,
    }
    return "lane:" + hashlib.sha256(_canonical(payload)).hexdigest()


def decide_lane(
    observation: LaneObservation,
    controls: LaneControlSnapshot,
    *,
    definitions: Sequence[AutonomousLaneDefinition] = BUILTIN_LANES,
) -> LaneDecision:
    """Return an explainable, deterministic decision without taking action."""

    if not isinstance(observation, LaneObservation):
        raise ValueError("observation must be a LaneObservation")
    if not isinstance(controls, LaneControlSnapshot):
        raise ValueError("controls must be a LaneControlSnapshot")
    patch = controls.patch_control(observation.identity.project, observation.identity.patch_id)
    ref = None if patch is None else patch.lane
    definition = None if ref is None else _definition(ref, definitions)
    key = _decision_key(ref, observation)
    empty_budget = ActionBudgets(0, 0, 0)

    def reject(code: str, explanation: str) -> LaneDecision:
        return LaneDecision(
            False, code, explanation, ref,
            "unavailable" if definition is None else definition.definition_digest,
            observation.identity, observation.evidence_id,
            observation.evidence_fingerprint, key,
            () if definition is None else definition.capabilities,
            empty_budget if definition is None else definition.budgets,
            (), controls.generation,
        )

    if not controls.global_enabled:
        return reject("global_disabled", "Autonomous lanes are disabled globally.")
    project = controls.project_control(observation.identity.project)
    if project is None or not project.enabled:
        return reject("project_disabled", f"Autonomous lanes are disabled for {observation.identity.project}.")
    if patch is None:
        return reject("patch_not_enrolled", "This patch is not enrolled in an autonomous lane.")
    if not patch.enabled:
        return reject("patch_disabled", "The autonomous lane kill switch is set for this patch.")
    if definition is None:
        return reject("lane_unavailable", f"Lane {ref.name} version {ref.version} is not installed.")
    if not observation.standing_policy_authorized:
        return reject(
            "standing_policy_not_authorized",
            "The existing standing policy does not authorize automatic deterministic retests.",
        )
    if not observation.primary_global_enabled:
        return reject(
            "primary_global_disabled",
            "The primary automation gate is disabled; lane enrollment grants no authority.",
        )
    if observation.identity != observation.current_identity:
        return reject("stale_revision", "The evidence does not describe the current exact patchset revision.")
    if observation.change_state != "open":
        return reject("change_not_open", f"The Gerrit change is {observation.change_state}.")
    if observation.evidence_kind != definition.evidence_kind:
        return reject("wrong_evidence_kind", f"This lane only accepts {definition.evidence_kind} evidence.")
    if observation.source != "maloo":
        return reject("wrong_evidence_source", "This lane only accepts normalized Maloo evidence.")
    if observation.non_maloo_minus_one:
        return reject("human_review_required", "A non-Maloo -1 requires human review before test handling.")
    if observation.active_run_id is not None:
        return reject("active_run", f"Run {observation.active_run_id} already owns this patch.")
    if not observation.base_evaluation_permits:
        return reject(
            "base_policy_rejected",
            "The established deterministic retest evaluator did not produce one safe action.",
        )
    if observation.actions_used >= definition.budgets.actions:
        return reject(
            "action_budget_exhausted",
            "This lane has already used its one action for the exact revision.",
        )
    if not observation.failures:
        return reject("no_test_failures", "The evidence contains no failed tests.")
    nondeterministic = [item for item in observation.failures if item.classification != "deterministic"]
    if nondeterministic:
        names = ", ".join(f"{item.suite}/{item.test}" for item in nondeterministic[:3])
        return reject("not_deterministic", f"Not every failure is deterministic: {names}.")
    if key in observation.consumed_keys:
        return reject("already_consumed", "This exact lane, revision, and evidence snapshot was already handled.")

    return LaneDecision(
        True,
        "eligible",
        "All failures are deterministic and the exact open revision passes every kill switch.",
        ref,
        definition.definition_digest,
        observation.identity,
        observation.evidence_id,
        observation.evidence_fingerprint,
        key,
        definition.capabilities,
        definition.budgets,
        ("request_retest",),
        controls.generation,
    )


def dry_run(
    observations: Iterable[LaneObservation],
    controls: LaneControlSnapshot,
    *,
    definitions: Sequence[AutonomousLaneDefinition] = BUILTIN_LANES,
) -> tuple[LaneDecision, ...]:
    """Evaluate historical normalized observations without writing history."""

    return tuple(decide_lane(item, controls, definitions=definitions) for item in observations)


class LaneControlStore:
    """Private atomic JSON persistence with optimistic concurrency."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def load(self) -> LaneControlSnapshot:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._read_unlocked()

    def save(self, desired: LaneControlSnapshot) -> LaneControlSnapshot:
        if not isinstance(desired, LaneControlSnapshot):
            raise ValueError("desired must be a LaneControlSnapshot")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self._read_unlocked()
            if desired.generation != current.generation:
                raise AutonomousLaneConflict(
                    f"expected generation {desired.generation}, found {current.generation}"
                )
            saved = dataclasses.replace(desired, generation=current.generation + 1)
            self._write_unlocked(saved)
            return saved

    def set_global_enabled(self, enabled: bool, *, expected_generation: int) -> LaneControlSnapshot:
        current = self.load()
        if current.generation != expected_generation:
            raise AutonomousLaneConflict(
                f"expected generation {expected_generation}, found {current.generation}"
            )
        return self.save(dataclasses.replace(current, global_enabled=_bool("enabled", enabled)))

    def set_project_enabled(
        self, project: str, enabled: bool, *, expected_generation: int
    ) -> LaneControlSnapshot:
        current = self.load()
        if current.generation != expected_generation:
            raise AutonomousLaneConflict(
                f"expected generation {expected_generation}, found {current.generation}"
            )
        project = _identifier("project", project)
        projects = {item.project: item for item in current.projects}
        projects[project] = ProjectControl(project, enabled)
        return self.save(dataclasses.replace(current, projects=tuple(projects.values())))

    def set_patch_lane(
        self,
        project: str,
        patch_id: str,
        lane: LaneRef,
        enabled: bool,
        *,
        expected_generation: int,
    ) -> LaneControlSnapshot:
        current = self.load()
        if current.generation != expected_generation:
            raise AutonomousLaneConflict(
                f"expected generation {expected_generation}, found {current.generation}"
            )
        control = PatchLaneControl(project, patch_id, lane, enabled)
        patches = {(item.project, item.patch_id): item for item in current.patches}
        patches[(control.project, control.patch_id)] = control
        return self.save(dataclasses.replace(current, patches=tuple(patches.values())))

    def clear_project(self, project: str, *, expected_generation: int) -> LaneControlSnapshot:
        current = self.load()
        if current.generation != expected_generation:
            raise AutonomousLaneConflict(
                f"expected generation {expected_generation}, found {current.generation}"
            )
        project = _identifier("project", project)
        return self.save(dataclasses.replace(
            current, projects=tuple(item for item in current.projects if item.project != project),
        ))

    def clear_patch_lane(
        self, project: str, patch_id: str, *, expected_generation: int
    ) -> LaneControlSnapshot:
        current = self.load()
        if current.generation != expected_generation:
            raise AutonomousLaneConflict(
                f"expected generation {expected_generation}, found {current.generation}"
            )
        key = (_identifier("project", project), _identifier("patch_id", patch_id))
        return self.save(dataclasses.replace(
            current,
            patches=tuple(
                item for item in current.patches if (item.project, item.patch_id) != key
            ),
        ))

    def _read_unlocked(self) -> LaneControlSnapshot:
        if not self.path.exists():
            return LaneControlSnapshot()
        try:
            if self.path.stat().st_size > _MAX_CONTROL_BYTES:
                raise AutonomousLaneError("autonomous-lane control file is too large")
            value = _json_loads_strict(self.path.read_text(encoding="utf-8"))
            return LaneControlSnapshot.from_dict(value)
        except AutonomousLaneError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AutonomousLaneError(f"invalid autonomous-lane control file: {exc}") from exc

    def _write_unlocked(self, snapshot: LaneControlSnapshot) -> None:
        encoded = _canonical(snapshot.to_dict()) + b"\n"
        if len(encoded) > _MAX_CONTROL_BYTES:
            raise AutonomousLaneError("autonomous-lane control file is too large")
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


@dataclasses.dataclass(frozen=True)
class DecisionAuditRecord:
    record_id: str
    recorded_at: str
    observation: LaneObservation
    controls: LaneControlSnapshot
    decision: LaneDecision

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": AUDIT_SCHEMA,
            "record_id": self.record_id,
            "recorded_at": self.recorded_at,
            "observation": self.observation.to_dict(),
            "controls": self.controls.to_dict(),
            "decision": self.decision.to_dict(),
        }


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    record_id: str
    matched: bool
    recorded: LaneDecision
    replayed: LaneDecision


class LaneDecisionHistory:
    """Append-only private JSONL audit history for decisions and replay."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def append(
        self,
        observation: LaneObservation,
        controls: LaneControlSnapshot,
        decision: LaneDecision,
        *,
        recorded_at: datetime | None = None,
    ) -> DecisionAuditRecord:
        expected = decide_lane(observation, controls)
        if expected != decision:
            raise ValueError("decision does not match its observation and control snapshot")
        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        digest_payload = {
            "observation": observation.to_dict(),
            "controls": controls.to_dict(),
            "decision": decision.to_dict(),
        }
        record = DecisionAuditRecord(
            "lane-audit:" + hashlib.sha256(_canonical(digest_payload)).hexdigest(),
            timestamp_text,
            observation,
            controls,
            decision,
        )
        encoded = _canonical(record.to_dict()) + b"\n"
        if len(encoded) > _MAX_AUDIT_LINE_BYTES:
            raise AutonomousLaneError("autonomous-lane audit record is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as source:
                    for number, line in enumerate(source, 1):
                        if not line.strip():
                            continue
                        existing = self._decode_record(_json_loads_strict(line), number)
                        if existing.record_id == record.record_id:
                            return existing
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def list(self) -> tuple[DecisionAuditRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[DecisionAuditRecord] = []
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                with self.path.open("r", encoding="utf-8") as source:
                    for number, line in enumerate(source, 1):
                        if len(line.encode("utf-8")) > _MAX_AUDIT_LINE_BYTES:
                            raise AutonomousLaneError(f"audit line {number} is too large")
                        if not line.strip():
                            continue
                        records.append(self._decode_record(_json_loads_strict(line), number))
            except AutonomousLaneError:
                raise
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise AutonomousLaneError(f"invalid autonomous-lane audit history: {exc}") from exc
        return tuple(records)

    def replay(self) -> tuple[ReplayResult, ...]:
        """Recompute every recorded decision using its original config snapshot."""

        return tuple(
            ReplayResult(
                record.record_id,
                record.decision == decide_lane(record.observation, record.controls),
                record.decision,
                decide_lane(record.observation, record.controls),
            )
            for record in self.list()
        )

    @staticmethod
    def _decode_record(value: Mapping[str, Any], line: int) -> DecisionAuditRecord:
        if not isinstance(value, Mapping):
            raise ValueError(f"audit line {line} must be an object")
        fields = ("schema", "record_id", "recorded_at", "observation", "controls", "decision")
        _strict_keys(value, fields, f"audit line {line}")
        if value.get("schema") != AUDIT_SCHEMA:
            raise ValueError(f"unsupported audit schema on line {line}")
        observation = LaneObservation.from_dict(value.get("observation"))
        controls = LaneControlSnapshot.from_dict(value.get("controls"))
        decision_value = value.get("decision")
        if not isinstance(decision_value, Mapping):
            raise ValueError("decision must be an object")
        expected = decide_lane(observation, controls)
        # The canonical serialized decision is the safest decoder: this both
        # rejects missing/extra/type-coerced fields and detects tampering.
        if decision_value != expected.to_dict():
            raise ValueError(f"recorded decision on line {line} does not replay")
        record_id = _text("record_id", value.get("record_id"), 80)
        payload = {
            "observation": observation.to_dict(),
            "controls": controls.to_dict(),
            "decision": expected.to_dict(),
        }
        wanted = "lane-audit:" + hashlib.sha256(_canonical(payload)).hexdigest()
        if record_id != wanted:
            raise ValueError(f"record_id mismatch on line {line}")
        recorded_at = _text("recorded_at", value.get("recorded_at"), 64)
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid recorded_at on line {line}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"recorded_at on line {line} lacks a timezone")
        return DecisionAuditRecord(record_id, recorded_at, observation, controls, expected)
