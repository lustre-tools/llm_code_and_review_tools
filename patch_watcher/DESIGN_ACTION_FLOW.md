# Patch Watcher Action Flow

> **Status, corrected 2026-09-08.** The product policy in this document -- the
> per-patch controls, the defaults, the review `-1` gate, the test-error
> decision tree, and the phase-by-phase roadmap -- is current and was re-read
> against the code on this date.
>
> Its statements about *how* that policy is executed were corrected for the
> carve-down of 2026-09-07/08, which deleted the worker-sandboxing and
> controller-owned-write subsystem, including `gerrit_reply.py`,
> `gerrit_upload.py`, `jenkins_retrigger.py`, the LTVM guest broker, and
> `WORKER_ENVIRONMENT_CONTRACT.md`. Every sentence that promised a
> credential-free worker or a controller-performed write has been replaced with
> what the code does now: an engineering run holds the operator's own
> credentials and performs its own Gerrit writes, and what it will not do is
> asked for in its prompt rather than enforced. Where that is the answer, the
> text says so. See `~/lustre_design_docs/plans/agent-orchestration/PLAN.md`
> for the target architecture.

This document describes the product flow toward Patch Shepherd-style patch
handling. Phase 1's deterministic Maloo retest and Phase 2's read-only
unknown-failure research are implemented. Existing-Jira association followed
by a retest is available only as a two-step, operator-approved workflow;
review-comment handling and exact Jenkins build-failure repair are implemented
as engineering runs, startable manually or from a confirmed standing automatic
policy. The Phase 5B controller writes -- exact Gerrit review replies and exact
Jenkins retriggers -- were implemented and then removed: reply posting moved
into the engineering run itself, and Jenkins retrigger no longer exists in any
form. Broader autonomous external writes remain future work.

The implementation-grade state, persistence, native Claude runner, human
messaging, LTVM, security, recovery, and phased-delivery contracts are in
`AGENT_ORCHESTRATION_DESIGN.md`. This document remains the product-policy flow;
the orchestration design explains how the flow is executed and made visible on
the dashboard.

There is no separate environment contract. `WORKER_ENVIRONMENT_CONTRACT.md`
defined an admitted execution environment that had to be attested before an
agent started; it and its enforcement were deleted in the carve-down. An agent
now runs in the environment the operator has on this host, described by
`CLAUDE.md`. `pw-configure` sets that host up and `pw-doctor` reports whether it
is still fit, but neither gates a run.

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

There is no longer a separate Gerrit upload/reply or Jenkins-retrigger
capability to leave disabled. Those switches were deleted in the 2026-09-07/08
carve-down: reply posting and patchset upload are performed by the engineering
run itself, and Jenkins retrigger no longer exists. Saving a build or review
policy is therefore the whole decision -- there is no second switch standing
between the run and a real Gerrit write -- and the confirmation page says so
rather than listing capabilities that stay off.

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
Gerrit state, change source, upload a patchset, or start an agent. That remains
exactly true: the deterministic path never launches Claude, and its only remote
write is one idempotent `maloo retest` through the durable outbox.

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

After that single confirmation, Patch Watcher gives the run a checkout of the
pinned revision -- normally a numbered checkout claimed from the pool, whose
index becomes the run's `co<N>-` VM name prefix -- and starts an agent in it.

**That agent runs in the operator's own environment.** It has a host shell,
passwordless sudo, the installed LLM tools, `ltvm`, and every service
credential the operator has, because it is launched with
`capability_profile="full"`: `--permission-mode bypassPermissions`, no tool
allowlist, and the ambient environment inherited unchanged. It edits the
checkout, creates and drives its own guests, and builds and tests there. It
receives Gerrit and Jenkins credentials, and it does have host-command
capability. This is the deliberate current design, not an oversight; the
earlier promise of a credential-free worker with no host commands ended with
the 2026-09-07/08 carve-down.

The immutable result classifies the failure as `patch_caused_fixed`,
infrastructure, transient, unrelated, ambiguous, or needs_human, and records
the diagnosis, the actual diff, and the build and test evidence.

