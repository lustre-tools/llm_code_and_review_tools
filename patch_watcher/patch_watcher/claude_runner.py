"""Patch Watcher's native Claude Code transport.

The public :class:`ClaudeRunner` is deliberately small.  A runner starts one
private per-run host process; that host owns Claude's streaming stdin/stdout,
the event log, and a mode-0600 Unix control socket.  Consequently a restarted
Patch Watcher controller can adopt the still-running host without duplicating
the Claude session or attempting to reattach raw pipes.

This module has no dependency on claude-voice-control.  It only uses Python's
standard library and Claude Code's documented stream-json interface.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "patch-watcher-claude-runner/v1"
MAX_CONTROL_BYTES = 1024 * 1024
MAX_GUIDANCE_CHARS = 32 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENT_TAIL = 200
DEFAULT_EVENT_MEMORY = 512
DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
READ_ONLY_REPORT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "patch-watcher-read-only-report/v1"},
        "state": {"enum": ["complete", "needs_input", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "findings": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["schema", "state", "summary", "findings"],
    "allOf": [
        {
            "if": {"properties": {"state": {"const": "needs_input"}}, "required": ["state"]},
            "then": {"required": ["question"]},
        }
    ],
}
ENGINEERING_REPORT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "patch-watcher-engineering-report/v1"},
        "state": {
            "enum": [
                "complete", "acknowledged", "needs_input", "failed",
                "resource_exhausted",
            ],
            # `resource_exhausted` used to appear here as a bare enum value, so
            # the only thing distinguishing it from `failed` was its name. The
            # controller treats them very differently -- it alerts an operator
            # and does not count the patch as judged -- so the schema says
            # which is which, and the run instructions repeat it.
            "description": (
                "complete: the task finished. acknowledged: you triaged every "
                "target and are deliberately leaving some of them to a human, "
                "with no question outstanding -- use this rather than inventing "
                "work you do not believe in. needs_input: one precise human "
                "decision is required, and you are waiting for the answer. "
                "failed: the work did not succeed, or "
                "the answer you reached is a negative one. resource_exhausted: "
                "this host could not supply the LTVM capacity the work needed."
            ),
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "changed_files": {
            "type": "array", "maxItems": 200,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "validation_requests": {
            "type": "array", "maxItems": 50,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "target": {"type": "string", "minLength": 1, "maxLength": 200},
                    "evidence_role": {
                        "enum": ["test", "build", "diagnostic", "other"]
                    },
                    "argv": {
                        "type": "array", "minItems": 1, "maxItems": 100,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                },
                "required": ["name", "target", "argv"],
            },
        },
        "review_mode": {"enum": ["simple", "all"]},
        "review_snapshot_sha256": {
            "type": "string", "pattern": "^[0-9a-f]{64}$"
        },
        "comment_results": {
            "type": "array", "maxItems": 200,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "comment_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "assessment": {"enum": ["simple", "nontrivial", "ambiguous"]},
                    "disposition": {
                        "enum": ["addressed", "reply_draft", "needs_human", "not_attempted"]
                    },
                    "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "reply_draft": {"type": "string", "maxLength": 4000},
                    "changed_files": {
                        "type": "array", "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                },
                "required": ["comment_id", "assessment", "disposition", "summary", "changed_files"],
            },
        },
        "jenkins_snapshot_sha256": {
            "type": "string", "pattern": "^[0-9a-f]{64}$"
        },
        "jenkins_resolution": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "build_id": {"type": "string", "minLength": 1, "maxLength": 500},
                "classification": {
                    "enum": [
                        "patch_caused_fixed", "infrastructure", "transient",
                        "unrelated", "ambiguous", "needs_human",
                    ]
                },
                "diagnosis": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["build_id", "classification", "diagnosis"],
        },
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["schema", "state", "summary", "changed_files", "validation_requests"],
    "allOf": [{
        "if": {"properties": {"state": {"const": "needs_input"}}, "required": ["state"]},
        "then": {"required": ["question"]},
    }],
}
UNKNOWN_FAILURE_RECOMMENDATIONS = frozenset(
    {"known_failure", "transient", "patch_caused", "needs_human", "inconclusive"}
)
UNKNOWN_FAILURE_REPORT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "patch-watcher-unknown-failure-report/v1"},
        "state": {"enum": ["complete", "needs_input", "failed"]},
        "recommendation": {"enum": sorted(UNKNOWN_FAILURE_RECOMMENDATIONS)},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "evidence_references": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "evidence_ref": {"type": "string", "minLength": 1, "maxLength": 256},
                    "locator": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "supports": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["evidence_ref", "locator", "supports"],
            },
        },
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["schema", "state", "recommendation", "summary", "evidence_references"],
    "allOf": [
        {
            "if": {"properties": {"state": {"const": "needs_input"}}, "required": ["state"]},
            "then": {"required": ["question"]},
        }
    ],
}


class ClaudeRunnerError(RuntimeError):
    """Base class for typed runner failures."""


class RunnerProtocolError(ClaudeRunnerError):
    """The host or Claude stream returned invalid protocol data."""


class RunnerAdoptionError(ClaudeRunnerError):
    """A persisted runner handle could not safely be adopted."""


class RunnerIdentityError(ClaudeRunnerError):
    """A process identity changed, normally because a PID was reused."""


class RunnerStateError(ClaudeRunnerError):
    """An operation is not valid in the current runner state."""


@dataclasses.dataclass(frozen=True)
class ProcessIdentity:
    """PID identity strong enough to reject accidental PID reuse."""

    pid: int
    start_token: str
    process_group_id: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessIdentity:
        return cls(
            pid=int(value["pid"]),
            start_token=str(value["start_token"]),
            process_group_id=int(value["process_group_id"]),
        )


@dataclasses.dataclass(frozen=True)
class ReadOnlyRunSpec:
    """Everything the transport needs to launch one bounded conversation.

    The historical class name is retained for persisted launch specs.
    ``read_only`` reads local source and evidence.  ``full`` is the working
    agent profile: an unrestricted tool set including Bash, in the same
    environment a developer has on this host -- ltvm, the LLM tools, and a
    Lustre checkout.  What a ``full`` agent may do is stated in its prompt,
    not enforced here.
    """

    run_id: str
    session_id: str
    cwd: str
    runtime_dir: str
    prompt: str
    name: str = ""
    model: str = ""
    effort: str = ""
    claude_binary: str = "claude"
    report_kind: str = "read_only"
    capability_profile: str = "read_only"
    mcp_config_json: str = "{}"

    def validate(self) -> None:
        if not RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("run_id must be a short filesystem-safe identifier")
        try:
            uuid.UUID(self.session_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("session_id must be a UUID") from exc
        cwd = Path(self.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError("cwd must be an existing directory")
        if cwd == Path.home().resolve():
            raise ValueError("read-only workers may not use the home directory as cwd")
        runtime = Path(self.runtime_dir).expanduser().resolve()
        if runtime == Path("/") or runtime == Path.home().resolve():
            raise ValueError("runtime_dir must be a dedicated private run directory")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(self.prompt) > 256 * 1024:
            raise ValueError("prompt is too large")
        if Path(self.claude_binary).name != "claude":
            raise ValueError("claude_binary must identify the Claude Code executable")
        if self.effort and self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported effort")
        if self.report_kind not in {"read_only", "unknown_failure_research", "engineering"}:
            raise ValueError("unsupported report kind")
        if self.capability_profile not in {"read_only", "full"}:
            raise ValueError("unsupported capability profile")
        if self.report_kind == "engineering" and self.capability_profile != "full":
            raise ValueError("engineering reports require the full capability profile")
        if self.capability_profile == "full" and self.report_kind != "engineering":
            raise ValueError("the full capability profile requires an engineering report")
        try:
            mcp_config = json.loads(self.mcp_config_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("mcp_config_json must be valid JSON") from exc
        if not isinstance(mcp_config, Mapping):
            raise ValueError("mcp_config_json must contain an object")
        # No profile brokers MCP: the only server this ever accepted was
        # pw_ltvm, whose module was deleted.
        if mcp_config != {}:
            raise ValueError("MCP is unavailable to every capability profile")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReadOnlyRunSpec:
        fields = {field.name for field in dataclasses.fields(cls)}
        unknown = set(value) - fields
        if unknown:
            raise ValueError("unknown run spec fields: " + ", ".join(sorted(unknown)))
        spec = cls(**{key: str(item) for key, item in value.items()})
        spec.validate()
        return spec


@dataclasses.dataclass(frozen=True)
class RunnerHandle:
    """Durable coordinates needed to reconnect after controller restart."""

    run_id: str
    session_id: str
    socket_path: str
    event_log_path: str
    state_path: str
    host_identity: ProcessIdentity
    claude_identity: ProcessIdentity | None = None
    protocol: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunnerHandle:
        host_identity = ProcessIdentity.from_dict(value["host_identity"])
        raw_claude = value.get("claude_identity")
        return cls(
            run_id=str(value["run_id"]),
            session_id=str(value["session_id"]),
            socket_path=str(value["socket_path"]),
            event_log_path=str(value["event_log_path"]),
            state_path=str(value["state_path"]),
            host_identity=host_identity,
            claude_identity=(ProcessIdentity.from_dict(raw_claude) if raw_claude else None),
            protocol=str(value.get("protocol", PROTOCOL_VERSION)),
        )


@dataclasses.dataclass(frozen=True)
class RunnerEvent:
    cursor: int
    timestamp: float
    type: str
    payload: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunnerEvent:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        return cls(
            cursor=int(value["cursor"]),
            timestamp=float(value["timestamp"]),
            type=str(value["type"]),
            payload=dict(payload),
        )


@dataclasses.dataclass(frozen=True)
class RunnerSnapshot:
    handle: RunnerHandle
    state: str
    turn_state: str
    started_at: float
    last_event_at: float
    last_cursor: int
    last_message: str
    returncode: int | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunnerSnapshot:
        return cls(
            handle=RunnerHandle.from_dict(value["handle"]),
            state=str(value["state"]),
            turn_state=str(value["turn_state"]),
            started_at=float(value["started_at"]),
            last_event_at=float(value["last_event_at"]),
            last_cursor=int(value["last_cursor"]),
            last_message=str(value.get("last_message", "")),
            returncode=(None if value.get("returncode") is None else int(value["returncode"])),
        )


@dataclasses.dataclass(frozen=True)
class GuidanceDelivery:
    delivery_id: str
    state: str
    duplicate: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GuidanceDelivery:
        return cls(str(value["delivery_id"]), str(value["state"]), bool(value.get("duplicate")))


@dataclasses.dataclass(frozen=True)
class ReconciliationProbe:
    alive: bool
    identity_match: bool
    control_reachable: bool
    adoptable: bool
    reason: str
    snapshot: RunnerSnapshot | None = None


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # os.fdopen only adopts the descriptor once it returns; if it raises the
        # raw descriptor is still open and nothing else refers to it.  The
        # finally below used to unlink the temporary file and nothing more, so
        # every failure here burned a descriptor -- and the host calls this on
        # every state persist, which is once per stream event.  Clear the
        # sentinel only after the wrapper owns the descriptor.
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _default_process_identity(pid: int) -> ProcessIdentity:
    if pid <= 0:
        raise ProcessLookupError(pid)
    os.kill(pid, 0)
    proc_stat = Path("/proc") / str(pid) / "stat"
    if proc_stat.exists():
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) < 22:
            raise ProcessLookupError(pid)
        start_token = "proc:" + fields[21]
    else:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        start = result.stdout.strip()
        if result.returncode or not start:
            raise ProcessLookupError(pid)
        start_token = "ps:" + start
    return ProcessIdentity(pid=pid, start_token=start_token, process_group_id=os.getpgid(pid))


def _same_identity(expected: ProcessIdentity, actual: ProcessIdentity) -> bool:
    return expected.pid == actual.pid and expected.start_token == actual.start_token


# Patch Watcher's private copies of the service-tool credentials, and the
# variable each CLI reads to find them.  The agent runs `gerrit`, `maloo`,
# `jenkins` and `jira` itself -- that is the whole premise of giving it the
# environment CLAUDE.md describes -- so these decide which identity those calls
# use.  Pointing each tool at Patch Watcher's own file means rotating a
# credential here cannot change what the same command does from the operator's
# shell, and an operator rotating theirs cannot silently change what agents do.
#
# A file that does not exist is simply not pointed at: the tools fail loudly on
# a dangling pointer (by design -- falling back would run with whatever
# credentials were lying around), so an unconfigured tool must keep its default
# lookup rather than be handed a path to nothing.
AGENT_TOOL_CREDENTIALS = (
    ("GERRIT_CLI_ENV_FILE", "gerrit.env"),
    ("MALOO_TOOL_ENV_FILE", "maloo.env"),
    ("JENKINS_TOOL_ENV_FILE", "jenkins.env"),
    ("JIRA_TOOL_CONFIG", "jira.json"),
)
PRIVATE_CONFIG_DIR = Path.home() / ".config" / "patch-watcher"


def _tool_credential_pointers(
    config_dir: Path | None = None,
) -> dict[str, str]:
    """Map each service CLI to Patch Watcher's own credential file."""
    directory = config_dir if config_dir is not None else PRIVATE_CONFIG_DIR
    pointers = {}
    for variable, filename in AGENT_TOOL_CREDENTIALS:
        candidate = directory / filename
        if candidate.is_file():
            pointers[variable] = str(candidate)
    return pointers


