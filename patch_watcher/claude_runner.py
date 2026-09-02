"""Patch Watcher's native, read-only Claude Code transport.

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
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


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
        "state": {"enum": ["complete", "needs_input", "failed", "resource_exhausted"]},
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

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProcessIdentity":
        return cls(
            pid=int(value["pid"]),
            start_token=str(value["start_token"]),
            process_group_id=int(value["process_group_id"]),
        )


@dataclasses.dataclass(frozen=True)
class ReadOnlyRunSpec:
    """Everything the transport needs to launch one bounded conversation.

    The historical class name is retained for persisted Phase 0C launch specs.
    ``source_edit`` enables only file editing in an isolated checkout.
    ``source_edit_ltvm`` additionally exposes one controller-built local MCP
    broker for open-ended execution inside exact-owner LTVM guests.  Bash,
    browser, ambient MCP servers, and service credentials remain unavailable.
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
        if self.capability_profile not in {
            "read_only", "source_edit", "source_edit_ltvm"
        }:
            raise ValueError("unsupported capability profile")
        if self.report_kind == "engineering" and self.capability_profile not in {
            "source_edit", "source_edit_ltvm"
        }:
            raise ValueError("engineering reports require a source-edit capability profile")
        if self.capability_profile in {"source_edit", "source_edit_ltvm"} and self.report_kind != "engineering":
            raise ValueError("source_edit capability requires an engineering report")
        try:
            mcp_config = json.loads(self.mcp_config_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("mcp_config_json must be valid JSON") from exc
        if not isinstance(mcp_config, Mapping):
            raise ValueError("mcp_config_json must contain an object")
        if self.capability_profile == "source_edit_ltvm":
            servers = mcp_config.get("mcpServers")
            if not isinstance(servers, Mapping) or set(servers) != {"pw_ltvm"}:
                raise ValueError("source_edit_ltvm requires only the pw_ltvm MCP server")
            server = servers["pw_ltvm"]
            if not isinstance(server, Mapping) or set(server) != {"command", "args"}:
                raise ValueError("pw_ltvm MCP server configuration is invalid")
            command = server.get("command")
            arguments = server.get("args")
            if (
                not isinstance(command, str)
                or not Path(command).is_absolute()
                or "\x00" in command
                or not isinstance(arguments, list)
                or not 1 <= len(arguments) <= 16
                or any(
                    not isinstance(item, str) or not item or "\x00" in item
                    or len(item.encode("utf-8")) > 4096
                    for item in arguments
                )
            ):
                raise ValueError("pw_ltvm MCP command is invalid")
        elif mcp_config != {}:
            raise ValueError("MCP is unavailable without the LTVM capability profile")

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReadOnlyRunSpec":
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
    claude_identity: Optional[ProcessIdentity] = None
    protocol: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        value = dataclasses.asdict(self)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerHandle":
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
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerEvent":
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
    returncode: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerSnapshot":
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
    def from_dict(cls, value: Mapping[str, Any]) -> "GuidanceDelivery":
        return cls(str(value["delivery_id"]), str(value["state"]), bool(value.get("duplicate")))


@dataclasses.dataclass(frozen=True)
class ReconciliationProbe:
    alive: bool
    identity_match: bool
    control_reachable: bool
    adoptable: bool
    reason: str
    snapshot: Optional[RunnerSnapshot] = None


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
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


def _safe_environment(
    source: Optional[Mapping[str, str]] = None,
    *,
    capability_profile: str = "read_only",
) -> Dict[str, str]:
    """Remove ambient service-write credentials while preserving model auth."""

    environment = dict(source if source is not None else os.environ)
    protected_model_keys = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",  # Claude may be configured through Bedrock.
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    service_markers = ("GERRIT", "MALOO", "JENKINS", "JIRA", "JANITOR", "GITHUB", "GITLAB")
    secret_suffixes = ("_TOKEN", "_PASSWORD", "_PASS", "_SECRET", "_API_KEY")
    for key in list(environment):
        upper = key.upper()
        if upper in protected_model_keys:
            continue
        if any(marker in upper for marker in service_markers) or upper.endswith(secret_suffixes):
            environment.pop(key, None)
    environment["CLAUDE_CODE_SAFE_MODE"] = "1"
    if capability_profile not in {
        "read_only", "source_edit", "source_edit_ltvm"
    }:
        raise ValueError("unsupported capability profile")
    environment["PATCH_WATCHER_CAPABILITY_PROFILE"] = capability_profile
    return environment


def build_read_only_claude_command(spec: ReadOnlyRunSpec) -> List[str]:
    """Return a shell-free command with the profile's exact bounded tools."""

    spec.validate()
    report_schema = {
        "unknown_failure_research": UNKNOWN_FAILURE_REPORT_SCHEMA,
        "engineering": ENGINEERING_REPORT_SCHEMA,
        "read_only": READ_ONLY_REPORT_SCHEMA,
    }[spec.report_kind]
    if spec.capability_profile == "read_only":
        tools = "Read,Glob,Grep"
    elif spec.capability_profile == "source_edit":
        tools = "Read,Glob,Grep,Edit,Write"
    else:
        tools = (
            "Read,Glob,Grep,Edit,Write,"
            "mcp__pw_ltvm__list,mcp__pw_ltvm__target_list,"
            "mcp__pw_ltvm__target_fetch,mcp__pw_ltvm__create,"
            "mcp__pw_ltvm__cluster_create,mcp__pw_ltvm__push_source,"
            "mcp__pw_ltvm__exec,mcp__pw_ltvm__cluster_exec,"
            "mcp__pw_ltvm__destroy"
        )
    command = [
        spec.claude_binary,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--replay-user-messages",
        "--session-id",
        spec.session_id,
        "--no-chrome",
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--tools",
        tools,
        "--mcp-config",
        spec.mcp_config_json,
        "--json-schema",
        json.dumps(report_schema, sort_keys=True, separators=(",", ":")),
    ]
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
    }
    unknown = set(value) - allowed
    if unknown:
        raise RunnerProtocolError(
            "engineering report has unknown fields: " + ", ".join(sorted(unknown))
        )
    if value.get("schema") != "patch-watcher-engineering-report/v1":
        raise RunnerProtocolError("engineering report has unsupported schema")
    state = value.get("state")
    if state not in {"complete", "needs_input", "failed", "resource_exhausted"}:
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
    normalized: Dict[str, Any] = {
        "schema": value["schema"], "state": state, "summary": summary.strip(),
        "changed_files": normalized_files, "validation_requests": normalized_requests,
    }
    if question is not None:
        normalized["question"] = question.strip()
    return normalized


