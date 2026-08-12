# lreview

Run AI patch reviews (the
[review-prompts](https://github.com/verygreen/review-prompts/)
`review-core.md` deep-dive regression analysis) on a batch of Gerrit
changes in parallel, collect the resulting `gerrit-review.json` files,
and post them to Gerrit as inline review comments.

Each change is reviewed by a **headless AI agent** process (Claude Code
by default) running in its **own git worktree** pinned to the change's
current patchset. Because every review runs in its own worktree, the
`./gerrit-review.json` files produced by the review never collide; the
runner collects them into the results directory as
`gerrit-review-<change>_ps<N>.json`.

## Getting started (from a fresh clone)

```bash
# 1. Install the tools (offers a venv where needed; 'source' ends
#    with it activated — plain ./install.sh works too)
git clone <this-repo> && cd llm_code_and_review_tools
source install.sh

# 2. Guided setup — checks each prerequisite, offers to clone the
#    review prompts, verifies your Gerrit credentials live
lreview setup                   # or: lreview setup --agent codex

# 3. First review
lreview run --repo /path/to/lustre-release 64086
lreview post
```

`lreview setup` walks through the three prerequisites and prints
exact instructions for anything missing:

1. **An agent CLI** on PATH — `claude` (Claude Code, the default;
   `npm install -g @anthropic-ai/claude-code`, then log in), or
   `codex` / `gemini` / `opencode` for other AIs (see Agents below).
2. **The review prompts** — bundled with this repository as the
   [review-prompts](https://github.com/verygreen/review-prompts/)
   submodule, which `./install.sh` initializes automatically (or
   `git submodule update --init review-prompts`). Reviews reference
   `review-core.md` directly (the repo's own quick-start and
   automation form), so no `setup.sh` skill installation is needed.
   Resolution order: `--prompts-dir` / `$REVIEW_PROMPTS_DIR` →
   bundled submodule → a legacy `~/.claude/commands/kreview.md`
   skill install → `~/review-prompts`; `lreview setup` offers to
   initialize/clone when missing.
3. **Gerrit credentials** (`GERRIT_URL`, `GERRIT_USER`, `GERRIT_PASS`
   in the environment or `~/.config/gerrit-cli/.env`; HTTP password
   from Gerrit → Settings → HTTP Credentials). Setup verifies them
   with a real read-only API call.

`lreview check` runs the same checks non-interactively (for scripts
and CI).

## Agents

> **Disclaimer:** only the **claude** backend has been verified
> end-to-end. codex, gemini, and opencode are untested best-effort
> integrations — expect to tweak invocation flags (`--agent-arg`) and
> please report what works.

The review prompt is agent-agnostic — it mandates the
`gerrit-review.json` / `review-metadata.json` output files, so
collection and posting work the same for every agent. Every backend
receives the same instruction:

```
Using the prompt <prompts>/review-core.md run a deep dive regression
analysis of the top commit
```

("deep dive regression analysis" is deliberate — per the
review-prompts README it gets better prompt compliance than calling
it a review.) Select the backend with
`--agent {claude,codex,gemini,opencode}` or `LREVIEW_AGENT`:

- **claude** (default) — the verified backend, run with stream-json
  output, which is what powers the live token counter, final
  token/cost figures, and model detection.
- **codex / gemini / opencode** — best-effort backends via
  `codex exec`, `gemini --yolo -p`, or `opencode run`. Reviews, logs,
  collection, and posting all work; live token/cost/model niceties
  are claude-only (the status line falls back to log size, and the
  posted prefix falls back to `--model` or the agent name). These
  invocations have not been battle-tested — use `--agent-arg` to
  adjust flags if your CLI version differs.

## Quick start

```bash
cd /path/to/lustre-release          # or pass --repo

# Review three changes, 5 in parallel (default), don't post yet
lreview run 64086 64087 64090

# Inspect lreview-results/gerrit-review-*.json, then post
lreview post

# Or review and post in one go
lreview run 64086 64087 --post
```

While reviews run, a colored status line at the bottom of the terminal
is redrawn in place every few seconds — elapsed time and a **live token
counter** per running review (from the stream-json `estimated_tokens`
events claude emits; also your liveness signal — a frozen counter means
a stuck review). Event lines scroll above it; completions show final
tokens and cost from the result event, and the batch summary prints
per-change and total tokens/cost:

```
[64086_ps40] review started: LU-12668 lov: handle ESHUTDOWN for LSEEK
[64086_ps40] 3 finding(s), severity medium (30m34s, 2.1M tok, $4.18) -> gerrit-review-64086_ps40.json
running: 64087_ps12 31m08s 1.8M tok, 64090_ps3 12m40s 731k tok | done 1/3
```

When stdout is not a terminal (piped, logged), output degrades to plain
appended lines (status once a minute) with no escape codes; `NO_COLOR`
disables colors. Logs are stream-json event files — pipe through `jq`
to read, e.g. `jq -r '.result // empty' kreview-*.log` for the final
review text.

Reviews run on **opus** by default (`--model sonnet` / `--model fable`
or `LREVIEW_MODEL` to change). Posted messages are prefixed
`[AI review - <model>]`, stamped with the model that actually ran the
review and rendered as a bold standalone first line with a blank line
before the message body:

> **[AI review - opus]**
>
> (suggestion) This fires on the first selected mirror, ...

Override the prefix with `--prefix` or `LREVIEW_PREFIX` in the
environment; pass `--prefix ''` for none. A custom prefix may itself
contain the `<model>` placeholder, e.g.
`--prefix '[Marc Bot - AI review - <model>]'` posts as
`[Marc Bot - AI review - opus]`. The model is taken from `--model`,
falling back to the model claude reports in the review log.

## How it works

1. Every change (number or URL) is resolved to its **current patchset**
   revision SHA up front — a typo fails fast before any review starts.
2. The change ref is fetched into the source repo and a detached
   worktree is created per change (a lustre-release checkout is ~75 MB;
   worktrees share the object store). Worktrees go to
   `<repo>/../ai_worktrees/lreview/` when an `ai_worktrees`
   directory exists next to the repo, else under the results dir.
3. Up to `--jobs` (default 5) headless agent processes run the review
   prompt concurrently, one per worktree (worktree names carry the pid,
   so concurrent lreview invocations never collide). Full output of
   each run is logged to `kreview-<change>_ps<N>.log`; on timeout the
   whole agent process group is killed.
4. The prompt writes `./gerrit-review.json` **only when it finds issues**,
   and `./review-metadata.json` (severity score) for **every completed
   analysis** — the metadata file is the completion marker, so a run
   that produced neither is recorded as `failed`, not silently clean.
   Both artifacts are collected; the worktree is then removed
   (`--keep-worktrees` to keep them). One crashed or malformed review
   never aborts the rest of the batch.
5. `summary.json` in the results dir records per change: patchset, SHA,
   status (`findings` / `clean` / `failed` / `timeout` /
   `invalid-json`), finding count, severity, model, tokens, cost, and
   whether it was posted.
   All manifest access is flock-protected and written atomically, so a
   `post` of earlier results can safely overlap a running batch.
6. Posting (`--post` or the `post` subcommand) goes through gerrit-cli's
   `post_review`, targets the Gerrit host recorded at review time, and
   is **pinned to the reviewed revision SHA** — if a new patchset lands
   while the review runs, comments still attach to the patchset that
   was actually reviewed. The posted flag survives re-reviews of the
   same patchset (guarding against double-posting; `--force` to
   repost), and reviewing a newer patchset resets it while keeping a
   `last_posted` record.

A URL that pins an explicit patchset (`.../+/64086/38`) reviews that
patchset instead of the current one.

## Local reviews (no Gerrit)

```bash
lreview run --repo ~/git/lustre-release                    # the checked-out
                                                           # HEAD, in place
lreview run --repo ~/git/lustre-release --local br1 br2    # branch tips, each
                                                           # in its own worktree
```

With no changes at all, the review runs directly in the repo against
whatever is checked out — no worktree is created, and any stale
`gerrit-review.json` lying in the repo from a manual run is moved
aside first so it can't be mistaken for this run's result. `--local`
makes the change arguments local refs (branches, SHAs) instead of
Gerrit changes; each gets its own worktree exactly like Gerrit
changes do, so they run in parallel.

Local results get the same collection, `summary.json` entries (keyed
`<ref>_<sha7>`, marked `local`), and Markdown reports — but they are
never posted; `post` skips them.

## Results directory

```
lreview-results/
├── gerrit-review-64086_ps40.json     # the review (only when findings)
├── review-metadata-64086_ps40.json   # severity score + issue count
├── kreview-64086_ps40.log            # full claude event log (stream-json)
├── markdown/
│   └── 64086_LU-..._ps40.md          # human-readable report (see below)
└── summary.json                      # per-change manifest (see above)
```

Every review with findings also gets a **human-readable Markdown
report** under `markdown/<change>_<subject>_ps<N>.md` — change link,
severity, model/tokens/cost, the overall assessment, and each finding
anchored to its file and line. The paths are listed at the end of the
`run` output, so results can be read comfortably without posting
anything to Gerrit. `lreview render` (re)generates the reports for
review JSONs that already exist — from older runs predating this
feature, or after hand-editing — pulling stats from `summary.json`
when it matches and the `review-metadata-*.json` sidecar otherwise.

The logs are JSONL event streams; useful jq one-liners:

```bash
jq -r '.result // empty' kreview-64086_ps40.log      # final review text
jq -r 'select(.type=="assistant").message.content[]?
       | .text // .name' kreview-64086_ps40.log       # what claude did
```

## Commands

```
lreview setup                    # guided first-time setup
lreview check                    # verify agent CLI + prompts + Gerrit
lreview run <change|url>... [options]
lreview render [file.json...]    # (re)generate Markdown reports from
                                 # existing review JSONs (--results-dir)
lreview post [<change|url>...] [options]
```

Changes are Gerrit change numbers or URLs; flags may come before or
after them, but keep the change list contiguous (argparse cannot
rejoin a positional list split around a flag).

### run options

| Option | Default | Description |
|---|---|---|
| `--repo PATH` | `.` | Source git repository |
| `--local` | off | Changes are git refs of `--repo`; no changes at all = checked-out HEAD, in place (no flag needed); not postable |
| `--jobs, -j N` | 5 | Parallel reviews |
| `--timeout SECS` | 7200 | Per-review timeout |
| `--results-dir DIR` | `./lreview-results` | Logs, JSONs, summary.json |
| `--worktrees-dir DIR` | auto | Where worktrees are created |
| `--keep-worktrees` | off | Keep worktrees after review |
| `--agent NAME` | `claude` (or `$LREVIEW_AGENT`) | Agent backend: claude, codex, gemini, opencode |
| `--model NAME` | `opus` for claude (or `$LREVIEW_MODEL`); other agents use their own default | Model for the review runs |
| `--effort LEVEL` | claude's default (or `$LREVIEW_EFFORT`) | Reasoning effort: low/medium/high/xhigh/max (claude-only) |
| `--agent-arg=ARG` | — | Extra agent-CLI arg (repeatable; `--claude-arg` is a legacy alias) |
| `--post` | off | Post findings when batch finishes |
| `--prefix TEXT` | `[AI review - <model>]` | Message prefix; `<model>` placeholder substituted (`$LREVIEW_PREFIX` overrides the default; `''` for none) |
| `--prompts-dir DIR` | auto | review-prompts location (repo root or its `kernel/`); default: `$REVIEW_PROMPTS_DIR`, the bundled submodule, a legacy kreview.md install, or `~/review-prompts` |

### post options

`post` with no change numbers posts every unposted review with
findings from the results dir; with numbers/URLs it posts just those.
`--results-dir`, `--prefix` as above; `--force` reposts an
already-posted review.

### Exit codes

`run`: 0 all reviews clean or with findings, 1 any failed/timed out
(or a post error with `--post`), 2 prompts/agent CLI not available.
`post`: 0 posted/skipped, 1 any error. `check` / `setup`: 0 ready,
2 not ready.

## Environment variables

| Variable | Effect |
|---|---|
| `LREVIEW_AGENT` | Default for `--agent` (else `claude`) |
| `LREVIEW_MODEL` | Default for `--model` (else `opus` for claude) |
| `LREVIEW_PREFIX` | Default for `--prefix`; `<model>` substituted |
| `REVIEW_PROMPTS_DIR` | Path to the review-prompts clone |
| `NO_COLOR` | Disable colored output |
| `GERRIT_URL/USER/PASS` | Gerrit credentials (via gerrit-cli) |

## Notes

- The agent processes run in their CLI's unattended mode (claude:
  `--dangerously-skip-permissions`, codex:
  `--dangerously-bypass-approvals-and-sandbox`, gemini: `--yolo`) —
  they must read the tree, run git/grep, and write one JSON file; each
  runs confined to its own disposable worktree.
- Reviews are expensive (a deep analysis of a non-trivial patch can run
  30–90 minutes and significant tokens). Start with one change to
  calibrate before batching.
- This is an operator-facing tool and prints human-readable output,
  unlike the JSON-emitting agent tools in this repository.
