# Patch Watcher Action Flow

This document describes the product flow toward Patch Shepherd-style patch
handling. Phase 1's deterministic Maloo retest and Phase 2's read-only
unknown-failure research are implemented. Existing-Jira association followed
by a retest is available only as a two-step, operator-approved workflow;
later build, review, and source-editing actions remain designs or disabled
stubs.

The implementation-grade state, persistence, native Claude runner, human
messaging, LTVM, security, recovery, and phased-delivery contracts are in
`AGENT_ORCHESTRATION_DESIGN.md`. This document remains the product-policy flow;
the orchestration design explains how the flow can be executed safely and made
visible on the dashboard. `WORKER_ENVIRONMENT_CONTRACT.md` separately defines
the admitted execution environment in which an agent may perform that flow.

## Per-patch controls

Each watched Gerrit patch gets its own test-error policy beside its status:

- **Disabled:** observe the safe top-level gates but create no action;
- **Advise:** calculate and display the exact eligible action;
- **Approval:** prepare a durable action and require a separate operator
  confirmation; or
- **Automatic:** permit an eligible action only while the separately confirmed
  global external-execution gate is enabled.

The control also sets a per-revision action budget and exposes a dry-run
evaluation. Build and review handlers remain disabled stubs.

The settings belong to the individual patch, not to the page globally. They
must remain visible while the patch is refreshed so an operator can see both
the current Gerrit state and the selected handling policy.

## Defaults

Newly added patches use safe defaults:

- Test-error policy: **Disabled**
- Per-revision external-action budget: zero until an operator saves a policy
- Global automatic-execution gate: **Disabled**
- Build and review handlers: unavailable

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

Build-failure handling means a Jenkins build failure specifically. It is
intentionally a stub in the first implementation: it may record and surface
the Jenkins failure, but does not attempt diagnosis or other follow-up.
Retesting belongs to the test-error branch, not this build-failure stub. The
build branch is therefore a deliberate dead end until its criteria and
actions are designed separately.

## Handle reviews (Phase 4A)

The page offers two manually started, exact-revision review-handling choices.
Starting either mode confirms the immutable unresolved-comment snapshot and
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
an ambiguous push without blindly retrying. Review replies remain drafts and
are never posted by this flow. Any ambiguity or incomplete result fails to the
human instead of widening authority.

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

### Phase 4: autonomous lanes

Only well-understood, narrow patch classes can progress without a human at
every step. Those lanes must have named eligibility rules, strict budgets,
reversible outcomes where possible, monitoring, and a clear escalation path.
The default for all other patches remains human approval.

The detailed plan deliberately adds durable-observer and manual read-only-agent
foundation phases before automatic actions. It also records a later
containerization track, including restricted-egress and offline-tool profiles.
Initial read-only workers may run unsandboxed, visibly labeled as such, but
isolation is required before broad code execution or autonomous operation.
