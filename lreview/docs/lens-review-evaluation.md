# Lens reviews: evaluation

How `--lenses` was chosen, and what it buys. Measured 2026-11-13 .. 16 on
`lreview bench` (`lreview/benchmark/cases.json`: 43 changes merged into
Lustre master, 51 known bugs, 47 of them fixed by a later commit). Claude
Code 2.1.306 (2.1.325 for the last control runs and the check of the
merged option), Sonnet 5.5 reviewer at `--effort high`, `--lean`,
`--preload`, offline, scored with `lreview bench report --judge`. About
$540 of reviews at list price. Updated 2026-11-17 with how the reviewer
settles lens candidates ("Lens finds that do not reach the review") and
what a lens review costs ("Where the cost goes"), about $90 more.

Which bug each variant found, run by run, is in
`lens-review-benchmark-results.md`.

## Summary

- **One run can find substantially more.** In a lens review, the reviewer
  starts five parallel subagents after categorising the change, each
  digging into one class of bug, and then verifies their candidates
  itself. Known-bug recall went from 59% to 70% on one case set and from
  83% to 92.5% on another, real defects per review rose by roughly
  35-50%, and precision did not drop (99 of 100 sampled defect findings
  confirmed real).
- **Running an ordinary review several times does not get there.** Two
  or three runs add 1-3 points each; one lens review is above what three
  ordinary runs reach together.
- **Cost:** about 5x an ordinary review with the lenses at high effort,
  about 2.5x at medium (the `--lenses` default), which keeps roughly 70%
  of the gain.
- **Not worth doing:** adding the TodoWrite tool, a "recall first" line in
  the protocol, more effort, more repeats, or carrying notes between runs
  (`--memory`).
- **The reviewer used to drop much of what the lenses found:** of the known
  bugs a lens review missed, a lens had written 43% down. Making the
  reviewer settle every candidate with evidence raised known bugs found
  from 8/22 to 13/22 on the cases where this happened, at the same cost
  ($1.64 vs $1.67 a review) and a minute faster; defect precision held,
  `(minor)` precision fell (11/12 to 8/12 in a blind check).
- **Where a lens review's cost goes:** the reviewer's work after the lenses
  return (settling candidates on a ~100K context) is the largest part;
  each lens costs about 9% of a review, needed or not. The reviewer at
  `--effort medium` cut the cost by 22% and the time by 30% at the same
  recall, once it is told to run the lenses on every change.

## Why one run misses bugs

- Repeating identical runs mostly re-finds the same bugs. On the
  benchmark's calibration (Opus 5.5 medium, two runs), a bug was either
  found in both runs (64%) or in neither; only four were found in exactly
  one.
- `review-core.md` and `callstack.md` already require the checks that
  catch the missed bugs: lock requirements, locking on error paths,
  resource propagation, initialisation. One agent carrying every check
  through every change does not complete them all.
- The missed bugs cluster: lock coverage, teardown order, uninitialised
  fields, where a configure guard is defined, cross-feature semantics,
  test scripts.
- Published work points the same way. CRBugBench (arXiv 2610.13668): the
  same model run five times gained 4.7 points for 5.4x the cost;
  subagents choosing their own directions *lost* 3.7 points; a
  "comprehensive" single-agent prompt gained 5.6. Sashiko (Roman Gushchin,
  LKML 2026-03-19): a multi-stage per-concern protocol "improved the
  discovery rate by almost 10%" over Mason's prompts. Cursor's Bugbot: the
  largest gain came from an agentic design with "aggressive prompts".
  Lens reviews differ from the subagent setup that lost in CRBugBench in that
  each lens has a fixed brief.

## What a lens review does

After TASK 1C, lreview's TASK 1D (`lreview/lenses.py`) has the reviewer
start five subagents in one message, one per lens (`lreview/lens.md`):

- **concurrency**: every shared object touched, how existing code protects
  it, every new access holding the same protection; atomic context, lock
  ordering, RCU, races with teardown
- **lifecycle**: initialised before the first user, released once on
  every path and after the last user, teardown order against asynchronous
  work, error paths over half-built objects
- **data**: field initialisation, integer types and overflow, input
  validation, lengths and bounds, wire and disk formats, units
- **semantics**: every caller and every interacting flag, mode or interop
  case; a new check on every entry point; comments and commit message
  against the code
- **build**: where each `HAVE_*`/`CONFIG_*` is defined and under which
  configure options, header guards against definitions, kernel
  compatibility, tests

