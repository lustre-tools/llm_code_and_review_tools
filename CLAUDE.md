# LLM Code and Review Tools

Repository of CLI tools designed for LLM agent use. Each tool
outputs JSON to stdout by default (no envelope wrapper). Use
`--envelope` for the full `{ok, data, meta}` wrapper. Do NOT
use `--pretty` — raw JSON is preferred; agents parse it directly.
Exception: `lreview` is an operator-facing orchestrator and prints
human-readable colored output (no `--envelope`).

## Tools

### JIRA tool (`jira`, v0.5.2)

Bug tracking, issue management, and test failure research.

**Key commands:** `jira get`, `jira search` (JQL), `jira comment`,
`jira create`, `jira update`, `jira link`, `jira transition`,
`jira assign`, `jira filter list/export/import`.

**Automatic cloud routing:** Projects listed in `JIRA_CLOUD_PROJECTS`
are automatically routed to the Cloud instance. No `-I` flag needed.
Routing is by project prefix — extracted from issue keys and JQL.
`-I` overrides auto-routing when specified explicitly.

**Multi-instance support:** `-I <name>` selects a named instance
from `~/.jira-tool.json`. Without `-I`, auto-routes by project
prefix or falls back to the default instance.

**Cloud vs Server:** The tool auto-detects JIRA Cloud instances
(`.atlassian.net`) and handles API differences transparently:
- Uses REST API v3 for Cloud, v2 for Server
- Converts description/comment text to Atlassian Document Format
  (ADF) on write, and ADF back to plain text on read
- Uses `accountId` instead of `username` for Cloud (GDPR mode)
- Resolves display names to `accountId` automatically for
  assign, watch, and unwatch commands
- Uses `nextPageToken` pagination for Cloud search

**Configuration (`~/.jira-tool.json`):**
```json
{
  "instances": {
    "lu": {
      "server": "https://jira.whamcloud.com",
      "auth": {
        "type": "bearer",
        "token": "<Whamcloud personal access token>"
      }
    },
    "cloud": {
      "server": "https://your-org.atlassian.net",
      "auth": {
        "type": "basic",
        "email": "<your email>",
        "token": "<Atlassian API token>"
      }
    }
  },
  "default": "lu"
}
```

**Cloud routing env vars (`~/.zshrc`):**
```bash
JIRA_CLOUD_SERVER="https://your-org.atlassian.net"
JIRA_CLOUD_EMAIL="user@example.com"
JIRA_CLOUD_TOKEN="<Atlassian API token>"
JIRA_CLOUD_PROJECTS="PROJ1,PROJ2"   # comma-separated project prefixes
```

**Token generation:**
- Server PAT: JIRA → Profile → Personal Access Tokens
- Cloud API token: `id.atlassian.com/manage-profile/security/api-tokens`

Run `jira --help` for full command list.

### Confluence tool (`confluence`, aliased as `cf`, v0.1.0)

Read-only Confluence wiki search and page reads.

**Key commands:** `confluence search <text>` (`--space`, `--cql`),
`confluence get <id-or-url>`, `confluence spaces`, `confluence check`,
`confluence config-sample`, `confluence describe`. No write commands.

**Multi-instance (`~/.confluence-tool.json`):** same shape as
`~/.jira-tool.json`; `-I <name>` selects an instance, default is `cloud`.
- `cloud` — Confluence Cloud (`.atlassian.net/wiki`), basic auth (email +
  Atlassian API token, the same token as the JIRA `cloud` instance). Uses
  `{server}/rest/api`.
- `whamcloud` — the public Lustre community wiki (`wiki.whamcloud.com`),
  `auth.type = "none"`. Reached **anonymously and read-only**, which
  structurally guarantees only public (open-source) Lustre articles are
  accessible there.

```json
{
  "instances": {
    "cloud": {
      "server": "https://your-org.atlassian.net/wiki",
      "auth": {"type": "basic", "email": "<email>", "token": "<Atlassian API token>"}
    },
    "whamcloud": {
      "server": "https://wiki.whamcloud.com",
      "auth": {"type": "none"}
    }
  },
  "default": "cloud"
}
```

Run `confluence --help` or `confluence describe` for the full surface.

### Gerrit tool (`gerrit`, aliased as `gc`, v0.2.4)

Code review, comment management, patch workflows, and CI triage.

**Key commands:**
- **Review:** `gc comments <url>`,
  `gc reply <idx> "msg" [--url <url>]` (URL defaults to the one
  remembered from the last `gc comments`), `gc review <url>`,
  `gc done <url> <idx>`, `gc ack <url> <idx>`
