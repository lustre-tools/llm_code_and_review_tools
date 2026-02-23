# LLM Code and Review Tools

CLI tools designed for LLM agents to interact with code review and issue tracking systems.

## Tools

| Tool | Purpose | Directory |
|------|---------|-----------|
| **jira** | JIRA issue tracking | [jira_tool/](jira_tool/) |
| **gerrit-cli** | Gerrit code review comments | [gerrit_cli/](gerrit_cli/) |
| **jenkins** | Jenkins build server | [jenkins_tool/](jenkins_tool/) |
| **maloo** | Lustre CI test results | [maloo_tool/](maloo_tool/) |

## Design Philosophy

These tools share common design principles:

- **Structured JSON output** with consistent envelope format (`ok`, `data`/`error`, `meta`) — use `--json` flag on commands that default to human-readable output
- **Deterministic behavior** - same input produces same output shape
- **Context-aware pagination** - built-in limits to avoid overwhelming LLM context windows
- **Explicit inputs** - no implicit defaults, fail fast with clear error codes
- **Human-readable by default** (jenkins) or JSON by default (jira, gerrit-comments) — all support both modes

## Quick Start

### JIRA Tool

```bash
# Install
cd jira_tool && pip install -e .

# Configure
export JIRA_SERVER="https://jira.example.com"
export JIRA_TOKEN="your-api-token"

# Use
jira issue get PROJ-123
jira issue search "project = PROJ AND status = Open"
jira issue comments PROJ-123 --limit 5
```

### Gerrit CLI Tool

```bash
# Install
cd gerrit_cli && pip install -e .

# Configure
export GERRIT_URL="https://review.example.com"
export GERRIT_USER="your-username"
export GERRIT_PASS="your-http-password"

# Use
gerrit-cli extract https://review.example.com/c/project/+/12345
gerrit-cli review https://review.example.com/c/project/+/12345
gerrit-cli series https://review.example.com/c/project/+/12345
```

### Jenkins Tool

```bash
# Install
cd jenkins_tool && pip install -e .

# Configure
export JENKINS_URL="https://build.whamcloud.com"
export JENKINS_USER="your-username"
export JENKINS_TOKEN="your-api-token"

# Use (read-only) — human-readable output by default; add --json for machine output
jenkins jobs                             # All jobs with status
jenkins jobs --view lustre               # Jobs in a specific view
jenkins builds lustre-reviews --limit 10 # Recent builds for a job
jenkins build lustre-reviews 121881      # Build details + matrix runs (per-config status)
jenkins build lustre-master lastFailedBuild  # Last failed build
jenkins console lustre-reviews 121880    # Console output (last 200 lines)
jenkins console lustre-master 4704 --grep "error"  # Search console output
jenkins run-console lustre-reviews 121880 "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel"
jenkins review 54225                     # Find builds for a Gerrit change number

# Write operations
jenkins abort lustre-reviews 121884      # Abort a running build and all sub-builds
jenkins abort lustre-reviews 121884 --kill  # Hard-kill if graceful abort doesn't work
jenkins retrigger lustre-reviews 121880  # Retrigger via Gerrit Trigger plugin

# Machine/agent usage — add --json to any command for JSON envelope output
jenkins builds lustre-reviews --limit 10 --json
jenkins build lustre-reviews 121881 --json
```

Output defaults to human-readable tables and plain text. Use `--json` on any command to get the
standard JSON envelope (`ok`, `data`, `meta`) for programmatic consumption. Errors always go to
stderr in human mode, or to stdout as `{"ok": false, "error": {...}}` in `--json` mode.

Matrix builds (e.g. `lustre-reviews`) run sub-builds per configuration (arch, distro, build type).
The `build` command lists all runs with per-config status; `run-console` fetches logs for a specific config.
The `retrigger` command uses the Gerrit Trigger plugin, so the build gets the correct Gerrit refspec.

## Documentation

- **[AGENTS.md](AGENTS.md)** - Combined guide for AI agents (recommended starting point)

Per-tool documentation:

- **JIRA Tool**: [README](jira_tool/README.md) | [Architecture](jira_tool/ARCHITECTURE.md) | [Skills](jira_tool/SKILLS.md)
- **Gerrit CLI**: [README](gerrit_cli/README.md) | [Architecture](gerrit_cli/ARCHITECTURE.md) | [Skills](gerrit_cli/SKILLS.md)
- **Jenkins Tool**: [README](jenkins_tool/README.md)
- **Maloo Tool**: [README](maloo_tool/README.md)

## Output Format

All tools use a standard JSON response envelope:

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

## License

MIT
