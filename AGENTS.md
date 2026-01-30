# LLM Code and Review Tools - Development Guide

This repository contains CLI tools for LLM agents: `jira` and `gerrit-comments`.

For tool usage documentation (to be installed in other repos), see [docs/TOOL_USAGE.md](docs/TOOL_USAGE.md).

## Project Structure

```
llm_code_and_review_tools/
├── jira_tool/           # JIRA CLI tool
│   ├── cli.py           # Click-based CLI
│   ├── client.py        # REST API client
│   ├── config.py        # Configuration
│   ├── envelope.py      # JSON response formatting
│   └── errors.py        # Error codes
├── gerrit_comments/     # Gerrit review tool
│   ├── cli.py           # Command handlers
│   ├── client.py        # Gerrit API client
│   └── ...
├── llm_tool_common/     # Shared utilities
├── docs/                # Documentation for tool users
│   └── TOOL_USAGE.md    # Agent instructions for using the tools
└── .beads/              # Issue tracking database
```

---

## Issue Tracking with Beads

This project uses **beads** (`bd`) for issue tracking. Issues are prefixed with `jira-`.

### Quick Reference

| Command | Action |
|---------|--------|
| `bd ready` | Find unblocked work |
| `bd list --status=open` | All open issues |
| `bd show <id>` | View issue details |
| `bd create --title="..." --type=feature --priority=2` | Create issue |
| `bd update <id> --status=in_progress` | Claim work |
| `bd close <id>` | Complete work |
| `bd stats` | Project health |

### Priority Values

Use numeric priorities 0-4:
- **P0**: Critical/blocking
- **P1**: High priority
- **P2**: Medium (default)
- **P3**: Low priority
- **P4**: Backlog

### Starting Work

```bash
bd ready                              # Find available work
bd show <id>                          # Review issue details
bd update <id> --status in_progress   # Claim it
```

### Completing Work

```bash
bd close <id>                         # Mark complete
bd sync                               # Sync beads with git
git add . && git commit -m "..."      # Commit changes
git push                              # Push to remote
```

### Important Notes

- **Do NOT use** `bd edit` - it opens an editor which blocks agents
- Run `bd prime` after context compaction or new session
- Use `bd doctor` to check for sync problems

---

## Development Workflow

### Building

```bash
pip install -e .                      # Install in development mode
# or
make install                          # Uses Makefile
```

### Testing

```bash
# Run all tests
pytest

# Run specific tool tests
pytest jira_tool/tests/
pytest gerrit_comments/tests/

# With coverage
pytest --cov=jira_tool --cov=gerrit_comments
```

### Code Style

- Use dataclasses for data structures
- Use type hints throughout
- Keep functions focused and under ~60 lines
- Follow existing patterns in the codebase
- All new functionality must include tests

### JSON Output Format

All tools use a standard envelope:

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

---

## Session Close Protocol

Before ending a work session:

1. `git status` - Check what changed
2. `git add <files>` - Stage changes
3. `bd sync` - Sync beads database
4. `git commit -m "..."` - Commit
5. `git push` - Push to remote (MANDATORY)

Work is NOT complete until `git push` succeeds.
