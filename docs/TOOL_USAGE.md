# AI Agent Guidelines

This document provides guidance for AI agents using these tools.

## Tools Overview

| Tool | Purpose | Commands |
|------|---------|----------|
| `bd` | Task tracking (beads) | `bd ready`, `bd create`, `bd close`, etc. |
| `jira` | JIRA issue tracking | `jira issue get`, `jira issue search`, etc. |
| `gerrit-comments` | Gerrit code review | `gerrit-comments extract`, `gerrit-comments review`, etc. |

---

# Beads (bd) Task Tracking

This project uses **bd (beads)** for issue tracking.
Run `bd prime` for workflow context, or the hooks will auto-inject it at session start.

## Quick Reference

| Command | Action |
|---------|--------|
| `bd ready` | Find unblocked work |
| `bd create "Title" --type task --priority 2` | Create issue |
| `bd update <id> --status in_progress` | Claim work |
| `bd close <id>` | Complete work |
| `bd sync` | Sync with git (run at session end) |

## Priority Values

Use numeric priorities 0-4 (NOT "high"/"medium"/"low"):
- **P0**: Critical/blocking
- **P1**: High priority
- **P2**: Medium (default)
- **P3**: Low priority
- **P4**: Backlog

## Common Workflows

### Starting Work
```bash
bd ready                              # Find available work
bd show <id>                          # Review issue details
bd update <id> --status in_progress   # Claim it
```

### Completing Work
```bash
bd close <id>                         # Mark complete
bd sync --from-main                   # Pull beads updates from main
git add . && git commit -m "..."      # Commit your changes
```

### Creating Dependent Work
```bash
bd create --title="Implement feature X" --type=feature
bd create --title="Write tests for X" --type=task
bd dep add <tests-id> <feature-id>    # Tests depend on feature
```

### Viewing Dependencies
```bash
bd show <id>                          # See blockers and blocked-by
bd blocked                            # Show all blocked issues
bd dep tree <id>                      # View dependency tree
```

## Session Close Protocol

Before ending a session, run this checklist:
1. `git status` - Check what changed
2. `git add <files>` - Stage code changes
3. `bd sync --from-main` - Pull beads updates
4. `git commit -m "..."` - Commit changes

## Important Notes

- **Do NOT use** `bd edit` - it opens an editor which blocks agents
- **Do NOT use** markdown files or other tools for task tracking - use beads
- Run `bd prime` after context compaction or new session for full workflow context
- Use `bd stats` to see project health (open/closed/blocked counts)
- Use `bd doctor` to check for sync problems or missing hooks

---

# JIRA Tool

## Configuration

```bash
export JIRA_SERVER="https://jira.example.com"
export JIRA_TOKEN="your-api-token"
```

## Common Commands

```bash
# Get issue details
jira issue get LU-12345

# Search issues
jira issue search "project = LU AND status = Open" --limit 10

# Read comments (context-aware pagination)
jira issue comments LU-12345 --limit 5

# List attachments
jira issue attachments LU-12345

# Get attachment content (with size limit)
jira attachment content 12345 --max-size 100000

# Check available transitions
jira issue transitions LU-12345

# Transition issue
jira issue transition LU-12345 31 --comment "Moving to In Progress"

# Add comment
jira issue comment LU-12345 "Comment text"

# Create issue
jira issue create --project LU --type Bug --summary "Bug title"
```

## Tips

1. **Start with search or get** to understand context before making changes
2. **Use `--limit` for comments** to avoid context overflow
3. **Check transitions** before attempting to transition an issue
4. **Use `--summary-only`** when you just need comment metadata
5. **Check attachment size** before downloading content

---

# Gerrit Comments Tool

## Configuration

```bash
export GERRIT_URL="https://review.whamcloud.com"
export GERRIT_USER="your-username"
export GERRIT_PASS="your-http-password"
```

## Workflow Commands

### Review Series (Recommended Workflow)

