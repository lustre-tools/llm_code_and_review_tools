# Patch Watcher Agent Orchestration Design

> **Status, corrected 2026-09-08.** This document is trusted to describe the
> observer and evaluator, the durable state machines, the run/session
> lifecycle, execution profiles and timeouts, human messaging and operator
> controls, the checkout and LTVM resource lifecycle, recovery and
> reconciliation, the dashboard, the deterministic retest path, and the
> autonomous-lane framework. Those were re-read against the code on this date.
>
> It has been corrected for the carve-down of 2026-09-07/08, which deleted the
> worker-sandboxing and worker-admission subsystem -- `ltvm_mcp_server.py`,
> `ltvm_guest_exec.py`, `ltvm_ssh_transport.py`, `pw_worker.py`,
> `worker_contract.py`, `worker_doctor.py`, `worker_admission_views.py`,
> `gerrit_reply.py`, `gerrit_upload.py`, `jenkins_retrigger.py`,
> `external_action_views.py`, `worker_profiles/`, `worker_schemas/`, and
> `WORKER_ENVIRONMENT_CONTRACT.md` -- and with it the premise that an agent is
> an untrusted, credential-free worker whose external writes the controller
> performs on its behalf. Sections that described those parts now say what was
> removed and what, if anything, replaced it, rather than being deleted: the
> carve-down is part of this design's history.
>
> Read every remaining safety sentence literally. Where the answer is "the
> prompt asks the agent not to, and nothing enforces it", the text now says so.
> Isolation is a roadmap, not a control. See
> `~/lustre_design_docs/plans/agent-orchestration/PLAN.md` for the target
> architecture and the remaining phases.

This document turns the Patch Watcher action flow into an implementation
contract. `DESIGN_ACTION_FLOW.md` remains the product-policy description: it
defines when test, build, and review conditions matter. This document defines
how Patch Watcher observes those conditions, starts controlled work, exposes
that work to a person, and recovers safely.

There is no separate environment contract. `WORKER_ENVIRONMENT_CONTRACT.md`
defined a versioned worker profile, run envelope, and environment attestation
that had to be admitted before a Claude process started; it was deleted in the
2026-09-07/08 carve-down along with the code that enforced it. Its replacement
is deliberately weaker and simpler: `pw-configure` writes the private per-tool
credential files once, and `pw-doctor` reports whether this host still looks
able to run agents. Neither gates a launch. The premise is that an agent's
environment is the one a developer has on this box, described by `CLAUDE.md`,
not an attested sandbox.

The intended direction is gradual. Patch Watcher begins as an observable,
deterministic retest controller. It gains read-only Claude Code research, then
source-editing and VM-backed engineering work, and eventually narrow lanes that
may complete without a human. Containerized workers and restricted network
access remain a later phase; nothing in the current implementation isolates the
agent process from the host. Read-only research runs are labeled in the UI as
read-only, unsandboxed host workers, and engineering runs are labeled with the
capability they actually hold.

## Goals

- Preserve the current useful patch-status dashboard while adding durable
  automation state.
- Make every trigger, decision, external action, agent message, and result
  inspectable from the dashboard.
- Give a human clear controls to start, message, pause, interrupt, resume,
  cancel, retry, or supersede work.
- Never let two workers act concurrently on the same Gerrit change.
- Pin work to an exact Gerrit patchset and revision SHA; stale work must never
  affect a newer patchset.
- Grant each run only the capabilities required for that run.
- Survive Patch Watcher, the Claude runner, Claude Code, and host restarts
  without losing the audit trail or repeating ambiguous external actions.
- Reuse the LLM review tools and LTVM rather than reimplementing Gerrit, JIRA,
  Maloo, Jenkins, checkout, build, or VM operations.
- Keep the deployment simple enough to run as one local service initially.

## Product surfaces and future work entry modes

The current implementation is **patch-centric**, but the managed engineer
infrastructure should not be permanently coupled to Gerrit observations. The
long-term product has three distinct ways to create work:

1. **Watched Gerrit patch.** Patch Watcher observes a change, applies its
   configured policy, and may start a revision-pinned run to investigate or
   advance that patch. This remains the primary workflow covered by the phased
   plan below.
2. **Jira-ticket engineering.** A person supplies a Jira issue key. The system
   retrieves and snapshots the ticket, establishes the relevant project,
   repositories, source baselines, acceptance criteria, and permitted actions,
   and starts an engineering run whose goal is the ticket rather than an
   already-existing Gerrit change. That run may eventually create one or more
   patches, so its identity and lifecycle cannot be modeled as merely another
   watched-patch run.
3. **Free-form engineering.** A person supplies an arbitrary prompt plus
   explicit project/repository and environment context. This is a separate,
   more general entry point and must not be smuggled into the patch
   investigation text box. It needs its own validation, provenance,
   permissions, budgets, and approval behavior.

The dashboard should eventually put the second and third modes on a separate
**Engineering work** page or clearly separate section, not in each watched
patch row. That surface can offer **Start from Jira ticket** and **Start from
prompt** while reusing the same durable run, session, message, resource,
timeout, human-intervention, and audit machinery.

This is a design direction only. Neither new entry point exists, and the
current **Investigate** action must remain narrowly pinned to an existing
Gerrit revision. Jira content and a user-authored prompt are task inputs, not
authority: they must not be able to widen what a run does. That is harder to
guarantee than it was when this was written, because an engineering run's
capability is no longer a controller-issued envelope -- it is the operator's
own account, and the only thing shaping the run is the prompt the controller
composes. A free-form entry point would put attacker-influenced text into that
prompt, which is why it stays disabled.

## Non-goals for the first executable phases

- A general distributed workflow engine.
- Multiple active agents on one patch.
- Gerrit votes, abandons, or writes to a change other than the pinned one.
- JIRA writes, and Jenkins builds, retriggers, or cancels.
- Treating free-form agent text as authorization for a *controller* action.
- Treating a browser page load as the scheduler.

Two entries that used to sit in this list have moved out of it, and the change
is the single most important thing to know about the current system:

- **Source modification and patch upload are no longer non-goals.** A
  review-handling or build-repair run edits the pinned revision's source and
  pushes the resulting patchset itself. See *Core invariants* 5 and 11.
- **Untrusted build and test commands do run on the Patch Watcher host.** The
  agent has a host shell and passwordless sudo, and `ltvm build` runs on the
  host by design. Only the code under test executes in a guest. The host and
  the Patch Watcher web service are the same machine.

Everything in the first list is a *prompt* rule, not an enforced one, except
the last: the browser genuinely cannot schedule work, because the observer runs
on its own thread. The others are sentences in `ENVIRONMENT_POLICY`
(`run_controller.py`) that an engineering agent is asked to obey and is capable
of breaking.

## Design review: gaps in the current prototype

The current prototype is intentionally small, but these gaps must be closed
before automation is enabled:

1. The watch list and history are process memory. A restart loses them.
2. Refresh is driven by a browser meta-refresh. Polling and automation must run
   even when no browser is open.
3. Patch observation, automation policy, trigger, agent run, Claude turn, and
   external action are not separate concepts yet. Combining them produces
   misleading states such as “idle but working” or “failed but waiting.”
4. There is no transaction that prevents duplicate runs or duplicate retest
   requests when two refreshes observe the same state.
5. There is no exact patchset/SHA pin or final stale-patchset check before a
   side effect.
6. Claude Voice Control has useful process/session patterns, but making its CLI
   and registry a Patch Watcher dependency would couple two different products.
7. `WORKER_STATUS` text markers are useful for compatibility, but they are too
   weak to be the only agent protocol.
8. Current checkboxes do not define trigger mode, capability scope, budgets,
   approval rules, or what a policy edit does to an active run.
9. LTVM work needs explicit VM and checkout ownership, cleanup, and artifact
   rules.
10. Gerrit comments, source, CI logs, and JIRA text are untrusted inputs. They
    can contain prompt-injection text and must never redefine worker authority.
11. Crash recovery and ambiguous external-call recovery are unspecified.
12. The dashboard does not yet have a run detail view, conversation, pending
    question, delivery state, or operator controls.
13. The worker environment is still implicitly Patrick's Mac account: tools,
    versions, paths, instructions, credentials, and host services are not an
    attestable portable contract.

The phased plan below addresses these before progressively enabling more
powerful actions.

Gap 13 was closed once, by the worker-environment contract, and then reopened
deliberately. The 2026-09-07/08 carve-down removed the attestable portable
contract and adopted the opposite answer: the worker environment *is* the
operator's own account, described by `CLAUDE.md`, and the tool's job is to help
set that account up correctly (`pw-configure`) and to say when it stops being
correct (`pw-doctor`). Portability is no longer claimed. The rest of the list
is closed as described in the phase notes below.

## Core invariants

These are implementation rules, not suggestions.

1. **One active run per Gerrit change.** The database enforces this. A second
   trigger is coalesced or supersedes the existing run through an explicit
   transition; it never silently starts another worker.
2. **Every run is revision-pinned.** It stores change number, patchset number,
   and revision SHA. Before every *controller-owned* external write, Patch
   Watcher refreshes and compares all three. An agent's own Gerrit writes are
   not covered by that guard: the agent is told which revision it is pinned to
   and is trusted to write only to it. A revision that moves during a run
   terminalizes that run as `stale`, which is what actually stops it.
3. **The database is authoritative.** Patch Watcher's Claude runner owns Claude
   process continuity; LTVM performs requested VM operations; neither decides
   Patch Watcher workflow state.
4. **Policy is snapshotted at run creation.** Editing patch policy affects the
   next run by default. The UI requires a separate explicit action to alter or
   cancel an active run.
5. **Deterministic Maloo writes are controller-owned; engineering writes are
   not.** The controller still owns the two remote writes it makes itself --
   `maloo retest` and `maloo link-bug` -- and executes each only after
   validating policy, revision, budget, and idempotency against a durable
   outbox. Everything else is now the agent's own. An engineering run
   (`engineering`, `review_comments`, or `build_failure`) is launched with
   `capability_profile="full"`. That means permission mode
   `bypassPermissions`, no `--tools` allowlist, no `--safe-mode` or
   `--restricted`, and the ambient environment inherited unchanged --
   including every service credential the operator has. That agent posts its own Gerrit replies and
   pushes its own patchsets with the `gerrit` CLI, and creates and destroys its
   own LTVM guests. The controller-owned Gerrit reply, patchset-upload, and
   Jenkins-retrigger writers (`gerrit_reply.py`, `gerrit_upload.py`,
   `jenkins_retrigger.py`) and their kill switches were deleted in the
   2026-09-07/08 carve-down; there is no capability grant, tool broker, or
   credential injection left between the agent and those services.

   What bounds an engineering agent is therefore its prompt, plus three
   structural facts: the checkout it is given, the `co<N>-` VM name prefix it
   is told to use (which is also the only thing terminal cleanup can find), and
   the launch-time refusal described in invariant 11. `ENVIRONMENT_POLICY` in
   `run_controller.py` is the prompt text, and it forbids Gerrit votes and
   abandons, any Gerrit write to a change other than the pinned one, all JIRA
   writes, all Jenkins writes, all Maloo writes, host package/service/`/etc`
   changes, and writes into the operator's own working trees. None of that is
   enforced. It is asked for, and an agent that ignores it succeeds.
6. **No *controller* action is inferred from prose.** Only a validated
   structured report can advance run state or cause a controller-owned write.
   This says nothing about what the agent does with its own hands, which is
   invariant 5's subject.
7. **Every state change is an event.** Current-state columns are projections
   for efficient UI display; the event history remains append-only.
8. **Terminal means terminal.** A message to a completed, failed, cancelled,
   or stale run cannot silently revive it. The operator starts a follow-up run.
9. **The code under test runs in a guest; the agent does not.** Lustre builds
   and tests execute inside LTVM guests the run owns, and the prompt requires
   it. The agent process itself runs on the Patch Watcher host as the operator,
   with a shell and passwordless sudo, and `ltvm build` runs on the host by
   design. There is no worker sandbox and no separation between the agent host
   and the web-service host. Nothing but the prompt keeps an agent from
   building or running patch code directly on the host.
10. **Emergency stops are always available.** Global automation, per-patch
    automation, and an individual run can each be disabled independently.