Each lens writes every candidate it cannot rule out to
`review-context/LENS-<lens>.json` and does not verify; the reviewer
verifies all of them in the protocol's own Task 2/3 and reports as usual.
The lens text is generic: nothing in it names a benchmark bug.

## Case sets

Each round re-measured the ordinary review on its own cases, so every
variant is compared with an ordinary review of the same code.

- **Pilot**: 19 cases (the benchmark's 11 `hard` + 8 `quick`), 21 known
  bugs, three runs each; four ideas side by side. The lenses were written
  after looking at the hard bugs, so a win here alone could be tuning.
- **Eval**: the 32 `eval` cases, 40 bugs, two runs each; only ordinary vs
  lens review. 24 of these cases were not in the pilot and their bugs were not
  looked at.
- **Cost round**: 7 cases where the lens review had beaten the ordinary review
  (9 bugs), two runs each: cheaper lens variants against the full lens review
  run again on the same cases.

On 2026-11-17 `test238-setup-rc-unchecked` (eval set, lnet-mr-forwarding)
was dropped from the benchmark: it is not a bug (`setup_router_test` can
only fail through calls that end in `error()` or `skip()`). The tables
below were measured with it. Without it, eval is 64/76 (84%) ordinary
against 72/78 (92%) for the lens review, and the cost round is 5/21
ordinary, 13/16 lenses at high, 10/16 at medium, 10/16 with three lenses,
9/16 with Haiku lenses. `lens-review-benchmark-results.md` has the
corrected per-bug tables.

## Results

### One run

| set | variant | $/review | min | findings | known bugs per run |
|---|---|---|---|---|---|
| pilot | ordinary review | 0.45 | 2.1 | 6.25 | 37/63 (59%) |
| pilot | + TodoWrite | 0.44 | 2.1 | 6.28 | 39/63 (62%) |
| pilot | + "recall matters more than brevity" line | 0.48 | 2.4 | 6.96 | 39/63 (62%) |
| pilot | **lens review, lenses at high** | 2.37 | 4.9 | 6.84 | **44/63 (70%)** |
| eval | ordinary review | 0.50 | 2.5 | 7.38 | 65/78 (83%) |
| eval | **lens review, lenses at high** | 2.46 | 5.2 | 8.14 | **74/80 (92.5%)** |

63 = 21 bugs x 3 runs. Eval 78: one ordinary review read a later fix and
is left out. Per bug, the lens review was better on 4 and worse on none (pilot),
better on 7 and worse on 1 (eval). A sign test per set gives p ~ 0.06-0.07;
the direction agrees on both sets and on the cases unseen while designing
the lenses.

### Several runs against one lens review

The `lreview-spin-cycle` skill reviews a change round after round. On the
benchmark the code does not change between runs, as in the rounds after
the author has nothing left to fix. Union over N runs, averaged over every
choice of N of the runs available:

| set | what is run | known bugs found | cost per change |
|---|---|---|---|
| pilot | ordinary x1 | 58.7% | $0.45 |
| pilot | ordinary x2 | 60.3% | $0.90 |
| pilot | ordinary x3 | 61.9% | $1.35 |
| pilot | **lens review x1** | **69.8%** | $2.37 |
| pilot | lens review x2 | 73.0% | $4.74 |
| pilot | lens review x3 | 76.2% | $7.11 |
| eval | ordinary x1 | 83.4% | $0.50 |
| eval | ordinary x2 | 86.8% | $0.99 |
| eval | **lens review x1** | **92.5%** | $2.46 |
| eval | lens review x2 | 95.0% | $4.92 |
| cost round | ordinary, all 2-3 runs together | 56% | ~$1.2-1.8 |
| cost round | lens review, lenses at medium, x1 | 67% | $1.42 |
| cost round | lens review, lenses at medium, x2 | 78% | $2.84 |

Earlier, on the same benchmark, rounds that read the previous round's
notes (`--memory`) found 8 of 20 known bugs against 11 for three
independent rounds: repeated rounds must stay independent.

### Precision

A blind check: 25 random `(defect)` findings per variant per set, judged
by Opus 5.5 at high effort against the code at the case's commit, without
being told which variant wrote them, as REAL, FALSE or UNSURE.

| set | ordinary review | lens review |
|---|---|---|
| pilot | 24 REAL, 1 FALSE | 25 REAL |
| eval | 25 REAL | 25 REAL |

The checker's reasons cite code lines and its one FALSE was an exact
refutation, so it was not agreeing by default. The lens review filed 113 against
79 `(defect)` comments (pilot) and 172 against 129 (eval): roughly 35-50%
more real defects per review. It also found real bugs outside the answer
key, for example `mdt_swap_layouts()` reading `msl_dv1`/`dv2` past the
8-byte minimum buffer (change 64757).

### Cheaper lens reviews

Cost round: 7 cases, 9 bugs, two runs each.

| variant | $/review | min | Mtok | known bugs per run | share of the lens review's gain |
|---|---|---|---|---|---|
| ordinary review | ~0.59 | ~2.5 | ~1.2 | 6/23 (26%) | 0% |
| **full lens review, lenses Sonnet high** | 3.01 | 6.4 | 6.9 | **15/18 (83%)** | 100% |
| **lenses Sonnet medium** (the default) | **1.42** | 3.9 | 2.8 | 12/18 (67%) | ~70% |
| three lenses (concurrency, lifecycle, data), Sonnet high | 1.60 | 4.7 | 3.3 | 12/18 (67%) | ~70% |
| lenses Haiku high | 1.91 | 9.8 | 16.5 | 11/18 (61%) | ~60% |

Medium-effort lenses lose an initialisation bug (`skip-ipv6-uninit`, 2/2 to
0/2) and half of `legacy-nid-oob`; three lenses lose the test-script
version gate (no build lens). Haiku lenses lose pieces everywhere and use
six times the tokens of Sonnet lenses, so they are dearer as well as
worse. With 18 runs per variant, 15 against 12 is not statistically
separable: read the cheap variants as about 70% of the gain at half the
price. `--lenses` defaults to Sonnet-medium lenses (five lenses keep the
build lens); `--lens-effort high` is the full variant.

### Never found

`lnetctl-import` fgets overflow (see below), lfs mirror nosync+stale
verify, root project-quota force flag, nodemap fileset parent check;
nodemap ban search without the lock in 1 of 6 lens reviews. The qmt
workqueue teardown order was first reported with strict verification
(below).

## Lens finds that do not reach the review

For every known bug a lens review missed, an LLM judge read the candidates
the lenses had written. Across all lens reviews the final review missed 47
(review, bug) pairs, and a lens had written the bug down in **20 of them
(43%)**. The qmt workqueue teardown order, which no review ever reported,
was in the lifecycle lens's candidates in all three pilot runs. In the
other direction, of 179 known bugs that lens reviews did report, only 3
(under 2%) came from the reviewer's own analysis rather than a lens.

The reviewer's own words show why it dropped them:

| dropped bug | the reviewer's reason | |
|---|---|---|
| qmt workqueue order | "the old global was set to NULL at the same point, so this behaviour is unchanged" | the patch moved it; never checked against the parent |
| unlocked tree dump | "debug-only, and the unlocked dump is an existing pattern" | a new call on a new path |
| svc stats read | "mirrors the existing `lprocfs_counter_add()`" | not checked |
| fgets overflow, dma map errors | "pre-existing", "predate the patch" | the patch introduced both |
| interop version gate | "test-only" (review-core.md's exclusion) | a real interop failure |
| legacy NID over-read | "real MGS versions grant both flags together" | an assumption the code does not enforce |
| (several) | "the eliminated ones were mostly low-confidence" | dropped in bulk, unexamined |

**The fix (strict verification, TASK 1D in `lreview/lenses.py`).** The
reviewer settles every candidate on its own: it eliminates one only by
pointing at what prevents it (a lock, a check, an ordering, or code that
shows the claim wrong); "pre-existing" needs `git show HEAD^:<file>`, and
code the patch adds, moves, reorders or newly calls is the patch's; an
assumption about usage counts only when the code enforces it; a candidate
it can neither confirm nor eliminate is reported as `(minor)`, saying what
could not be settled; a test that can give a wrong result (passes without
exercising the change, fails in a configuration the suite runs, carries on
after its setup failed) is reportable in a lens review despite
review-core.md's test-script exclusion; and it lists every candidate's
disposition. The lenses read the parent before calling code pre-existing.

On the 8 cases where drops had happened, two runs each, lenses at medium:

| | ordinary review | lenses, settled as before | strict verification |
|---|---|---|---|
| $/review | 0.52 | 1.64 | 1.67 |
| minutes | 2.6 | 5.1 | 4.1 |
| known bugs per run | 7/26 (27%) | 8/22 (36%) | **13/22 (59%)** |
| found in at least one run | 4/11 | 4/11 | **7/11** |
| lens finds dropped (of the misses) | -- | 12 of 15 | 3 of 9 |
| `(defect)` precision, blind check | -- | 10/12 | 11/12 |
| `(minor)` precision, blind check | -- | 11/12 | 8/12 |

The gains are `legacy-nid-oob` (0/2 to 2/2, the usage-assumption rule),
the interop version gate (0/2 to 2/2, the test rule) and the qmt
workqueue order (0/2 to 1/2, the first review ever to report it). All
three arrived as `(minor)` questions, and three of the four `(minor)`
findings the blind check called wrong are such questions -- one of them
the qmt comment itself, a bug a later commit on master fixed, so the
checker is fallible on exactly the cases nobody can settle quickly.
Still dropped: the svc stats read. Not written by any lens in these runs:
the fgets overflow, the unlocked tree dump, the unlocked ban search.

## Where the cost goes

Measured from lreview's telemetry (per-call cost, the fan-out point taken
at the reviewer call that starts the lenses), 46 lens reviews at medium
against 30 ordinary reviews of the same changes:

| | $/review | calls |
|---|---|---|
| ordinary review | 0.56 | 16, peak context 112K |
| lens review: reviewer before the lenses (diff, context, categories) | 0.26 | 6 |
| lens review: five lenses | 0.73 | |
| lens review: reviewer after the lenses (own analysis, settling candidates, report) | 0.68 | 17, mean context 112K |

About 70% of the after-phase is re-reading the cached context on every
call. There is no separate judge: the reviewer that runs the protocol also
settles the candidates.

- **Is an unneeded lens free?** No. On changes that touch no `HAVE_*`,
  `CONFIG_*`, `#if`, configure or test file, the build lens still cost
  $0.13, 9% of the review -- the same as where it has work. Skipping it on
  those changes (11 of 43 benchmark cases; one run each) kept recall on
  their known bugs (10/12, the two misses not build-lens bugs) and saved
  0-13% there, about 2% across all reviews: not worth a rule yet.
- **A lean reviewer** that does not re-hunt the lenses' classes (no
  callstack.md; keeps reachability, commit message, Fixes:, cross-lens
  questions) kept recall on the 8 cases (6/11) but saved only 7%: it still
  made 17 calls after the lenses, settling candidates.
