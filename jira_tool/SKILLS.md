# JIRA Tool Skills

This document describes how to use the `jira` CLI tool for interacting with JIRA issues.

## Setup

Set environment variables before using:
```bash
export JIRA_SERVER="https://jira.example.com"
export JIRA_TOKEN="your-api-token"
```

Or use command-line options: `--server` and `--token`

## Output Format

All commands return JSON with this structure:
- `ok`: boolean indicating success
- `data`: the response payload (on success)
- `error`: error details (on failure)
- `meta`: metadata including tool name, command, and timestamp

Use `--pretty` flag for human-readable formatted output.

## Common Workflows

### Get Issue Details
```bash
jira issue get LU-12345
```
Returns: key, summary, description, status, priority, assignee, reporter, labels, dates

### Read Comments (Context-Aware)
```bash
# Default: last 5 comments (safe for LLM context)
jira issue comments LU-12345

# More comments
jira issue comments LU-12345 --limit 10

# Pagination
jira issue comments LU-12345 --limit 5 --offset 5

# Summary only (minimal context usage)
jira issue comments LU-12345 --summary-only
```

### Search Issues
```bash
# Basic JQL search
jira issue search "project = LU AND status = Open"

# With pagination
jira issue search "project = LU ORDER BY created DESC" --limit 10 --offset 0
```

### List Attachments
```bash
jira issue attachments LU-12345
```
Returns: id, filename, size, mime_type, author, created, content_url

### Get Attachment Content
```bash
# Small text files (default 100KB limit)
jira attachment content 12345

# Larger files (specify limit in bytes)
jira attachment content 12345 --max-size 1048576

# Raw output (for piping to file)
jira attachment content 12345 --raw > file.txt
```

### Check Available Transitions
```bash
jira issue transitions LU-12345
```
Returns list of: id, name, to_status

### Perform Transition
```bash
# Transition by ID (get ID from transitions command)
jira issue transition LU-12345 31

# With comment
jira issue transition LU-12345 31 --comment "Moving to In Progress"
```

### Add Comment
```bash
jira issue comment LU-12345 "This is my comment text"
```

### Create Issue
```bash
jira issue create --project LU --type Bug --summary "Bug title" --description "Details"
```

## Error Handling

Exit codes:
- 0: Success
- 1: General error
- 2: Authentication error
- 3: Not found
- 4: Invalid input
- 5: Network error

Error responses include:
- `code`: Machine-readable error code (e.g., "AUTH_FAILED", "ISSUE_NOT_FOUND")
- `message`: Human-readable description
- `http_status`: HTTP status code (if applicable)
- `details`: Additional context

## Tips for LLM Agents

1. **Start with search or get** to understand context before making changes
2. **Use `--limit` for comments** to avoid context overflow
3. **Check transitions** before attempting to transition an issue
4. **Use `--summary-only`** when you just need comment metadata
5. **Check attachment size** before downloading content
6. **Parse the `ok` field** first to determine success/failure

## Example Session

```bash
# 1. Find issues assigned to me
jira issue search "assignee = currentUser() AND status != Done" --limit 5

# 2. Get details on one
jira issue get LU-12345

# 3. Read recent comments
jira issue comments LU-12345 --limit 3

# 4. Check if there are attachments
jira issue attachments LU-12345

# 5. Read a small attachment
jira attachment content 67890

# 6. Check what transitions are available
jira issue transitions LU-12345

# 7. Move to In Progress (if transition ID 31 is available)
jira issue transition LU-12345 31 --comment "Starting work on this"
```