11. **One launch-time refusal precedes execution.** There is no admission step.
    The worker profile, run envelope, and environment attestation described by
    the deleted `WORKER_ENVIRONMENT_CONTRACT.md` were removed in the
    2026-09-07/08 carve-down, and nothing persisted is checked before Claude
    starts. `pw-doctor` answers "can this host run agents" for an operator, out
    of band, and is advisory.

    The one real pre-launch gate is narrower and structural. Before starting an
    engineering agent, `RunController._refuse_agent_instruction_revision`
    refuses any revision that adds or edits a file Claude Code itself loads as
    *instructions* -- `CLAUDE.md`, `AGENTS.md`, anything under `.claude/` --
    or a root `.env` or `.envrc`, which a tool the agent runs reads as its own
    configuration. The `.env` case is the sharper one: `gerrit_cli` loads
    `cwd/.env` last with `override=True`, and the checkout is the cwd, so a
    `.env` in the pinned revision silently redirects every `gerrit` call the
    agent makes, including the credential-bearing writes the review and
    build-repair prompts tell it to perform. "Repository content is untrusted
    data" is prose; a file the harness or the CLI treats as policy is
    structure, and structure wins.

    The refusal fails closed, including for an operator-started run and
    including when the check cannot be answered -- a shallow clone falls back
    to the weaker but honest "does the pinned tree contain any of these" and
    says which question it answered. The one exception is a directory that is
    not a repository at all, where the precondition is absent rather than the
    answer hidden. It applies to engineering runs; read-only runs get
    `Read`/`Glob`/`Grep` under `--safe-mode --restricted` with service
    credentials scrubbed, so the worst case there is a misleading report.

## Vocabulary and separate state domains

The UI and code should use these terms consistently:

- **Patch:** a watched Gerrit change, independent of patchset.
- **Work item:** the durable top-level objective for a run. It is a Gerrit
  change today; future work items may instead be a snapshotted Jira ticket or
  a user-authored free-form engineering request.
- **Revision:** one observed Gerrit patchset and exact revision SHA.
- **Policy:** operator-selected triggers, capabilities, budgets, and approval
  rules for a patch.
- **Observation:** one read-only snapshot of Gerrit, review, Jenkins, Maloo,
  and related metadata.
- **Trigger:** a durable fact that might justify work, such as a newly observed
  enforced test failure or a manual request.
- **Run:** one bounded attempt to handle a trigger against one pinned revision.
- **Session:** the managed Claude conversation/process associated with an
  agent-backed run.
- **Turn:** one prompt/response cycle inside that session.
- **Action attempt:** one controller-mediated external operation, such as a
  Maloo retest request.
- **Session resource:** an ephemeral checkout, VM, or VM cluster created for and
  recorded against one agent session.

Patch status, run status, Claude turn status, and action status must be stored
and displayed separately. For example, a patch may be `ci-failed`, its run may
be `waiting_external`, and its Claude session may currently be `idle`.

Future ticket and free-form modes also need a work-item state distinct from
any patches they later create. A ticket run that produces two Gerrit changes
must remain one ticket work item with two separately revision-pinned patch
outputs; it must not silently turn into either patch's watcher state.

## Initial architecture

Implement the boundaries below as Python modules, but deploy them as one
service plus subprocesses at first. This avoids premature microservices while
keeping later separation possible.

### Web UI and HTTP API

- Reads durable projections from the database.
- Validates operator commands and records them as commands/events.
- Never performs long-running Gerrit, agent, build, or VM work in a request.
- Uses CSRF protection and authenticated access before being exposed beyond
  localhost.

### Observer

- Polls watched changes on the configured schedule, independent of browsers.
- Calls read-only Gerrit, Jenkins, and Maloo adapters.
- Stores a normalized observation and a bounded reference to raw adapter data.
- Emits trigger candidates only when the normalized state meaningfully changes.

### Evaluator

- Applies `DESIGN_ACTION_FLOW.md` and the effective per-patch policy.
- Applies the non-Maloo Code-Review `-1` gate before the test flow.
- Suppresses, coalesces, or queues a trigger with a recorded reason.
- Does not call Claude for deterministic rules.

### Dispatcher and reconciler

- Claims the per-patch active-run slot transactionally. A SQL trigger on
  `pw_managed_session` aborts a second active session for the same patch.
- Creates runs and dispatches deterministic actions or agent work.
- Coalesces overlapping ticks with a non-blocking in-process lock:
  `BackgroundObserver.tick` and `RunController.tick` each take a
  `threading.Lock` and return immediately if another thread holds it.

  **There is no singleton dispatcher lock.** The renewable cross-process lock
  described here was never built, and nothing replaced it. Two `patch-watcher`
  processes started against the same databases would both observe, both
  evaluate, and both dispatch. The per-patch session trigger and the action
  outbox's idempotency keys are what stand between that and duplicate work;
  they are strong for the deterministic Maloo path and are the only protection
  anywhere else. Treat single-process operation as an operational requirement,
  not an enforced one.
- Periodically reconciles database state with Claude runner sessions, action
  adapters, checkouts, and the LTVM inventory.
- Detects stalled, orphaned, externally completed, and stale work.

### Runner adapters

- **Deterministic runner:** executes bounded controller workflows such as one
  Maloo retest request without starting Claude.
- **Native Claude runner:** starts, resumes, messages, interrupts, stops, and
  reads structured event streams for Claude sessions. It borrows proven ideas
  from Claude Voice Control without depending on that application.
- **Checkout adapter:** verifies and pins source checkouts. Two shapes: a
  numbered checkout claimed from `CheckoutPool` (the normal path, and the only
  one that yields a VM name prefix), or a private per-run clone under the run
  directory when no pool is declared.
- **Direct LTVM use:** the agent invokes ordinary `ltvm` commands from its own
  shell, choosing target, topology, and VM parameters itself. Patch Watcher
  neither brokers nor observes those commands. It finds the resulting VMs by
  the run's `co<N>-` name prefix and handles terminal cleanup. See *LTVM rules*
  for why the prefix, and not an owner token, is the ownership model.

### Tool adapters

Wrap `gc`, `maloo`, `jenkins`, `jira`, and later `janitor` using their JSON
output and documented exit codes. Each adapter returns a normalized typed
result while retaining a redacted raw-result reference for debugging. Do not
parse human-oriented terminal tables.

### Resource sampler

- Samples the worker host independently of the browser and records sample
  freshness and collection errors.
- Reads total, available, and used host memory plus swap and memory-pressure
  indicators from a supported OS API.
- Measures each live Claude process tree rather than only the parent process.
  Prefer proportional set size (PSS) where the OS exposes it; otherwise label
  resident set size (RSS) as an estimate that may double-count shared pages.
- Inventories all current LTVM VMs. For each VM, keep configured guest memory
  separate from the actual host memory used by its VM process; neither number
  is silently substituted for the other.
- Reads `owner_id` from `ltvm list --json` and associates a VM with a session
  when it carries that session's durable owner value. Nothing sets that value
  any more -- see *LTVM rules* -- so in practice every VM the sampler sees is
  unassociated, and the resource page groups guests under a session by the
  run's `co<N>-` name prefix instead. The parser is retained because LTVM still
  implements the owner contract and Patch Watcher may use it again.
- Never adopts an unmatched VM for cleanup. Legacy, operator-built, and
  externally owned VMs remain visible as unassociated resources.
- Samples on demand with a short cache (15 seconds) rather than on a schedule.
  Samples are not persisted: there is no `resource_sample` history and no
  retention policy. The dashboard never presents a sample without its
  timestamp, and a collection failure is shown as such.

## Persistence model

Use SQLite, with foreign keys enabled, WAL mode, transactions, and schema
migrations. A single local deployment does not need PostgreSQL, but the schema
must not depend on Python process memory.

State is split across **four** databases, not one, each owned by exactly one
module and independently migrated:

| Database | Owner | Holds |
| --- | --- | --- |
| `~/.local/state/patch-watcher/sessions.sqlite3` | `session_state.py` | managed sessions, events, messages, questions, runner handles, owned resources, reminder/delivery ledgers |
| `~/.local/state/patch-watcher/automation.sqlite3` | `automation_state.py` | patches, policies, observations, triggers, deterministic runs, the action outbox, settings and their audit, research policy and admission slots |
| `~/.local/state/patch-watcher/runs/engineering.sqlite3` | `engineering_state.py` | checkout allocations and events, execution manifests, artifacts, validation executions/attempts, capacity cooldowns and retry grants |
| `~/.local/state/patch-watcher/checkout-pool.sqlite3` | `workspace.py` | which run holds which numbered pool checkout |

They are separate files with no cross-database foreign keys, so consistency
between them is the controller's job, reconciled every tick rather than
enforced by the schema. A run therefore exists as a session row *and* a
checkout allocation *and* a pool claim, and terminal cleanup has to settle all
three. Selecting isolated databases for the first two is supported
(`--session-database`, `--automation-database`); the other two follow the runs
directory and the pool config.

Some non-database state is deliberately a file: standing policies
(`~/.config/patch-watcher/standing-policies.json`), autonomous-lane controls
(`autonomous-lanes.json`) and their append-only decision audit
(`autonomous-lanes.jsonl`), the watch list (`patches.txt`), and the structured
error log (`errors.jsonl`).

The table below is the logical entity model. Physical table names are
`pw_`-prefixed and do not correspond one-to-one; three entities in it are not
persisted at all, as noted after the table.

Minimum entities:

| Entity | Important fields |
| --- | --- |
| `patch` | id, Gerrit URL/change number/project, enabled, current lifecycle, created/updated |
| `patch_revision` | patch id, patchset, revision SHA, subject, owner, observed timestamps |
| `patch_policy` | version, triggers, capabilities, approvals, budgets, notification settings |
| `observation` | patch/revision, checked time, normalized review/CI state, source fingerprints |
| `trigger` | patch/revision, type, fingerprint, state, reason, first/last observed |
| `run` | patch/revision, trigger, policy snapshot, type, execution profile, effective timeout limits, state, summary/question/error, started/last-qualifying-activity/deadline timestamps, version |
| `agent_session` | run, runner/session id, worker host, process identity, state, started/last-event/ended timestamps |
| `run_event` | run, monotonic sequence, actor, type, structured payload, timestamp |
| `run_message` | run, author, body, urgency, delivery state, target question/turn, timestamps |
| `action_attempt` | run, action type, idempotency key, state, request/result, timestamps |
| `session_resource` | run/session, type, name, create request, environment, lifecycle state, last seen, cleanup result |
| `artifact` | run, kind, path/URI, content hash, size, description, retention state |
| `notification` | run/patch, type, destination, idempotency key, delivery state/result |
| `worker_host` | stable identity, display name, OS/architecture, total memory, last seen, sampler state/error |
| `resource_sample` | host/session/resource scope, measured time, CPU, RSS/PSS, configured guest memory, swap/pressure fields, quality/source |
| `service_cursor` | observer/reconciler/Claude-log cursors and last successful activity |

`worker_host`, `resource_sample`, and `service_cursor` are **not implemented**.
Host and VM resource figures are collected live and cached for 15 seconds, so
there is no sampled history to query and no persisted host identity; the
observer and reconciler re-derive their position from run and session state on
every tick rather than from a stored cursor. They are left in the table as the
shape a multi-host deployment would need, not as a description of what exists.

Required constraints include:

- one patch per Gerrit server and change number;
- one revision per patchset/SHA;
- one trigger per stable fingerprint;
- one non-terminal run per patch -- enforced by a SQL trigger on
  `pw_managed_session` that aborts a second active session, and by a partial
  unique index on the deterministic `pw_automation_run`;
- one open human question per session (`pw_one_open_question_per_session`);
- one action attempt per idempotency key;
- one live allocation per checkout path (`pw_checkout_active_path`);
- ordered, unique event sequence numbers per run.

There is no unique ownership record per VM name. VM ownership is a naming
convention checked at cleanup time, not a database constraint; see *LTVM
rules*.

Large Claude logs, build logs, and VM artifacts stay in private files; the
database stores metadata, bounded excerpts, hashes, and paths. Secrets and raw
credentials never enter events or artifacts.

## State machines

### Trigger states

`automation_state.TRIGGER_STATES` has four:

- `pending`: recorded and eligible, but not yet owned by a run.
- `claimed`: a run has taken it and is acting on it.
- `consumed`: the run that claimed it finished with it.
- `stale`: the revision or external condition moved before it was consumed.

The six-state model this section used to describe -- `candidate`, `queued`,
`coalesced`, `suppressed`, `obsolete`, `dispatched` -- was never implemented.
The distinctions it drew are still made, but elsewhere: "not eligible" and "why
not" are the *decision* recorded by the pure evaluator
(`retest_policy.evaluate_retests`, `standing_policy`) and shown in the
timeline, not a trigger state; and coalescing happens when a duplicate
fingerprint is recognized rather than by parking a trigger in a state.

A stable fingerprint includes the change number, revision SHA, trigger type,
and triggering external identifiers (for example Maloo session/test group).
Repeated polling therefore updates `last_observed` instead of creating new
work, and a manual button and an automatic observation of the same fact produce
the same coalescing identity.

### Run states

- `queued`: durable and eligible, but not allocated.
- `preparing`: starting the session and preparing its source checkout.
- `running`: controller or agent is actively performing a step.
- `waiting_external`: waiting for a known CI, timer, or other external result.
- `waiting_human`: paused on one explicit operator question or decision.
- `paused`: explicitly paused by an operator without an unanswered question.
- `blocked`: cannot proceed because of infrastructure, authentication, or an
  unmet dependency; operator intervention is required.
- `resource_exhausted`: terminal outcome for a run that could not obtain the
  local LTVM CPU, memory, disk, address, or other host capacity it requested.
  It schedules session-resource cleanup and a per-patch retry cooldown.
