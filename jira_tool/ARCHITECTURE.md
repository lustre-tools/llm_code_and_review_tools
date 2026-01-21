# JIRA Tool Architecture

## Overview

This tool provides a thin, LLM-agent-focused CLI wrapper around the JIRA REST API. It is designed for deterministic behavior, strict JSON output, and stable command semantics.

## Design Principles

1. **JSON-only Interface**: All output is structured JSON. Human-readable formatting is available via `--pretty` flag.
2. **Explicit Inputs**: No implicit defaults. Required fields must be provided explicitly. Fail fast with clear error codes.
3. **Deterministic Behavior**: Same input produces same output shape. No silent field truncation.
4. **Thin Command Surface**: Minimal commands covering core agent workflows.
5. **LLM Context Awareness**: Comments and attachments support pagination/size limits to avoid overwhelming LLM context windows.

## Standard Response Envelope

### Success Response
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

### Error Response
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

## Exit Codes

- `0`: Success
- `1`: General error
- `2`: Authentication error
- `3`: Not found
- `4`: Invalid input
- `5`: Network/connection error

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
