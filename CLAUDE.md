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
Reads (`get`, `search`, `comments`) work with no token against a public
Jira; writes need one.

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
- Converts description/comment Markdown to Atlassian Document Format
  (ADF) on write, and ADF back to the same Markdown on read: code
  fences, lists (nested), `>` quotes, tables, `code`, `**bold**`,
  `*italic*`, `[text](url)`, and `@[Display Name]` mentions
- Threaded replies: `jira comment KEY --reply-to <comment id> "..."`;
  comments in a thread carry `parent_id` (the thread's root comment)
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
- A `.env` set with a Cloud `JIRA_SERVER` and `JIRA_CLOUD_EMAIL` uses
  basic auth; scoped Cloud tokens are routed through
  `api.atlassian.com/ex/jira/<cloud id>` automatically
  (`JIRA_CLOUD_ID` to pin it, `site` to disable)

Run `jira --help` for full command list.

### Gerrit tool (`gerrit`, aliased as `gc`, v0.2.5)

Code review, comment management, patch workflows, and CI triage.
Reads work with only `GERRIT_URL`; anything that changes a patch needs
`GERRIT_USER` and `GERRIT_PASS`.

Every command that takes a change accepts a URL, a bare change number or
a Change-Id (bare, or Gerrit's `project~branch~Change-Id` triplet) --
the Change-Id being what a checkout has to hand, from the commit
message. A Change-Id shared across branches resolves to the open change;
if several are open the error lists them.

**Operating mode (`GERRIT_CLI_MODE`):** `full` (the default, also for
an unset or empty variable) publishes immediately; `drafts` (and any
other value) writes every comment as a Gerrit draft, visible only to
the account owner until they publish it in the web UI. In `drafts` mode a cover
message becomes a patchset-level draft, votes are refused (Gerrit has
no draft vote), and non-comment operations (reviewers, topic, hashtag,
abandon, restore, rebase) are unaffected. Gated commands report
`"gerrit_cli_mode"` and `"published"` in their JSON. The account
owner sets this variable; agents must not set, unset, or override it.

**Key commands:**
- **Review:** `gc comments <url>`,
  `gc reply <idx|comment_id> "msg" [--url <url>]` (URL defaults to
  the one remembered from the last `gc comments`; indices shift as
  threads resolve, so use the thread's `root_comment.id` for more
  than one reply), `gc review <url>`,
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
  `gc series-status <url>`, `gc related <url>`,
  `gc diff <url> [A [B]]` (between patchsets; default the newest
  against the one before)
- **CI:** `gc maloo <url>`, `gc watch <file>`
- **Search:** `gc search <query>`, `gc s <query>` (`--all` follows the
  pages; `gc explain search` lists the operators that find related changes)
- **Manage:** `gc vote <url> <label> <score>`, `gc message <url> "msg"`,
  `gc set-topic <url> <topic>`, `gc hashtag <url> --add <tag>`,
  `gc rebase <url>`, `gc abandon <url>`, `gc restore <url>`,
  `gc checkout <url>`
- **Upload commits:** `gc --user <set> upload <change>` pushes HEAD as
  the change's next patchset, over HTTPS as that set's account. It
  refuses a HEAD whose Change-Id is not the change's, and rewrites a
  committer email the account has not registered. One commit by
  default; `--series` pushes every new commit up to HEAD, each to its
  own change. `--dry-run` checks everything and pushes nothing. (`gc
  push` is different: it posts staged comment replies.) See
  `gerrit_cli/README.md` ("Uploading a Patchset").
- **Reviewers:** `gc reviewers <url>`, `gc add-reviewer <url> <name>`,
  `gc remove-reviewer <url> <name>`, `gc find-user <name>`,
  `gc reviewer-candidates <url> --repo <lustre-release> --ref upstream/master`
  (ranks who reviewed, by Reviewed-by trailers, and wrote the change's
  files over the last 12 months of local git history; who MAINTAINERS
  names for the files is listed beside the ranking, not in it)

**Configuration:** environment variables, or a `.env` file.
Variables already set in the environment always win. Only one file
is read, the first that exists of:
1. `$GERRIT_CLI_ENV_FILE`, if set (an error if it doesn't exist)
2. `~/.config/gerrit-cli/.env`
3. `/etc/gerrit-cli/.env`
4. `/shared/support_files/.env`
5. `./.env`

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

Runs AI patch reviews — the
[review-prompts](https://github.com/verygreen/review-prompts/)
`review-core.md` deep-dive regression analysis, referenced directly
by prompt (no skill/slash-command install needed, just a clone) — on
a batch of Gerrit changes in parallel: one headless agent process per
change (Claude Code by default; codex/auggie/cursor/gemini/opencode
via `--agent` / `$LREVIEW_AGENT`, best-effort), each in its own git worktree, then
posts the collected `gerrit-review.json` results via gerrit-cli.

```bash
lreview setup                        # Guided first-time setup
lreview check                        # Verify agent/prompts/Gerrit
lreview run 64086 64087 --repo lustre-release   # Review (no post)
lreview post                         # Post collected findings
lreview run 64086 --post             # Review + post in one go
lreview run --mode light 64086 --repo lustre-release  # cheap single-pass
lreview chat 64086                   # interactive session over an existing review
lreview run --repo ~/tree --last 3   # Review the newest 3 local commits
```

`--mode light` runs the bundled single-pass light review instead of
the review-core deep dive (much cheaper; artifacts `-light`-suffixed
so the modes never overwrite each other).

`--last N` (`-n`) points lreview at a directory instead of Gerrit and
reviews the newest N commits of that repo, each in its own worktree
pinned to that commit. Local results are never posted; `--output`
(`-o`, default `<results-dir>/review-last<N>.txt` with `--last`)
writes the whole batch as one plain-text dump -- assessment and
findings per commit, including the clean and failed ones.


Defaults: sonnet model (`--model` / `$LREVIEW_MODEL` for opus/fable),
5 parallel reviews (`--jobs`), 2h per-review timeout, a review
silent for 10 minutes ended as `stalled` after one retry
(`--stall-timeout`), results in
`lreview-results/` in this checkout (per-change JSON + log +
summary.json; `--results-dir` / `$LREVIEW_RESULTS_DIR` override), a live
status line with token counter while reviews run, and posted messages
prefixed `[AI review - <model>]` on a bold own line (`--prefix` /
`$LREVIEW_PREFIX` override; a `<model>` placeholder in a custom prefix
is substituted too). Posting is pinned to the reviewed
patchset revision and guarded against double-posting. Prompts are
found via `--prompts-dir` / `$REVIEW_PROMPTS_DIR`, a legacy
kreview.md skill install, or `~/review-prompts`; if missing, the
tool offers to clone review-prompts. Claude reviews run `--lean` and
`--preload` by default (minimal tools, no MCP/skills/CLAUDE.md, a
5-minute prompt cache; the protocol in the system prompt and the commit
in the first message); every review writes a `.telemetry.json` beside
its log, and `lreview stats` shows where reviews spent their time and
money. `lreview bench` runs the standard benchmark set
(`lreview/lreview/benchmark/cases.json`: merged changes with bugs later
fixed) -- run it before and after changing lreview, its defaults, the
prompts or the model. See `lreview/README.md`.

### Other tools

Also installed from this repo:
- `maloo` — Lustre CI test results (testing.whamcloud.com);
  needs `MALOO_USER`/`MALOO_PASS`. See `maloo_tool/README.md`.
- `jenkins` — Jenkins build server (build.whamcloud.com). Reads need no
  credentials; `JENKINS_USER`/`JENKINS_TOKEN` are only for `abort` and
  `retrigger`. See `jenkins_tool/README.md`.
- `janitor` — Gerrit Janitor test results; no auth, optional
  `JANITOR_URL` env var. See the top-level `README.md`.
- `lustre-crash` — drgn-based crash-dump analysis; no auth.
  See `lustre_crash/README.md`.
- `pw-cli`: asks Patch Watcher for what tagging it on a change asks
  (levels, stop, halt, reviewers, lreview, request runs, feedback; not
  backlog), signed in with gerrit-cli's Gerrit login, which its
  `/tagger/v1/` routes check with Gerrit. Its roles decide what each
  person may do, as for tags. `pw-admin` is the Authelia-login client
  for everything else.
- `release-priority`: a release's priority changes (the Gerrit hashtag
  `218_review_priority` for 2.18) and blockers (`218_review_blocker`,
  shown first), each with the Gerrit Tracker's status and whose move it
  is; for 2.18 it reads the public page's `data.json`, otherwise Gerrit. `me` uses gerrit-cli's login to say what is the
  engineer's to do (their changes that need them, the reviews they owe)
  and which changes need review; `list`, `master-next` and `show` work
  without a login. Read only. The same changes are on the public page
  https://mulberrytree.us/release_priority/. See
  `release_priority/README.md`.

## Claude skills

`skills/` holds task-shaped skills for these tools --
`lustre-ci-triage`, `gerrit-patch-workflow`, `lreview-spin-cycle`,
`lustre-crash-triage`, `jira-bug-research`, `lustre-debug-logs`,
`pw-cli`, `release-priority`. `./install.sh` links them
into `~/.claude/skills`; `--skills` does only that, `--no-skills`
skips it. They are symlinks, so edits in this checkout take effect
immediately -- edit the skill here, not the copy in `~/.claude`.

Keep them task-shaped rather than tool-shaped: each CLI already
answers `--help` and `describe`, so a skill earns its place by
carrying the sequencing and judgment that those cannot.

## Update notices

The core tools (`gerrit`/`gc`, `jira`, `maloo`, `jenkins`, `janitor`,
`lreview`, `pw-cli`, `release-priority`) check GitHub daily and, when this checkout is behind, print
one line to stderr:

```
llm-tools: update available (3507cba -> 8c1f2e4).  Run: /path/to/install.sh --update
```

It is not an error, and the command's JSON on stdout is unaffected.
Tell the user and offer to run the update; do not run it mid-task on
your own initiative. It repeats at most once a day, so seeing it once
in a session is expected. Implementation:
`llm_tool_common/src/llm_tool_common/update_check.py`, called first in
each core tool's `main()`; `install.sh --update` does the pull.

## Timing log

`llm-tools-timing on` (or `LLM_TOOLS_TIMING=1|<file>` for one process)
makes every tool run append a JSON line to
`~/.local/state/llm-tools/timing.jsonl`: wall, wait (HTTP and child
processes) and self time, and each request with its latency and size.
Nothing goes to stdout/stderr. `llm-tools-timing summary|slowest|endpoints`
reads it. Use it to tell server latency from the tool's own cost before
changing a command. Implementation: `llm_tool_common/timing.py`.

## Versioning

Each tool's `pyproject.toml` carries its own version. The tracked
pre-commit hook bumps the patch of every tool with staged changes;
docs- and test-only commits move nothing, and a version edited by hand
in the same commit is left alone. Enable it per clone:

```bash
make hooks        # git config core.hooksPath .githooks
```

## Development

All tools share `llm-tool-common` for envelope formatting and
error handling. Each tool is an editable pip install from its
subdirectory.

**Install a tool for development:**
```bash
cd jira_tool && pip install -e .
cd gerrit_cli && pip install -e .
```

**Testing:** `make unit-test` runs every tool's offline tests, each in
its own directory; for one tool, `pytest -m 'not integration'` in its
directory. `-m integration` selects the live tests, which need network
and credentials and only read. No test carries the `unit` marker, so
`-m unit` selects nothing.

## Output format

All tools except `lreview` follow the same output convention:
- **Default:** JSON data payload only (no wrapper)
- **`--envelope`:** Full `{ok: bool, data: ..., meta: ...}` wrapper
- **`--debug`:** (jira) Debug output to stderr
- **Exit codes:** 0 = success, 1 = general error, 2 = auth error,
  3 = not found, 4 = invalid input, 5 = network error