- `succeeded`: intended bounded outcome completed.
- `failed`: attempt ended unsuccessfully and is not automatically continuing.
- `cancelled`: operator stopped the run.
- `stale`: pinned Gerrit revision is no longer current.

These twelve are exactly `session_state.SESSION_STATES`. Transitions use
optimistic concurrency and are recorded as session events. `waiting_human`,
`waiting_external`, `paused`, and `blocked` keep the logical
one-active-run-per-patch claim.

A new patchset ends the run rather than flagging it. When a successful refresh
moves the revision, `reconcile_patch_revision` calls
`mark_stale_for_revision`, which terminalizes the session as `stale` with
failure code `patch_revision_changed` immediately, records the pinned and
observed revisions in the result, closes the guest capability, and releases the
checkout as soon as the worker settles. Terminal cleanup then stops a live
worker through the ordinary escalating-stop path.

The `superseded_revision` flag this section used to describe -- letting the
worker finish its bounded work against the old revision while holding the patch
claim -- was never implemented, and the current behavior is its opposite. That
matters in two directions. It is safer, because an agent that now performs its
own Gerrit writes must not keep working against a revision that has moved. It
is also more destructive: a long engineering run loses its work when a new
patchset lands, and the only record left is the terminal result and whatever
artifacts had been captured.

### Claude state mapping

The runner keeps process facts and workflow facts apart. A report's `state`
field, validated against the run's JSON schema, is what moves a run:

| Report state or runner observation | Patch Watcher interpretation |
| --- | --- |
| stream activity, no terminal report | session is active; run remains `running` and the inactivity clock resets |
| `needs_input` | validate the question and `ask_human`, which moves the run to `waiting_human`. The runner is *not* stopped: the session stays live for the answer |
| `complete` | stop the runner, settle the guest capability, check the report against the controller's own observation of the checkout, then `succeeded` |
| `resource_exhausted` | stop the runner, finish `resource_exhausted` with `ltvm_resource_exhausted`, alert once, clean up, and start the per-patch capacity cooldown |
| `failed` | stop the runner and finish `failed` with `worker_report_failed` |
| missing, malformed, or contradicted report | finish `failed` with `worker_report_invalid`; nothing is retried and the agent is not asked to correct it |
| failed/stopped process with no terminal report | reconcile as `failed` or `cancelled` according to cause, or adopt a healthy process after a restart |

`blocked` and `paused` are real session states, but no report produces them:
`paused` comes from an operator control, and `blocked` is currently only a
state the UI knows how to render. The four report states above are exactly the
enums the schemas accept.

Claude process state alone never marks a run successful.

### Action-attempt states

- `planned`, `executing`, `succeeded`, `failed`, `ambiguous`, or `cancelled`.

The controller writes `planned` and its idempotency key before invoking a tool.
If it crashes during the call, startup reconciliation treats it as `ambiguous`
and checks remote state before any retry. This is an outbox-style contract:
exactly-once delivery cannot be assumed from an HTTP or CLI call.

For example, a Maloo retest key may be:

```text
maloo-retest:<change>:<revision-sha>:<session-id>
```

## Trigger and dispatch algorithm

1. Observer stores a new normalized revision snapshot.
2. Evaluator applies the review gate and patch policy.
3. It creates or updates one fingerprinted trigger.
4. In one database transaction, dispatcher checks:
   - global automation enabled;
   - patch automation enabled;
   - trigger mode permits this event;
   - no non-terminal run owns the patch;
   - current revision still matches the trigger;
   - retry, runtime, turn, and action budgets remain.
5. Dispatcher snapshots policy, creates the run, claims the active-run slot, and
   marks the trigger `claimed`.
6. Runner executes one bounded step, records events/actions, and yields to the
   reconciler between steps.
7. Any newer revision immediately stales both the trigger and the run before
   another controller side effect can occur.

Manual triggers follow the same path and share the same coalescing identity, so
a manual button and an automatic observation of the same fact cannot start two
runs. They do not bypass revision, ownership, budget, or idempotency checks.

Step 7 bounds the *controller*. It does not bound an engineering agent already
running: the run is terminalized and its worker is stopped by cleanup, but a
write the agent was mid-way through issuing is not recalled.

## Policy and capabilities

The visible checkboxes are a friendly projection of a versioned policy, not
authorization by themselves. Each patch shows both inherited defaults and
effective overrides.

The per-patch standing policy (`standing_policy.py`) has four fields, all
persisted per Gerrit change and all defaulting to the safe value:

- `trigger_mode`: `manual` or `automatic`;
- `test_failures`: `off`, `deterministic`, or `investigate`;
- `build_failures`: `off` or `repair`;
- `review_comments`: `off`, `simple`, or `all`.

Automatic triggering additionally requires the independent global
automatic-execution gate, which is confirmed separately and defaults off, and
saving an automatic policy requires its own signed one-use confirmation naming
the exact capabilities it turns on.

The deterministic Maloo test policy carries the four operator-visible action
modes, and these are real (`automation_state.POLICY_MODES`):

- `disabled`: observe only; create no recommendation or run;
- `advise`: evaluate and display what would be done, but perform no action;
- `approval`: prepare the exact action and wait for an operator to approve it;
- `automatic`: execute when all policy and safety checks pass.

Research has its own independent three-mode policy -- `disabled`, `manual`,
`automatic` -- with a per-revision run budget. New and migrated patches default
to `disabled`/`off`/`manual`.

Model, effort, and execution profile are per-run choices rather than policy
fields, and the run detail page reports what the run actually used. Timeout
limits are code constants, not overridable per patch. The LTVM capacity
cooldown is a store-level configuration (15 minutes doubling to a 24-hour
maximum), not a policy field.

### There is no capability grant

This section previously defined a composable capability vocabulary --
`read_gerrit`, `request_maloo_retest`, `edit_source`, `start_ltvm`,
`post_gerrit_message`, `reply_review_comment`, `vote_gerrit`,
`upload_patchset`, `network_general` -- and asserted that "early agents receive
read capabilities only" while "the controller retains Gerrit, Maloo, Jenkins,
JIRA, and VM credentials and performs approved writes through adapters." None
of those capability names exists in the code, and the second sentence is now
false for every engineering run.

What exists is two capability *profiles*, chosen by request kind, not by
policy:

- `read_only`, for **Investigate** and unknown-failure research. Tools
  `Read`, `Glob`, `Grep`; the hardening flags `--safe-mode`, `--restricted`,
  `--strict-mcp-config`, and `--disable-slash-commands`; permission mode
  `dontAsk`; and an environment
  with every `GERRIT`/`MALOO`/`JENKINS`/`JIRA`/`JANITOR`/`GITHUB`/`GITLAB`
  variable and every `_TOKEN`/`_PASSWORD`/`_SECRET`/`_API_KEY` suffix removed,
  keeping only the model-auth keys. This profile really cannot write anywhere.
- `full`, for `engineering`, `review_comments`, and `build_failure`. No tool
  allowlist, no hardening flags, `--permission-mode bypassPermissions`, and the
  ambient environment inherited unchanged. This profile can do anything the
  operator can do.

There is nothing in between and nothing composable. A `source_edit` profile and
an MCP-brokered `source_edit_ltvm` profile are still accepted by
`ReadOnlyRunSpec`, but nothing constructs them: the LTVM MCP server they
depended on (`ltvm_mcp_server.py`) was deleted in the 2026-09-07/08 carve-down,
so `source_edit_ltvm` cannot start, and `source_edit` has no caller. Treat them
as vestigial.

One consequence deserves stating plainly: the LLM tools read their credentials
from files under `~/.config`, not from the environment, and `HOME` is never
overridden. Scrubbing the environment therefore does not disarm those tools --
what disarms them for a `read_only` run is that it has no `Bash` tool with
which to invoke them. Give that profile a shell and the credential scrubbing
buys almost nothing.

Prompt text is not defense in depth here; for a `full` run it is the only
defense. Enforcement is intended to arrive with containerization, and until
then the honest statement is the one the run detail page makes: the run has a
host shell, the installed LLM tools, and real service credentials, and makes
its own Gerrit and CI writes.

Enabling an automatic policy shows an explicit confirmation that spells out
exactly this, and applies to future runs. A global emergency stop prevents new
automatic dispatch without destroying evidence from existing runs.

## Exact revision and stale-work handling

At run creation, store:

- Gerrit server and change number;
- patchset number;
- revision commit SHA;
- subject/project/branch;
- relevant CI result identifiers;
- policy version and trigger fingerprint.

Before a controller-owned write -- in practice a Maloo retest or bug link --
re-fetch the current Gerrit revision and compare patchset and SHA. A mismatch:

1. records the revision change as a durable event;
2. prevents the action;
3. stales the trigger and terminalizes the active run as `stale` with failure
   code `patch_revision_changed`, recording both the pinned and the observed
   revision;
4. preserves the run's logs and artifacts;
5. stops the worker through the ordinary escalating-stop path, then cleans its
   checkout and prefix-matched VMs, unless an operator has explicitly retained
   a resource for debugging; and
6. lets the new revision create its own trigger and run once the old run
   releases the per-patch active-run claim.

Step 3 is the whole guard for an engineering run. Because that run performs its
own Gerrit writes, there is no pre-write revision check the controller can
interpose; what protects the newer patchset is that the older run is
terminalized and its worker stopped promptly after the revision is observed to
have moved. The window between the push and the next successful refresh is
real, and nothing closes it.

An operator may view or download stale artifacts, but cannot “resume anyway.”
They can start a new run whose prompt includes a bounded summary or selected
artifacts from the old run.

## Native Claude runner

Patch Watcher should own a small `ClaudeRunner` interface and a native
implementation over Claude Code's structured stream protocol. Claude Voice
Control is a useful reference for persistent conversations, structured event
capture, interruption, resumption, and separate turn/task state, but it is not
a runtime dependency and its registry/session model is not copied wholesale.

### Session ownership

- Create one managed Claude session per Patch Watcher run, named like
  `pw-68160-ps4-a1b2c3`.
- A session may have many turns and human messages during that run.
- A new Gerrit revision gets a new run and session. This prevents stale context
  from silently governing a new patchset.
- Store `run_id`, patch/change, patchset, SHA, policy version, capability
  profile, checkout path, and session-resource identifiers in Patch Watcher.
- Archive, rather than delete, terminal sessions after their retention policy
  permits it.

### API boundary

The `ClaudeRunner` interface provides:

- start/send/status/list/interrupt/stop/archive;
- JSON results and typed errors;
- an event cursor or bounded event tail;
- session/task/turn identifiers and timestamps.

The first implementation may borrow and simplify the cctty/streaming code from
Claude Voice Control, but it lives behind the Patch Watcher-owned interface.
If both applications later need the same stable implementation, extract a
small independent runner library rather than making either application depend
on the other or vendoring two drifting copies.

The runner remains the source of process facts, while the Patch Watcher
database owns workflow facts. On startup the reconciler compares both and
adopts, resumes, or marks sessions orphaned without silently duplicating them.

### Continuation behavior

A run is one prompt and one session. There is no controller-driven turn loop:
the agent works until it emits a terminal report or asks a question, and the
controller does not send it "the next bounded prompt". The only text Patch
Watcher injects after the initial prompt is operator guidance and the answer to
a `needs_input` question, both delivered through the durable outbound-guidance
ledger. The controller policy boundary that this section once placed between
every turn does not exist -- for a `full` run there is nothing to place it
around, because the agent is not asking permission for anything.

What still bounds the run is time (the profile deadlines), the terminal-report
contract, and operator controls.

### New patch activity while a session is running

New Gerrit, review, or CI observations are recorded and shown while a Claude
session is running, but they are not injected into that session. Patch Watcher
does not interrupt, restart, or redirect the session in response to ordinary
new activity, and the worker continues against the revision and evidence
snapshot it started with.

A newer patchset is the exception, and it is decisive rather than advisory: the
run is terminalized `stale` at once and its worker stopped, as described under
*Run states*. There is no **Superseded revision** flag and no grace period in
which the worker finishes its current work; that design was never built. After
the run terminalizes, the evaluator considers the newest observation and may
create a new run. Patch Watcher still never tries to merge new context into a
live engineering session.

### Worker report protocol

There is exactly one terminal report per run, validated by Claude Code against
a `--json-schema` the controller passes at launch. Three schemas exist in
`claude_runner.py`, one per report kind, and all three set
`"additionalProperties": false` -- so an extra field is a rejection, not a
warning:

- `patch-watcher-read-only-report/v1`: `schema`, `state`, `summary`,
  `findings`, optional `question`. Required: all but `question`.
- `patch-watcher-unknown-failure-report/v1`: adds `recommendation` (one of
  `known_failure`, `transient`, `patch_caused`, `needs_human`, `inconclusive`)
  and `evidence_references`, each a `{evidence_ref, locator, supports}` triple
  citing a `record:`/`artifact:` identifier the controller actually captured.
