---
name: lreview-spin-cycle
description: This skill should be used to run AI patch reviews with lreview - "review these commits before I push", "run lreview on this change", "do a local review pass", "review the last two commits", "post the AI review to Gerrit", "review this GitHub PR", "what did the review find". Covers the pre-push spin cycle, triaging findings, when to stop spinning, and the comment-bloat trap that long review cycles create.
version: 0.1.0
---

# The lreview spin cycle

`lreview` runs the review-prompts deep-dive review on commits in parallel,
one headless agent per change, each in its own git worktree. It reviews
local commits (no Gerrit change and no push needed), Gerrit changes, or a
GitHub PR.

The point of a local pass is that human reviewers and bots do not spend a
round trip on findings a local run already catches.

## Running a local pass before pushing

```bash
lreview run --repo <tree> --last 2 -o /tmp/lreview1.txt
```

- `--last N` reviews the newest N commits of `--repo`, each in its own
  worktree pinned to that commit. The working tree is untouched.
- Local results are never posted. **Do not pass `--post` on a pre-push
  pass** -- that posts to Gerrit.
- Per-review timeout defaults to 7200s. Run it in the background with
  output redirected to a file; never wait on it in the foreground.
- To wait for that background run, wait on its **pid**. Never wait on a
  `pgrep -f` pattern naming the command: the waiting shell's own command
  line contains the pattern, so `pgrep` matches the waiter itself and the
  loop never ends -- reporting the run as still going long after it
  finished. The same trap makes `pkill -f <cmd>` kill the shell that runs
  it, before it kills anything else.

  ```bash
  lreview run --repo <tree> --last 6 -o /tmp/r1.txt > /tmp/r1.console 2>&1 &
  lrpid=$!
  while kill -0 $lrpid 2>/dev/null; do sleep 30; done
  ```
- One log per commit lands in the results directory as
  `kreview-<ref>-<timestamp>.log`; `-o` also writes one plain-text dump of
  the whole batch, including the clean commits.
- `--mode light` is one cheap focused pass instead of the deep dive.
  `--jobs N` controls parallelism (default 5).

## The cycle

1. Run `lreview run --repo <tree> --last <N> -o <out>`.
2. Triage **every** finding: fix it, or decide it is wrong or out of scope
   and be able to say why. Do not silently skip any.
3. Amend the commits. Do not stack fixup commits -- the point is that the
   pushed patch is already clean.
4. Re-run on the amended commits.
5. Repeat until two rounds in a row are clean.

A clean round is one that turns up nothing but wording or style items and
findings you consciously decline. A round that leads to a real fix resets
the count to zero.

## When to stop

Rounds converge on wording, not bugs. Stop after two clean rounds in a
row, not one: runs are not deterministic, and the next pass can find what
a clean one missed. Past that, further rounds cost roughly $10 and 25
minutes each and mostly churn prose.

Before spending another round on the same backend, get a second opinion:

```bash
lreview run --repo <tree> --last 2 --agent codex --model astra -o /tmp/lreview-codex.txt
```

On one series this found two real defects that ten opus rounds had missed,
at about four times fewer tokens and half the wall time. Different
backends fail differently; `lreview models` lists what each accepts
(claude: opus, sonnet, fable, haiku; codex: astra, sol, terra, luna,
spark, with an `--effort` level).

## The comment-bloat trap

The strong pull during a spin cycle is to answer each finding by adding an
explanatory comment. That is how a 5-line function grows a 16-line header,
and it is the default failure mode of a long cycle.

Fix the code, or reply on the patch and decline. A comment written to
preempt a reviewer is noise to the next human reader.

Before pushing, re-read every comment the patch adds and delete the ones
that exist only because a reviewer asked. Lustre code is sparsely
commented; match it. Never hedge in a comment -- one wrong "belt and
braces" line drew three separate rounds of reviewers proposing to delete
correct code.

## Gerrit and GitHub

```bash
lreview run 64086 64087 --repo lustre-release   # review, do not post
lreview post                                    # post what was collected
lreview run 64086 --post                        # review and post
lreview run --github <pr-url> --repo <path> --post
lreview chat 64086                              # ask about an existing review
lreview render                                  # re-render collected JSON to Markdown
```

Posting is pinned to the reviewed patchset revision and guarded against
double-posting. Posted messages carry a prefix (`--prefix`, default
`[AI review - <model>]`, with `<model>` substituted).

## Setup and health

```bash
lreview check     # agent CLI, review prompts, Gerrit credentials
lreview setup     # guided first-time setup; offers to clone review-prompts
```

`lreview` needs three things: an agent CLI on PATH (`claude` by default,
each with its own login), a clone of the review-prompts repo
(`--prompts-dir` / `$REVIEW_PROMPTS_DIR`, else `~/review-prompts`), and
Gerrit credentials for anything that touches Gerrit -- the same ones
`./install.sh --configure --only gerrit` writes.

Unlike the other tools in this repo, `lreview` is operator-facing: it
prints human-readable coloured output, not JSON, and has no `--envelope`.

## Reporting findings back

Say what was found and what was done with each item: fixed, or declined
and why. Say plainly whether the round was clean and how many clean rounds
in a row that makes -- the second one is the signal to stop.
