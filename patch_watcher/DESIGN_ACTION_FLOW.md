# Patch Watcher Action Flow

This document describes the product flow toward Patch Shepherd-style patch
handling. Phase 1's deterministic Maloo retest and Phase 2's read-only
unknown-failure research are implemented. Existing-Jira association followed
by a retest is available only as a two-step, operator-approved workflow;
manual review-comment handling and exact Jenkins build-failure repair are also
implemented as isolated engineering runs. The first Phase 5B controller
writes—exact Gerrit review replies and exact Jenkins retriggers—are implemented
as manual, independently gated actions. Broader autonomous external writes
remain future work.

The implementation-grade state, persistence, native Claude runner, human
messaging, LTVM, security, recovery, and phased-delivery contracts are in
`AGENT_ORCHESTRATION_DESIGN.md`. This document remains the product-policy flow;
the orchestration design explains how the flow can be executed safely and made
visible on the dashboard. `WORKER_ENVIRONMENT_CONTRACT.md` separately defines
the admitted execution environment in which an agent may perform that flow.

## Per-patch controls

Each watched Gerrit patch has one compact standing policy beside its status:

- **Trigger:** manual or automatic;
- **Tests:** off, deterministic handling, or investigate unknown failures;
- **Builds:** off or repair the exact Jenkins failure; and
- **Reviews:** off, handle simple comments, or handle all comments.

The saved policy follows the Gerrit change, while every decision binds to an
exact patchset, revision SHA, and evidence fingerprint. Manual buttons and
automatic observation use the same coalescing identity, so the same event does
not start two runs. Automatic triggers require the independent global
execution gate. The controller retains bounded per-revision action/run budgets
for the underlying deterministic and research flows.

The settings belong to the individual patch, not to the page globally. They
must remain visible while the patch is refreshed so an operator can see both
the current Gerrit state and the selected handling policy.

## Defaults

Newly added patches use safe defaults:

- Trigger: **Manual**
- Tests, builds, and reviews: **Off**
- Per-revision external-action budget: zero until an operator saves a policy
- Global automatic-execution gate: **Disabled**
- Gerrit upload/reply and Jenkins-retrigger capabilities: **Disabled**

Defaults should be configurable later, but changing them must never silently
enable an automated action for existing patches.

## Implemented Phase 1 flow

1. Refresh the patch and record its Gerrit, review, and CI state.
2. Present the state and the patch's selected handling settings.
3. Apply the patch's Disabled, Advise, Approval, or Automatic policy.
4. Record a fingerprinted decision, durable trigger/run, and exact action when
   policy permits it.
5. Before a Maloo request, re-fetch Gerrit and reconcile Maloo remote state.
6. Request at most one session-level retest, enter `waiting_external`, and
   observe its outcome without blind retries.

This phase can request only a Maloo retest. It cannot post comments, alter
Gerrit state, change source, upload a patchset, or grant an agent authority.

## Test-error workflow (modeled on Patch Shepherd)

### Top-level gate

Before checking any tests, inspect the current patchset's review votes. If a
reviewer other than Maloo has submitted a `-1` review, stop this flow for the
patch: record the reviewer, patchset, and review message, mark the patch as
**needs human review**, and do not query or process test failures. A Maloo
`-1` is a CI signal and does not trigger this gate.

The implemented test-error flow is:

1. Check the patch's current Gerrit patchset and fetch its Maloo results.
2. Consider only enforced test failures; record the test, session, suite, and
   failing subtests in the error log.
3. If a retest is already pending for a test group, wait and do not duplicate
   it.
4. Inspect each failed suite for linked bugs. A linked bug provides the
   explanation to carry forward and is the basis for a later retest request.
5. If no bug is linked, collect the failure details for research rather than
   guessing. Patch Shepherd sends those unknown failures to its JIRA research
   agent, which searches for a matching issue and assesses whether the patch
   is related.
6. Record the resulting recommendation (retest, needs review, stop, or
   investigate) and include it in the next status report.

## Build-error handling (Jenkins)

Build-failure handling means repair of one completed Jenkins failure for the
exact current Gerrit revision. It is distinct from Maloo test-error retesting.
It starts either manually or from an explicitly confirmed automatic standing
policy while the independent global gate is enabled. The start grant binds all
authority to the change, patchset, revision SHA, Gerrit ref, Jenkins job and
build number, and a digest of the captured build and bounded console-log
snapshot.