- `patch-watcher-engineering-report/v1`: `schema`, `state`, `summary`,
  `changed_files`, `validation_requests`, plus the review fields
  (`review_mode`, `review_snapshot_sha256`, `comment_results`) and build fields
  (`jenkins_snapshot_sha256`, `jenkins_resolution`) used by those two flows.

`state` is `complete`, `needs_input`, or `failed` in all three, and
`resource_exhausted` additionally in the engineering schema. `needs_input`
requires `question`.

The `patch-watcher-worker/v1` envelope this section used to specify -- with
`run_id`, `current_step`, `artifacts`, a structured `error`, and
`requested_actions` -- never existed, and a report written to it would now be
**rejected**, both because those fields are not in any schema and because
`additionalProperties` is false. `requested_actions` in particular has no
counterpart: there is no controller action for an agent to request any more.
The `WORKER_STATUS` text marker is likewise gone.

Validation is strict and terminal. A missing report, one that fails the schema,
or one the controller can contradict from its own observation of the checkout
finishes the run `worker_report_invalid`: the runner is stopped, nothing is
retried, and the agent is not asked to correct it. The prompt says so in as
many words, because the alternative is losing hours of real VM work to a field
the agent never knew was cross-checked. Text and tool-stream events remain
available as activity and logs.

## Agent execution profiles and timeouts

Agent-backed work uses one of two explicit execution profiles. The profile and
its effective limits are snapshotted into the run so later configuration
changes cannot silently change an active session.

### Triage profile

Use `triage` for short sessions that inspect captured evidence and pinned
source and then report. A triage run must not create LTVM VMs, and in practice
it holds the `read_only` capability profile, so it cannot: it has no shell. Its
default maximum wall-clock runtime is
20 minutes, measured from successful Claude process start. Reaching that
deadline fails the run with `agent_runtime_timeout`; activity does not extend
the deadline, and the prompt tells the agent so in as many words so that it
scopes the work to fit. Operator extension of the deadline is not implemented;
the only recovery is a new run.

### Engineering profile

Use `engineering` for debugging, patch development, builds, tests, and other
work that may create LTVM VMs. These runs do not have a short wall-clock limit.
Instead, the default failure threshold is 30 minutes without qualifying
activity while the run is `preparing` or `running`. Reaching the threshold
fails the run with `agent_inactivity_timeout`.

Qualifying activity is **any event on the run's own Claude stream**: a tool
call, a tool result, or a line of the agent's own text. The rule is
deliberately that broad. Refreshing the clock only on assistant *text* timed
out healthy runs thirty minutes into their first long command, because an agent
inside one `ltvm build lustre` or `auster` call emits a tool-use block and then
nothing for far longer than that.

The richer signals this section once listed -- output from an owned command,
changing CPU or I/O counters for a long-running command, a state transition on
a session-owned VM -- are not used. The controller no longer brokers guest
commands, so it does not see them; it sees the agent's stream and nothing
else. A dashboard refresh, an observer poll, and activity from an unrelated
process do not reset the clock, because none of them appears on that stream.
The residual hazard is stated in the prompt rather than engineered around: a
long silent stretch of the agent's own thinking, with no tool call, is
indistinguishable from a hang and will be killed.

The inactivity clock runs only in `preparing` and `running`. It is suspended in
`waiting_human`, `waiting_external`, `paused`, and `blocked`, because those
states already identify why progress is intentionally stopped. Resumption
starts a fresh inactivity interval and records that transition.

### Long-running notices and absolute cap

Every agent session also has a nonextendable absolute wall-clock cap of 48
hours. Triage sessions ordinarily hit their 20-minute limit first. A
non-terminal engineering session may continue as long as it is making
progress, but Patch Watcher sends a status email when its wall-clock age
reaches two hours and every two hours thereafter, including while it is in a
waiting state. Each reminder has an interval-derived idempotency key, and a
service restart must not resend an interval already recorded as delivered.

The reminder is informational: it does not pause the worker or reset any
clock. It includes the patch and run, elapsed time, current step, last
qualifying activity, bounded excerpts of the most recent session messages,
current resource use, owned VMs, and an authenticated **Kill session** link.
The link opens the run page with the destructive action ready for explicit
confirmation; an email GET request must never kill a process by itself.

At 48 hours Patch Watcher fails the run with `agent_absolute_runtime_cap` and
uses the same stop, artifact collection, notification, and owner-scoped cleanup
path as other timeouts. The cap cannot be extended from the dashboard.

### Timeout response

When either timeout fires, Patch Watcher must:

1. transactionally re-read the run version, state, deadline, and last
   qualifying activity so activity racing with the timeout wins; if the
   deadline is still expired, atomically mark the run `failed` with the exact
   timeout code, configured limit, start time, and last activity time;
2. interrupt Claude and, after a bounded grace period, stop the process if it
   has not exited;
3. collect available logs and artifacts before terminal cleanup;
4. purge only the checkout and LTVM resources owned by that run/session;
5. send one immediate idempotent email with the patch, run, timeout reason,
   last activity, cleanup state, and dashboard link; and
6. show **Failed — 20-minute runtime limit**, **Failed — inactive for 30
   minutes**, or **Failed — 48-hour absolute limit** on the patch and run
   pages.

The dashboard shows the profile, start time, last qualifying activity, current
step, and remaining runtime or inactivity time for every active agent run. A
timeout never silently restarts or resumes a session; retry creates a new run.

## Human messaging and control

The patch detail page contains a run conversation and a message composer. A
message is first committed durably; delivery is asynchronous through the
outbound-guidance ledger, whose states are `pending`, `delivered`, and
`failed`, each with a unique idempotency key and a recorded claimant. There is
no `acknowledged` state: the ledger knows the message was handed to the runner,
not that the agent read it, and the timeline shows when the agent next produced
activity instead.

### Message behavior by run state

- **Running:** default delivery waits for the current turn boundary, avoiding
  accidental interruption during a command. The operator may explicitly choose
  **Interrupt and send**, which records the interrupt and then delivers after
  the runner confirms the turn stopped.
- **Waiting for human:** the response targets the displayed question ID. On
  successful delivery the run returns to `queued`/`running`.
- **Waiting external:** a message may be queued, but does not automatically
  cancel the wait unless the operator also chooses resume/change course.
- **Paused:** a message is stored; **Resume with this message** is a separate
  explicit choice.
- **Blocked:** a message may explain a repaired dependency, but the controller
  rechecks the dependency before resuming.
- **Terminal or stale:** the composer offers a follow-up. It never resumes the
  old run silently, and the follow-up is always a *read-only investigation*
  pinned to the patch's current revision, whatever kind the original run was --
  the control says so, because labelling it "start follow-up run" on a failed
  review or build-repair run promised another run of that kind.

`waiting_external` and `blocked` are reachable states for a deterministic
automation run, which has no Claude session and therefore no composer. A
managed agent session in practice only reaches `waiting_human`, `paused`, and
the terminal states, so those two bullets describe controls the UI offers
rather than paths an agent run is currently observed to take.

Each submission includes the expected run version and optional question ID so
an answer cannot accidentally target a superseded state. Duplicate browser
submissions use an idempotency token. The timeline shows author, delivery
state, target turn/question, and when Claude next produced activity.

The active-run card exposes **Send guidance** as the operator's deliberate
“prod.” It can clarify or change the requested approach after the operator has
reviewed recent activity. By default it queues guidance for the next safe turn
boundary; **Interrupt and send** is a separate, more disruptive choice. This
human control is distinct from patch observation: Patch Watcher never injects
new Gerrit or CI activity into a running session automatically.

Operator run controls, all POST-only behind a display-only confirmation page
where they are destructive:

- **Pause after current step** (`queued`, `preparing`, `running`,
  `waiting_external`, `blocked`);
- **Interrupt turn** (`preparing`, `running`);
- **Resume** (`paused`, `waiting_external`, `blocked`). Deliberately not
  offered in `waiting_human`: that state resumes only through a question-bound
  answer submitted by the guidance composer, and a generic resume would bypass
  that contract;
- **Stop and cancel** and **Kill session** while active, both behind a
  confirmation. Kill stops the Claude process, records who requested it, marks
  the run cancelled, and begins artifact collection and cleanup;
- **Retry as a new run** / **Start follow-up investigation** when terminal;
- **Open bounded/raw log** and artifacts.

**Supersede with a new run** and **Archive session** are not implemented. A new
patchset supersedes by staling the old run, and terminal sessions are retained
rather than archived.

Interrupting one Claude turn does not destroy its checkout or VMs. Once the
session/run becomes terminal, Patch Watcher automatically collects configured
artifacts and schedules all resources created by that session for purge. An
operator may explicitly retain a resource for debugging; retained resources
remain prominent until their retention ends or the operator purges them.

## Waiting for human contract

`waiting_human` is a resumable paused state, not success or failure.

**One open question per session is enforced, not merely asked for.**
`pw_one_open_question_per_session` is a partial unique index over open
questions, and a second `ask_human` on a session that already has one is
rejected. A question given to a terminal session is rejected too. Each question
carries a stable ID that the answer must target, so an answer cannot land on a
superseded question.

The rest of the entering contract is prompt-level, and the schema shapes what
can be carried: `question` is one bounded string, and the prompt asks for "one
precise question" and for `needs_input` rather than `complete` whenever any
target still needs a human. Structured choices, a recommended default, and a
per-answer consequence map have no place in the schema to live; the context a
reviewer needs comes from the run's timeline, the report's summary, and the
captured artifacts alongside the question, not from fields inside it.

The runner is not stopped while a question is open -- the session stays live to
receive the answer -- and no new prompt is sent until one arrives. Repeated
triggers are coalesced. On reply, the answer is delivered through the durable
guidance ledger. A new patchset stales the run before an answer can revive it,
and the operator starts a fresh run instead.

`blocked` is reserved for a broken dependency or environment (authentication,
missing target, unavailable service, lost VM) rather than a judgment question.
The UI must not blur these two states. No report state currently produces
`blocked`, so it is at present a state the UI can render rather than one a run
reaches.

## Deterministic retest workflow

Automatic retesting is best implemented as a deterministic controller run,
not a Claude task:

1. Apply the current-patchset non-Maloo review `-1` gate.
2. Read enforced Maloo failures and current retest state.
3. If a matching retest is already pending, enter `waiting_external` and do
   not request another.
4. If a failed suite has the required linked bug and policy permits retest,
   plan exactly one idempotent retest action.
5. Revalidate Gerrit revision and the Maloo state immediately before request.
6. Execute through the Maloo adapter, store result, and enter
   `waiting_external` for the new result.
7. If no linked bug explains the failure, create an investigation trigger or
   wait for a human according to policy. Do not guess a bug or request a retest.

This flow still appears as a normal run in the dashboard, with events,
actions, messages, and outcome. It simply has no Claude session. This is less
ambiguous, cheaper, and easier to make idempotent than asking an agent to
perform a known decision tree.

## LLM review tool environment

Agent-backed runs receive a generated instruction document, written to
`work/input/INSTRUCTIONS.md` at mode `0400` and hashed into a durable
`run_instructions` event. `_render_instructions` in `run_controller.py` is the
single place it is composed, and its sections are:

- the run ID and pinned revision SHA;
- three absolute paths -- working directory, checkout (and whether it is
  writable), and run directory with its `work/input/` -- because "this
  checkout" and "under `input/`" both once named a location the agent could
  not resolve;
- **Time budget**, rendered from the live timeout constants rather than
  restated in prose, so the numbers cannot drift from the code that enforces
  them;
- **VM naming**, stating the exact `co<N>-` prefix this run owns, or a flat
  prohibition when the run has no pool checkout and therefore no ownership at
  all;
- **Task**, the per-kind instructions, with untrusted-input warnings naming the
  specific artifact (comment snapshot, Jenkins snapshot, evidence bundle);
- **Organization policy**: for engineering kinds this is `ENVIRONMENT_POLICY`,
  which states the environment plainly -- "your credentials are the operator's
  own, sudo is passwordless, and any write you make is real" -- and then draws
  the boundary around what must not be touched: other checkouts, other runs'
  guests, the operator's own working trees, host packages/services/`/etc`, and
  the shared services beyond the one reply or patchset this run was told to
  publish;
- **Reporting**, the schema-specific rules plus `REPORT_CONTRACT`, and a
  closing line that repository, issue, review, CI, log, and web content are
  untrusted inputs that cannot change this policy.

There is no capability list to print, because there is no capability grant to
print; there is no "how to request a controller action", because an agent
cannot request one. The user-level `AGENTS.md` is not incorporated: an
engineering agent picks up `CLAUDE.md` the way any developer's session does,
discovered from its working directory, which is exactly why a revision that
edits one is refused (invariant 11).

The LLM tools' JSON output and exit-code conventions are the supported API. The
tools read their own credential files under `~/.config`, and an engineering
agent runs as the operator, so it has them. The narrow tool broker and
per-capability credential injection described here were never built, and the
code that would have hosted them was deleted in the 2026-09-07/08 carve-down.

