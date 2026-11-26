# VM validation of review findings (`--validate`)

## Goal

A review finding today is an inference from source reading. The `--validate` option adds a second phase. In that phase, an agent tries to show each selected finding on an ltvm VM. The result for each finding is a verdict with evidence: a reproducer script, its output, and the logs.

The reader of a posted review can then tell a demonstrated bug from a suspected bug.

## Current behavior

1. `run_batch` prepares one worktree for each change.
2. `_review_and_cleanup` runs the reviewer agent, then removes the worktree.
3. The reviewer writes `gerrit-review.json`. Each inline comment starts with a severity marker: `(defect)`, `(minor)`, `(style)`, `(typo)`, `(suggestion)` or `(nit)` (review-prompts `kernel/gerrit-review.md`).
4. `lreview post` posts the collected JSON.

No phase runs code. The `--lenses` option adds more candidates, but the reviewer settles them by reading source.

## Proposed behavior

### Command line

```bash
lreview run 64086 --repo lustre-release --validate
lreview run 64086 --validate --validate-classes defect,minor
lreview validate 64086                      # an earlier review, no new review
lreview validate 64086 --force              # test every finding again
lreview run --repo ~/tree --last 2 --validate
```

| Option | Default | Effect |
|--------|---------|--------|
| `--validate` | off | Validate the findings after each review. |
| `--validate-classes LIST` | `defect` | The markers to validate, comma-separated. `all` selects every marker. |
| `--validate-jobs N` | `1` | Validations that run at the same time. Each one holds VMs. |
| `--validate-model`, `--validate-effort` | `opus`, `high` | Model and effort of the validator agent. |
| `--validate-timeout SECS` | `5400` | Time limit for the validation of one change. |
| `--validate-target T` | `rocky9` | The ltvm target for the default VM. |

`--validate` is a plain flag. The flag does not take an optional value. With `nargs='?'`, argparse reads `lreview run --validate 64086` as `--validate=64086`, because the change numbers are positional. A separate `--validate-classes` option prevents that problem.

`lreview validate <change>...` validates an earlier review, with no new review. Thus you can review cheaply first, read the findings, and then validate only the changes that matter. The section "Workflows" gives where its findings come from and which revision it tests.

### Flow

```
review pool (--jobs 5)               validation pool (--validate-jobs 1)
  worktree -> reviewer -> JSON  --->   selected findings?
                                         no:  remove worktree
                                         yes: validator agent in the same
                                              worktree -> validation JSON
                                              -> destroy VMs -> remove worktree
```

1. The review phase does not change.
2. lreview parses `gerrit-review.json`. It selects each comment whose marker is in `--validate-classes`. The full and light prompts both write the markers. A comment with no marker counts as `(minor)`. It gives each selected comment an ID: `F1`, `F2`, and so on, in file and line order.
3. With no selected comment, lreview removes the worktree as it does now.
4. With selected comments, the change goes into the validation queue. The worktree stays, because the validator builds Lustre from it.
5. The validator agent runs in the worktree. It writes `validation.json` and a `validation/F<n>/` directory for each finding.
6. lreview collects the files to the results directory, destroys the VMs that the validator made, and removes the worktree.

Validation always uses a dedicated worktree. A local review of the checked-out HEAD runs in place, in the user's own checkout (`run_batch(in_place=True)`). For that review, lreview makes a worktree for the validation. Thus the `instrumented` method, the baseline checkout and `.ltvm-staging/` never touch the user's checkout.

The validation limit is separate from the review limit, and it is smaller. Five reviews run well in parallel. Five sets of VMs on one host do not, because of the ltvm memory budget. One validation at a time is the safe default. A semaphore with `--validate-jobs` slots sets the limit. The review worker thread runs the validation. While it waits for a slot, it keeps its review slot, so a later review can start late.

### Workflows

Chris settled these uses on 2026-11-25.

| Use | Findings | Tested revision | Posting |
|-----|----------|-----------------|---------|
| `run --validate` | The new review. | The reviewed patchset. | None. `lreview post` posts later. |
| `run --validate --post` | The new review. | The reviewed patchset. | One post for each change, after its validation, with the verdicts applied. |
| `run --local`, `--last N` | The new review. | The reviewed commit. | Never. The text dump holds the full QA reports. |
| `validate N`, with a local review of N | The local review JSON. | The newest patchset, or the merged commit. | With `--post`: a review not yet posted goes out with the verdicts applied. A posted review gets thread replies. |
| `validate N`, no local review of N | The unresolved AI-review threads on Gerrit, from any account. | The newest patchset, or the merged commit. | With `--post`: thread replies. |
| `validate N --drafts` | The unresolved drafts of the logged-in account. | The newest patchset, or the merged commit. | With `--post`: the verdict goes into each draft. Nothing is published. |

