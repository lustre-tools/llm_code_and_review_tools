# LLM Code and Review Tools

CLI tools designed for LLM agents to interact with code review and issue tracking systems.

## Tools

| Tool | Purpose | Directory |
|------|---------|-----------|
| **jira** | JIRA issue tracking | [jira_tool/](jira_tool/) |
| **gerrit-cli** / **gc** | Gerrit code review | [gerrit_cli/](gerrit_cli/) |
| **jenkins** | Jenkins build server | [jenkins_tool/](jenkins_tool/) |
| **maloo** | Lustre CI test results | [maloo_tool/](maloo_tool/) |

For agent usage instructions, see [docs/TOOL_USAGE.md](docs/TOOL_USAGE.md).

## Design Philosophy

- **Structured JSON output** with consistent envelope (`ok`, `data`/`error`, `meta`) — use `--pretty` for formatted output
- **Deterministic behavior** - same input produces same output shape
- **Context-aware pagination** - built-in limits to avoid overwhelming LLM context windows
- **Explicit inputs** - no implicit defaults, fail fast with clear error codes

## Quick Start

```bash
# Install all tools
cd /shared/llm_code_and_review_tools
./install.sh
```

Per-tool install: `cd <tool_dir> && pip install -e .`

Environment variables per tool:

| Tool | Variables |
|------|-----------|
| jira | `JIRA_SERVER`, `JIRA_TOKEN` (on-prem); `JIRA_CLOUD_SERVER`, `JIRA_CLOUD_EMAIL`, `JIRA_CLOUD_TOKEN`, `JIRA_CLOUD_PROJECTS` (cloud) |
| gerrit | `GERRIT_URL`, `GERRIT_USER`, `GERRIT_PASS` |
| jenkins | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN` |
| maloo | `MALOO_USER`, `MALOO_PASS` |

Cloud routing: projects listed in `JIRA_CLOUD_PROJECTS` (comma-separated) are automatically routed to the Atlassian Cloud instance. All other projects use the on-prem server. See `jira_tool/` README for multi-instance config via `~/.jira-tool.json`.

## Output Format

All tools use a standard JSON response envelope:

```json
{"ok": true, "data": {...}, "meta": {"tool": "jira", "command": "issue.get", "timestamp": "2024-01-15T10:30:00Z"}}
```

Exit codes: 0=success, 1=general, 2=auth, 3=not found, 4=invalid input, 5=network.

---

## Project Structure

```
llm_code_and_review_tools/
├── jira_tool/           # JIRA CLI tool
├── gerrit_cli/          # Gerrit review tool
├── jenkins_tool/        # Jenkins build tool
├── maloo_tool/          # Maloo CI results tool
├── llm_tool_common/     # Shared utilities
├── docs/                # Agent usage documentation
└── .beads/              # Issue tracking database
```

## Issue Tracking with Beads

This project uses **beads** (`bd`) for issue tracking. Issues are prefixed with `jira-`.

**IMPORTANT**: This repo uses a separate `beads-sync` branch (configured in `.beads/config.yaml`) to keep beads metadata commits out of the main git history. Do NOT comment out the `sync-branch` setting.

| Command | Action |
|---------|--------|
| `bd ready` | Find unblocked work |
| `bd show <id>` | View issue details |
| `bd update <id> --status in_progress` | Claim work |
| `bd close <id>` | Complete work |
| `bd sync` | Export to JSONL, commit to beads-sync branch |

**Claiming a bead (full sequence):**
```bash
bd ready                              # Find available work
bd update <id> --status in_progress   # Claim locally
bd sync                               # Commit to beads-sync branch
git push origin beads-sync            # Push immediately!
```

Only after `git push` succeeds is the bead truly claimed. Without pushing, another worker may claim the same bead.

**Do NOT use** `bd edit` — it opens an editor which blocks agents.
Run `bd prime` after context compaction or new session.

## Development

```bash
pip install -e .                      # Install in development mode
pytest                                # Run all tests
pytest --cov=jira_tool --cov=gerrit_cli  # With coverage
```

Code style: dataclasses, type hints, functions under ~60 lines, tests for all new functionality.

## Session Close Protocol

Before ending a work session:

1. `git status` — check what changed
2. `git add <files>` && `git commit -m "..."`
3. `bd sync` — sync beads database
4. `git push origin HEAD` — push code changes (MANDATORY)
5. `git push origin beads-sync` — push beads metadata (MANDATORY)

Work is NOT complete until both pushes succeed.

## License

BSD 2-Clause. See [LICENSE](LICENSE).
