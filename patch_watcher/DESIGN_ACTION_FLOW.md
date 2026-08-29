# Patch Watcher Action Flow

This document describes the first step toward Patch Shepherd-style patch
handling. It is a design plan only: the controls do not execute automated
actions yet.

The implementation-grade state, persistence, native Claude runner, human
messaging, LTVM, security, recovery, and phased-delivery contracts are in
`AGENT_ORCHESTRATION_DESIGN.md`. This document remains the product-policy flow;
the orchestration design explains how the flow can be executed safely and made
visible on the dashboard.

## Per-patch controls

Each watched Gerrit patch gets its own action settings beside its status:

- **Action:** a dropdown whose initial option is **Do nothing**.
- **Handle build errors:** a checkbox.
- **Handle test errors:** a checkbox.

The settings belong to the individual patch, not to the page globally. They
must remain visible while the patch is refreshed so an operator can see both
the current Gerrit state and the selected handling policy.

## Defaults

Newly added patches use safe defaults:

- Action: **Do nothing**
- Handle build errors: unchecked
- Handle test errors: unchecked

Defaults should be configurable later, but changing them must never silently
enable an automated action for existing patches.

## Planned flow

1. Refresh the patch and record its Gerrit, review, and CI state.
2. Present the state and the patch's selected handling settings.
3. If a checked error category matches, record a pending recommendation.
4. In a later phase, add an explicit operator-approved action for that
   recommendation (for example, investigate or request a retest).

Until that later phase is designed and approved, Patch Watcher remains
read-only: checking a box records intent but does not post comments, request
retests, alter Gerrit state, or send other external actions.

## Test-error workflow (modeled on Patch Shepherd)

### Top-level gate

Before checking any tests, inspect the current patchset's review votes. If a
reviewer other than Maloo has submitted a `-1` review, stop this flow for the
patch: record the reviewer, patchset, and review message, mark the patch as
**needs human review**, and do not query or process test failures. A Maloo
`-1` is a CI signal and does not trigger this gate.

When test-error handling is eventually enabled, the intended flow is:

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

## Handle reviews (future stubs)

The page will eventually offer two review-handling choices. Both are stubs in
the first implementation and are disabled: they do not invoke Claude Code or
modify Gerrit.

- **Handle simple comments:** shell out to Claude Code with a narrowly scoped
  prompt. It may fix clearly trivial review comments, but must leave harder or
  ambiguous comments unresolved, report them, and escalate to a human (for
  example by email).
- **Handle all comments:** shell out to Claude Code with permission to attempt
  every review comment. If it cannot resolve a comment safely, or determines
  that human judgment is needed, it leaves the comment unresolved and
  escalates to a human.

In both modes, the eventual integration must preserve the full review context,
log Claude's result, and require explicit policy/configuration before any
write action is enabled.

## Agent orchestration roadmap

Patch Watcher will grow from an observer into a controlled engineering-agent
orchestrator. The design must remain incremental: a capability is unavailable
until its policy, trigger, execution boundary, reporting, and recovery path
are all implemented.

### Common control model

Each patch owns an independent policy and, at most, one active agent run.
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

This is the first executable capability, modeled closely on Patch Shepherd.

1. On refresh, evaluate the review `-1` gate and test-error policy.
2. Inspect enforced Maloo failures and detect any already-pending retest.
3. For a failed suite with a linked bug, queue one bounded retest request.
4. For an unknown failure, record the evidence and recommend or request
   human/agent investigation according to policy; do not invent a bug link.
5. Record each request and its outcome in the run history, then include it in
   the daily report.

Initial permissions are limited to read-only Gerrit/Maloo inspection and a
single Maloo retest request. No Gerrit write, code change, or patch upload is
part of this phase.

### Phase 2: investigation agents

An agent can investigate an unknown test failure or review feedback. It may
read patch context, CI details, and linked issue data; it produces an evidence
report and either a recommendation or a human escalation. It still does not
modify source or Gerrit.

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