Rules that apply to every use:

- **Tested revision.** A Gerrit change is always tested on its newest patchset. Each verdict records the tested commit and patchset, and each reply names the patchset. A new patchset during `run --validate` does not stop the post. The post goes on the reviewed patchset, and the reply text names the tested one.
- **Findings from Gerrit.** Each unresolved thread whose root comment has the tag `autogenerated:ai-review` becomes one finding. The newest comment of a thread decides the state of the thread. Before it reads the severity marker, lreview removes the bot prefix line. lreview does not merge two copies of one bug. The validator sees both and can give the same verdict to each. The manifest gets an entry with `"source": "gerrit"`. `post --force` never posts such an entry as a review, because the comments belong to other accounts.
- **A second validation.** A finding has a stable ID: its Gerrit comment ID, or a hash of its file and text. A second validation of the same tested commit keeps each verdict except `inconclusive`, and tests only the rest. `--force` tests every finding again. When the tested commit changed, lreview tests every finding again, because a verdict on patchset 4 says nothing about patchset 5.
- **Refuted findings.** lreview does not post them. It reports them on stdout, and it writes them to `refuted-<change>.json` in the format that `gc review --post-comments` reads. A person can edit that file and post it. A refuted finding that is on Gerrit already gets a thread reply instead.
- **Merged changes.** A confirmed bug of a merged change is in the branch, so its proof stays private (see "Private bugs"). The option `--post` then posts only the refuted replies. The output on stdout says that the confirmed bugs are in the branch and need a ticket. It also says to report them privately. lreview does not file the ticket.
- **VMs.** lreview always destroys them, except with `--keep-vms`. The QA report holds what a developer needs to debug the bug later.
- **Review memory.** Main deprecated `--memory`, so the verdicts do not go into the memory notes.
- **Patch Watcher.** Out of scope.

### Validator agent

The validator is a new bundled prompt, `lreview/validate-prompt.md`, next to `light-prompt.md` and `lens.md`. It gets:

- The commit message and diff.
- The selected findings, each with its ID, file, line and full comment text.
- The path to the ltvm skill file, or a short ltvm guide inline. Lean reviews load no skills, so the prompt must give the path or the text.
- The VM names it may use, and the owner ID.

The prompt tells the agent to do these steps for each finding:

1. Write the claim as a test: the trigger, the expected wrong result, and the observation that shows it.
2. Pick the least intrusive method that can show the claim. The methods are in the next table, from the strongest evidence to the weakest.
3. Write a reproducer script, `validation/F<n>/repro.sh`. The script must run again from a fresh deploy without changes. A comment block at its top gives the full setup from a host with no VMs. The setup is the `ltvm create` command with its options, the deploy, and the instrumentation diff.
4. Run the script. Save the output, the `lctl dk` excerpt, the console log and the crash summary, where they apply.
5. Give a verdict.
6. Write the QA report, `validation/F<n>/report.md`.

The QA report is the bug report that a QA engineer gives to development. The report has seven sections: title and verdict, environment, steps to reproduce, expected and actual result, evidence excerpts, analysis, and debug hints. A `confirmed` or `inconclusive` finding gets a full report. A `refuted` finding gets a short report, with the proof that the path ran correctly. An `obsolete` or `not-testable` finding gets one or two lines in its summary.

| Method | What the agent does | Example |
|--------|---------------------|---------|
| `unmodified` | Runs the patched tree as is, with a test, a script or a configuration. | A new sanity subtest, a `lfs` call with a bad argument, an interop cluster. |
| `fail_loc` | Uses a fault point that exists in the tree: `lctl set_param fail_loc=`. | `OBD_FAIL_*` to force an error path, or a delay to open a race window. |
| `kernel-debug` | Boots with kernel debug options. | `slub_debug=FZPU` for a use-after-free, `failslab` for an allocation failure. |
| `instrumented` | Adds a temporary fault point or `CDEBUG` line to the tree, builds, deploys, then restores the source. | A new `OBD_FAIL_TIMEOUT` in the race window. |
| `build` | Builds for a target or configuration. | A build break on a kernel version, or with `--disable-server`. |