def _safe_environment(
    source: Mapping[str, str] | None = None,
    *,
    capability_profile: str = "read_only",
    config_dir: Path | None = None,
) -> dict[str, str]:
    """Build the child environment for one run.

    Bounded profiles have ambient service-write credentials removed while model
    auth is preserved.  The ``full`` profile deliberately inherits the ambient
    environment unchanged: its whole point is that the agent works with the same
    real credentials a developer has.  Note that the LLM tools read their
    credentials from files under ``~/.config`` rather than the environment, and
    HOME is never overridden, so those are reachable from every profile.
    """

    environment = dict(source if source is not None else os.environ)
    # Applied to every profile.  A bounded run cannot shell out at all (its
    # tool allowlist is Read/Glob/Grep), so this changes nothing for it; making
    # the pointers unconditional keeps one answer to "which credentials does an
    # agent use" instead of one per profile.
    environment.update(_tool_credential_pointers(config_dir))
    if capability_profile == "full":
        environment["PATCH_WATCHER_CAPABILITY_PROFILE"] = capability_profile
        return environment
    protected_model_keys = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",  # Claude may be configured through Bedrock.
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    service_markers = ("GERRIT", "MALOO", "JENKINS", "JIRA", "JANITOR", "GITHUB", "GITLAB")
    secret_suffixes = ("_TOKEN", "_PASSWORD", "_PASS", "_SECRET", "_API_KEY")
    pointer_keys = {variable for variable, _ in AGENT_TOOL_CREDENTIALS}
    for key in list(environment):
        upper = key.upper()
        if upper in protected_model_keys:
            continue
        # The pointers name a FILE, never a secret, and every one of them
        # matches a service marker -- stripping them here would silently send a
        # bounded run back to the operator's own credentials.
        if upper in pointer_keys:
            continue
        if any(marker in upper for marker in service_markers) or upper.endswith(secret_suffixes):
            environment.pop(key, None)
    environment["CLAUDE_CODE_SAFE_MODE"] = "1"
    if capability_profile != "read_only":
        raise ValueError("unsupported capability profile")

    environment["PATCH_WATCHER_CAPABILITY_PROFILE"] = capability_profile
    return environment