Only `patch_caused_fixed` may reach `complete`, and the controller enforces
that: a `complete` report with any other classification is rejected, as is one
whose Jenkins snapshot digest or build ID does not match the captured failure,
and the diff is re-derived from the checkout rather than taken from the report.
A run with no pool checkout -- and therefore no guest capacity -- is told it
cannot classify `patch_caused_fixed` at all and must not upload.

**The upload itself is the agent's.** For a `patch_caused_fixed` result with a
nonempty diff and successful build and test evidence, the prompt tells it to
push the new patchset with the `gerrit` CLI: commit and push, then
`git reset --soft` back to the pinned revision so the tree still carries the
change as an uncommitted diff, and report after that -- because the controller
derives its own diff from the checkout only once the report arrives. The
controller-owned upload path is gone: there is no private staging checkout, no
pre-publication recapture of the Jenkins failure, no idempotency binding over
the plan, and no kill switch. Whether the evidence justifies publishing is the
agent's judgement, made under the rules in its prompt.

A stale revision or build snapshot, an infrastructure/transient/unrelated/
ambiguous classification, a missing diff, failed validation, or resource
exhaustion escalates to a human. A settled negative verdict is recorded as
`failed` carrying its classification and diagnosis rather than discarded --
correctly concluding "this was not the patch" is the run succeeding at what it
was asked. A successful push is observed on the next refresh as a new revision,
which stales the run that made it.

Because the push is no longer a claimed controller action, there is nothing to
reconcile after an ambiguous one. An agent that pushes and then dies leaves the
next refresh to discover the new patchset. Jenkins retriggers, aborts, and
configuration changes are outside this flow entirely: the prompt forbids every
Jenkins write, and the controller has no retrigger of its own since Phase 5B
was removed.

## Handle reviews (Phase 4A)

The page offers two exact-revision review-handling choices. Either may be
started manually or by a standing automatic policy, but automatic use requires
an explicit confirmation of that policy plus the independent global execution
gate. Starting either mode binds the immutable unresolved-comment snapshot and
starts a Claude Code run against it.

**That run holds the operator's Gerrit credentials.** Like build repair, it is
an engineering run with a host shell and no tool allowlist. The earlier
statement that "the worker has no Gerrit credentials" ended with the
2026-09-07/08 carve-down, which deleted the controller-owned reply and upload
writers.

- **Handle simple comments:** a narrowly scoped prompt. It may fix clearly
  trivial review comments, but must leave harder or ambiguous ones unattempted
  and return one precise human question instead.
- **Handle all comments:** permission to attempt every review comment. If it
  cannot resolve one safely, or judges that human judgment is needed, it leaves
  the comment unresolved and returns a question.

In both modes the controller preserves the full exact-revision review snapshot
and holds the run to it. Each thread in the snapshot is one target, and the
target comment is the **last** entry of that thread's `comments` array -- the
newest comment, usually a follow-up rather than the one that opened the thread.
The report must carry exactly one disposition per target comment ID, that set
and no other, in every report including a question; the controller compares the
sets and fails the run on any difference. A `complete` report may not contain a
deferred comment, and in `simple` mode may not contain a comment assessed
`nontrivial` or `ambiguous`. The reported review mode and snapshot digest must
match the run's own. The diff is re-derived by the controller from the checkout
and cross-checked against the reported changed files.

**The replies and the patchset are the agent's own writes.** A complete run
must have successful test evidence and a nonempty diff, and the prompt then
tells it to post each reply on that thread's target comment and upload the new
patchset itself with the `gerrit` CLI. There is no draft stage, no separate
reply confirmation, and no controller preflight on the reply's revision and
location. A run with no guest capacity is told it cannot build or test, cannot
reach a complete result, and must post nothing and upload nothing -- that is
now the only case in which replies stay unposted.

Any ambiguity or incomplete result fails to the human instead of widening
authority. What has changed is the shape of that guarantee: it is enforced on
what the controller can check for itself -- the snapshot digest, the target
comment set, the assessment rules, the diff -- and asked for in the prompt
everywhere else.

## Phase 5B controller writes (built, then removed)