The `instrumented` method changes the code under test. The verdict records the method and the diff of the instrumentation, so the reader can judge the result. The `deploy-lustre` command ships the tree as it is at deploy time. The prompt therefore tells the agent to deploy the variant before it restores the source. The prompt also sends page-size findings to `rocky10-16k`, and multi-node findings to a cluster.

### Verdicts

| Verdict | Meaning | Evidence that it needs |
|---------|---------|------------------------|
| `confirmed` | The VM showed the wrong behavior that the finding claims. | Reproducer and the output that shows the failure. |
| `refuted` | The VM showed that the claimed behavior does not occur. | Proof that the run reached the code path, for example a debug log line or a hit counter, and that the result was correct. |
| `obsolete` | The code that the finding names is gone or changed in the tested revision. | A diff excerpt between the reviewed and the tested commit. Only for a finding reviewed on an older revision. |
| `inconclusive` | The agent ran a reproducer, but it did not show the failure. | Reproducer, output, and the reason that the run proves nothing, for example "race window not hit in 200 runs". |
| `not-testable` | No VM can show the claim. | The reason, for example "needs a CXI NIC" or "the finding concerns a comment". |

A run that does not show a bug does not refute it. The prompt states this rule, as `SETTLE_RULES` in `lenses.py` does for lens candidates. `refuted` needs positive proof that the code path ran.

lreview checks the files behind each verdict. These verdicts become `inconclusive`, with a note that says why:

- A `confirmed`, `refuted` or `obsolete` verdict with no reproducer or evidence file in the collected directory.
- An `obsolete` verdict for a finding that the review saw on the tested commit.

A `confirmed` or `refuted` verdict with no QA report keeps its verdict. lreview adds the note "the validator wrote no QA report". The Gerrit reply does not use the report, so the patch author still gets the reply.

### Baseline run

For a `confirmed` finding, the agent can run the same reproducer on the parent commit (`HEAD^`). A pass on the parent shows that the change introduced the bug. A failure on the parent shows a pre-existing bug. The baseline run costs one more build and deploy, so it is off by default. The options `--post` and `--post-report` turn it on, because no confirmed bug goes public without it. See fork 3 and "Private bugs".

### Private bugs

Gerrit and the LU Jira are public. A review already posts bugs that it finds in older code, as `(minor)` or `(suggestion)` comments, or with `--lenses` in the cover message. Validation adds proof: the verdict, the steps, the actual result and the QA report. For a bug in landed code, that proof is a ready exploit. The tool cannot reliably tell an exploit from a plain bug, so it treats each bug the same.

A confirmed bug is in landed code when one of these is true:

- Somebody merged the change.
- The reproducer also fails on the parent commit (the baseline run).
- The validator's code check gives `origin: base`.
- No baseline run shows that the change brought the bug in.

The code check is cheap, and the validator always does it. It compares the commit with its parent and gives `origin` (`change`, `base` or `unknown`) with a one-line reason. The baseline run decides when the code check gives `unknown`.

For a confirmed bug in landed code, lreview does these things:

- It posts the finding text as it does with no validation. Validation never removes a finding from a review.
- It posts no proof: no "Confirmed on a VM" note in the review, no thread reply, and no Jira report. It posts no bare verdict either, because a verdict alone tells a reader which public finding is a real bug.
- It leaves the bug out of the verdict counts in the cover message.
- It prints `PRIVATE: <reason>` under the finding on stdout, and marks the finding in the index. The person who ran lreview has the report and can give it to the author privately.

The bug keeps no `posted` mark. A later run with a passing baseline run can then post the proof as a reply in the thread of the finding.

A post needs the baseline run. So lreview tests a kept confirmed verdict again when that verdict has no baseline run.

### Output

`validation-<slug>.json` in the results directory:

```json
{
  "target": "rocky9",
  "vms": ["bl3-lrv-64086"],
  "findings": [
    {
      "id": "F1",
      "file": "lustre/osc/osc_request.c",
      "line": 1412,
      "class": "defect",
      "verdict": "confirmed",
      "method": "fail_loc",
      "baseline": "passes",
      "summary": "fail_loc=0x40a forces the error path. The client oopses in osc_brw_fini_request.",
      "steps": "1. ltvm create ... 2. lctl set_param fail_loc=0x40a 3. dd ...",
      "actual": "BUG: unable to handle page fault ... osc_brw_fini_request+0x1a2",
      "reproducer": "F1/repro.sh",
      "report": "F1/report.md",
      "evidence": ["F1/output.txt", "F1/console.txt"],
      "posted": false
    }
  ],
  "tested": {"sha": "...", "patchset": 5, "merged": false, "branch": "master"}
}
```