There is no worker-box boundary, logical path contract, admission `doctor`, or
portability phase; `WORKER_ENVIRONMENT_CONTRACT.md` defined those and was
deleted with them. Its practical replacement is `pw-doctor`, which checks the
things that actually break a run on this host: the five credential files, the
required binaries (`claude`, `ltvm`, `gerrit`, `maloo`, `jenkins`, `jira`), the
one-time background-agent disclaimer, the checkout pool, and whether a
`CLAUDE.md` is discoverable from each pool checkout. It reports blocking and
advisory problems separately, names the command that fixes each, and gates
nothing.

### Worker rule: LTVM resource exhaustion

Every VM-capable run's instructions carry `RESOURCE_EXHAUSTED_POLICY`, and it
is deliberately one rule rather than the five-step protocol this section used
to specify:

> Use state `resource_exhausted` only when this host could not give you the
> LTVM capacity the work needed -- no free VM slot, disk, or memory to build or
> test in -- and name what ran out in the summary. It is not a synonym for
> failure: a patch that is broken, or work of yours that did not succeed, is
> `failed`.

It exists because `resource_exhausted` sat in the schema as a bare enum value
next to `failed`, with nothing telling the agent which was which, while the
controller treats them very differently. There is no separate error-code field,
no structured requested-topology field, and no list of already-created resource
identifiers: the schema has no place to put them, and the controller finds this
run's guests by name prefix anyway. `blocked` is not a report state, so the
old rule 5 has no counterpart -- a non-capacity creation failure is `failed`,
or `needs_input` if a human could unblock it.

On a validated `resource_exhausted` report, Patch Watcher:

- records a first-class `resource_exhausted` run outcome and timeline event;
- stops that agent attempt and collects its diagnostics;
- purges the run's prefix-matched VM/cluster resources, minus the pre-run
  baseline, so the failed attempt does not worsen capacity pressure;
- sends one immediate deduplicated status alert with the patch, run, evidence,
  cleanup state, and dashboard link;
- records a per-patch capacity cooldown: 15 minutes for the first exhaustion,
  doubling with each consecutive one, capped at 24 hours.
  `claim_validation_attempt` refuses to start another VM-capable run for that
  patch while the cooldown is live, and a later success clears the consecutive
  count; and
- continues ordinary read-only patch observation during the cooldown.

The dashboard shows the cooldown state, retry-not-before time, remaining time,
and consecutive-exhaustion count, and a `resource_exhausted` run offers
**Retry now as a new run** behind a confirmation. That control starts a new
run; it does not override the cooldown, so a retry inside the window is
refused with the cooldown as its reason. The one-use retry grant that was meant
to override it is unused: `claim_validation_attempt` still accepts one, but
nothing ever issued a grant and a fresh database no longer creates
`pw_validation_retry_grant` at all. **Extend cooldown** and **disable VM
automation** do not exist. Repeated exhaustion does not silently loop; the
global LTVM-capacity warning is not implemented.

## Full checkouts and ephemeral LTVM resources

An agent that edits or executes code needs a resource manifest stored with the
run.

### Checkout rules

- Give the run a full checkout at the pinned revision. Do not use Git worktrees
  for Lustre builds: generated configuration, staging, modules, and other
  source-adjacent state make independent checkouts the safer boundary.
- Prefer a numbered checkout claimed from `CheckoutPool` over cloning per run.
  A Lustre tree is expensive to create and its build state is worth keeping, so
  the pool declares a fixed set of indices in
  `~/.config/patch-watcher/checkout-pool.json` and hands one to a run at a
  time, recorded in `checkout-pool.sqlite3`. Membership is explicit, never
  "every numbered directory under the root": an agent resets and cleans its
  checkout before every run, so a checkout the operator works in must not be
  listed. An undeclared pool is empty, and a run then falls back to a private
  per-run clone under its run directory.
- The pool index is the run's VM ownership. Only a pooled run gets a `co<N>-`
  prefix; a fallback clone gets none, and the prompt then forbids guests
  outright, because a guest created without a prefix cannot be attributed or
  cleaned up and leaks permanently.
- Record repository remote, base branch, revision SHA, path, and initial dirty
  state.
- Never reuse a dirty checkout across runs; a pooled checkout is reset and
  cleaned before it is handed out.
- Release a pool checkout back to the pool on terminal cleanup -- never delete
  it. A per-run clone is removed. Both paths must run, or the pool silently
  shrinks by one tree per leak.
- The controller derives the run's diff itself, from the checkout, after the
  terminal report arrives. The agent is therefore told to leave its work
  uncommitted with respect to HEAD at report time, and to keep scratch files in
  `/tmp`: everything left in the checkout that is not `ltvm build` output is
  captured as part of the diff, and `changed_files` is cross-checked against
  it.

### LTVM rules

VMs are not drawn from a pre-existing pool. The agent decides whether it needs
one VM or a cluster and chooses the target, architecture, topology, memory,
disks, and other LTVM arguments appropriate to the task. It creates those VMs
on demand and they are disposable session resources.

**Ownership is by VM name prefix, not by an owner token.** A run holding pool
checkout N owns the name space `coN-`, and its prompt tells it to name every
guest `coN-<role>` -- which is already the mandatory convention in `CLAUDE.md`.
Claude then invokes ordinary commands, without Patch Watcher choosing their
substantive arguments:

```bash
ltvm create co3-sanity ...
sudo ltvm cluster create co3 mgs+mds:co3-mds:1 oss:co3-oss:3
```

The trailing dash is load-bearing: without it checkout 3 would claim
`co31-sanity`, which belongs to checkout 31. A cluster's own name (`co3`) and
its members (`co3-<role>`) are both inside the namespace.

Naming is the whole ownership model, with one safeguard against its weakness.
Because a prefix cannot distinguish this run's `co3-sanity` from one the
operator built by hand an hour ago, the controller records a **durable
baseline** of the VMs already carrying the prefix before the run starts;
terminal cleanup destroys prefix-matched guests minus that baseline. If the
baseline cannot be proved -- the inventory was unreadable at the wrong moment
-- that is recorded too, and "I could not see what was already here" never
widens what cleanup destroys.

Patch Watcher previously launched Claude with a durable opaque value such as
`patch-watcher:<session-id>` in `LTVM_OWNER_ID`, and LTVM resolved ownership
once per create operation from an explicit `--owner`, that variable, or a
`pid:` fallback, persisting it on the VM and every cluster member. **LTVM still
implements that contract; Patch Watcher no longer uses it.** `LTVM_OWNER_ID`
appears in zero lines of Patch Watcher code -- the deleted LTVM broker was the
thing that set it -- so `ltvm list --json` reports `owner_id: null` for every
guest an agent creates. `owner_id_for_session` and the inventory parser that
reads `owner_id` are retained: they still label Patch Watcher's *own* durable
resource records, and they would work again if the variable were set.

The prefix is weaker than the token it replaced. It is a convention the agent
is asked to follow, it collides with an operator's own `co<N>-` guests except
for the baseline, and a guest the agent names anything else is invisible to
cleanup and leaks. That is the trade the pool model made in exchange for
dropping the broker, and it should be re-examined before autonomous VM work
runs unattended.

This preserves agent autonomy: Claude invokes normal LTVM commands and chooses
their substantive arguments.

The session-resource lifecycle is:

- `creating`: the agent requested creation but it has not been confirmed;
- `active`: LTVM reports the VM with this session's owner identifier;
- `cleanup_pending`: the session is terminal and purge is queued;
- `destroying`: Patch Watcher issued the exact destroy operation;
- `destroyed`: LTVM confirms the VM no longer exists;
- `cleanup_failed`: destruction failed and will be retried/escalated;
- `retained`: an operator explicitly preserved it for bounded debugging;
- `orphaned`: ownership exists but Patch Watcher cannot match a healthy session.

Operational rules:

- The reconciler queries LTVM's machine-readable inventory and associates VMs
  with a run by the run's `co<N>-` name prefix, minus the recorded pre-run
  baseline.
- Use descriptive roles after the prefix (`co3-sanity`, `co3-mds`) as a human
  aid; the prefix carries the attribution.
- Check published targets before building, validate the target against the
  pinned Lustre checkout, and default to 2 GiB unless the task needs more.
- Record target, architecture, kernel, page size when known, variant, topology,
  vCPU, memory, disks, create command/result, and deployment revision.
- Capture commands, exit status, bounded output, console logs, and result
  artifacts.
- On `succeeded`, `failed`, `resource_exhausted`, `cancelled`, or `stale`, stop
  a live worker first, then collect configured artifacts, then purge the run's
  prefix-matched VMs and clusters. Cleanup is a durable, retried finalization
  step; the run remains visibly cleaning until LTVM confirms removal, and a
  resource nothing can even plan an action for is explicitly abandoned rather
  than left pinning the run forever.
- `waiting_human`, `waiting_external`, `paused`, and recoverable `blocked`
  sessions retain their VMs because the session is not finished.
- Retention is an explicit operator exception with a visible expiry. After the
  retention period, the exact owner-recorded resources return to cleanup.
- A global resources page shows every Patch Watcher-owned VM, session, state,
  age, last-seen time, and cleanup error.
- Never destroy a VM outside the run's prefix, and never one recorded in its
  pre-run baseline. Surface unmatched resources for human reconciliation
  instead.

LTVM isolates the code being tested. It does not isolate the Claude process or
its host credentials, so it is not a replacement for worker containerization.

## Worker isolation and network roadmap

**Nothing in this section is implemented.** It is the plan, kept because it is
still the plan, and it must not be read as a description of a control that
exists.

The current position is the opposite of the one this section originally
assumed. Read-only runs execute on the host and are labeled as read-only,
unsandboxed host workers; that label is accurate, and their tool set really is
`Read`/`Glob`/`Grep` with service credentials scrubbed. But general
source-editing runs arrived *before* isolation rather than after it: an
engineering run has a host shell, passwordless sudo, the operator's real
credentials, and permission to build untrusted patch code. Its run page says
so. The entry criterion this section defined -- isolation before broad code
execution -- was not met; it was set aside, deliberately, on the judgement that
a single-operator tool on a personal host can carry that risk while the rest of
the system is built.

The `Workspace` protocol that was meant to be the seam a container backend
slotted into has been removed, because nothing ever went through it: every
process launch goes through `claude_runner`. When containerization happens the
seam belongs where processes are actually created --
`build_read_only_claude_command` plus the spawn in `ClaudeHost` -- with
`workspace.py` supplying the directory to mount.

Before autonomous operation, add worker isolation:

- rootless container, non-root user, read-only base image;
- only the run checkout and per-run scratch directory mounted writable;
- no host home directory, SSH agent, Docker/Podman socket, runner socket, or broad
  credential directory mounted;
- CPU, memory, process, disk, runtime, and output limits;
- access to host LTVM through a narrow container bridge that preserves the
  agent-selected arguments and injects the session owner; controller-mediated
  external review/CI write actions;
- per-capability credential injection with automatic expiry/cleanup;
- explicit network profile recorded in policy and run history.

Network profiles should eventually include:

- `host-unrestricted` (the current mode for every run, and the only one);
- `container-standard` (normal outbound access, still isolated from host);
- `container-restricted` (egress allowlist for the model endpoint and local
  controller/tool broker; no arbitrary direct internet);
- `container-offline-tools` (tool execution has no network; the host broker
  supplies prefetched inputs and mediates model transport/actions).

A Claude Code process normally needs access to its model service, so “no
internet” must be implemented either as allowlisted model-only egress or by
separating the model transport from a no-network tool sandbox. The UI should
not claim full network denial while silently allowing general egress.

One place in the engineering run projection still reports
`isolation_profile: session-owned-ltvm` and a `network_profile` of
`controller-mediated`. Both are stale strings left over from the broker, and
neither is true: there is no isolation profile and no controller mediation.
Treat them as a defect to remove, not as documentation.

Containerization is a later track. It was an entry criterion before broad code
execution and it did not hold; it remains the entry criterion before autonomous
VM-backed or write-capable lanes, which is why the first autonomous lane grants
zero agent runs.

## Dashboard information architecture

### Global overview

The top of the page begins with a worker-host memory summary. For the initial
single-host deployment it shows host name, sample time, physical total,
available, used, cache/reclaimable memory when the OS reports it, swap use,
memory pressure, and the amount attributable to active Claude process trees and
LTVM VM processes. Configured guest RAM is shown alongside, but not added to
physical usage. If values cannot be measured or reconciled, display **unknown**
or **estimated** instead of manufacturing a total. The summary becomes warning
or critical at configurable available-memory/pressure thresholds.

The remainder of the overview shows:

- observer/scheduler health and last successful poll;
- global automation state and emergency stop;
- counts of queued, running, waiting-human, waiting-external, blocked, failed,
  resource-exhausted, and stale runs;
- available/busy Claude slots plus active, retained, orphaned, and
  cleanup-failed LTVM resources;
