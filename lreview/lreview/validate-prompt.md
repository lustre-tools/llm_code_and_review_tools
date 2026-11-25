# Validate review findings on a VM

A code review of a Lustre change found the bugs listed at the end of
this prompt. The reviewer found them by reading source code. Your task is
to show, on ltvm virtual machines, whether each bug is real in the
revision in the current directory. You give each finding a verdict
with evidence. You also write a report for each finding, as a QA
engineer writes a bug report. The developer who reads it must be able
to reproduce the bug and start the debug without you.

The current directory holds the newest revision of the change. A
finding can come from a review of an older revision. "This run" says
which.

You do not review the commit again. You do not look for new bugs. You
do not change the review.

## Rules

1. A run that does not show a bug does not refute the bug. Give
   `refuted` only with positive proof. The run reached the code path
   that the finding names, and the result was correct. Proof of the
   path is a debug log line, a counter, a `CDEBUG` message or a probe
   hit.
2. A `confirmed` verdict needs output that shows the wrong behavior
   that the finding claims. Examples are the oops in the named
   function, the wrong return code, the leaked object, the corrupt
   data. A
   failure for another reason (a setup error, an unrelated test
   failure) is not a confirmation. Say so and continue.
3. Do not stop at the first problem with the environment. A build
   error in your reproducer, or a VM that does not boot, is a problem
   to fix, not a verdict.
4. Keep to the time limit for each finding. When the time is up, give
   the best verdict that the evidence supports and move to the next
   finding.
5. Work only on VMs that you create in this run. Other sessions share
   the ltvm host. Do not stop, deploy to, or destroy a VM that you did
   not create. Do not run `ltvm release --force`.
6. Do not run `ltvm update`, `ltvm install` or `ltvm doctor --fix`.
   Do not change `/etc/ltvm.conf`. When ltvm refuses for lack of
   memory, use `--wait 1800` and wait.
7. Do not push, post, or contact Gerrit, Jira or any other service.

## Long commands

A Lustre build or deploy takes 10 to 30 minutes and prints little.
Run it in the foreground, with the Bash tool's `timeout` parameter set
to 3600000 (one hour). This session allows that value. Redirect the
output to a file under `validation/` and read the end of the file
afterwards, so that a failed build does not fill your context:

```bash
ltvm deploy-lustre <vm> --lustre-tree "$PWD" --mount \
    > validation/deploy.log 2>&1; echo rc=$?; tail -40 validation/deploy.log
```

Bound every ssh command that can hang, as the ltvm guide says.

## Procedure

1. Read the ltvm guide named in "This run". Read the commit
   (`git show --stat HEAD`, then the parts of the diff that the
   findings name).
2. Plan the VMs. One VM serves most findings: create it, build and
   deploy once, then `ltvm vm snapshot <vm> clean` after the deploy.
   Use `ltvm vm restore <vm> clean` before each finding. Create a
   cluster only for a finding that needs more than one node (failover,
   interop, LNet routing, a client and server on different nodes). Use
   `--kernel-args 'slub_debug=FZPU'` at create time when a finding is
   a use-after-free, a double free or an overrun.
3. For each finding, in ID order:
   1. Write down the claim as a test: the trigger, the wrong result
      that the finding predicts, and the observation that shows it.
   2. Pick the least intrusive method that can show the claim:

      | Method | Use |
      |--------|-----|
      | `unmodified` | The patched tree as is: a sanity subtest, a script, a command, a configuration. |
      | `fail_loc` | A fault point that is already in the tree: `lctl set_param fail_loc=... fail_val=...`. Find them in `lustre/include/obd_support.h`. |
      | `kernel-debug` | Kernel debug options: `slub_debug`, `failslab`, `fail_page_alloc`, `kmemleak`. |
      | `instrumented` | A temporary change to the tree: a new `OBD_FAIL_TIMEOUT` to widen a race window, or a `CDEBUG` line to prove a path. |
      | `build` | A build for another target or configuration, for a build break. |

   3. Write the reproducer as `validation/F<n>/repro.sh`. It must run
      again from a fresh deploy without edits. It runs on the host and
      reaches the VMs with ssh, or it says at the top where it runs.
      lreview destroys the VMs when you finish, so start the script
      with a comment block that gives the full setup from a host with
      no VMs: the `ltvm create` (or `ltvm cluster create`) command with
      all its options and kernel arguments, the deploy command, and how
      to apply `instrumentation.diff` if there is one.
   4. Run it. Save the output as `validation/F<n>/output.txt`. Where
      they apply, save `lctl dk` excerpts (`validation/F<n>/dk.txt`),
      `ltvm vm console-log` (`validation/F<n>/console.txt`) and a crash
      summary from `ltvm vm crash-collect`.
   5. Give the verdict.
   6. Write the report (below).
4. Write `validation.json` (below) before you finish. Write it also
   when a finding is inconclusive or when you run out of time. lreview
   reads only that file. A finding with no entry counts as
   inconclusive.

### The `instrumented` method

The `instrumented` method changes the code under test, so it gives
weaker evidence. Use it only when the other methods cannot show the
claim.

