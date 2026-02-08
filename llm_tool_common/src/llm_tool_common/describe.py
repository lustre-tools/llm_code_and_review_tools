"""Tool self-description helpers for LLM discoverability.

This module provides data structures and helpers for tools to expose
their full API surface in a machine-readable format. When an LLM runs
`jira describe` or `gc describe`, it gets a structured JSON response
listing all commands, their arguments, types, defaults, and suggested
next actions.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Argument:
    """Describes a single command argument or option."""

    name: str
    description: str
    type: str = "string"
    required: bool = False
    default: Any = None
    choices: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "required": self.required,
        }
        if self.default is not None:
            d["default"] = self.default
        if self.choices is not None:
            d["choices"] = self.choices
        return d


@dataclass
class Command:
    """Describes a single CLI command."""

    name: str
    description: str
    usage: str
    arguments: list[Argument] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "usage": self.usage,
        }
        if self.arguments:
            d["arguments"] = [a.to_dict() for a in self.arguments]
        if self.examples:
            d["examples"] = self.examples
        if self.next_actions:
            d["next_actions"] = self.next_actions
        if self.output_fields:
            d["output_fields"] = self.output_fields
        return d


@dataclass
class ToolDescription:
    """Complete machine-readable description of a CLI tool."""

    name: str
    version: str
    description: str
    commands: list[Command] = field(default_factory=list)
    env_vars: list[dict[str, str]] = field(default_factory=list)
    output_format: str = "JSON envelope with ok/data/error/meta fields"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "output_format": self.output_format,
            "env_vars": self.env_vars,
            "commands": [c.to_dict() for c in self.commands],
        }
