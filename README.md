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

Shared utilities live in `llm_tool_common/`.

### Bundled submodules

- **lustre-drgn-tools** -- drgn-based Lustre vmcore analysis
  (`lustre_triage.py`, `obd_devs.py`, `ldlm_dumplocks.py`, etc.).
  Separate repo bundled as a git submodule. Requires drgn;
  run `lustre-drgn-tools/install-drgn.sh` to set up.

### Beta / experimental

- **patch_shepherd** -- automated patch series monitoring
  (`gerrit watch`). Not yet suitable for general use.

## Install

```bash
./install.sh            # install all tools
./install.sh --uninstall
```

Per-tool: `cd <tool_dir> && pip install -e .`

Requires Python 3.9+.

## Configuration

### Gerrit

Set environment variables directly or in a `.env` file
(searched in order: `./.env`, `~/.config/gerrit-cli/.env`,
`/etc/gerrit-cli/.env`):

```bash
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=your-username
GERRIT_PASS=your-http-password
```

To get your HTTP password: log into Gerrit, go to
Settings > HTTP Credentials > Generate Password.

Optional: `GERRIT_SSH_USER` for SSH operations (defaults to
`GERRIT_USER`).

Verify: `gerrit info <any-change-url>`

### JIRA

**Single instance** -- environment variables:

```bash
JIRA_SERVER=https://jira.example.com
JIRA_TOKEN=your-bearer-token
```

**Multiple instances** -- `~/.jira-tool.json`:

```json
{
  "instances": {
    "onprem": {
      "server": "https://jira.example.com",
      "auth": {"type": "bearer", "token": "..."}
    },
    "cloud": {
      "server": "https://yourorg.atlassian.net",
      "auth": {"type": "basic", "email": "you@co.com", "token": "..."}
    }
  },
  "default": "onprem"
}
```

Auth types:
- **bearer** -- for on-prem JIRA Server/Data Center. Create a
  Personal Access Token in your JIRA profile settings.
- **basic** -- for Atlassian Cloud. Uses your email + an API
  token created at https://id.atlassian.com/manage-profile/security/api-tokens

Select instance with `jira -I cloud get EX-1234`. Projects
listed in `JIRA_CLOUD_PROJECTS` (comma-separated env var) are
automatically routed to the cloud instance.

Verify: `jira get <any-issue-key>`

### Maloo

Maloo is the Lustre CI test results system at
testing.whamcloud.com.

```bash
MALOO_USER=your-username
MALOO_PASS=your-password
```

Verify: `maloo queue`

### Jenkins

```bash
JENKINS_URL=https://build.whamcloud.com
JENKINS_USER=your-username
JENKINS_TOKEN=your-api-token
```

To get your API token: log into Jenkins, go to your user
profile > Configure > API Token > Add new Token.

Verify: `jenkins build <any-build-url>`

### Other Tools

| Tool | Notes |
|------|-------|
| Janitor | Uses Gerrit credentials (no extra config) |
| Crash Tool | No auth required |

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
├── patch_shepherd/      # Patch series monitoring (beta)
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
