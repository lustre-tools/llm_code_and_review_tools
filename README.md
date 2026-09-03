# LLM Code and Review Tools

CLI tools designed for LLM agents to interact with code review,
CI, issue tracking, and crash analysis systems.

## Tools

| Tool | Command | Purpose |
|------|---------|---------|
| **Gerrit CLI** | `gerrit` / `gc` | Gerrit code review -- comments, replies, reviewer management, patch series, Maloo triage |
| **lreview** | `lreview` | Parallel AI patch reviews -- runs the review-prompts analysis headless on N Gerrit changes, posts results |
| **JIRA** | `jira` | JIRA issue tracking -- get, search, comment, create, transition |
| **Maloo** | `maloo` | Lustre CI test results -- failures, retests, bug linking |
| **Jenkins** | `jenkins` | Jenkins build server -- build status, console logs, retriggers |
| **Janitor** | `janitor` | Gerrit Janitor test results (separate from Maloo/enforced CI) |
| **Lustre Crash** | `lustre-crash` | Crash dump analysis using drgn, with structured JSON output |
| **Gerrit Dashboard** | `gerrit-dashboard` | Per-user "what needs my attention?" web dashboard -- ranked triage, @-mention tracking, watchlist with CI state ([details](gerrit_dashboard/README.md)) |

Shared utilities live in `llm_tool_common/`.

### lreview in one minute