After that single confirmation, Patch Watcher creates a dedicated full
checkout. Claude may edit it and run open-ended diagnostic, build, and test
commands only inside LTVM guests carrying the exact run-owner ID. It receives
neither Gerrit nor Jenkins credentials and has no host-command capability.
The immutable result classifies the failure as `patch_caused_fixed`,
infrastructure, transient, unrelated, or ambiguous and records the diagnosis,
actual diff, and explicit guest build and test evidence.

Only `patch_caused_fixed` may publish, and only when the diff is nonempty and
both an explicitly tagged build step and an explicitly tagged test step
succeeded. The run-start confirmation preauthorizes exactly one
controller-owned patchset upload, so there is no second upload confirmation.
Immediately before publication the controller recaptures the exact Jenkins
failure, rechecks the Gerrit revision, reconstructs and validates the proposed
commit in private staging, and uses the durable upload ledger's idempotency
binding over the run, change, patchset, revision, diff, and validation evidence.

A stale revision or build snapshot, infrastructure/transient/unrelated/
ambiguous classification, missing diff, failed or incomplete validation,
resource exhaustion, or preparation/upload problem escalates to a human.
After a claimed or ambiguous push, normal completion and periodic restart
reconciliation inspect Gerrit for the proposed commit; they never blindly
repeat the push. Success refreshes the watched change so the uploaded patchset
is observed as a new revision and the completed run's old authority cannot be
reused. Jenkins retriggers, aborts, configuration changes, and other wider
Jenkins writes are outside this phase. Exact Jenkins retrigger is implemented
separately in Phase 5B and disabled by default; aborts, configuration changes,
and other wider writes remain future work. The same exact failed build is a
terminal one-use dispatch identity: failure or ambiguity after a claim permits
reconciliation only, not another blind submission.

## Handle reviews (Phase 4A)

The page offers two exact-revision review-handling choices. Either may be
started manually or by a standing automatic policy, but automatic use requires
an explicit confirmation of that policy plus the independent global execution
gate. Starting either mode binds the immutable unresolved-comment snapshot and
authorizes an isolated Claude Code run. The worker has no Gerrit credentials.

- **Handle simple comments:** shell out to Claude Code with a narrowly scoped
  prompt. It may fix clearly trivial review comments, but must leave harder or
  ambiguous comments unresolved, report them, and escalate to a human (for
  example by email).
- **Handle all comments:** shell out to Claude Code with permission to attempt
  every review comment. If it cannot resolve a comment safely, or determines
  that human judgment is needed, it leaves the comment unresolved and
  escalates to a human.

In both modes the controller preserves the full exact-revision review
snapshot, records one disposition per target comment, captures the proposed
diff and reply drafts, and requires successful LTVM test evidence. A qualifying
run uploads one new patchset automatically under the run-start authorization;
there is no second upload confirmation. The controller rechecks both the
revision and comment-snapshot digest immediately before upload and reconciles
an ambiguous push without blindly retrying. Review replies remain drafts
during this engineering flow. If the separate reply capability is enabled, an
operator may later confirm posting the exact immutable drafts. Any ambiguity
or incomplete result fails to the human instead of widening authority.

## Phase 5B controller writes

Exact Jenkins retrigger and immutable Gerrit review-reply posting are
implemented as two independent controller capabilities. Both default to off,
have separate kill switches and durable claims, and never expose credentials
to a worker. Enabling patchset upload enables neither action.

A Jenkins retrigger is bound to one completed failed parent build and its exact
change, patchset, revision, ref, project/branch, and failure-snapshot digest.
Its dispatch identity is terminal and one-use: success is complete, while a
failed or ambiguous dispatch is reconciliation-only and cannot be blindly
retried as the same action.

A review reply is bound to the immutable comment ID and file/line/range on the
revision where the comment was originally made. That original revision may be
historical after the review handler uploads a new patchset. The preflight must
therefore verify the original revision and exact unresolved comment/location;
it must not rewrite the target to the newly current revision. Posting remains
a separately confirmed action and uses a deterministic tag plus a one-use,
reconciliation-only claim.

## Agent orchestration roadmap

Patch Watcher will grow from an observer into a controlled engineering-agent
orchestrator. The design must remain incremental: a capability is unavailable
until its policy, trigger, execution boundary, reporting, and recovery path
are all implemented.

### Common control model

Each patch owns an independent policy and, at most, one active run of each
controller-managed workflow; deterministic Phase 1 runs do not start an agent.
The page will eventually show:

- enabled capabilities (for example, automatic retest or review handling);
- triggering mode: manual only, on matching state change, or scheduled;
- the active run's state: queued, running, waiting for human, complete,
  failed, or cancelled;
- started time, last activity, current step, and a bounded human-readable
  activity log;
- a durable run history containing inputs, decisions, tool actions, results,
  errors, and links to artifacts.

Before starting a run, the controller transactionally claims that patch's
single active-run slot. If
an active run already owns that patch, the trigger is recorded as coalesced and
does not start a second agent. A newer patchset invalidates stale work and is
shown clearly; it never silently applies an old run's result to the new
patchset.

All agent actions have an explicit capability grant. The agent receives only
the tools needed for its enabled capability, and every external action is
logged with the patchset, reason, and result. Human escalation moves the run
to **waiting for human** and sends the configured notification; it does not
retry indefinitely.

### Phase 1: automatic retest

**Implemented.** This is the first executable capability, modeled closely on
Patch Shepherd.

1. On refresh, evaluate the review `-1` gate and test-error policy.
2. Inspect enforced Maloo failures and detect any already-pending retest.
3. Group enforced failures by Maloo session. Queue one bounded session-level
   retest only when every failed suite in that session has accepted Jira
   evidence.
4. For an unknown failure, record the evidence and recommend or request
   human/agent investigation according to policy; do not invent a bug link.
5. Record each request and its outcome in the run history, then include it in
   the daily report.

Initial permissions are limited to read-only Gerrit/Maloo inspection and a
single Maloo retest request. No Gerrit write, code change, or patch upload is
part of this phase.

### Phase 2: investigation agents

**Implemented for unknown Maloo failures.** A separately configured policy is
Disabled, Manual, or Automatic, with a per-revision run budget. Automatic
research also respects the global execution kill switch. The controller gives
Claude an immutable, exact-revision evidence bundle and pinned source checkout
with only Read, Glob, and Grep. External evidence is untrusted input. The
structured result must classify the failure as known failure, transient,
patch-caused, needs human, or inconclusive and cite only captured evidence.
It cannot modify source or contact Gerrit, Maloo, Jira, Jenkins, or LTVM.

After research, an operator may enter an existing Jira key for an exact
observed failure. Planning is inert. The association requires its own signed
confirmation, exact-revision revalidation, and remote acceptance. Only then
is a separate retest action planned, and that retest requires a second signed
operator confirmation. Ambiguous writes are terminal and are never blindly
retried. Jira creation, Gerrit comments, and automatic failure association are
not implemented.

### Phase 3: controlled patch work

An agent receives an isolated full checkout for a pinned patchset,
build, run prescribed tests, and prepare a proposed patch revision. Any
change remains an artifact for review; uploading a Gerrit patchset requires a
separate, explicit capability and policy.

### Phase 6: autonomous lanes

**Phase 6A and the first Phase 6B lane are implemented.** Lane definitions are
code-owned, named, and versioned; saved state can select a definition but
cannot change its predicates or capabilities. Global, project, and patch kill
switches default off, use optimistic concurrency, and require a signed one-use
confirmation when authority is widened. Disabling is immediate.

The first lane, `deterministic-test-retest` version 1, wraps the existing
crash-safe Maloo retest path. It does not introduce a second writer. An
enrolled patch still needs the existing automatic/deterministic standing
policy and primary global gate. It admits only one already-safe retest action
per exact revision, grants no agent run, Gerrit, Jenkins, Jira, or LTVM
capability, and rechecks every switch immediately before the Maloo write.
Unknown evidence, incomplete Jira association, a non-Maloo -1, patchset drift,
budget exhaustion, and external ambiguity all stop rather than widen scope.

Every exact decision stores normalized evidence, the control generation,
reason, capability, and budget in an append-only private audit. Replay invokes
only the pure evaluator and creates no triggers, runs, actions, or remote
writes. The dashboard displays the definition, switches, decision reasons,
outcomes, budget, and replay result. Patches not enrolled in a lane retain the
pre-existing approval/standing-policy behavior.

The detailed plan deliberately adds durable-observer and manual read-only-agent
foundation phases before automatic actions. It also records a later
containerization track, including restricted-egress and offline-tool profiles.
Initial read-only workers may run unsandboxed, visibly labeled as such, but
isolation is required before broad code execution or autonomous operation.