```bash
# 1. Start reviewing - checks out first patch with comments
gerrit-comments review-series <URL>

# 2. For each patch: fix issues, stage replies, finish
gerrit-comments stage --done <index>        # Mark comment as done
gerrit-comments stage <index> "message"     # Reply with message
git add <files> && git commit --amend --no-edit
gerrit-comments finish-patch                # Auto-advances to next

# 3. When done
gerrit-comments abort --keep-changes
git push origin HEAD:refs/for/master
```

### Information Commands

```bash
# Extract unresolved comments with code context
gerrit-comments extract <URL>

# Get code changes for review
gerrit-comments review <URL>

# Find all patches in a series
gerrit-comments series <URL>

# Get comments from all patches in series
gerrit-comments series-comments <URL>

# Show status of all patches
gerrit-comments series-status <URL>
```

### Navigation

```bash
# Jump to specific patch
gerrit-comments work-on-patch <URL> <change>

# Manually advance to next patch
gerrit-comments next-patch

# Check session status
gerrit-comments status
```

### Staging Management

```bash
# List staged operations
gerrit-comments staged list

# Show staged for specific change
gerrit-comments staged show <change>

# Push staged operations
gerrit-comments push <change>

# Clear staged operations
gerrit-comments staged clear [change]
```

### Reply to Comments

```bash
# Reply to thread 0 with message
gerrit-comments reply <URL> 0 "Fixed in next patchset"

# Mark as done
gerrit-comments reply --done <URL> 0

# Acknowledge
gerrit-comments reply --ack <URL> 1
```

### Interactive Mode

```bash
gerrit-comments interactive <URL>
# Actions: d=done, r=reply, a=ack, s=skip, q=quit, p=push
```

## Tips

1. **Work earliest to latest** - finish-patch rebases all later patches
2. **Stage replies as you fix** - don't forget before finish-patch
3. **finish-patch auto-advances** - finds next patch with comments
4. **Conflicts?** - fix, `git add`, then `finish-patch` again
5. **Use `series-status`** before starting work on a series

---

# Output Format

All tools (jira, gerrit-comments) use a standard JSON response envelope:

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

On error:
```json
{
  "ok": false,
  "error": {
    "code": "AUTH_FAILED",
    "message": "Authentication failed",
    "http_status": 401
  },
  "meta": { ... }
}
```

**Always check the `ok` field first** to determine success/failure.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Authentication error |
| 3 | Not found |
| 4 | Invalid input |
| 5 | Network error |

---

# Testing

```bash
# JIRA tool tests
cd jira_tool && pytest tests/

# Gerrit comments tests
cd gerrit_comments && pytest gerrit_comments/tests/
```

---

# Architecture

## JIRA Tool Structure

```
jira_tool/
├── cli.py            # CLI entry point
├── client.py         # JIRA REST API client
├── config.py         # Configuration loading
├── envelope.py       # Response envelope helpers
└── errors.py         # Error codes and exceptions
```

## Gerrit Comments Structure

```
gerrit_comments/
├── cli.py            # Command handlers
├── parsers.py        # Argparse definitions
├── client.py         # Gerrit REST API client
├── models.py         # Data models
├── extractor.py      # Comment extraction
├── reviewer.py       # Review workflow
├── replier.py        # Reply submission
├── series.py         # Patch series discovery
├── staging.py        # Pending replies
├── rebase.py         # Rebase workflow
└── git_utils.py      # Git helpers
```

## Layer Diagram (Gerrit Comments)

```
CLI Layer        cli.py + parsers.py
     |
     v
Workflow Layer   reviewer.py, series.py, rebase.py, replier.py
     |
     v
Core Layer       client.py, models.py, extractor.py, staging.py
     |
     v
Utility Layer    git_utils.py, tmux_vim.py
```

---

# Code Style

- Use dataclasses for data structures
- Use type hints
- Keep functions focused and under ~60 lines
- Follow existing patterns in the codebase
- All new functionality must include tests

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
