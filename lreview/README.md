# lreview

Run the **kreview** AI patch-review skill on a batch of Gerrit changes
in parallel, collect the resulting `gerrit-review.json` files, and post
them to Gerrit as inline review comments.

Each change is reviewed by a **headless AI agent** process (Claude Code
by default) running in its **own git worktree** pinned to the change's
current patchset. Because every review runs in its own worktree, the
`./gerrit-review.json` files produced by kreview never collide; the
runner collects them into the results directory as
`gerrit-review-<change>_ps<N>.json`.

## Prerequisites

1. **An agent CLI** on PATH — `claude` (Claude Code, the default),
   `codex`, `gemini`, or `opencode`.
2. **The kreview skill** from the
   [review-prompts](https://github.com/verygreen/review-prompts/) repo,
   installed for your agent:
   ```bash
   git clone https://github.com/verygreen/review-prompts/ ~/review-prompts
   cd ~/review-prompts && ./setup.sh claude kernel   # or codex/gemini/opencode
   ```
   `lreview check [--agent X]` verifies the whole chain; `lreview run`
   offers to perform this setup interactively when the skill is missing.
3. **Gerrit credentials** (`GERRIT_URL`, `GERRIT_USER`, `GERRIT_PASS`)
   in the environment — same as gerrit-cli, which this tool uses as a
   library.

## Agents

> **Disclaimer:** only the **claude** backend has been verified
> end-to-end. codex, gemini, and opencode are untested best-effort
> integrations — expect to tweak invocation flags (`--agent-arg`) and
> please report what works.

kreview itself is agent-agnostic — the prompt mandates the
`gerrit-review.json` / `review-metadata.json` output files, so
collection and posting work the same for every agent. Select with
`--agent {claude,codex,gemini,opencode}` or `LREVIEW_AGENT`:

- **claude** (default) — the verified backend. Invokes the installed
  `/kreview` slash command with stream-json output, which is what
  powers the live token counter, final token/cost figures, and model
  detection.
- **codex / gemini / opencode** — best-effort backends: lreview passes
  the installed kreview command file's content as the prompt text (no
  reliance on each CLI's slash-command support) and runs `codex exec`,
  `gemini --yolo -p`, or `opencode run` respectively. Reviews, logs,
  collection, and posting all work; live token/cost/model niceties are
  claude-only (the status line falls back to log size, and the posted
  prefix falls back to `--model` or the agent name). These invocations
  have not been battle-tested — use `--agent-arg` to adjust flags if
  your CLI version differs.

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
3. Up to `--jobs` (default 5) headless agent processes run the kreview
   prompt concurrently, one per worktree (worktree names carry the pid,
   so concurrent lreview invocations never collide). Full output of
   each run is logged to `kreview-<change>_ps<N>.log`; on timeout the
   whole agent process group is killed.
4. kreview writes `./gerrit-review.json` **only when it finds issues**,
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

## Results directory

```
lreview-results/
├── gerrit-review-64086_ps40.json     # the review (only when findings)
├── review-metadata-64086_ps40.json   # severity score + issue count
├── kreview-64086_ps40.log            # full claude event log (stream-json)
└── summary.json                      # per-change manifest (see above)
```

The logs are JSONL event streams; useful jq one-liners:

```bash
jq -r '.result // empty' kreview-64086_ps40.log      # final review text
jq -r 'select(.type=="assistant").message.content[]?
       | .text // .name' kreview-64086_ps40.log       # what claude did
```

## Commands

```
lreview check                    # verify claude + skill + prompts
lreview run <change|url>... [options]
lreview post [<change|url>...] [options]
```

Changes are Gerrit change numbers or URLs; flags may come before or
after them, but keep the change list contiguous (argparse cannot
rejoin a positional list split around a flag).

### run options

| Option | Default | Description |
|---|---|---|
| `--repo PATH` | `.` | Source git repository |
| `--jobs, -j N` | 5 | Parallel reviews |
| `--timeout SECS` | 7200 | Per-review timeout |
| `--results-dir DIR` | `./lreview-results` | Logs, JSONs, summary.json |
| `--worktrees-dir DIR` | auto | Where worktrees are created |
| `--keep-worktrees` | off | Keep worktrees after review |
| `--agent NAME` | `claude` (or `$LREVIEW_AGENT`) | Agent backend: claude, codex, gemini, opencode |
| `--model NAME` | `opus` for claude (or `$LREVIEW_MODEL`); other agents use their own default | Model for the review runs |
| `--agent-arg=ARG` | — | Extra agent-CLI arg (repeatable; `--claude-arg` is a legacy alias) |
| `--post` | off | Post findings when batch finishes |
| `--prefix TEXT` | `[AI review - <model>]` | Message prefix; `<model>` placeholder substituted (`$LREVIEW_PREFIX` overrides the default; `''` for none) |
| `--setup-dest DIR` | `~/review-prompts` | Where to clone review-prompts if the skill is missing (`$REVIEW_PROMPTS_DIR`) |

### post options

`post` with no change numbers posts every unposted review with
findings from the results dir; with numbers/URLs it posts just those.
`--results-dir`, `--prefix` as above; `--force` reposts an
already-posted review.

### Exit codes

`run`: 0 all reviews clean or with findings, 1 any failed/timed out
(or a post error with `--post`), 2 skill/setup not available.
`post`: 0 posted/skipped, 1 any error. `check`: 0 ready, 2 not ready.

## Environment variables

| Variable | Effect |
|---|---|
| `LREVIEW_AGENT` | Default for `--agent` (else `claude`) |
| `LREVIEW_MODEL` | Default for `--model` (else `opus` for claude) |
| `LREVIEW_PREFIX` | Default for `--prefix`; `<model>` substituted |
| `REVIEW_PROMPTS_DIR` | Default clone location for the skill repo |
| `NO_COLOR` | Disable colored output |
| `GERRIT_URL/USER/PASS` | Gerrit credentials (via gerrit-cli) |

## Notes

- The agent processes run in their CLI's unattended mode (claude:
  `--dangerously-skip-permissions`, codex:
  `--dangerously-bypass-approvals-and-sandbox`, gemini: `--yolo`) —
  they must read the tree, run git/grep, and write one JSON file; each
  runs confined to its own disposable worktree.
- Reviews are expensive (a deep kreview of a non-trivial patch can run
  30–90 minutes and significant tokens). Start with one change to
  calibrate before batching.
- This is an operator-facing tool and prints human-readable output,
  unlike the JSON-emitting agent tools in this repository.