Paths are relative to the evidence directory, `validation-<slug>/`. lreview also writes `validation-<slug>.md`, an index with one line for each finding and a link to its report. At the end of a run, stdout lists each finding with its verdict and report path. Then it gives the index, the refuted findings file, and the ticket reminder for a merged change.

lreview validates this file in `_collect_json`, as it does `gerrit-review.json`. Each selected ID must have exactly one entry. A missing entry becomes `inconclusive` with the summary "validator gave no verdict".

The summary manifest gets a count for each verdict. The run report and the `--output` text dump show the verdict next to each finding.

### Posting

`lreview post` uses the verdicts. The default behavior for each verdict is in fork 2. In all choices, the posted text never includes the full reproducer. A one-line method and result goes into the comment, for example:

```
(defect) ... original comment ...

Confirmed on a VM, patchset 5 (fail_loc, rocky9): the client oopses in osc_brw_fini_request.

Steps to reproduce:
...

Actual result:
...
```

A confirmed verdict carries the summary, the steps to reproduce and the actual result. The author cannot read the local report. lreview cuts the text at 4000 characters.

Only a `confirmed` or `refuted` verdict reaches Gerrit. When a change has neither, `post` posts the review as it is, with no verdict line. Text that lreview wrote, for example "validator gave no verdict", never goes to Gerrit.

Three cases need a rule:

- **`run --post --validate`.** The post for a change waits until its validation ends. A change with no selected finding posts at once.
- **Findings that are on Gerrit already.** Two cases are the same here. One is a review that lreview posted before. The other is a validation of the AI-review threads from Gerrit. The double-post guard in `poster.py` refuses a second post of the same review. Thus lreview posts each decisive verdict as a reply in the thread of its finding, with `in_reply_to`. A reply goes on the revision of the comment that it answers, so lreview posts one review for each such revision. The replies use the prefix `[AI validation - <model>]`, with the validator's model. A finding read from Gerrit carries its comment ID. Gerrit does not return comment IDs when lreview posts a review. To get them, the tool reads the change's published comments (`GET /changes/N/comments`) and finds each finding by file, line, tag, revision and text. A finding that lreview cannot find gets a line in a patchset-level message instead. That message also gives the verdict counts. The message does not say "not posted", because the comments are on Gerrit already. gerrit-cli's `post_review` now passes `in_reply_to` through. Each verdict records its own `posted` mark, so a later run posts only the new verdicts.
- **The cover message after lreview holds a finding.** The reviewer wrote the cover message, for example "Problematic, see inline comments.", before validation. The tool does not rewrite that text. It adds one line under the text, for example `VM validation: 2 confirmed, 1 refuted (not posted), 1 inconclusive`. When lreview holds every `(defect)` finding, the line also says so. The tool posts no vote now, so no vote needs a change.

### Verdicts in the reviewer's drafts

A reviewer can post lreview's findings as Gerrit drafts, then keep, edit or delete each draft before publishing. `lreview validate N --drafts` validates those drafts.

- lreview reads the drafts with `GET /changes/N/drafts`, which needs the Gerrit login. Each unresolved draft that starts a thread becomes one finding. The patchset-level draft, a draft on `/COMMIT_MSG` and a draft reply are not findings. The drafts carry no AI-review tag, so lreview selects them by the severity marker only.
- Gerrit lets a comment reply only to a published comment. Thus with `--post`, lreview changes each draft in place (`PUT .../revisions/R/drafts/ID`, gerrit-cli `update_draft`). It adds the verdict block under the draft text, with the validation prefix on its own line. A second post replaces that block. It keeps the reviewer's edits of the text above the block, but it removes text under the block.
- lreview never publishes a draft. The setting `GERRIT_CLI_MODE` does not change this behavior.
- A reviewer can publish every draft with one click. Thus a draft gets the same proof rule as Gerrit. A confirmed bug in landed code gets the verdict and the reason, with no steps and no actual result. A bug with no baseline run keeps no `posted` mark. A later validation with the baseline tests it again, and its proof can then replace that line.
- The manifest key is `N-drafts`. Thus the threads and the drafts of one change keep separate verdicts. The option `--post-report` skips such an entry, because only the reviewer sees the findings.