- Save the change as `validation/F<n>/instrumentation.diff`
  (`git diff > ...`) before you build.
- `ltvm deploy-lustre` ships the tree as it is when you deploy. Build
  and deploy the variant first. Then restore the source with
  `git checkout -- .` and `git clean -fd -- lustre lnet libcfs`.
- Redeploy the unmodified tree before the next finding.

### Code that changed since the review

"This run" says which findings come from an older revision. For each
of them, compare the code first with `git diff <reviewed commit> HEAD
-- <file>`. When the code that the finding names is gone, or no longer
does what the finding claims, give `obsolete`. Save the relevant part
of the diff as `validation/F<n>/obsolete.diff` and give it as
evidence. An obsolete finding needs no VM run and no report. When the
code moved but still does the same thing, test the finding as usual.

### The report

Write `validation/F<n>/report.md` for each `confirmed` and each
`inconclusive` finding. Write it for a developer who did not see your
run. Use these sections:

1. **Title and verdict.** One line: the bug, and the verdict.
2. **Environment.** The tested commit and patchset, the ltvm target,
   the kernel version, the VM shape (vCPUs, memory, disks, cluster
   roles) and the kernel arguments.
3. **Steps to reproduce.** From a host with no VMs. Give the path of
   `repro.sh`, and of `instrumentation.diff` if there is one.
4. **Expected result and actual result.**
5. **Evidence.** Short excerpts of the console log, `lctl dk` and the
   crash summary that show the failure. Give the path of each full
   file.
6. **Analysis.** Where in the code the failure starts. What the run
   proves, and what it does not prove.
7. **Debug hints.** The debug masks, `fail_loc` values or probes that
   helped, and what to try next.

For an `inconclusive` finding, sections 4 to 7 say what you tried,
what you saw, and what a longer run needs.

For a `refuted` finding, write a short report with the title, the
environment and the steps. Add the evidence that the run reached the
code path and that the result was correct.

A `not-testable` finding needs no report. Its `summary` gives the
reason in one or two sentences.

### Baseline run

When "This run" says the baseline run is ON, run the reproducer of
each `confirmed` finding on the parent commit too. Use `git checkout
HEAD^`, deploy and run. Then use `git checkout -` and deploy again. Record
`"baseline": "passes"` when the bug does not show on the parent (the
commit introduced it) or `"fails"` when it does (the bug is older).
When the baseline run is OFF, omit the field.

### Verdicts

| Verdict | Meaning |
|---------|---------|
| `confirmed` | The VM showed the wrong behavior that the finding claims. |
| `refuted` | The run reached the code path and the result was correct. |
| `obsolete` | The code that the finding names is gone or changed in the tested revision, so the claim no longer applies. Only for a finding reviewed on an older revision. |
| `inconclusive` | You ran a reproducer, but it did not show the failure, and you have no proof that the path ran correctly. Say why: for example "race window not hit in 200 runs". |
| `not-testable` | No VM can show the claim. Say why: for example "needs a CXI NIC", "the finding is about a comment", "needs a 64K page target, none on this host". |

## Output: validation.json

Write `./validation.json` in the current directory:

```json
{
  "target": "rocky9",
  "vms": ["<the VMs you created>"],
  "findings": [
    {
      "id": "F1",
      "verdict": "confirmed",
      "method": "fail_loc",
      "target": "rocky9",
      "baseline": "passes",
      "summary": "One or two sentences: what you ran and what it showed.",
      "steps": "1. ltvm create ... 2. ltvm deploy-lustre ... 3. ...",
      "actual": "The failure, as the output shows it (a few lines).",
      "reproducer": "F1/repro.sh",
      "instrumentation": null,
      "report": "F1/report.md",
      "evidence": ["F1/output.txt", "F1/console.txt"]
    }
  ]
}
```

- One entry for each finding ID, with the same `id`.
- `verdict` is one of `confirmed`, `refuted`, `obsolete`,
  `inconclusive`, `not-testable`. `method` is one of `unmodified`,
  `fail_loc`, `kernel-debug`, `instrumented`, `build`, or null for
  `obsolete` and `not-testable`.
- Paths are relative to `validation/`. lreview counts a `confirmed` or
  `refuted` verdict only when its reproducer or an evidence file
  exists. It counts an `obsolete` verdict only with its diff.
  Otherwise the verdict becomes `inconclusive`. Always write the report
  too. lreview marks a verdict with no report as incomplete.
- `summary`, `steps` and `actual` go to Gerrit for a confirmed
  finding, in a reply to the finding. Write them for the patch author:
  plain facts, the commands, the fault point, the observed failure.
  The author cannot read your files, so `steps` must work without
  them: give the commands, not the path of `repro.sh`. Keep `steps`
  and `actual` short. The full detail is in the report.
- Do not name the patchset or the target in `summary`. lreview puts
  them at the start of the reply.
- In `steps`, write `<vm>` for a VM name and `<tree>` for the Lustre
  tree. The VM names of this host mean nothing to the author.

Check the file with `python3 -m json.tool validation.json` before you
finish.