def validate_read_only_report(value: Any) -> Mapping[str, Any]:
    """Validate and normalize the bounded Phase 0C final report.

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
    normalized: Dict[str, Any] = {
        "schema": value["schema"],
        "state": state,
        "summary": summary.strip(),
        "findings": [finding.strip() for finding in findings],
    }
    if question is not None:
        normalized["question"] = question.strip()
    return normalized


def validate_unknown_failure_report(value: Any) -> Mapping[str, Any]:
    """Validate the bounded syntactic Phase 2 research report.

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
                    "unknown-failure evidence reference %s is invalid" % field
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
    normalized: Dict[str, Any] = {
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


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limited>"
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            if any(marker in key.lower() for marker in ("token", "password", "secret", "authorization")):
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
        self.socket_path = self.runtime_dir / "claude.sock"
        self.event_log_path = self.runtime_dir / "events.jsonl"
        self.state_path = self.runtime_dir / "host-state.json"
        self.process_factory = process_factory
        self.identity_reader = identity_reader
        self.signal_group = signal_group
        self.clock = clock
        self.events_memory: Deque[Mapping[str, Any]] = deque(maxlen=event_memory)
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.process: Any = None
        self.handle: Optional[RunnerHandle] = None
        self.state = "starting"
        self.turn_state = "starting"
        self.started_at = self.clock()
        self.last_event_at = self.started_at
        self.last_cursor = self._last_log_cursor()
        self.last_message = ""
        self.returncode: Optional[int] = None
        self.stopping = False
        self.stop_force = False
        self._delivery_states = self._load_delivery_states()
        self._pending: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None

    def _prepare_private_paths(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_dir, 0o700)
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

    def _load_delivery_states(self) -> Dict[str, str]:
        path = Path(self.spec.runtime_dir).expanduser().resolve() / "events.jsonl"
        states: Dict[str, str] = {}
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
        if not text.strip() or len(text) > MAX_GUIDANCE_CHARS and not initial:
            raise ValueError("guidance must contain 1..32768 characters")
        with self.lock:
            existing = self._delivery_states.get(delivery_id)
            if existing is not None:
                return GuidanceDelivery(delivery_id, existing, True)
            if self.state not in {"running", "idle"}:
                raise RunnerStateError("runner is not accepting guidance")
            state = "sent" if initial or self.turn_state == "idle" else "queued"
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
                self._append_event("protocol_error", {"reason": "invalid_json", "bytes": len(line)})
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

    def event_tail(self, after_cursor: int = 0, limit: int = 100) -> List[RunnerEvent]:
        if after_cursor < 0:
            raise ValueError("after_cursor must not be negative")
        limit = max(1, min(int(limit), MAX_EVENT_TAIL))
        values: Deque[RunnerEvent] = deque(maxlen=limit)
        with self.event_log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                    event = RunnerEvent.from_dict(raw)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                if event.cursor > after_cursor:
                    values.append(event)
        return list(values)

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
                except socket.timeout:
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
                    try:
                        self.process.wait(timeout=5.0)
                    except Exception:
                        pass
                if self.process.poll() is None:
                    try:
                        self._signal_claude(signal.SIGKILL if self.stopping else signal.SIGTERM)
                    except ClaudeRunnerError:
                        pass
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=2.0)
                except Exception:
                    pass
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2.0)


