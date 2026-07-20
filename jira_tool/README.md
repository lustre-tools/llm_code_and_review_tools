# JIRA Tool

A thin, LLM-agent-focused CLI wrapper around the JIRA REST API.

## Installation

```bash
# Using pip
pip install -e .

# Using uv (recommended)
uv pip install -e .
```

## Configuration

Configuration sources, in priority order:

1. Command-line options: `--server` and `--token`
2. Environment variables: `JIRA_SERVER` and `JIRA_TOKEN`
3. Config file `~/.jira-tool.json` (path overridable with `--config`)

```bash
export JIRA_SERVER="https://jira.example.com"
export JIRA_TOKEN="your-api-token"
```

The config file may define named instances under `"instances"` with a
`"default"` key; select one with `-I <name>` (e.g. `jira -I cloud get
EX-1234`). `jira config sample` emits a ready-to-edit sample file.
Exception to the priority order: when an instance is selected
explicitly with `-I`, its config takes precedence over the
`JIRA_SERVER`/`JIRA_TOKEN` environment variables (`--server`/`--token`
still override everything).

Automatic cloud routing: projects listed in the `JIRA_CLOUD_PROJECTS`
environment variable (comma-separated prefixes) are automatically
routed to the Cloud instance defined by the `JIRA_CLOUD_SERVER`,
`JIRA_CLOUD_EMAIL`, and `JIRA_CLOUD_TOKEN` environment variables; an
explicit `-I` overrides auto-routing.

## Quick Start

```bash
# Get issue details
jira get PROJ-123

# Get issue with comments inline
jira get PROJ-123 --comments 5

# Search issues
jira search "project = PROJ AND status = Open"

# Read comments (with pagination)
jira comments PROJ-123 --limit 5

# List attachments
jira attachments PROJ-123

# Check available transitions
jira transitions PROJ-123

# Add a comment
jira comment PROJ-123 "My comment text"

# Create an issue
jira create --project PROJ --type Bug --summary "Bug title"
```

## Output Format

By default, commands output only the raw JSON data payload (no wrapper):

```json
{"key": "PROJ-123", "summary": "...", ...}
```

Use `--envelope` to include the full `ok`/`data`/`meta` wrapper:

```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "tool": "jira",
    "command": "get",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

On failure, the default output is likewise the bare error object;
`--envelope` wraps it as `{"ok": false, "error": ..., "meta": ...}`.

Use `--pretty` for human-readable formatted output. Like `--envelope`,
it works in any position: `jira --pretty get KEY` or `jira get KEY --pretty`.

## Commands

These tables cover the most common operations only; run `jira --help`
for the full command list (linking, labels, components, fix versions,
subtasks, worklogs, watchers, users, filters, project metadata, etc.)
or `jira describe` for machine-readable API documentation.

### Issue Operations

| Command | Description |
|---------|-------------|
| `jira get <key>` | Get issue details (add `--comments N` to inline the N most recent comments) |
| `jira comments <key>` | Get comments with pagination |
| `jira attachments <key>` | List attachments |
| `jira search <jql>` | Search with JQL |
| `jira create` | Create a new issue |
| `jira comment <key> <body>` | Add a comment |
| `jira transitions <key>` | List available transitions |
| `jira transition <key> <id>` | Transition to new state |

### Attachment Operations

| Command | Description |
|---------|-------------|
| `jira attachment get <id>` | Get attachment metadata |
| `jira attachment content <id>` | Download content (with size limits) |
| `jira attachment upload <key> <file>` | Upload an attachment to an issue (`jira attach` is a top-level alias) |
| `jira attachment delete <id>` | Delete an attachment |

### Config Operations

| Command | Description |
|---------|-------------|
| `jira config test` | Test connectivity |
| `jira config show` | Show configuration (redacted) |
| `jira config sample` | Output a sample configuration file |

## LLM Context Awareness

Built-in protections for LLM context windows:

- **Comments**: Default limit of 10, use `--limit N` for more
- **Attachments**: Default 100KB limit, use `--max-size N` to override
- **Search**: Default 20 results, use `--limit N` for more

## Documentation

- [Agent usage guide](../AGENTS.md) - Repo-level usage documentation for LLM agents

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Lint
ruff check jira_tool/ tests/
```

## License

MIT
