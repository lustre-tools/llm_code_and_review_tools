"""Shared utilities for LLM-focused CLI tools.

This package provides common functionality shared between tools like
jira_tool and gerrit_cli, including:
- Response envelope helpers for standardized JSON output
- Base error classes and exit codes
"""

from .envelope import (
    success_response,
    error_response,
    error_response_from_dict,
    format_json,
)
from .errors import (
    ExitCode,
    ErrorCode,
    ToolError,
    AuthError,
    NotFoundError,
    InvalidInputError,
    NetworkError,
    ConfigError,
)
from .describe import (
    Argument,
    Command,
    ToolDescription,
)
from .config import (
    CredentialSetError,
    DEFAULT_SET,
    USERNAME_KEYS,
    apply_credential_set,
    argv_option_value,
    credential_sets,
    env_file_locations,
    env_file_variable,
    hoist_args,
    load_env_files,
    parse_env_file,
    resolve_credential_set,
    resolve_env_file,
)
from .decorators import handle_errors

__all__ = [
    # Envelope functions
    "success_response",
    "error_response",
    "error_response_from_dict",
    "format_json",
    # Error classes
    "ExitCode",
    "ErrorCode",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
    # Describe helpers
    "Argument",
    "Command",
    "ToolDescription",
    # Config
    "env_file_variable",
    "load_env_files",
    # Credential sets
    "CredentialSetError",
    "DEFAULT_SET",
    "USERNAME_KEYS",
    "apply_credential_set",
    "argv_option_value",
    "credential_sets",
    "env_file_locations",
    "hoist_args",
    "parse_env_file",
    "resolve_credential_set",
    "resolve_env_file",
    # Decorators
    "handle_errors",
]