- **The reviewer at `--effort medium`** (lenses at medium too): 6/11 on
  the 8 cases, $1.30 (-22%) and 2.9 minutes (-30%), its after-phase down
  to 9 calls. Its one miss is the qmt bug, in the run where it skipped the
  lenses on the ten-line change ("the change is about ten lines in three
  files, so I did the Task 2 analysis myself"); TASK 1D now says to run
  them for every change, however small. That rerun was cut short by a
  usage limit, so medium is not yet the default.

## Setup notes

- The lenses run as a custom agent type, `lens`, defined per review with
  `claude --agents`; its `model` and `effort` are honoured (a Haiku lens's
  messages report Haiku; `effort` low vs high changes its thinking). A
  custom agent can only use tools the session has, so a lean review adds
  `Agent` to its `--tools`.
- Claude Code 2.1.325 lists the Agent tool as `Task` and drops `TodoWrite`
  from a `--tools` list; neither affects lens reviews.
- In one of ~120 lens reviews a lens changed directory and wrote its
  `./review-context/LENS-data.json` under the prompts directory, where the
  reviewer never looked. The merged `--lenses` gives each lens an absolute
  output directory instead.
- The benchmark arms with lenses at high used general-purpose subagents that
  inherited the reviewer's model and effort; `--lenses --lens-effort high`
  defines the same thing as the `lens` agent.

## Known issues in lreview bench found on the way

Fixed since: two `bench run`s started together raced in `prepare_repos()`
(now locked); a failed judge call was cached and counted as a miss (now
asked again and left out of the counts), and the judge's self-correction
broke the parser (the last JSON object now counts); an expired login or a
usage limit ("You've hit your 5-hour limit") failed every later review,
several after billing partial work (no further review starts now); and
`test238-setup-rc-unchecked` was not a bug (dropped). Still open:

- The `eval` set is saturated by construction (bugs the default finds at
  least sometimes); the `hard` labels are stale (Sonnet at high now finds
  three of them every time); regex scoring overcounts (37 judged against 46
  by regex on the same reviews), so always use `--judge`.

## Not tested

Opus as the reviewer with Sonnet lenses; the upstream per-file
orchestrator (`kernel/agent/orc.md`); a union of Claude and another model;
effort levels for the ordinary review; spin-cycle rounds whose code
changes between rounds; strict verification on the full pilot and eval
sets; the reviewer at medium with the "every change" wording.