**Both actions were implemented and then deleted in the 2026-09-07/08
carve-down.** They were exact Jenkins retrigger and immutable Gerrit
review-reply posting: two independent controller capabilities, both defaulting
to off, with separate kill switches and durable claims, never exposing
credentials to a worker, and neither implied by enabling patchset upload.

A Jenkins retrigger was bound to one completed failed parent build and its
exact change, patchset, revision, ref, project/branch, and failure-snapshot
digest, with a terminal one-use dispatch identity: success completed it, and a
failed or ambiguous dispatch was reconciliation-only rather than blindly
retryable.

A review reply was bound to the immutable comment ID and file/line/range on the
revision where the comment was originally made -- which may be historical once
the review handler has uploaded a new patchset -- and the preflight verified
that original revision and exact unresolved comment and location rather than
rewriting the target to the newly current revision. Posting was a separately
confirmed action using a deterministic Gerrit tag and a one-use,
reconciliation-only claim.

Where each went:

- **Reply posting moved into the review-handling run**, which now posts each
  reply itself as described above. The historical-revision binding, the
  location preflight, the deterministic tag, the one-use claim, and the
  independent kill switch went with the writer. The prompt tells the agent
  which comment each reply belongs on; nothing verifies that it landed there.
- **Jenkins retrigger was removed and not replaced.** No controller action
  performs one, and the agent's prompt forbids every Jenkins write -- no build,
  retrigger, or cancel. Jenkins access is read-only, through the
  failure-snapshot client.

This is the section a reader is most likely to remember wrongly. There are no
longer any separate external-write capabilities, kill switches, or durable
write claims. Confirming a build or review policy starts an engineering run,
and that run's own credentials are the authority for everything it does.

## Agent orchestration roadmap

Patch Watcher will grow from an observer into a controlled engineering-agent
orchestrator. The design must remain incremental: a capability is unavailable
until its policy, trigger, execution boundary, reporting, and recovery path
are all implemented.

"Execution boundary" no longer means a capability grant. For an engineering
run it means the checkout the run is given, the VM name prefix it owns, the
launch-time refusal of a revision that rewrites the agent's own instructions,
and the rules its prompt states. Isolation -- a container, restricted egress,
withheld credentials -- is the intended boundary and is not built.

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

Read-only runs receive only the tools their work needs: `Read`, `Glob`, `Grep`,
with the hardening flags on and service credentials scrubbed from the
environment. Engineering runs receive everything the operator has, and their
run page says so. Every *controller* action is logged with the patchset,
reason, and result; an agent's own writes are visible only through its report,
its captured diff, and the Gerrit state a later refresh observes. Human
escalation moves the run to **waiting for human** and sends the configured
notification; it does not retry indefinitely.

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

Permissions are limited to read-only Gerrit/Maloo inspection and a single
idempotent Maloo retest request. No Gerrit write, code change, patch upload, or
Claude session is part of this phase, and that is still exactly true.

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

**Implemented, but not as scoped here.** An agent receives a checkout pinned to
a patchset -- normally a numbered pool checkout, whose index is its VM name
prefix -- and may build, test in the guests it creates, and change the source.

The original scoping said the change "remains an artifact for review" and that
uploading required "a separate, explicit capability and policy". Neither holds.
The separate upload capability was deleted in the 2026-09-07/08 carve-down, and
the review and build-repair flows now instruct the agent to push the patchset
itself. A plain engineering run started from the **Investigate**/engineering
action is the exception the original rule survives in: its prompt says the
session produces a diff and evidence for human review and tells it not to
upload a patchset unless the operator asked for one -- an instruction, not a
withheld capability.

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

The detailed plan deliberately added durable-observer and manual
read-only-agent foundation phases before automatic actions, and both were
built. It also records a later containerization track, including
restricted-egress and offline-tool profiles.

That track has not been started, and the sequencing it assumed did not hold.
Read-only workers do run unsandboxed and are visibly labeled as such; but broad
code execution arrived before isolation rather than after it, so an engineering
run today builds and tests patch code on the host with the operator's own
credentials. The isolation requirement stands for autonomous operation, which
is why the only autonomous lane is one that starts no agent at all.