`lreview` reviews Gerrit changes with AI and posts the findings as
inline review comments. Each change is reviewed by a headless agent
process — Claude Code by default; codex, gemini, and opencode are also
supported (`--agent`; currently only claude is verified as working,
the others are best-effort) — running the
[review-prompts](https://github.com/verygreen/review-prompts/)
`review-core.md` deep-dive regression analysis in its own git
worktree, pinned to the change's current patchset; results (`gerrit-review-*.json`, logs, `summary.json`) land
in `lreview-results/` inside this checkout (`--results-dir` /
`$LREVIEW_RESULTS_DIR` to change), and posting goes through gerrit-cli with an
`[AI review - <model>]` prefix, guarded against double-posting.

```bash
lreview setup                                    # guided first-time setup
lreview run --repo lustre-release 64086 64087    # review (5 parallel, opus)
lreview post                                     # post findings after inspection
lreview run --repo lustre-release --post 64086   # or review + post in one go
lreview run --mode light --repo lustre-release 64086  # cheap single-pass review
lreview run --repo lustre-release --last 3      # or review the newest 3
                                                # local commits, no Gerrit
```

Live colored status with per-review token counter while running;
`-j N` for concurrency, `--model sonnet|fable` to switch models,
`--mode light` for a cheap single-pass review (light artifacts are
`-light`-suffixed and never overwrite full-review results).
Details: [lreview/README.md](lreview/README.md).

### Bundled submodules

- **review-prompts** -- the AI review prompts lreview runs
  (verygreen/review-prompts, `lustre-dev` branch). Initialized by
  `./install.sh`; lreview finds it automatically.
- **lustre-drgn-tools** -- drgn-based Lustre vmcore analysis
  (`lustre_triage.py`, `obd_devs.py`, `ldlm_dumplocks.py`, etc.).
  Separate repo bundled as a git submodule. Requires drgn;
  `./install.sh` sets it up automatically, or run
  `lustre-drgn-tools/install-drgn.sh` manually.

### Beta / experimental

- **patch_shepherd** -- automated patch series monitoring
  (`gerrit watch`). Not yet suitable for general use.

## Install

```bash
./install.sh            # install all tools
./install.sh --uninstall
```

Besides the Python tools, `./install.sh` also initializes git
submodules and installs drgn if missing (via
`lustre-drgn-tools/install-drgn.sh`, falling back to
`pip install drgn`).

On macOS the drgn/lustre-drgn-tools step is skipped: drgn ships no
macOS wheels, its source build uses Linux-only APIs, and the
required elfutils has no Homebrew bottle. vmcore analysis needs a
Linux host; `LLM_TOOLS_TRY_DRGN=1` forces the attempt anyway.

Per-tool: `cd <tool_dir> && pip install -e .`

Requires Python 3.11+.

**Externally managed Python (macOS Homebrew, newer distros):** when
the system Python refuses pip installs (PEP 668's
`externally-managed-environment` error), `./install.sh` offers to
create a virtual environment (default `<repo>/.venv`, path editable
at the prompt) and installs the tools into it. `./install.sh --venv
[PATH]` selects that non-interactively, and an existing `.venv` in
the repo is reused automatically on later runs (including
`--uninstall`). The tools' executables are then symlinked into
`~/.local/bin` (pipx-style), so they work from any shell **without
activating the venv** — activation is only needed for python-level
use. `--uninstall` removes those symlinks again.

To end up with the venv activated in your current shell, run the
installer sourced (works in zsh and bash):

```bash
source install.sh            # install + activate in this shell
```

(A normally-executed `./install.sh` is a child process and cannot
change your shell's environment; sourcing is what makes activation
possible.)

## Configuration

### Gerrit

Set environment variables directly or in a `.env` file. All
existing files from these locations are loaded, highest priority
first (earlier-listed files override later ones): `./.env`,
`~/.config/gerrit-cli/.env`, `/etc/gerrit-cli/.env`,
`/shared/support_files/.env`:

```bash
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=your-username
GERRIT_PASS=your-http-password
```

To get your HTTP password: log into Gerrit, go to
Settings > HTTP Credentials > Generate Password.

Optional: `GERRIT_SSH_USER` for SSH operations. If unset, the SSH
user is auto-discovered from `ssh://user@<gerrit-host>` URLs in
`git remote -v`, then from shell alias definitions, then from the
`GERRIT_USER=` entry in `~/.config/gerrit-cli/.env`; the
`GERRIT_USER` environment variable itself is not used.

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
automatically routed to a Cloud client built from the
`JIRA_CLOUD_SERVER`, `JIRA_CLOUD_EMAIL`, and `JIRA_CLOUD_TOKEN`
environment variables — not from a config-file instance.
`JIRA_CLOUD_SERVER` and `JIRA_CLOUD_TOKEN` must be set or routed
commands fail with a config error (`JIRA_CLOUD_EMAIL` is needed
for Atlassian Cloud basic auth). An explicit `-I` always
overrides auto-routing.

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

Verify: `jenkins jobs`

### lreview

Run `lreview setup` — it walks through the three prerequisites
(agent CLI on PATH, a clone of the review-prompts repo, the Gerrit
credentials above), offers to clone the prompts, and verifies the
credentials with a live read-only call. `lreview check` is the
non-interactive equivalent. Optional environment variables:

```bash
LREVIEW_MODEL='sonnet'                            # default model (default: opus)
LREVIEW_AGENT='claude'                            # agent backend: claude/codex/
                                                  # gemini/opencode (only claude
                                                  # verified)
LREVIEW_PREFIX='[Marc Bot - AI review - <model>]' # posted-message prefix;
                                                  # <model> is substituted
REVIEW_PROMPTS_DIR=~/review-prompts               # path to review-prompts clone
NO_COLOR=1                                        # disable colored output
```

### Other Tools

| Tool | Notes |
|------|-------|
| Janitor | No auth required; optional `JANITOR_URL` (default `https://testing.whamcloud.com/gerrit-janitor`) |
| Lustre Crash | No auth required |

## Output Format

All tools output raw JSON by default (no envelope). Use `--envelope`
for the full `{ok, data, meta}` wrapper. Use `--pretty` for
human-readable formatted output. Exception: `lreview` is an
operator-facing orchestrator and prints human-readable colored output.

```json
{"ok": true, "data": {...}, "meta": {"tool": "jira", "command": "issue.get"}}
```

Exit codes: 0=success, 1=general error, 2=auth, 3=not found,
4=invalid input, 5=network.

## Project Structure

```
llm_code_and_review_tools/
├── gerrit_cli/          # Gerrit code review CLI
├── lreview/             # Parallel AI patch reviews (review-prompts)
├── review-prompts/      # AI review prompts (submodule)
├── jira_tool/           # JIRA issue tracking CLI
├── maloo_tool/          # Maloo CI results CLI
├── jenkins_tool/        # Jenkins build server CLI
├── janitor_tool/        # Gerrit Janitor results CLI
├── lustre_crash/        # Crash dump analysis CLI (lustre-crash)
├── patch_shepherd/      # Patch series monitoring (beta)
├── lustre-drgn-tools/   # drgn vmcore analysis (submodule)
├── llm_tool_common/     # Shared utilities
├── install.sh           # Unified installer
└── pyproject.toml       # Test configuration
```

## Development

```bash
./install.sh                       # Install all tools (editable/dev mode)
cd <tool_dir> && pip install -e .  # Or install a single tool in dev mode
pytest                             # Run all tests (from repo root)
```

Code style: dataclasses, type hints, functions under ~60 lines,
tests for new functionality. See CLAUDE.md for agent instructions.

## License

BSD 2-Clause. See [LICENSE](LICENSE).