- recent errors and unsent notifications;
- default policy. There is no isolation mode to show: every run is an
  unsandboxed host worker, and the run detail page states each run's actual
  capability instead.

### Sessions and LTVM resources

The overview includes a collapsible active-sessions section and a link to a
full resources page:

- One row per current Claude session: patch, run, profile, state, elapsed time,
  last qualifying activity, most recent message summary, Claude process-tree
  memory, and current step.
- Expanding a session shows a bounded tail of recent messages/events, the full
  timeout countdowns, **Send guidance**, **Interrupt and send**, and **Kill
  session** controls.
- VMs matching a session's `co<N>-` prefix are nested under it. Each shows
  name, topology/role, state, age, configured guest memory, measured host
  memory when available, CPU use, and cleanup state. Running VM memory is
  attributed only after the QEMU process and the exact VM name both agree.
- A separate **Other LTVM VMs** group shows every currently inventoried VM that
  matches no active run's prefix, including stopped, legacy, and
  operator-built guests. These are observable but are never destroyed by Patch
  Watcher's automatic cleanup.
- Group and page totals avoid double counting. Claude process-tree memory and
  VM-process memory are separate components; host used/available memory remains
  the authoritative capacity view.

Every metric includes its sample age. A collection failure leaves the last
sample visible but clearly stale, logs the error, and prevents the inactivity
detector from treating missing telemetry as positive activity.

### Patch table

Keep the compact patch/review/CI presentation, and add:

- effective automation summary (for example `Retest: auto`, `Research: manual`);
- active-run badge and current step;
- last agent/controller activity and a one-line most-recent-message summary;
- prominent waiting-human/blocked/resource-exhausted/stale indicator;
- one link to patch detail.

Merged or abandoned patches retain terminal watch-state behavior and do not
start new runs.

### Patch detail

Sections:

1. Current Gerrit/review/CI observation and exact revision.
2. Effective policy, inherited defaults, pending edits, and safety budgets.
3. Active run card: state, reason, step, model, execution profile, runtime,
   last qualifying activity, timeout countdown, checkout, session-created VMs,
   and an explicit capability boundary. The boundary text follows the
   *capability profile the run was started with*, derived from its immutable
   request event -- never the session profile, which says `engineering` for a
   manual investigation that holds no write capability at all. An engineering
   run reads: the run has a host shell, the installed LLM tools, and real
   service credentials, and makes its own Gerrit and CI writes.
4. Pending human question or blocker, displayed above routine logs.
5. Conversation and message composer with a bounded recent tail, expandable
   history, delivery state, **Send guidance**, and interrupt option.
6. Timeline of observations, triggers, policy decisions, messages, worker
   reports, actions, errors, and recovery events.
7. Artifacts and bounded/raw logs.
8. Prior runs, including resource-exhausted, stale, and cancelled runs.

### Controls and safety in the UI

- Separate **refresh observation** from **start run**.
- Explain why an action is enabled, disabled, suppressed, or waiting.
- Preview the effective policy and exact behavior change before saving. The
  automatic-policy confirmation must name what the handlers will really do,
  with the operator's own credentials, rather than listing capability switches
  that no longer exist.
- Show `disabled`, `advise`, `approval`, or `automatic` beside the
  deterministic test policy, and `off`/`repair`/`simple`/`all` beside the
  handlers; never rely on a generic "enabled" label for write behavior.
- Provide a policy **dry-run** that evaluates the current patch without acting.
- Require confirmation for interrupt, cancel, destructive cleanup, enabling an
  automatic policy, enabling the global gate, or enabling an autonomous lane.
  GET must never mutate; every destructive control is a POST behind a
  display-only confirmation page.
- Use text plus color/icons; never encode state by color alone.

## Recovery, timeouts, and reconciliation

On startup and periodically:

1. Mark non-terminal runs `recovering` internally while keeping their last
   user-facing state visible.
2. Compare each run with its Claude runner session, action attempts, checkout
   allocation, pool claim, and prefix-matched LTVM inventory.
3. Adopt a healthy running Claude process rather than starting another.
4. If a host died, resume only when policy permits and no action is ambiguous;
   otherwise mark blocked with a specific recovery action.
5. Reconcile every `executing` action against remote state before retry.
6. Verify revision freshness.
7. Continue terminal cleanup, retain explicit debugging resources, and surface
   any prefix-matched resources that no longer belong to a healthy session.
   A tick that fails must fail for one tick only: the reconciliation loop is
   guarded as a whole, because one transient `database is locked` escaping it
   used to kill the supervisor thread and stop dispatch permanently while the
   web app kept serving.
8. Record the recovery decision as an event.

Use distinct limits for:

- triage wall-clock runtime (20 minutes by default);
- engineering inactivity (30 minutes by default);
- current command/step deadline;
- absolute agent-session runtime (48 hours, nonextendable);
- Claude turn and continuation count;
- adapter retries/backoff;
- external action count;
- retained log/artifact size.

No-output alone is not proof of a hung build or test. For the engineering
profile, any stream event -- a tool call, a tool result, or a line of the
agent's own text -- resets the inactivity clock, which is what makes one long
`ltvm build lustre` or a single `auster` invocation safe. The prompt says so
explicitly, so that a long silent stretch of thinking is a known hazard rather
than a surprise. Never use self-matching process polls.

Retry only operations known to be safe and idempotent. Exhaustion becomes a
visible `blocked` or `failed` outcome, not an invisible loop.

## Notifications and reports

- Immediate notification for `waiting_human`, LTVM resource exhaustion, agent
  runtime/inactivity timeout, a revision blocked for modifying agent
  instruction files, repeated failure, emergency stop, and ambiguous external
  action. Each alert is sent once per session and cause.
- Active engineering sessions send a status reminder after two hours and every
  two hours thereafter. Reminders include bounded recent messages and an
  authenticated link to the run's confirmed Kill-session control.
- Daily email summarizes observations, runs, actions, errors, and unanswered
  questions.
- Test-email remains available and contains a bounded recent summary.
- Notification delivery is itself idempotent and logged.
- Email is a notification channel, not an authorization channel; replies do
  not control runs until an authenticated reply workflow is separately built.

## Security and trust boundaries

- Bind locally until deployed behind authenticated TLS (for example a reverse
  proxy on Mulberry Server). Add application sessions/CSRF protection before
  accepting remote commands.
- Keep private config mode `0600`; never show credentials in the UI, prompts,
  logs, subprocess arguments, or artifacts.
- Treat patch code and all remote text as attacker-controlled data. Say so in
  the prompt, and where the harness or a tool would treat repository content as
  *policy* rather than data, refuse the revision instead of relying on the
  sentence: see invariant 11 for `CLAUDE.md`, `AGENTS.md`, `.claude/`, and a
  root `.env`/`.envrc`.
- Do not expose generic shell execution through dashboard fields. The dashboard
  has no command box; operator text reaches a run only as guidance or as an
  answer to a question, and only the agent decides what to run.
- Build and test patch code in an owned LTVM guest. This is a prompt rule for a
  `full` run, not an enforced boundary -- the agent has a host shell and
  `ltvm build` runs on the host regardless. State it as a rule the agent is
  asked to follow, never as a guarantee.
- Redact tokens, cookies, passwords, SSH material, and sensitive environment
  values before storing tool output.
- Record actor identity for operator commands once multi-user access exists.
  Today every control records `local-dashboard-user`, which is honest for a
  single-operator localhost deployment and is not a real identity.
- Use least-privilege service and container identities. Not done: an agent runs
  as the operator with passwordless sudo. This is the largest open item in this
  section and the reason containerization is the next safety milestone.
- Back up the four SQLite databases and keep event/artifact retention
  configurable.

## Phased implementation plan

Each phase is deployable and has a hard exit test. Later controls may be shown
disabled as design previews, but must not imply functionality.

### Current implementation checkpoint

The phase numbering below is kept because it is how the work was sequenced and
how the code still reads. Its capability model, however, was rewritten by the
2026-09-07/08 carve-down, so read each phase note before its build list.

**Phase 0A** is complete: durable observer and scheduler, SQLite schema and
migrations, normalized history and error log, live resource sampling, and both
automation flags defaulting off.

**Phase 0B was implemented and then deleted.** It supplied the worker profile,
run envelope, environment attestation, admission `doctor`, and the
`host-unsandboxed-mac-v1` compatibility profile, and it blocked a launch until
an attestation was persisted. All of it -- `worker_contract.py`,
`worker_doctor.py`, `worker_admission_views.py`, `worker_profiles/`,
`worker_schemas/`, `pw_worker.py`, and `WORKER_ENVIRONMENT_CONTRACT.md` -- was
removed. What survives is the part that turned out to be load-bearing on its
own: private per-run directory layouts, generated and hashed instruction text
written to `work/input/INSTRUCTIONS.md`, and durable run provenance. Admission
is replaced by `pw-configure` (set the host up once) and `pw-doctor` (say
whether it still is), neither of which gates a launch.

**Phase 0C** is complete and is still the shape of every read-only run: an
operator-started, exact-revision investigation with a reconnectable native
runner, durable events and messages, waiting-human and live guidance, confirmed
stop/kill controls, restart adoption, structured completion, timeouts and
reminders, and owner-scoped cleanup. It grants no Gerrit, CI, Jira, LTVM,
source-editing, shell, or upload capability, and that remains true.

**Phase 1** (deterministic automatic retest) is complete and unchanged. It
keeps the mechanical decision and the idempotent remote action in the
controller, with its own durable trigger/outbox/event ledger and no Claude
session. **Phase 2** (read-only unknown-failure research) is complete and
unchanged: `Read`/`Glob`/`Grep`, an immutable evidence bundle, citations
checked against captured evidence, and no external write. The follow-on
existing-Jira association and retest remain two separately confirmed
controller actions with no agent authority. These two phases are the only
remaining places where the original controller-owned-write model still
describes the code, and they still describe it exactly.

**Phase 3** is where the model changed. Phase 3A's tool-limited source-edit
profile, Phase 3B's MCP guest broker, and Phase 3C's controller-owned upload
path were all replaced by one thing: an engineering run started with
`capability_profile="full"`. The agent edits the checkout with an ordinary
shell, creates and drives its own guests with ordinary `ltvm` commands, and
pushes its own patchset with the `gerrit` CLI. `gerrit_upload.py`, the upload
ledger, the private staging checkout, the one-use POST binding, the
`evidence_role` upload gate, and the per-command guest ledger are gone. The
sentence "Claude never receives Gerrit credentials" is now false: it receives
all of them, because it runs as the operator.

What is left of Phase 3's discipline is real but narrower: the checkout is
still exact-revision-pinned and private to the run, the diff is still captured
by the controller from the checkout rather than taken from the agent's word,
`changed_files` is still cross-checked against that diff, and the run-start
confirmation is still a signed, expiring, exact-revision act.

**Phase 4A** (review handling) and **Phase 5A** (Jenkins build repair) are
implemented, in both manual and confirmed-standing-policy form, with their
immutable snapshots and digest checks intact. Their publication step changed
with Phase 3: the agent posts the replies and uploads the patchset itself.
"Review replies remain drafts" is no longer true of either flow.

**Phase 5B** was implemented and then deleted. Its two controller-owned writers
-- exact Gerrit review-reply posting and exact Jenkins retrigger -- their kill
switches, durable claims, and reconciliation-only ambiguity handling went with
`gerrit_reply.py`, `jenkins_retrigger.py`, and `external_action_views.py`.
Reply posting moved into the agent. Jenkins retrigger simply no longer exists
in any form, and the prompt forbids the agent from performing one.

**Phase 6A and the first Phase 6B lane** are implemented and unchanged. The
first lane grants zero agent runs, which is why it survived the carve-down
untouched.

The containerization/isolation gate remains unmet, and unlike before, broad
code execution and automatic external writes have shipped without it. Every run
is an unsandboxed host worker. That gate is still the entry criterion for an
autonomous lane that starts an agent, which is why no such lane exists.

### Phase 0A: durable observer

Build:

- SQLite schema/migrations for patches, revisions, observations, policies,
  triggers, events, notifications, and service cursors;
- migrate seed-file/in-memory patches without losing the current UI;
- independent scheduler/observer and manual Refresh All command;
- persistent normalized history and error log;
- worker-host resource sampler and top-level memory summary with freshness and
  collection-error states;
- global/patch automation flags, both off;
- service health and last-poll display.

Exit criteria:

- watch list/history survive restart;
- no browser is required for scheduled polling;
- concurrent refresh requests do not duplicate observations/triggers;
- a changed patchset is represented as a new exact revision;
- current tests plus migration/restart tests pass;
- host memory totals are sourced, timestamped, and do not confuse configured
  guest memory with physical host use.

### Phase 0B: worker environment admission (built, then removed)

**This phase was implemented and then deleted in the 2026-09-07/08 carve-down.**
The build list and exit criteria below are kept as the record of what it was,
because a reader who remembers admission blocking a launch is not wrong about
the past. Nothing in it is a description of the current system.

Built:

- schemas for the worker profile, per-run envelope, and environment
  attestation defined in `WORKER_ENVIRONMENT_CONTRACT.md`;
- the truthful `host-unsandboxed-mac-v1` compatibility profile;
- private logical run directories and generated, hashed portable worker
  instructions rather than a dependency on the operator's home directory;
- `pw-worker doctor` with offline tool/version, checkout, path, resource,
  broker/report-channel, and optional LTVM health checks;
- run persistence for profile/hash, environment instance, attestation,
  instruction hash, and broker session ID; and
- dashboard admission state, failed-preflight reason, provenance, and visible
  isolation/network profile.

Its exit criteria were:

- Claude is never started before a successful persisted attestation;
- missing tools, incompatible runtime versions, dirty/wrong checkouts,
  insufficient resources, and unavailable capabilities each block with a
  precise redacted reason;
- a worker may rely only on declared logical paths and capabilities, not
  Patrick-specific paths or dotfiles;
- the current host is labeled **Unsandboxed host worker** and is eligible only
  for manual read-only work; and
- manifest/schema compatibility, version drift, sanitization, and restart
  behavior have automated tests.

What replaced it, and what did not:

- Private run directories and hashed generated instructions survived, and are
  still how a run's prompt is produced and recorded.
- Admission became setup. `pw-configure` writes the five private credential
  files at mode `0600` without echoing secrets or clobbering existing values;
  `pw-doctor` checks those files, the required binaries, the one-time
  background-agent disclaimer, the checkout pool, and `CLAUDE.md`
  discoverability, separating blocking from advisory findings. Neither runs
  before a launch, and neither can stop one.
- The portability requirement was abandoned rather than met. A worker now
  depends on the operator's home directory, dotfiles, credential files, and
  installed tools by design.
- The only surviving pre-launch block is the agent-instruction refusal in
  invariant 11, which answers a different question: not "is this host fit to
  run an agent" but "is this revision fit to be handed to one".

### Phase 0C: run control and manual read-only agent

Build:

- run/event/message/action/resource schema;
- dispatcher and startup reconciler;
- native `ClaudeRunner` over the structured stream protocol;
- one manual **Investigate** run pinned to a revision with read-only tools;
- run detail page, conversation, waiting-human question, message delivery,
  pause/interrupt/cancel/resume/follow-up controls;
- active-session list with process-tree memory, recent-message summary/tail,
  Send-guidance and confirmed Kill-session controls;
- triage/engineering execution profiles, qualifying-activity tracking,
  timeout termination, owner-scoped cleanup, immediate timeout email, and
  visible countdown/failure reason;
- two-hour engineering reminders and the nonextendable 48-hour absolute cap;
- structured worker report validation;
- clearly visible unsandboxed-worker label.

Exit criteria:

- one patch cannot obtain two active runs under race;
- human messages deliver exactly once and their state is visible;
- waiting-human survives service restart and resumes only after a valid answer;
- terminal/stale runs cannot be silently resumed;
- a live Claude runner session is adopted after Patch Watcher restart;
- simulated 20-minute triage runtime and 30-minute engineering inactivity each
  fail exactly once, email exactly once, and clean only run-owned resources;
- waiting-human/external and paused/blocked time does not consume an inactivity
  interval;
- reminder intervals survive restart without duplicate email, and the 48-hour
  cap cannot be extended;
- an email Kill-session link cannot mutate state through GET and reaches the
  authenticated confirmation flow;
- no external write capability is present.

### Phase 1: deterministic automatic retest

**Implementation status: complete.** The checked-in controller, Maloo adapter,
pure policy evaluator, background observer, durable automation ledger,
dashboard confirmations/timeline, and notification projections implement this
phase. External execution still defaults off globally and per patch.

Build:

- full top-level review gate and Maloo test flow;
- fingerprinted triggers and deterministic controller runner;
- linked-bug and pending-retest checks;
- action outbox, idempotency, ambiguous-call reconciliation;
- per-patch automatic/manual test policy, budgets, dry-run preview;
- `waiting_external` result polling and dashboard timeline;
- immediate/daily notifications.

Exit criteria:

- the same failure cannot produce two retest requests under repeated polls,
  restarts, or concurrent refreshes;
- a non-Maloo `-1`, pending retest, unknown failure, disabled policy, and stale
  patchset each suppress action with the correct visible reason;
- simulated crash during request reconciles remote state before retry;
- revision changes between planning and execution prevent the request;
- no Claude session is required for the mechanical path.

### Phase 2: unknown-failure research agent

Implemented:

- automatic or manual investigation trigger for failures without linked bugs;
- controller-captured, immutable Maloo evidence plus a pinned source checkout;
- evidence report, recommendation, and human escalation;
- runtime/turn budgets and bounded artifact/log display;
- prompt-injection defenses and controller-owned tool access.

The agent gets no Gerrit, Maloo, Jira, Jenkins, LTVM, shell, or file-write
tool: `read_only` really means `Read`, `Glob`, `Grep` under the hardening
flags `--safe-mode`, `--restricted`, `--strict-mcp-config`, and
`--disable-slash-commands`, in an environment with the service credentials
scrubbed, and its checkout and evidence directory
are made read-only on disk. This is the one phase whose capability statement
the carve-down did not weaken. Identical evidence deduplicates across retries
and restarts. Automatic
triggering has its own confirmed per-patch policy and budget and also respects
the global execution switch. Existing-Jira association and the subsequent
retest are a separate two-step operator-approved controller workflow: each
action is revision-pinned, independently confirmed, remotely reconciled, and
never retried after an ambiguous outcome.

Exit criteria:

- agent cannot retest, comment, vote, edit, upload, or access ungranted secrets;
- every factual recommendation links to captured evidence;
- malformed/missing reports block cleanly;
- human can message, redirect, or stop the run from the dashboard;
- a newer patchset stales the run before its recommendation can be acted on.

### Phase 3: isolated execution foundation

**Superseded by the 2026-09-07/08 carve-down.** Phase 3 was deliberately split
so that source editing, execution, and publication would not arrive as one
oversized capability grant. They now arrive as exactly that: one
`capability_profile="full"` launch that grants all three at once. The three
sub-phases are kept below as the record of what was built and what was removed,
each with a note on what survives. Do not read them as current constraints.

#### Phase 3A: private source-edit runs

Built:

- full independent checkout lifecycle;
- exact-revision, two-step operator confirmation before a source-edit worker
  starts;
- a source-edit Claude profile limited to `Read`, `Glob`, `Grep`, `Edit`, and
  `Write` inside the dedicated checkout, with no Bash, MCP, browser, service
  credentials, or Gerrit write capability;
- capture the actual Git diff/status independently of the agent and retain
  them as immutable, digest-addressed evidence;
- accept desired validation only as a bounded argv manifest. The request is
  inert until a later controller stage admits and executes it;
- dashboard progress, messages, exact revision, checkout ownership, evidence,
  and explicit **Gerrit upload disabled** status.

Now: the checkout lifecycle, the signed exact-revision run-start confirmation,
and the independently captured diff all survive and are still enforced --
`changed_files` in the report must equal what the controller finds in the
checkout, or the run fails `worker_report_invalid`. The tool-limited
`source_edit` profile survives only as an unused branch in
`ReadOnlyRunSpec.validate`; nothing constructs it. `validation_requests`
survives as optional planning evidence with an `evidence_role` tag, but nothing
executes it, so it is now a record of what the agent thought should be run
rather than a request awaiting admission. **Gerrit upload disabled** is no
longer a status the UI can truthfully show.

#### Phase 3B: session-owned LTVM validation

Built:

- consume LTVM's existing session-scoped `owner_id` inventory and implement
  reconciliation;
- inventory all current LTVM VMs, associate matching owner IDs beneath their
  sessions, and show configured guest memory separately from measured host
  process use;
- agent-driven, on-demand VM/cluster creation with target
  list/fetch/validate guidance and recorded VM environment;
- structured LTVM resource-exhaustion reporting, email, partial-resource
  cleanup, per-patch cooldown, and operator retry controls;
- one explicitly confirmed, run-level capability for open-ended command
  execution inside exactly owner-matched guests; commands are audited as they
  execute and do not require per-command approval or an allowlist;
- no arbitrary host command box: dashboard messages steer Claude, while all
  build, test, and diagnostic shell text executes through the guest broker;
- artifact collection, cleanup, quarantine, and orphan reconciliation;
- rootless worker container prototype and network-profile display.

The controller, not the web request and not an untrusted repository file,
granted the session capability after the operator confirmed the engineering
run. The exact-owner broker then admitted VM lifecycle calls and forwarded
open-ended commands only into that session's guests, rechecking ownership and
recording each result. No Phase 3 command ran inside the Patch Watcher web
service or the Claude host environment.

**The broker is gone.** `ltvm_mcp_server.py`, `ltvm_guest_exec.py`, and
`ltvm_ssh_transport.py` were deleted in the 2026-09-07/08 carve-down, together
with the per-command guest ledger they wrote (`pw_validation_command_claim`,
now a no-op migration step). The agent runs `ltvm` from its own shell, and the
controller neither sees nor grades an individual guest command. The exact-owner
inventory check before confined literal-IP SSH, and the plan for an atomic
LTVM owner-checked exec RPC, no longer have a caller.

What survives is bookkeeping and its one useful consequence, revocation. The
"guest capability" is still created and approved at run start and closed the
moment the session terminalizes, and it is still the row that carries the
capacity cooldown: a `resource_exhausted` terminal report finishes the
validation attempt as exhausted, which is what writes the cooldown and blocks
the next VM-capable run for that patch. VM inventory, per-session grouping,
guest-versus-host memory separation, artifact collection, cleanup, quarantine,
and orphan reconciliation all survive, with ownership by name prefix rather
than by owner token. The rootless worker container prototype was never built.

#### Phase 3C: separately gated Gerrit upload (built, then removed)

**This phase was implemented and then deleted in the 2026-09-07/08 carve-down.**
It was a distinct upload capability, disabled by default and never implied by
permission to edit, build, or test. Upload required an exact-current-patchset
recheck, a reviewable diff and test evidence, and a controller-generated upload
plan; for review and build repair the single run-start confirmation
preauthorized one qualifying upload, with no second approval, and an ambiguous
outcome reconciled with Gerrit before any retry. It used a private durable
upload ledger and a fresh controller-only staging checkout reconstructed from
the immutable diff artifact after the worker checkout had been deleted.
Validation requests carried an explicit `evidence_role`, and upload required at
least one successful `test` role rather than inferring a test from a command
label. Preparation verified the old Gerrit Change-Id and recorded the amended
commit SHA before dispatch, under a one-use durable binding covering the old
revision, diff, test evidence, and proposed commit.

`gerrit_upload.py`, the upload ledger, the staging checkout, the one-use
binding, and the `evidence_role` upload gate are all gone. **The agent pushes
its own patchset with the `gerrit` CLI**, on its own judgement of whether it
has a nonempty diff and successful build and test evidence, using the
operator's credentials. The kill switch this phase existed to provide no longer
exists.

Two things survive from it, and both are ordering constraints rather than
gates. The uploaded patchset still becomes a new observed revision, which
stales the run that produced it. And because the controller derives the diff
from the checkout only *after* the terminal report arrives, the prompt requires
the agent to commit and push, then `git reset --soft` back to the pinned
revision so the tree still carries the change as an uncommitted diff, and only
then report -- otherwise its own upload would erase the evidence of what it
did.

The original exit criteria, none of which now hold as stated:

- untrusted build/test code does not execute in the web service or host worker
  context;
- two runs cannot share a writable checkout or owner-attributed VM;
- cancellation and restart do not destroy unrelated VMs;
- environment and test results are reproducible from the run manifest;
- a simulated LTVM capacity failure creates no retry loop, purges only
  owner-matched partial resources, emails once, and suppresses that patch until
  cooldown or confirmed manual retry;
- restricted-egress behavior is tested and honestly represented.
- Phase 3A/3B completion does not enable upload; 3C has its own capability,
  confirmation, audit, and kill switch.

Of these, the checkout-sharing and unrelated-VM criteria still hold (the pool's
partial unique index and the prefix baseline respectively), and the capacity
criterion holds with prefix-matched cleanup. The first, fourth, sixth, and
seventh do not: patch code executes on the host, no run manifest reproduces an
environment the controller never observed, there is no egress restriction to
test, and editing now does enable upload.

### Phase 4: review handling and proposed edits (implemented)

Built:

- **Handle simple comments** and **Handle all comments**, manual and as a
  confirmed standing automatic policy;
- exact current-patchset review-comment snapshot, immutable and digested;
- an engineering run against that snapshot;
- per-comment disposition and the controller-captured diff as immutable
  artifacts;
- one explicit run-start approval that is the run's whole authority;
- fail-to-human behavior for ambiguity, nontrivial work in simple mode, stale
  comments/revisions, failed validation, or uncertain publication.

