# JIRA Tool Architecture

## Overview

This tool provides a thin, LLM-agent-focused CLI wrapper around the JIRA REST API. It is designed for deterministic behavior, strict JSON output, and stable command semantics.

## Design Principles

1. **JSON-only Interface**: All output is structured JSON. Human-readable formatting is available via `--pretty` flag.
2. **Explicit Inputs**: No implicit defaults. Required fields must be provided explicitly. Fail fast with clear error codes.
3. **Deterministic Behavior**: Same input produces same output shape. No silent field truncation.
4. **Thin Command Surface**: Minimal commands covering core agent workflows.
5. **LLM Context Awareness**: Comments and attachments support pagination/size limits to avoid overwhelming LLM context windows.

## Output Format Specification

This section provides a complete specification of the output format, including the design rationale for each decision. This format is intended to be shared across multiple LLM-focused CLI tools (e.g., JIRA, Gerrit, etc.).

### Design Goals

The output format was designed with the following goals in mind:

1. **LLM Parseability**: LLMs should be able to reliably extract information without complex parsing logic or regex
2. **Determinism**: Same input always produces the same output structure (though values may differ)
3. **Debuggability**: Sufficient metadata for troubleshooting without exposing sensitive information
4. **Scriptability**: Works well in shell pipelines and CI/CD systems
5. **Consistency**: Identical patterns across all commands and tools in the suite

### Standard Response Envelope

Every command produces a JSON object with exactly three top-level keys: `ok`, `data` or `error`, and `meta`.