# Keys the report schemas carry that cannot survive the trip to the model.
# The schema given to --json-schema becomes a tool's input_schema, and:
#
#   $schema  the CLI validates --json-schema with a validator that has only
#            draft-07 registered, and refused ours outright -- "no schema with
#            key or ref https://json-schema.org/draft/2020-12/schema" -- so
#            claude exited 1 before reading a message.
#   allOf    "API Error: 400 tools.N.custom.input_schema: input_schema does
#            not support oneOf, allOf, or anyOf at the top level".
#
# Both are dropped on the way out only.  The schema we validate reports
# against keeps them, and the rule the allOf expresses -- a needs_input report
# must carry a question -- is enforced in validate_engineering_report and
# validate_read_only_report, which is what actually rejects a bad report.
_CLI_UNSUPPORTED_SCHEMA_KEYS = ("$schema", "allOf")


def _cli_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The report schema as the CLI and the API will accept it."""
    return {
        key: value
        for key, value in schema.items()
        if key not in _CLI_UNSUPPORTED_SCHEMA_KEYS
    }


def build_read_only_claude_command(spec: ReadOnlyRunSpec) -> list[str]:
    """Return a shell-free command with the profile's exact bounded tools."""

    spec.validate()
    report_schema = {
        "unknown_failure_research": UNKNOWN_FAILURE_REPORT_SCHEMA,
        "engineering": ENGINEERING_REPORT_SCHEMA,
        "read_only": READ_ONLY_REPORT_SCHEMA,
    }[spec.report_kind]
    tools = "" if spec.capability_profile == "full" else "Read,Glob,Grep"
    command = [
        spec.claude_binary,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        # Required with --print and stream-json output; without it the CLI
        # exits 1 before reading a single message, which read downstream as
        # "the control socket became unreachable".
        "--verbose",
        "--replay-user-messages",
        "--session-id",
        spec.session_id,
        "--no-chrome",
        "--permission-mode",
        "bypassPermissions" if spec.capability_profile == "full" else "dontAsk",
        "--json-schema",
        json.dumps(_cli_json_schema(report_schema), sort_keys=True, separators=(",", ":")),
    ]
    if spec.capability_profile != "full":
        # Bounded profiles keep the tool allowlist and the hardening flags.
        # "full" deliberately runs an unrestricted session: its boundary is the
        # prompt and the checkout, not a capability grant.
        command[command.index("--no-chrome") + 1 : command.index("--no-chrome") + 1] = [
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        # The spec's "no servers" is {}, which the validator enforces; the CLI
        # spells the same thing {"mcpServers": {}} and rejects a bare {} with
        # "Invalid MCP configuration: mcpServers: Invalid input", exiting 1
        # before reading a message.  Every read-only run -- investigation and
        # failure research, the whole read-only half of the ladder -- died
        # that way, reported only as "host_process_missing".
        command.extend([
            "--tools", tools,
            "--mcp-config", json.dumps(
                {"mcpServers": json.loads(spec.mcp_config_json)},
                sort_keys=True, separators=(",", ":"),
            ),
        ])
    if spec.name:
        command.extend(["--name", spec.name])
    if spec.model:
        command.extend(["--model", spec.model])
    if spec.effort:
        command.extend(["--effort", spec.effort])
    return command


def validate_engineering_report(value: Any) -> Mapping[str, Any]:
    """Validate the source-edit worker's bounded, non-authoritative report."""

    if not isinstance(value, Mapping):
        raise RunnerProtocolError("engineering report must be an object")
    allowed = {
        "schema", "state", "summary", "changed_files", "validation_requests", "question",
        "review_mode", "review_snapshot_sha256", "comment_results",
        "jenkins_snapshot_sha256", "jenkins_resolution",
    }
    unknown = set(value) - allowed
    if unknown:
        raise RunnerProtocolError(
            "engineering report has unknown fields: " + ", ".join(sorted(unknown))
        )
    if value.get("schema") != "patch-watcher-engineering-report/v1":
        raise RunnerProtocolError("engineering report has unsupported schema")
    state = value.get("state")
    if state not in {
        "complete", "acknowledged", "needs_input", "failed", "resource_exhausted",
    }:
        raise RunnerProtocolError("engineering report has invalid state")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise RunnerProtocolError("engineering report summary is invalid")
    changed_files = value.get("changed_files")
    if not isinstance(changed_files, list) or len(changed_files) > 200:
        raise RunnerProtocolError("engineering report changed_files is invalid")
    normalized_files = []
    for item in changed_files:
        if not isinstance(item, str) or not item.strip() or len(item) > 1000 or "\x00" in item:
            raise RunnerProtocolError("engineering report contains an invalid changed file")
        path = item.strip()
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise RunnerProtocolError("engineering report changed files must be checkout-relative")
        normalized_files.append(path)
    requests = value.get("validation_requests")
    if not isinstance(requests, list) or len(requests) > 50:
        raise RunnerProtocolError("engineering report validation_requests is invalid")
    normalized_requests = []
    for request in requests:
        if (
            not isinstance(request, Mapping)
            or not {"name", "target", "argv"}.issubset(request)
            or set(request) - {"name", "target", "argv", "evidence_role"}
        ):
            raise RunnerProtocolError("engineering validation request fields are invalid")
        name, target, argv = request.get("name"), request.get("target"), request.get("argv")
        evidence_role = request.get("evidence_role", "other")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise RunnerProtocolError("engineering validation request name is invalid")
        if not isinstance(target, str) or not target.strip() or len(target) > 200:
            raise RunnerProtocolError("engineering validation request target is invalid")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 100:
            raise RunnerProtocolError("engineering validation request argv is invalid")
        if evidence_role not in {"test", "build", "diagnostic", "other"}:
            raise RunnerProtocolError("engineering validation evidence role is invalid")
        normalized_argv = []
        for argument in argv:
            if not isinstance(argument, str) or not argument or len(argument) > 4000 or "\x00" in argument:
                raise RunnerProtocolError("engineering validation request argument is invalid")
            normalized_argv.append(argument)
        normalized_requests.append(
            {
                "name": name.strip(), "target": target.strip(),
                "argv": normalized_argv, "evidence_role": evidence_role,
            }
        )
    question = value.get("question")
    if question is not None and (
        not isinstance(question, str) or not question.strip() or len(question) > 2000
    ):
        raise RunnerProtocolError("engineering report question is invalid")
    if state == "needs_input" and question is None:
        raise RunnerProtocolError("needs_input engineering report requires question")
    review_mode = value.get("review_mode")
    snapshot_digest = value.get("review_snapshot_sha256")
    comment_results = value.get("comment_results")
    review_fields = (review_mode, snapshot_digest, comment_results)
    if any(item is not None for item in review_fields):
        if review_mode not in {"simple", "all"}:
            raise RunnerProtocolError("engineering report review_mode is invalid")
        if not isinstance(snapshot_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", snapshot_digest
        ):
            raise RunnerProtocolError("engineering report review snapshot is invalid")
        if not isinstance(comment_results, list) or len(comment_results) > 200:
            raise RunnerProtocolError("engineering report comment_results is invalid")
        normalized_comments = []
        seen_comment_ids = set()
        for item in comment_results:
            if not isinstance(item, Mapping) or set(item) - {
                "comment_id", "assessment", "disposition", "summary", "reply_draft", "changed_files",
            } or not {"comment_id", "assessment", "disposition", "summary", "changed_files"}.issubset(item):
                raise RunnerProtocolError("engineering comment result fields are invalid")
            comment_id = item.get("comment_id")
            assessment = item.get("assessment")
            disposition = item.get("disposition")
            item_summary = item.get("summary")
            reply_draft = item.get("reply_draft")
            item_files = item.get("changed_files")
            if (
                not isinstance(comment_id, str) or not comment_id.strip()
                or len(comment_id) > 256 or comment_id in seen_comment_ids
            ):
                raise RunnerProtocolError("engineering comment id is invalid")
            if disposition not in {
                "addressed", "reply_draft", "needs_human", "not_attempted",
            }:
                raise RunnerProtocolError("engineering comment disposition is invalid")
            if assessment not in {"simple", "nontrivial", "ambiguous"}:
                raise RunnerProtocolError("engineering comment assessment is invalid")
            if not isinstance(item_summary, str) or not item_summary.strip() or len(item_summary) > 2000:
                raise RunnerProtocolError("engineering comment summary is invalid")
            if reply_draft is not None and (
                not isinstance(reply_draft, str) or len(reply_draft) > 4000 or "\x00" in reply_draft
            ):
                raise RunnerProtocolError("engineering reply draft is invalid")
            if not isinstance(item_files, list) or len(item_files) > 50:
                raise RunnerProtocolError("engineering comment changed_files is invalid")
            normalized_item_files = []
            for item_file in item_files:
                if (
                    not isinstance(item_file, str) or not item_file.strip()
                    or len(item_file) > 1000 or "\x00" in item_file
                    or Path(item_file).is_absolute() or ".." in Path(item_file).parts
                ):
                    raise RunnerProtocolError("engineering comment changed file is invalid")
                normalized_item_files.append(item_file.strip())
            seen_comment_ids.add(comment_id)
            normalized_item = {
                "comment_id": comment_id,
                "assessment": assessment,
                "disposition": disposition,
                "summary": item_summary.strip(),
                "changed_files": normalized_item_files,
            }
            if reply_draft is not None:
                normalized_item["reply_draft"] = reply_draft
            normalized_comments.append(normalized_item)
    jenkins_digest = value.get("jenkins_snapshot_sha256")
    jenkins_resolution = value.get("jenkins_resolution")
    jenkins_fields = (jenkins_digest, jenkins_resolution)
    if any(item is not None for item in jenkins_fields):
        if not isinstance(jenkins_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", jenkins_digest
        ):
            raise RunnerProtocolError("engineering report Jenkins snapshot is invalid")
        if not isinstance(jenkins_resolution, Mapping) or set(jenkins_resolution) != {
            "build_id", "classification", "diagnosis",
        }:
            raise RunnerProtocolError("engineering report Jenkins resolution fields are invalid")
        build_id = jenkins_resolution.get("build_id")
        classification = jenkins_resolution.get("classification")
        diagnosis = jenkins_resolution.get("diagnosis")
        if not isinstance(build_id, str) or not build_id.strip() or len(build_id) > 500:
            raise RunnerProtocolError("engineering report Jenkins build ID is invalid")
        if classification not in {
            "patch_caused_fixed", "infrastructure", "transient", "unrelated",
            "ambiguous", "needs_human",
        }:
            raise RunnerProtocolError("engineering report Jenkins classification is invalid")
        if not isinstance(diagnosis, str) or not diagnosis.strip() or len(diagnosis) > 4000:
            raise RunnerProtocolError("engineering report Jenkins diagnosis is invalid")
    normalized: dict[str, Any] = {
        "schema": value["schema"], "state": state, "summary": summary.strip(),
        "changed_files": normalized_files, "validation_requests": normalized_requests,
    }
    if question is not None:
        normalized["question"] = question.strip()
    if any(item is not None for item in review_fields):
        normalized["review_mode"] = review_mode
        normalized["review_snapshot_sha256"] = snapshot_digest
        normalized["comment_results"] = normalized_comments
    if any(item is not None for item in jenkins_fields):
        normalized["jenkins_snapshot_sha256"] = jenkins_digest
        normalized["jenkins_resolution"] = {
            "build_id": build_id.strip(),
            "classification": classification,
            "diagnosis": diagnosis.strip(),
        }
    return normalized


def validate_read_only_report(value: Any) -> Mapping[str, Any]:
    """Validate and normalize a bounded read-only investigation report.

    Claude also receives the equivalent JSON Schema.  This independent check is
    required because process output is evidence, never workflow authority.
    """

    if not isinstance(value, Mapping):
        raise RunnerProtocolError("worker report must be an object")
    allowed = {"schema", "state", "summary", "findings", "question"}
    unknown = set(value) - allowed
    if unknown:
        raise RunnerProtocolError("worker report has unknown fields: " + ", ".join(sorted(unknown)))
    if value.get("schema") != "patch-watcher-read-only-report/v1":
        raise RunnerProtocolError("worker report has unsupported schema")
    state = value.get("state")
    if state not in {"complete", "needs_input", "failed"}:
        raise RunnerProtocolError("worker report has invalid state")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise RunnerProtocolError("worker report summary is invalid")
    findings = value.get("findings")
    if not isinstance(findings, list) or len(findings) > 50:
        raise RunnerProtocolError("worker report findings are invalid")
    for finding in findings:
        if not isinstance(finding, str) or not finding.strip() or len(finding) > 4000:
            raise RunnerProtocolError("worker report contains an invalid finding")
    question = value.get("question")
    if question is not None and (
        not isinstance(question, str) or not question.strip() or len(question) > 2000
    ):
        raise RunnerProtocolError("worker report question is invalid")
    if state == "needs_input" and question is None:
        raise RunnerProtocolError("needs_input worker report requires question")
    normalized: dict[str, Any] = {
        "schema": value["schema"],
        "state": state,
        "summary": summary.strip(),
        "findings": [finding.strip() for finding in findings],
    }
    if question is not None:
        normalized["question"] = question.strip()
    return normalized


def validate_unknown_failure_report(value: Any) -> Mapping[str, Any]:
    """Validate a bounded unknown-failure research report.

    The workflow controller separately verifies that every evidence reference
    names a record captured in the immutable request bundle.
    """

    if not isinstance(value, Mapping):
        raise RunnerProtocolError("unknown-failure report must be an object")
    allowed = {
        "schema", "state", "recommendation", "summary",
        "evidence_references", "question",
    }
    unknown = set(value) - allowed
    if unknown:
        raise RunnerProtocolError(
            "unknown-failure report has unknown fields: " + ", ".join(sorted(unknown))
        )
    if value.get("schema") != "patch-watcher-unknown-failure-report/v1":
        raise RunnerProtocolError("unknown-failure report has unsupported schema")
    state = value.get("state")
    if state not in {"complete", "needs_input", "failed"}:
        raise RunnerProtocolError("unknown-failure report has invalid state")
    recommendation = value.get("recommendation")
    if recommendation not in UNKNOWN_FAILURE_RECOMMENDATIONS:
        raise RunnerProtocolError("unknown-failure report has invalid recommendation")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise RunnerProtocolError("unknown-failure report summary is invalid")
    references = value.get("evidence_references")
    if not isinstance(references, list) or not 1 <= len(references) <= 50:
        raise RunnerProtocolError("unknown-failure report evidence references are invalid")
    normalized_references = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise RunnerProtocolError("unknown-failure evidence reference must be an object")
        if set(reference) != {"evidence_ref", "locator", "supports"}:
            raise RunnerProtocolError("unknown-failure evidence reference fields are invalid")
        normalized_reference = {}
        for field, maximum in (("evidence_ref", 256), ("locator", 1000), ("supports", 2000)):
            item = reference.get(field)
            if not isinstance(item, str) or not item.strip() or len(item) > maximum:
                raise RunnerProtocolError(
                    f"unknown-failure evidence reference {field} is invalid"
                )
            normalized_reference[field] = item.strip()
        normalized_references.append(normalized_reference)
    question = value.get("question")
    if question is not None and (
        not isinstance(question, str) or not question.strip() or len(question) > 2000
    ):
        raise RunnerProtocolError("unknown-failure report question is invalid")
    if state == "needs_input" and question is None:
        raise RunnerProtocolError("needs_input unknown-failure report requires question")
    normalized: dict[str, Any] = {
        "schema": value["schema"],
        "state": state,
        "recommendation": recommendation,
        "summary": summary.strip(),
        "evidence_references": normalized_references,
    }
    if question is not None:
        normalized["question"] = question.strip()
    return normalized


def _user_message(text: str) -> str:
    payload = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


# The CLI prefixes its own refusals this way ("Error: Invalid MCP
# configuration", "Error: When using --print, ..."), which is what makes them
# safe to quote: they are the tool's words, not a passthrough of whatever
# happened to be on the stream.
CLI_ERROR_PREFIX = "Error:"
MAX_CLI_ERROR_TEXT = 500


def _is_number(value: Any) -> bool:
    """True for a real number; bool is an int subclass and is not one here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limited>"
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            sensitive = any(
                marker in key.lower()
                for marker in ("token", "password", "secret", "authorization")
            )
            # A credential is a string.  "input_tokens": 7661 is a COUNT, and
            # redacting it destroyed every usage record the CLI reported while
            # protecting nothing -- the marker "token" matches input_tokens,
            # cacheReadInputTokens and thinkingTokens as readily as auth_token.
            if sensitive and not _is_number(item):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = _redact(item, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:16 * 1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1024]


def _bounded_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    cleaned = _redact(value)
    encoded = json.dumps(cleaned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_EVENT_BYTES:
        return cleaned
    return {
        "type": str(value.get("type", "unknown"))[:128],
        "subtype": str(value.get("subtype", ""))[:128],
        "truncated": True,
        "original_bytes": len(encoded),
    }


def _assistant_text(event: Mapping[str, Any]) -> str:
    if event.get("type") == "result" and isinstance(event.get("result"), str):
        return str(event["result"])[:8192]
    if event.get("type") != "assistant":
        return ""
    message = event.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "\n".join(parts)[:8192]


# An AF_UNIX address is 108 bytes including its NUL, so a socket path has 107
# usable characters.
_SUN_PATH_MAX = 107


def control_socket_path(run_id: str) -> Path:
    """Where one run's control socket lives.

    Deliberately NOT under the run's runtime directory.  That put it at
    ~/.local/state/patch-watcher/runs/<run id>/work/scratch/claude/claude.sock
    -- 108 characters for a real engineering run id, one over the limit -- so
    every such run died with "AF_UNIX path too long" before its socket
    existed, and the two places that derived the path had to agree on it
    besides.  It is addressed by a digest of the run id under the user's
    runtime directory: short, private, and cleaned up when the login session
    ends.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:16]
    path = Path(base) / "patch-watcher" / f"{digest}.sock"
    if len(str(path).encode("utf-8")) > _SUN_PATH_MAX:
        raise ClaudeRunnerError(
            f"control socket path {path} exceeds the {_SUN_PATH_MAX}-character "
            "AF_UNIX limit; set XDG_RUNTIME_DIR to a shorter directory"
        )
    return path