The snapshot discipline is enforced and worth stating precisely. Each thread in
the snapshot is one target, and the target comment is the **last** entry of
that thread's `comments` array -- the newest comment, usually a follow-up
rather than the one that opened the thread. The report must key exactly one
`comment_results` entry to each of those comment IDs, that set and no other, in
every report including `needs_input`; the controller compares the sets and
fails the run on any difference. A `complete` report may not contain a deferred
comment, and in `simple` mode may not contain a `nontrivial` or `ambiguous`
assessment. The reported `review_mode` and `review_snapshot_sha256` must match
the run's own.

Exit criteria, with current status:

- "simple" mode escalates any ambiguous/nontrivial comment without attempting
  it -- **holds**, enforced on the report;
- "all" mode attempts broadly but still escalates uncertainty -- **holds**;
- ~~neither engineering mode posts review replies as part of patchset upload;
  replies remain drafts until the separate Phase 5B action~~ -- **no longer
  true.** The agent posts each reply itself, on the target comment, with the
  `gerrit` CLI, and uploads the patchset itself. There is no draft stage and no
  separate confirmation. A run with no guest capacity is told it cannot reach a
  complete result and must not post or upload at all, which is the only
  remaining case where replies stay unposted;
- a qualifying run uploads one new patchset without a second approval step --
  **holds in effect**, but through the agent rather than a controller writer,
  so the kill switch, idempotency ledger, and reconciliation-only ambiguity
  handling it named no longer exist;
- all edits map to a specific comment and pinned revision -- **holds**;
- ~~containerization/isolation gate is met before executing patch code~~ --
  **not met.** Patch code executes unsandboxed on the host. This criterion was
  set aside rather than satisfied.

### Phase 5A: Jenkins build-failure repair (implemented)

Build:

- expose a manual action, plus explicitly confirmed standing automatic policy,
  only for one completed failed Jenkins build belonging to the exact current
  Gerrit patchset; automatic starts also require the independent global gate;
- require one confirmation that binds the run to the immutable change,
  patchset, revision SHA/ref, Jenkins job/build, and complete bounded-log
  snapshot digest;
- give the worker a dedicated full checkout and open-ended commands in the LTVM
  guests it owns. (Originally: guests carrying the exact session owner, with
  Gerrit and Jenkins credentials, host command execution, and publication kept
  in the controller. None of that holds -- the run has a host shell and the
  operator's credentials, and ownership is by `co<N>-` name prefix.);
- require a structured classification of `patch_caused_fixed`, infrastructure,
  transient, unrelated, or ambiguous, plus an independently captured nonempty
  diff and successful explicitly tagged build and test evidence;
- for `patch_caused_fixed` only, treat the run-start confirmation as authority
  for one patchset upload without a second approval. The agent performs that
  upload itself with the `gerrit` CLI;
- after success, refresh and observe the new patchset as a new revision, which
  stales the completed run.

Exit criteria, with current status:

- only `patch_caused_fixed` with a nonempty diff and successful build and test
  evidence can reach `complete` -- **holds on the report**: the controller
  rejects a `complete` report whose classification is anything else, rejects a
  `jenkins_snapshot_sha256` or `build_id` that does not match the immutable
  snapshot, and refuses `patch_caused_fixed` entirely for a run with no guest
  capacity. It does not hold on *publication*, because publication is no longer
  a controller step it can withhold: the agent decides whether its evidence
  qualifies, and pushes;
- infrastructure/transient/unrelated/ambiguous results, no diff, failed
  validation, and resource exhaustion fail to a human without widening
  capability -- **holds**, and a settled negative verdict is recorded as
  `failed` carrying the classification and diagnosis rather than discarded;
- ~~an upload never targets or rebases over an unexpected patchset and the
  worker never receives service credentials~~ -- **not true.** The worker
  receives every service credential, and only its prompt tells it which change
  to push to;
- ~~request, report, diff, evidence, staging commit, and upload identities are
  durable and one publication binding covers the exact run, change, patchset,
  revision, diff, and validation evidence~~ -- **partly.** Request, report,
  diff, and evidence identities are still durable; the staging commit and the
  one-use publication binding were deleted with Phase 3C;
- ~~a claimed or ambiguous push is reconciled against Gerrit after completion
  callback or restart and is never blindly repeated~~ -- **no longer
  implemented.** There is no push claim to reconcile. An agent that pushes and
  then dies leaves nothing for the controller to reconcile against; the next
  refresh simply observes a new patchset.

### Phase 5B: initial wider Jenkins and Gerrit writes (built, then removed)

**Both actions were implemented and then deleted in the 2026-09-07/08
carve-down**, along with `gerrit_reply.py`, `jenkins_retrigger.py`, and
`external_action_views.py`. They were:

- **Post review replies:** after a successful exact-snapshot review run and
  successful patchset upload, an operator could confirm the immutable reply
  artifact. A reply stayed bound to the historical original revision and the
  exact comment ID and file/line/range from that snapshot, and was not rebound
  to the newer patchset the handler produced. Immediately before POST the
  controller verified that original revision and exact unresolved comment and
  location, using a deterministic Gerrit tag and a durable pre-write claim,
  with uncertainty reconciliation-only and never a blind retry.
- **Retrigger Jenkins:** an operator could confirm one retrigger of the exact
  completed failed parent build, bound to the current revision, Gerrit ref,
  project/branch, and failure-snapshot digest, checking for a newer equivalent
  build before dispatch, spending one action budget, claiming the write
  durably, and reconciling only against a newer build with the same exact
  Gerrit parameters. The exact failed build was a terminal one-use dispatch
  identity.

Where each went:

- **Reply posting moved into the agent.** The review-handling prompt now tells
  it to post each reply on the target comment itself with the `gerrit` CLI, as
  part of the same run that made the edits. The historical-revision binding,
  the location recheck, the deterministic tag, the pre-write claim, and the
  independent kill switch are all gone; the agent is simply told which comment
  each reply belongs on.
- **Jenkins retrigger was removed outright and not replaced.** There is no
  retrigger in the controller and none in the agent: `ENVIRONMENT_POLICY`
  forbids any Jenkins write -- no build, retrigger, or cancel -- and Jenkins
  access is read-only through the failure-snapshot client.

There are therefore no separate external-action capabilities and no separate
kill switches. Confirming a patch's automatic policy starts the engineering
run, and that run's own credentials are the authority for everything it does;
the confirmation page says exactly that rather than listing switches that stay
off.

Jenkins aborts and configuration changes, general Gerrit messages and votes,
and any automatic write beyond what an engineering run performs remain future
work. If they return as controller actions, each still needs its own narrow
capability, approval rule, exact-state binding, idempotency contract,
reconciliation behavior, budget, audit trail, and escalation path -- and that
list is now also the specification for whatever replaces prompt-only
enforcement of the agent's own writes.

### Phase 6: autonomous lanes

Phase 6A and the first narrow Phase 6B lane are implemented:

- code-owned, immutable named/versioned definitions and pure evaluators;
- CAS-protected global/per-project/per-patch controls, all disabled by default;
- signed one-use confirmation for enabling and immediate kill-switch disable;
- exact-revision decision records, bounded recent outcomes, and side-effect-free
  historical replay;
- a first `deterministic-test-retest` version 1 lane which wraps the existing
  Maloo retest outbox and allows one remote write per exact revision; and
- final switch, definition, standing-policy, primary-gate, project, patch, and
  revision revalidation before the existing Maloo writer is called.

Lane enrollment is an additional restriction, never an authority grant. The
first lane requires the already-confirmed automatic/deterministic standing
policy and primary global automation gate. It has zero agent-run budget and no
Gerrit, Jenkins, Jira, patch-upload, checkout, or LTVM capability. Unenrolled
patches retain their existing behavior, so adopting the framework cannot
silently change established workflows.

Exit criteria:

- a lane cannot expand its own eligibility or capabilities;
- dry-run/replay demonstrates expected decisions over historical data;
- every autonomous result has a complete audit trail;
- failures, uncertainty, policy drift, and unexpected external state fail to a
  human rather than improvising.

### Future parallel surface: ticket and free-form engineering

This work is intentionally not assigned to the patch-shepherding phases yet.
Before enabling it, design and build:

- an **Engineering work** page with separate Jira-ticket and free-form prompt
  start forms;
- a versioned work-item envelope that records input kind, immutable original
  input, submitter, selected project/repositories, source baselines,
  acceptance criteria, capability policy, budget, and approval policy;
- Jira issue retrieval and snapshotting, including instance identity and
  issue-version/change detection;
- an explicit discovery or human-confirmation step when a ticket does not
  unambiguously identify its repositories, branch, expected deliverable, or
  test environment;
- work-item concurrency rules and repository/patch write locks, since one
  ticket may create multiple patches and multiple tickets may mention the same
  repository;
- first-class outputs linking proposed commits, Gerrit changes, tests,
  artifacts, questions, and final outcome back to the originating work item;
- distinct prompt-injection boundaries for Jira content and free-form task
  text; and
- dashboard history and controls equivalent to patch runs: current state,
  recent messages, resource use, guidance, waiting-human questions, stop,
  retry, follow-up, and cleanup.

Initial acceptance should be read-only planning from a Jira ticket, followed
by an isolated, approval-gated implementation lane. Free-form engineering
should remain disabled until its required repository/environment selection
and capability controls are explicit and tested. Neither mode should inherit
automatic Gerrit, Jira, CI, or upload authority merely because the requested
task mentions such an action.

## Test strategy

Build a fake-adapter test harness before enabling actions. Required suites:

- table-driven evaluator and state-transition tests;
- SQLite migration, constraint, transaction, and crash-restart tests;
- duplicate poll/trigger/run/action races;
- stale patchset at every transition and immediately before writes;
- Claude runner lost process, idle turn, invalid report, needs-input,
  interrupt, and resume behavior;
- triage wall-clock and engineering inactivity boundaries, qualifying versus
  irrelevant activity, suspended waiting-state clocks, one-time notification,
  stop escalation, and owner-scoped timeout cleanup;
- human message idempotency, stale question, queued delivery, and terminal-run
  follow-up behavior;
- external action success/failure/timeout/ambiguous/reconciliation;
- VM name-prefix attribution, pre-run baseline capture and its unprovable
  case, terminal purge, orphan, cleanup retry, cleanup abandonment, retention,
  and unrelated-VM protection;
- host/session/VM memory attribution, process-tree accounting, stale samples,
  missing telemetry, no-double-count totals, and unassociated VM display;
- LTVM resource exhaustion versus ordinary create failure, email idempotency,
  partial-cluster cleanup, cooldown expiry, and manual override;
- two-hour reminder cadence across restarts, bounded message excerpts,
  authenticated kill-link behavior, confirmed human kill, and the 48-hour
  absolute cap;
- capability-profile selection: that `read_only` really is
  `Read`/`Glob`/`Grep` under the hardening flags with service credentials
  scrubbed, that `full` is chosen only for the three engineering request
  kinds, and that the UI's boundary text follows the capability profile rather
  than the session profile;
- the agent-instruction refusal: a revision adding or editing `CLAUDE.md`,
  `AGENTS.md`, `.claude/*`, or a root `.env`/`.envrc` blocks the run, a shallow
  clone falls back to the weaker tree check and says so, an unanswerable check
  blocks, and a non-repository directory does not;
- secret-redaction, prompt-injection, CSRF, and auth tests;
- event replay: rebuild current projections from a recorded event sequence;
- end-to-end dry-run with fake Gerrit/Maloo/Jenkins/JIRA/Claude/LTVM adapters.

Production integrations get explicit opt-in integration tests, and the two that
exist -- `integration_check.py` and the Chrome-driven `browser_check.py` -- are
read-only against Gerrit: they watch deliberately dormant changes and never
post, vote, upload, or start an agent. The default test suite performs no
network requests, sends no mail, changes no Gerrit state, and creates no VM.

There is now a class of behavior this strategy cannot cover. When the boundary
is a sentence in a prompt rather than a capability the controller withholds,
no test can assert that an agent stayed inside it. The tests can and do assert
that the *prompt says* the right thing and that the controller's own checks
fire; whether the agent obeys is observed in production, on a single operator's
host, by that operator. That asymmetry is the strongest argument for
containerization.

## Implementation decisions to settle before each phase

These should not block Phase 0, but each must be resolved before its dependent
capability is enabled:

- service manager on Mac versus Linux deployment target;
- authenticated remote access design for Mulberry Server;
- exact native Claude runner interface and structured-event contract;
- Maloo's remote identifiers and best reconciliation query for ambiguous
  retest calls;
- artifact retention limits and backup location;
- agent model/effort and budget accounting source;
- container runtime and model-endpoint network strategy -- now the top open
  item, since it is the only thing that would turn the prompt's rules into
  enforced ones;
- credential broker design, if credentials are ever to be withheld from an
  agent again;
- retained-VM expiry and artifact collection policy before automatic purge;
- Gerrit identity and approval policy for eventual automated writes.

No unresolved decision should be hidden behind an enabled checkbox. The
dashboard should show the capability as unavailable and explain the missing
prerequisite.