- **Post review from JSON:** `gc review --post-comments <file> <url>`
  posts a cover message + inline line comments in one call. The file
  holds `message`/`vote`/`tag`/`comments` (Gerrit REST dict or flat
  list). `--prefix '[Marc Bot]'` prepends a bot name to every message;
  `--dry-run` previews the exact payload without posting. See
  `gerrit_cli/README.md` ("Posting a Review from JSON").
- **Patch workflow:** `gc work-on-patch <url>`, `gc finish-patch`,
  `gc next-patch`, `gc abort`, `gc status`
- **Info:** `gc info <url>`, `gc series-info <url>`,
  `gc series-status <url>`, `gc related <url>`, `gc diff <url>`
- **CI:** `gc maloo <url>`, `gc watch <file>`
- **Search:** `gc search <query>`, `gc s <query>`
- **Manage:** `gc vote <url> <label> <score>`, `gc message <url> "msg"`,
  `gc set-topic <url> <topic>`, `gc hashtag <url> --add <tag>`,
  `gc rebase <url>`, `gc abandon <url>`, `gc restore <url>`,
  `gc checkout <url>`
- **Reviewers:** `gc reviewers <url>`, `gc add-reviewer <url> <name>`,
  `gc remove-reviewer <url> <name>`, `gc find-user <name>`

**Configuration:** Environment variables in `.env` file, loaded
from (in priority order, highest first):
1. `./.env`
2. `~/.config/gerrit-cli/.env`
3. `/etc/gerrit-cli/.env`
4. `/shared/support_files/.env`

All matching files are loaded; higher-priority files override
values from lower-priority ones.

**Required env vars:**
```bash
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=<your Gerrit username>
GERRIT_PASS=<your Gerrit HTTP password>
```

**HTTP password:** Log into `review.whamcloud.com` → Settings →
HTTP Credentials → Generate Password.

Run `gerrit --help` for full command list.
Run `gc examples` for common workflow examples.
Run `gc explain <command>` for detailed usage of a command.

### lreview tool (`lreview`, v0.1.0)

Runs the kreview AI patch-review skill (from the external
[review-prompts](https://github.com/verygreen/review-prompts/) repo) on
a batch of Gerrit changes in parallel — one headless agent process per
change (Claude Code by default; codex/gemini/opencode via `--agent` /
`$LREVIEW_AGENT`, best-effort), each in its own git worktree — then
posts the collected `gerrit-review.json` results via gerrit-cli.

```bash
lreview check                        # Verify claude + skill setup
lreview run 64086 64087 --repo lustre-release   # Review (no post)
lreview post                         # Post collected findings
lreview run 64086 --post             # Review + post in one go
```

Defaults: opus model (`--model` / `$LREVIEW_MODEL` for sonnet/fable),
5 parallel reviews (`--jobs`), 2h per-review timeout, results in
`./lreview-results/` (per-change JSON + log + summary.json), a live
status line with token counter while reviews run, and posted messages
prefixed `[AI review - <model>]` on a bold own line (`--prefix` /
`$LREVIEW_PREFIX` override; a `<model>` placeholder in a custom prefix
is substituted too). Posting is pinned to the reviewed
patchset revision and guarded against double-posting. If the kreview
skill is not installed, the tool offers to clone review-prompts and
run its `setup.sh claude kernel`. See `lreview/README.md`.

### Other tools

Also installed from this repo:
- `maloo` — Lustre CI test results (testing.whamcloud.com);
  needs `MALOO_USER`/`MALOO_PASS`. See `maloo_tool/README.md`.
- `jenkins` — Jenkins build server (build.whamcloud.com); needs
  `JENKINS_USER`/`JENKINS_TOKEN`. See `jenkins_tool/README.md`.
- `janitor` — Gerrit Janitor test results; no auth, optional
  `JANITOR_URL` env var. See the top-level `README.md`.
- `lustre-crash` — drgn-based crash-dump analysis; no auth.
  See `lustre_crash/README.md`.

## Development

All tools share `llm-tool-common` for envelope formatting and
error handling. Each tool is an editable pip install from its
subdirectory.

**Install a tool for development:**
```bash
cd jira_tool && pip install -e .
cd gerrit_cli && pip install -e .
```

**Testing:** `pytest` from each tool directory. Use `-m unit`
for unit tests, `-m integration` for tests requiring network.

## Output format

All tools except `lreview` follow the same output convention:
- **Default:** JSON data payload only (no wrapper)
- **`--envelope`:** Full `{ok: bool, data: ..., meta: ...}` wrapper
- **`--debug`:** (jira) Debug output to stderr
- **Exit codes:** 0 = success, 1 = general error, 2 = auth error,
  3 = not found, 4 = invalid input, 5 = network error