#### Success Response
```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "tool": "jira",
    "command": "issue.get",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

#### Error Response
```json
{
  "ok": false,
  "error": {
    "code": "AUTH_FAILED",
    "message": "Authentication failed",
    "http_status": 401,
    "details": {}
  },
  "meta": {
    "tool": "jira",
    "command": "issue.get",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

### Field-by-Field Design Rationale

#### The `ok` Field (Boolean)

```json
"ok": true
```

**Why a boolean?**
- LLMs can trivially check success/failure with a single field inspection
- No ambiguity: `true` means success, `false` means failure
- Eliminates the need to check for presence of `error` field or parse status codes
- Works identically across all commands—no special cases

**Why not HTTP status codes?**
- Not all operations map cleanly to HTTP semantics
- CLI tools may have errors that don't originate from HTTP (config errors, network timeouts)
- A boolean is more direct for programmatic consumption

#### The `data` Field (Success Payload)

```json
"data": {
  "key": "PROJ-123",
  "summary": "Fix login bug",
  "status": "In Progress"
}
```

**Design decisions:**
- Only present when `ok: true`
- Contains the actual response payload—never metadata
- Shape varies by command but is documented and consistent per-command
- Never truncated silently (if truncation occurs, it's explicit in the data or meta)

**Why separate from `meta`?**
- Clear separation between "what you asked for" vs "information about the request"
- LLMs can extract just the `data` field without filtering out metadata
- Makes response structure predictable

#### The `error` Field (Failure Payload)

```json
"error": {
  "code": "ISSUE_NOT_FOUND",
  "message": "Issue PROJ-999 does not exist",
  "http_status": 404,
  "details": {
    "issue_key": "PROJ-999"
  }
}
```

**Design decisions:**
- Only present when `ok: false`
- Always contains `code` (machine-readable) and `message` (human-readable)
- `http_status` included when the error originates from an HTTP response
- `details` provides structured context for programmatic handling

**Why string error codes instead of numeric?**
- Self-documenting: `"AUTH_FAILED"` is immediately understandable
- LLMs can reason about the error type without a lookup table
- No collision concerns across different tools
- Easier to extend without reserving number ranges

**Why both `code` and `message`?**
- `code` is for programmatic matching (stable, never changes)
- `message` is for display/logging (may be refined over time)
- LLMs can use either depending on the task

#### The `meta` Field (Request Metadata)

```json
"meta": {
  "tool": "jira",
  "command": "issue.get",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Design decisions:**
- Always present in both success and error responses
- Contains information about the request, not the response data
- `tool`: Identifies which tool generated this response (useful when multiple tools share the format)
- `command`: The specific operation that was invoked (dot-notation for namespacing)
- `timestamp`: ISO-8601 UTC timestamp of when the response was generated

**Why include `tool` and `command`?**
- Enables log aggregation and debugging across multi-tool workflows
- LLMs can verify they're processing the expected response type
- Useful for audit trails and correlation

**Why ISO-8601 timestamps?**
- Universally parseable standard
- Lexicographically sortable
- Unambiguous timezone handling (always UTC with `Z` suffix)

### Exit Codes

Exit codes provide a secondary signal for shell scripts and CI/CD systems.

| Code | Name | Description |
|------|------|-------------|
| 0 | SUCCESS | Operation completed successfully |
| 1 | GENERAL_ERROR | Unspecified error |
| 2 | AUTH_ERROR | Authentication/authorization failure |
| 3 | NOT_FOUND | Requested resource does not exist |
| 4 | INVALID_INPUT | Malformed input or invalid parameters |
| 5 | NETWORK_ERROR | Connection failure or timeout |

**Why separate exit codes from error codes?**
- Exit codes are for shell-level control flow (`if jira issue get X; then ...`)
- Error codes are for programmatic inspection of the JSON response
- Exit codes are coarse-grained (5 categories); error codes are fine-grained (many specific codes)
- Some environments only have access to exit codes (e.g., simple shell scripts)

**Why these specific categories?**
- Cover the most common failure modes that require different handling
- Auth errors (2): May need credential refresh
- Not found (3): May need to create the resource or handle gracefully
- Invalid input (4): Caller bug, fix the request
- Network error (5): Retry may help

### JSON Formatting

**Default: Compact JSON (no whitespace)**
```bash
jira issue get PROJ-123
# {"ok":true,"data":{...},"meta":{...}}
```

**Pretty printing: `--pretty` flag**
```bash
jira issue get PROJ-123 --pretty
# {
#   "ok": true,
#   "data": {...},
#   "meta": {...}
# }
```

**Why compact by default?**
- Smaller output size (relevant for LLM context limits)
- Single line per response (easier to process in pipelines)
- `--pretty` available when human readability is needed

**Why `--pretty` instead of detecting TTY?**
- Deterministic: same command always produces same format
- LLMs invoke commands non-interactively; auto-detection would be wrong
- Explicit is better than implicit

### Error Code Registry

Error codes follow a naming convention: `CATEGORY_SPECIFIC_ERROR`

| Code | Category | Exit Code | Description |
|------|----------|-----------|-------------|
| `AUTH_FAILED` | Auth | 2 | Credentials rejected |
| `AUTH_MISSING` | Auth | 2 | No credentials provided |
| `ISSUE_NOT_FOUND` | Resource | 3 | Issue does not exist |
| `PROJECT_NOT_FOUND` | Resource | 3 | Project does not exist |
| `TRANSITION_NOT_FOUND` | Resource | 3 | Transition ID invalid |
| `INVALID_INPUT` | Input | 4 | Generic input validation failure |
| `INVALID_JQL` | Input | 4 | JQL syntax error |
| `MISSING_REQUIRED_FIELD` | Input | 4 | Required parameter not provided |
| `INVALID_TRANSITION` | Input | 4 | Transition not available for current state |
| `CONNECTION_ERROR` | Network | 5 | Could not connect to server |
| `TIMEOUT` | Network | 5 | Request timed out |
| `SERVER_ERROR` | Server | 1 | Server returned 5xx error |
| `RATE_LIMITED` | Server | 1 | Too many requests |
| `CONFIG_ERROR` | Config | 1 | Configuration invalid |
| `CONFIG_NOT_FOUND` | Config | 1 | Configuration file missing |

### Guidelines for Extending This Format

When adding new commands or adapting this format for other tools:

1. **Never add top-level fields**: The envelope is always `{ok, data|error, meta}`
2. **Keep `data` shape documented**: Each command's `data` structure should be specified
3. **Add error codes as needed**: Follow the `CATEGORY_SPECIFIC` naming pattern
4. **Preserve exit code semantics**: Map new errors to existing exit codes when possible
5. **Include command in meta**: Use dot-notation (e.g., `attachment.get`, `issue.create`)
6. **Timestamps are always UTC**: Use `Z` suffix, never local time

## Commands

### Issue Operations

| Command | Description |
|---------|-------------|
| `jira issue get <key>` | Get issue details (summary, description, status, etc.) |
| `jira issue comments <key>` | Get comments with pagination support |
| `jira issue attachments <key>` | List attachments for an issue |
| `jira issue search <jql>` | Search issues using JQL |
| `jira issue create` | Create a new issue |
| `jira issue comment <key> <body>` | Add a comment to an issue |
| `jira issue transitions <key>` | List available transitions |
| `jira issue transition <key> <id>` | Transition issue to a new state |

### Attachment Operations

| Command | Description |
|---------|-------------|
| `jira attachment get <id>` | Get attachment metadata |
| `jira attachment content <id>` | Download attachment content (with size limits) |

### Config Operations

| Command | Description |
|---------|-------------|
| `jira config test` | Test connectivity to JIRA server |
| `jira config show` | Show current configuration (redacted) |
| `jira config sample` | Output sample configuration file |

### Comments Pagination Strategy

To manage LLM context limits, comments are fetched with awareness of size:

- Default: Returns last 5 comments (configurable)
- `--limit N`: Fetch N comments
- `--offset N`: Skip first N comments
- `--all`: Fetch all comments (use with caution)
- `--summary-only`: Return only metadata, not full content

### Attachment Size Limits

Attachments have built-in size protection for LLM safety:

- Default max size: 100KB for content retrieval
- `--max-size N`: Override size limit (bytes)
- `--max-size 0`: No limit (use with caution)
- `--raw`: Output raw content to stdout (for piping)

## Configuration

### Config File Location
`~/.jira-tool.json`

### Config File Format
```json
{
  "server": "https://jira.example.com",
  "auth": {
    "type": "token",
    "token": "your-api-token"
  }
}
```

Or simplified:
```json
{
  "server": "https://jira.example.com",
  "token": "your-api-token"
}
```

### Environment Variable Overrides
- `JIRA_SERVER`: Override server URL
- `JIRA_TOKEN`: Override API token

Environment variables take precedence over config file values.

## Module Structure

```
jira_tool/
├── jira_tool/
│   ├── __init__.py       # Package exports
│   ├── cli.py            # CLI entry point using Click
│   ├── client.py         # JIRA REST API client
│   ├── config.py         # Configuration loading
│   ├── envelope.py       # Response envelope helpers
│   └── errors.py         # Error codes and exceptions
├── tests/
│   ├── unit/             # Unit tests (mocked HTTP)
│   └── integration/      # Integration tests (live API, read-only)
├── pyproject.toml        # Project configuration
├── .pre-commit-config.yaml
└── ARCHITECTURE.md
```

## JIRA REST API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/rest/api/2/issue/{key}` | GET | Get issue |
| `/rest/api/2/issue/{key}/comment` | GET | Get comments |
| `/rest/api/2/issue/{key}/comment` | POST | Add comment |
| `/rest/api/2/issue/{key}/transitions` | GET | Get available transitions |
| `/rest/api/2/issue/{key}/transitions` | POST | Perform transition |
| `/rest/api/2/search` | POST | Search with JQL |
| `/rest/api/2/issue` | POST | Create issue |
| `/rest/api/2/attachment/{id}` | GET | Get attachment metadata |
| `/rest/api/2/serverInfo` | GET | Get server info |

## Development

### Setup
```bash
# Install with uv
uv pip install --system -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

### Running Tests
```bash
# Unit tests only
pytest tests/unit/

# Integration tests (requires JIRA_SERVER and JIRA_TOKEN)
JIRA_SERVER=https://jira.example.com JIRA_TOKEN=xxx pytest tests/integration/

# All tests with coverage
pytest --cov=jira_tool --cov-report=term-missing
```

### Linting
```bash
# Check
ruff check jira_tool/ tests/

# Auto-fix
ruff check --fix jira_tool/ tests/
```

## Security Considerations

- Tokens are never logged or included in error output
- `config show` redacts token values
- Config file should have restricted permissions (0600)
- Attachment downloads have size limits to prevent memory issues

## Future Considerations

- Shared conventions with Gerrit tool (same envelope, error model, exit codes)
- Attachment upload support
- Bulk operations (with explicit opt-in)
- Webhook support for real-time updates
