# LLM Code and Review Tools

CLI tools designed for LLM agents to interact with code review,
CI, issue tracking, and crash analysis systems.

## Tools

| Tool | Command | Purpose |
|------|---------|---------|
| **Gerrit CLI** | `gerrit` / `gc` | Gerrit code review -- comments, replies, reviewer management, patch series, Maloo triage |
| **JIRA** | `jira` | JIRA issue tracking -- get, search, comment, create, transition |
| **Maloo** | `maloo` | Lustre CI test results -- failures, retests, bug linking |
| **Jenkins** | `jenkins` | Jenkins build server -- build status, console logs, retriggers |
| **Janitor** | `janitor` | Gerrit Janitor test results (separate from Maloo/enforced CI) |
| **Crash Tool** | `crash-tool` | Non-interactive crash dump analysis with structured JSON output |
| **Patch Shepherd** | `gerrit watch` | Monitor patch series through CI and review |
| **lustre-drgn-tools** | `lustre_triage.py` etc. | drgn-based Lustre vmcore analysis (submodule) |

Shared utilities live in `llm_tool_common/`.

## Install

```bash
./install.sh            # install all tools
./install.sh --uninstall
```

Per-tool: `cd <tool_dir> && pip install -e .`

Requires Python 3.9+.

## Configuration

Tools are configured via environment variables or config files:

| Tool | Environment Variables | Config File |
|------|----------------------|-------------|
| JIRA | `JIRA_SERVER`, `JIRA_TOKEN` (on-prem); `JIRA_CLOUD_SERVER`, `JIRA_CLOUD_EMAIL`, `JIRA_CLOUD_TOKEN`, `JIRA_CLOUD_PROJECTS` (cloud) | `~/.jira-tool.json` |
| Gerrit | `GERRIT_URL`, `GERRIT_USER`, `GERRIT_PASS` | `~/.config/gerrit-cli/` |
| Maloo | `MALOO_USER`, `MALOO_PASS` | -- |
| Jenkins | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN` | -- |
| Janitor | -- | Uses Gerrit credentials |
| Crash Tool | -- | No auth required |

JIRA supports multiple instances. Projects listed in
`JIRA_CLOUD_PROJECTS` (comma-separated) route to Atlassian Cloud;
all others use the on-prem server. See `jira_tool/` for details.

## Output Format

All tools output raw JSON by default (no envelope). Use `--envelope`
for the full `{ok, data, meta}` wrapper. Use `--pretty` for
human-readable formatted output.

```json
{"ok": true, "data": {...}, "meta": {"tool": "jira", "command": "issue.get"}}
```

Exit codes: 0=success, 1=general error, 2=auth, 3=not found,
4=invalid input, 5=network.

## Project Structure

```
llm_code_and_review_tools/
├── gerrit_cli/          # Gerrit code review CLI
├── jira_tool/           # JIRA issue tracking CLI
├── maloo_tool/          # Maloo CI results CLI
├── jenkins_tool/        # Jenkins build server CLI
├── janitor_tool/        # Gerrit Janitor results CLI
├── crash_tool/          # Crash dump analysis CLI
├── patch_shepherd/      # Patch series monitoring
├── lustre-drgn-tools/   # drgn vmcore analysis (submodule)
├── llm_tool_common/     # Shared utilities
├── install.sh           # Unified installer
└── pyproject.toml       # Test configuration
```

## Development

```bash
pip install -e .          # Install in dev mode
pytest                    # Run all tests
```

Code style: dataclasses, type hints, functions under ~60 lines,
tests for new functionality. See CLAUDE.md for agent instructions.

## License

BSD 2-Clause. See [LICENSE](LICENSE).