### QA reports to the ticket

`lreview validate --post-report` sends the QA report of each confirmed finding to the Jira ticket of the change. The ticket is the key at the start of the commit subject, for example `LU-21098`. A subject with no key gets no upload, and stdout says so. A local review gets no upload, because it has no change to link.

- lreview calls the `jira` tool: `jira attachment upload` for each report, then one `jira comment`. The comment has the validation prefix, the change URL, and the tested patchset and commit. For each finding, it gives the link to its Gerrit comment and the attachment name. A finding read from Gerrit has its comment ID. For lreview's own posted review, lreview finds the comment as the Gerrit replies do. With no comment, the line gives the file and line.
- The attachment name is `lreview-<change>-ps<N>-<sha8>-F<n>-<time>.md`. The time is the UTC time of the verdict (`validated_at`), so two validations of one revision give two names. A verdict from before lreview recorded that time uses the time of the report file.
- Each finding records `report_posted` (the ticket and the attachment name) after its upload. A later run does not attach the same report again. The mark goes in before the comment, so a failed comment does not cause a second upload.
- Only `confirmed` findings go to the ticket. A refuted finding stays local, as for Gerrit. A bug in landed code (see "Private bugs") gets no upload, and the detail on stdout gives the reason.
- The flag is separate from `--post`. A second `lreview validate --post-report` of the same revision keeps the verdicts and only attaches the reports.

### VM lifecycle

- Before the first review or validator starts, lreview checks the host. The tool `ltvm` must be on PATH, `ltvm doctor` must report no issue, and the default kernel of `--validate-target` must have a ready image (`ltvm target list --json`). A failed check stops the command with the reason and the fix. A validator on a broken host spends its whole time limit on setup errors. It must not run `ltvm install` or `doctor --fix`, so it gives no verdict. The check takes about 2 seconds. A target list that lreview cannot read, from an older ltvm, skips the image check.
- The validator names VMs by the ltvm rule: `<hh><N>-lrv-<change>`. lreview picks `<hh>` and `<N>` before it starts the agent, and passes the names in the prompt.
- lreview exports `LTVM_OWNER_ID=lreview:<run id>:<change>` and `LTVM_OWNER_PID`. ltvm reads `LTVM_OWNER_ID` before it looks for a Claude Code session (`vm_claim.current_owner`). Thus the claims identify the run, and `ltvm list --json` finds the VMs.
- `create` and `cluster create` get `--wait 1800`. A validation waits for memory and does not fail.
- The validator uses one VM for all findings of a change. It deploys once, then uses `ltvm vm snapshot` and `ltvm vm restore` between findings.
- After the agent exits, lreview destroys every VM that has its owner ID. The cleanup also runs after a timeout, a stall or Ctrl-C. `kill_stragglers` gets a matching VM sweep.
- `--keep-vms` leaves the VMs for a person to examine.

### Long builds

The Claude Code Bash tool stops a foreground command after 10 minutes by default. A Lustre build or a deploy can take longer. Nobody has measured these times for lreview: a build takes 10 to 20 minutes, and a first build for a new kernel takes much longer. Thus lreview starts the validator with `BASH_MAX_TIMEOUT_MS=3600000`, and the prompt tells the agent to give a build or deploy a timeout of one hour. The agent runs builds in the foreground, and sends their output to a file. The lean tool set is enough, and background tasks are not necessary.

lreview ends a review as stalled when its log does not grow for 10 minutes. A validator in a one-hour build writes nothing for that time. The validation phase therefore uses a stall timeout of 65 minutes by default, 5 minutes more than the longest command. The per-change timeout still limits the cost.

### Agents

Only the `claude` agent at first. The validator needs long Bash calls, ssh, and enough judgment to write reproducers. The other agents can follow when a benchmark shows that they give good verdicts.

## Decisions

Chris accepted the recommended option of each fork on 2026-11-24. The other options stay below as a record of the rejected choices.

### Fork 1: one validator for each change, or one for each finding

- **One for each change (chosen).** One agent, one build, one VM. It shares the deploy across findings, and it can see when two findings are the same bug. The risk is that a hard finding uses the time of the others. A time limit for each finding in the prompt reduces that risk.
- **One for each finding.** Clean isolation and simple retries, but N builds and N VMs for each change. That cost is too high for a change with 4 defects.

### Fork 2: what `post` does with the verdicts