def _receive_json(connection: socket.socket) -> Mapping[str, Any]:
    chunks: List[bytes] = []
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
    ) -> None:
        self.host_launcher = host_launcher
        self.identity_reader = identity_reader
        self.requester = requester
        self.ready_timeout = ready_timeout

    def start(self, spec: ReadOnlyRunSpec) -> RunnerSnapshot:
        spec.validate()
        runtime = Path(spec.runtime_dir).expanduser().resolve()
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(runtime, 0o700)
        spec_path = runtime / "launch-spec.json"
        _atomic_private_json(spec_path, spec.to_dict())
        command = [sys.executable, str(Path(__file__).resolve()), "_host", "--spec", str(spec_path)]
        process = self.host_launcher(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
        )
        host_identity = self.identity_reader(int(process.pid))
        preliminary = RunnerHandle(
            run_id=spec.run_id,
            session_id=spec.session_id,
            socket_path=str(runtime / "claude.sock"),
            event_log_path=str(runtime / "events.jsonl"),
            state_path=str(runtime / "host-state.json"),
            host_identity=host_identity,
        )
        deadline = time.monotonic() + self.ready_timeout
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ClaudeRunnerError("Claude host exited before its control socket was ready")
            try:
                snapshot = self.status(preliminary)
                if snapshot.handle.run_id != spec.run_id or snapshot.handle.session_id != spec.session_id:
                    raise RunnerIdentityError("Claude host returned a different run identity")
                return snapshot
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout, RunnerProtocolError, OSError) as exc:
                last_error = exc
                time.sleep(0.05)
        try:
            os.killpg(host_identity.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise ClaudeRunnerError("Claude host did not become ready") from last_error

    def _request(self, handle: RunnerHandle, request_type: str, **values: Any) -> Mapping[str, Any]:
        request = {"protocol": PROTOCOL_VERSION, "type": request_type, **values}
        return self.requester(handle.socket_path, request, 5.0)

    def status(self, handle: RunnerHandle) -> RunnerSnapshot:
        response = self._request(handle, "status")
        snapshot = RunnerSnapshot.from_dict(response["snapshot"])
        if snapshot.handle.run_id != handle.run_id or snapshot.handle.session_id != handle.session_id:
            raise RunnerIdentityError("control socket belongs to another run")
        return snapshot

    def list(self, handles: Iterable[RunnerHandle]) -> List[RunnerSnapshot]:
        snapshots = []
        for handle in handles:
            try:
                snapshots.append(self.status(handle))
            except (ClaudeRunnerError, OSError):
                continue
        return snapshots

    def events(self, handle: RunnerHandle, *, after_cursor: int = 0, limit: int = 100) -> List[RunnerEvent]:
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
            return ReconciliationProbe(True, False, False, False, "host_pid_reused")
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


def _host_main(spec_path: Path) -> int:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    spec = ReadOnlyRunSpec.from_dict(raw)
    spec_path.unlink()
    host = ClaudeHost(spec)

    def request_stop(_signum: int, _frame: object) -> None:
        # Keep the signal handler lock-free.  The serve-loop finalizer performs
        # the verified TERM/grace/KILL sequence.
        host.stopping = True
        host.stop_force = False

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    host.serve()
    if host.process is None:
        return 1
    returncode = host.process.poll()
    return int(returncode or 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
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