class ClaudeHost:
    """Long-lived owner of one Claude stream and its control socket."""

    def __init__(
        self,
        spec: ReadOnlyRunSpec,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        identity_reader: Callable[[int], ProcessIdentity] = _default_process_identity,
        signal_group: Callable[[int, int], None] = os.killpg,
        clock: Callable[[], float] = time.time,
        event_memory: int = DEFAULT_EVENT_MEMORY,
    ) -> None:
        spec.validate()
        self.spec = spec
        self.runtime_dir = Path(spec.runtime_dir).expanduser().resolve()
        self.socket_path = control_socket_path(spec.run_id)
        self.event_log_path = self.runtime_dir / "events.jsonl"
        self.state_path = self.runtime_dir / "host-state.json"
        self.process_factory = process_factory
        self.identity_reader = identity_reader
        self.signal_group = signal_group
        self.clock = clock
        self.events_memory: deque[Mapping[str, Any]] = deque(maxlen=event_memory)
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.process: Any = None
        self.handle: RunnerHandle | None = None
        self.state = "starting"
        self.turn_state = "starting"
        self.started_at = self.clock()
        self.last_event_at = self.started_at
        self.last_cursor = self._last_log_cursor()
        self.last_message = ""
        self.returncode: int | None = None
        self.stopping = False
        self.stop_force = False
        self._delivery_states = self._load_delivery_states()
        self._pending: queue.Queue[tuple[str, str]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    def _prepare_private_paths(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_dir, 0o700)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.socket_path.parent, 0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()
        descriptor = os.open(self.event_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(self.event_log_path, 0o600)

    def _last_log_cursor(self) -> int:
        path = Path(self.spec.runtime_dir).expanduser().resolve() / "events.jsonl"
        if not path.exists():
            return 0
        last = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    last = max(last, int(value.get("cursor", 0)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        return last

    def _load_delivery_states(self) -> dict[str, str]:
        path = Path(self.spec.runtime_dir).expanduser().resolve() / "events.jsonl"
        states: dict[str, str] = {}
        if not path.exists():
            return states
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "guidance_delivery":
                    continue
                payload = event.get("payload", {})
                delivery_id = payload.get("delivery_id")
                state = payload.get("state")
                if isinstance(delivery_id, str) and isinstance(state, str):
                    states[delivery_id] = state
        return states

    def _append_event(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self.log_lock:
            self.last_cursor += 1
            timestamp = self.clock()
            event = {
                "cursor": self.last_cursor,
                "timestamp": timestamp,
                "type": event_type,
                "payload": _bounded_payload(payload),
            }
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            with self.event_log_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.events_memory.append(event)
            self.last_event_at = timestamp
            return event

    def _persist_state(self) -> None:
        if self.handle is None:
            return
        with self.state_lock:
            _atomic_private_json(self.state_path, self.snapshot().to_dict())

    def start(self) -> RunnerHandle:
        self._prepare_private_paths()
        command = build_read_only_claude_command(self.spec)
        self.process = self.process_factory(
            command,
            cwd=str(Path(self.spec.cwd).expanduser().resolve()),
            env=_safe_environment(capability_profile=self.spec.capability_profile),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            shell=False,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise ClaudeRunnerError("Claude process did not provide streaming pipes")
        claude_identity = self.identity_reader(int(self.process.pid))
        host_identity = self.identity_reader(os.getpid())
        self.handle = RunnerHandle(
            run_id=self.spec.run_id,
            session_id=self.spec.session_id,
            socket_path=str(self.socket_path),
            event_log_path=str(self.event_log_path),
            state_path=str(self.state_path),
            host_identity=host_identity,
            claude_identity=claude_identity,
        )
        self.state = "running"
        self.turn_state = "running"
        self._append_event(
            "host_started",
            {
                "run_id": self.spec.run_id,
                "session_id": self.spec.session_id,
                "claude_pid": claude_identity.pid,
                "capability_profile": self.spec.capability_profile,
                "command": [*command, "<streamed-prompt>"],
            },
        )
        self._reader_thread = threading.Thread(
            target=self._read_stream,
            name="pw-claude-stream-" + self.spec.run_id,
            daemon=True,
        )
        self._reader_thread.start()
        self._accept_delivery("initial:" + self.spec.run_id, self.spec.prompt, initial=True)
        self._persist_state()
        return self.handle

    def _write_prompt(self, delivery_id: str, text: str) -> None:
        try:
            with self.write_lock:
                self.process.stdin.write(_user_message(text))
                self.process.stdin.flush()
            with self.lock:
                self._delivery_states[delivery_id] = "sent"
                self.turn_state = "running"
            self._append_event(
                "guidance_delivery",
                {"delivery_id": delivery_id, "state": "sent", "characters": len(text)},
            )
        except (BrokenPipeError, OSError, ValueError) as exc:
            with self.lock:
                self._delivery_states[delivery_id] = "failed"
            self._append_event(
                "guidance_delivery",
                {"delivery_id": delivery_id, "state": "failed", "error": type(exc).__name__},
            )
            raise
        finally:
            self._persist_state()

    def _accept_delivery(self, delivery_id: str, text: str, *, initial: bool = False) -> GuidanceDelivery:
        if not DELIVERY_ID_RE.fullmatch(delivery_id):
            raise ValueError("delivery_id must be a short stable identifier")
        if not text.strip() or (len(text) > MAX_GUIDANCE_CHARS and not initial):
            raise ValueError("guidance must contain 1..32768 characters")
        with self.lock:
            existing = self._delivery_states.get(delivery_id)
            if existing is not None:
                return GuidanceDelivery(delivery_id, existing, True)
            if self.state not in {"running", "idle"}:
                raise RunnerStateError("runner is not accepting guidance")
            # Idle is not enough: anything already queued must go first. The
            # reader thread sets turn_state = "idle" and only reaches
            # _deliver_next_pending several fsyncing appends later, so a
            # message accepted in that window used to be written straight to
            # stdin ahead of one that had been waiting -- both delivered, both
            # marked delivered, and the agent reading them in the wrong order.
            deliver_now = initial or (
                self.turn_state == "idle" and self._pending.empty()
            )
            state = "sent" if deliver_now else "queued"
            self._delivery_states[delivery_id] = "accepted"
            self._append_event(
                "guidance_delivery",
                {
                    "delivery_id": delivery_id,
                    "state": "accepted",
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "characters": len(text),
                },
            )
            if state == "queued":
                self._delivery_states[delivery_id] = "queued"
                self._pending.put((delivery_id, text))
                self._append_event(
                    "guidance_delivery", {"delivery_id": delivery_id, "state": "queued"}
                )
                return GuidanceDelivery(delivery_id, "queued", False)
        self._write_prompt(delivery_id, text)
        return GuidanceDelivery(delivery_id, "sent", False)

    def _deliver_next_pending(self) -> None:
        try:
            delivery_id, text = self._pending.get_nowait()
        except queue.Empty:
            return
        self._write_prompt(delivery_id, text)

    def _read_stream(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            if len(line.encode("utf-8", errors="replace")) > MAX_CONTROL_BYTES:
                self._append_event("protocol_error", {"reason": "stream_line_too_large"})
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # An unparseable line may be anything, so its content is not
                # persisted -- except when it is the CLI announcing its own
                # refusal to start.  Claude's stderr is folded into this
                # stream, so "Error: Invalid MCP configuration", which killed
                # every read-only run, arrived here and was recorded as a
                # BYTE COUNT: the only explanation that existed, discarded,
                # leaving the run to report "host_process_missing".
                #
                # Only the tool's own "Error:" lines are kept, bounded.  Every
                # other unparseable line is still counted and not quoted.
                payload = {"reason": "invalid_json", "bytes": len(line)}
                stripped = line.strip()
                if stripped.startswith(CLI_ERROR_PREFIX):
                    payload["text"] = stripped[:MAX_CLI_ERROR_TEXT]
                self._append_event("protocol_error", payload)
                continue
            if not isinstance(raw, Mapping):
                self._append_event("protocol_error", {"reason": "event_not_object"})
                continue
            text = _assistant_text(raw)
            with self.lock:
                if text:
                    self.last_message = text
                if raw.get("type") == "result":
                    self.turn_state = "idle"
            self._append_event("claude_event", dict(raw))
            if raw.get("type") == "result":
                if "structured_output" not in raw:
                    self._append_event(
                        "worker_report_invalid", {"reason": "missing_structured_output"}
                    )
                else:
                    try:
                        validator = {
                            "unknown_failure_research": validate_unknown_failure_report,
                            "engineering": validate_engineering_report,
                            "read_only": validate_read_only_report,
                        }[self.spec.report_kind]
                        report = validator(raw.get("structured_output"))
                    except RunnerProtocolError as exc:
                        self._append_event(
                            "worker_report_invalid", {"reason": str(exc)[:1000]}
                        )
                    else:
                        self._append_event("worker_report", report)
            self._persist_state()
            if raw.get("type") == "result":
                self._deliver_next_pending()
        returncode = self.process.poll()
        if returncode is None:
            try:
                returncode = self.process.wait(timeout=0.1)
            except Exception:
                returncode = None
        with self.lock:
            self.returncode = returncode
            self.state = "stopped" if self.stopping else ("completed" if returncode == 0 else "failed")
            self.turn_state = "stopped"
        self._append_event("process_exit", {"returncode": returncode, "requested": self.stopping})
        self._persist_state()

    def _signal_claude(self, sig: int) -> None:
        if self.handle is None or self.handle.claude_identity is None:
            raise RunnerStateError("Claude process identity is unavailable")
        expected = self.handle.claude_identity
        try:
            actual = self.identity_reader(expected.pid)
        except ProcessLookupError as exc:
            raise RunnerStateError("Claude process is no longer running") from exc
        if not _same_identity(expected, actual):
            raise RunnerIdentityError("refusing to signal a reused PID")
        self.signal_group(expected.process_group_id, sig)

    def interrupt(self) -> None:
        with self.lock:
            if self.state not in {"running", "idle"} or self.turn_state != "running":
                raise RunnerStateError("there is no running Claude turn to interrupt")
            self._signal_claude(signal.SIGINT)
            self.turn_state = "interrupting"
        self._append_event("interrupt_requested", {"signal": "SIGINT"})
        self._persist_state()

    def request_stop(self, force: bool) -> None:
        with self.lock:
            if self.state in {"stopped", "completed", "failed"}:
                return
            self.stopping = True
            self.stop_force = self.stop_force or force
            self.state = "stopping"
            self._signal_claude(signal.SIGKILL if force else signal.SIGTERM)
        self._append_event("stop_requested", {"force": force})
        self._persist_state()

    def snapshot(self) -> RunnerSnapshot:
        if self.handle is None:
            raise RunnerStateError("host has not started")
        with self.lock:
            return RunnerSnapshot(
                handle=self.handle,
                state=self.state,
                turn_state=self.turn_state,
                started_at=self.started_at,
                last_event_at=self.last_event_at,
                last_cursor=self.last_cursor,
                last_message=self.last_message,
                returncode=self.returncode,
            )

    def event_tail(self, after_cursor: int = 0, limit: int = 100) -> list[RunnerEvent]:
        """Return up to ``limit`` events immediately AFTER ``after_cursor``.

        Oldest-first, not newest-first. This used to collect into a
        ``deque(maxlen=limit)``, which keeps the LAST ``limit`` matches -- and
        the consumer advances its cursor with ``max(after, event.cursor)`` and
        stops when a page comes back short. Together that silently discarded
        everything between the cursor and the newest page: measured at 400 of
        500 pending events lost, never stored and never turned into messages.

        It is a cursor read, so it must move forward from the cursor and let
        the caller ask again. The name is part of the host protocol and is kept.
        """

        if after_cursor < 0:
            raise ValueError("after_cursor must not be negative")
        limit = max(1, min(int(limit), MAX_EVENT_TAIL))
        values: list[RunnerEvent] = []
        with self.event_log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                    event = RunnerEvent.from_dict(raw)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                if event.cursor <= after_cursor:
                    continue
                values.append(event)
                if len(values) >= limit:
                    break
        return values

    def handle_request(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.get("protocol") != PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported runner protocol")
        request_type = request.get("type")
        if request_type == "status":
            return {"snapshot": self.snapshot().to_dict()}
        if request_type == "events":
            events = self.event_tail(int(request.get("after_cursor", 0)), int(request.get("limit", 100)))
            serialized = []
            response_bytes = 128
            for event in events:
                value = dataclasses.asdict(event)
                event_bytes = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
                if serialized and response_bytes + event_bytes > 768 * 1024:
                    break
                serialized.append(value)
                response_bytes += event_bytes
            return {
                "events": serialized,
                "more": len(serialized) < len(events),
                "last_cursor": self.last_cursor,
            }
        if request_type == "guidance":
            delivery = self._accept_delivery(str(request.get("delivery_id", "")), str(request.get("text", "")))
            return {"delivery": dataclasses.asdict(delivery)}
        if request_type == "interrupt":
            self.interrupt()
            return {"accepted": True}
        if request_type == "stop":
            self.request_stop(bool(request.get("force", False)))
            return {"accepted": True}
        raise RunnerProtocolError("unknown control request")

    def serve(self) -> None:
        if self.handle is None:
            self.start()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(8)
        listener.settimeout(0.2)
        try:
            while not self.stopping and self.process.poll() is None:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    try:
                        request = _receive_json(connection)
                        response = {"ok": True, **self.handle_request(request)}
                    except Exception as exc:
                        response = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
                    connection.sendall(
                        (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                    )
        finally:
            listener.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            if self.process.poll() is None:
                if self.stopping and not self.stop_force:
                    with contextlib.suppress(Exception):
                        self.process.wait(timeout=5.0)
                if self.process.poll() is None:
                    with contextlib.suppress(ClaudeRunnerError):
                        self._signal_claude(signal.SIGKILL if self.stopping else signal.SIGTERM)
            if self.process.poll() is None:
                with contextlib.suppress(Exception):
                    self.process.wait(timeout=2.0)
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2.0)


def _receive_json(connection: socket.socket) -> Mapping[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(min(65536, MAX_CONTROL_BYTES - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_CONTROL_BYTES:
            raise RunnerProtocolError("control request is too large")
        if b"\n" in chunk:
            break
    try:
        value = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerProtocolError("control request is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise RunnerProtocolError("control request must be an object")
    return value


def _socket_request(socket_path: str, request: Mapping[str, Any], timeout: float = 5.0) -> Mapping[str, Any]:
    payload = (json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_CONTROL_BYTES:
        raise ValueError("control request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(payload)
        response = _receive_json(client)
    if not response.get("ok"):
        raise RunnerProtocolError(str(response.get("error", "runner host rejected the request")))
    return response


class ClaudeRunner:
    """Controller-side interface to durable Patch Watcher Claude hosts."""

    def __init__(
        self,
        *,
        host_launcher: Callable[..., Any] = subprocess.Popen,
        identity_reader: Callable[[int], ProcessIdentity] = _default_process_identity,
        requester: Callable[[str, Mapping[str, Any], float], Mapping[str, Any]] = _socket_request,
        ready_timeout: float = 10.0,
        stop_timeout: float = 10.0,
    ) -> None:
        self.host_launcher = host_launcher
        self.identity_reader = identity_reader
        self.requester = requester
        self.ready_timeout = ready_timeout
        # How long a host abandoned by a failed `start` gets to shut itself
        # down after SIGTERM.  It has to cover the host serve-loop finalizer,
        # which waits on the `claude` child before killing it, or we escalate
        # to SIGKILL while the child is still being reaped and orphan it.
        self.stop_timeout = stop_timeout

    def start(self, spec: ReadOnlyRunSpec) -> RunnerSnapshot:
        spec.validate()
        runtime = Path(spec.runtime_dir).expanduser().resolve()
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(runtime, 0o700)
        spec_path = runtime / "launch-spec.json"
        _atomic_private_json(spec_path, spec.to_dict())
        command = [sys.executable, str(Path(__file__).resolve()), "_host", "--spec", str(spec_path)]
        # The host's stderr used to go to /dev/null.  A host that dies before
        # its socket is ready then left one sentence behind -- "exited before
        # its control socket was ready" -- and nothing else, which is what the
        # first real run on a host produced.  It goes to a private file now,
        # and its tail rides along in the failure the operator sees.
        stderr_path = runtime / "host.stderr"
        stderr_fd = os.open(str(stderr_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = self.host_launcher(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fd,
                start_new_session=True,
                shell=False,
            )
        finally:
            os.close(stderr_fd)
        # Everything from here on runs under the abandon guard.  The host is
        # already launched, so any exit that is not a returned snapshot has to
        # take the process down with it -- including the identity read, whose
        # failure leaves an unreaped child.
        host_identity: ProcessIdentity | None = None
        adopted = False
        try:
            host_identity = self.identity_reader(int(process.pid))
            preliminary = RunnerHandle(
                run_id=spec.run_id,
                session_id=spec.session_id,
                socket_path=str(control_socket_path(spec.run_id)),
                event_log_path=str(runtime / "events.jsonl"),
                state_path=str(runtime / "host-state.json"),
                host_identity=host_identity,
            )
            deadline = time.monotonic() + self.ready_timeout
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ClaudeRunnerError(
                        "Claude host exited before its control socket was ready "
                        f"(exit status {process.returncode}): "
                        + _stderr_tail(stderr_path)
                    )
                try:
                    snapshot = self.status(preliminary)
                    if snapshot.handle.run_id != spec.run_id or snapshot.handle.session_id != spec.session_id:
                        raise RunnerIdentityError("Claude host returned a different run identity")
                    adopted = True
                    return snapshot
                except (TimeoutError, FileNotFoundError, ConnectionRefusedError, RunnerProtocolError, OSError) as exc:
                    last_error = exc
                    time.sleep(0.05)
            raise ClaudeRunnerError("Claude host did not become ready") from last_error
        finally:
            if not adopted:
                self._abandon_host(process, host_identity)

    def _host_is_gone(self, process: Any, identity: ProcessIdentity) -> bool:
        """Has the host we launched actually left the process table?

        `poll` is the authority for a process we launched, because it reaps:
        a killed child stays a zombie that `kill(pid, 0)` still answers for
        until someone collects it, so an identity-only check would report a
        corpse as alive forever.  The identity check is the backstop for a
        launcher that handed back something we cannot reap -- a PID that is
        gone, or now carries a different start token, is equally proof that
        our host is not running.
        """
        with contextlib.suppress(Exception):
            if process.poll() is not None:
                return True
        try:
            actual = self.identity_reader(identity.pid)
        except (ProcessLookupError, PermissionError):
            return True
        return not _same_identity(identity, actual)

    def _abandon_host(self, process: Any, identity: ProcessIdentity | None) -> None:
        """Take down a host `start` launched but never handed to a caller.

        Only the ready-timeout path used to signal at all, and it sent one
        SIGTERM and assumed it landed.  Every other failure -- a
        RunnerIdentityError from the run-identity check or from `status`
        (a sibling of RunnerProtocolError, so the retry tuple never caught
        it), a KeyError on a response without "snapshot", a ValueError out of
        RunnerSnapshot.from_dict -- returned straight past the cleanup and
        left a detached host plus its `claude` child running under
        --permission-mode bypassPermissions in a real checkout, with nothing
        tracking it.  Self-healing does not cover it: the `host-state.json`
        fallback in the controller is exactly wrong for the identity
        mismatch, which is the case where that file describes somebody else's
        process.

        SIGTERM strictly before SIGKILL, because the `claude` child lives in
        its own session: killing the host outright orphans the child, whereas
        the host serve-loop finalizer does the verified teardown of the child
        on its way out.  Escalation is driven by observation, not by a fixed
        sleep, and both signals are gated on the start token so a recycled PID
        cannot be hit.
        """
        if identity is None:
            # We never established an identity for this PID, so signalling it
            # could hit a process that merely inherited the number.  Reaping
            # the child we launched is the most we can safely do.
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
            return
        for signum, grace in ((signal.SIGTERM, self.stop_timeout), (signal.SIGKILL, 2.0)):
            if self._host_is_gone(process, identity):
                return
            try:
                actual = self.identity_reader(identity.pid)
            except (ProcessLookupError, PermissionError):
                return
            if not _same_identity(identity, actual):
                return
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(actual.process_group_id, signum)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if self._host_is_gone(process, identity):
                    return
                time.sleep(0.02)
        # Surviving a SIGKILL to its process group means the host is wedged in
        # uninterruptible sleep and will die when it leaves it.  We are on the
        # way out of a failing `start`; raising here would replace the caller's
        # real diagnosis with this one, so let the original failure through.

    def _request(self, handle: RunnerHandle, request_type: str, **values: Any) -> Mapping[str, Any]:
        request = {"protocol": PROTOCOL_VERSION, "type": request_type, **values}
        return self.requester(handle.socket_path, request, 5.0)

    def status(self, handle: RunnerHandle) -> RunnerSnapshot:
        response = self._request(handle, "status")
        snapshot = RunnerSnapshot.from_dict(response["snapshot"])
        if snapshot.handle.run_id != handle.run_id or snapshot.handle.session_id != handle.session_id:
            raise RunnerIdentityError("control socket belongs to another run")
        return snapshot

    def list(self, handles: Iterable[RunnerHandle]) -> builtins.list[RunnerSnapshot]:
        snapshots = []
        for handle in handles:
            try:
                snapshots.append(self.status(handle))
            except (ClaudeRunnerError, OSError):
                continue
        return snapshots

    def events(self, handle: RunnerHandle, *, after_cursor: int = 0, limit: int = 100) -> builtins.list[RunnerEvent]:
        response = self._request(handle, "events", after_cursor=after_cursor, limit=limit)
        return [RunnerEvent.from_dict(value) for value in response.get("events", [])]

    def queue_guidance(self, handle: RunnerHandle, delivery_id: str, text: str) -> GuidanceDelivery:
        response = self._request(handle, "guidance", delivery_id=delivery_id, text=text)
        return GuidanceDelivery.from_dict(response["delivery"])

    def interrupt(self, handle: RunnerHandle) -> None:
        self._request(handle, "interrupt")

    def terminate(self, handle: RunnerHandle) -> None:
        self._request(handle, "stop", force=False)

    def kill(self, handle: RunnerHandle) -> None:
        try:
            self._request(handle, "stop", force=True)
            return
        except (OSError, RunnerProtocolError):
            pass
        # A dead control socket means the host cannot reap its child.  Kill the
        # independently-sessioned Claude process first, then the host, verifying
        # both start tokens so neither signal can hit a reused PID.
        if handle.claude_identity is not None:
            try:
                claude_actual = self.identity_reader(handle.claude_identity.pid)
            except ProcessLookupError:
                claude_actual = None
            if claude_actual is not None:
                if not _same_identity(handle.claude_identity, claude_actual):
                    raise RunnerIdentityError("refusing to kill a reused Claude PID")
                os.killpg(claude_actual.process_group_id, signal.SIGKILL)
        actual = self.identity_reader(handle.host_identity.pid)
        if not _same_identity(handle.host_identity, actual):
            raise RunnerIdentityError("refusing to kill a reused host PID")
        os.killpg(actual.process_group_id, signal.SIGKILL)

    def probe(self, handle: RunnerHandle) -> ReconciliationProbe:
        try:
            actual = self.identity_reader(handle.host_identity.pid)
        except (ProcessLookupError, PermissionError):
            return ReconciliationProbe(False, False, False, False, "host_process_missing")
        if not _same_identity(handle.host_identity, actual):
            # A reused PID proves OUR host process is gone: some other process
            # now holds that pid. Reporting alive=True here wedged cleanup --
            # `_cleanup_session` and `_reconcile_ltvm_resources` both gate on
            # `alive`, so a recycled pid meant a terminal run never released
            # its checkout or destroyed its guests, forever. Signalling stays
            # refused by the identity guards in terminate/kill, so calling it
            # dead cannot make us signal the innocent process.
            return ReconciliationProbe(False, False, False, False, "host_pid_reused")
        try:
            snapshot = self.status(handle)
        except (OSError, RunnerProtocolError, RunnerIdentityError):
            return ReconciliationProbe(True, True, False, False, "control_socket_unreachable")
        return ReconciliationProbe(True, True, True, True, "adoptable", snapshot)

    def adopt(self, handle: RunnerHandle) -> RunnerSnapshot:
        probe = self.probe(handle)
        if not probe.adoptable or probe.snapshot is None:
            raise RunnerAdoptionError(probe.reason)
        return probe.snapshot


def request_host_stop(host: ClaudeHost) -> None:
    """Ask a host to wind down, from a signal handler.

    Lock-free on purpose: the serve-loop finalizer performs the verified
    TERM/grace/KILL sequence, and a handler that blocked on `lock` could
    deadlock against the thread it interrupted.

    It must not CLEAR `stop_force`. An operator's kill sets that over the
    control socket, and the controller's stop ladder then signals this host
    too -- so a SIGTERM arriving after the kill used to downgrade the forced
    stop back to graceful, and the finalizer waited out a five second grace
    the operator had explicitly declined.
    """

    host.stopping = True


def _stderr_tail(path: Path, *, limit: int = 1200) -> str:
    """The last lines a dead host wrote, or a sentence saying it wrote none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "no stderr was captured"
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return "the host wrote nothing to stderr"
    tail = "\n".join(lines[-12:])
    return tail[-limit:]


def _host_main(spec_path: Path) -> int:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    spec = ReadOnlyRunSpec.from_dict(raw)
    spec_path.unlink()
    host = ClaudeHost(spec)

    def on_signal(_signum: int, _frame: object) -> None:
        request_host_stop(host)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    host.serve()
    if host.process is None:
        return 1
    returncode = host.process.poll()
    return int(returncode or 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch Watcher native Claude transport")
    subparsers = parser.add_subparsers(dest="command", required=True)
    host_parser = subparsers.add_parser("_host", help=argparse.SUPPRESS)
    host_parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "_host":
        return _host_main(args.spec)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