- **Annotate, and hold refuted findings (chosen).**
  A `confirmed` finding gets the validation line. The tool does not post a `refuted` finding. It lists that finding in the run report with its evidence. `inconclusive` and `not-testable` findings post as they are. A refuted finding has positive proof against it, so a post would waste the author's time.
- **Annotate only.** Post every finding, each with its verdict. That choice is the most transparent, but it puts findings with proof against them on the patch.
- **Do not change posting.** Verdicts stay local. That choice is the safest at first, but it loses the main value for the patch author.

### Fork 3: baseline run on the parent commit

- **Off by default, `--validate-baseline` to turn it on (chosen).** It doubles the build and deploy time for each confirmed finding. A review that stays local needs to know only that the bug is real. The options `--post` and `--post-report` turn it on. A post must not show a bug that is in the branch.
- **On by default.** It tells a regression from a pre-existing bug, which the review prompt now decides from source reading only.

### Fork 4: `instrumented` method

- **Allow it, and label it (chosen).** Many race findings cannot show without a widened window. The verdict records the instrumentation diff.
- **Forbid it.** Every `confirmed` verdict is then on unmodified code. More race findings become `inconclusive`.

## Risks and limits

- **Cost.** A validation includes a Lustre build of 10 to 20 minutes, a deploy, and an agent session on a large model. Expect 30 to 90 minutes and several dollars for each change. The default class `defect` keeps the number of validations low.
- **False confirmation.** An agent can write a reproducer that fails for a reason that is not the finding, for example a setup error. The prompt needs the output to show the specific wrong behavior that the finding names. The saved reproducer lets a person check the result.
- **Races.** Many `(defect)` findings are races. Without `instrumented`, most of them become `inconclusive`.
- **Hardware.** o2ib on real HCAs, kfilnd and efalnd have no VM equivalent. Their findings become `not-testable`. The `shmrdma` device covers some o2ib behavior.
- **Host contention.** Other sessions share the ltvm host. The claim mechanism and `--wait` handle the sharing. A full host makes validations slow, not wrong.
- **Trust.** A review today only reads the patch. A validation builds the patch and runs it. The `configure` script and the Makefiles of any Gerrit author run in a podman container on the host. The result runs as root in the VMs. The validator agent also runs with `--dangerously-skip-permissions` on the host. Use `--validate` only on changes from authors you trust, or on a host where that risk is acceptable. The `--allow-tool` option cannot restrict the validator, so `run` refuses `--allow-tool` with `--validate`.
- **Page size.** The project rule is 4KB and 64KB pages. ltvm has `rocky10-16k`, which is aarch64 only, and no published release of it. Its first use needs a kernel build, which can take longer than the 5400 second default. `rocky9-64k` cannot boot under Apple Silicon HVF. The validator marks a page-size finding `not-testable` when no such target is ready on the host.

## Test plan

1. Unit tests for these items: marker parsing, class selection, ID assignment, `validation.json` schema checks and the missing-verdict rule. Also test post filtering for each verdict, and `--validate` before positional change numbers.
2. VM cleanup test: kill a validation during a deploy, and check that lreview destroys its VMs.
3. Benchmark: run `lreview bench` with `--validate` on the cases in `benchmark/cases.json`. First check two conditions. The bench offline shim (`OFFLINE_COMMANDS` in `bench.py`) must not block `ltvm`, `ssh` or the container fetches. Each old merged commit must also pass the ltvm compatibility gate and build for a current target kernel. Drop the cases that fail these checks. Each case is a merged change with a bug that a later commit fixed. Measure two rates:
   - Known bugs that the reviewer finds and the validator marks `confirmed`.
   - Findings that do not match the known bug that the validator marks `refuted`. Read these by hand, because some are real bugs that the case does not list.
4. Compare `--validate-model opus` with `sonnet` on the same cases before you choose the default.

## Status

BUILT on branch `lreview-validate`, with the workflow decisions of 2026-11-25. Unit tests PASS. The `ltvm list` and owner checks ran against the real host. UNRUN end to end: no validator session has built a change on a VM yet. No verdict reply has gone to a real Gerrit change.

The branch does not build these parts of the design:

- `lreview bench` does not take `--validate` yet (test plan item 3).
- The Markdown report does not show the verdicts. The batch summary, the `--output` text dump and `summary.json` show them.
- The validator records no telemetry file. Its tokens, cost and time are in the `validation` record of `summary.json`.
