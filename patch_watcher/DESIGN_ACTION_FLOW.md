# Patch Watcher Action Flow

This document describes the first step toward Patch Shepherd-style patch
handling. It is a design plan only: the controls do not execute automated
actions yet.

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

Build-error handling will follow the same observe, classify, log, and
recommend pattern, with its own criteria added when that checkbox is
implemented.
